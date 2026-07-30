#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [ "$(id -u)" -ne 0 ]; then
  echo "必须以 root 运行：sudo $0" >&2
  exit 1
fi

PAUSE="$ROOT/data/live_paused.flag"
activated=0

mkdir -p "$ROOT/data"
touch "$PAUSE"

SYSTEM_USER="${WEATHER_SYSTEM_USER:-${SUDO_USER:-xiong}}"
SYSTEM_UID="$(id -u "$SYSTEM_USER")"

# Close every old owner before taking the SQLite linearization lock.  The
# pause file prevents new order admission, while bootout prevents a resident
# writer from racing the activation generation update.
for label in \
  com.weather.reported_trajectory_no \
  com.weather.market_ws \
  com.weather.clob_order_service; do
  launchctl disable "system/$label" >/dev/null 2>&1 || true
  launchctl bootout "system/$label" >/dev/null 2>&1 || true
done
# The model-release watcher is a retired non-core producer.  It must be
# unloaded in the same maintenance boundary so readiness cannot observe a
# fourth system-domain owner after the three target services are installed.
launchctl disable system/com.weather.model_release_watcher >/dev/null 2>&1 || true
launchctl bootout system/com.weather.model_release_watcher >/dev/null 2>&1 || true
for label in com.weather.monitor.daemon com.xiong.weather.ui; do
  launchctl disable "gui/${SYSTEM_UID}/${label}" >/dev/null 2>&1 || true
  launchctl bootout "gui/${SYSTEM_UID}/${label}" >/dev/null 2>&1 || true
done

pause_activation() {
  if [ "$activated" -eq 1 ]; then
    /usr/bin/python3 - "$ROOT" <<'PY'
import sqlite3
import sys
from pathlib import Path
root = Path(sys.argv[1])
conn = sqlite3.connect(root / "data/archive.db", timeout=10)
conn.execute("UPDATE reported_trajectory_activation_state SET state='paused',updated_at=datetime('now') WHERE singleton=1")
conn.commit()
PY
  fi
  touch "$PAUSE"
}
trap pause_activation ERR

/usr/bin/python3 - "$ROOT" <<'PY'
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root))
from reported_trajectory_no_schema import apply_reported_trajectory_no_schema

config = json.loads((root / "config/trading/reported_trajectory_no_v1.json").read_text())
if config.get("production_source_allowlist") != ["amsc", "awc"]:
    raise SystemExit("system_cutover_requires_amsc_awc_allowlist")
if config.get("order_policy", {}).get("maximum_buy_price") != 0.99:
    raise SystemExit("system_cutover_requires_max_buy_price_0_99")
if config.get("order_policy", {}).get("deploy_all_available") is not False:
    raise SystemExit("system_cutover_requires_deploy_all_available_false")
if config.get("order_policy", {}).get("maximum_order_notional_usdc") != 50.0:
    raise SystemExit("system_cutover_requires_maximum_order_notional_50")
if config.get("order_policy", {}).get("collateral_balance_check") is not False:
    raise SystemExit("system_cutover_requires_collateral_balance_check_disabled")
encoded = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
config_sha = hashlib.sha256(encoded).hexdigest()
conn = sqlite3.connect(root / "data/archive.db", timeout=10)
conn.execute("PRAGMA busy_timeout=10000")
apply_reported_trajectory_no_schema(conn)
# Schema DDL/data backfill uses SQLite's implicit transaction.  Complete it
# before taking the activation linearization lock below; otherwise SQLite
# rejects the nested BEGIN IMMEDIATE and the controlled cutover fails closed.
conn.commit()
prior = conn.execute("SELECT generation FROM reported_trajectory_activation_state WHERE singleton=1").fetchone()
generation = int(prior[0] or 0) + 1 if prior else 1
payload = {
    "schema": "reported_trajectory_no_activation_v1",
    "generation": generation,
    "state": "enabled",
    "strategy_id": "reported_trajectory_no_v1",
    "maximum_buy_price": float(config["order_policy"]["maximum_buy_price"]),
    "order_type": "FAK",
    "capital_policy": {
        "unified_pool": True,
        "deploy_all_available": False,
        "maximum_order_notional_usdc": float(config["order_policy"]["maximum_order_notional_usdc"]),
    },
    "production_source_allowlist": ["amsc", "awc"],
    "exit_policy": {"side": "SELL", "limit_price": 0.99, "order_type": "GTC"},
    "old_bayes_execution_disabled": True,
    "awc_amsc_disjoint": True,
    "config_sha256": config_sha,
    "activated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
}
activation_sha = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
conn.execute("BEGIN IMMEDIATE")
conn.execute(
    """INSERT INTO reported_trajectory_activation_state(
      singleton,generation,state,activation_sha256,config_sha256,
      old_bayes_execution_disabled,awc_amsc_disjoint,activation_json,updated_at
    ) VALUES(1,?,'enabled',?,?,1,1,?,?)
    ON CONFLICT(singleton) DO UPDATE SET generation=excluded.generation,state='enabled',
      activation_sha256=excluded.activation_sha256,config_sha256=excluded.config_sha256,
      old_bayes_execution_disabled=1,awc_amsc_disjoint=1,
      activation_json=excluded.activation_json,updated_at=excluded.updated_at""",
    (generation, activation_sha, config_sha, json.dumps(payload, ensure_ascii=False, sort_keys=True), payload["activated_at"]),
)
conn.commit()
print(json.dumps({"generation": generation, "config_sha256": config_sha, "activation_sha256": activation_sha}))
PY
activated=1

bash "$ROOT/scripts/install_reported_trajectory_no_system_services.sh"
activated=0
echo "system_generation_cutover_ok"
