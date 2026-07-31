#!/bin/bash
set -euo pipefail

ROOT="/Users/xiong/weather"
TARGET_LABEL="com.weather.reported_trajectory_no"
MARKET_LABEL="com.weather.market_ws"
ORDER_LABEL="com.weather.clob_order_service"
UID_VALUE="$(id -u)"
DOMAIN="gui/${UID_VALUE}"
INSTALLED_PLIST="${HOME}/Library/LaunchAgents/com.weather.reported_trajectory_no.plist"
PAUSE="$ROOT/data/live_paused.flag"
CONFIRM="${1:-}"

if [ "$CONFIRM" != "--confirm-prepared-supersession-production-no-live" ]; then
  echo "用法：$0 --confirm-prepared-supersession-production-no-live" >&2
  exit 2
fi
if [ "$(id -u)" -eq 0 ]; then
  echo "必须以登录用户运行，禁止 root/sudo" >&2
  exit 1
fi
if [ ! -f "$INSTALLED_PLIST" ]; then
  echo "prepared_supersession_installed_plist_missing" >&2
  exit 1
fi
for service_label in "$TARGET_LABEL" "$MARKET_LABEL" "$ORDER_LABEL"; do
  if launchctl print "system/${service_label}" >/dev/null 2>&1; then
    echo "prepared_supersession_system_domain_residue:${service_label}" >&2
    exit 1
  fi
done

service_pid() {
  launchctl print "${DOMAIN}/$1" 2>/dev/null \
    | /usr/bin/awk '/^[[:space:]]*pid = [0-9]+/{print $3; exit}'
}

market_pid_before="$(service_pid "$MARKET_LABEL")"
order_pid_before="$(service_pid "$ORDER_LABEL")"
target_pid_before="$(service_pid "$TARGET_LABEL")"
if [ -z "$market_pid_before" ] || [ -z "$order_pid_before" ] \
  || [ -z "$target_pid_before" ]; then
  echo "prepared_supersession_requires_three_running_user_services" >&2
  exit 1
fi

read -r old_cycle_id baseline_supersession_count \
  baseline_order_event_count baseline_execution_receipt_count <<EOF
$(/usr/bin/python3 - "$ROOT/data/bayes_basket_target_ssot.db" <<'PY'
import sqlite3
import sys
with sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True) as conn:
    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    old = conn.execute(
        "SELECT c.cycle_id FROM basket_target_execution_cycles c "
        "JOIN auto_trade_decisions d ON d.cycle_id=c.cycle_id "
        "WHERE c.station_id='RKPK' AND c.event_date='2026-07-29' "
        "AND c.state='prepared' AND d.decided_state='prepared' "
        "ORDER BY c.created_at DESC,c.rowid DESC LIMIT 1"
    ).fetchone()
    if old is None:
        raise SystemExit("prepared_supersession_old_cycle_missing")
    supersessions = (
        conn.execute(
            "SELECT COUNT(*) FROM "
            "basket_target_prepared_supersession_receipts"
        ).fetchone()[0]
        if "basket_target_prepared_supersession_receipts" in tables
        else 0
    )
    events = conn.execute(
        "SELECT COUNT(*) FROM basket_target_order_events"
    ).fetchone()[0]
    receipts = (
        conn.execute(
            "SELECT COUNT(*) FROM basket_target_execution_receipts"
        ).fetchone()[0]
        if "basket_target_execution_receipts" in tables
        else 0
    )
    print(str(old[0]), int(supersessions), int(events), int(receipts))
PY
)
EOF

mkdir -p "$ROOT/data"
touch "$PAUSE"
target_stopped=0

restore_target() {
  touch "$PAUSE"
  if [ "$target_stopped" = "1" ] \
    && ! launchctl print "${DOMAIN}/$TARGET_LABEL" >/dev/null 2>&1; then
    launchctl bootstrap "$DOMAIN" "$INSTALLED_PLIST" >/dev/null 2>&1 || true
  fi
}
trap restore_target ERR

launchctl bootout "${DOMAIN}/$TARGET_LABEL"
target_stopped=1
deadline=$((SECONDS + 30))
while [ "$SECONDS" -lt "$deadline" ]; do
  if ! launchctl print "${DOMAIN}/$TARGET_LABEL" >/dev/null 2>&1; then
    break
  fi
  sleep 0.25
