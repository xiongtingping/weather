#!/bin/bash
set -euo pipefail

ROOT="/Users/xiong/weather"
DB="$ROOT/data/bayes_basket_target_ssot.db"
MANIFEST="$ROOT/config/trading/delta_basket_score_v2_rkpk_no_live_bootstrap_v3.json"
STATION_MANIFEST="$ROOT/config/trading/bayes_basket_target_target_v2_station_policy_no_live_v2.json"
PY="$ROOT/.venv/bin/python"
TARGET_LABEL="com.weather.reported_trajectory_no"
MARKET_LABEL="com.weather.market_ws"
ORDER_LABEL="com.weather.clob_order_service"
TARGET_PLIST="/Library/LaunchDaemons/${TARGET_LABEL}.plist"
LIVE_PAUSE="$ROOT/data/live_paused.flag"
STAMP="$(/bin/date -u +%Y%m%dT%H%M%SZ)"
RECEIPT="$ROOT/output/audits/delta_v2_official_tick_grid_no_live_${STAMP}.json"
CONFIRM="${1:-}"

if [ "$CONFIRM" != "--confirm-delta-v2-official-tick-grid-no-live" ]; then
  echo "用法：sudo $0 --confirm-delta-v2-official-tick-grid-no-live" >&2
  exit 2
fi
if [ "$(/usr/bin/id -u)" -ne 0 ]; then
  echo "必须以 root 运行：sudo $0 --confirm-delta-v2-official-tick-grid-no-live" >&2
  exit 1
fi
for required in "$DB" "$MANIFEST" "$STATION_MANIFEST" "$PY" "$TARGET_PLIST"; do
  if [ ! -f "$required" ]; then
    echo "delta_v2_tick_grid_required_path_missing=$required" >&2
    exit 1
  fi
done

service_pid() {
  /bin/launchctl print "system/$1" 2>/dev/null \
    | /usr/bin/awk '/^[[:space:]]*pid = [0-9]+/{print $3; exit}'
}

market_pid_before="$(service_pid "$MARKET_LABEL")"
order_pid_before="$(service_pid "$ORDER_LABEL")"
target_pid_before="$(service_pid "$TARGET_LABEL")"
if [ -z "$market_pid_before" ] || [ -z "$order_pid_before" ] \
  || [ -z "$target_pid_before" ]; then
  echo "delta_v2_tick_grid_requires_three_running_system_services" >&2
  exit 1
fi

"$PY" - "$DB" "$MANIFEST" "$LIVE_PAUSE" <<'PY'
import json
import shutil
import sqlite3
import sys
from pathlib import Path

database, manifest_path, pause_path = (Path(value) for value in sys.argv[1:])
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
if (
    manifest.get("authority_scope") != "PRODUCTION_NO_LIVE"
    or manifest.get("decision") != "NO_LIVE_ACTIVATED"
    or manifest.get("activation_generation") != 2
    or manifest.get("fill_price_tick_size") != 0.001
    or manifest.get("oos_gate_passed") is not False
    or manifest.get("live_submit_authorized") is not False
    or manifest.get("calls_order_gateway") is not False
    or int(manifest.get("orders_submitted", -1)) != 0
):
    raise SystemExit("delta_v2_tick_grid_manifest_boundary_invalid")
if not pause_path.is_file():
    raise SystemExit("delta_v2_tick_grid_live_pause_missing")
free = shutil.disk_usage(database.parent).free
database_bytes = database.stat().st_size
if free < max(2 * 1024**3, database_bytes * 3):
    raise SystemExit("delta_v2_tick_grid_free_space_too_low")
with sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=30) as conn:
    if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise SystemExit("delta_v2_tick_grid_preflight_quick_check_failed")
PY

/usr/bin/touch "$LIVE_PAUSE"
target_stopped=0

