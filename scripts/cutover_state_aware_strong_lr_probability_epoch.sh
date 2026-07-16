#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

mkdir -p "$ROOT/data/logs"
CUTOVER_LOG="${STATE_AWARE_STRONG_LR_CUTOVER_LOG:-$ROOT/data/logs/state_aware_strong_lr_probability_epoch_cutover.log}"
exec > >(tee -a "$CUTOVER_LOG") 2>&1

LAST_STEP="initialize"
CUTOVER_COMPLETE=0
ACTIVATION_WRITTEN=0
ACTIVATION_ROLLBACK="$(mktemp)"
cp config/production_probability_runtime_activation.json "$ACTIVATION_ROLLBACK"
SKIP_PROBABILITY_EPOCH=0
BACKUP_PARTIAL=""
LOCAL_BACKUP_PARTIAL=""
LOCAL_BACKUP=""
EXTERNAL_BACKUP=""
LOCAL_BACKUP_PROBE=""
SUDO_KEEPALIVE_PID=""

cutover_step() {
  LAST_STEP="$1"
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] cutover_step=$LAST_STEP"
}

start_sudo_keepalive() {
  local cutover_pid="$$"
  (
    while kill -0 "$cutover_pid" 2>/dev/null; do
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

verify_writer_quiescence() {
  local attempt=""
  for attempt in 1 2 3; do
    if python3 scripts/controlled_apply_maintenance_window_packet.py \
      --db data/archive.db --summary --max-items-output 20; then
      return 0
    fi
    [ "$attempt" = "3" ] || sleep 0.25
  done
  return 1
}

wait_for_archive_wal_keeper() {
  local attempt=""
  local max_attempts="${PM_ARCHIVE_WAL_KEEPER_ACCEPTANCE_ATTEMPTS:-30}"
  local python_bin=""
  python_bin="$(command -v python3)"
  for attempt in $(seq 1 "$max_attempts"); do
    # The system-domain monitor and keeper run as root.  Run this read-only
    # process/FD inspection in the same privilege domain so EPERM is never
    # misclassified as a dead PID or a missing archive handle.
    if sudo -n "$python_bin" scripts/archive_db_wal_keeper_audit.py \
      --db data/archive.db \
      --status data/archive_wal_keeper_status_latest.json; then
      return 0
    fi
    [ "$attempt" = "$max_attempts" ] || sleep 1
  done
  echo "archive WAL keeper did not become ready after ${max_attempts}s" >&2
  return 1
}

stop_monitor_process_tree() {
  local pattern=""
  local pids=""
  local -a patterns=(
    "$ROOT/scripts/polymarket_monitor_daemon.sh"
    "$ROOT/scripts/hard_source_direct_poller.py --loop"
    "$ROOT/scripts/hard_source_direct_poller.sh"
    "$ROOT/scripts/amsc_zuuu_metar_taf_poll_once.sh"
    "$ROOT/scripts/watch_zuuu_speci_once.py"
    "$ROOT/scripts/run_amsc_p10_shadow_source_sidecar.py"
  )
  for pattern in "${patterns[@]}"; do
    pids="$(pgrep -f "$pattern" 2>/dev/null || true)"
    [ -z "$pids" ] || sudo kill -TERM $pids 2>/dev/null || true
  done
  for _ in $(seq 1 40); do
    pids=""
    for pattern in "${patterns[@]}"; do
      pids="$pids $(pgrep -f "$pattern" 2>/dev/null || true)"
    done
    pids="$(printf '%s' "$pids" | xargs 2>/dev/null || true)"
    [ -z "$pids" ] && return 0
    sleep 0.25
  done
  for pattern in "${patterns[@]}"; do
    pids="$(pgrep -f "$pattern" 2>/dev/null || true)"
    [ -z "$pids" ] || sudo kill -KILL $pids 2>/dev/null || true
  done
  sleep 0.25
  for pattern in "${patterns[@]}"; do
    if pgrep -f "$pattern" >/dev/null 2>&1; then
      echo "monitor child retained after bounded shutdown: $pattern" >&2
      return 1
    fi
  done
}

current_activation_has_p0_lineage() {
  python3 - "$ROOT/data/archive.db" <<'PY'
import sqlite3
import sys

from production_probability_activation_contract import (
    ProductionProbabilityActivationError,
    load_production_probability_activation,
)

try:
    activation = load_production_probability_activation()
    with sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True) as conn:
        count = int(conn.execute(
            """SELECT COUNT(*) FROM production_initial_posterior_lineage
               WHERE activation_contract_sha256=?""",
            (activation["activation_contract_sha256"],),
        ).fetchone()[0] or 0)
except (OSError, sqlite3.Error, ProductionProbabilityActivationError, KeyError):
    raise SystemExit(1)
raise SystemExit(0 if count > 0 else 1)
PY
}

state_aware_probability_epoch_active() {
  python3 - "$ROOT/data/archive.db" <<'PY'
import sqlite3
import sys

from production_probability_activation_contract import load_production_probability_activation
from production_probability_runtime_manifest import current_runtime_manifest_sha256
from strong_observation_lr_operational_state_baseline import build_operational_state_aware_strong_lr_baseline

try:
    activation = load_production_probability_activation()
    expected = build_operational_state_aware_strong_lr_baseline()["artifact_sha256"]
    if activation.get("strong_lr_artifact_sha256") != expected:
        raise SystemExit(1)
    if activation.get("runtime_manifest_sha256") != current_runtime_manifest_sha256():
        raise SystemExit(1)
    with sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True) as conn:
        count = int(conn.execute(
            """SELECT COUNT(*) FROM production_initial_posterior_lineage
               WHERE activation_contract_sha256=?""",
            (activation["activation_contract_sha256"],),
        ).fetchone()[0] or 0)
except (OSError, sqlite3.Error, ValueError, KeyError):
    raise SystemExit(1)
raise SystemExit(0 if count == 1 else 1)
PY
}

