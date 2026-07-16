#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"

EVENT_DATE="${REPORTED_X_REFRESH_EVENT_DATE:-$(TZ=Asia/Shanghai date +%F)}"
DECISION_AS_OF="${REPORTED_X_REFRESH_DECISION_AS_OF:-$(date -u +%Y-%m-%dT%H:%M:%S.000Z)}"
ACTIVE_ACTIVATION="$ROOT/config/production_probability_runtime_activation.json"
BACKUP_DIR="${REPORTED_X_REFRESH_BACKUP_DIR:-$ROOT/data/local_cutover_backups}"
EXTERNAL_BACKUP_DIR="${REPORTED_X_REFRESH_EXTERNAL_BACKUP_DIR:-/Volumes/Expansion/熊/backups}"
LOG="$ROOT/data/logs/reported_x_runtime_manifest_epoch_refresh.log"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

mkdir -p "$ROOT/data/logs" "$BACKUP_DIR"
exec > >(tee -a "$LOG") 2>&1

LAST_STEP="initialize"
SERVICES_PAUSED=0
DB_MUTATED=0
BACKUP=""
ACTIVATION_BACKUP="$(mktemp)"

step() {
  LAST_STEP="$1"
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] step=$LAST_STEP"
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
  if [ -n "$pids" ]; then
    sudo kill -KILL $pids 2>/dev/null || true
    sleep 0.25
  fi
  pids="$(system_monitor_process_group_pids "$monitor_pgids")"
  if [ -n "$pids" ]; then
    echo "system monitor process group survived maintenance pause: $pids" >&2
    return 1
  fi
  return 0
}

pause_services() {
  # This flag means recovery is now responsible for restoring services, even
  # if a later pause step fails before writer quiescence is reached.
  SERVICES_PAUSED=1
  # The system monitor owns root writer children. Stop their supervisor before
  # user-domain weatherctl tries to retire repository writer processes.
  stop_system_monitor
  scripts/weatherctl.sh stop
  for label in \
    com.xiong.weather.ui \
    com.weather.market_ws \
    com.weather.task_queue_worker \
    com.weather.target_realization_worker \
    com.weather.trade_engine \
    com.weather.auto_trade_entry_1m; do
    launchctl bootout "gui/$(id -u)/${label}" 2>/dev/null || \
      launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/${label}.plist" 2>/dev/null || true
  done
  pids="$(pgrep -f "$ROOT/scan_weather_market.py.*--auto-trade-job-worker" 2>/dev/null || true)"
  [ -z "$pids" ] || kill -TERM $pids 2>/dev/null || true
  for _ in $(seq 1 40); do
    pgrep -f "$ROOT/scan_weather_market.py.*--auto-trade-job-worker" >/dev/null 2>&1 || break
    sleep 0.25
  done
  if pgrep -f "$ROOT/scan_weather_market.py.*--auto-trade-job-worker" >/dev/null 2>&1; then
    echo "auto-trade writer survived maintenance pause" >&2
    return 1
  fi
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

prune_superseded_local_refresh_backups() {
  local candidate=""
  local deleted_list=""
  local receipt=""
  deleted_list="$(mktemp)"
  receipt="$BACKUP_DIR/prune_manifest.reported_x_refresh.${STAMP}.json"
  while IFS= read -r candidate; do
    [ -n "$candidate" ] || continue
    [ "$candidate" = "$BACKUP" ] && continue
    rm -f \
      "$candidate" "$candidate-wal" "$candidate-shm" \
      "$candidate.partial" "$candidate.partial-wal" "$candidate.partial-shm"
    echo "$candidate" >> "$deleted_list"
  done < <(find "$BACKUP_DIR" -maxdepth 1 -type f \
    -name 'archive.pre-reported-x-manifest-refresh.*.db' | sort)
  "$PYTHON_BIN" - "$deleted_list" "$BACKUP" "$receipt" "$EXTERNAL_BACKUP_STATUS" <<'PY'
import json
import os
from pathlib import Path
import sys
import tempfile
from datetime import UTC, datetime

deleted_path, retained_path, receipt_path, external_status = sys.argv[1:]
deleted = [line for line in Path(deleted_path).read_text(encoding="utf-8").splitlines() if line]
payload = {
    "schema": "reported_x_refresh_local_backup_prune_manifest_v1",
    "created_at_utc": datetime.now(UTC).isoformat(),
    "state": "completed",
    "external_backup_status": external_status,
    "deleted_superseded_local_backups": deleted,
    "retained_current_local_backup": retained_path,
}
destination = Path(receipt_path)
descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
try:
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, destination)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
Path(deleted_path).unlink(missing_ok=True)
PY
}

