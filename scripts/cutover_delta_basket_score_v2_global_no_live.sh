#!/bin/bash
set -euo pipefail

ROOT="/Users/xiong/weather"
MANIFEST="$ROOT/config/trading/delta_basket_score_v2_global_no_live_bootstrap_v1.json"
DATABASE="$ROOT/data/bayes_basket_target_ssot.db"
PAUSE="$ROOT/data/live_paused.flag"
TARGET_LABEL="com.weather.reported_trajectory_no"
MARKET_LABEL="com.weather.market_ws"
ORDER_LABEL="com.weather.clob_order_service"
TARGET_PLIST="/Library/LaunchDaemons/${TARGET_LABEL}.plist"
CONFIRM="${1:-}"
MAX_DATABASE_BYTES=$((256 * 1024 * 1024))
MAX_GROWTH_BYTES=$((32 * 1024 * 1024))
MIN_FREE_BYTES=$((1024 * 1024 * 1024))

if [ "$CONFIRM" != "--confirm-global-delta-v2-production-no-live" ]; then
  echo "用法：sudo $0 --confirm-global-delta-v2-production-no-live" >&2
  exit 2
fi
if [ "$(id -u)" -ne 0 ]; then
  echo "必须以 root 运行：sudo $0 --confirm-global-delta-v2-production-no-live" >&2
  exit 1
fi
if [ ! -f "$DATABASE" ] || [ ! -f "$MANIFEST" ] || [ ! -f "$TARGET_PLIST" ]; then
  echo "delta_v2_global_cutover_required_file_missing" >&2
  exit 1
fi

service_pid() {
  launchctl print "system/$1" 2>/dev/null \
    | /usr/bin/awk '/^[[:space:]]*pid = [0-9]+/{print $3; exit}'
}

target_pid_before="$(service_pid "$TARGET_LABEL")"
market_pid_before="$(service_pid "$MARKET_LABEL")"
order_pid_before="$(service_pid "$ORDER_LABEL")"
if [ -z "$target_pid_before" ] || [ -z "$market_pid_before" ] \
  || [ -z "$order_pid_before" ]; then
  echo "delta_v2_global_cutover_requires_three_running_services" >&2
  exit 1
fi

/usr/bin/python3 - "$TARGET_PLIST" "$MANIFEST" "$DATABASE" <<'PY'
import json
import os
import plistlib
import sqlite3
import sys
from pathlib import Path

plist_path, manifest_path, database = map(Path, sys.argv[1:])
plist = plistlib.loads(plist_path.read_bytes())
environment = plist.get("EnvironmentVariables") or {}
if environment.get("PM_TARGET_PRODUCTION_WRITER") != "1":
    raise SystemExit("delta_v2_global_cutover_writer_not_enabled")
if environment.get("PM_TARGET_EXECUTION_MODE") != "production_no_live":
    raise SystemExit("delta_v2_global_cutover_execution_mode_invalid")
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
if (
    manifest.get("station_id") != "*"
    or manifest.get("authority_scope") != "PRODUCTION_NO_LIVE"
    or manifest.get("oos_gate_passed") is not False
    or manifest.get("live_submit_authorized") is not False
    or manifest.get("calls_order_gateway") is not False
    or int(manifest.get("orders_submitted", -1)) != 0
):
    raise SystemExit("delta_v2_global_cutover_manifest_boundary_invalid")
free = os.statvfs(database.parent)
if free.f_bavail * free.f_frsize < 1024 * 1024 * 1024:
    raise SystemExit("delta_v2_global_cutover_free_space_too_low")
with sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=30) as conn:
    if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise SystemExit("delta_v2_global_cutover_database_check_failed")
PY

database_bytes_before="$(stat -f %z "$DATABASE")"
order_events_before="$(
  /usr/bin/sqlite3 -readonly "$DATABASE" \
    "SELECT count(*) FROM basket_target_order_events;"
)"
touch "$PAUSE"
target_stopped=0

