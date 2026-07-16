#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

EVENT_DATE="${PHYSICAL_STATE_BAYES_V6_EVENT_DATE:-$(TZ=Asia/Shanghai date +%F)}"
DECISION_AS_OF="${PHYSICAL_STATE_BAYES_V6_DECISION_AS_OF:-$(date -u +%Y-%m-%dT%H:%M:%S.000Z)}"
ACTIVE_ACTIVATION="$ROOT/config/production_probability_runtime_activation.json"
CANDIDATE_ACTIVATION="$ROOT/config/production_probability_runtime_activation.v6_candidate.json"
EXTERNAL_VOLUME_ROOT="${PHYSICAL_STATE_BAYES_V6_EXTERNAL_VOLUME_ROOT:-/Volumes/Expansion}"
EXTERNAL_BACKUP_DIR="${PHYSICAL_STATE_BAYES_V6_EXTERNAL_BACKUP_DIR:-/Volumes/Expansion/熊/backups}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="$EXTERNAL_BACKUP_DIR/archive.pre-amsc-slot-complete-v6.$STAMP.db"
LOCAL_STAGE="/private/tmp/archive.pre-amsc-slot-complete-v6.$STAMP.db"
LOG="$ROOT/data/logs/amsc_slot_complete_v6_trade_caps_cutover.log"
ACTIVE_POLL_ROUTE="$ROOT/scripts/amsc_zuuu_metar_taf_poll_once.sh"
CANDIDATE_POLL_ROUTE="$ROOT/scripts/amsc_zuuu_metar_taf_poll_once.slot_complete_candidate.sh"
ROLLBACK_DIR="$(mktemp -d)"
LAST_STEP="initialize"
SERVICES_PAUSED=0
ACTIVATION_MUTATED=0
POLL_ROUTE_MUTATED=0
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

verify_external_backup_mount() {
  test -d "$EXTERNAL_VOLUME_ROOT" -a -d "$EXTERNAL_BACKUP_DIR" -a -w "$EXTERNAL_BACKUP_DIR"
  test "$(stat -f%d "$EXTERNAL_VOLUME_ROOT")" != "$(stat -f%d "$ROOT")"
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
  local failed_db="${LOCAL_STAGE}.failed-cutover-db"
  if [ ! -s "$LOCAL_STAGE" ]; then
    sqlite3 data/archive.db 'PRAGMA quick_check;' | grep -qx ok
    return
  fi
  rm -f data/archive.db-wal data/archive.db-shm
  # The verified local stage and archive.db live on the same APFS data
  # volume.  Rename restore needs no second 16GB copy and therefore remains
  # safe when free space is smaller than the database.
  test "$(stat -f%d "$LOCAL_STAGE")" = "$(stat -f%d data)"
  if [ -e "$failed_db" ]; then
    echo "rollback collision preserves both database files: $failed_db" >&2
    return 1
  fi
  mv data/archive.db "$failed_db"
  if ! mv "$LOCAL_STAGE" data/archive.db; then
    mv "$failed_db" data/archive.db || true
    return 1
  fi
  if ! sqlite3 data/archive.db 'PRAGMA quick_check;' | grep -qx ok; then
    mv data/archive.db "$LOCAL_STAGE"
    mv "$failed_db" data/archive.db
    return 1
  fi
  rm -f "$failed_db"
}

reclaim_quiescent_writer_lease() {
  local lock="$ROOT/data/db_writer.lock"
  local holder_pid=""
  [ -d "$lock" ] || return 0
  holder_pid="$(cat "$lock/pid" 2>/dev/null || true)"
  case "$holder_pid" in
    ''|*[!0-9]*) echo "invalid archive writer lease metadata: $lock" >&2; return 1 ;;
  esac
  if ps -p "$holder_pid" -o pid= 2>/dev/null | grep -q '[0-9]'; then
    echo "archive writer lease still has live holder pid=$holder_pid" >&2
    return 1
  fi
  # Only maintenance may remove a root-owned dead lease, and only after the
  # independent writer-quiescence audit has already passed.
  sudo rm -rf "$lock"
  [ ! -e "$lock" ]
}