done
if launchctl print "${DOMAIN}/$TARGET_LABEL" >/dev/null 2>&1; then
  echo "prepared_supersession_target_did_not_stop" >&2
  exit 1
fi

/usr/bin/python3 \
  "$ROOT/scripts/apply_bayes_basket_target_prepared_supersession_schema.py" \
  --db "$ROOT/data/bayes_basket_target_ssot.db" \
  --apply \
  --confirm-reported-trajectory-no-stopped

# Mandatory idempotency foldback while the sole writer remains stopped.
replay="$(
  /usr/bin/python3 \
    "$ROOT/scripts/apply_bayes_basket_target_prepared_supersession_schema.py" \
    --db "$ROOT/data/bayes_basket_target_ssot.db" \
    --apply \
    --confirm-reported-trajectory-no-stopped
)"
/usr/bin/python3 - "$replay" <<'PY'
import json
import sys
value = json.loads(sys.argv[1])
if value.get("state") != "unchanged":
    raise SystemExit("prepared_supersession_migration_not_idempotent")
if value.get("quick_check") != "ok":
    raise SystemExit("prepared_supersession_migration_replay_quick_check_failed")
PY

launchctl bootstrap "$DOMAIN" "$INSTALLED_PLIST"
target_stopped=0
deadline=$((SECONDS + 30))
target_pid_after=""
while [ "$SECONDS" -lt "$deadline" ]; do
  target_pid_after="$(service_pid "$TARGET_LABEL")"
  if [ -n "$target_pid_after" ] \
    && [ "$target_pid_after" != "$target_pid_before" ]; then
    break
  fi
  sleep 0.25
done
if [ -z "$target_pid_after" ] \
  || [ "$target_pid_after" = "$target_pid_before" ]; then
  echo "prepared_supersession_target_pid_did_not_advance" >&2
  exit 1
fi
if [ "$(service_pid "$MARKET_LABEL")" != "$market_pid_before" ]; then
  echo "prepared_supersession_market_ws_pid_changed" >&2
  exit 1
fi
if [ "$(service_pid "$ORDER_LABEL")" != "$order_pid_before" ]; then
  echo "prepared_supersession_order_service_pid_changed" >&2
  exit 1
fi
if [ ! -f "$PAUSE" ]; then
  echo "prepared_supersession_live_pause_missing" >&2
  exit 1
fi

# Wait for the resident consumer to observe a fresh P1/WS scope and perform
# the first real atomic replacement.  The wait is bounded and never submits
# an order; failure keeps live paused and exits fail closed.
deadline=$((SECONDS + 45))
supersession_observed=0
while [ "$SECONDS" -lt "$deadline" ]; do
  current_count="$(
    /usr/bin/sqlite3 -readonly "$ROOT/data/bayes_basket_target_ssot.db" \
      "SELECT count(*) FROM basket_target_prepared_supersession_receipts;"
  )"
  if [ "$current_count" -gt "$baseline_supersession_count" ]; then
    supersession_observed=1
    break
  fi
  sleep 0.5
done
if [ "$supersession_observed" != "1" ]; then
  echo "prepared_supersession_receipt_not_materialized" >&2
  exit 1
fi

/usr/bin/python3 - \
  "$ROOT/data/bayes_basket_target_ssot.db" \
  "$old_cycle_id" \
  "$baseline_order_event_count" \
  "$baseline_execution_receipt_count" <<'PY'
import json
import math
import sqlite3
import sys