restore_target_on_error() {
  /usr/bin/touch "$LIVE_PAUSE"
  if [ "$target_stopped" = "1" ] \
    && ! /bin/launchctl print "system/$TARGET_LABEL" >/dev/null 2>&1; then
    /bin/launchctl bootstrap system "$TARGET_PLIST" >/dev/null 2>&1 || true
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
  echo "delta_v2_tick_grid_target_did_not_stop" >&2
  exit 1
fi

apply_receipt="$(
  "$PY" "$ROOT/scripts/apply_delta_basket_score_v2_no_live_bootstrap.py" \
    --db "$DB" \
    --manifest "$MANIFEST" \
    --apply \
    --confirm-reported-trajectory-no-stopped
)"
replay_receipt="$(
  "$PY" "$ROOT/scripts/apply_delta_basket_score_v2_no_live_bootstrap.py" \
    --db "$DB" \
    --manifest "$MANIFEST" \
    --apply \
    --confirm-reported-trajectory-no-stopped
)"
station_receipt="$(
  "$PY" "$ROOT/scripts/apply_bayes_basket_target_target_v2_station_policy_no_live.py" \
    --db "$DB" \
    --manifest "$STATION_MANIFEST" \
    --apply \
    --confirm-reported-trajectory-no-stopped TARGET_V2_STATION_POLICY_NO_LIVE
)"
station_replay_receipt="$(
  "$PY" "$ROOT/scripts/apply_bayes_basket_target_target_v2_station_policy_no_live.py" \
    --db "$DB" \
    --manifest "$STATION_MANIFEST" \
    --apply \
    --confirm-reported-trajectory-no-stopped TARGET_V2_STATION_POLICY_NO_LIVE
)"

"$PY" - \
  "$ROOT" "$DB" "$MANIFEST" "$STATION_MANIFEST" \
  "$apply_receipt" "$replay_receipt" \
  "$station_receipt" "$station_replay_receipt" \
  "$RECEIPT" "$target_pid_before" "$market_pid_before" "$order_pid_before" <<'PY'
from datetime import datetime, timedelta
import json
import sqlite3
import sys
from pathlib import Path

root = Path(sys.argv[1])
database = Path(sys.argv[2])
manifest_path = Path(sys.argv[3])
station_manifest_path = Path(sys.argv[4])
apply_receipt = json.loads(sys.argv[5])
replay_receipt = json.loads(sys.argv[6])
station_receipt = json.loads(sys.argv[7])
station_replay_receipt = json.loads(sys.argv[8])
receipt_path = Path(sys.argv[9])
target_pid_before, market_pid_before, order_pid_before = sys.argv[10:13]
sys.path.insert(0, str(root))

from bayes_basket_target.delta_v2_artifact import resolve_delta_v2_policy
from bayes_basket_target.delta_v2_fill_artifact import (
    resolve_delta_v2_fill_distribution,
)
from bayes_basket_target.resource_bounds import write_bounded_json
from bayes_basket_target.production_io import (
    compile_production_cycle_from_ssot,
)

manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
station_manifest = json.loads(
    station_manifest_path.read_text(encoding="utf-8")
)
if apply_receipt.get("state") not in {"COMMITTED", "UNCHANGED_ZERO_WRITE"}:
    raise SystemExit("delta_v2_tick_grid_apply_state_invalid")
if (
    replay_receipt.get("state") != "UNCHANGED_ZERO_WRITE"
    or int(replay_receipt.get("rows_written", -1)) != 0
):
    raise SystemExit("delta_v2_tick_grid_replay_not_idempotent")
if station_receipt.get("state") not in {
    "COMMITTED",
    "UNCHANGED_ZERO_WRITE",
}:
    raise SystemExit("delta_v2_tick_grid_station_apply_state_invalid")
if (
    station_replay_receipt.get("state") != "UNCHANGED_ZERO_WRITE"
    or int(station_replay_receipt.get("rows_written", -1)) != 0
):
    raise SystemExit("delta_v2_tick_grid_station_replay_not_idempotent")
if any(
    receipt.get("live_submit_authorized") is not False
    or receipt.get("calls_order_gateway") is not False
    or int(receipt.get("orders_submitted", -1)) != 0
    for receipt in (
        apply_receipt,
        replay_receipt,
        station_receipt,
        station_replay_receipt,
    )
):
    raise SystemExit("delta_v2_tick_grid_boundary_open")

