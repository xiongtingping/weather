#!/bin/bash
set -euo pipefail

echo "RETIRED_EXTERNAL_DISK_PRODUCTION_CUTOVER_FORBIDDEN: use scripts/cutover_v6_probability_and_soft_market_projection.sh" >&2
exit 2

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# The physical weather day and market settlement day are ET; do not roll to a
# new event before the ET station day has changed (CN midnight is too early).
EVENT_DATE="${PHYSICAL_STATE_BAYES_V6_EVENT_DATE:-$(TZ=America/New_York date +%F)}"
DECISION_AS_OF="${PHYSICAL_STATE_BAYES_V6_DECISION_AS_OF:-$(date -u +%Y-%m-%dT%H:%M:%S.000Z)}"
ACTIVE_ACTIVATION="$ROOT/config/production_probability_runtime_activation.json"
CANDIDATE_ACTIVATION="$ROOT/config/production_probability_runtime_activation.v6_candidate.json"
EXTERNAL_BACKUP_DIR="${PHYSICAL_STATE_BAYES_V6_EXTERNAL_BACKUP_DIR:-/Volumes/Expansion/熊/backups}"
EXTERNAL_VOLUME_ROOT="${PHYSICAL_STATE_BAYES_V6_EXTERNAL_VOLUME_ROOT:-/Volumes/Expansion}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="$EXTERNAL_BACKUP_DIR/archive.pre-physical-state-bayes-v6.$STAMP.db"
LOCAL_BACKUP_STAGE="/private/tmp/archive.pre-physical-state-bayes-v6.$STAMP.db.local"
LOG="$ROOT/data/logs/physical_state_bayes_v6_production_cutover.log"
ROLLBACK_DIR="$(mktemp -d)"
LAST_STEP="initialize"
CUTOVER_COMPLETE=0
SERVICES_PAUSED=0
DB_MUTATED=0
ROUTES_MUTATED=0
ACTIVATION_MUTATED=0
V6_COMMITTED=0
SUDO_KEEPALIVE_PID=""
CANDIDATE_FILE_SHA256=""
RUNTIME_MANIFEST_SHA256=""
EXTERNAL_VOLUME_FS_ID=""
ROOT_VOLUME_FS_ID=""

mkdir -p "$ROOT/data/logs"
exec > >(tee -a "$LOG") 2>&1

step() {
  LAST_STEP="$1"
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] step=$LAST_STEP"
}

start_sudo_keepalive() {
  local parent_pid="$$"
  (
    while kill -0 "$parent_pid" 2>/dev/null; do
      sudo -n -v >/dev/null 2>&1 || exit 1
      sleep 45
    done
  ) &
  SUDO_KEEPALIVE_PID="$!"
}

stop_sudo_keepalive() {
  if [ -n "${SUDO_KEEPALIVE_PID:-}" ]; then
    kill "$SUDO_KEEPALIVE_PID" 2>/dev/null || true
    wait "$SUDO_KEEPALIVE_PID" 2>/dev/null || true
    SUDO_KEEPALIVE_PID=""
  fi
}

system_monitor_pids() {
  pgrep -f "$ROOT/scripts/polymarket_monitor_daemon.sh" 2>/dev/null || true
}

system_monitor_process_group_pids() {
  local pgids="$1"
  local pgid=""
  [ -n "$pgids" ] || return 0
  for pgid in $pgids; do
    ps -axo pid=,pgid= | awk -v wanted="$pgid" '$2 == wanted {print $1}'
  done | sort -u
}

