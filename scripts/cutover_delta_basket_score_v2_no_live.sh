#!/bin/bash
set -euo pipefail

ROOT="/Users/xiong/weather"
MANIFEST="$ROOT/config/trading/delta_basket_score_v2_rkpk_no_live_bootstrap_v2.json"
LABEL="com.weather.reported_trajectory_no"
MARKET_LABEL="com.weather.market_ws"
ORDER_LABEL="com.weather.clob_order_service"
UID_VALUE="$(id -u)"
DOMAIN="gui/${UID_VALUE}"
INSTALLED_PLIST="${HOME}/Library/LaunchAgents/com.weather.reported_trajectory_no.plist"
PAUSE="$ROOT/data/live_paused.flag"
CONFIRM="${1:-}"

if [ "$CONFIRM" != "--confirm-delta-basket-score-v2-production-no-live" ]; then
  echo "用法：$0 --confirm-delta-basket-score-v2-production-no-live" >&2
  exit 2
fi
if [ "$(id -u)" -eq 0 ]; then
  echo "必须以登录用户运行，禁止 root/sudo" >&2
  exit 1
fi
if [ ! -f "$INSTALLED_PLIST" ]; then
  echo "delta_v2_cutover_installed_plist_missing" >&2
  exit 1
fi
for service_label in "$LABEL" "$MARKET_LABEL" "$ORDER_LABEL"; do
  if launchctl print "system/${service_label}" >/dev/null 2>&1; then
    echo "delta_v2_cutover_system_domain_residue:${service_label}" >&2
    exit 1
  fi
done

service_pid() {
  launchctl print "${DOMAIN}/$1" 2>/dev/null \
    | /usr/bin/awk '/^[[:space:]]*pid = [0-9]+/{print $3; exit}'
}

market_pid_before="$(service_pid "$MARKET_LABEL")"
order_pid_before="$(service_pid "$ORDER_LABEL")"
target_pid_before="$(service_pid "$LABEL")"
if [ -z "$market_pid_before" ] || [ -z "$order_pid_before" ] \
  || [ -z "$target_pid_before" ]; then
  echo "delta_v2_cutover_requires_three_running_user_services" >&2
  exit 1
fi

/usr/bin/python3 - "$INSTALLED_PLIST" <<'PY'
import plistlib
import sys
from pathlib import Path

value = plistlib.loads(Path(sys.argv[1]).read_bytes())
env = value.get("EnvironmentVariables") or {}
if env.get("PM_TARGET_PRODUCTION_WRITER") != "1":
    raise SystemExit("delta_v2_cutover_target_writer_not_enabled")
if env.get("PM_TARGET_EXECUTION_MODE") != "production_no_live":
    raise SystemExit("delta_v2_cutover_execution_mode_invalid")
if env.get("WEATHER_SERVICE_DOMAIN") != "user":
    raise SystemExit("delta_v2_cutover_service_domain_invalid")
PY

/usr/bin/python3 - \
  "$ROOT/data/bayes_basket_target_ssot.db" \
  "$MANIFEST" <<'PY'
import json
import sqlite3
import sys
from pathlib import Path

database, manifest_path = sys.argv[1:]
manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
with sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=30) as conn:
    row = conn.execute(
        "SELECT policy_version_vector_json "
        "FROM basket_policy_training_fact WHERE station_id='RKPK' "
        "ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
if row is None:
    raise SystemExit("delta_v2_cutover_rkpk_scope_evidence_missing")
scope = json.loads(row[0])
if scope.get("maturity_bin") not in manifest.get("maturity_bins", []):
    raise SystemExit("delta_v2_cutover_current_maturity_not_covered")
if scope.get("liquidity_bin") != manifest.get("liquidity_bin"):
    raise SystemExit("delta_v2_cutover_current_liquidity_not_covered")
if scope.get("strategy_version") != manifest.get("strategy_version"):
    raise SystemExit("delta_v2_cutover_current_strategy_not_covered")
PY

baseline_decision_rowid="$(
  /usr/bin/sqlite3 -readonly "$ROOT/data/bayes_basket_target_ssot.db" \
  "SELECT coalesce(max(rowid),0) FROM auto_trade_decisions;"
)"
baseline_fact_rowid="$(
  /usr/bin/sqlite3 -readonly "$ROOT/data/bayes_basket_target_ssot.db" \
  "SELECT coalesce(max(rowid),0) FROM basket_policy_training_fact;"
)"
baseline_order_event_count="$(
  /usr/bin/sqlite3 -readonly "$ROOT/data/bayes_basket_target_ssot.db" \
  "SELECT count(*) FROM basket_target_order_events;"
)"