with sqlite3.connect(database, timeout=30) as conn:
    conn.execute("PRAGMA foreign_keys=ON")
    mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    if mode != "wal":
        raise SystemExit("delta_v2_tick_grid_journal_mode_not_wal")
    if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise SystemExit("delta_v2_tick_grid_quick_check_failed")
    if conn.execute("PRAGMA foreign_key_check").fetchall():
        raise SystemExit("delta_v2_tick_grid_foreign_key_check_failed")
    resolved_ids = []
    for maturity in manifest["maturity_bins"]:
        policy = resolve_delta_v2_policy(
            conn,
            station_id="RKPK",
            stage="P1",
            season_bin="summer",
            maturity_bin=str(maturity),
            liquidity_bin="liquid",
            strategy_version="target-v2",
            evaluation_at_utc="2026-07-29T01:16:00Z",
            required_authority_scope="PRODUCTION_NO_LIVE",
        )
        expected = f"delta-v2-policy-rkpk-summer-liquid-v3-{maturity}"
        if policy.artifact_id != expected:
            raise SystemExit(
                f"delta_v2_tick_grid_policy_resolution_invalid:{policy.artifact_id}"
            )
        resolved_ids.append(policy.artifact_id)
        fill = resolve_delta_v2_fill_distribution(
            conn,
            station_id="RKPK",
            stage="P1",
            season_bin="summer",
            maturity_bin=str(maturity),
            liquidity_bin="liquid",
            bucket="40°C",
            price=0.004,
            timeout_seconds=60.0,
            queue_ahead_shares=97.95,
            spread=0.003,
            depth=565.0,
            quote_version="official-ws-cutover-proof",
            evaluation_at_utc="2026-07-29T01:16:00Z",
            required_authority_scope="PRODUCTION_NO_LIVE",
            policy_fill_distribution_artifact_sha256=(
                policy.fill_distribution_artifact_sha256
            ),
        )
        if not fill.artifact_id.endswith("-p004of1000"):
            raise SystemExit("delta_v2_tick_grid_exact_fill_resolution_invalid")
    old_active = conn.execute(
        "SELECT count(*) FROM basket_delta_v2_policy_artifact a "
        "WHERE a.artifact_id LIKE 'delta-v2-policy-rkpk-summer-liquid-v2-%' "
        "AND (SELECT p.decision FROM basket_delta_v2_policy_promotion p "
        "WHERE p.artifact_id=a.artifact_id "
        "ORDER BY p.decision_seq DESC LIMIT 1)<>'SUPERSEDED'"
    ).fetchone()[0]
    if int(old_active) != 0:
        raise SystemExit("delta_v2_tick_grid_old_policy_not_superseded")
    observed_at = conn.execute(
        "SELECT observed_at FROM basket_official_ws_evidence_cache "
        "WHERE station_id='RKPK' AND event_date='2026-07-29'"
    ).fetchone()
    if observed_at is None:
        raise SystemExit("delta_v2_tick_grid_ws_scope_missing")
    evaluation_at = (
        datetime.fromisoformat(
            str(observed_at[0]).replace("Z", "+00:00")
        )
        + timedelta(seconds=1)
    ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    compiled = compile_production_cycle_from_ssot(
        conn,
        station_id="RKPK",
        event_date="2026-07-29",
        evaluation_at_utc=evaluation_at,
        season_bin="summer",
        maturity_bin="d1",
        liquidity_bin="liquid",
        strategy_version="target-v2",
        policy_authority_scope="PRODUCTION_NO_LIVE_CANDIDATE",
    )
    selected_rows = (
        (compiled.get("delta_v2_audit") or {})
        .get("selected_actions", [])
    )
    selected_actions = [
        dict(row.get("action") or {})
        for row in selected_rows
        if isinstance(row, dict) and isinstance(row.get("action"), dict)
    ]
    sizing = [
        row
        for row in compiled.get("order_sizing_decisions", [])
        if row.get("candidate_present") is True
    ]
    if (
        compiled.get("status") != "ready_no_live"
        or compiled.get("reason_codes") != []
        or compiled.get("cadence_allows_candidate") is not True
        or compiled.get("cadence_decision") != "ALLOW_CANDIDATE"
        or compiled.get("cadence_authority")
        != "DELTA_BASKET_SCORE_V2"
        or not sizing
        or {
            str(row.get("bucket") or "") for row in sizing
        }
        != set(
            str(value)
            for value in (compiled.get("plan") or {}).get(
                "execution_gap_support", []
            )
        )
        or {
            str(row.get("bucket") or "") for row in sizing
        }
        != {
            str(row.get("bucket") or "") for row in selected_actions
        }
        or any(
            row.get("cash_template_caps_target") is not False
            or row.get("eligible") is not True
            or float(row.get("c_order", 0.0)) <= 0.0
            for row in sizing
        )
        or abs(
            sum(float(row.get("cash_clip", 0.0)) for row in selected_actions)
            - sum(float(row.get("c_order", 0.0)) for row in sizing)
        )
        > (
            sum(
                float(row.get("r_limit", 0.0))
                * float(row.get("share_quantum", 0.0))
                for row in sizing
            )
            + 1e-9
        )
    ):
        raise SystemExit("delta_v2_tick_grid_vertical_compile_not_ready")

write_bounded_json(
    receipt_path,
    {
        "schema": "delta_v2_official_tick_grid_cutover_receipt_v1",
        "state": "MIGRATED_PENDING_TARGET_RESTART",
        "database": str(database),
        "manifest": str(manifest_path),
        "station_manifest": str(station_manifest_path),
        "apply": apply_receipt,
        "replay": replay_receipt,
        "station_apply": station_receipt,
        "station_replay": station_replay_receipt,
        "resolved_policy_ids": resolved_ids,
        "exact_price_proof": 0.004,
        "vertical_compile": {
            "status": compiled["status"],
            "reason_codes": compiled["reason_codes"],
            "cadence_decision": compiled["cadence_decision"],
            "cadence_authority": compiled["cadence_authority"],
            "selected_actions": selected_actions,
            "sizing": sizing,
        },
        "target_pid_before": int(target_pid_before),
        "market_pid_before": int(market_pid_before),
        "order_pid_before": int(order_pid_before),
        "journal_mode": "wal",
        "quick_check": "ok",
        "live_pause_retained": True,
        "execution_mode": "production_no_live",
        "live_submit_authorized": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    },
)
print(str(receipt_path))
PY

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
  echo "delta_v2_tick_grid_target_pid_did_not_advance" >&2
  exit 1
fi

market_pid_after="$(service_pid "$MARKET_LABEL")"
order_pid_after="$(service_pid "$ORDER_LABEL")"
if [ "$market_pid_after" != "$market_pid_before" ] \
  || [ "$order_pid_after" != "$order_pid_before" ]; then
  echo "delta_v2_tick_grid_protected_service_pid_changed" >&2
  exit 1
fi
if [ ! -f "$LIVE_PAUSE" ]; then
  echo "delta_v2_tick_grid_live_pause_lost" >&2
  exit 1
fi

"$PY" - "$ROOT" "$RECEIPT" "$target_pid_after" <<'PY'
import json
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from bayes_basket_target.resource_bounds import write_bounded_json

path = Path(sys.argv[2])
value = json.loads(path.read_text(encoding="utf-8"))
value["state"] = "MIGRATED_AND_TARGET_RESTARTED"
value["target_pid_after"] = int(sys.argv[3])
write_bounded_json(path, value)
PY

trap - ERR
echo "delta_v2_official_tick_grid_no_live_ok"
echo "target_pid=$target_pid_before->$target_pid_after"
echo "market_ws_pid=$market_pid_after"
echo "clob_order_service_pid=$order_pid_after"
echo "live_pause_retained=true"
echo "receipt=$RECEIPT"