assert_trading_activations_disabled() {
  python3 - \
    "$ROOT/config/trading/dynamic_three_leg_production_activation.json" \
    "$ROOT/config/trading/market_alpha_take_profit_production_activation.json" <<'PY'
import json
import sys
from pathlib import Path

dynamic = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
alpha = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
if dynamic.get("state") != "disabled" or alpha.get("state") != "disabled":
    raise SystemExit("trading_activations_must_remain_disabled")
for key in ("risk_reservation_write_authorized", "live_decision_write_authorized", "live_submit_authorized"):
    if dynamic.get(key) is not False:
        raise SystemExit(f"dynamic_activation_flag_must_be_false:{key}")
for key in ("live_decision_write_authorized", "live_submit_authorized"):
    if alpha.get(key) is not False:
        raise SystemExit(f"market_alpha_activation_flag_must_be_false:{key}")
PY
}

restore_services() {
  sudo install -o root -g wheel -m 644 \
    launchd/com.weather.monitor.daemon.plist \
    /Library/LaunchDaemons/com.weather.monitor.daemon.plist || true
  sudo launchctl enable system/com.weather.monitor.daemon || true
  sudo launchctl bootstrap system /Library/LaunchDaemons/com.weather.monitor.daemon.plist 2>/dev/null || true
  sudo launchctl kickstart -k system/com.weather.monitor.daemon || true
  scripts/weatherctl.sh start || true
}

