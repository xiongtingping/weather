#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

EVENT_DATE="${PHYSICAL_STATE_BAYES_V6_EVENT_DATE:-$(TZ=Asia/Shanghai date +%F)}"
DECISION_AS_OF="${PHYSICAL_STATE_BAYES_V6_DECISION_AS_OF:-$(date -u +%Y-%m-%dT%H:%M:%S.000Z)}"
ACTIVE_ACTIVATION="$ROOT/config/production_probability_runtime_activation.json"
CANDIDATE_ACTIVATION="$ROOT/config/production_probability_runtime_activation.v6_candidate.json"
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
CANDIDATE_FILE_SHA256=""
RUNTIME_MANIFEST_SHA256=""

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

start_sudo_keepalive() {
  local parent_pid="$$"
  (while kill -0 "$parent_pid" 2>/dev/null; do sudo -n -v >/dev/null 2>&1 || exit 1; sleep 45; done) &
  SUDO_KEEPALIVE_PID="$!"
}

stop_sudo_keepalive() {
  if [ -n "${SUDO_KEEPALIVE_PID:-}" ]; then
    kill "$SUDO_KEEPALIVE_PID" 2>/dev/null || true
    wait "$SUDO_KEEPALIVE_PID" 2>/dev/null || true
    SUDO_KEEPALIVE_PID=""
  fi
}

stop_system_monitor() {
  sudo launchctl disable system/com.weather.monitor.daemon || true
  sudo launchctl bootout system /Library/LaunchDaemons/com.weather.monitor.daemon.plist 2>/dev/null || true
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
    "$ROOT/scan_weather_market.py --auto-trade-job-worker"
  )
  for pattern in "${patterns[@]}"; do
    pids="$(pgrep -f "$pattern" 2>/dev/null || true)"
    [ -z "$pids" ] || kill -TERM $pids 2>/dev/null || sudo kill -TERM $pids 2>/dev/null || true
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
  sudo install -o root -g wheel -m 644 launchd/com.weather.monitor.daemon.plist /Library/LaunchDaemons/com.weather.monitor.daemon.plist
  sudo launchctl enable system/com.weather.monitor.daemon
  sudo launchctl bootstrap system /Library/LaunchDaemons/com.weather.monitor.daemon.plist 2>/dev/null || true
  sudo launchctl kickstart -k system/com.weather.monitor.daemon
  scripts/weatherctl.sh start
  SERVICES_PAUSED=0
}

verify_quiescence() {
  local attempt=""
  for attempt in 1 2 3 4 5; do
    python3 scripts/controlled_apply_maintenance_window_packet.py --db data/archive.db --summary --max-items-output 30 && return 0
    [ "$attempt" = 5 ] || sleep 0.5
  done
  return 1
}

restore_database() {
  test -s "$LOCAL_STAGE" || return 0
  rm -f data/archive.db-wal data/archive.db-shm
  cp -p "$LOCAL_STAGE" "data/.archive.db.restore.$$"
  mv "data/.archive.db.restore.$$" data/archive.db
}

recover() {
  local rc=$?
  if [ "$CUTOVER_COMPLETE" != 1 ]; then
    if [ "$NEW_V6_COMMITTED" = 1 ]; then
      echo "post-commit acceptance failed: step=$LAST_STEP rc=$rc; new v6 preserved fail-closed, old activation fallback forbidden" >&2
    else
      echo "pre-commit cutover failed: step=$LAST_STEP rc=$rc; restoring exact previous v6 state" >&2
      if [ "$SERVICES_PAUSED" != 1 ] && { [ "$DB_MUTATED" = 1 ] || [ "$ACTIVATION_MUTATED" = 1 ]; }; then pause_services || true; fi
      [ "$DB_MUTATED" = 1 ] && restore_database || true
      [ "$ACTIVATION_MUTATED" = 1 ] && cp -p "$ROLLBACK_DIR/production_probability_runtime_activation.json" "$ACTIVE_ACTIVATION" || true
      if [ "$SOURCE_MUTATED" = 1 ]; then
        cp -p "$ROLLBACK_DIR/production_probability_v6_canonical_event_drain_runtime.py" "$ACTIVE_DRAIN_RUNTIME" || true
        cp -p "$ROLLBACK_DIR/production_probability_v6_amsc_archive_ingress.py" "$ACTIVE_INGRESS_RUNTIME" || true
      fi
    fi
    [ "$SERVICES_PAUSED" = 1 ] && resume_services || true
  fi
  stop_sudo_keepalive
  rm -f "$LOCAL_STAGE" "${LOCAL_STAGE}-journal" "${LOCAL_STAGE}-wal" "${LOCAL_STAGE}-shm"
  rm -rf "$ROLLBACK_DIR"
  exit "$rc"
}
trap recover EXIT

