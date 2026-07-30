#!/bin/bash
set -euo pipefail

ROOT="/Users/xiong/weather"
DB="$ROOT/data/bayes_basket_target_ssot.db"
PY="$ROOT/.venv/bin/python"
TARGET_LABEL="com.weather.reported_trajectory_no"
MARKET_LABEL="com.weather.market_ws"
ORDER_LABEL="com.weather.clob_order_service"
TARGET_SERVICE="system/$TARGET_LABEL"
TARGET_PLIST="/Library/LaunchDaemons/${TARGET_LABEL}.plist"
LIVE_PAUSE="$ROOT/data/live_paused.flag"
ACTIVATION="$ROOT/config/trading/bayes_basket_target_production_no_live_activation_v1.json"
CONFIRM="${1:-}"

if [ "$CONFIRM" != "--confirm-probability-evidence-production-no-live" ]; then
  echo "用法：sudo $0 --confirm-probability-evidence-production-no-live" >&2
  exit 2
fi
if [ "$(/usr/bin/id -u)" -ne 0 ]; then
  echo "必须以root运行：sudo $0 --confirm-probability-evidence-production-no-live" >&2
  exit 1
fi
for required in "$DB" "$PY" "$TARGET_PLIST" "$LIVE_PAUSE" "$ACTIVATION"; do
  if [ ! -f "$required" ]; then
    echo "probability_evidence_required_path_missing=$required" >&2
    exit 1
  fi
done

service_pid() {
  /bin/launchctl print "system/$1" 2>/dev/null \
    | /usr/bin/awk '/^[[:space:]]*pid = [0-9]+/{print $3; exit}'
}

target_pid_before="$(service_pid "$TARGET_LABEL")"
market_pid_before="$(service_pid "$MARKET_LABEL")"
order_pid_before="$(service_pid "$ORDER_LABEL")"
if [ -z "$target_pid_before" ] || [ -z "$market_pid_before" ] \
  || [ -z "$order_pid_before" ]; then
  echo "probability_evidence_requires_three_running_system_services" >&2
  exit 1
fi

preflight="$("$PY" - "$DB" "$ACTIVATION" "$LIVE_PAUSE" <<'PY'
import json
import shutil
import sqlite3
import sys
from pathlib import Path

database, activation_path, pause_path = (Path(value) for value in sys.argv[1:])
activation = json.loads(activation_path.read_text(encoding="utf-8"))
for key in (
    "live_decision_write_authorized",
    "live_order_authorized",
    "live_submit_authorized",
    "places_orders",
    "writes_production_db",
    "calls_order_gateway",
    "network_access",
):
    if activation.get(key) is not False:
        raise SystemExit(f"probability_evidence_activation_boundary_open:{key}")
if activation.get("execution_mode") != "production_no_live":
    raise SystemExit("probability_evidence_execution_mode_invalid")
if not pause_path.is_file():
    raise SystemExit("probability_evidence_live_pause_missing")
free = shutil.disk_usage(database.parent).free
db_bytes = database.stat().st_size
wal_path = Path(str(database) + "-wal")
wal_bytes = wal_path.stat().st_size if wal_path.exists() else 0
if free < 1 << 30 or db_bytes > 256 << 20:
    raise SystemExit(
        "probability_evidence_resource_gate_failed:"
        f"free={free},db={db_bytes},wal={wal_bytes}"
    )
with sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=30) as conn:
    if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise SystemExit("probability_evidence_preflight_quick_check_failed")
    counts = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "basket_target_order_intents",
            "basket_target_order_events",
            "basket_target_order_reconciliations",
        )
    }
print(json.dumps({
    "free_bytes": free,
    "db_bytes": db_bytes,
    "wal_bytes": wal_bytes,
    "order_counts": counts,
}, sort_keys=True))
PY
)"

target_stopped=0
restart_target() {
  /usr/bin/touch "$LIVE_PAUSE"
  /bin/launchctl enable "$TARGET_SERVICE" >/dev/null 2>&1 || true
  if ! /bin/launchctl print "$TARGET_SERVICE" >/dev/null 2>&1; then
    /bin/launchctl bootstrap system "$TARGET_PLIST" >/dev/null 2>&1 || true
  fi
  /bin/launchctl kickstart -k "$TARGET_SERVICE" >/dev/null 2>&1 || true
}
restore_target_on_error() {
  rc=$?
  if [ "$target_stopped" = "1" ]; then
    restart_target
  fi
  echo "probability_evidence_cutover_failed rc=$rc" >&2
  exit "$rc"
}
trap restore_target_on_error ERR INT TERM

/usr/bin/touch "$LIVE_PAUSE"
/bin/launchctl bootout "$TARGET_SERVICE"
target_stopped=1
deadline=$((SECONDS + 30))
while [ "$SECONDS" -lt "$deadline" ]; do
  if ! /bin/launchctl print "$TARGET_SERVICE" >/dev/null 2>&1; then
    break
  fi
  /bin/sleep 0.25
done
if /bin/launchctl print "$TARGET_SERVICE" >/dev/null 2>&1; then
  echo "probability_evidence_target_did_not_stop" >&2
  exit 1
fi