restore_target() {
  touch "$PAUSE"
  if [ "$target_stopped" = "1" ] \
    && ! launchctl print "system/$TARGET_LABEL" >/dev/null 2>&1; then
    launchctl bootstrap system "$TARGET_PLIST" >/dev/null 2>&1 || true
  fi
}
trap restore_target ERR

launchctl bootout "system/$TARGET_LABEL"
target_stopped=1
deadline=$((SECONDS + 30))
while [ "$SECONDS" -lt "$deadline" ]; do
  if ! launchctl print "system/$TARGET_LABEL" >/dev/null 2>&1; then
    break
  fi
  sleep 0.25
done
if launchctl print "system/$TARGET_LABEL" >/dev/null 2>&1; then
  echo "delta_v2_global_cutover_target_did_not_stop" >&2
  exit 1
fi

/usr/bin/python3 \
  "$ROOT/scripts/apply_delta_basket_score_v2_no_live_bootstrap.py" \
  --db "$DATABASE" \
  --manifest "$MANIFEST" \
  --apply \
  --confirm-reported-trajectory-no-stopped

database_bytes_after="$(stat -f %z "$DATABASE")"
if [ "$database_bytes_after" -gt "$MAX_DATABASE_BYTES" ] \
  || [ $((database_bytes_after - database_bytes_before)) -gt "$MAX_GROWTH_BYTES" ]; then
  echo "delta_v2_global_cutover_database_budget_exceeded" >&2
  exit 1
fi

launchctl bootstrap system "$TARGET_PLIST"
target_stopped=0
deadline=$((SECONDS + 30))
target_pid_after=""
while [ "$SECONDS" -lt "$deadline" ]; do
  target_pid_after="$(service_pid "$TARGET_LABEL")"
  if [ -n "$target_pid_after" ] && [ "$target_pid_after" != "$target_pid_before" ]; then
    break
  fi
  sleep 0.25
done
if [ -z "$target_pid_after" ] || [ "$target_pid_after" = "$target_pid_before" ]; then
  echo "delta_v2_global_cutover_target_pid_did_not_advance" >&2
  exit 1
fi

market_pid_after="$(service_pid "$MARKET_LABEL")"
order_pid_after="$(service_pid "$ORDER_LABEL")"
if [ "$market_pid_after" != "$market_pid_before" ] \
  || [ "$order_pid_after" != "$order_pid_before" ]; then
  echo "delta_v2_global_cutover_non_target_pid_changed" >&2
  exit 1
fi
if [ ! -f "$PAUSE" ]; then
  echo "delta_v2_global_cutover_live_pause_missing" >&2
  exit 1
fi

/usr/bin/python3 - "$ROOT" "$DATABASE" "$order_events_before" <<'PY'
import sqlite3
import sys

root, database, order_events_before = sys.argv[1:]
sys.path.insert(0, root)
from bayes_basket_target.delta_v2_artifact import resolve_delta_v2_policy

with sqlite3.connect(database, timeout=30) as conn:
    conn.execute("PRAGMA busy_timeout=30000")
    if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise SystemExit("delta_v2_global_cutover_quick_check_failed")
    rows = conn.execute(
        "SELECT maturity_bin,count(*) "
        "FROM basket_delta_v2_policy_activation "
        "WHERE station_id='*' AND stage='*' "
        "AND strategy_version='target-v2' "
        "AND authority_scope='PRODUCTION_NO_LIVE' "
        "GROUP BY maturity_bin ORDER BY maturity_bin"
    ).fetchall()
    if rows != [("d1", 1), ("d2", 1), ("d2_fallback", 1)]:
        raise SystemExit("delta_v2_global_cutover_policy_scope_invalid")
    oos_claims = conn.execute(
        "SELECT count(*) FROM basket_delta_v2_policy_artifact "
        "WHERE station_id='*' AND oos_gate_passed<>0"
    ).fetchone()[0]
    if int(oos_claims) != 0:
        raise SystemExit("delta_v2_global_cutover_false_oos_claim")
    for station in ("EDDM", "EPWA"):
        policy = resolve_delta_v2_policy(
            conn,
            station_id=station,
            stage="P0-M",
            season_bin="summer",
            maturity_bin="d2",
            liquidity_bin="liquid",
            strategy_version="target-v2",
            evaluation_at_utc="2026-07-29T16:00:00Z",
        )
        if policy.station_id != "*" or policy.oos_gate_passed:
            raise SystemExit(
                "delta_v2_global_cutover_fallback_resolution_invalid"
            )
    rkpk = resolve_delta_v2_policy(
        conn,
        station_id="RKPK",
        stage="P1",
        season_bin="summer",
        maturity_bin="d2",
        liquidity_bin="liquid",
        strategy_version="target-v2",
        evaluation_at_utc="2026-07-29T16:00:00Z",
    )
    if rkpk.station_id != "RKPK":
        raise SystemExit(
            "delta_v2_global_cutover_exact_station_precedence_lost"
        )
    order_events_after = conn.execute(
        "SELECT count(*) FROM basket_target_order_events"
    ).fetchone()[0]
    if int(order_events_after) != int(order_events_before):
        raise SystemExit("delta_v2_global_cutover_order_event_drift")