recover() {
  local rc=$?
  local rollback_safe=1
  local allow_resume=0
  if [ "$CUTOVER_COMPLETE" != 1 ]; then
    if [ "$NEW_V6_COMMITTED" = 1 ]; then
      echo "post-commit acceptance failed: step=$LAST_STEP rc=$rc; new v6 preserved fail-closed, old activation fallback forbidden" >&2
      if [ "$SERVICES_PAUSED" != 1 ]; then
        pause_services || rollback_safe=0
      fi
      allow_resume=0
    else
      echo "pre-commit cutover failed: step=$LAST_STEP rc=$rc; restoring exact previous v6 state" >&2
      if [ "$SERVICES_PAUSED" != 1 ] && { [ "$DB_MUTATED" = 1 ] || [ "$ACTIVATION_MUTATED" = 1 ]; }; then
        pause_services || rollback_safe=0
      fi
      if [ "$DB_MUTATED" = 1 ] && ! restore_database; then rollback_safe=0; fi
      if [ "$ACTIVATION_MUTATED" = 1 ] && ! cp -p "$ROLLBACK_DIR/production_probability_runtime_activation.json" "$ACTIVE_ACTIVATION"; then rollback_safe=0; fi
      if [ "$POLL_ROUTE_MUTATED" = 1 ] && ! cp -p "$ROLLBACK_DIR/amsc_zuuu_metar_taf_poll_once.sh" "$ACTIVE_POLL_ROUTE"; then rollback_safe=0; fi
      if [ "$rollback_safe" = 1 ] && [ "$SERVICES_PAUSED" = 1 ]; then
        if python3 scripts/production_probability_v6_live_acceptance.py \
          --db data/archive.db --activation "$ACTIVE_ACTIVATION" --event-date "$EVENT_DATE"; then
          allow_resume=1
        else
          rollback_safe=0
        fi
      elif [ "$rollback_safe" = 1 ]; then
        allow_resume=1
      fi
    fi
    if [ "$SERVICES_PAUSED" = 1 ] && [ "$allow_resume" = 1 ]; then
      if ! resume_services; then
        rollback_safe=0
        allow_resume=0
      fi
    fi
    if [ "$rollback_safe" != 1 ] || [ "$allow_resume" != 1 ]; then
      echo "recovery_state=fail_closed_services_stopped recovery_materials_preserved=true" >&2
    fi
  fi
  stop_sudo_keepalive
  if [ "$CUTOVER_COMPLETE" = 1 ] || { [ "$rollback_safe" = 1 ] && [ "$allow_resume" = 1 ]; }; then
    rm -f "$LOCAL_STAGE" "${LOCAL_STAGE}-journal" "${LOCAL_STAGE}-wal" "${LOCAL_STAGE}-shm"
    rm -rf "$ROLLBACK_DIR"
  else
    echo "local_stage=$LOCAL_STAGE" >&2
    echo "rollback_dir=$ROLLBACK_DIR" >&2
  fi
  exit "$rc"
}
trap recover EXIT

step "preflight_existing_v6_candidate_route_and_trade_caps"
test "$EVENT_DATE" = "$(TZ=Asia/Shanghai date +%F)"
verify_external_backup_mount
python3 scripts/probability_document_authority_audit.py --summary
python3 scripts/build_production_probability_v6_candidate_activation.py --write
python3 scripts/production_probability_v6_readiness.py \
  --db data/archive.db --summary --cutover-candidate-pre-rehearsal
