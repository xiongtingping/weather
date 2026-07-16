#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

EVENT_DATE="${STRONG_V5_EVENT_DATE:-$(TZ=Asia/Shanghai date +%F)}"
DECISION_AS_OF="${STRONG_V5_DECISION_AS_OF:-$(date -u +%Y-%m-%dT%H:%M:%S.000Z)}"
ACTIVE_ACTIVATION="$ROOT/config/production_probability_runtime_activation.json"
CANDIDATE_ACTIVATION="$ROOT/config/production_probability_runtime_activation.reported_x_candidate.json"
LOCAL_BACKUP_DIR="${STRONG_V5_LOCAL_BACKUP_DIR:-$ROOT/data/local_cutover_backups}"
EXTERNAL_BACKUP_DIR="${STRONG_V5_EXTERNAL_BACKUP_DIR:-/Volumes/Expansion/熊/backups}"
DRILL_RECEIPT="$ROOT/data/logs/cumulative_strong_likelihood_v5_no_order_drill.json"
LOG="$ROOT/data/logs/cumulative_strong_likelihood_v5_cutover.log"
ACCEPTANCE_RECEIPT="$ROOT/data/logs/cumulative_strong_likelihood_v5_live_acceptance.json"
READINESS_RECEIPT="$ROOT/data/logs/cumulative_strong_likelihood_v5_live_readiness.json"
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
  strong_observation_lr_evaluator.py
  strong_observation_lr_operational_state_baseline.py
  sequential_bayes_filter_shadow_runtime.py
  sequential_bayes_shadow_pipeline.py
  production_probability_successor_activation_contract.py
  reported_x_successor_checkpoint_runtime.py
  reported_x_successor_checkpoint_reader.py
  production_reported_x_public_ssot_writer.py
  scripts/production_reported_x_successor_acceptance.py
  scripts/production_reported_x_runtime_readiness.py
  tests/test_strong_observation_lr_operational_state_baseline.py
  tests/test_sequential_bayes_filter_shadow_runtime.py
  tests/test_reported_x_successor_checkpoint_runtime.py
  tests/test_production_reported_x_public_ssot_writer.py
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
python3 scripts/probability_document_authority_audit.py --summary

# This patch-based command owns only the initial v5 cutover.  Once a v5
# activation exists, runtime source changes must create a new manifest epoch;
# attempting to re-apply the frozen migration patch is both ambiguous and
# unnecessary.  Fail before touching services, source, activation, or the DB
# and give the operator the single current maintenance command.
if python3 - "$ACTIVE_ACTIVATION" <<'PY'
import json
import sys

path = sys.argv[1]
try:
    activation = json.load(open(path, encoding="utf-8"))
except (FileNotFoundError, json.JSONDecodeError, OSError):
    raise SystemExit(1)

formula = str(activation.get("bayes_formula_id") or "")
composer = str(activation.get("composer_schema") or "")
is_v5 = (
    formula
    == "p0_prior_z_weak_once_then_canonical_cumulative_evidence_replacement_delta_log_lr_then_weak_revision_replacement_then_reported_x_projection_v3"
    and composer == "sequential_bayes_filter_reported_x_v5_cumulative_replacement"
)
raise SystemExit(0 if is_v5 else 1)
PY
then
  cat >&2 <<'EOF'
initial v5 cutover already complete; no mutation performed
this command must not be rerun against an existing v5 activation
current maintenance command:
  cd /Users/xiong/weather
  sudo -v
  bash scripts/refresh_reported_x_runtime_manifest_epoch.sh
EOF
  CUTOVER_COMPLETE=1
  exit 2
fi

# This is a one-time cutover.  A later invocation must not try to re-apply the
# migration patch or roll an already healthy production epoch backwards.  The
# production readiness receipt is authoritative, but "ready" alone is not
# sufficient: require the v5 cumulative-replacement identity from the latest
# committed strong update before treating the cutover as already complete.
if ! git apply --check scripts/cumulative_strong_likelihood_v5.patch >/dev/null 2>&1; then
  CURRENT_READINESS="$ROLLBACK_DIR/current_probability_runtime_readiness.json"
  if python3 scripts/production_reported_x_runtime_readiness.py \
      --db data/archive.db --activation "$ACTIVE_ACTIVATION" \
      --event-date "$EVENT_DATE" >"$CURRENT_READINESS" && \
    python3 - "$CURRENT_READINESS" <<'PY'
