#!/bin/bash
set -euo pipefail

echo "RETIRED_EXTERNAL_DISK_PRODUCTION_CUTOVER_FORBIDDEN: use scripts/cutover_v6_probability_and_soft_market_projection.sh" >&2
exit 2

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

EVENT_DATE="${REPORTED_X_EVENT_DATE:-$(TZ=Asia/Shanghai date +%F)}"
DECISION_AS_OF="${REPORTED_X_DECISION_AS_OF:-$(date -u +%Y-%m-%dT%H:%M:%S.000Z)}"
ACTIVE_ACTIVATION="$ROOT/config/production_probability_runtime_activation.json"
CANDIDATE_ACTIVATION="$ROOT/config/production_probability_runtime_activation.reported_x_candidate.json"
LOCAL_BACKUP_DIR="${REPORTED_X_LOCAL_BACKUP_DIR:-$ROOT/data/local_cutover_backups}"
EXTERNAL_BACKUP_DIR="${REPORTED_X_EXTERNAL_BACKUP_DIR:-/Volumes/Expansion/熊/backups}"
DRILL_RECEIPT="$ROOT/data/logs/reported_x_successor_no_order_drill_latest.json"
LOG="$ROOT/data/logs/reported_x_probability_epoch_cutover.log"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

mkdir -p "$ROOT/data/logs" "$LOCAL_BACKUP_DIR"
exec > >(tee -a "$LOG") 2>&1

LAST_STEP="initialize"
CUTOVER_COMPLETE=0
SERVICES_PAUSED=0
SERVICES_RUNNING=1
DB_MUTATED=0
PATCH_APPLIED=0
BACKUP=""
ROLLBACK_DIR="$(mktemp -d)"
CORE_FILES=(
  sequential_bayes_filter_shadow_runtime.py
  sequential_bayes_shadow_pipeline.py
  p2_canonical_amsc_forward_writer.py
  scripts/apply_amsc_probability_fast_lane_from_plan.py
  scripts/watch_zuuu_speci_once.py
  scripts/apply_amsc_taf_forecast_revision_from_plan.py
  scripts/apply_production_canonical_probability_updates.py
)

step() {
  LAST_STEP="$1"
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] step=$LAST_STEP"
}

pause_services() {
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
  sudo launchctl disable system/com.weather.monitor.daemon || true
  sudo launchctl bootout system /Library/LaunchDaemons/com.weather.monitor.daemon.plist 2>/dev/null || true
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
  SERVICES_PAUSED=1
  SERVICES_RUNNING=0
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
  SERVICES_RUNNING=1
}

restore_exact_database() {
  [ -n "$BACKUP" ] && [ -f "$BACKUP" ] || return 0
  rm -f data/archive.db-wal data/archive.db-shm
  cp -c "$BACKUP" data/.archive.db.restore.$$ 
  mv data/.archive.db.restore.$$ data/archive.db
}

restore_source_and_activation() {
  local file
  for file in "${CORE_FILES[@]}"; do
    if [ -f "$ROLLBACK_DIR/$file" ]; then
      mkdir -p "$(dirname "$file")"
      cp "$ROLLBACK_DIR/$file" "$file"
    fi
  done
  [ -f "$ROLLBACK_DIR/production_probability_runtime_activation.json" ] && \
    cp "$ROLLBACK_DIR/production_probability_runtime_activation.json" "$ACTIVE_ACTIVATION"
}

recover() {
  rc=$?
  if [ "$CUTOVER_COMPLETE" != "1" ]; then
    echo "cutover failed: step=$LAST_STEP rc=$rc; restoring exact pre-cutover state" >&2
    if [ "$SERVICES_RUNNING" = "1" ] && { [ "$DB_MUTATED" = "1" ] || [ "$PATCH_APPLIED" = "1" ]; }; then
      if ! pause_services; then
        echo "rollback refused: writers could not be proven quiescent; backup=$BACKUP source_rollback=$ROLLBACK_DIR" >&2
        exit 99
      fi
    fi
    if [ "$DB_MUTATED" = "1" ]; then
      restore_exact_database || true
    fi
    if [ "$PATCH_APPLIED" = "1" ]; then
      restore_source_and_activation || true
    fi
  fi
  if [ "$SERVICES_PAUSED" = "1" ]; then
    resume_services || true
  fi
  rm -rf "$ROLLBACK_DIR"
  exit "$rc"
}
trap recover EXIT

step "preflight"
test "$EVENT_DATE" = "$(TZ=Asia/Shanghai date +%F)"
test -d "$EXTERNAL_BACKUP_DIR" -a -w "$EXTERNAL_BACKUP_DIR"
test -s "$DRILL_RECEIPT"
git apply --check scripts/reported_x_successor_core.patch
python3 scripts/production_probability_transaction_b_cutover_readiness.py \
  --db data/archive.db --summary >/dev/null
