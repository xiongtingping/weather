#!/bin/bash
set -euo pipefail

ROOT="/Users/xiong/weather"
PY="$ROOT/.venv/bin/python"
DB="$ROOT/data/bayes_basket_target_ssot.db"
RUNTIME="$ROOT/config/bayes_basket_target/no_live_runtime_v1.json"
ACTIVATION="$ROOT/config/trading/bayes_basket_target_production_no_live_activation_v1.json"
PAUSE="$ROOT/data/live_paused.flag"
STATUS="$ROOT/data/reported_trajectory_no_status.json"
TARGET_LABEL="com.weather.reported_trajectory_no"
MARKET_LABEL="com.weather.market_ws"
ORDER_LABEL="com.weather.clob_order_service"
TARGET_PLIST="/Library/LaunchDaemons/${TARGET_LABEL}.plist"

if [ "${1:-}" != "--confirm-target-only-production-no-live-recovery" ]; then
  echo "用法：sudo $0 --confirm-target-only-production-no-live-recovery" >&2
  exit 2
fi
if [ "$(/usr/bin/id -u)" -ne 0 ]; then
  echo "必须以root运行" >&2
  exit 1
fi
for path in "$PY" "$DB" "$RUNTIME" "$ACTIVATION" "$TARGET_PLIST"; do
  if [ ! -f "$path" ]; then
    echo "required_path_missing=$path" >&2
    exit 1
  fi
done

service_pid() {
  /bin/launchctl print "system/$1" 2>/dev/null \
    | /usr/bin/awk '/^[[:space:]]*pid = [0-9]+/{print $3; exit}'
}

market_before="$(service_pid "$MARKET_LABEL")"
order_before="$(service_pid "$ORDER_LABEL")"
target_before="$(service_pid "$TARGET_LABEL")"
if [ -z "$market_before" ] || [ -z "$order_before" ]; then
  echo "market_ws_and_clob_order_service_must_be_running" >&2
  exit 1
fi
if [ -n "$target_before" ]; then
  echo "reported_trajectory_no_already_running=$target_before" >&2
  exit 1
fi

"$PY" - "$DB" "$RUNTIME" "$ACTIVATION" "$TARGET_PLIST" <<'PY'
import json
import plistlib
import shutil
import sqlite3
import sys
from pathlib import Path

database, runtime_path, activation_path, plist_path = map(Path, sys.argv[1:])
runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
activation = json.loads(activation_path.read_text(encoding="utf-8"))
with plist_path.open("rb") as handle:
    plist = plistlib.load(handle)
environment = plist.get("EnvironmentVariables") or {}
proxy = "http://127.0.0.1:7897"
if (
    runtime.get("execution_mode") != "production_no_live"
    or float(runtime.get("target_remaining_budget_usdc", 0.0)) != 20.0
    or runtime.get("live_submit_authorized") is not False
    or runtime.get("calls_order_gateway") is not False
    or activation.get("execution_mode") != "production_no_live"
    or activation.get("live_submit_authorized") is not False
    or activation.get("calls_order_gateway") is not False
    or environment.get("PM_TARGET_EXECUTION_MODE") != "production_no_live"
    or environment.get("PM_TARGET_PRODUCTION_WRITER") != "1"
    or environment.get("PM_CLOB_ORDER_SERVICE_URL")
    != "http://127.0.0.1:8765"
    or any(
        environment.get(key) != proxy
        for key in (
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
            "http_proxy", "https_proxy", "all_proxy",
        )
    )
):
    raise SystemExit("target_recovery_no_live_boundary_invalid")
if shutil.disk_usage(database.parent).free < 8 * 1024**3:
    raise SystemExit("target_recovery_minimum_free_disk_guard")
with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
    if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise SystemExit("target_recovery_preflight_quick_check_failed")
    balance, reserved = connection.execute(
        "SELECT balance_usdc,reserved_usdc "
        "FROM basket_target_capital_pool WHERE pool_id=1"
    ).fetchone()
    if float(balance) != 20.0 or float(reserved) != 0.0:
        raise SystemExit("target_recovery_capital_epoch_invalid")
PY

/usr/bin/touch "$PAUSE"
started_at="$(/bin/date -u +%Y-%m-%dT%H:%M:%SZ)"
target_loaded=0
recovery_succeeded=0

rollback_target_only() {
  /usr/bin/touch "$PAUSE"
  if [ "$recovery_succeeded" != "1" ] && [ "$target_loaded" = "1" ]; then
    /bin/launchctl bootout "system/$TARGET_LABEL" >/dev/null 2>&1 || true
  fi
}
trap rollback_target_only EXIT INT TERM

/bin/launchctl bootstrap system "$TARGET_PLIST"
target_loaded=1

deadline=$((SECONDS + 120))
target_after=""
while [ "$SECONDS" -lt "$deadline" ]; do
  target_after="$(service_pid "$TARGET_LABEL")"
  if [ -n "$target_after" ] && "$PY" - "$STATUS" "$started_at" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])
if not path.is_file():
    raise SystemExit(1)
value = json.loads(path.read_text(encoding="utf-8"))
completed = datetime.fromisoformat(
    str(value.get("completed_at") or "").replace("Z", "+00:00")
).astimezone(timezone.utc)
started = datetime.fromisoformat(
    sys.argv[2].replace("Z", "+00:00")
).astimezone(timezone.utc)
if (
    completed < started
    or value.get("execution_mode") != "production_no_live"
    or value.get("live_submit_authorized") is not False
    or value.get("calls_order_gateway") is not False
):
    raise SystemExit(1)
PY
  then
    break
  fi
  /bin/sleep 1
done

market_after="$(service_pid "$MARKET_LABEL")"
order_after="$(service_pid "$ORDER_LABEL")"
if [ -z "$target_after" ] \
  || [ "$market_after" != "$market_before" ] \
  || [ "$order_after" != "$order_before" ] \
  || [ ! -f "$PAUSE" ]; then
  echo "target_recovery_service_postcondition_failed" >&2
  exit 1
fi

"$PY" - "$DB" <<'PY'
import sqlite3
import sys

with sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True) as connection:
    if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise SystemExit("target_recovery_postflight_quick_check_failed")
    if connection.execute(
        "SELECT COUNT(*) FROM basket_target_order_intents "
        "WHERE live_submit_authorized=1"
    ).fetchone()[0] != 0:
        raise SystemExit("target_recovery_live_intent_detected")
    if connection.execute(
        "SELECT COUNT(*) FROM basket_target_order_events"
    ).fetchone()[0] != 0:
        raise SystemExit("target_recovery_order_event_detected")
    if connection.execute(
        "SELECT COUNT(*) FROM basket_target_execution_receipts"
    ).fetchone()[0] != 0:
        raise SystemExit("target_recovery_execution_receipt_detected")
PY

recovery_succeeded=1
trap - EXIT INT TERM
echo "reported_trajectory_no_target_only_recovery_ok"
echo "reported_trajectory_no_pid=$target_after"
echo "market_ws_pid=$market_after"
echo "clob_order_service_pid=$order_after"
echo "live_pause_retained=true"
echo "orders_submitted=0"