step "preflight_existing_v6_and_station_day"
test "$EVENT_DATE" = "$(TZ=Asia/Shanghai date +%F)"
printf 'v6-local-clone-probe\n' > "$ROLLBACK_DIR/clone-probe.source"
/bin/cp -c "$ROLLBACK_DIR/clone-probe.source" "$ROLLBACK_DIR/clone-probe.copy"
cmp -s "$ROLLBACK_DIR/clone-probe.source" "$ROLLBACK_DIR/clone-probe.copy"
python3 scripts/probability_document_authority_audit.py --summary
python3 scripts/production_storage_boundary_audit.py --summary
python3 scripts/production_probability_v6_live_acceptance.py \
  --db data/archive.db --activation "$ACTIVE_ACTIVATION" --event-date "$EVENT_DATE"
python3 scripts/install_v6_market_transaction_branch_runtime.py
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

if [ "${PHYSICAL_STATE_BAYES_V6_PREFLIGHT_ONLY:-0}" = 1 ]; then
  CUTOVER_COMPLETE=1
  trap - EXIT
  rm -rf "$ROLLBACK_DIR"
  echo "preflight_state=ready_for_v6_runtime_hotfix_cutover"
  exit 0
fi

step "authorize_maintenance_window"
sudo -v
start_sudo_keepalive
cp -p "$ACTIVE_ACTIVATION" "$ROLLBACK_DIR/production_probability_runtime_activation.json"
cp -p "$ACTIVE_DRAIN_RUNTIME" "$ROLLBACK_DIR/production_probability_v6_canonical_event_drain_runtime.py"
cp -p "$ACTIVE_INGRESS_RUNTIME" "$ROLLBACK_DIR/production_probability_v6_amsc_archive_ingress.py"

step "pause_all_archive_writers"
pause_services
verify_quiescence

step "install_peak_timing_and_default_off_market_transaction_runtime_then_freeze_candidate"
SOURCE_MUTATED=1
python3 scripts/install_v6_market_transaction_branch_runtime.py \
  --apply --confirm INSTALL_V6_MARKET_TRANSACTION_BRANCH_RUNTIME
python3 scripts/build_production_probability_v6_candidate_activation.py --write
CANDIDATE_FILE_SHA256="$(/usr/bin/shasum -a 256 "$CANDIDATE_ACTIVATION" | awk '{print $1}')"
RUNTIME_MANIFEST_SHA256="$(python3 -c 'import json; print(json.load(open("config/production_probability_runtime_activation.v6_candidate.json"))["runtime_manifest_sha256"])')"
verify_candidate_frozen

step "test_and_rehearse_new_runtime_without_live_writes"
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
python3 scripts/replay_physical_state_bayes_v6_canonical.py
python3 scripts/production_probability_v6_zero_order_drill.py
python3 scripts/production_probability_v6_host_rehearsal.py --db data/archive.db --event-date "$EVENT_DATE"
verify_candidate_frozen

step "create_local_apfs_rollback_clone"
test "$(sqlite3 data/archive.db 'PRAGMA wal_checkpoint(TRUNCATE);')" = "0|0|0"
/bin/cp -c data/archive.db "$LOCAL_STAGE"
test -s "$LOCAL_STAGE"
test "$(stat -f%z data/archive.db)" = "$(stat -f%z "$LOCAL_STAGE")"
sqlite3 "$LOCAL_STAGE" 'SELECT COUNT(*) > 0 FROM sqlite_master;' | grep -qx 1
verify_candidate_frozen

step "atomically_install_new_v6_activation"
ACTIVATION_MUTATED=1
cp "$CANDIDATE_ACTIVATION" "${ACTIVE_ACTIVATION}.tmp.$$"
mv "${ACTIVE_ACTIVATION}.tmp.$$" "$ACTIVE_ACTIVATION"

step "create_new_generation_p0_and_replay_all_canonical_events"
DB_MUTATED=1
python3 scripts/bootstrap_production_probability_v6.py \
  --db data/archive.db --activation "$ACTIVE_ACTIVATION" \
  --event-date "$EVENT_DATE" --decision-as-of "$DECISION_AS_OF" \
  --apply --confirm BOOTSTRAP_PRODUCTION_PHYSICAL_STATE_BAYES_V6

step "accept_new_v6_before_resume"
python3 scripts/production_probability_v6_readiness.py --db data/archive.db --summary
python3 scripts/production_probability_v6_live_acceptance.py --db data/archive.db --activation "$ACTIVE_ACTIVATION" --event-date "$EVENT_DATE"
python3 scripts/production_probability_v6_peak_timing_acceptance.py \
  --db data/archive.db --event-date "$EVENT_DATE"
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
python3 scripts/check_ui_page_probability_consistency.py --runtime --refresh --summary

CUTOVER_COMPLETE=1
trap - EXIT
stop_sudo_keepalive
rm -f "$LOCAL_STAGE" "${LOCAL_STAGE}-journal" "${LOCAL_STAGE}-wal" "${LOCAL_STAGE}-shm"
rm -rf "$ROLLBACK_DIR"
echo "cutover_state=production_v6_runtime_hotfix_active_no_fallback"
echo "rollback_clone=local_apfs_cow_deleted_after_acceptance"
echo "external_archive=outside_production_boundary_not_required"
echo "log=$LOG"
