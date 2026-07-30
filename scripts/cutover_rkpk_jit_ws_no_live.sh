#!/bin/bash
set -euo pipefail

ROOT="/Users/xiong/weather"
DB="$ROOT/data/bayes_basket_target_ssot.db"
ACTIVATION="$ROOT/config/trading/bayes_basket_target_production_no_live_activation_v1.json"
RUNTIME="$ROOT/config/bayes_basket_target/no_live_runtime_v1.json"
PY="$ROOT/.venv/bin/python"
TARGET_LABEL="com.weather.reported_trajectory_no"
MARKET_LABEL="com.weather.market_ws"
ORDER_LABEL="com.weather.clob_order_service"
TARGET_PLIST="/Library/LaunchDaemons/${TARGET_LABEL}.plist"
LIVE_PAUSE="$ROOT/data/live_paused.flag"
STAMP="$(/bin/date -u +%Y%m%dT%H%M%SZ)"
RECEIPT="$ROOT/output/audits/rkpk_jit_ws_no_live_${STAMP}.json"
CONFIRM="${1:-}"

if [ "$CONFIRM" != "--confirm-rkpk-jit-ws-production-no-live" ]; then
  echo "用法：sudo $0 --confirm-rkpk-jit-ws-production-no-live" >&2
  exit 2
fi
if [ "$(/usr/bin/id -u)" -ne 0 ]; then
  echo "必须以 root 运行：sudo $0 --confirm-rkpk-jit-ws-production-no-live" >&2
  exit 1
fi
for required in "$DB" "$ACTIVATION" "$RUNTIME" "$PY" "$TARGET_PLIST" "$LIVE_PAUSE"; do
  if [ ! -f "$required" ]; then
    echo "rkpk_jit_ws_required_path_missing=$required" >&2
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
pause_sha_before="$(/usr/bin/shasum -a 256 "$LIVE_PAUSE" | /usr/bin/awk '{print $1}')"
if [ -z "$target_pid_before" ] || [ -z "$market_pid_before" ] \
  || [ -z "$order_pid_before" ]; then
  echo "rkpk_jit_ws_requires_three_running_system_services" >&2
  exit 1
fi

"$PY" - "$DB" "$ACTIVATION" "$RUNTIME" "$LIVE_PAUSE" <<'PY'
import shutil
import sqlite3
import sys
from pathlib import Path

database, activation_path, runtime_path, pause_path = (
    Path(value) for value in sys.argv[1:]
)
sys.path.insert(0, str(activation_path.parents[2]))
from bayes_basket_target.activation import load_target_activation
from bayes_basket_target.database import DEFAULT_TARGET_DB, resolve_target_database
from bayes_basket_target.runtime_config import load_target_no_live_runtime_config

activation = load_target_activation(activation_path)
runtime = load_target_no_live_runtime_config(runtime_path)
if resolve_target_database(database) != DEFAULT_TARGET_DB.resolve():
    raise SystemExit("rkpk_jit_ws_target_database_identity_invalid")
freshness = activation["quote_freshness_policy"]
if (
    activation.get("execution_mode") != "production_no_live"
    or activation.get("live_submit_authorized") is not False
    or activation.get("calls_order_gateway") is not False
    or freshness.get("policy_id") != "target-official-ws-60s-no-live-v2"
    or float(freshness.get("max_quote_age_seconds", 0.0)) != 60.0
    or freshness.get("authority_scope") != "PRODUCTION_NO_LIVE_CANDIDATE"
    or runtime.get("target_only") is not True
    or runtime.get("capital_scope") != "target_owned_only"
    or float(runtime.get("target_remaining_budget_usdc", 0.0)) != 12.0
    or runtime.get("reads_shared_live_capital") is not False
    or runtime.get("calls_order_gateway") is not False
):
    raise SystemExit("rkpk_jit_ws_activation_boundary_invalid")
if not pause_path.is_file():
    raise SystemExit("rkpk_jit_ws_live_pause_missing")