recover() {
  rc=$?
  if [ "$CUTOVER_COMPLETE" != "1" ]; then
    if [ "$ACTIVATION_WRITTEN" = "1" ] && current_activation_has_p0_lineage; then
      echo "cutover incomplete: step=$LAST_STEP rc=$rc; committed append-only probability epoch preserved" >&2
    elif [ "$ACTIVATION_WRITTEN" = "1" ]; then
      cp "$ACTIVATION_ROLLBACK" config/production_probability_runtime_activation.json
      echo "cutover failed before successor P0 commit: step=$LAST_STEP rc=$rc; prior activation restored" >&2
    else
      echo "cutover failed before activation write: step=$LAST_STEP rc=$rc; prior activation unchanged" >&2
    fi
    echo "cutover log: $CUTOVER_LOG" >&2
  fi
  restore_services
  stop_sudo_keepalive
  if [ -n "${BACKUP_PARTIAL:-}" ] && [ -f "$BACKUP_PARTIAL" ]; then
    mv "$BACKUP_PARTIAL" "${BACKUP_PARTIAL}.incomplete" 2>/dev/null || true
  fi
  if [ -n "${LOCAL_BACKUP_PARTIAL:-}" ] && [ -f "$LOCAL_BACKUP_PARTIAL" ]; then
    mv "$LOCAL_BACKUP_PARTIAL" "${LOCAL_BACKUP_PARTIAL}.incomplete" 2>/dev/null || true
  fi
  if [ -n "${LOCAL_BACKUP_PROBE:-}" ] && [ -f "$LOCAL_BACKUP_PROBE" ]; then
    rm -f "$LOCAL_BACKUP_PROBE" 2>/dev/null || true
  fi
  rm -f "$ACTIVATION_ROLLBACK"
  exit "$rc"
}
trap recover EXIT

cutover_step "verify_operator_and_external_backup_volume"
assert_trading_activations_disabled
EVENT_DATE="$(TZ=Asia/Shanghai date +%F)"
DECISION_AS_OF="$(date -u +%Y-%m-%dT%H:%M:%S.000Z)"
if state_aware_probability_epoch_active; then
  SKIP_PROBABILITY_EPOCH=1
fi
if [ "$SKIP_PROBABILITY_EPOCH" != "1" ]; then
  cutover_step "read_only_full_canonical_state_replay"
  if python3 - <<'PY' >/dev/null 2>&1
from production_probability_activation_contract import load_production_probability_activation
load_production_probability_activation()
PY
  then
    python3 scripts/preview_state_aware_strong_lr_replay.py \
      --db data/archive.db \
      --event-date "$EVENT_DATE"
  else
    # An intentional Bayes-formula/runtime-manifest successor makes the old
    # activation invalid before the maintenance window.  Source/canonical
    # evidence remains durable; the new activation is compiled below and its
    # P0 bootstrap performs the authoritative full canonical replay before
    # services resume.
    echo "current activation is the retired formula; successor P0 replay will be verified inside the maintenance window"
  fi
fi
cutover_step "verify_probability_runtime_manifest_dependency_closure"
python3 - <<'PY'
import json
from production_probability_runtime_manifest import audit_runtime_manifest_dependency_closure

result = audit_runtime_manifest_dependency_closure()
print(json.dumps(result, ensure_ascii=False, sort_keys=True))
raise SystemExit(0 if result.get("ok") is True else 2)
PY
if [ "$SKIP_PROBABILITY_EPOCH" = "1" ] && \
   python3 scripts/activate_dynamic_position_market_alpha_schema.py --db data/archive.db >/dev/null 2>&1; then
  cutover_step "already_active_read_only_acceptance"
  if ! sudo -n true 2>/dev/null; then
    echo "sudo credential is not cached; run 'sudo -v' immediately before read-only keeper FD acceptance" >&2
    exit 2
  fi
  wait_for_archive_wal_keeper
  python3 scripts/production_probability_transaction_b_cutover_readiness.py --db data/archive.db --summary
  python3 scripts/production_sequential_bayes_chain_audit.py \
    --db data/archive.db --event-date "$EVENT_DATE" --summary
  python3 scripts/production_sequential_bayes_cutover_acceptance.py \
    --db data/archive.db --event-date "$EVENT_DATE"
  python3 scripts/audit_strong_state_discrimination.py --db data/archive.db
  python3 scripts/production_dynamic_three_leg_readiness.py --db data/archive.db --summary
  python3 scripts/production_trade_gate_zero_audit.py
  python3 scripts/check_ssot_alignment.py
  assert_trading_activations_disabled
  CUTOVER_COMPLETE=1
  trap - EXIT
  rm -f "$ACTIVATION_ROLLBACK"
  echo "state-aware probability epoch and default-off position/market-alpha schema already active; no writes performed"
  exit 0
fi
if ! sudo -n true 2>/dev/null; then
  echo "sudo credential is not cached; run 'sudo -v' immediately before this script" >&2
  exit 2
