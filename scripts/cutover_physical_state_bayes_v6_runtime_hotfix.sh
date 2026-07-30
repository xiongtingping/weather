#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Physical X/S and the market target use the frozen ZUUU resolution-local
# date.  This is distinct from operations_date_et used for trading statistics.
EVENT_DATE="${PHYSICAL_STATE_BAYES_V6_EVENT_DATE:-$(TZ=Asia/Shanghai date +%F)}"
DECISION_AS_OF="${PHYSICAL_STATE_BAYES_V6_DECISION_AS_OF:-$(date -u +%Y-%m-%dT%H:%M:%S.000Z)}"
NEXT_EVENT_DATE="$(python3 - <<'PY'
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
print((datetime.now(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=1)).isoformat())
PY
)"
NEXT_DAY_HANDOFF_AS_OF=""
ACTIVE_ACTIVATION="$ROOT/config/production_probability_runtime_activation.json"
CANDIDATE_ACTIVATION="$ROOT/config/production_probability_runtime_activation.v6_candidate.json"
BUNDLE_CONTRACT="$ROOT/config/production_probability_runtime_bundle.v6_candidate.json"
ACTIVE_DRAIN_RUNTIME="$ROOT/production_probability_v6_canonical_event_drain_runtime.py"
ACTIVE_INGRESS_RUNTIME="$ROOT/production_probability_v6_amsc_archive_ingress.py"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOCAL_STAGE="/private/tmp/archive.pre-v6-runtime-hotfix.$STAMP.db"
LOG="$ROOT/data/logs/physical_state_bayes_v6_runtime_hotfix_cutover.log"
ROLLBACK_DIR="$(mktemp -d)"
LAST_STEP="initialize"
SERVICES_PAUSED=0
ACTIVATION_MUTATED=0
SOURCE_MUTATED=0
DB_MUTATED=0
NEW_V6_COMMITTED=0
CUTOVER_COMPLETE=0
SUDO_KEEPALIVE_PID=""
MONITOR_DOMAIN="gui/$(id -u)/com.weather.monitor.daemon"
MONITOR_PLIST="$HOME/Library/LaunchAgents/com.weather.monitor.daemon.plist"
CANDIDATE_FILE_SHA256=""
RUNTIME_MANIFEST_SHA256=""
BUNDLE_DIR=""

mkdir -p "$ROOT/data/logs"
exec > >(tee -a "$LOG") 2>&1

step() {
  LAST_STEP="$1"
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] step=$LAST_STEP"
}

verify_candidate_frozen() {
  test -n "$CANDIDATE_FILE_SHA256" -a -n "$RUNTIME_MANIFEST_SHA256"
  test "$CANDIDATE_FILE_SHA256" = "$(/usr/bin/shasum -a 256 "$CANDIDATE_ACTIVATION" | awk '{print $1}')"
  EXPECTED_RUNTIME_MANIFEST_SHA256="$RUNTIME_MANIFEST_SHA256" python3 - <<'PY'
import json, os
from pathlib import Path
from production_probability_v6_activation_contract import validate_v6_activation
from production_probability_v6_runtime_manifest import v6_runtime_manifest_sha256

candidate = validate_v6_activation(json.loads(Path("config/production_probability_runtime_activation.v6_candidate.json").read_text()))
expected = os.environ["EXPECTED_RUNTIME_MANIFEST_SHA256"]
if candidate["runtime_manifest_sha256"] != expected or v6_runtime_manifest_sha256(Path.cwd()) != expected:
    raise SystemExit("v6_runtime_or_candidate_drift")
print("v6_runtime_and_candidate_frozen=true")
PY
}

