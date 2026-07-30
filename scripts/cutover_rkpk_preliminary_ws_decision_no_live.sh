#!/bin/bash
set -euo pipefail

ROOT="/Users/xiong/weather"
PY="$ROOT/.venv/bin/python"
DB="$ROOT/data/bayes_basket_target_ssot.db"
RUNTIME="$ROOT/config/bayes_basket_target/no_live_runtime_v1.json"
ACTIVATION="$ROOT/config/trading/bayes_basket_target_production_no_live_activation_v1.json"
MANIFEST="$ROOT/data/bayes_basket_target_official_ws_manifest.json"
LIVE_PAUSE="$ROOT/data/live_paused.flag"
TARGET_LABEL="com.weather.reported_trajectory_no"
MARKET_LABEL="com.weather.market_ws"
ORDER_LABEL="com.weather.clob_order_service"
TARGET_PLIST="/Library/LaunchDaemons/${TARGET_LABEL}.plist"
MARKET_PLIST="/Library/LaunchDaemons/${MARKET_LABEL}.plist"
RECEIPT="$ROOT/output/audits/rkpk_preliminary_ws_decision_no_live_latest.json"

if [ "${1:-}" != "--confirm-rkpk-preliminary-ws-decision-production-no-live" ]; then
  echo "用法：sudo $0 --confirm-rkpk-preliminary-ws-decision-production-no-live" >&2
  exit 2
fi
if [ "$(/usr/bin/id -u)" -ne 0 ]; then
  echo "必须以root运行" >&2
  exit 1
fi
for path in \
  "$PY" "$DB" "$RUNTIME" "$ACTIVATION" "$LIVE_PAUSE" \
  "$TARGET_PLIST" "$MARKET_PLIST"; do
  if [ ! -f "$path" ]; then
    echo "required_path_missing=$path" >&2
    exit 1
  fi
done

service_pid() {
  /bin/launchctl print "system/$1" 2>/dev/null \
    | /usr/bin/awk '/^[[:space:]]*pid = [0-9]+/{print $3; exit}'
}

target_before="$(service_pid "$TARGET_LABEL")"
market_before="$(service_pid "$MARKET_LABEL")"
order_before="$(service_pid "$ORDER_LABEL")"
if [ -z "$target_before" ] || [ -z "$market_before" ] \
  || [ -z "$order_before" ]; then
  echo "three_system_services_must_be_running" >&2
  exit 1
fi

"$PY" - "$DB" "$RUNTIME" "$ACTIVATION" "$LIVE_PAUSE" <<'PY'
import json
import shutil
import sqlite3
import sys
from pathlib import Path

database, runtime_path, activation_path, pause = map(Path, sys.argv[1:])
sys.path.insert(0, str(runtime_path.parents[2]))
from bayes_basket_target.activation import load_target_activation
from bayes_basket_target.runtime_config import load_target_no_live_runtime_config

runtime = load_target_no_live_runtime_config(runtime_path)
activation = load_target_activation(activation_path)
if (
    runtime["execution_mode"] != "production_no_live"
    or float(runtime["target_remaining_budget_usdc"]) != 20.0
    or runtime["live_submit_authorized"] is not False
    or runtime["calls_order_gateway"] is not False
    or activation["execution_mode"] != "production_no_live"
    or activation["live_submit_authorized"] is not False
    or activation["calls_order_gateway"] is not False
    or not pause.is_file()
):
    raise SystemExit("no_live_boundary_or_20u_runtime_invalid")
if shutil.disk_usage(database.parent).free < 8 * 1024**3:
    raise SystemExit("minimum_free_disk_guard")
with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as conn:
    if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise SystemExit("preflight_quick_check_failed")
PY

/usr/bin/touch "$LIVE_PAUSE"
target_stopped=0
market_stopped=0
cutover_succeeded=0