fi
start_sudo_keepalive
BACKUP_DIR="${STATE_AWARE_STRONG_LR_BACKUP_DIR:-/Volumes/Expansion/熊/backups}"
if [ ! -d "$BACKUP_DIR" ] || [ ! -w "$BACKUP_DIR" ]; then
  echo "external backup directory is unavailable or not writable: $BACKUP_DIR" >&2
  exit 2
fi
LOCAL_BACKUP_DIR="${STATE_AWARE_STRONG_LR_LOCAL_BACKUP_DIR:-$ROOT/data/local_cutover_backups}"
mkdir -p "$LOCAL_BACKUP_DIR"
if [ -L "$LOCAL_BACKUP_DIR" ]; then
  echo "local APFS backup directory must not be a symlink: $LOCAL_BACKUP_DIR" >&2
  exit 4
fi
if [ "$(stat -f %d data/archive.db)" != "$(stat -f %d "$LOCAL_BACKUP_DIR/.")" ]; then
  echo "local APFS backup directory is not on archive.db filesystem: $LOCAL_BACKUP_DIR" >&2
  exit 4
fi
cutover_step "preflight_local_apfs_clone_capability"
LOCAL_BACKUP_PROBE="$LOCAL_BACKUP_DIR/.clone-capability-probe.$$"
cp -c config/production_probability_runtime_activation.json "$LOCAL_BACKUP_PROBE"
cmp config/production_probability_runtime_activation.json "$LOCAL_BACKUP_PROBE"
rm -f "$LOCAL_BACKUP_PROBE"
LOCAL_BACKUP_PROBE=""

PREFLIGHT_BACKUP="$(ls -t "$BACKUP_DIR"/archive.pre-strong-lr-state-v2.*.db 2>/dev/null | head -n 1 || true)"
if [ -n "$PREFLIGHT_BACKUP" ]; then
  cutover_step "preflight_transaction_b_on_latest_verified_backup"
  python3 scripts/production_transaction_b_no_order_drill.py \
    --backup "$PREFLIGHT_BACKUP"
fi

cutover_step "pause_writer_services"
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
stop_monitor_process_tree

WORKER_PROCESS_PATTERN="$ROOT/scan_weather_market.py.*--auto-trade-job-worker"
WORKER_PIDS="$(pgrep -f "$WORKER_PROCESS_PATTERN" 2>/dev/null || true)"
if [ -n "$WORKER_PIDS" ]; then
  kill -TERM $WORKER_PIDS 2>/dev/null || true
fi
for _ in $(seq 1 40); do
  if ! pgrep -f "$WORKER_PROCESS_PATTERN" >/dev/null 2>&1; then
    break
  fi
  sleep 0.25
done
if pgrep -f "$WORKER_PROCESS_PATTERN" >/dev/null 2>&1; then
  echo "target-realization worker retained archive.db after the bounded shutdown window" >&2
  exit 3
fi

cutover_step "verify_writer_quiescence"
verify_writer_quiescence

cutover_step "verify_archive_writer_queue_claim"
python3 - <<'PY'
from archive_db_write_queue import archive_db_write_gate

with archive_db_write_gate(
    "data/archive.db",
    owner="state_aware_probability_cutover_preflight",
    wait_sec=3.0,
):
    pass
PY

cutover_step "create_exact_local_apfs_clone"
BACKUP_STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOCAL_BACKUP="$LOCAL_BACKUP_DIR/archive.pre-strong-lr-state-v2.${BACKUP_STAMP}.db"
LOCAL_BACKUP_PARTIAL="${LOCAL_BACKUP}.partial.$$"
# archive.db is already writer-quiescent.  Checkpoint WAL first, then take an
# APFS clone.  This is a stable pre-cutover image without putting the 13 GiB
# ExFAT transfer inside the AMSC observation outage.
checkpoint_result="$(sqlite3 data/archive.db 'PRAGMA wal_checkpoint(TRUNCATE);')"
if [ "$checkpoint_result" != "0|0|0" ]; then
  echo "archive WAL checkpoint did not close cleanly: $checkpoint_result" >&2
  exit 4