publish_verified_rehearsal_receipts() {
  # Readiness consumes the receipts in the production audit namespace.  Keep
  # them content-addressed with the exact bundle that backs the active
  # activation; otherwise an already-installed epoch can be reported stale
  # solely because its predecessor's receipts remain in output/audits.
  mkdir -p output/audits
  test -s "$BUNDLE_DIR/canonical_replay.json"
  cp -p "$BUNDLE_DIR/canonical_replay.json" output/audits/physical_state_bayes_v6_canonical_replay.json.tmp.$$
  mv output/audits/physical_state_bayes_v6_canonical_replay.json.tmp.$$ output/audits/physical_state_bayes_v6_canonical_replay.json
  test -s "$BUNDLE_DIR/zero_order.json"
  cp -p "$BUNDLE_DIR/zero_order.json" output/audits/physical_state_bayes_v6_zero_order.json.tmp.$$
  mv output/audits/physical_state_bayes_v6_zero_order.json.tmp.$$ output/audits/physical_state_bayes_v6_zero_order.json
  test -s "$BUNDLE_DIR/host_rehearsal.json"
  cp -p "$BUNDLE_DIR/host_rehearsal.json" output/audits/physical_state_bayes_v6_host_rehearsal.json.tmp.$$
  mv output/audits/physical_state_bayes_v6_host_rehearsal.json.tmp.$$ output/audits/physical_state_bayes_v6_host_rehearsal.json
}

start_sudo_keepalive() {
  SUDO_KEEPALIVE_PID=""
}

stop_sudo_keepalive() {
  if [ -n "${SUDO_KEEPALIVE_PID:-}" ]; then
    kill "$SUDO_KEEPALIVE_PID" 2>/dev/null || true
    wait "$SUDO_KEEPALIVE_PID" 2>/dev/null || true
    SUDO_KEEPALIVE_PID=""
  fi
}

stop_system_monitor() {
  launchctl disable "$MONITOR_DOMAIN" || true
  launchctl bootout "gui/$(id -u)" "$MONITOR_PLIST" 2>/dev/null || true
}

pause_services() {
  SERVICES_PAUSED=1
  stop_system_monitor
  scripts/weatherctl.sh stop
  local pattern="" pids=""
  local -a patterns=(
    "$ROOT/scripts/polymarket_monitor_daemon.sh"
    "$ROOT/scripts/amsc_zuuu_metar_taf_poll_once.sh"
    "$ROOT/scripts/run_amsc_zuuu_metar_taf_poll_immutable.sh"
    "$ROOT/scripts/watch_zuuu_speci_once.py"
    "$ROOT/scripts/apply_current_weak_source_revisions_v6.py"
    "$ROOT/scan_weather_market.py --auto-trade-job-worker"
  )
  for pattern in "${patterns[@]}"; do
    pids="$(pgrep -f "$pattern" 2>/dev/null || true)"
    [ -z "$pids" ] || kill -TERM $pids 2>/dev/null || true
  done
  for _ in $(seq 1 80); do
    pids=""
    for pattern in "${patterns[@]}"; do pids="$pids $(pgrep -f "$pattern" 2>/dev/null || true)"; done
    pids="$(printf '%s' "$pids" | xargs 2>/dev/null || true)"
    [ -z "$pids" ] && return 0
    sleep 0.25
  done
  echo "governed writer process survived pause: $pids" >&2
  return 1
}

resume_services() {
  # The production owner is the three system-domain services. A failed
  # cutover must not resurrect the retired GUI monitor or a user-domain
  # market_ws alongside them; that creates the dual-writer race this
  # maintenance window is meant to prevent. The operator resumes the system
  # services explicitly after the new activation is accepted.
  launchctl disable "$MONITOR_DOMAIN" || true
  launchctl bootout "gui/$(id -u)" "$MONITOR_PLIST" 2>/dev/null || true
  echo "legacy_gui_services_remain_disabled=true"
  SERVICES_PAUSED=0
}

run_nonblocking_ui_consistency_diagnostic() {
  # UI is a read-only presentation surface, not a production consumer.  The
  # three system services are restored by the explicit operator handoff after
  # this script exits, so a stopped 3002 listener must not invalidate the
  # already-committed probability activation or prevent that handoff.
  if ! /usr/bin/curl -fsS --max-time 2 \
    "http://127.0.0.1:${PM_UI_PORT:-3002}/api/ui_snapshot_summary?limit=1" \
    >/dev/null 2>&1; then
    echo "ui_runtime_check=deferred_nonblocking; reason=ui_not_running_or_not_production_consumer"
    return 0
  fi
  if ! python3 scripts/check_ui_page_probability_consistency.py --runtime --refresh --summary; then
    echo "ui_runtime_check=diagnostic_failed_nonblocking; probability_cutover_not_blocked"
  fi
}