free = shutil.disk_usage(database.parent).free
if free < max(2 * 1024**3, database.stat().st_size * 3):
    raise SystemExit("rkpk_jit_ws_free_space_too_low")
with sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=30) as conn:
    if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise SystemExit("rkpk_jit_ws_preflight_quick_check_failed")
PY

/usr/bin/touch "$LIVE_PAUSE"
target_stopped=0

restore_target_on_error() {
  /usr/bin/touch "$LIVE_PAUSE"
  if [ "$target_stopped" = "1" ] \
    && ! /bin/launchctl print "system/$TARGET_LABEL" >/dev/null 2>&1; then
    /bin/launchctl bootstrap system "$TARGET_PLIST" >/dev/null 2>&1 || true
  fi
  market_pid_error="$(service_pid "$MARKET_LABEL")"
  order_pid_error="$(service_pid "$ORDER_LABEL")"
  if [ "$market_pid_error" != "$market_pid_before" ] \
    || [ "$order_pid_error" != "$order_pid_before" ]; then
    echo "rkpk_jit_ws_non_target_pid_changed_on_error" >&2
  fi
}
trap restore_target_on_error ERR

/bin/launchctl bootout "system/$TARGET_LABEL"
target_stopped=1
deadline=$((SECONDS + 30))
while [ "$SECONDS" -lt "$deadline" ]; do
  if ! /bin/launchctl print "system/$TARGET_LABEL" >/dev/null 2>&1; then
    break
  fi
  /bin/sleep 0.25
done
if /bin/launchctl print "system/$TARGET_LABEL" >/dev/null 2>&1; then
  echo "rkpk_jit_ws_target_did_not_stop" >&2
  exit 1
fi

capital_receipt="$(
  "$PY" - "$DB" "$RUNTIME" <<'PY'
from datetime import datetime, timezone
import json
import sqlite3
import sys
from pathlib import Path

database, runtime_path = (Path(value) for value in sys.argv[1:])
sys.path.insert(0, str(runtime_path.parents[2]))
from bayes_basket_target.database import DEFAULT_TARGET_DB, resolve_target_database
from bayes_basket_target.no_live_capital_epoch import (
    migrate_target_no_live_capital_epoch,
)

resolved = resolve_target_database(database)
if resolved != DEFAULT_TARGET_DB.resolve():
    raise SystemExit("rkpk_jit_ws_capital_database_identity_invalid")
now = datetime.now(timezone.utc).isoformat(
    timespec="milliseconds"
).replace("+00:00", "Z")
with sqlite3.connect(
    resolved, timeout=60, isolation_level=None
) as conn:
    conn.execute("PRAGMA busy_timeout=60000")
    apply_result = migrate_target_no_live_capital_epoch(
        conn,
        runtime_config_path=runtime_path,
        migrated_at_utc=now,
    )
    replay_result = migrate_target_no_live_capital_epoch(
        conn,
        runtime_config_path=runtime_path,
        migrated_at_utc=now,
    )
    if (
        replay_result.get("state") != "UNCHANGED_ZERO_WRITE"
        or replay_result.get("rows_written") != 0
        or apply_result.get("calls_order_gateway") is not False
        or apply_result.get("orders_submitted") != 0
    ):
        raise SystemExit("rkpk_jit_ws_capital_replay_not_idempotent")
    if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise SystemExit("rkpk_jit_ws_capital_post_apply_quick_check_failed")
print(json.dumps({
    "apply": apply_result,
    "replay": replay_result,
}, sort_keys=True))
PY
)"

activation_receipt="$(
  "$PY" - "$DB" "$ACTIVATION" <<'PY'
from datetime import datetime, timezone
import json
import sqlite3
import sys
from pathlib import Path

database, activation_path = (Path(value) for value in sys.argv[1:])
sys.path.insert(0, str(activation_path.parents[2]))
from bayes_basket_target.activation import load_target_activation