import json
import sys

receipt = json.load(open(sys.argv[1], encoding="utf-8"))
identity = ((receipt.get("evidence") or {}).get("strong_likelihood_update_identity") or {})
if not (
    receipt.get("ok") is True
    and receipt.get("state") == "ready"
    and identity.get("likelihood_update_kind")
    == "cumulative_evidence_replacement_delta_log_lr_v1"
):
    raise SystemExit(1)
PY
  then
    cat "$CURRENT_READINESS"
    echo "cutover_state=already_production_ready; no source, database, service, or order mutation performed"
    CUTOVER_COMPLETE=1
    exit 0
  fi
  cat "$CURRENT_READINESS" 2>/dev/null || true
  echo "preflight failed: v5 patch is not applicable and current production cannot prove the cumulative-replacement identity" >&2
  exit 2
fi

echo "preflight_check=current_probability_runtime_readiness"
if ! python3 scripts/production_reported_x_runtime_readiness.py \
  --db data/archive.db --activation "$ACTIVE_ACTIVATION" \
  --event-date "$EVENT_DATE"; then
  echo "preflight failed: current probability runtime is not ready; the JSON above is the authoritative blocker list" >&2
  exit 2
fi
python3 -m pytest -q \
  tests/test_probability_document_authority_audit.py \
  tests/test_strong_observation_lr_operational_state_baseline.py \
  tests/test_sequential_bayes_filter_shadow_runtime.py \
  tests/test_sequential_bayes_shadow_pipeline.py \
  tests/test_reported_x_successor_checkpoint_runtime.py \
  tests/test_production_probability_successor_activation_contract.py \
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
BACKUP="$LOCAL_BACKUP_DIR/archive.pre-strong-v5.${STAMP}.db"
cp -c data/archive.db "${BACKUP}.partial"
sqlite3 "${BACKUP}.partial" 'PRAGMA schema_version; PRAGMA page_count; PRAGMA page_size;' >/dev/null
mv "${BACKUP}.partial" "$BACKUP"
touch "$BACKUP"
test -s "$BACKUP"

step "switch_runtime_sources"
git apply scripts/cumulative_strong_likelihood_v5.patch
PATCH_APPLIED=1
python3 -m py_compile \
  strong_observation_lr_evaluator.py \
  strong_observation_lr_operational_state_baseline.py \
  sequential_bayes_filter_shadow_runtime.py \
  sequential_bayes_shadow_pipeline.py \
  production_probability_successor_activation_contract.py \
  reported_x_successor_checkpoint_runtime.py \
  reported_x_successor_checkpoint_reader.py \
  production_reported_x_public_ssot_writer.py \
  scripts/production_reported_x_successor_acceptance.py \
  scripts/production_reported_x_runtime_readiness.py
python3 - "$DRILL_RECEIPT" <<'PY'
import json, sys
from production_probability_successor_runtime_manifest import successor_runtime_manifest_sha256
receipt = json.load(open(sys.argv[1], encoding="utf-8"))
if receipt.get("ok") is not True or receipt.get("state") != "accepted":
    raise SystemExit("cumulative_strong_v5_no_order_drill_not_accepted")
if receipt.get("runtime_manifest_sha256") != successor_runtime_manifest_sha256():
    raise SystemExit("cumulative_strong_v5_no_order_drill_manifest_drift")
if any(int(value) != 0 for value in (receipt.get("zero_order_table_deltas") or {}).values()):
    raise SystemExit("cumulative_strong_v5_no_order_drill_created_execution_rows")
PY
python3 -m pytest -q \
  tests/test_strong_observation_lr_operational_state_baseline.py \
  tests/test_sequential_bayes_filter_shadow_runtime.py \
  tests/test_sequential_bayes_shadow_pipeline.py \
  tests/test_reported_x_successor_checkpoint_runtime.py \
  tests/test_production_reported_x_public_ssot_writer.py \
  tests/test_production_probability_successor_activation_contract.py

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
  --activation "$ACTIVE_ACTIVATION" --event-date "$EVENT_DATE" --summary \
  | tee "${ACCEPTANCE_RECEIPT}.tmp"