mkdir -p "$ROOT/data"
touch "$PAUSE"
target_stopped=0

restore_target_service() {
  touch "$PAUSE"
  market_pid_on_error="$(service_pid "$MARKET_LABEL")"
  order_pid_on_error="$(service_pid "$ORDER_LABEL")"
  if [ "$market_pid_on_error" != "$market_pid_before" ]; then
    echo "delta_v2_cutover_error_path_market_pid_changed" >&2
  fi
  if [ "$order_pid_on_error" != "$order_pid_before" ]; then
    echo "delta_v2_cutover_error_path_order_pid_changed" >&2
  fi
  if [ "$target_stopped" = "1" ] \
    && ! launchctl print "${DOMAIN}/$LABEL" >/dev/null 2>&1; then
    launchctl bootstrap "$DOMAIN" "$INSTALLED_PLIST" >/dev/null 2>&1 || true
  fi
}
trap restore_target_service ERR

launchctl bootout "${DOMAIN}/$LABEL"
target_stopped=1
deadline=$((SECONDS + 30))
while [ "$SECONDS" -lt "$deadline" ]; do
  if ! launchctl print "${DOMAIN}/$LABEL" >/dev/null 2>&1; then
    break
  fi
  sleep 0.25
done
if launchctl print "${DOMAIN}/$LABEL" >/dev/null 2>&1; then
  echo "delta_v2_cutover_target_did_not_stop" >&2
  exit 1
fi

/usr/bin/python3 "$ROOT/scripts/apply_bayes_basket_target_delta_v2_schema.py" \
  --db "$ROOT/data/bayes_basket_target_ssot.db" \
  --apply \
  --confirm-reported-trajectory-no-stopped

/usr/bin/python3 \
  "$ROOT/scripts/apply_delta_basket_score_v2_no_live_bootstrap.py" \
  --db "$ROOT/data/bayes_basket_target_ssot.db" \
  --manifest "$MANIFEST" \
  --apply \
  --confirm-reported-trajectory-no-stopped

/usr/bin/python3 \
  "$ROOT/scripts/repair_bayes_basket_target_false_quarantine.py" \
  --db "$ROOT/data/bayes_basket_target_ssot.db" \
  --store "$ROOT/data/runtime/bayes_basket_target_cash_phase/forward_observations.sqlite3" \
  --apply \
  --confirm-reported-trajectory-no-stopped

# A second execution is a mandatory idempotency foldback: it must discover no
# unresolved allowlisted identity and perform zero writes.
repair_replay="$(
  /usr/bin/python3 \
    "$ROOT/scripts/repair_bayes_basket_target_false_quarantine.py" \
    --db "$ROOT/data/bayes_basket_target_ssot.db" \
    --store "$ROOT/data/runtime/bayes_basket_target_cash_phase/forward_observations.sqlite3" \
    --apply \
    --confirm-reported-trajectory-no-stopped
)"
/usr/bin/python3 - "$repair_replay" <<'PY'
import json
import sys

value = json.loads(sys.argv[1])
if value.get("state") != "already_current":
    raise SystemExit("delta_v2_cutover_forward_repair_not_idempotent")
if int(value.get("candidate_count") or 0) != 0:
    raise SystemExit("delta_v2_cutover_forward_repair_replayed_write")
if value.get("calls_order_gateway") is not False:
    raise SystemExit("delta_v2_cutover_forward_repair_gateway_boundary_open")
PY

/usr/bin/python3 - "$ROOT" <<'PY'
import sqlite3
import sys

sys.path.insert(0, sys.argv[1])
from bayes_basket_target.forward_observation_store import (
    DEFAULT_STORE_PATH,
    DEFAULT_STORE_ROOT,
    read_forward_store_state,
)

state = read_forward_store_state(
    DEFAULT_STORE_PATH,
    allowed_root=DEFAULT_STORE_ROOT,
)
if int(state["last_source_rowid"]) != 630:
    raise SystemExit("delta_v2_cutover_forward_cursor_drift")