fi
cp -c data/archive.db "$LOCAL_BACKUP_PARTIAL"
test "$(stat -f %z data/archive.db)" = "$(stat -f %z "$LOCAL_BACKUP_PARTIAL")"
sqlite3 "$LOCAL_BACKUP_PARTIAL" 'PRAGMA schema_version; PRAGMA page_count; PRAGMA page_size;' >/dev/null
mv "$LOCAL_BACKUP_PARTIAL" "$LOCAL_BACKUP"
LOCAL_BACKUP_PARTIAL=""
BACKUP="$LOCAL_BACKUP"
test -s "$BACKUP"
echo "exact_local_backup=$BACKUP"

# This additive schema is default-off and must bind to the exact database
# image represented by BACKUP.  Applying it before P0 avoids comparing a
# pre-P0 backup with a database whose append-only probability epoch has
# already advanced.
cutover_step "apply_default_off_position_continuation_market_alpha_schema"
python3 scripts/activate_dynamic_position_market_alpha_schema.py \
  --db data/archive.db \
  --backup "$BACKUP" \
  --apply --writer-quiescent \
  --confirm ACTIVATE_DYNAMIC_POSITION_MARKET_ALPHA_SCHEMA
python3 scripts/activate_dynamic_position_market_alpha_schema.py --db data/archive.db
cutover_step "apply_weak_forecast_revision_schema"
python3 scripts/activate_weak_forecast_revision_schema.py \
  --db data/archive.db --apply --writer-quiescent \
  --confirm ACTIVATE_WEAK_FORECAST_REVISION_SCHEMA
python3 scripts/activate_weak_forecast_revision_schema.py --db data/archive.db
python3 scripts/production_trade_gate_zero_audit.py
assert_trading_activations_disabled

cutover_step "install_current_monitor_deployment"
sudo install -o root -g wheel -m 644 \
  launchd/com.weather.monitor.daemon.plist \
  /Library/LaunchDaemons/com.weather.monitor.daemon.plist

if [ "$SKIP_PROBABILITY_EPOCH" != "1" ]; then
  cutover_step "materialize_content_addressed_strong_lr_v2"
  python3 scripts/materialize_operational_state_strong_lr_baseline.py \
    --apply --confirm MATERIALIZE_OPERATIONAL_STATE_STRONG_LR_BASELINE_V2

  cutover_step "materialize_successor_probability_activation"
  python3 scripts/materialize_production_probability_activation.py \
    --db data/archive.db \
    --backup "$BACKUP" \
    --event-date "$EVENT_DATE" \
    --decision-as-of "$DECISION_AS_OF" \
    --apply --writer-quiescent \
    --confirm ACTIVATE_SEQUENTIAL_BAYES_PROBABILITY_MAINLINE
  ACTIVATION_WRITTEN=1

  cutover_step "commit_successor_p0_and_replay_canonical_events"
  python3 scripts/bootstrap_production_probability_p0.py \
    --db data/archive.db \
    --event-date "$EVENT_DATE" \
    --decision-as-of "$DECISION_AS_OF" \
    --apply \
    --confirm BOOTSTRAP_PRODUCTION_PROBABILITY_P0
fi

cutover_step "verify_orderless_probability_epoch"
python3 scripts/production_probability_transaction_b_cutover_readiness.py \
  --db data/archive.db --summary
python3 scripts/production_sequential_bayes_chain_audit.py \
  --db data/archive.db --event-date "$EVENT_DATE" --summary

python3 scripts/production_dynamic_three_leg_readiness.py --db data/archive.db --summary
assert_trading_activations_disabled

cutover_step "resume_services"
sudo launchctl enable system/com.weather.monitor.daemon
sudo launchctl bootstrap system /Library/LaunchDaemons/com.weather.monitor.daemon.plist 2>/dev/null || true
sudo launchctl kickstart -k system/com.weather.monitor.daemon
scripts/weatherctl.sh start

cutover_step "accept_ssot_and_state_discrimination"
wait_for_archive_wal_keeper
python3 scripts/production_sequential_bayes_cutover_acceptance.py \
  --db data/archive.db --event-date "$EVENT_DATE"
python3 scripts/audit_strong_state_discrimination.py --db data/archive.db
python3 scripts/check_ssot_alignment.py