stop_system_monitor() {
  local pids=""
  local monitor_pids=""
  local monitor_pgids=""
  monitor_pids="$(system_monitor_pids)"
  monitor_pgids="$(
    for pid in $monitor_pids; do
      ps -p "$pid" -o pgid= 2>/dev/null | tr -d '[:space:]'
    done | sort -u
  )"
  sudo launchctl disable system/com.weather.monitor.daemon || true
  sudo launchctl bootout system /Library/LaunchDaemons/com.weather.monitor.daemon.plist 2>/dev/null || true
  for _ in $(seq 1 40); do
    pids="$(system_monitor_process_group_pids "$monitor_pgids")"
    [ -z "$pids" ] && return 0
    sleep 0.25
  done
  pids="$(system_monitor_process_group_pids "$monitor_pgids")"
  [ -z "$pids" ] || sudo kill -TERM $pids 2>/dev/null || true
  for _ in $(seq 1 40); do
    pids="$(system_monitor_process_group_pids "$monitor_pgids")"
    [ -z "$pids" ] && return 0
    sleep 0.25
  done
  pids="$(system_monitor_process_group_pids "$monitor_pgids")"
  [ -z "$pids" ] || sudo kill -KILL $pids 2>/dev/null || true
  sleep 0.25
  pids="$(system_monitor_process_group_pids "$monitor_pgids")"
  if [ -n "$pids" ]; then
    echo "system monitor process group survived maintenance pause: $pids" >&2
    return 1
  fi
}

pause_services() {
  SERVICES_PAUSED=1
  # Root-owned pollers must lose their supervisor before user-domain writers
  # are retired, otherwise the monitor can recreate a DB holder mid-pause.
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
    for pattern in "${patterns[@]}"; do
      pids="$pids $(pgrep -f "$pattern" 2>/dev/null || true)"
    done
    pids="$(printf '%s' "$pids" | xargs 2>/dev/null || true)"
    [ -z "$pids" ] && break
    sleep 0.25
  done
  [ -z "$pids" ] || { echo "governed writer process survived pause: $pids" >&2; return 1; }
}

resume_services() {
  sudo install -o root -g wheel -m 644 \
    launchd/com.weather.monitor.daemon.plist \
    /Library/LaunchDaemons/com.weather.monitor.daemon.plist
  sudo launchctl enable system/com.weather.monitor.daemon
  sudo launchctl bootstrap system /Library/LaunchDaemons/com.weather.monitor.daemon.plist 2>/dev/null || true
  sudo launchctl kickstart -k system/com.weather.monitor.daemon
  scripts/weatherctl.sh start
  SERVICES_PAUSED=0
}

verify_quiescence() {
  local attempt=""
  for attempt in 1 2 3 4 5; do
    if python3 scripts/controlled_apply_maintenance_window_packet.py \
      --db data/archive.db --summary --max-items-output 30; then
      return 0
    fi
    [ "$attempt" = "5" ] || sleep 0.5
  done
  return 1
}

restore_database() {
  local restore_source="$BACKUP"
  if [ -s "$LOCAL_BACKUP_STAGE" ]; then
    restore_source="$LOCAL_BACKUP_STAGE"
  fi
  [ -s "$restore_source" ] || return 0
  rm -f data/archive.db-wal data/archive.db-shm
  cp -p "$restore_source" "data/.archive.db.restore.$$"
  mv "data/.archive.db.restore.$$" data/archive.db
}

verify_frozen_v6_runtime() {
  test -n "$CANDIDATE_FILE_SHA256"
  test -n "$RUNTIME_MANIFEST_SHA256"
  test "$CANDIDATE_FILE_SHA256" = "$(/usr/bin/shasum -a 256 "$CANDIDATE_ACTIVATION" | awk '{print $1}')"
  EXPECTED_RUNTIME_MANIFEST_SHA256="$RUNTIME_MANIFEST_SHA256" python3 - <<'PY'
import json
import os
from pathlib import Path

from production_probability_v6_activation_contract import validate_v6_activation
from production_probability_v6_runtime_manifest import v6_runtime_manifest_sha256

candidate = validate_v6_activation(
    json.loads(Path("config/production_probability_runtime_activation.v6_candidate.json").read_text(encoding="utf-8"))
)
expected = os.environ["EXPECTED_RUNTIME_MANIFEST_SHA256"]
current = v6_runtime_manifest_sha256(Path.cwd())
if candidate.get("runtime_manifest_sha256") != expected or current != expected:
    raise SystemExit(
        f"v6_runtime_or_candidate_drift:expected={expected}:candidate={candidate.get('runtime_manifest_sha256')}:current={current}"
    )
print("v6_runtime_and_candidate_frozen=true")
PY
}