python3 - "${ACCEPTANCE_RECEIPT}.tmp" <<'PY'
import json, sys
receipt = json.load(open(sys.argv[1], encoding="utf-8"))
if receipt.get("ok") is not True or receipt.get("state") != "accepted":
    raise SystemExit("cumulative_strong_v5_live_acceptance_not_accepted")
if any(int(value) != 0 for value in (receipt.get("zero_order_table_deltas") or {}).values()):
    raise SystemExit("cumulative_strong_v5_live_acceptance_created_execution_rows")
PY
mv "${ACCEPTANCE_RECEIPT}.tmp" "$ACCEPTANCE_RECEIPT"
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
    --event-date "$EVENT_DATE" >"${READINESS_RECEIPT}.tmp"; then
    cat "${READINESS_RECEIPT}.tmp"
    mv "${READINESS_RECEIPT}.tmp" "$READINESS_RECEIPT"
    readiness_ok=1
    break
  fi
  cat "${READINESS_RECEIPT}.tmp" 2>/dev/null || true
  sleep 1
done
test "$readiness_ok" = "1"

CUTOVER_COMPLETE=1
step "copy_exact_pre_cutover_backup_to_external_volume"
EXTERNAL_BACKUP="$EXTERNAL_BACKUP_DIR/$(basename "$BACKUP")"
EXTERNAL_BACKUP_STATUS="copied"
EXTERNAL_BACKUP_DEFERRED_RECEIPT=""
if cp "$BACKUP" "${EXTERNAL_BACKUP}.partial" \
  && cmp "$BACKUP" "${EXTERNAL_BACKUP}.partial" \
  && mv "${EXTERNAL_BACKUP}.partial" "$EXTERNAL_BACKUP"; then
  :
else
  external_backup_rc=$?
  EXTERNAL_BACKUP_STATUS="deferred"
  rm -f "${EXTERNAL_BACKUP}.partial"
  EXTERNAL_BACKUP_DEFERRED_RECEIPT="$LOCAL_BACKUP_DIR/external_backup_deferred.${STAMP}.json"
  python3 - \
    "$EXTERNAL_BACKUP_DEFERRED_RECEIPT" "$BACKUP" "$EXTERNAL_BACKUP" \
    "$external_backup_rc" "$ACTIVE_ACTIVATION" <<'PY'
import json
import os
from pathlib import Path
import sys
from datetime import datetime, timezone

receipt_path, source, destination, returncode, activation_path = map(Path, sys.argv[1:])
activation = json.loads(activation_path.read_text(encoding="utf-8"))
payload = {
    "schema": "cumulative_strong_likelihood_v5_external_backup_deferred_v1",
    "state": "external_backup_deferred_core_cutover_accepted",
    "created_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    "source_backup": str(source.resolve()),
    "source_size_bytes": source.stat().st_size,
    "destination_backup": str(destination),
    "copy_returncode": int(str(returncode)),
    "activation_effective_at": activation.get("activation_effective_at"),
    "runtime_manifest_sha256": activation.get("runtime_manifest_sha256"),
    "core_probability_cutover_accepted": True,
    "requires_operator_backup_retry": True,
    "places_orders": False,
}
encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
temporary = receipt_path.with_name(f".{receipt_path.name}.tmp.{os.getpid()}")
temporary.write_bytes(encoded)
os.replace(temporary, receipt_path)
PY
fi

trap - EXIT
rm -rf "$ROLLBACK_DIR"
echo "cumulative_strong_likelihood_v5_cutover_complete backup=$BACKUP external_backup=$EXTERNAL_BACKUP external_backup_status=$EXTERNAL_BACKUP_STATUS external_backup_deferred_receipt=$EXTERNAL_BACKUP_DEFERRED_RECEIPT acceptance=$ACCEPTANCE_RECEIPT readiness=$READINESS_RECEIPT log=$LOG"