recover() {
  rc=$?
  if [ "$rc" != "0" ]; then
    echo "reported-X manifest epoch refresh failed: step=$LAST_STEP rc=$rc" >&2
    if [ "$DB_MUTATED" = "1" ] && [ "$SERVICES_PAUSED" != "1" ]; then
      if ! pause_services; then
        echo "rollback refused: writers could not be proven quiescent; backup=$BACKUP" >&2
        exit 99
      fi
    fi
    if [ "$DB_MUTATED" = "1" ] && [ -s "$BACKUP" ]; then
      rm -f data/archive.db-wal data/archive.db-shm
      cp -c "$BACKUP" data/.archive.db.restore.$$
      mv data/.archive.db.restore.$$ data/archive.db
    fi
    if [ -s "$ACTIVATION_BACKUP" ]; then
      cp "$ACTIVATION_BACKUP" "$ACTIVE_ACTIVATION"
    fi
  fi
  if [ "$SERVICES_PAUSED" = "1" ]; then
    resume_services || true
  fi
  rm -f "$ACTIVATION_BACKUP"
  exit "$rc"
}
trap recover EXIT

step "preflight_document_and_runtime_tests"
test "$EVENT_DATE" = "$(TZ=Asia/Shanghai date +%F)"
test -s "$ACTIVE_ACTIVATION"
python3 scripts/probability_document_authority_audit.py --summary
python3 - <<'PY'
from production_probability_successor_runtime_manifest import audit_successor_runtime_manifest_dependency_closure

result = audit_successor_runtime_manifest_dependency_closure()
if result.get("ok") is not True:
    raise SystemExit(f"reported-X runtime dependency closure blocked: {result}")
print(result)
PY
python3 -m pytest -q \
  tests/test_realization_state_projection_runtime.py \
  tests/test_strong_physical_evidence_projection_runtime.py \
  tests/test_production_probability_successor_activation_contract.py \
  tests/test_production_reported_x_public_ssot_writer.py \
  tests/test_production_reported_x_initial_writer.py \
  tests/test_production_reported_x_canonical_event_drain_runtime.py \
  tests/test_production_reported_x_runtime_readiness.py \
  tests/test_production_reported_x_amsc_archive_probability_ingress.py \
  tests/test_p2_canonical_amsc_forward_writer.py \
  tests/test_archive_db_write_queue.py \
  tests/test_fetch_zuuu_metar_taf.py \
  tests/test_trade_strategy_loop_audit.py \
  tests/test_amsc_poll_fast_lane_ordering.py \
  tests/test_amsc_poller_release_window_static.py
python3 trade_strategy_loop_audit.py \
  --db data/archive.db --event-date "$EVENT_DATE" --summary
sudo -v

step "freeze_activation_and_pause_all_writers"
cp "$ACTIVE_ACTIVATION" "$ACTIVATION_BACKUP"
pause_services
python3 scripts/controlled_apply_maintenance_window_packet.py \
  --db data/archive.db --summary --max-items-output 20

step "prove_archive_writer_gate_reclaimable"
python3 - <<'PY'
from archive_db_write_queue import archive_db_write_gate

# A governed writer can be terminated milliseconds after it has durably
# claimed the filesystem queue. The quiescence packet above proves there is
# no live archive.db holder; this bounded claim additionally proves that an
# orphan/zombie queue owner has been reclaimed before backup and P0 replay.
with archive_db_write_gate(
    "data/archive.db",
    owner="reported_x_manifest_refresh_quiescence_probe",
    wait_sec=5.0,
    poll_sec=0.05,
):
    pass