python3 -m pytest -q \
  tests/test_report_defined_physical_x_runtime.py \
  tests/test_reported_x_successor_checkpoint_runtime.py \
  tests/test_production_reported_x_public_ssot_writer.py \
  tests/test_production_reported_x_initial_checkpoint.py \
  tests/test_production_reported_x_initial_writer.py \
  tests/test_production_reported_x_weak_revision_writer.py \
  tests/test_production_reported_x_weak_revision_event_runtime.py \
  tests/test_production_reported_x_canonical_event_drain_runtime.py
sudo -v

step "freeze_rollback_sources"
for file in "${CORE_FILES[@]}"; do
  mkdir -p "$ROLLBACK_DIR/$(dirname "$file")"
  cp "$file" "$ROLLBACK_DIR/$file"
done
cp "$ACTIVE_ACTIVATION" "$ROLLBACK_DIR/production_probability_runtime_activation.json"

step "pause_all_writers"
pause_services
python3 scripts/controlled_apply_maintenance_window_packet.py \
  --db data/archive.db --summary --max-items-output 20

step "create_exact_local_backup"
checkpoint_result="$(sqlite3 data/archive.db 'PRAGMA wal_checkpoint(TRUNCATE);')"
test "$checkpoint_result" = "0|0|0"
BACKUP="$LOCAL_BACKUP_DIR/archive.pre-reported-x.${STAMP}.db"
cp -c data/archive.db "${BACKUP}.partial"
sqlite3 "${BACKUP}.partial" 'PRAGMA schema_version; PRAGMA page_count; PRAGMA page_size;' >/dev/null
mv "${BACKUP}.partial" "$BACKUP"
touch "$BACKUP"
test -s "$BACKUP"

step "switch_runtime_sources"
git apply scripts/reported_x_successor_core.patch
PATCH_APPLIED=1
python3 -m py_compile \
  sequential_bayes_filter_shadow_runtime.py \
  sequential_bayes_shadow_pipeline.py \
  p2_canonical_amsc_forward_writer.py \
  production_reported_x_amsc_archive_probability_ingress.py \
  scripts/apply_amsc_probability_fast_lane_from_plan.py \
  scripts/watch_zuuu_speci_once.py \
  scripts/apply_amsc_taf_forecast_revision_from_plan.py \
  scripts/apply_production_canonical_probability_updates.py
python3 - "$DRILL_RECEIPT" <<'PY'
import json, sys
from production_probability_successor_runtime_manifest import successor_runtime_manifest_sha256
receipt = json.load(open(sys.argv[1], encoding="utf-8"))
if receipt.get("ok") is not True or receipt.get("state") != "accepted":
    raise SystemExit("reported_x_no_order_drill_not_accepted")
if receipt.get("runtime_manifest_sha256") != successor_runtime_manifest_sha256():
    raise SystemExit("reported_x_no_order_drill_manifest_drift")
if any(int(value) != 0 for value in (receipt.get("zero_order_table_deltas") or {}).values()):
    raise SystemExit("reported_x_no_order_drill_created_execution_rows")
PY

step "apply_additive_reported_x_schema"
DB_MUTATED=1
python3 scripts/activate_reported_x_successor_schema.py \
  --db data/archive.db --backup "$BACKUP" --apply --writer-quiescent \
  --confirm ACTIVATE_REPORTED_X_SUCCESSOR_SCHEMA

step "materialize_active_successor_activation"
python3 scripts/materialize_reported_x_successor_activation.py \
  --event-date "$EVENT_DATE" --decision-as-of "$DECISION_AS_OF" \
  --output "$ACTIVE_ACTIVATION" --apply --writer-quiescent \
  --confirm ACTIVATE_REPORTED_X_PROBABILITY_MAINLINE

step "commit_p0_and_replay_all_canonical_events"
python3 scripts/bootstrap_reported_x_probability_p0.py \
  --db data/archive.db --activation "$ACTIVE_ACTIVATION" \
  --event-date "$EVENT_DATE" --decision-as-of "$DECISION_AS_OF" \
  --apply --confirm BOOTSTRAP_REPORTED_X_PROBABILITY_P0

step "accept_reported_x_ssot_and_zero_order_boundary"
python3 scripts/production_reported_x_successor_acceptance.py \
  --db data/archive.db --baseline-db "$BACKUP" \
  --activation "$ACTIVE_ACTIVATION" --event-date "$EVENT_DATE" --summary
python3 scripts/apply_production_canonical_probability_updates.py \
  --db data/archive.db --activation "$ACTIVE_ACTIVATION" \
  --event-date "$EVENT_DATE"
python3 scripts/production_trade_gate_zero_audit.py

step "resume_services"
resume_services
sleep 2
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

CUTOVER_COMPLETE=1
step "copy_exact_pre_cutover_backup_to_external_volume"
EXTERNAL_BACKUP="$EXTERNAL_BACKUP_DIR/$(basename "$BACKUP")"
cp "$BACKUP" "${EXTERNAL_BACKUP}.partial"
cmp "$BACKUP" "${EXTERNAL_BACKUP}.partial"
mv "${EXTERNAL_BACKUP}.partial" "$EXTERNAL_BACKUP"

trap - EXIT
rm -rf "$ROLLBACK_DIR"
echo "reported_x_probability_cutover_complete backup=$BACKUP external_backup=$EXTERNAL_BACKUP log=$LOG"