# The probability epoch is committed and accepted at this boundary.  The
# external copy below is deliberately post-recovery durability work; a volume
# disappearing after preflight must not relabel an accepted append-only epoch
# as an incomplete probability cutover.
CUTOVER_COMPLETE=1

cutover_step "copy_frozen_pre_cutover_image_to_external_backup"
EXTERNAL_BACKUP="$BACKUP_DIR/archive.pre-strong-lr-state-v2.${BACKUP_STAMP}.db"
BACKUP_PARTIAL="${EXTERNAL_BACKUP}.partial.$$"
# Services are already live again.  Copy the immutable APFS clone, never the
# now-changing archive.db, so the slow external transfer cannot corrupt backup
# consistency or consume the observation downtime budget.  If the removable
# volume disappeared after preflight, persist an exact retry receipt instead
# of failing the already accepted probability epoch.
EXTERNAL_BACKUP_STATE="deferred"
EXTERNAL_BACKUP_DEFERRED_RECEIPT="$LOCAL_BACKUP_DIR/external_backup_deferred.${BACKUP_STAMP}.json"
if [ -d "$BACKUP_DIR" ] && [ -w "$BACKUP_DIR" ]; then
  set +e
  COPYFILE_DISABLE=1 cp "$LOCAL_BACKUP" "$BACKUP_PARTIAL"
  external_copy_rc=$?
  if [ "$external_copy_rc" = "0" ]; then
    test "$(stat -f %z "$LOCAL_BACKUP")" = "$(stat -f %z "$BACKUP_PARTIAL")"
    external_copy_rc=$?
  fi
  if [ "$external_copy_rc" = "0" ]; then
    sqlite3 "$BACKUP_PARTIAL" 'PRAGMA schema_version; PRAGMA page_count; PRAGMA page_size;' >/dev/null
    external_copy_rc=$?
  fi
  if [ "$external_copy_rc" = "0" ]; then
    mv "$BACKUP_PARTIAL" "$EXTERNAL_BACKUP"
    external_copy_rc=$?
  fi
  set -e
  if [ "$external_copy_rc" = "0" ]; then
    BACKUP_PARTIAL=""
    EXTERNAL_BACKUP_STATE="copied_and_verified"
    echo "external_backup=$EXTERNAL_BACKUP"
  fi
else
  external_copy_rc=2
fi
if [ "$EXTERNAL_BACKUP_STATE" != "copied_and_verified" ]; then
  [ ! -f "$BACKUP_PARTIAL" ] || mv "$BACKUP_PARTIAL" "${BACKUP_PARTIAL}.incomplete"
  BACKUP_PARTIAL=""
  python3 - "$EXTERNAL_BACKUP_DEFERRED_RECEIPT" "$LOCAL_BACKUP" "$EXTERNAL_BACKUP" "$external_copy_rc" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

receipt_path = Path(sys.argv[1])
local_backup = Path(sys.argv[2])
payload = {
    "schema": "probability_cutover_external_backup_deferred_v1",
    "state": "deferred_external_volume_unavailable_or_copy_failed",
    "recorded_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    "local_frozen_backup": str(local_backup),
    "local_frozen_backup_size": local_backup.stat().st_size,
    "intended_external_backup": sys.argv[3],
    "copy_returncode": int(sys.argv[4]),
    "probability_epoch_already_committed_and_accepted": True,
    "writes_archive_db": False,
    "places_orders": False,
}
temporary = receipt_path.with_name(f".{receipt_path.name}.tmp.{os.getpid()}")
temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
os.replace(temporary, receipt_path)
PY
  echo "external_backup_deferred_receipt=$EXTERNAL_BACKUP_DEFERRED_RECEIPT" >&2
fi

cutover_step "complete"
stop_sudo_keepalive
trap - EXIT
rm -f "$ACTIVATION_ROLLBACK"
echo "state-aware strong-LR v2 probability epoch and default-off position/market-alpha schema cutover completed; local_backup=$LOCAL_BACKUP external_backup_state=$EXTERNAL_BACKUP_STATE external_backup=$EXTERNAL_BACKUP"