activation = load_target_activation(activation_path)
now = datetime.now(timezone.utc).isoformat(
    timespec="milliseconds"
).replace("+00:00", "Z")
with sqlite3.connect(database, timeout=60, isolation_level=None) as conn:
    conn.execute("PRAGMA busy_timeout=60000")
    row = conn.execute(
        "SELECT generation,activation_sha256 FROM "
        "basket_strategy_activation_state WHERE singleton=1"
    ).fetchone()
    if row is None:
        raise SystemExit("rkpk_jit_ws_activation_state_missing")
    previous_generation = int(row[0])
    previous_sha = str(row[1])
    target_sha = str(activation["activation_contract_sha256"])
    if previous_sha == target_sha:
        generation = previous_generation
        state = "UNCHANGED_ZERO_WRITE"
    else:
        generation = previous_generation + 1
        payload = {
            **activation,
            "generation": generation,
            "activated_at": now,
        }
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "UPDATE basket_strategy_activation_state SET "
            "generation=?,state='enabled',strategy_id=?,activation_sha256=?,"
            "activation_json=?,updated_at=? WHERE singleton=1 "
            "AND generation=? AND activation_sha256=?",
            (
                generation,
                str(activation["strategy_id"]),
                target_sha,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                now,
                previous_generation,
                previous_sha,
            ),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            raise SystemExit("rkpk_jit_ws_activation_cas_failed")
        conn.commit()
        state = "COMMITTED"
    if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise SystemExit("rkpk_jit_ws_post_apply_quick_check_failed")
print(json.dumps({
    "state": state,
    "generation": generation,
    "previous_generation": previous_generation,
    "activation_sha256": target_sha,
    "execution_mode": "production_no_live",
    "live_submit_authorized": False,
    "calls_order_gateway": False,
    "orders_submitted": 0,
}, sort_keys=True))
PY
)"

/bin/launchctl bootstrap system "$TARGET_PLIST"
target_stopped=0
deadline=$((SECONDS + 30))
target_pid_after=""
while [ "$SECONDS" -lt "$deadline" ]; do
  target_pid_after="$(service_pid "$TARGET_LABEL")"
  if [ -n "$target_pid_after" ] \
    && [ "$target_pid_after" != "$target_pid_before" ]; then
    break
  fi
  /bin/sleep 0.25
done
if [ -z "$target_pid_after" ] \
  || [ "$target_pid_after" = "$target_pid_before" ]; then
  echo "rkpk_jit_ws_target_pid_did_not_advance" >&2
  exit 1
fi

market_pid_after="$(service_pid "$MARKET_LABEL")"
order_pid_after="$(service_pid "$ORDER_LABEL")"
if [ "$market_pid_after" != "$market_pid_before" ] \
  || [ "$order_pid_after" != "$order_pid_before" ]; then
  echo "rkpk_jit_ws_non_target_pid_changed" >&2
  exit 1
fi
if [ ! -f "$LIVE_PAUSE" ]; then
  echo "rkpk_jit_ws_live_pause_missing_after_restart" >&2
  exit 1
fi
pause_sha_after="$(/usr/bin/shasum -a 256 "$LIVE_PAUSE" | /usr/bin/awk '{print $1}')"
if [ "$pause_sha_after" != "$pause_sha_before" ]; then
  echo "rkpk_jit_ws_live_pause_content_changed" >&2
  exit 1
fi

"$PY" - \
  "$DB" "$ACTIVATION" "$RECEIPT" "$activation_receipt" "$capital_receipt" \
  "$target_pid_before" "$target_pid_after" \
  "$market_pid_before" "$market_pid_after" \
  "$order_pid_before" "$order_pid_after" \
  "$pause_sha_before" "$pause_sha_after" <<'PY'
from datetime import datetime, timezone
import json
import sqlite3
import sys
from pathlib import Path

