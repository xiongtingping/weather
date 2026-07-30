#!/bin/bash
set -euo pipefail

ROOT="/Users/xiong/weather"
DB="$ROOT/data/bayes_basket_target_ssot.db"
ACTIVATION="$ROOT/config/trading/bayes_basket_target_production_no_live_activation_v1.json"
LIVE_PAUSE="$ROOT/data/live_paused.flag"
WS_MANIFEST="$ROOT/data/bayes_basket_target_official_ws_manifest.json"
TARGET_LABEL="com.weather.reported_trajectory_no"
MARKET_LABEL="com.weather.market_ws"
ORDER_LABEL="com.weather.clob_order_service"
CONFIRM="${1:-}"

if [ "$CONFIRM" != "--confirm-probability-consumer-authority-production-no-live" ]; then
  echo "用法：sudo $0 --confirm-probability-consumer-authority-production-no-live" >&2
  exit 2
fi
if [ "$(id -u)" -ne 0 ]; then
  echo "必须以 root 运行：sudo $0 --confirm-probability-consumer-authority-production-no-live" >&2
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
if [ -z "$target_before" ] || [ -z "$market_before" ] || [ -z "$order_before" ]; then
  echo "probability_consumer_reload_requires_three_running_system_services" >&2
  exit 1
fi

/usr/bin/touch "$LIVE_PAUSE"
/usr/bin/python3 -m py_compile \
  "$ROOT/bayes_basket_target/production_io.py" \
  "$ROOT/bayes_basket_target/p0_preselection.py" \
  "$ROOT/bayes_basket_target/delta_v2_runtime.py" \
  "$ROOT/bayes_basket_target/basket_kernel/delta_v2.py" \
  "$ROOT/bayes_basket_target/production_worker.py" \
  "$ROOT/scripts/resident_target_ws_publisher.py"

order_events_before="$(
  /usr/bin/python3 - "$DB" "$ACTIVATION" <<'PY'
import json
import shutil
import sqlite3
import sys
from pathlib import Path

database = Path(sys.argv[1])
activation = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
if (
    activation.get("execution_mode") != "production_no_live"
    or activation.get("state") != "production_no_live"
    or activation.get("live_submit_authorized") is not False
    or activation.get("authorizes_order") is not False
):
    raise SystemExit("probability_consumer_reload_no_live_boundary_invalid")
if shutil.disk_usage(database.parent).free < 2 * 1024 * 1024 * 1024:
    raise SystemExit("probability_consumer_reload_min_free_space_guard")
with sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=30) as conn:
    if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise SystemExit("probability_consumer_reload_quick_check_failed")
    print(conn.execute(
        "SELECT COUNT(*) FROM basket_target_order_events"
    ).fetchone()[0])
PY
)"
preselection_before="$(
  /usr/bin/python3 - "$DB" <<'PY'
import sqlite3
import sys
with sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True) as conn:
    print(conn.execute(
        "SELECT COUNT(*) FROM basket_p0_preselection_plan"
    ).fetchone()[0])
PY
)"

/bin/launchctl kickstart -k "system/$MARKET_LABEL"
market_after="$(wait_for_new_pid "$MARKET_LABEL" "$market_before")" || {
  echo "probability_consumer_reload_market_pid_did_not_advance" >&2
  exit 1
}

deadline=$((SECONDS + 30))
dual_scope_ready=false
while [ "$SECONDS" -lt "$deadline" ]; do
  if /usr/bin/python3 - "$WS_MANIFEST" <<'PY' >/dev/null 2>&1
import json
import sys
from pathlib import Path

manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
scopes = manifest.get("scopes")
if (
    manifest.get("state") != "connected"
    or not isinstance(scopes, list)
    or len(scopes) != 2
):
    raise SystemExit(1)
states = {str(item.get("event_day_state") or "") for item in scopes}
if states != {"open", "not_started"}:
    raise SystemExit(1)
for item in scopes:
    fresh = item.get("probability_evidence_fresh")
    if fresh not in {True, False}:
        raise SystemExit(1)
    if item.get("event_day_state") == "not_started" and fresh is not True:
        raise SystemExit(1)