verify_external_backup_mount() {
  local current_fs_id=""
  test -d "$EXTERNAL_VOLUME_ROOT" -a -d "$EXTERNAL_BACKUP_DIR" -a -w "$EXTERNAL_BACKUP_DIR" || {
    echo "external backup volume or directory unavailable: volume=$EXTERNAL_VOLUME_ROOT backup_dir=$EXTERNAL_BACKUP_DIR" >&2
    return 1
  }
  current_fs_id="$(stat -f%d "$EXTERNAL_VOLUME_ROOT" 2>/dev/null)" || {
    echo "external backup volume stat failed: volume=$EXTERNAL_VOLUME_ROOT" >&2
    return 1
  }
  test -n "$EXTERNAL_VOLUME_FS_ID" -a "$current_fs_id" = "$EXTERNAL_VOLUME_FS_ID" || {
    echo "external backup volume identity drift: expected=$EXTERNAL_VOLUME_FS_ID current=$current_fs_id" >&2
    return 1
  }
  test "$current_fs_id" != "$ROOT_VOLUME_FS_ID" || {
    echo "external backup path resolved onto root workspace filesystem: fs_id=$current_fs_id" >&2
    return 1
  }
}

recover() {
  local rc=$?
  if [ "$CUTOVER_COMPLETE" != "1" ]; then
    if [ "$V6_COMMITTED" = "1" ]; then
      echo "post-commit v6 acceptance failed: step=$LAST_STEP rc=$rc; v6 preserved fail-closed, v5 fallback forbidden" >&2
      [ "$SERVICES_PAUSED" = "1" ] && resume_services || true
      stop_sudo_keepalive
      rm -f "$LOCAL_BACKUP_STAGE" "${LOCAL_BACKUP_STAGE}-journal"
      rm -rf "$ROLLBACK_DIR"
      exit "$rc"
    fi
    echo "pre-commit cutover failed: step=$LAST_STEP rc=$rc; restoring exact v5 production state" >&2
    if [ "$SERVICES_PAUSED" != "1" ] && { [ "$DB_MUTATED" = "1" ] || [ "$ROUTES_MUTATED" = "1" ] || [ "$ACTIVATION_MUTATED" = "1" ]; }; then
      pause_services || true
    fi
    [ "$DB_MUTATED" = "1" ] && restore_database || true
    [ "$ROUTES_MUTATED" = "1" ] && {
      cp -p "$ROLLBACK_DIR/amsc_zuuu_metar_taf_poll_once.sh" scripts/amsc_zuuu_metar_taf_poll_once.sh
      cp -p "$ROLLBACK_DIR/watch_zuuu_speci_once.py" scripts/watch_zuuu_speci_once.py
      cp -p "$ROLLBACK_DIR/polymarket_monitor_daemon.sh" scripts/polymarket_monitor_daemon.sh
    }
    [ "$ACTIVATION_MUTATED" = "1" ] && cp -p "$ROLLBACK_DIR/production_probability_runtime_activation.json" "$ACTIVE_ACTIVATION"
    [ "$SERVICES_PAUSED" = "1" ] && resume_services || true
    [ -f "${BACKUP}.partial" ] && mv "${BACKUP}.partial" "${BACKUP}.incomplete" 2>/dev/null || true
    echo "rollback_complete=true backup=$BACKUP log=$LOG" >&2
  fi
  stop_sudo_keepalive
  rm -f "$LOCAL_BACKUP_STAGE" "${LOCAL_BACKUP_STAGE}-journal"
  rm -rf "$ROLLBACK_DIR"
  exit "$rc"
}
trap recover EXIT