PY

# Registry metadata and future P0 publication have an independent cadence.
# The restarted target service must publish and ingest a bounded multi-station
# catalog without waiting for an NWP release or settlement.
registry_deadline=$((SECONDS + 75))
registry_ready=0
while [ "$SECONDS" -lt "$registry_deadline" ]; do
  if /usr/bin/python3 - \
    "$ROOT/data/runtime/bayes_basket_target_nwp/market_registry_forward.json" \
    "$DATABASE" <<'PY'
import json
import sqlite3
import sys
from pathlib import Path

snapshot_path = Path(sys.argv[1])
database = sys.argv[2]
if not snapshot_path.is_file() or snapshot_path.stat().st_size > 16 * 1024 * 1024:
    raise SystemExit(1)
value = json.loads(snapshot_path.read_text(encoding="utf-8"))
filters = value.get("filters") or {}
if (
    filters.get("current_catalog_only") is not True
    or filters.get("include_not_started_for_p0") is not True
    or int(value.get("row_count") or 0) <= 0
    or int(value.get("row_count") or 0) > 1980
):
    raise SystemExit(1)
snapshot_sha256 = str(value.get("snapshot_sha256") or "")
with sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=10) as conn:
    row = conn.execute(
        "SELECT row_count FROM basket_target_market_registry_snapshot "
        "WHERE snapshot_sha256=?",
        (snapshot_sha256,),
    ).fetchone()
    if row is None or int(row[0]) != int(value["row_count"]):
        raise SystemExit(1)
    stations = conn.execute(
        "SELECT count(DISTINCT station_id) "
        "FROM basket_target_market_registry "
        "WHERE snapshot_sha256=? AND source_eligible=1",
        (snapshot_sha256,),
    ).fetchone()[0]
    if int(stations) < 18:
        raise SystemExit(1)
PY
  then
    registry_ready=1
    break
  fi
  sleep 1
done
if [ "$registry_ready" -ne 1 ]; then
  echo "delta_v2_global_cutover_registry_decoupling_not_ready" >&2
  exit 1
fi
database_bytes_after="$(stat -f %z "$DATABASE")"
if [ "$database_bytes_after" -gt "$MAX_DATABASE_BYTES" ] \
  || [ $((database_bytes_after - database_bytes_before)) -gt "$MAX_GROWTH_BYTES" ]; then
  echo "delta_v2_global_cutover_final_database_budget_exceeded" >&2
  exit 1
fi

trap - ERR
echo "delta_basket_score_v2_global_no_live_ok"
echo "reported_trajectory_no_pid=$target_pid_before->$target_pid_after"
echo "market_ws_pid=$market_pid_after"
echo "clob_order_service_pid=$order_pid_after"
echo "database_bytes=$database_bytes_before->$database_bytes_after"
echo "registry_export_decoupled_from_nwp=true"
echo "future_preselection_waits_for_settlement=false"
echo "forward_oos_blocks_production_no_live=false"
echo "forward_oos_blocks_live_or_parameter_promotion=true"
echo "live_pause_retained=true"