verify_quiescence() {
  local attempt=""
  for attempt in 1 2 3 4 5; do
    python3 scripts/controlled_apply_maintenance_window_packet.py --db data/archive.db --summary --max-items-output 30 && return 0
    [ "$attempt" = 5 ] || sleep 0.5
  done
  return 1
}

verify_archive_writer_gate_quiescent() {
  python3 - <<'PY'
from archive_db_write_queue import archive_db_write_gate

with archive_db_write_gate(
    "data/archive.db",
    owner="physical_state_bayes_v6_cutover_quiescence_probe",
    wait_sec=120.0,
    poll_sec=0.1,
):
    pass
print("archive_db_writer_gate_quiescent=true")
PY
}

verify_explicit_recovery_baseline() {
  test "${PHYSICAL_STATE_BAYES_V6_RECOVERY_CONFIRM:-}" = "RECOVER_VALID_V6_BASELINE"
  python3 - <<'PY'
import json
import sqlite3
from pathlib import Path

from physical_state_bayes_v6.schema_contract import build_forward_plan
from production_probability_v6_activation_contract import validate_v6_activation

db = Path("data/archive.db").resolve()
with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
    quick_check = str(conn.execute("PRAGMA quick_check").fetchone()[0])
if quick_check != "ok":
    raise SystemExit("recovery_baseline_quick_check_failed")
validate_v6_activation(json.loads(Path("config/production_probability_runtime_activation.json").read_text()))
plan = build_forward_plan(db)
actions = list(plan.get("actions") or [])
if not plan.get("ok") or plan.get("block_reasons") or any(action.get("action") != "add_column" for action in actions):
    raise SystemExit("recovery_baseline_schema_not_pure_additive")
print(json.dumps({
    "ok": True,
    "state": "explicit_valid_v6_recovery_baseline_accepted",
    "quick_check": quick_check,
    "additive_action_count": len(actions),
}, sort_keys=True))
PY
}

restore_database() {
  test -s "$LOCAL_STAGE" || return 0
  local restore_tmp="data/.archive.db.restore.$$"
  rm -f data/archive.db-wal data/archive.db-shm
  rm -f "$restore_tmp"
  if ! /bin/cp -c "$LOCAL_STAGE" "$restore_tmp"; then
    rm -f "$restore_tmp"
    return 1
  fi
  if [ "$(stat -f%z "$LOCAL_STAGE")" != "$(stat -f%z "$restore_tmp")" ] \
    || [ "$(sqlite3 "$restore_tmp" 'PRAGMA quick_check;')" != "ok" ]; then
    rm -f "$restore_tmp"
    return 1
  fi
  mv "$restore_tmp" data/archive.db
}

recover() {
  local rc=$?
  local restore_ok=1
  if [ "$CUTOVER_COMPLETE" != 1 ]; then
    if [ "$NEW_V6_COMMITTED" = 1 ]; then
      echo "post-commit acceptance failed: step=$LAST_STEP rc=$rc; new v6 preserved fail-closed, old activation fallback forbidden" >&2
    else
      echo "pre-commit cutover failed: step=$LAST_STEP rc=$rc; restoring exact previous v6 state" >&2
      if [ "$SERVICES_PAUSED" != 1 ] && { [ "$DB_MUTATED" = 1 ] || [ "$ACTIVATION_MUTATED" = 1 ]; }; then pause_services || true; fi
      if [ "$DB_MUTATED" = 1 ] && ! restore_database; then
        restore_ok=0
        echo "rollback restore verification failed; services remain paused fail-closed" >&2
      fi
      [ "$ACTIVATION_MUTATED" = 1 ] && cp -p "$ROLLBACK_DIR/production_probability_runtime_activation.json" "$ACTIVE_ACTIVATION" || true
      if [ "$SOURCE_MUTATED" = 1 ]; then
        cp -p "$ROLLBACK_DIR/production_probability_v6_canonical_event_drain_runtime.py" "$ACTIVE_DRAIN_RUNTIME" || true
        cp -p "$ROLLBACK_DIR/production_probability_v6_amsc_archive_ingress.py" "$ACTIVE_INGRESS_RUNTIME" || true
      fi
    fi
    if [ "$SERVICES_PAUSED" = 1 ] && [ "$restore_ok" = 1 ]; then
      resume_services || true
    fi
  fi
  stop_sudo_keepalive
  rm -f "$LOCAL_STAGE" "${LOCAL_STAGE}-journal" "${LOCAL_STAGE}-wal" "${LOCAL_STAGE}-shm"
  rm -rf "$ROLLBACK_DIR"
  exit "$rc"
}
trap recover EXIT