step "preflight_unique_production_successor"
test "$EVENT_DATE" = "$(TZ=America/New_York date +%F)"
case "$EXTERNAL_BACKUP_DIR/" in
  "$EXTERNAL_VOLUME_ROOT"/*) ;;
  *) echo "external backup directory is outside frozen volume root" >&2; exit 2 ;;
esac
test -d "$EXTERNAL_VOLUME_ROOT" -a -d "$EXTERNAL_BACKUP_DIR" -a -w "$EXTERNAL_BACKUP_DIR"
EXTERNAL_VOLUME_FS_ID="$(stat -f%d "$EXTERNAL_VOLUME_ROOT")"
ROOT_VOLUME_FS_ID="$(stat -f%d "$ROOT")"
verify_external_backup_mount
DB_BYTES="$(stat -f%z data/archive.db)"
LOCAL_AVAILABLE_KB="$(df -Pk /private/tmp | awk 'NR==2 {print $4}')"
REQUIRED_LOCAL_STAGE_KB="$((DB_BYTES / 1024 + 1048576))"
if [ "$LOCAL_AVAILABLE_KB" -lt "$REQUIRED_LOCAL_STAGE_KB" ]; then
  echo "local backup staging space insufficient: available_kb=$LOCAL_AVAILABLE_KB required_kb=$REQUIRED_LOCAL_STAGE_KB" >&2
  exit 2
fi
EXTERNAL_AVAILABLE_KB="$(df -Pk "$EXTERNAL_BACKUP_DIR" | awk 'NR==2 {print $4}')"
REQUIRED_BACKUP_KB="$((DB_BYTES / 1024 + 1048576))"
if [ "$EXTERNAL_AVAILABLE_KB" -lt "$REQUIRED_BACKUP_KB" ]; then
  echo "external backup space insufficient: available_kb=$EXTERNAL_AVAILABLE_KB required_kb=$REQUIRED_BACKUP_KB" >&2
  exit 2
fi
python3 scripts/probability_document_authority_audit.py --summary
python3 -m pytest -q \
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
  tests/test_physical_state_bayes_v6_schema_contract.py
python3 scripts/build_production_probability_v6_candidate_activation.py --write
CANDIDATE_FILE_SHA256="$(/usr/bin/shasum -a 256 "$CANDIDATE_ACTIVATION" | awk '{print $1}')"
RUNTIME_MANIFEST_SHA256="$(python3 - <<'PY'
import json
from pathlib import Path

candidate = json.loads(Path("config/production_probability_runtime_activation.v6_candidate.json").read_text(encoding="utf-8"))
print(candidate["runtime_manifest_sha256"])
PY
)"
verify_frozen_v6_runtime
python3 scripts/replay_physical_state_bayes_v6_canonical.py
python3 scripts/production_probability_v6_zero_order_drill.py
python3 scripts/production_probability_v6_host_rehearsal.py --db data/archive.db --event-date "$EVENT_DATE"
python3 - <<'PY'
from pathlib import Path
from scripts.production_probability_v6_readiness import audit

result = audit(Path.cwd(), Path("data/archive.db").resolve())
expected = {
    "v6_live_routes_not_active",
    "v6_additive_schema_not_applied",
    "v6_active_activation_not_installed",
}
actual = set(result.get("blockers") or [])
if actual != expected:
    raise SystemExit(f"unexpected_pre_cutover_blockers:{sorted(actual)}")
print("pre_cutover_blockers_are_exact=true")
PY
if [ "${PHYSICAL_STATE_BAYES_V6_PREFLIGHT_ONLY:-0}" = "1" ]; then
  CUTOVER_COMPLETE=1
  trap - EXIT
  rm -rf "$ROLLBACK_DIR"
  echo "preflight_state=ready_for_controlled_production_cutover"
  exit 0
fi
sudo -v
start_sudo_keepalive

step "freeze_exact_rollback_inputs"
cp -p "$ACTIVE_ACTIVATION" "$ROLLBACK_DIR/production_probability_runtime_activation.json"
cp -p scripts/amsc_zuuu_metar_taf_poll_once.sh "$ROLLBACK_DIR/amsc_zuuu_metar_taf_poll_once.sh"
cp -p scripts/watch_zuuu_speci_once.py "$ROLLBACK_DIR/watch_zuuu_speci_once.py"
cp -p scripts/polymarket_monitor_daemon.sh "$ROLLBACK_DIR/polymarket_monitor_daemon.sh"

step "pause_all_archive_writers"
pause_services
verify_quiescence
verify_external_backup_mount

step "create_verified_external_database_backup"
checkpoint="$(sqlite3 data/archive.db 'PRAGMA wal_checkpoint(TRUNCATE);')"
test "$checkpoint" = "0|0|0"
rm -f "$LOCAL_BACKUP_STAGE" "${LOCAL_BACKUP_STAGE}-journal"
sqlite3 data/archive.db ".backup '$LOCAL_BACKUP_STAGE'"
sqlite3 "$LOCAL_BACKUP_STAGE" 'PRAGMA integrity_check;' | grep -qx ok
LOCAL_BACKUP_SHA256="$(/usr/bin/shasum -a 256 "$LOCAL_BACKUP_STAGE" | awk '{print $1}')"
verify_external_backup_mount
/bin/cp "$LOCAL_BACKUP_STAGE" "${BACKUP}.partial"
verify_external_backup_mount
test "$(stat -f%z "$LOCAL_BACKUP_STAGE")" = "$(stat -f%z "${BACKUP}.partial")"
verify_external_backup_mount
EXTERNAL_BACKUP_SHA256="$(/usr/bin/shasum -a 256 "${BACKUP}.partial" | awk '{print $1}')"
test "$LOCAL_BACKUP_SHA256" = "$EXTERNAL_BACKUP_SHA256"
mv "${BACKUP}.partial" "$BACKUP"
test -s "$BACKUP"

step "verify_frozen_v6_runtime_after_backup"
verify_frozen_v6_runtime

step "apply_additive_v6_schema"
DB_MUTATED=1
python3 scripts/activate_physical_state_bayes_v6_schema.py \
  --db data/archive.db --apply --writer-quiescent \
  --confirm ACTIVATE_PHYSICAL_STATE_BAYES_V6_SCHEMA

step "activate_metar_speci_and_taf_amd_v6_routes"
ROUTES_MUTATED=1
python3 production_probability_v6_live_route_contract.py \
  --apply --confirm ACTIVATE_PHYSICAL_STATE_BAYES_V6_LIVE_ROUTES

step "install_unique_v6_activation"
ACTIVATION_MUTATED=1
cp "$CANDIDATE_ACTIVATION" "${ACTIVE_ACTIVATION}.tmp.$$"
mv "${ACTIVE_ACTIVATION}.tmp.$$" "$ACTIVE_ACTIVATION"

step "bootstrap_v6_p0_and_drain_all_canonical_events"
python3 scripts/bootstrap_production_probability_v6.py \
  --db data/archive.db \
  --activation "$ACTIVE_ACTIVATION" \
  --event-date "$EVENT_DATE" \
  --decision-as-of "$DECISION_AS_OF" \
  --apply --confirm BOOTSTRAP_PRODUCTION_PHYSICAL_STATE_BAYES_V6

step "accept_unique_v6_before_service_resume"
python3 scripts/production_probability_v6_readiness.py --db data/archive.db --summary
python3 scripts/production_probability_v6_live_acceptance.py \
  --db data/archive.db --activation "$ACTIVE_ACTIVATION" --event-date "$EVENT_DATE"
V6_COMMITTED=1

step "resume_services_on_v6_only"
resume_services

step "post_resume_read_only_acceptance"
post_resume_accepted=0
for attempt in $(seq 1 15); do
  if python3 scripts/production_probability_v6_live_acceptance.py \
    --db data/archive.db --activation "$ACTIVE_ACTIVATION" --event-date "$EVENT_DATE"; then
    post_resume_accepted=1
    break
  fi
  [ "$attempt" = "15" ] || sleep 1
done
test "$post_resume_accepted" = "1"

CUTOVER_COMPLETE=1
trap - EXIT
stop_sudo_keepalive
rm -f "$LOCAL_BACKUP_STAGE" "${LOCAL_BACKUP_STAGE}-journal"
rm -rf "$ROLLBACK_DIR"
echo "cutover_state=production_v6_active_v5_retired_no_fallback"
echo "backup=$BACKUP"
echo "log=$LOG"