if int(state["observation_count"]) != 2:
    raise SystemExit("delta_v2_cutover_forward_observation_count_invalid")
if int(state["quarantine_count"]) != 0:
    raise SystemExit("delta_v2_cutover_forward_quarantine_unresolved")
if int(state["historical_quarantine_count"]) != 2:
    raise SystemExit("delta_v2_cutover_forward_quarantine_history_lost")
if int(state["repaired_quarantine_count"]) != 2:
    raise SystemExit("delta_v2_cutover_forward_repair_count_invalid")
with sqlite3.connect(DEFAULT_STORE_PATH) as connection:
    rows = connection.execute(
        "SELECT s.source_rowid,s.error_code,r.source_decision_sha256,"
        "r.observation_sha256 FROM forward_ingest_state s "
        "JOIN forward_ingest_repair r ON r.decision_id=s.decision_id "
        "ORDER BY s.source_rowid"
    ).fetchall()
if [int(row[0]) for row in rows] != [629, 630]:
    raise SystemExit("delta_v2_cutover_forward_repair_scope_invalid")
if any(
    str(row[1]) != "cash_phase_forward_binding_scope_or_clock_mismatch"
    or len(str(row[2])) != 64
    or len(str(row[3])) != 64
    for row in rows
):
    raise SystemExit("delta_v2_cutover_forward_repair_lineage_invalid")
print("forward_false_quarantine_repair_verified=2")
PY

launchctl bootstrap "$DOMAIN" "$INSTALLED_PLIST"
target_stopped=0
deadline=$((SECONDS + 30))
target_pid_after=""
while [ "$SECONDS" -lt "$deadline" ]; do
  target_pid_after="$(service_pid "$LABEL")"
  if [ -n "$target_pid_after" ] \
    && [ "$target_pid_after" != "$target_pid_before" ]; then
    break
  fi
  sleep 0.25
done
if [ -z "$target_pid_after" ] \
  || [ "$target_pid_after" = "$target_pid_before" ]; then
  echo "delta_v2_cutover_target_pid_did_not_advance" >&2
  exit 1
fi

market_pid_after="$(service_pid "$MARKET_LABEL")"
order_pid_after="$(service_pid "$ORDER_LABEL")"
if [ "$market_pid_after" != "$market_pid_before" ] \
  || [ "$order_pid_after" != "$order_pid_before" ]; then
  echo "delta_v2_cutover_non_target_pid_changed" >&2
  exit 1
fi
if [ ! -f "$PAUSE" ]; then
  echo "delta_v2_cutover_live_pause_missing" >&2
  exit 1
fi

/usr/bin/python3 - "$ROOT/data/bayes_basket_target_ssot.db" "$MANIFEST" <<'PY'
import json
import sqlite3
import sys

manifest = json.loads(open(sys.argv[2], encoding="utf-8").read())
with sqlite3.connect(sys.argv[1], timeout=30) as conn:
    conn.execute("PRAGMA busy_timeout=30000")
    if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise SystemExit("delta_v2_cutover_quick_check_failed")
    policies = conn.execute(
        "SELECT a.maturity_bin,p.decision,x.oos_gate_passed "
        "FROM basket_delta_v2_policy_activation a "
        "JOIN basket_delta_v2_policy_promotion p "
        "ON p.artifact_id=a.artifact_id "
        "JOIN basket_delta_v2_policy_artifact x "
        "ON x.artifact_id=a.artifact_id "
        "WHERE a.station_id='RKPK' AND a.authority_scope='PRODUCTION_NO_LIVE' "
        "ORDER BY a.maturity_bin"
    ).fetchall()
    if policies != [
        ("d1", "NO_LIVE_ACTIVATED", 0),
        ("d2_fallback", "NO_LIVE_ACTIVATED", 0),
    ]:
        raise SystemExit("delta_v2_cutover_policy_authority_invalid")
    fill = conn.execute(
        "SELECT count(*) FROM basket_delta_v2_fill_artifact a "
        "JOIN basket_delta_v2_fill_promotion p "
        "ON p.artifact_id=a.artifact_id "
        "WHERE a.station_id='RKPK' "
        "AND a.authority_scope='PRODUCTION_NO_LIVE' "
        "AND p.decision='NO_LIVE_ACTIVATED' "
        "AND a.oos_gate_passed=0"
    ).fetchone()[0]
    expected_fill = (
        len(manifest["maturity_bins"]) * len(manifest["price_grid"])
    )
    if int(fill) < expected_fill:
        raise SystemExit("delta_v2_cutover_fill_authority_invalid")
    vector_columns = {
        str(row[1])
        for row in conn.execute(
            "PRAGMA table_info(basket_target_execution_version_vectors)"
        )
    }
    required_vector_columns = {
        "economic_policy_schema",
        "economic_policy_artifact_sha256",
    }
    if not required_vector_columns.issubset(vector_columns):
        raise SystemExit("delta_v2_cutover_target_ledger_v2_columns_missing")
    recompute_columns = {
        str(row[1])
        for row in conn.execute(
            "PRAGMA table_info(basket_target_post_fill_recompute_requests)"
        )
    }
    if not required_vector_columns.issubset(recompute_columns):
        raise SystemExit(
            "delta_v2_cutover_post_fill_v2_columns_missing"
        )