post_stop_resource="$("$PY" - "$DB" <<'PY'
import json
import shutil
import sys
from pathlib import Path

database = Path(sys.argv[1])
free = shutil.disk_usage(database.parent).free
db_bytes = database.stat().st_size
wal_path = Path(str(database) + "-wal")
wal_bytes = wal_path.stat().st_size if wal_path.exists() else 0
if free < 1 << 30 or db_bytes > 256 << 20 or wal_bytes > 64 << 20:
    raise SystemExit(
        "probability_evidence_post_stop_resource_gate_failed:"
        f"free={free},db={db_bytes},wal={wal_bytes}"
    )
print(json.dumps({
    "free_bytes": free,
    "db_bytes": db_bytes,
    "wal_bytes": wal_bytes,
}, sort_keys=True))
PY
)"

apply_receipt="$(
  "$PY" "$ROOT/scripts/apply_bayes_basket_probability_evidence_schema.py" \
    --db "$DB" \
    --apply \
    --confirm-reported-trajectory-no-stopped
)"
replay_receipt="$(
  "$PY" "$ROOT/scripts/apply_bayes_basket_probability_evidence_schema.py" \
    --db "$DB" \
    --apply \
    --confirm-reported-trajectory-no-stopped
)"

restart_target
target_stopped=0
trap - ERR INT TERM

deadline=$((SECONDS + 30))
target_pid_after=""
while [ "$SECONDS" -lt "$deadline" ]; do
  target_pid_after="$(service_pid "$TARGET_LABEL")"
  if [ -n "$target_pid_after" ] && [ "$target_pid_after" != "$target_pid_before" ]; then
    break
  fi
  /bin/sleep 0.25
done
market_pid_after="$(service_pid "$MARKET_LABEL")"
order_pid_after="$(service_pid "$ORDER_LABEL")"
if [ -z "$target_pid_after" ] || [ "$target_pid_after" = "$target_pid_before" ]; then
  echo "probability_evidence_target_restart_failed" >&2
  exit 1
fi
if [ "$market_pid_after" != "$market_pid_before" ]; then
  echo "probability_evidence_market_ws_pid_changed" >&2
  exit 1
fi
if [ "$order_pid_after" != "$order_pid_before" ]; then
  echo "probability_evidence_order_service_pid_changed" >&2
  exit 1
fi
if [ ! -f "$LIVE_PAUSE" ]; then
  echo "probability_evidence_live_pause_lost" >&2
  exit 1
fi

"$PY" "$ROOT/scripts/reported_trajectory_no_production_acceptance.py"

"$PY" - \
  "$DB" "$preflight" "$post_stop_resource" "$apply_receipt" "$replay_receipt" \
  "$target_pid_before" "$target_pid_after" \
  "$market_pid_before" "$order_pid_before" <<'PY'
import json
import sqlite3
import sys
from pathlib import Path

database = Path(sys.argv[1])
preflight = json.loads(sys.argv[2])
post_stop_resource = json.loads(sys.argv[3])
apply_receipt = json.loads(sys.argv[4])
replay_receipt = json.loads(sys.argv[5])
target_before, target_after, market_pid, order_pid = sys.argv[6:10]
for receipt in (apply_receipt, replay_receipt):
    if (
        receipt.get("state") != "migrated"
        or receipt.get("quick_check") != "ok"
        or int(receipt.get("rows_written", -1)) != 0
        or receipt.get("performs_legacy_backfill") is not False
        or receipt.get("promotes_artifacts") is not False
        or receipt.get("calls_order_gateway") is not False
        or receipt.get("live_submit_authorized") is not False
        or int(receipt.get("orders_submitted", -1)) != 0
    ):
        raise SystemExit(f"probability_evidence_apply_receipt_invalid:{receipt}")
with sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=30) as conn:
    if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise SystemExit("probability_evidence_postflight_quick_check_failed")
    present = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    required = {
        "basket_probability_evidence_artifact",
        "basket_probability_evidence_promotion",
        "basket_probability_evidence_activation",
        "basket_probability_update_proof",
    }
    if not required <= present:
        raise SystemExit(
            "probability_evidence_tables_missing:"
            + ",".join(sorted(required - present))
        )
    post_counts = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "basket_target_order_intents",
            "basket_target_order_events",
            "basket_target_order_reconciliations",
        )
    }
if post_counts != preflight["order_counts"]:
    raise SystemExit(
        f"probability_evidence_order_counts_changed:{post_counts}"
    )
print(json.dumps({
    "schema": "bayes_basket_probability_evidence_cutover_receipt_v1",
    "state": "probability_evidence_cutover_ok",
    "target_pid": f"{target_before}->{target_after}",
    "market_ws_pid": market_pid,
    "clob_order_service_pid": order_pid,
    "wal_bytes_before_stop": preflight["wal_bytes"],
    "wal_bytes_after_stop": post_stop_resource["wal_bytes"],
    "order_counts": post_counts,
    "live_pause_retained": True,
    "calls_order_gateway": False,
    "live_submit_authorized": False,
    "orders_submitted": 0,
}, sort_keys=True))
PY

echo "probability_evidence_cutover_ok"