PY
  then
    dual_scope_ready=true
    break
  fi
  /bin/sleep 0.25
done
if [ "$dual_scope_ready" != true ]; then
  echo "probability_consumer_reload_dual_scope_manifest_missing" >&2
  exit 1
fi

/bin/launchctl kickstart -k "system/$TARGET_LABEL"
target_after="$(wait_for_new_pid "$TARGET_LABEL" "$target_before")" || {
  echo "probability_consumer_reload_target_pid_did_not_advance" >&2
  exit 1
}
deadline=$((SECONDS + 30))
while [ "$SECONDS" -lt "$deadline" ]; do
  preselection_now="$(
    /usr/bin/python3 - "$DB" <<'PY'
import sqlite3
import sys
with sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True) as conn:
    print(conn.execute(
        "SELECT COUNT(*) FROM basket_p0_preselection_plan"
    ).fetchone()[0])
PY
  )"
  if [ "$preselection_now" -gt "$preselection_before" ]; then
    break
  fi
  /bin/sleep 0.25
done

if [ "$(service_pid "$MARKET_LABEL")" != "$market_after" ]; then
  echo "probability_consumer_reload_market_service_changed" >&2
  exit 1
fi
if [ "$(service_pid "$ORDER_LABEL")" != "$order_before" ]; then
  echo "probability_consumer_reload_order_service_changed" >&2
  exit 1
fi
if [ ! -f "$LIVE_PAUSE" ]; then
  echo "probability_consumer_reload_live_pause_missing" >&2
  exit 1
fi

/usr/bin/python3 - "$DB" "$order_events_before" "$preselection_before" <<'PY'
import json
import sqlite3
import sys

database = sys.argv[1]
order_events_before = int(sys.argv[2])
preselection_before = int(sys.argv[3])
with sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=30) as conn:
    if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise SystemExit("probability_consumer_reload_post_quick_check_failed")
    order_events_after = conn.execute(
        "SELECT COUNT(*) FROM basket_target_order_events"
    ).fetchone()[0]
    live_intents = conn.execute(
        "SELECT COUNT(*) FROM basket_target_order_intents "
        "WHERE live_submit_authorized<>0"
    ).fetchone()[0]
    will_order = conn.execute(
        "SELECT COUNT(*) FROM auto_trade_decisions WHERE will_order<>0"
    ).fetchone()[0]
    preselection_after = conn.execute(
        "SELECT COUNT(*) FROM basket_p0_preselection_plan"
    ).fetchone()[0]
    if order_events_after != order_events_before:
        raise SystemExit("probability_consumer_reload_order_event_drift")
    if live_intents or will_order:
        raise SystemExit("probability_consumer_reload_no_live_boundary_drift")
    if preselection_after <= preselection_before:
        raise SystemExit("probability_consumer_reload_preselection_not_materialized")
    latest = conn.execute(
        "SELECT plan_json FROM basket_p0_preselection_plan "
        "ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    plan = json.loads(latest[0])
    runtime = dict((plan.get("compiled_plan") or {}).get("plan") or {})
    selection = dict(runtime.get("delta_v2_action_selection") or {})
    if "maker_candidate_diagnostics" not in runtime:
        raise SystemExit("probability_consumer_reload_delta_diagnostics_missing")
    if (
        not selection.get("required_buckets")
        and "complete_basket_candidate_missing"
        in set(selection.get("blockers") or ())
    ):
        raise SystemExit("probability_consumer_reload_empty_target_misclassified")
    print(json.dumps({
        "state": "probability_consumer_authority_no_live_ok",
        "quick_check": "ok",
        "order_events": order_events_after,
        "live_authorized_order_intents": live_intents,
        "will_order_decisions": will_order,
        "p0_preselection_count": preselection_after,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }, sort_keys=True))
PY

echo "reported_trajectory_no_pid=$target_before->$target_after"
echo "market_ws_pid=$market_before->$market_after"
echo "clob_order_service_pid=$order_before"
echo "live_pause_retained=true"