bash -n "$CANDIDATE_POLL_ROUTE"
python3 -m pytest -q \
  tests/test_amsc_poller_release_window_static.py \
  tests/test_amsc_poll_slot_runtime.py \
  tests/test_strong_posterior_three_leg_strategy.py \
  tests/test_target_realization_v3_two_leg_integration.py \
  tests/test_three_leg_trading_plan_shadow_runtime.py \
  tests/test_dynamic_three_leg_transition_runtime.py \
  tests/test_production_dynamic_three_leg_frozen_bundle.py \
  tests/test_production_dynamic_three_leg_input_provider.py \
  tests/test_production_dynamic_three_leg_decision_writer.py \
  tests/test_production_dynamic_three_leg_risk_reservation_writer.py \
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
  tests/test_check_ui_page_probability_consistency.py \
  tests/test_ui_server_runtime.py::test_sequential_v2_frontend_labels_physical_timeline_as_x_not_final_market_bucket \
  tests/test_ui_server_runtime.py::test_sequential_v3_trajectory_timeline_reads_reported_x_checkpoint_chain
python3 scripts/production_trade_gate_zero_audit.py

if [ "${PHYSICAL_STATE_BAYES_V6_PREFLIGHT_ONLY:-0}" = 1 ]; then
  CUTOVER_COMPLETE=1
  trap - EXIT
  rm -rf "$ROLLBACK_DIR"
  echo "preflight_state=ready_for_amsc_slot_complete_v6_trade_caps_cutover"
  exit 0
fi

step "authorize_maintenance_window"
sudo -v
start_sudo_keepalive
cp -p "$ACTIVE_ACTIVATION" "$ROLLBACK_DIR/production_probability_runtime_activation.json"
cp -p "$ACTIVE_POLL_ROUTE" "$ROLLBACK_DIR/amsc_zuuu_metar_taf_poll_once.sh"

step "pause_all_archive_writers"
pause_services
verify_quiescence
reclaim_quiescent_writer_lease
verify_external_backup_mount

step "install_slot_complete_route_and_freeze_new_v6_activation"
POLL_ROUTE_MUTATED=1
cp "$CANDIDATE_POLL_ROUTE" "${ACTIVE_POLL_ROUTE}.tmp.$$"
chmod 755 "${ACTIVE_POLL_ROUTE}.tmp.$$"
mv "${ACTIVE_POLL_ROUTE}.tmp.$$" "$ACTIVE_POLL_ROUTE"
python3 scripts/build_production_probability_v6_candidate_activation.py --write
CANDIDATE_FILE_SHA256="$(/usr/bin/shasum -a 256 "$CANDIDATE_ACTIVATION" | awk '{print $1}')"
RUNTIME_MANIFEST_SHA256="$(python3 -c 'import json; print(json.load(open("config/production_probability_runtime_activation.v6_candidate.json"))["runtime_manifest_sha256"])')"
verify_candidate_frozen

step "rehearse_new_route_bound_runtime_without_orders"
python3 scripts/replay_physical_state_bayes_v6_canonical.py
python3 scripts/production_probability_v6_zero_order_drill.py
python3 scripts/production_probability_v6_host_rehearsal.py --db data/archive.db --event-date "$EVENT_DATE"
verify_candidate_frozen

step "create_verified_external_database_backup"
test "$(sqlite3 data/archive.db 'PRAGMA wal_checkpoint(TRUNCATE);')" = "0|0|0"
sqlite3 data/archive.db ".backup '$LOCAL_STAGE'"
sqlite3 "$LOCAL_STAGE" 'PRAGMA integrity_check;' | grep -qx ok
/bin/cp "$LOCAL_STAGE" "${BACKUP}.partial"
test "$(/usr/bin/shasum -a 256 "$LOCAL_STAGE" | awk '{print $1}')" = "$(/usr/bin/shasum -a 256 "${BACKUP}.partial" | awk '{print $1}')"
mv "${BACKUP}.partial" "$BACKUP"
test -s "$BACKUP"
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
python3 scripts/check_ui_page_probability_consistency.py --runtime --refresh --summary

CUTOVER_COMPLETE=1
trap - EXIT
stop_sudo_keepalive
rm -f "$LOCAL_STAGE" "${LOCAL_STAGE}-journal" "${LOCAL_STAGE}-wal" "${LOCAL_STAGE}-shm"
rm -rf "$ROLLBACK_DIR"
echo "cutover_state=amsc_slot_complete_v6_trade_caps_active_no_fallback"
echo "backup=$BACKUP"
echo "log=$LOG"
