#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

DB="$ROOT/data/archive.db"
BACKUP_LINK_DIR="$ROOT/data/backups"
LOG="$ROOT/data/logs/dynamic_position_market_alpha_schema_cutover.log"
mkdir -p "$ROOT/data/logs"
exec > >(tee -a "$LOG") 2>&1

LAST_STEP="initialize"
CUTOVER_COMPLETE=0
SERVICES_PAUSED=0

step() {
  LAST_STEP="$1"
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] cutover_step=$LAST_STEP"
}

file_sha256() {
  shasum -a 256 "$1" | awk '{print $1}'
}

assert_disabled_activation_contracts() {
  python3 - \
    "$ROOT/config/trading/dynamic_three_leg_production_activation.json" \
    "$ROOT/config/trading/market_alpha_take_profit_production_activation.json" <<'PY'
import json
import sys
from pathlib import Path

dynamic = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
alpha = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
if dynamic.get("state") != "disabled":
    raise SystemExit("dynamic_three_leg_activation_must_remain_disabled")
for key in (
    "risk_reservation_write_authorized",
    "live_decision_write_authorized",
    "live_submit_authorized",
):
    if dynamic.get(key) is not False:
        raise SystemExit(f"dynamic_three_leg_activation_flag_must_be_false:{key}")
if alpha.get("state") != "disabled":
    raise SystemExit("market_alpha_activation_must_remain_disabled")
for key in ("live_decision_write_authorized", "live_submit_authorized"):
    if alpha.get(key) is not False:
        raise SystemExit(f"market_alpha_activation_flag_must_be_false:{key}")
PY
}

restore_services() {
  if [ "$SERVICES_PAUSED" != "1" ]; then
    return 0
  fi
  sudo launchctl enable system/com.weather.monitor.daemon || true
  sudo launchctl bootstrap system /Library/LaunchDaemons/com.weather.monitor.daemon.plist 2>/dev/null || true
  sudo launchctl kickstart -k system/com.weather.monitor.daemon || true
  scripts/weatherctl.sh start || true
  SERVICES_PAUSED=0
}

recover() {
  rc=$?
  if [ "$CUTOVER_COMPLETE" != "1" ]; then
    echo "dynamic position/market-alpha schema cutover failed: step=$LAST_STEP rc=$rc" >&2
    echo "cutover log: $LOG" >&2
  fi
  restore_services
  exit "$rc"
}
trap recover EXIT

step "preflight"
assert_disabled_activation_contracts
PROBABILITY_ACTIVATION_SHA="$(file_sha256 "$ROOT/config/production_probability_runtime_activation.json")"
DYNAMIC_ACTIVATION_SHA="$(file_sha256 "$ROOT/config/trading/dynamic_three_leg_production_activation.json")"
MARKET_ALPHA_ACTIVATION_SHA="$(file_sha256 "$ROOT/config/trading/market_alpha_take_profit_production_activation.json")"
set +e
python3 scripts/activate_dynamic_position_market_alpha_schema.py --db "$DB"
INSPECT_RC=$?
set -e
if [ "$INSPECT_RC" = "0" ]; then
  step "already_applied_read_only_acceptance"
  python3 scripts/production_trade_gate_zero_audit.py
  python3 scripts/production_probability_transaction_b_cutover_readiness.py \
    --db "$DB" --summary
  python3 scripts/production_dynamic_three_leg_readiness.py \
    --db "$DB" --summary
  assert_disabled_activation_contracts
  test "$(file_sha256 "$ROOT/config/production_probability_runtime_activation.json")" = "$PROBABILITY_ACTIVATION_SHA"
  test "$(file_sha256 "$ROOT/config/trading/dynamic_three_leg_production_activation.json")" = "$DYNAMIC_ACTIVATION_SHA"
  test "$(file_sha256 "$ROOT/config/trading/market_alpha_take_profit_production_activation.json")" = "$MARKET_ALPHA_ACTIVATION_SHA"
  CUTOVER_COMPLETE=1
  trap - EXIT
  echo "dynamic position/continuation/market-alpha schema already ready; read-only acceptance passed"
  exit 0
fi
if [ "$INSPECT_RC" != "2" ]; then
  echo "schema preflight failed unexpectedly: rc=$INSPECT_RC" >&2
  exit "$INSPECT_RC"
fi

step "pause_writer_services"
SERVICES_PAUSED=1
scripts/weatherctl.sh stop
launchctl bootout "gui/$(id -u)/com.weather.dynamic_three_leg_execution" 2>/dev/null || \
  launchctl bootout "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.weather.dynamic_three_leg_execution.plist" 2>/dev/null || true
sudo launchctl disable system/com.weather.monitor.daemon || true
sudo launchctl bootout system /Library/LaunchDaemons/com.weather.monitor.daemon.plist 2>/dev/null || true

step "verify_writer_quiescence_before_backup"
python3 scripts/controlled_apply_maintenance_window_packet.py \
  --db "$DB" --summary --max-items-output 20

step "create_external_backup"
BACKUP_REAL_DIR="$(python3 - "$BACKUP_LINK_DIR" <<'PY'
import sys
from pathlib import Path
print(Path(sys.argv[1]).expanduser().resolve())
PY
)"
case "$BACKUP_REAL_DIR" in
  /Volumes/*) ;;
  *)
    echo "external backup directory required; resolved path is $BACKUP_REAL_DIR" >&2
    exit 4
    ;;
esac
if [ ! -d "$BACKUP_REAL_DIR" ] || [ ! -w "$BACKUP_REAL_DIR" ]; then
  echo "external backup directory unavailable or not writable: $BACKUP_REAL_DIR" >&2
  exit 4
fi
BACKUP="$BACKUP_REAL_DIR/archive.pre-position-alpha.$(date -u +%Y%m%dT%H%M%SZ).db"
sqlite3 "$DB" ".backup '$BACKUP'"

step "verify_writer_quiescence_before_apply"
python3 scripts/controlled_apply_maintenance_window_packet.py \
  --db "$DB" --summary --max-items-output 20

step "apply_additive_schema"
python3 scripts/activate_dynamic_position_market_alpha_schema.py \
  --db "$DB" \
  --backup "$BACKUP" \
  --apply \
  --writer-quiescent \
  --confirm ACTIVATE_DYNAMIC_POSITION_MARKET_ALPHA_SCHEMA

step "post_apply_acceptance"
python3 scripts/activate_dynamic_position_market_alpha_schema.py --db "$DB"
python3 scripts/production_trade_gate_zero_audit.py
python3 scripts/production_probability_transaction_b_cutover_readiness.py \
  --db "$DB" --summary
python3 scripts/production_dynamic_three_leg_readiness.py \
  --db "$DB" --summary
assert_disabled_activation_contracts
test "$(file_sha256 "$ROOT/config/production_probability_runtime_activation.json")" = "$PROBABILITY_ACTIVATION_SHA"
test "$(file_sha256 "$ROOT/config/trading/dynamic_three_leg_production_activation.json")" = "$DYNAMIC_ACTIVATION_SHA"
test "$(file_sha256 "$ROOT/config/trading/market_alpha_take_profit_production_activation.json")" = "$MARKET_ALPHA_ACTIVATION_SHA"

step "restore_services"
restore_services

step "complete"
CUTOVER_COMPLETE=1
trap - EXIT
echo "dynamic position/continuation/market-alpha additive schema cutover completed; all trading activations remain disabled; backup=$BACKUP"