step "preflight_existing_v6_and_station_day"
test "$EVENT_DATE" = "$(TZ=Asia/Shanghai date +%F)"
# A disabled user-domain job can remain printable during launchd's teardown
# window.  Disable/boot it before deciding whether a legacy writer is truly
# resident; only an actual process remains a hard preflight blocker.
stop_system_monitor
if launchctl print system/com.weather.monitor.daemon 2>/dev/null | grep -Eq '^[[:space:]]+pid = [0-9]+$'; then
  echo "legacy system monitor still running; reboot once to complete passwordless user-domain migration" >&2
  exit 1
fi
if launchctl print system/com.weather.reported_trajectory_no 2>/dev/null | grep -Eq '^[[:space:]]+pid = [0-9]+$'; then
  echo "system reported_trajectory_no still owns archive.db; pause it as root before this user-domain cutover" >&2
  echo "run the privileged system-domain disable and bootout for com.weather.reported_trajectory_no, then retry" >&2
  exit 1
fi
if launchctl print "$MONITOR_DOMAIN" 2>/dev/null | grep -Eq '^[[:space:]]+pid = [0-9]+$' \
  || pgrep -f "$ROOT/scripts/weather_monitor_launchagent_entrypoint.sh" >/dev/null 2>&1 \
  || pgrep -f "$ROOT/scripts/polymarket_monitor_daemon.sh" >/dev/null 2>&1; then
  echo "legacy gui monitor still running; reboot once to complete passwordless user-domain migration" >&2
  exit 1
fi
printf 'v6-local-clone-probe\n' > "$ROLLBACK_DIR/clone-probe.source"
/bin/cp -c "$ROLLBACK_DIR/clone-probe.source" "$ROLLBACK_DIR/clone-probe.copy"
cmp -s "$ROLLBACK_DIR/clone-probe.source" "$ROLLBACK_DIR/clone-probe.copy"
python3 scripts/probability_document_authority_audit.py --summary
python3 scripts/production_storage_boundary_audit.py --summary
python3 scripts/production_resolution_date_boundary_audit.py --summary
if [ "${PHYSICAL_STATE_BAYES_V6_SKIP_BUNDLE_PREPARE:-0}" = 1 ]; then
  # A bundle that has already passed the isolated replay/test gates is safe to
  # reuse only after its content-addressed identity is rechecked.  This keeps
  # the maintenance window short without weakening the hard gate or silently
  # rebuilding a different candidate against a moving live archive.
  python3 - "$EVENT_DATE" <<'PY'
import json
import sys
from pathlib import Path

event_date = sys.argv[1][:10]
contract = json.loads(Path("config/production_probability_runtime_bundle.v6_candidate.json").read_text())
if contract.get("event_date") != event_date or contract.get("state") not in {"prepared_not_active", "already_active_verified_bundle"}:
    raise SystemExit("v6_existing_bundle_event_or_state_invalid")
PY
else
  python3 scripts/prepare_production_probability_v6_hotfix_bundle.py --event-date "$EVENT_DATE"
fi
# The active v6 identity may already be installed from a previous interrupted
# handoff.  In that case the candidate is no longer a pre-cutover baseline and
# must be checked with the installed contract; only an older active identity
# is checked against the bundle's baseline.
existing_bundle_dir="$(python3 - <<'PY'
import json
from pathlib import Path
print(json.loads(Path("config/production_probability_runtime_bundle.v6_candidate.json").read_text()).get("bundle_dir") or "")
PY
)"
if [ -n "$existing_bundle_dir" ] \
  && cmp -s "$ACTIVE_ACTIVATION" "$ROOT/$existing_bundle_dir/production_probability_runtime_activation.v6_candidate.json" \
  && cmp -s "$ACTIVE_DRAIN_RUNTIME" "$ROOT/$existing_bundle_dir/production_probability_v6_canonical_event_drain_runtime.py" \
  && cmp -s "$ACTIVE_INGRESS_RUNTIME" "$ROOT/$existing_bundle_dir/production_probability_v6_amsc_archive_ingress.py"; then
  python3 scripts/production_probability_v6_hotfix_bundle_audit.py --installed