restore_on_error() {
  if [ "$cutover_succeeded" = "1" ]; then
    return
  fi
  /usr/bin/touch "$LIVE_PAUSE"
  if [ "$market_stopped" = "1" ] \
    && ! /bin/launchctl print "system/$MARKET_LABEL" >/dev/null 2>&1; then
    /bin/launchctl bootstrap system "$MARKET_PLIST" >/dev/null 2>&1 || true
  fi
  if [ "$target_stopped" = "1" ] \
    && ! /bin/launchctl print "system/$TARGET_LABEL" >/dev/null 2>&1; then
    /bin/launchctl bootstrap system "$TARGET_PLIST" >/dev/null 2>&1 || true
  fi
}
trap restore_on_error EXIT INT TERM

/bin/launchctl bootout "system/$TARGET_LABEL"
target_stopped=1
/bin/launchctl bootout "system/$MARKET_LABEL"
market_stopped=1

capital_receipt="$(
  "$PY" - "$DB" "$RUNTIME" <<'PY'
from datetime import datetime, timezone
import json
import sqlite3
import sys
from pathlib import Path

database, runtime = map(Path, sys.argv[1:])
sys.path.insert(0, str(runtime.parents[2]))
from bayes_basket_target.no_live_capital_epoch import (
    migrate_target_no_live_capital_epoch,
)

now = datetime.now(timezone.utc).isoformat(
    timespec="milliseconds"
).replace("+00:00", "Z")
with sqlite3.connect(database, timeout=60, isolation_level=None) as conn:
    conn.execute("PRAGMA busy_timeout=60000")
    applied = migrate_target_no_live_capital_epoch(
        conn,
        runtime_config_path=runtime,
        migrated_at_utc=now,
    )
    replay = migrate_target_no_live_capital_epoch(
        conn,
        runtime_config_path=runtime,
        migrated_at_utc=now,
    )
    pool = conn.execute(
        "SELECT balance_usdc,reserved_usdc,capital_epoch_id "
        "FROM basket_target_capital_pool WHERE pool_id=1"
    ).fetchone()
    if (
        float(pool[0]) != 20.0
        or float(pool[1]) != 0.0
        or replay["state"] != "UNCHANGED_ZERO_WRITE"
        or replay["rows_written"] != 0
        or conn.execute("PRAGMA quick_check").fetchone()[0] != "ok"
    ):
        raise SystemExit("capital_epoch_postcondition_failed")
print(json.dumps({
    "apply": applied,
    "replay": replay,
    "capital_epoch_id": str(pool[2]),
}, sort_keys=True))
PY
)"

/bin/launchctl bootstrap system "$MARKET_PLIST"
market_stopped=0
/bin/launchctl bootstrap system "$TARGET_PLIST"
target_stopped=0

deadline=$((SECONDS + 180))
materialized=0
while [ "$SECONDS" -lt "$deadline" ]; do
  target_after="$(service_pid "$TARGET_LABEL")"
  market_after="$(service_pid "$MARKET_LABEL")"
  if [ -n "$target_after" ] && [ -n "$market_after" ] \
    && [ "$target_after" != "$target_before" ] \
    && [ "$market_after" != "$market_before" ]; then
    if "$PY" - "$DB" "$MANIFEST" <<'PY'
import json
import sqlite3
import sys
from pathlib import Path

database, manifest_path = map(Path, sys.argv[1:])
if not manifest_path.is_file():
    raise SystemExit(1)
manifest = json.loads(manifest_path.read_text())
if (
    manifest.get("schema")
    != "bayes_basket_target_resident_multi_scope_ws_manifest_v1"
    or manifest.get("state") != "connected"
    or manifest.get("authorizes_order") is not False
    or manifest.get("calls_order_gateway") is not False
    or manifest.get("live_submit_authorized") is not False
):
    raise SystemExit(1)
scope_keys = set((manifest.get("scope_manifests") or {}).keys())
if "RKPK|2026-07-30" not in scope_keys:
    raise SystemExit(1)