PY

/usr/bin/python3 \
  "$ROOT/scripts/accept_delta_basket_score_v2_no_live.py" \
  --db "$ROOT/data/bayes_basket_target_ssot.db" \
  --baseline-decision-rowid "$baseline_decision_rowid" \
  --baseline-fact-rowid "$baseline_fact_rowid" \
  --baseline-order-event-count "$baseline_order_event_count" \
  --timeout-seconds 45 \
  --allow-waiting-input \
  --allow-already-current

/usr/bin/python3 - \
  "$ROOT" \
  "$ROOT/data/bayes_basket_target_ssot.db" <<'PY'
from datetime import datetime, timezone
import sqlite3
import sys

sys.path.insert(0, sys.argv[1])
from bayes_basket_target.settlement_time import settlement_day_window

database = sys.argv[2]
now = datetime.now(timezone.utc)
verified = 0
with sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=30) as conn:
    conn.execute("PRAGMA query_only=ON")
    scopes = conn.execute(
        "SELECT DISTINCT station_id,event_date "
        "FROM basket_policy_training_fact "
        "ORDER BY event_date,station_id LIMIT 17"
    ).fetchall()
    if len(scopes) > 16:
        raise SystemExit("delta_v2_cutover_truth_scope_cap_exceeded")
    for station_id, event_date in scopes:
        registry = conn.execute(
            "SELECT settlement_timezone FROM basket_target_market_registry "
            "WHERE station_id=? AND event_date=? AND source_eligible=1 "
            "ORDER BY refreshed_at DESC,snapshot_sha256 DESC LIMIT 1",
            (station_id, event_date),
        ).fetchone()
        if registry is None:
            raise SystemExit(
                "delta_v2_cutover_truth_registry_scope_missing"
            )
        window = settlement_day_window(
            str(event_date), str(registry[0])
        )
        closed_at = datetime.fromisoformat(
            str(window["target_day_end_utc"]).replace("Z", "+00:00")
        ).astimezone(timezone.utc)
        if now < closed_at:
            continue
        truth = conn.execute(
            "SELECT truth_id FROM calibration_station_daily_high_truth "
            "WHERE station_id=? AND local_date=? AND qc_status='ACCEPTED' "
            "AND canonical_committed_at_utc>=? "
            "ORDER BY revision_no DESC,truth_id DESC LIMIT 1",
            (
                station_id,
                event_date,
                str(window["target_day_end_utc"]),
            ),
        ).fetchone()
        if truth is None:
            raise SystemExit(
                "delta_v2_cutover_closed_scope_truth_missing:"
                f"{station_id}:{event_date}"
            )
        verified += 1
print(f"closed_truth_scopes_verified={verified}")
PY

trap - ERR
echo "delta_basket_score_v2_no_live_ok"
echo "reported_trajectory_no_pid=$target_pid_before->$target_pid_after"
echo "market_ws_pid=$market_pid_after"
echo "clob_order_service_pid=$order_pid_after"
echo "forward_oos_blocks_production_no_live=false"
echo "forward_oos_blocks_live_or_parameter_promotion=true"
echo "live_pause_retained=true"