else
  python3 scripts/production_probability_v6_hotfix_bundle_audit.py
fi
if ! python3 scripts/production_probability_v6_live_acceptance.py \
  --db data/archive.db --activation "$ACTIVE_ACTIVATION" --event-date "$EVENT_DATE"; then
  if ! python3 scripts/production_probability_v6_predecessor_acceptance.py \
    --db data/archive.db --activation "$ACTIVE_ACTIVATION" --event-date "$EVENT_DATE"; then
    verify_explicit_recovery_baseline
  fi
fi
python3 scripts/production_probability_v6_hotfix_install_rehearsal.py
NEXT_DAY_HANDOFF_AS_OF="$(sqlite3 data/archive.db "
  SELECT w.decision_as_of
  FROM posterior_checkpoints p
  JOIN weak_forecast_evidence_snapshots w
    ON w.forecast_evidence_snapshot_id=p.forecast_evidence_snapshot_id
  WHERE p.target_identity='physical_weather_day_v1:ZUUU:${NEXT_EVENT_DATE}'
    AND p.checkpoint_kind='initial' AND p.state='committed'
  ORDER BY p.replay_generation DESC,p.rowid DESC LIMIT 1;
")"
NEXT_DAY_HANDOFF_AS_OF="${NEXT_DAY_HANDOFF_AS_OF:-$DECISION_AS_OF}"
BUNDLE_DIR="$ROOT/$(python3 -c 'import json; print(json.load(open("config/production_probability_runtime_bundle.v6_candidate.json"))["bundle_dir"])')"
CANDIDATE_FILE_SHA256="$(python3 -c 'import json; print(json.load(open("config/production_probability_runtime_bundle.v6_candidate.json"))["candidate_activation_sha256"])')"
RUNTIME_MANIFEST_SHA256="$(python3 -c 'import json; print(json.load(open("config/production_probability_runtime_bundle.v6_candidate.json"))["runtime_manifest_sha256"])')"
test -s "$BUNDLE_DIR/production_probability_v6_canonical_event_drain_runtime.py"
test -s "$BUNDLE_DIR/production_probability_v6_amsc_archive_ingress.py"
test -s "$BUNDLE_DIR/production_probability_runtime_activation.v6_candidate.json"
python3 -m pytest -q \
  tests/test_install_v6_market_transaction_branch_runtime.py \
  tests/test_production_probability_v6_market_context_runtime.py \
  tests/test_production_probability_v6_market_inputs_runtime.py \
  tests/test_production_probability_v6_realization_policy_runtime.py \
  tests/test_production_probability_v6_strong_physical_projection_runtime.py \
  tests/test_production_probability_v6_market_bundle_runtime.py \
  tests/test_production_probability_v6_market_transaction_writer.py \
  tests/test_production_probability_v6_transaction_branch_runtime.py \
  tests/test_production_probability_v6_transaction_commit_runtime.py \
  tests/test_production_storage_boundary_audit.py \
  tests/test_production_resolution_date_boundary_audit.py \
  tests/test_install_v6_path_auxiliary_runtime.py \
  tests/test_production_probability_v6_path_auxiliary_runtime.py \
  tests/test_physical_state_bayes_v6.py \
  tests/test_physical_state_bayes_v6_models.py \
  tests/test_physical_state_bayes_v6_forcing_adapter.py \
  tests/test_physical_state_bayes_v6_observation_adapter.py \
  tests/test_production_probability_v6_activation_contract.py \
  tests/test_production_probability_v6_p0_runtime.py \
  tests/test_production_probability_v6_candidate_compiler.py \
  tests/test_production_probability_v6_event_runtime.py \
  tests/test_production_probability_v6_weak_revision_runtime.py \
  tests/test_production_probability_v6_runtime_manifest.py \
  tests/test_production_probability_v6_live_route_contract.py \
  tests/test_forecast_background_context_runtime.py \
  tests/test_joint_weak_live_evidence_runtime.py \
  tests/test_weak_forecast_evidence_snapshot_runtime.py \
  tests/test_amsc_half_hour_trigger_runtime.py \
  tests/test_production_weak_forecast_revision_event_runtime.py \
  tests/test_physical_state_bayes_v6_cutover_static.py \
  tests/test_production_probability_v6_canonical_event_drain_runtime.py \
  tests/test_physical_state_bayes_v6_schema_contract.py \
  tests/test_apply_amsc_probability_fast_lane_v6_from_plan.py \
  tests/test_check_ui_page_probability_consistency.py