print("archive writer gate reclaimable")
PY

step "create_exact_pre_refresh_backup"
checkpoint_result="$(sqlite3 data/archive.db 'PRAGMA wal_checkpoint(TRUNCATE);')"
test "$checkpoint_result" = "0|0|0"
BACKUP="$BACKUP_DIR/archive.pre-reported-x-manifest-refresh.${STAMP}.db"
cp -c data/archive.db "${BACKUP}.partial"
sqlite3 "${BACKUP}.partial" 'PRAGMA schema_version; PRAGMA page_count; PRAGMA page_size;' >/dev/null
mv "${BACKUP}.partial" "$BACKUP"
test -s "$BACKUP"

step "materialize_new_orderless_runtime_activation"
python3 scripts/materialize_reported_x_successor_activation.py \
  --event-date "$EVENT_DATE" --decision-as-of "$DECISION_AS_OF" \
  --output "$ACTIVE_ACTIVATION" --apply --writer-quiescent \
  --confirm ACTIVATE_REPORTED_X_PROBABILITY_MAINLINE

step "commit_new_p0_generation_and_replay_all_canonical_events"
DB_MUTATED=1
python3 scripts/bootstrap_reported_x_probability_p0.py \
  --db data/archive.db --activation "$ACTIVE_ACTIVATION" \
  --event-date "$EVENT_DATE" --decision-as-of "$DECISION_AS_OF" \
  --apply --confirm BOOTSTRAP_REPORTED_X_PROBABILITY_P0

step "archive_pending_plans_consumed_by_new_checkpoint"
sudo "$PYTHON_BIN" scripts/amsc_probability_pending_queue.py archive-consumed \
  --pending-dir data/amsc_zuuu_metar_taf/pending_probability \
  --db data/archive.db --activation "$ACTIVE_ACTIVATION" \
  --event-date "$EVENT_DATE" --apply \
  --confirm ARCHIVE_CONSUMED_AMSC_PROBABILITY_PLANS

step "zero_order_acceptance"
python3 scripts/production_reported_x_successor_acceptance.py \
  --db data/archive.db --baseline-db "$BACKUP" \
  --activation "$ACTIVE_ACTIVATION" --event-date "$EVENT_DATE" --summary
python3 scripts/production_trade_gate_zero_audit.py

step "resume_services_and_verify_readiness"
resume_services
readiness_ok=0
for _ in $(seq 1 15); do
  if python3 scripts/production_reported_x_runtime_readiness.py \
    --db data/archive.db --activation "$ACTIVE_ACTIVATION" \
    --event-date "$EVENT_DATE"; then
    readiness_ok=1
    break
  fi
  sleep 1
done
test "$readiness_ok" = "1"

step "verify_unique_probability_and_safe_trade_loop"
python3 scripts/production_chain_uniqueness_audit.py \
  --db data/archive.db --summary
python3 trade_strategy_loop_audit.py \
  --db data/archive.db --event-date "$EVENT_DATE" --summary

step "copy_backup_to_external_volume_if_available"
EXTERNAL_BACKUP_STATUS="not_mounted"
if [ -d "$EXTERNAL_BACKUP_DIR" ] && [ -w "$EXTERNAL_BACKUP_DIR" ]; then
  external="$EXTERNAL_BACKUP_DIR/$(basename "$BACKUP")"
  cp "$BACKUP" "${external}.partial"
  cmp "$BACKUP" "${external}.partial"
  mv "${external}.partial" "$external"
  EXTERNAL_BACKUP_STATUS="copied"
fi

DB_MUTATED=0
trap - EXIT
rm -f "$ACTIVATION_BACKUP"
step "prune_superseded_local_refresh_backups"
prune_superseded_local_refresh_backups
echo "reported_x_runtime_manifest_epoch_refresh_complete backup=$BACKUP external_backup_status=$EXTERNAL_BACKUP_STATUS log=$LOG"
