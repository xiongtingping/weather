#!/bin/bash
set -euo pipefail

ROOT="/Users/xiong/weather"
TARGET_LABEL="com.weather.reported_trajectory_no"
MARKET_LABEL="com.weather.market_ws"
ORDER_LABEL="com.weather.clob_order_service"
TARGET_DB="$ROOT/data/bayes_basket_target_ssot.db"
TARGET_MANIFEST="$ROOT/data/bayes_basket_target_official_ws_manifest.json"
LIVE_PAUSE="$ROOT/data/live_paused.flag"
CONFIRM="${1:-}"

if [ "$CONFIRM" != "--confirm-p0-economic-identity-production-no-live" ]; then
  echo "用法：sudo $0 --confirm-p0-economic-identity-production-no-live" >&2
  exit 2
fi
if [ "$(id -u)" -ne 0 ]; then
  echo "必须以 root 运行：sudo $0 --confirm-p0-economic-identity-production-no-live" >&2
  exit 1
fi

service_pid() {
  /bin/launchctl print "system/$1" 2>/dev/null \
    | /usr/bin/awk '/^[[:space:]]*pid = [0-9]+/{print $3; exit}'
}

wait_for_new_pid() {
  label="$1"
  old_pid="$2"
  deadline=$((SECONDS + 30))
  while [ "$SECONDS" -lt "$deadline" ]; do
    current_pid="$(service_pid "$label")"
    if [ -n "$current_pid" ] && [ "$current_pid" != "$old_pid" ]; then
      echo "$current_pid"
      return 0
    fi
    /bin/sleep 0.25
  done
  return 1
}

target_before="$(service_pid "$TARGET_LABEL")"
market_before="$(service_pid "$MARKET_LABEL")"
order_before="$(service_pid "$ORDER_LABEL")"
if [ -z "$target_before" ] || [ -z "$market_before" ] \
  || [ -z "$order_before" ]; then
  echo "p0_economic_reload_requires_three_running_system_services" >&2
  exit 1
fi

/usr/bin/touch "$LIVE_PAUSE"

/usr/bin/python3 - "$ROOT" "$TARGET_DB" <<'PY'
import json
import shutil
import sqlite3
import sys
from pathlib import Path

root = Path(sys.argv[1])
database = Path(sys.argv[2])
activation = json.loads(
    (
        root
        / "config/trading/"
        "bayes_basket_target_production_no_live_activation_v1.json"
    ).read_text(encoding="utf-8")
)
if (
    activation.get("execution_mode") != "production_no_live"
    or activation.get("state") != "production_no_live"
    or activation.get("live_submit_authorized") is not False
    or activation.get("authorizes_order") is not False
):
    raise SystemExit("p0_economic_reload_no_live_boundary_invalid")
if shutil.disk_usage(database.parent).free < 2 * 1024 * 1024 * 1024:
    raise SystemExit("p0_economic_reload_min_free_space_guard")
with sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=30) as conn:
    if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise SystemExit("p0_economic_reload_target_quick_check_failed")
PY

started_at="$(/bin/date -u '+%Y-%m-%dT%H:%M:%SZ')"
manifest_mtime_before="$(
  /usr/bin/stat -f '%m' "$TARGET_MANIFEST" 2>/dev/null || echo 0
)"

/bin/launchctl kickstart -k "system/$MARKET_LABEL"
market_after="$(wait_for_new_pid "$MARKET_LABEL" "$market_before")" || {
  echo "p0_economic_reload_market_pid_did_not_advance" >&2
  exit 1
}
if [ "$(service_pid "$ORDER_LABEL")" != "$order_before" ]; then
  echo "p0_economic_reload_order_service_pid_changed" >&2
  exit 1
fi
if [ "$(service_pid "$TARGET_LABEL")" != "$target_before" ]; then
  echo "p0_economic_reload_target_changed_before_target_phase" >&2
  exit 1
fi

deadline=$((SECONDS + 30))
while [ "$SECONDS" -lt "$deadline" ]; do
  manifest_mtime="$(
    /usr/bin/stat -f '%m' "$TARGET_MANIFEST" 2>/dev/null || echo 0
  )"
  if [ "$manifest_mtime" -gt "$manifest_mtime_before" ]; then
    break
  fi
  /bin/sleep 0.25
done
if [ "${manifest_mtime:-0}" -le "$manifest_mtime_before" ]; then
  echo "p0_economic_reload_target_manifest_not_refreshed" >&2
  exit 1
fi

/bin/launchctl kickstart -k "system/$TARGET_LABEL"
target_after="$(wait_for_new_pid "$TARGET_LABEL" "$target_before")" || {
  echo "p0_economic_reload_target_pid_did_not_advance" >&2
  exit 1
}
if [ "$(service_pid "$MARKET_LABEL")" != "$market_after" ]; then
  echo "p0_economic_reload_market_pid_changed_twice" >&2
  exit 1
fi
if [ "$(service_pid "$ORDER_LABEL")" != "$order_before" ]; then
  echo "p0_economic_reload_order_service_pid_changed" >&2
  exit 1
fi
if [ ! -f "$LIVE_PAUSE" ]; then
  echo "p0_economic_reload_live_pause_missing" >&2
  exit 1
fi

/bin/sleep 35

/usr/bin/python3 - "$TARGET_DB" "$started_at" <<'PY'
import json
import sqlite3
import sys
from pathlib import Path

database = Path(sys.argv[1])
started_at = sys.argv[2]
with sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=30) as conn:
    if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise SystemExit("p0_economic_reload_post_quick_check_failed")
    rows = conn.execute(
        "SELECT checkpoint_id,plan_json FROM basket_p0_preselection_plan "
        "WHERE created_at>=? ORDER BY rowid",
        (started_at,),
    ).fetchall()
    identities = []
    for checkpoint_id, plan_json in rows:
        payload = json.loads(plan_json)
        economic = str(payload.get("economic_quote_sha256") or "")
        if len(economic) != 64:
            raise SystemExit(
                "p0_economic_reload_economic_quote_identity_missing"
            )
        identities.append((str(checkpoint_id), economic))
    if len(identities) != len(set(identities)):
        raise SystemExit("p0_economic_reload_duplicate_economic_write")
    result = {
        "state": "p0_economic_identity_no_live_ok",
        "new_preselection_count": len(rows),
        "distinct_economic_identity_count": len(set(identities)),
        "quick_check": "ok",
        "live_submit_authorized": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    print(json.dumps(result, sort_keys=True))
PY

echo "reported_trajectory_no_pid=$target_before->$target_after"
echo "market_ws_pid=$market_before->$market_after"
echo "clob_order_service_pid=$order_before"
echo "live_pause_retained=true"