if cmp -s "$ACTIVE_ACTIVATION" "$BUNDLE_DIR/production_probability_runtime_activation.v6_candidate.json" \
  && cmp -s "$ACTIVE_DRAIN_RUNTIME" "$BUNDLE_DIR/production_probability_v6_canonical_event_drain_runtime.py" \
  && cmp -s "$ACTIVE_INGRESS_RUNTIME" "$BUNDLE_DIR/production_probability_v6_amsc_archive_ingress.py"; then
  step "already_active_same_content_addressed_v6_epoch_read_only_acceptance"
  publish_verified_rehearsal_receipts
  python3 scripts/production_probability_v6_hotfix_bundle_audit.py --installed
  python3 scripts/production_probability_v6_readiness.py --db data/archive.db --summary
  python3 scripts/production_probability_v6_live_acceptance.py \
    --db data/archive.db --activation "$ACTIVE_ACTIVATION" --event-date "$EVENT_DATE"
  python3 scripts/production_probability_v6_peak_timing_acceptance.py \
    --db data/archive.db --event-date "$EVENT_DATE"
  run_nonblocking_ui_consistency_diagnostic
  CUTOVER_COMPLETE=1
  trap - EXIT
  rm -rf "$ROLLBACK_DIR"
  echo "cutover_state=already_active_same_content_addressed_v6_epoch_verified"
  echo "sudo_required=false"
  echo "external_archive=outside_production_boundary_not_required"
  exit 0
fi

if [ "${PHYSICAL_STATE_BAYES_V6_PREFLIGHT_ONLY:-0}" = 1 ]; then
  CUTOVER_COMPLETE=1
  trap - EXIT
  rm -rf "$ROLLBACK_DIR"
  echo "preflight_state=ready_for_v6_runtime_hotfix_cutover"
  exit 0
fi

step "authorize_maintenance_window"
start_sudo_keepalive
cp -p "$ACTIVE_ACTIVATION" "$ROLLBACK_DIR/production_probability_runtime_activation.json"
cp -p "$ACTIVE_DRAIN_RUNTIME" "$ROLLBACK_DIR/production_probability_v6_canonical_event_drain_runtime.py"
cp -p "$ACTIVE_INGRESS_RUNTIME" "$ROLLBACK_DIR/production_probability_v6_amsc_archive_ingress.py"

step "pause_all_archive_writers"
pause_services
verify_quiescence
verify_archive_writer_gate_quiescent

step "install_peak_timing_and_default_off_market_transaction_runtime_then_freeze_candidate"
SOURCE_MUTATED=1
python3 scripts/production_probability_v6_hotfix_bundle_audit.py
cp -p "$BUNDLE_DIR/production_probability_v6_canonical_event_drain_runtime.py" "$ACTIVE_DRAIN_RUNTIME.tmp.$$"
mv "$ACTIVE_DRAIN_RUNTIME.tmp.$$" "$ACTIVE_DRAIN_RUNTIME"
cp -p "$BUNDLE_DIR/production_probability_v6_amsc_archive_ingress.py" "$ACTIVE_INGRESS_RUNTIME.tmp.$$"
mv "$ACTIVE_INGRESS_RUNTIME.tmp.$$" "$ACTIVE_INGRESS_RUNTIME"
cp -p "$BUNDLE_DIR/production_probability_runtime_activation.v6_candidate.json" "$CANDIDATE_ACTIVATION.tmp.$$"
mv "$CANDIDATE_ACTIVATION.tmp.$$" "$CANDIDATE_ACTIVATION"
publish_verified_rehearsal_receipts
verify_candidate_frozen

step "verify_pre_rehearsed_candidate_inside_quiescent_boundary"
verify_candidate_frozen

