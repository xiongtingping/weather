#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.weather.reported_trajectory_no"
SERVICE="system/${LABEL}"
PLIST="/Library/LaunchDaemons/${LABEL}.plist"
ARCHIVE="$ROOT/data/archive.db"
TARGET="$ROOT/data/bayes_basket_target_ssot.db"
OBSERVATION_ID="rtobs_b213c31b511dc53a727a195e5b45b54e"

if [ "$(id -u)" -ne 0 ]; then
  echo "必须以root运行：sudo $0 --confirm-rkpk-reference-only" >&2
  exit 1
fi
if [ "${1:-}" != "--confirm-rkpk-reference-only" ]; then
  echo "缺少显式确认：--confirm-rkpk-reference-only" >&2
  exit 1
fi

SYSTEM_USER="${WEATHER_SYSTEM_USER:-${SUDO_USER:-xiong}}"
PYTHON="/Library/Frameworks/Python.framework/Versions/3.13/bin/python3"
if [ ! -x "$PYTHON" ]; then
  PYTHON="/usr/bin/python3"
fi
if [ ! -f "$PLIST" ]; then
  echo "system plist不存在：$PLIST" >&2
  exit 1
fi

pid_for() {
  /bin/launchctl print "system/$1" 2>/dev/null \
    | /usr/bin/awk '/^[[:space:]]*pid = / {print $3; exit}'
}

reported_before="$(pid_for "$LABEL")"
market_before="$(pid_for com.weather.market_ws)"
clob_before="$(pid_for com.weather.clob_order_service)"
if [ -z "$reported_before" ] || [ -z "$market_before" ] || [ -z "$clob_before" ]; then
  echo "三服务preflight PID不完整" >&2
  exit 1
fi

/usr/bin/sudo -u "$SYSTEM_USER" "$PYTHON" - "$ROOT" "$TARGET" "$ARCHIVE" "$OBSERVATION_ID" <<'PY'
import json
import sqlite3
import sys
from pathlib import Path
root, target_path, archive_path, observation_id = sys.argv[1:]
target = sqlite3.connect(f"file:{target_path}?mode=ro", uri=True)
archive = sqlite3.connect(f"file:{archive_path}?mode=ro", uri=True)
activation = json.load(open(
    Path(root) / "config/trading/"
    "bayes_basket_target_production_no_live_activation_v1.json",
    encoding="utf-8",
))
for key in (
    "live_decision_write_authorized", "live_order_authorized",
    "live_submit_authorized", "places_orders", "writes_production_db",
    "calls_order_gateway", "network_access",
):
    if activation.get(key) is not False:
        raise SystemExit(f"activation_zero_authorization_failed:{key}")
if activation.get("execution_mode") != "production_no_live":
    raise SystemExit("activation_not_production_no_live")
obs = archive.execute(
    "SELECT station_id,source_lane,report_type,valid_at,"
    "reported_temperature_c,raw_sha256,correction_suspected "
    "FROM reported_trajectory_observations WHERE observation_id=?",
    (observation_id,),
).fetchone()
expected = (
    "RKPK", "awc", "SPECI", "2026-07-28T03:15:00.000Z",
    34, "60a395770dca0e6883a8966fe66d7003e75080287524f74dc8ce211d019a38ac",
    1,
)
if obs != expected:
    raise SystemExit(f"rkpk_correction_observation_drift:{obs!r}")
for table in (
    "basket_target_order_intents",
    "basket_target_order_events",
    "basket_target_order_reconciliations",
):
    if target.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] != 0:
        raise SystemExit(f"target_zero_order_preflight_failed:{table}")
print("rkpk_correction_cutover_preflight_ok")
PY

reported_stopped=0
restart_reported() {
  /bin/launchctl enable "$SERVICE" >/dev/null 2>&1 || true
  if ! /bin/launchctl print "$SERVICE" >/dev/null 2>&1; then
    /bin/launchctl bootstrap system "$PLIST" >/dev/null 2>&1 || true
  fi
  /bin/launchctl kickstart -k "$SERVICE" >/dev/null 2>&1 || true
}
on_error() {
  rc=$?
  if [ "$reported_stopped" -eq 1 ]; then
    restart_reported
  fi
  echo "rkpk_correction_review_cutover_failed rc=$rc" >&2
  exit "$rc"
}
trap on_error ERR INT TERM

/bin/launchctl disable "$SERVICE"
/bin/launchctl bootout "$SERVICE"
reported_stopped=1

/usr/bin/sudo -u "$SYSTEM_USER" "$PYTHON" \
  "$ROOT/scripts/apply_reported_trajectory_correction_review_schema.py" \
  --db "$ARCHIVE" \
  --apply \
  --confirm-reported-trajectory-no-stopped

/usr/bin/sudo -u "$SYSTEM_USER" "$PYTHON" \
  "$ROOT/scripts/review_reported_trajectory_station_halt.py" \
  --db "$ARCHIVE" \
  --source-lane awc \
  --station-id RKPK \
  --event-date 2026-07-28 \
  --triggering-observation-id "$OBSERVATION_ID" \
  --decision CLEAR_REFERENCE_ONLY \
  --reviewer "$SYSTEM_USER" \
  --rationale \
  "03:15Z COR仅修订非整点SPECI；03:00Z与04:00Z正式整点METAR均确认35℃，COR保持reference-only。" \
  --confirm-review

restart_reported
reported_stopped=0
trap - ERR INT TERM

reported_after="$(pid_for "$LABEL")"
market_after="$(pid_for com.weather.market_ws)"
clob_after="$(pid_for com.weather.clob_order_service)"
if [ -z "$reported_after" ] || [ "$reported_after" = "$reported_before" ]; then
  echo "reported_trajectory_no未加载新PID" >&2
  exit 1
fi
if [ "$market_after" != "$market_before" ]; then
  echo "market_ws PID发生非授权变化" >&2
  exit 1
fi
if [ "$clob_after" != "$clob_before" ]; then
  echo "clob_order_service PID发生非授权变化" >&2
  exit 1
fi

/usr/bin/sudo -u "$SYSTEM_USER" "$PYTHON" \
  "$ROOT/scripts/reported_trajectory_no_production_acceptance.py"

/usr/bin/sudo -u "$SYSTEM_USER" "$PYTHON" - "$TARGET" "$ARCHIVE" <<'PY'
import json
import sqlite3
import sys
target_path, archive_path = sys.argv[1:]
target = sqlite3.connect(f"file:{target_path}?mode=ro", uri=True)
archive = sqlite3.connect(f"file:{archive_path}?mode=ro", uri=True)
review = archive.execute(
    "SELECT review_id,decision,content_sha256 "
    "FROM reported_trajectory_station_halt_reviews "
    "WHERE station_id='RKPK' AND event_date='2026-07-28' "
    "ORDER BY reviewed_at DESC,review_id DESC LIMIT 1"
).fetchone()
counts = {
    table: target.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    for table in (
        "basket_target_order_intents",
        "basket_target_order_events",
        "basket_target_order_reconciliations",
    )
}
if review is None or review[1] != "CLEAR_REFERENCE_ONLY":
    raise SystemExit("rkpk_review_receipt_missing")
if any(counts.values()):
    raise SystemExit(f"target_zero_order_postflight_failed:{counts}")
print(json.dumps({
    "state": "rkpk_correction_review_cutover_ok",
    "review_id": review[0],
    "review_content_sha256": review[2],
    "zero_order_counts": counts,
    "calls_order_gateway": False,
    "live_submit_authorized": False,
    "orders_submitted": 0,
}, sort_keys=True))
PY

echo "rkpk_correction_review_cutover_ok"