database, old_cycle_id, baseline_events, baseline_receipts = sys.argv[1:]
with sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=30) as conn:
    conn.row_factory = sqlite3.Row
    receipt = conn.execute(
        "SELECT * FROM basket_target_prepared_supersession_receipts "
        "WHERE old_cycle_id=?",
        (old_cycle_id,),
    ).fetchone()
    if receipt is None:
        raise SystemExit("prepared_supersession_expected_receipt_missing")
    old_cycle = conn.execute(
        "SELECT state FROM basket_target_execution_cycles WHERE cycle_id=?",
        (old_cycle_id,),
    ).fetchone()
    old_intents = conn.execute(
        "SELECT state FROM basket_target_order_intents WHERE cycle_id=?",
        (old_cycle_id,),
    ).fetchall()
    old_reservation = conn.execute(
        "SELECT state,release_reason FROM "
        "basket_target_capital_reservations WHERE cycle_id=?",
        (old_cycle_id,),
    ).fetchone()
    if old_cycle is None or str(old_cycle[0]) != "settled":
        raise SystemExit("prepared_supersession_old_cycle_not_settled")
    if not old_intents or any(str(row[0]) != "cancelled" for row in old_intents):
        raise SystemExit("prepared_supersession_old_intent_not_cancelled")
    if (
        old_reservation is None
        or str(old_reservation[0]) != "released"
        or str(old_reservation[1]) != "authority_input_superseded"
    ):
        raise SystemExit("prepared_supersession_old_reservation_not_released")

    new_cycle_id = str(receipt["new_cycle_id"])
    new_decision_id = str(receipt["new_decision_id"])
    new_cycle = conn.execute(
        "SELECT state FROM basket_target_execution_cycles WHERE cycle_id=?",
        (new_cycle_id,),
    ).fetchone()
    new_decision = conn.execute(
        "SELECT decided_state,will_order FROM auto_trade_decisions "
        "WHERE decision_id=? AND cycle_id=?",
        (new_decision_id, new_cycle_id),
    ).fetchone()
    new_fact = conn.execute(
        "SELECT fact_id,decision_scope_key FROM basket_policy_training_fact "
        "WHERE cycle_id=? AND decision_id=?",
        (new_cycle_id, new_decision_id),
    ).fetchone()
    new_reservation = conn.execute(
        "SELECT amount_usdc,state FROM basket_target_capital_reservations "
        "WHERE cycle_id=?",
        (new_cycle_id,),
    ).fetchone()
    pool = conn.execute(
        "SELECT balance_usdc,reserved_usdc,version "
        "FROM basket_target_capital_pool WHERE pool_id=1"
    ).fetchone()
    if new_cycle is None or str(new_cycle[0]) != "prepared":
        raise SystemExit("prepared_supersession_new_cycle_not_prepared")
    if (
        new_decision is None
        or str(new_decision[0]) not in {"prepared", "blocked"}
        or int(new_decision[1]) != 0
    ):
        raise SystemExit("prepared_supersession_new_decision_invalid")
    if (
        new_fact is None
        or str(new_fact[1]) != str(receipt["new_decision_scope_key"])
    ):
        raise SystemExit("prepared_supersession_new_training_fact_mismatch")
    if new_reservation is None or str(new_reservation[1]) != "active":
        raise SystemExit("prepared_supersession_new_reservation_invalid")
    if not math.isclose(
        float(pool[1]), float(new_reservation[0]), rel_tol=0.0, abs_tol=1e-8
    ):
        raise SystemExit("prepared_supersession_pool_reservation_mismatch")

    events = conn.execute(
        "SELECT COUNT(*) FROM basket_target_order_events"
    ).fetchone()[0]
    receipts = conn.execute(
        "SELECT COUNT(*) FROM basket_target_execution_receipts"
    ).fetchone()[0]
    if int(events) != int(baseline_events):
        raise SystemExit("prepared_supersession_order_event_count_changed")
    if int(receipts) != int(baseline_receipts):
        raise SystemExit("prepared_supersession_execution_receipt_count_changed")
    print(json.dumps({
        "schema": "basket_target_prepared_supersession_cutover_acceptance_v1",
        "old_cycle_id": old_cycle_id,
        "new_cycle_id": new_cycle_id,
        "new_decision_id": new_decision_id,
        "new_training_fact_id": str(new_fact[0]),
        "new_decision_state": str(new_decision[0]),
        "released_amount_usdc": float(receipt["released_amount_usdc"]),
        "new_reserved_amount_usdc": float(new_reservation[0]),
        "capital_pool_version": int(pool[2]),
        "order_event_count": int(events),
        "execution_receipt_count": int(receipts),
        "calls_order_gateway": False,
        "places_orders": False,
        "live_submit_authorized": False,
        "orders_submitted": 0,
        "state": "PASS",
    }, sort_keys=True))
PY

trap - ERR
echo "prepared_supersession_no_live_ok"
echo "reported_trajectory_no_pid=$target_pid_before->$target_pid_after"
echo "market_ws_pid=$market_pid_before"
echo "clob_order_service_pid=$order_pid_before"
echo "live_pause_retained=true"