step "create_local_apfs_rollback_clone"
test "$(sqlite3 data/archive.db 'PRAGMA wal_checkpoint(TRUNCATE);')" = "0|0|0"
/bin/cp -c data/archive.db "$LOCAL_STAGE"
test -s "$LOCAL_STAGE"
test "$(stat -f%z data/archive.db)" = "$(stat -f%z "$LOCAL_STAGE")"
sqlite3 "$LOCAL_STAGE" 'SELECT COUNT(*) > 0 FROM sqlite_master;' | grep -qx 1
verify_candidate_frozen

step "atomically_install_new_v6_activation"
DB_MUTATED=1
python3 scripts/activate_physical_state_bayes_v6_schema.py \
  --db data/archive.db --apply --writer-quiescent \
  --confirm ACTIVATE_PHYSICAL_STATE_BAYES_V6_SCHEMA
ACTIVATION_MUTATED=1
cp "$CANDIDATE_ACTIVATION" "${ACTIVE_ACTIVATION}.tmp.$$"
mv "${ACTIVE_ACTIVATION}.tmp.$$" "$ACTIVE_ACTIVATION"
python3 scripts/production_probability_v6_hotfix_bundle_audit.py --installed

step "create_new_generation_p0_and_replay_all_canonical_events"
python3 scripts/bootstrap_production_probability_v6.py \
  --db data/archive.db --activation "$ACTIVE_ACTIVATION" \
  --event-date "$EVENT_DATE" --decision-as-of "$DECISION_AS_OF" \
  --apply --confirm BOOTSTRAP_PRODUCTION_PHYSICAL_STATE_BAYES_V6

step "consume_latest_current_day_weak_source_revisions_before_acceptance"
python3 scripts/apply_current_weak_source_revisions_v6.py \
  --db data/archive.db --activation "$ACTIVE_ACTIVATION" \
  --event-date "$EVENT_DATE"

step "create_new_generation_next_day_p0_at_strict_handoff_asof"
python3 scripts/bootstrap_production_probability_v6.py \
  --db data/archive.db --activation "$ACTIVE_ACTIVATION" \
  --event-date "$NEXT_EVENT_DATE" --decision-as-of "$NEXT_DAY_HANDOFF_AS_OF" \
  --apply --confirm BOOTSTRAP_PRODUCTION_PHYSICAL_STATE_BAYES_V6

step "apply_next_day_forward_only_weak_source_revision"
python3 scripts/apply_current_weak_source_revisions_v6.py \
  --db data/archive.db --activation "$ACTIVE_ACTIVATION" \
  --event-date "$NEXT_EVENT_DATE"

step "accept_new_v6_before_resume"
python3 scripts/production_probability_v6_readiness.py --db data/archive.db --summary
python3 scripts/production_probability_v6_live_acceptance.py --db data/archive.db --activation "$ACTIVE_ACTIVATION" --event-date "$EVENT_DATE"
python3 scripts/production_probability_v6_peak_timing_acceptance.py \
  --db data/archive.db --event-date "$EVENT_DATE"
python3 scripts/production_probability_v6_live_acceptance.py \
  --db data/archive.db --activation "$ACTIVE_ACTIVATION" --event-date "$NEXT_EVENT_DATE"
NEW_V6_COMMITTED=1

step "resume_services_on_new_v6_only"
resume_services

step "post_resume_acceptance"
accepted=0
for attempt in $(seq 1 15); do
  if python3 scripts/production_probability_v6_live_acceptance.py --db data/archive.db --activation "$ACTIVE_ACTIVATION" --event-date "$EVENT_DATE"; then accepted=1; break; fi
  [ "$attempt" = 15 ] || sleep 1
done
test "$accepted" = 1
python3 scripts/production_probability_v6_peak_timing_acceptance.py \
  --db data/archive.db --event-date "$EVENT_DATE"
run_nonblocking_ui_consistency_diagnostic

CUTOVER_COMPLETE=1
trap - EXIT
stop_sudo_keepalive
rm -f "$LOCAL_STAGE" "${LOCAL_STAGE}-journal" "${LOCAL_STAGE}-wal" "${LOCAL_STAGE}-shm"
rm -rf "$ROLLBACK_DIR"
echo "cutover_state=production_v6_runtime_hotfix_active_no_fallback"
echo "rollback_clone=local_apfs_cow_deleted_after_acceptance"
echo "external_archive=outside_production_boundary_not_required"
echo "service_restore_handoff=run_sudo_resume_and_install_system_services"
echo "log=$LOG"