database, activation_path, receipt_path = (
    Path(value) for value in sys.argv[1:4]
)
activation_receipt = json.loads(sys.argv[4])
capital_receipt = json.loads(sys.argv[5])
(
    target_before,
    target_after,
    market_before,
    market_after,
    order_before,
    order_after,
    pause_sha_before,
    pause_sha_after,
) = sys.argv[6:14]
sys.path.insert(0, str(activation_path.parents[2]))
from bayes_basket_target.activation import load_target_activation
from bayes_basket_target.resource_bounds import write_bounded_json

activation = load_target_activation(activation_path)
with sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=30) as conn:
    row = conn.execute(
        "SELECT generation,activation_sha256 FROM "
        "basket_strategy_activation_state WHERE singleton=1"
    ).fetchone()
    if row is None or str(row[1]) != activation["activation_contract_sha256"]:
        raise SystemExit("rkpk_jit_ws_activation_state_drift")
    if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise SystemExit("rkpk_jit_ws_final_quick_check_failed")
    pool = conn.execute(
        "SELECT balance_usdc,reserved_usdc,capital_epoch_id,version "
        "FROM basket_target_capital_pool WHERE pool_id=1"
    ).fetchone()
    expected_pool = capital_receipt["apply"]["after"]
    if (
        pool is None
        or abs(float(pool[0]) - float(expected_pool["balance_usdc"])) > 1e-9
        or float(pool[1]) < 0
        or float(pool[1]) > float(pool[0])
        or str(pool[2]) != str(expected_pool["capital_epoch_id"])
        or int(pool[3]) < int(expected_pool["version"])
    ):
        raise SystemExit("rkpk_jit_ws_final_capital_pool_drift")
    execution_counts = {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in (
            "basket_target_order_events",
            "basket_target_execution_receipts",
            "basket_target_execution_transition_versions",
            "basket_target_post_fill_recompute_requests",
        )
    }
    if any(execution_counts.values()):
        raise SystemExit("rkpk_jit_ws_unexpected_execution_write")
    unauthorized_intents = int(conn.execute(
        "SELECT COUNT(*) FROM basket_target_order_intents "
        "WHERE live_submit_authorized<>0"
    ).fetchone()[0])
    if unauthorized_intents:
        raise SystemExit("rkpk_jit_ws_live_authorized_intent_present")
identity = {
    "schema": "rkpk_jit_ws_no_live_cutover_receipt_v2",
    "state": "ACTIVATED_AND_TARGET_RESTARTED",
    "created_at_utc": datetime.now(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z"),
    "database": str(database),
    "activation": activation_receipt,
    "capital_epoch_migration": capital_receipt,
    "capital_pool_after_restart": {
        "balance_usdc": float(pool[0]),
        "reserved_usdc": float(pool[1]),
        "capital_epoch_id": str(pool[2]),
        "version": int(pool[3]),
    },
    "execution_state_counts": execution_counts,
    "live_authorized_intent_count": unauthorized_intents,
    "quote_freshness_policy": activation["quote_freshness_policy"],
    "target_pid_before": int(target_before),
    "target_pid_after": int(target_after),
    "market_ws_pid_before": int(market_before),
    "market_ws_pid_after": int(market_after),
    "clob_order_service_pid_before": int(order_before),
    "clob_order_service_pid_after": int(order_after),
    "execution_mode": "production_no_live",
    "live_pause_retained": True,
    "live_pause_sha256_before": pause_sha_before,
    "live_pause_sha256_after": pause_sha_after,
    "live_submit_authorized": False,
    "calls_order_gateway": False,
    "orders_submitted": 0,
    "quick_check": "ok",
}
write_bounded_json(receipt_path, identity)
print(json.dumps(identity, ensure_ascii=False, sort_keys=True))
PY

trap - ERR
echo "rkpk_jit_ws_no_live_ok"
echo "reported_trajectory_no_pid=$target_pid_before->$target_pid_after"
echo "market_ws_pid=$market_pid_after"
echo "clob_order_service_pid=$order_pid_after"
echo "live_pause_retained=true"
