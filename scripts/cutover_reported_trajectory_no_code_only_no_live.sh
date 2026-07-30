#!/bin/bash
set -euo pipefail

ROOT="/Users/xiong/weather"
LABEL="com.weather.reported_trajectory_no"
MARKET_LABEL="com.weather.market_ws"
ORDER_LABEL="com.weather.clob_order_service"
PAUSE="$ROOT/data/live_paused.flag"
CONFIRM="${1:-}"

if [ "$CONFIRM" != "--confirm-code-only-production-no-live" ]; then
  echo "用法：sudo $0 --confirm-code-only-production-no-live" >&2
  exit 2
fi
if [ "$(id -u)" -ne 0 ]; then
  echo "必须以 root 运行：sudo $0 --confirm-code-only-production-no-live" >&2
  exit 1
fi

service_pid() {
  launchctl print "system/$1" 2>/dev/null \
    | /usr/bin/awk '/^[[:space:]]*pid = [0-9]+/{print $3; exit}'
}

market_pid_before="$(service_pid "$MARKET_LABEL")"
order_pid_before="$(service_pid "$ORDER_LABEL")"
target_pid_before="$(service_pid "$LABEL")"
if [ -z "$market_pid_before" ] || [ -z "$order_pid_before" ] \
  || [ -z "$target_pid_before" ]; then
  echo "code_only_cutover_requires_three_running_system_services" >&2
  exit 1
fi

# This cutover is strictly a production_no_live code reload.  It must never
# open the live boundary, even on success or rollback.
mkdir -p "$ROOT/data"
touch "$PAUSE"

rollback_to_pause() {
  touch "$PAUSE"
  /usr/bin/python3 - "$ROOT" <<'PY' >/dev/null 2>&1 || true
import sqlite3
import sys
from pathlib import Path
root = Path(sys.argv[1])
with sqlite3.connect(root / "data/archive.db", timeout=30) as conn:
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute(
        "UPDATE reported_trajectory_activation_state "
        "SET state='paused',updated_at=datetime('now') WHERE singleton=1"
    )
    conn.commit()
PY
}
trap rollback_to_pause ERR

/usr/bin/python3 - "$ROOT" <<'PY'
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

root = Path(sys.argv[1])
config = json.loads(
    (root / "config/trading/reported_trajectory_no_v1.json").read_text()
)
target = json.loads(
    (
        root
        / "config/trading/"
        "bayes_basket_target_production_no_live_activation_v1.json"
    ).read_text()
)
if config.get("production_source_allowlist") != ["amsc", "awc"]:
    raise SystemExit("code_only_cutover_source_allowlist_invalid")
order = config.get("order_policy") or {}
if (
    order.get("maximum_buy_price") != 0.99
    or order.get("deploy_all_available") is not False
    or order.get("maximum_order_notional_usdc") != 50.0
):
    raise SystemExit("code_only_cutover_reported_trajectory_policy_invalid")
if (
    target.get("execution_mode") != "production_no_live"
    or target.get("state") != "production_no_live"
    or target.get("live_submit_authorized") is not False
    or target.get("authorizes_order") is not False
):
    raise SystemExit("code_only_cutover_target_no_live_boundary_invalid")

encoded = json.dumps(
    config,
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
).encode()
config_sha = hashlib.sha256(encoded).hexdigest()
now = datetime.now(timezone.utc).isoformat(
    timespec="milliseconds"
).replace("+00:00", "Z")
with sqlite3.connect(root / "data/archive.db", timeout=60) as conn:
    conn.execute("PRAGMA busy_timeout=60000")
    row = conn.execute(
        "SELECT generation FROM reported_trajectory_activation_state "
        "WHERE singleton=1"
    ).fetchone()
    if row is None:
        raise SystemExit("code_only_cutover_activation_missing")
    generation = int(row[0] or 0) + 1
    payload = {
        "schema": "reported_trajectory_no_activation_v1",
        "generation": generation,
        "state": "enabled",
        "strategy_id": "reported_trajectory_no_v1",
        "maximum_buy_price": 0.99,
        "order_type": "FAK",
        "capital_policy": {
            "unified_pool": True,
            "deploy_all_available": False,
            "maximum_order_notional_usdc": 50.0,
        },
        "production_source_allowlist": ["amsc", "awc"],
        "exit_policy": {
            "side": "SELL",
            "limit_price": 0.99,
            "order_type": "GTC",
        },
        "old_bayes_execution_disabled": True,
        "awc_amsc_disjoint": True,
        "production_no_live": True,
        "live_pause_retained": True,
        "config_sha256": config_sha,
        "activated_at": now,
    }
    activation_sha = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "UPDATE reported_trajectory_activation_state SET "
        "generation=?,state='enabled',activation_sha256=?,config_sha256=?,"
        "old_bayes_execution_disabled=1,awc_amsc_disjoint=1,"
        "activation_json=?,updated_at=? WHERE singleton=1",
        (
            generation,
            activation_sha,
            config_sha,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            now,
        ),
    )
    conn.commit()
print(
    json.dumps(
        {
            "generation": generation,
            "config_sha256": config_sha,
            "activation_sha256": activation_sha,
            "live_pause_retained": True,
        },
        sort_keys=True,
    )
)
PY

launchctl kickstart -k "system/$LABEL"

deadline=$((SECONDS + 30))
target_pid_after=""
while [ "$SECONDS" -lt "$deadline" ]; do
  target_pid_after="$(service_pid "$LABEL")"
  if [ -n "$target_pid_after" ] && [ "$target_pid_after" != "$target_pid_before" ]; then
    break
  fi
  sleep 0.25
done
if [ -z "$target_pid_after" ] || [ "$target_pid_after" = "$target_pid_before" ]; then
  echo "code_only_cutover_target_pid_did_not_advance" >&2
  exit 1
fi

market_pid_after="$(service_pid "$MARKET_LABEL")"
order_pid_after="$(service_pid "$ORDER_LABEL")"
if [ "$market_pid_after" != "$market_pid_before" ] \
  || [ "$order_pid_after" != "$order_pid_before" ]; then
  echo "code_only_cutover_non_target_pid_changed" >&2
  exit 1
fi
if [ ! -f "$PAUSE" ]; then
  echo "code_only_cutover_live_pause_missing" >&2
  exit 1
fi

trap - ERR
echo "reported_trajectory_no_code_only_no_live_ok"
echo "reported_trajectory_no_pid=$target_pid_before->$target_pid_after"
echo "market_ws_pid=$market_pid_after"
echo "clob_order_service_pid=$order_pid_after"
echo "live_pause_retained=true"