with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as conn:
    preselection = conn.execute(
        "SELECT COUNT(*) FROM basket_p0_preselection_plan "
        "WHERE station_id='RKPK' AND event_date='2026-07-30' "
        "AND state='PRELIMINARY_NO_LIVE'"
    ).fetchone()[0]
    decision = conn.execute(
        "SELECT COUNT(*) FROM auto_trade_decisions "
        "WHERE station_id='RKPK' AND event_date='2026-07-30' "
        "AND decided_state='blocked' AND will_order=0 "
        "AND reason_codes_json LIKE '%preliminary_no_live_p1_pending%'"
    ).fetchone()[0]
    intents = conn.execute(
        "SELECT COUNT(*) FROM basket_target_order_intents i "
        "JOIN auto_trade_decisions d ON d.decision_id=i.decision_id "
        "WHERE d.station_id='RKPK' AND d.event_date='2026-07-30'"
    ).fetchone()[0]
    if preselection < 1 or decision < 1 or intents != 0:
        raise SystemExit(1)
PY
    then
      materialized=1
      break
    fi
  fi
  /bin/sleep 2
done

target_after="$(service_pid "$TARGET_LABEL")"
market_after="$(service_pid "$MARKET_LABEL")"
order_after="$(service_pid "$ORDER_LABEL")"
if [ -z "$target_after" ] || [ -z "$market_after" ] \
  || [ "$target_after" = "$target_before" ] \
  || [ "$market_after" = "$market_before" ] \
  || [ "$order_after" != "$order_before" ] \
  || [ "$materialized" != "1" ]; then
  echo "service_pid_postcondition_failed" >&2
  exit 1
fi

/bin/mkdir -p "$ROOT/output/audits"
"$PY" - \
  "$DB" "$MANIFEST" "$RECEIPT" "$capital_receipt" \
  "$target_before" "$target_after" "$market_before" "$market_after" \
  "$order_before" "$order_after" <<'PY'
from datetime import datetime, timezone
import json
import sqlite3
import sys
from pathlib import Path

(
    database, manifest_path, receipt_path, capital_json,
    target_before, target_after, market_before, market_after,
    order_before, order_after,
) = sys.argv[1:]
manifest = json.loads(Path(manifest_path).read_text())
with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as conn:
    pool = conn.execute(
        "SELECT balance_usdc,reserved_usdc,capital_epoch_id "
        "FROM basket_target_capital_pool WHERE pool_id=1"
    ).fetchone()
    preselection = conn.execute(
        "SELECT COUNT(*),MAX(created_at) FROM basket_p0_preselection_plan "
        "WHERE station_id='RKPK' AND event_date='2026-07-30'"
    ).fetchone()
    decision = conn.execute(
        "SELECT COUNT(*),MAX(created_at) FROM auto_trade_decisions "
        "WHERE station_id='RKPK' AND event_date='2026-07-30' "
        "AND reason_codes_json LIKE '%preliminary_no_live_p1_pending%'"
    ).fetchone()
    quick_check = conn.execute("PRAGMA quick_check").fetchone()[0]
receipt = {
    "schema": "rkpk_preliminary_ws_decision_no_live_cutover_v1",
    "created_at_utc": datetime.now(timezone.utc).isoformat().replace(
        "+00:00", "Z"
    ),
    "state": "MATERIALIZED",
    "execution_mode": "production_no_live",
    "capital": json.loads(capital_json),
    "pool": {
        "balance_usdc": pool[0],
        "reserved_usdc": pool[1],
        "capital_epoch_id": pool[2],
    },
    "ws_scope_keys": sorted(
        (manifest.get("scope_manifests") or {}).keys()
    ),
    "preselection_count": preselection[0],
    "preselection_latest": preselection[1],
    "preliminary_decision_count": decision[0],
    "preliminary_decision_latest": decision[1],
    "service_pids": {
        "reported_trajectory_no": [int(target_before), int(target_after)],
        "market_ws": [int(market_before), int(market_after)],
        "clob_order_service": [int(order_before), int(order_after)],
    },
    "quick_check": quick_check,
    "calls_order_gateway": False,
    "live_submit_authorized": False,
    "orders_submitted": 0,
}
Path(receipt_path).write_text(
    json.dumps(receipt, ensure_ascii=False, sort_keys=True) + "\n",
    encoding="utf-8",
)
print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
PY

cutover_succeeded=1
trap - EXIT INT TERM
echo "rkpk_preliminary_ws_decision_no_live_ok"
