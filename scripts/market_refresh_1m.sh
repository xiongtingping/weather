#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ -f "$ROOT/scripts/lib/load_launchctl_env.sh" ]; then
  . "$ROOT/scripts/lib/load_launchctl_env.sh"
  load_repo_env_file "$ROOT/.env"
fi

: "${PM_NOFILE_SOFT:=8192}"
if [[ "$PM_NOFILE_SOFT" =~ ^[0-9]+$ ]]; then
  ulimit -Sn "$PM_NOFILE_SOFT" 2>/dev/null || true
fi

. "$ROOT/scripts/lib/run_with_timeout.sh"
. "$ROOT/scripts/lib/process_governance.sh"
. "$ROOT/scripts/lib/resolve_project_python.sh"
. "$ROOT/scripts/lib/weather_refresh_coordinator.sh"
if ! weather_refresh_register_entrypoint "market_refresh" "market" "${PM_MARKET_REFRESH_LANE_NAME:-${PM_REFRESH_COORDINATOR_LANE_NAME:-weather_refresh_lane}}" "$ROOT/data/market_refresh.log" "market_refresh"; then
  exit 0
fi

PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
  PY="$(resolve_project_python "$ROOT" || true)"
fi
if [ -z "${PY:-}" ] || [ ! -x "$PY" ]; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') [market_refresh] missing project python runtime" >> "$ROOT/data/market_refresh.log"
  exit 1
fi
if weather_refresh_skip_if_live_ssot "market_refresh" "$ROOT/data/market_refresh.log" "market_refresh"; then
  exit 0
fi
MARKET_REFRESH_NICE="$(normalize_nice_level "${PM_MARKET_REFRESH_1M_NICE:-${PM_MARKET_REFRESH_NICE:-10}}" 10)"
: "${PM_ARCHIVE_WRITE_SCOPE:=chengdu_zuuu}"
export PM_ARCHIVE_WRITE_SCOPE

_facts_materialize_window_from_scan() {
  local run_date="${1:-}"
  local days_arg="${2:-}"
  local include_yesterday="${3:-0}"
  "$PY" - "$ROOT/data/archive.db" "$run_date" "$days_arg" "$include_yesterday" <<'PY'
import sqlite3
import sys
from datetime import date, timedelta

db_path, run_date_raw, days_arg_raw, include_yesterday_raw = sys.argv[1:5]
run_date_text = str(run_date_raw or "").strip().split("T", 1)[0]
try:
    run_day = date.fromisoformat(run_date_text)
except Exception:
    print("")
    print("1")
    raise SystemExit(0)

include_yesterday = str(include_yesterday_raw or "").strip().lower() in {"1", "true", "yes", "on"}
days_ahead = 0
parts = str(days_arg_raw or "").strip().split()
if len(parts) >= 2:
    try:
        days_ahead = int(parts[1])
    except Exception:
        days_ahead = 0

end_day = run_day + timedelta(days=max(0, days_ahead))
if days_ahead < 0:
    conn = None
    try:
        conn = sqlite3.connect(db_path, timeout=1.0)
        cur = conn.cursor()
        row = cur.execute(
            """
            SELECT MAX(event_date)
            FROM (
              SELECT event_date FROM model_snapshots WHERE run_date = ?
              UNION ALL
              SELECT event_date FROM market_snapshots WHERE run_date = ?
              UNION ALL
              SELECT event_date FROM event_runs WHERE run_date = ?
            )
            """,
            (run_date_text, run_date_text, run_date_text),
        ).fetchone()
        candidate_text = str((row[0] if row else "") or "").strip().split("T", 1)[0]
        if candidate_text:
            candidate_day = date.fromisoformat(candidate_text)
            if candidate_day > end_day:
                end_day = candidate_day
    except Exception:
        pass
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass

start_day = run_day - timedelta(days=1) if include_yesterday else run_day
if end_day < start_day:
    end_day = start_day
days = max(1, (end_day - start_day).days + 1)
print(end_day.isoformat())
print(days)
PY
}

WEATHER_FAST_DISCOVERY_RAN=0
_run_weather_fast_discovery_once() {
  if [ "${WEATHER_FAST_DISCOVERY_RAN}" = "1" ]; then
    return 0
  fi
  WEATHER_FAST_DISCOVERY_RAN=1
  if [ "${PM_WEATHER_FAST_DISCOVERY_ENABLED:-1}" != "1" ]; then
    return 0
  fi
  local live_reason=""
  live_reason="$(weather_refresh_live_ssot_critical_reason "market_refresh_discovery" 2>/dev/null || true)"
  if [ -n "${live_reason:-}" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [market_refresh] weather_discovery_fast skipped live=${live_reason}" >> "$ROOT/data/market_refresh.log"
    return 0
  fi
  local discovery_lane_acquired_here=0
  if [ "${PM_WEATHER_DISCOVERY_COORDINATOR_LANE_ENABLED:-1}" = "1" ] \
    && [ "${WEATHER_SCAN_LANE_LOCK_ACQUIRED:-0}" != "1" ]; then
    if ! weather_scan_lane_acquire "weather_discovery" "${PM_WEATHER_DISCOVERY_LANE_WAIT_SEC:-0}" "$ROOT/data/market_refresh.log" "market_refresh"; then
      echo "$(date '+%Y-%m-%d %H:%M:%S') [market_refresh] weather_discovery_fast skipped scan lane busy" >> "$ROOT/data/market_refresh.log"
      return 0
    fi
    discovery_lane_acquired_here=1
  fi
  local discovery_db_lock=0
  if [ "${WEATHER_REFRESH_DB_WRITER_LOCK_ACQUIRED:-0}" != "1" ]; then
    if ! weather_refresh_db_writer_acquire "market_refresh_discovery" "${PM_WEATHER_DISCOVERY_DB_LOCK_WAIT_SEC:-0}" "$ROOT/data/market_refresh.log" "market_refresh"; then
      echo "$(date '+%Y-%m-%d %H:%M:%S') [market_refresh] weather_discovery_fast skipped db_writer busy" >> "$ROOT/data/market_refresh.log"
      if [ "$discovery_lane_acquired_here" = "1" ]; then
        weather_scan_lane_release
      fi
      return 0
    fi
    discovery_db_lock=1
  fi
  local discovery_days="${PM_WEATHER_DISCOVERY_DAYS_AHEAD:-${PM_SCAN_FAST_DAYS_AHEAD:-2}}"
  if ! [[ "${discovery_days}" =~ ^-?[0-9]+$ ]] || [ "${discovery_days}" -lt 0 ]; then
    discovery_days="2"
  fi
  local discovery_timeout
  discovery_timeout="$(normalize_positive_int_max "${PM_WEATHER_DISCOVERY_TIMEOUT_SEC:-12}" 12 18)"
  mkdir -p "$ROOT/data"
  echo "---- $(date '+%Y-%m-%d %H:%M:%S') weather_discovery_fast start (days=${discovery_days} timeout=${discovery_timeout}s) ----" >> "$ROOT/data/market_refresh.log"
  set +e
  run_with_timeout "$discovery_timeout" "$ROOT/data/market_refresh.log" \
    nice -n "$MARKET_REFRESH_NICE" \
    env \
      PM_WEATHER_DISCOVERY_GAMMA_MAX_PAGES="${PM_WEATHER_DISCOVERY_GAMMA_MAX_PAGES:-4}" \
      PM_WEATHER_DISCOVERY_GAMMA_TIMEOUT="${PM_WEATHER_DISCOVERY_GAMMA_TIMEOUT:-4}" \
      PM_WEATHER_DISCOVERY_MAX_EVENTS="${PM_WEATHER_DISCOVERY_MAX_EVENTS:-80}" \
      PM_WEATHER_DISCOVERY_MAX_DETAILS="${PM_WEATHER_DISCOVERY_MAX_DETAILS:-0}" \
      PYTHONUNBUFFERED=1 \
    "$PY" "$ROOT/scripts/discover_weather_markets_fast.py" \
      --days-ahead "$discovery_days"
  local discovery_rc="$?"
  set -e
  if [ "$discovery_rc" -ne 0 ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [market_refresh] weather_discovery_fast exit=${discovery_rc}, continue market refresh" >> "$ROOT/data/market_refresh.log"
  fi
  if [ "$discovery_db_lock" = "1" ]; then
    weather_refresh_db_writer_release
  fi
  if [ "$discovery_lane_acquired_here" = "1" ]; then
    weather_scan_lane_release
  fi
  return 0
}

_kick_model_scan_if_new_market_signal() {
  local discovery_signal="${PM_WEATHER_DISCOVERY_SIGNAL_PATH:-$ROOT/data/weather_discovery_new_market_signal.json}"
  if [ ! -f "$discovery_signal" ]; then
    return 0
  fi
  local signal_recent
  signal_recent="$("$PY" - "$discovery_signal" "${PM_WEATHER_DISCOVERY_SIGNAL_MAX_AGE_SEC:-180}" <<'PY' 2>/dev/null || true
import os
import sys
import time

path = sys.argv[1]
try:
    max_age = float(sys.argv[2])
except Exception:
    max_age = 180.0
try:
    age = time.time() - os.path.getmtime(path)
except Exception:
    raise SystemExit(0)
if age >= 0 and age <= max_age:
    print("1")
PY
  )"
  if [ "${signal_recent:-0}" = "1" ]; then
    if [ "${PM_WEATHER_DISCOVERY_KICK_MODEL_SCAN:-0}" != "1" ]; then
      echo "$(date '+%Y-%m-%d %H:%M:%S') [market_refresh] new weather market signal; model_scan kick disabled, coordinator owns scan cadence" >> "$ROOT/data/market_refresh.log"
      return 0
    fi
    local live_reason
    live_reason="$(weather_refresh_live_ssot_critical_reason "market_refresh_kick_model_scan" 2>/dev/null || true)"
    if [ -n "${live_reason:-}" ]; then
      echo "$(date '+%Y-%m-%d %H:%M:%S') [market_refresh] new weather market signal; skip model_scan kick live=${live_reason}" >> "$ROOT/data/market_refresh.log"
      return 0
    fi
    local model_label="gui/$(id -u)/com.weather.model_scan"
    if launchctl print "$model_label" 2>/dev/null | grep -q "state = running"; then
      echo "$(date '+%Y-%m-%d %H:%M:%S') [market_refresh] new weather market signal; model_scan already running, skip destructive kick ${model_label}" >> "$ROOT/data/market_refresh.log"
    else
      echo "$(date '+%Y-%m-%d %H:%M:%S') [market_refresh] new weather market signal; kick ${model_label}" >> "$ROOT/data/market_refresh.log"
      launchctl kickstart "$model_label" >> "$ROOT/data/market_refresh.log" 2>&1 || true
    fi
  fi
  return 0
}

# Auto-trade running: do NOT stop market refresh. Instead, focus refresh on Top OK candidates
# (priority-only mode) to keep quotes fresh for execution with minimal fan-out.
AUTO_LOCK_DIR="$ROOT/data/auto_trade_runtime.lock"
AUTO_LOCK_META="$AUTO_LOCK_DIR/meta.json"
: "${PM_AUTO_TRADE_RUNTIME_LOCK_STALE_SEC:=1800}"
AUTO_TRADE_ACTIVE=0
_auto_trade_worker_active() {
  auto_trade_worker_process_active \
    "${PM_AUTO_TRADE_WORKER_ACTIVE_MAX_SEC:-900}" \
    "$ROOT/data/market_refresh.log" \
    "market_refresh"
}
_auto_trade_entry_active() {
  pgrep -f "scripts/auto_trade_entry_1m.sh" >/dev/null 2>&1 \
    || pgrep -f "scan_weather_market.py .*--auto-trade-enqueue" >/dev/null 2>&1 \
    || pgrep -f "weather_strategy_batch.py .*--enqueue" >/dev/null 2>&1
}
_queued_entry_job_count() {
  local db_path="$ROOT/data/archive.db"
  if [ ! -f "$db_path" ]; then
    echo "0"
    return 0
  fi
  local count=""
  count="$(sqlite3 "$db_path" \
    "PRAGMA busy_timeout=100; SELECT COALESCE(COUNT(*),0) FROM auto_trade_jobs WHERE source='entry_1m' AND state='queued';" \
    2>/dev/null | tail -n 1 | tr -dc '0-9')"
  if [ -z "${count:-}" ]; then
    count="0"
  fi
  echo "$count"
}
_recent_market_tick_stale_decision_count() {
  local db_path="$ROOT/data/archive.db"
  if [ ! -f "$db_path" ]; then
    echo "0"
    return 0
  fi
  local window_min="${PM_MARKET_REFRESH_DECISION_STALE_BYPASS_WINDOW_MIN:-15}"
  if ! [[ "${window_min}" =~ ^[0-9]+$ ]] || [ "${window_min}" -lt 1 ]; then
    window_min="15"
  fi
  local run_date="${PM_MARKET_REFRESH_RUN_DATE:-}"
  if [ -z "${run_date:-}" ]; then
    run_date="$(TZ=America/New_York date '+%Y-%m-%d')"
  fi
  if ! [[ "${run_date}" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
    run_date="$(TZ=America/New_York date '+%Y-%m-%d')"
  fi
  local cutoff=""
  cutoff="$("$PY" - "$window_min" <<'PY' 2>/dev/null || true
import sys
from datetime import datetime, timedelta, timezone

try:
    minutes = int(sys.argv[1])
except Exception:
    minutes = 15
dt = datetime.now(timezone.utc) - timedelta(minutes=max(1, minutes))
print(dt.isoformat(timespec="seconds"))
PY
)"
  if [ -z "${cutoff:-}" ]; then
    cutoff="$(date -u '+%Y-%m-%dT%H:%M:%S+00:00')"
  fi
  local recent_limit="${PM_MARKET_REFRESH_DECISION_RECENT_ID_LIMIT:-5000}"
  if ! [[ "${recent_limit}" =~ ^[0-9]+$ ]] || [ "${recent_limit}" -lt 500 ]; then
    recent_limit="5000"
  fi
  local count=""
  count="$(sqlite3 "$db_path" "
    PRAGMA busy_timeout=100;
    WITH recent AS (
      SELECT slug, event_date, reason_codes_json, details_json, created_at
      FROM auto_trade_decisions
      WHERE id >= COALESCE((SELECT MAX(id) - ${recent_limit} FROM auto_trade_decisions), 0)
        AND slug IS NOT NULL AND slug != ''
        AND action = 'entry'
        AND event_date >= date('${run_date}')
        AND created_at >= '${cutoff}'
    )
    SELECT COALESCE(COUNT(DISTINCT slug),0)
    FROM recent
    WHERE slug IS NOT NULL AND slug != ''
      AND reason_codes_json LIKE '%market_tick_stale%'
    ;
  " 2>/dev/null | tail -n 1 | tr -dc '0-9')"
  if [ -z "${count:-}" ]; then
    count="0"
  fi
  echo "$count"
}
_current_actionable_tick_stale_count() {
  local db_path="$ROOT/data/archive.db"
  if [ ! -f "$db_path" ]; then
    echo "0"
    return 0
  fi
  local run_date="${PM_MARKET_REFRESH_RUN_DATE:-}"
  if [ -z "${run_date:-}" ]; then
    run_date="$(TZ=America/New_York date '+%Y-%m-%d')"
  fi
  if ! [[ "${run_date}" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
    run_date="$(TZ=America/New_York date '+%Y-%m-%d')"
  fi
  local threshold="${PM_MARKET_REFRESH_ACTIONABLE_STALE_SEC:-600}"
  if ! [[ "${threshold}" =~ ^[0-9]+$ ]] || [ "${threshold}" -lt 60 ]; then
    threshold="600"
  fi
  local cutoff=""
  cutoff="$("$PY" - "$threshold" <<'PY' 2>/dev/null || true
import sys
from datetime import datetime, timedelta, timezone

try:
    seconds = int(sys.argv[1])
except Exception:
    seconds = 600
dt = datetime.now(timezone.utc) - timedelta(seconds=max(60, seconds))
print(dt.isoformat(timespec="seconds"))
PY
)"
  if [ -z "${cutoff:-}" ]; then
    cutoff="$(date -u '+%Y-%m-%dT%H:%M:%S+00:00')"
  fi
  local decision_window_min="${PM_MARKET_REFRESH_ACTIONABLE_DECISION_WINDOW_MIN:-60}"
  if ! [[ "${decision_window_min}" =~ ^[0-9]+$ ]] || [ "${decision_window_min}" -lt 5 ]; then
    decision_window_min="60"
  fi
  local decision_cutoff=""
  decision_cutoff="$("$PY" - "$decision_window_min" <<'PY' 2>/dev/null || true
import sys
from datetime import datetime, timedelta, timezone

try:
    minutes = int(sys.argv[1])
except Exception:
    minutes = 60
dt = datetime.now(timezone.utc) - timedelta(minutes=max(5, minutes))
print(dt.isoformat(timespec="seconds"))
PY
)"
  if [ -z "${decision_cutoff:-}" ]; then
    decision_cutoff="${cutoff}"
  fi
  local recent_limit="${PM_MARKET_REFRESH_DECISION_RECENT_ID_LIMIT:-5000}"
  if ! [[ "${recent_limit}" =~ ^[0-9]+$ ]] || [ "${recent_limit}" -lt 500 ]; then
    recent_limit="5000"
  fi
  local count=""
  count="$(sqlite3 "$db_path" "
    PRAGMA busy_timeout=100;
    WITH recent_decisions AS (
      SELECT run_id, slug, event_date, will_order, decided_state, reason_codes_json, details_json, created_at
      FROM auto_trade_decisions
      WHERE id >= COALESCE((SELECT MAX(id) - ${recent_limit} FROM auto_trade_decisions), 0)
        AND slug IS NOT NULL AND slug != ''
        AND event_date IS NOT NULL AND event_date != ''
        AND action = 'entry'
        AND event_date >= date('${run_date}')
        AND created_at >= '${decision_cutoff}'
    ),
    candidate_subjects AS (
      SELECT DISTINCT slug, event_date
      FROM recent_decisions
      WHERE slug IS NOT NULL AND slug != ''
        AND event_date IS NOT NULL AND event_date != ''
        AND (
          run_id = (SELECT MAX(run_id) FROM recent_decisions)
          OR COALESCE(will_order, 0) = 1
          OR decided_state = 'entry_ok'
          OR reason_codes_json LIKE '%weather_rules_contract_incomplete%'
        )
    ),
    active_facts AS (
      SELECT f.slug, f.event_date
      FROM candidate_subjects f
      WHERE EXISTS (
        SELECT 1
        FROM market_tokens mt
        WHERE mt.slug = f.slug
          AND mt.event_date = f.event_date
          AND COALESCE(mt.market_closed, 0) = 0
          AND COALESCE(mt.market_archived, 0) = 0
          AND COALESCE(mt.market_active, 1) != 0
      )
    )
    SELECT COALESCE(COUNT(*),0)
    FROM active_facts f
    WHERE NOT EXISTS (
      SELECT 1
      FROM event_market_ticks t
      WHERE t.slug = f.slug
        AND t.event_date = f.event_date
        AND t.created_at >= '${cutoff}'
      LIMIT 1
    );
  " 2>/dev/null | tail -n 1 | tr -dc '0-9')"
  if [ -z "${count:-}" ]; then
    count="0"
  fi
  echo "$count"
}
if [ -d "$AUTO_LOCK_DIR" ]; then
  lock_info="$(AUTO_META="$AUTO_LOCK_META" "$PY" - <<'PY' 2>/dev/null || true
import json, os, datetime
path = (os.environ.get("AUTO_META") or "").strip()
try:
    with open(path, "r", encoding="utf-8") as f:
        meta = json.load(f) or {}
except Exception:
    meta = {}
started = str(meta.get("started_at") or "").strip()
pid = str(meta.get("pid") or "").strip()
if not started:
    raise SystemExit(0)
try:
    dt = datetime.datetime.fromisoformat(started.replace("Z", "+00:00"))
except Exception:
    raise SystemExit(0)
if dt.tzinfo is None:
    dt = dt.replace(tzinfo=datetime.timezone.utc)
now = datetime.datetime.now(datetime.timezone.utc)
age = int(max(0, (now - dt).total_seconds()))
print(f"{age}|{pid}")
PY
)"
  age_sec="${lock_info%%|*}"
  lock_pid="${lock_info##*|}"
  if [ -n "${lock_pid:-}" ] && ! kill -0 "${lock_pid}" 2>/dev/null; then
    rm -rf "$AUTO_LOCK_DIR" 2>/dev/null || true
  elif [ -n "${age_sec:-}" ] && [ "${age_sec:-0}" -ge 0 ] && [ "${age_sec:-0}" -lt "${PM_AUTO_TRADE_RUNTIME_LOCK_STALE_SEC}" ]; then
    # Force priority-only mode (no "others") while auto-trade is active.
    : "${PM_MARKET_REFRESH_PRIORITY_OTHERS_PER_RUN_AUTO:=0}"
    : "${PM_MARKET_REFRESH_PRIORITY_CANDIDATES_PER_RUN_AUTO:=2}"
    PM_MARKET_REFRESH_PRIORITY_OTHERS_PER_RUN="${PM_MARKET_REFRESH_PRIORITY_OTHERS_PER_RUN_AUTO}"
    PM_MARKET_REFRESH_PRIORITY_CANDIDATES_PER_RUN="${PM_MARKET_REFRESH_PRIORITY_CANDIDATES_PER_RUN_AUTO}"
    AUTO_TRADE_ACTIVE=1
    export PM_SCAN_PRIORITY_CANDIDATES_PER_RUN="${PM_MARKET_REFRESH_PRIORITY_CANDIDATES_PER_RUN}"
    export PM_SCAN_PRIORITY_OTHERS_PER_RUN="${PM_MARKET_REFRESH_PRIORITY_OTHERS_PER_RUN}"
    echo "$(date '+%Y-%m-%d %H:%M:%S') [market_refresh] auto_trade running, prioritize Top OK (age_sec=${age_sec})" >> "$ROOT/data/market_refresh.log"
  else
    # Stale lock (UI crashed mid-run); clear it and proceed.
    rm -rf "$AUTO_LOCK_DIR" 2>/dev/null || true
  fi
fi
# Avoid overlapping market refreshes or racing the main scan.
LOCK_DIR="$ROOT/data/market_refresh.lock"
LOCK_PID_FILE="$LOCK_DIR/pid"
DB_LOCK_DIR="$ROOT/data/db_writer.lock"
DB_LOCK_PID_FILE="$DB_LOCK_DIR/pid"
DB_LOCK_OWNER_FILE="$DB_LOCK_DIR/owner"
_scan_pid_running() {
  local pid="${1:-}"
  if [ -z "$pid" ]; then
    return 1
  fi
  if ! kill -0 "$pid" 2>/dev/null; then
    return 1
  fi
  local cmdline
  cmdline="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  # pid file stores the wrapper script pid ($$), not the python pid.
  # Treat it as running if it's this script, or the python market-refresh invocation.
  if [[ "$cmdline" == *"scripts/market_refresh_1m.sh"* ]]; then
    return 0
  fi
  if [[ "$cmdline" == *"scan_weather_market.py"* && "$cmdline" == *"--scan-weather-today"* && "$cmdline" == *"--market-refresh-only"* ]]; then
    return 0
  fi
  return 1
}

_pid_running() {
  local pid="${1:-}"
  if ! [[ "${pid}" =~ ^[0-9]+$ ]]; then
    return 1
  fi
  kill -0 "${pid}" 2>/dev/null
}

_market_tick_p90_age_sec() {
  local db_path="$ROOT/data/archive.db"
  if [ ! -f "$db_path" ]; then
    return 1
  fi
"$PY" - "$db_path" <<'PY' 2>/dev/null || true
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

if ZoneInfo is not None:
    run_date = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
else:
    run_date = datetime.now(timezone(timedelta(hours=-4))).date().isoformat()

db_path = sys.argv[1]
write_scope = str(os.environ.get("PM_ARCHIVE_WRITE_SCOPE") or "").strip()
slug_filter = ""
params = [run_date, run_date]
if write_scope == "chengdu_zuuu":
    slug_filter = "AND h.slug >= ? AND h.slug < ?"
    params.extend(["highest-temperature-in-chengdu-", "highest-temperature-in-chengdu."])
cutoff_iso = (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat(timespec="seconds")
try:
    conn = sqlite3.connect(db_path, timeout=1.0)
    conn.row_factory = sqlite3.Row
    deadline = time.monotonic() + float(os.environ.get("PM_MARKET_REFRESH_HEALTH_SQL_TIMEOUT_SEC", "2.0"))
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 1000)
    rows = conn.execute(
        f"""
        WITH active AS (
          SELECT h.slug, h.event_date
          FROM event_fact_latest l
          JOIN event_fact_history h
            ON h.id = l.fact_id
          WHERE h.run_date = ?
            AND h.event_date >= date(?)
            {slug_filter}
        )
        SELECT (
          SELECT MAX(t.created_at)
          FROM event_market_ticks t
          WHERE t.slug = active.slug
            AND t.event_date = active.event_date
        ) AS ts
        FROM active
        """,
        tuple(params),
    ).fetchall()
    conn.close()
except Exception:
    raise SystemExit(0)

if not rows:
    raise SystemExit(0)

now = datetime.now(timezone.utc)
ages = []
for row in rows:
    ts = row["ts"] if row is not None else None
    if not ts:
        continue
    text = str(ts).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except Exception:
        try:
            dt = datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except Exception:
            continue
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    ages.append(int(max(0, (now - dt).total_seconds())))

if not ages:
    raise SystemExit(0)
ages.sort()
idx = int(round(0.90 * (len(ages) - 1)))
idx = max(0, min(idx, len(ages) - 1))
print(ages[idx])
PY
}

_market_tick_max_age_sec() {
  local db_path="$ROOT/data/archive.db"
  if [ ! -f "$db_path" ]; then
    return 1
  fi
"$PY" - "$db_path" <<'PY' 2>/dev/null || true
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

if ZoneInfo is not None:
    run_date = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
else:
    run_date = datetime.now(timezone(timedelta(hours=-4))).date().isoformat()

db_path = sys.argv[1]
write_scope = str(os.environ.get("PM_ARCHIVE_WRITE_SCOPE") or "").strip()
slug_filter = ""
params = [run_date, run_date]
if write_scope == "chengdu_zuuu":
    slug_filter = "AND h.slug >= ? AND h.slug < ?"
    params.extend(["highest-temperature-in-chengdu-", "highest-temperature-in-chengdu."])
try:
    conn = sqlite3.connect(db_path, timeout=1.0)
    conn.row_factory = sqlite3.Row
    deadline = time.monotonic() + float(os.environ.get("PM_MARKET_REFRESH_HEALTH_SQL_TIMEOUT_SEC", "2.0"))
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 1000)
    rows = conn.execute(
        f"""
        WITH active AS (
          SELECT h.slug, h.event_date
          FROM event_fact_latest l
          JOIN event_fact_history h
            ON h.id = l.fact_id
          WHERE h.run_date = ?
            AND h.event_date >= date(?)
            {slug_filter}
        )
        SELECT (
          SELECT MAX(t.created_at)
          FROM event_market_ticks t
          WHERE t.slug = active.slug
            AND t.event_date = active.event_date
        ) AS ts
        FROM active
        """,
        tuple(params),
    ).fetchall()
    conn.close()
except Exception:
    raise SystemExit(0)

now = datetime.now(timezone.utc)
ages = []
for row in rows:
    text = str(row["ts"] or "").strip().replace("Z", "+00:00")
    if not text:
        continue
    try:
        dt = datetime.fromisoformat(text)
    except Exception:
        try:
            dt = datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except Exception:
            continue
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    ages.append(int(max(0, (now - dt).total_seconds())))
if ages:
    print(max(ages))
PY
}

_market_tick_incomplete_coverage_count() {
  local db_path="$ROOT/data/archive.db"
  if [ ! -f "$db_path" ]; then
    echo 0
    return 0
  fi
"$PY" - "$db_path" <<'PY' 2>/dev/null || echo 0
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

if ZoneInfo is not None:
    run_date = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
else:
    run_date = datetime.now(timezone(timedelta(hours=-4))).date().isoformat()

db_path = sys.argv[1]
write_scope = str(os.environ.get("PM_ARCHIVE_WRITE_SCOPE") or "").strip()
slug_filter = ""
params = [run_date, run_date]
if write_scope == "chengdu_zuuu":
    slug_filter = "AND h.slug >= ? AND h.slug < ?"
    params.extend(["highest-temperature-in-chengdu-", "highest-temperature-in-chengdu."])
try:
    conn = sqlite3.connect(db_path, timeout=1.0)
    conn.row_factory = sqlite3.Row
    deadline = time.monotonic() + float(os.environ.get("PM_MARKET_REFRESH_HEALTH_SQL_TIMEOUT_SEC", "2.0"))
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 1000)
    rows = conn.execute(
        f"""
        WITH active AS (
          SELECT h.slug, h.event_date
          FROM event_fact_latest l
          JOIN event_fact_history h
            ON h.id = l.fact_id
          WHERE h.run_date = ?
            AND h.event_date >= date(?)
            {slug_filter}
        ),
        token_counts AS (
          SELECT mt.slug, mt.event_date, COUNT(DISTINCT mt.outcome) AS expected
          FROM market_tokens mt
          JOIN active a
            ON a.slug = mt.slug AND a.event_date = mt.event_date
          WHERE COALESCE(mt.market_closed, 0) = 0
            AND COALESCE(mt.market_archived, 0) = 0
            AND COALESCE(mt.market_active, 1) != 0
          GROUP BY mt.slug, mt.event_date
        )
        SELECT COUNT(*) AS n
        FROM token_counts tc
        WHERE tc.expected > 0
          AND (
            SELECT COUNT(DISTINCT t.outcome)
            FROM event_market_ticks t INDEXED BY idx_event_market_ticks_slug_event_created
            WHERE t.slug = tc.slug
              AND t.event_date = tc.event_date
              AND t.created_at >= ?
              AND (t.side IS NULL OR t.side = '' OR UPPER(t.side) = 'YES')
          ) < tc.expected
        """
        ,
        tuple(params + [cutoff_iso]),
    ).fetchone()
    conn.close()
except Exception:
    print(0)
    raise SystemExit(0)
print(int(rows["n"] or 0) if rows is not None else 0)
PY
}

_market_ws_active() {
  local hb_path="${PM_MARKET_WS_HEARTBEAT_PATH:-$ROOT/data/market_ws_heartbeat.json}"
  local max_age="${PM_MARKET_WS_HEARTBEAT_MAX_AGE_SEC:-20}"
  if ! [[ "${max_age}" =~ ^[0-9]+$ ]] || [ "${max_age}" -lt 1 ]; then
    max_age=20
  fi
  if [ -f "$hb_path" ]; then
    local age=""
    age="$("$PY" - "$hb_path" <<'PY' 2>/dev/null || true
import json
import sys
from datetime import datetime, timezone

try:
    payload = json.load(open(sys.argv[1], "r", encoding="utf-8"))
    if not payload.get("ok"):
        raise SystemExit(1)
    text = str(payload.get("updated_at") or "").strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    print(int(max(0, (datetime.now(timezone.utc) - dt).total_seconds())))
except Exception:
    raise SystemExit(1)
PY
)"
    if [[ "${age:-}" =~ ^[0-9]+$ ]] && [ "$age" -le "$max_age" ]; then
      return 0
    fi
  fi
  # A live process is not enough to suppress REST refresh; only a fresh OK
  # heartbeat proves that WebSocket ticks are actually flowing.
  return 1
}

if [ -z "${tick_age_p90_sec:-}" ]; then
  tick_age_p90_sec="$(_market_tick_p90_age_sec)"
fi
PM_MARKET_REFRESH_STALE_BYPASS_SEC="$(normalize_positive_int_max "${PM_MARKET_REFRESH_STALE_BYPASS_SEC:-120}" 120 3600)"
MARKET_TICKS_STALE_BYPASS=0
if [[ "${tick_age_p90_sec:-}" =~ ^[0-9]+$ ]] && [ "${tick_age_p90_sec}" -ge "${PM_MARKET_REFRESH_STALE_BYPASS_SEC}" ]; then
  MARKET_TICKS_STALE_BYPASS=1
fi
# Auto-trade workers repair candidate ticks on demand, but recent decision-level
# stale blockers mean that repair already failed and the fixed market-refresh
# lane should help. A global stale p90 alone is still not enough, because it can
# be dominated by old/non-actionable markets.
decision_stale_bypass_count="$(_recent_market_tick_stale_decision_count)"
actionable_tick_stale_count="$(_current_actionable_tick_stale_count)"
tick_coverage_miss_count="$(_market_tick_incomplete_coverage_count)"
MARKET_TICKS_COVERAGE_BYPASS=0
if [[ "${tick_coverage_miss_count:-0}" =~ ^[0-9]+$ ]] && [ "${tick_coverage_miss_count:-0}" -gt 0 ]; then
  MARKET_TICKS_COVERAGE_BYPASS=1
  MARKET_TICKS_STALE_BYPASS=1
fi
_run_weather_fast_discovery_once
_kick_model_scan_if_new_market_signal
if [ -z "${PM_MARKET_REFRESH_STALE_BYPASS_DURING_AUTO_TRADE+x}" ]; then
  if [ "${MARKET_TICKS_COVERAGE_BYPASS}" = "1" ] \
    || { [ "${MARKET_TICKS_STALE_BYPASS}" = "1" ] \
    && (
      { [[ "${decision_stale_bypass_count:-0}" =~ ^[0-9]+$ ]] && [ "${decision_stale_bypass_count:-0}" -gt 0 ]; } \
      || { [[ "${actionable_tick_stale_count:-0}" =~ ^[0-9]+$ ]] && [ "${actionable_tick_stale_count:-0}" -gt 0 ]; }
    ); }; then
    PM_MARKET_REFRESH_STALE_BYPASS_DURING_AUTO_TRADE=1
  else
    PM_MARKET_REFRESH_STALE_BYPASS_DURING_AUTO_TRADE=0
  fi
fi

if [ "${PM_MARKET_REFRESH_WS_PREFERRED:-1}" = "1" ] && _market_ws_active; then
  ws_fresh_skip_sec="${PM_MARKET_REFRESH_WS_FRESH_SKIP_SEC:-25}"
  if ! [[ "${ws_fresh_skip_sec}" =~ ^[0-9]+$ ]] || [ "${ws_fresh_skip_sec}" -lt 1 ]; then
    ws_fresh_skip_sec=25
  fi
  if [ "${PM_MARKET_REFRESH_WS_SKIP_WHEN_ACTIONABLE_FRESH:-1}" = "1" ] \
    && [[ "${decision_stale_bypass_count:-0}" =~ ^[0-9]+$ ]] \
    && [ "${decision_stale_bypass_count:-0}" -eq 0 ] \
    && [[ "${actionable_tick_stale_count:-0}" =~ ^[0-9]+$ ]] \
    && [ "${actionable_tick_stale_count:-0}" -eq 0 ] \
    && [[ "${tick_coverage_miss_count:-0}" =~ ^[0-9]+$ ]] \
    && [ "${tick_coverage_miss_count:-0}" -eq 0 ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [market_refresh] skip REST; market WebSocket covers actionable set p90=${tick_age_p90_sec:-na}s coverage_miss=0" >> "$ROOT/data/market_refresh.log"
    exit 0
  elif [[ "${tick_coverage_miss_count:-0}" =~ ^[0-9]+$ ]] && [ "${tick_coverage_miss_count:-0}" -gt 0 ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [market_refresh] force REST; market tick outcome coverage incomplete events=${tick_coverage_miss_count} p90=${tick_age_p90_sec:-na}s" >> "$ROOT/data/market_refresh.log"
  fi
  tick_age_max_sec="$(_market_tick_max_age_sec)"
  if [[ "${tick_age_max_sec:-}" =~ ^[0-9]+$ ]] \
    && [ "${tick_age_max_sec}" -le "${ws_fresh_skip_sec}" ] \
    && [[ "${decision_stale_bypass_count:-0}" =~ ^[0-9]+$ ]] \
    && [ "${decision_stale_bypass_count:-0}" -eq 0 ] \
    && [[ "${actionable_tick_stale_count:-0}" =~ ^[0-9]+$ ]] \
    && [ "${actionable_tick_stale_count:-0}" -eq 0 ] \
    && [[ "${tick_coverage_miss_count:-0}" =~ ^[0-9]+$ ]] \
    && [ "${tick_coverage_miss_count:-0}" -eq 0 ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [market_refresh] skip REST; market WebSocket fresh max_age=${tick_age_max_sec}s p90=${tick_age_p90_sec:-na}s coverage_miss=0" >> "$ROOT/data/market_refresh.log"
    exit 0
  elif [[ "${tick_coverage_miss_count:-0}" =~ ^[0-9]+$ ]] && [ "${tick_coverage_miss_count:-0}" -gt 0 ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [market_refresh] skip blocked; WebSocket fresh but outcome coverage incomplete events=${tick_coverage_miss_count}" >> "$ROOT/data/market_refresh.log"
  fi
fi

if [ "${PM_MARKET_REFRESH_YIELD_TO_AUTO_TRADE_ACTIVE:-1}" = "1" ]; then
  if [ "${AUTO_TRADE_ACTIVE}" = "1" ]; then
    if [ "${MARKET_TICKS_STALE_BYPASS}" = "1" ] && [ "${PM_MARKET_REFRESH_STALE_BYPASS_DURING_AUTO_TRADE}" = "1" ]; then
      echo "$(date '+%Y-%m-%d %H:%M:%S') [market_refresh] auto_trade active but ticks stale/incomplete, enter guarded refresh tick_age_p90=${tick_age_p90_sec}s stale_sec=${PM_MARKET_REFRESH_STALE_BYPASS_SEC} decision_stale=${decision_stale_bypass_count:-0} actionable_stale=${actionable_tick_stale_count:-0} coverage_miss=${tick_coverage_miss_count:-0}" >> "$ROOT/data/market_refresh.log"
    else
      echo "$(date '+%Y-%m-%d %H:%M:%S') [market_refresh] yield to active auto_trade worker tick_age_p90=${tick_age_p90_sec:-na}s stale_bypass=${MARKET_TICKS_STALE_BYPASS} decision_stale=${decision_stale_bypass_count:-0} actionable_stale=${actionable_tick_stale_count:-0} bypass_during_auto=${PM_MARKET_REFRESH_STALE_BYPASS_DURING_AUTO_TRADE}" >> "$ROOT/data/market_refresh.log"
      exit 0
    fi
  fi
  if _auto_trade_worker_active; then
    AUTO_TRADE_ACTIVE=1
    if [ "${MARKET_TICKS_STALE_BYPASS}" = "1" ] && [ "${PM_MARKET_REFRESH_STALE_BYPASS_DURING_AUTO_TRADE}" = "1" ]; then
      echo "$(date '+%Y-%m-%d %H:%M:%S') [market_refresh] auto_trade worker active but ticks stale/incomplete, enter guarded refresh tick_age_p90=${tick_age_p90_sec}s stale_sec=${PM_MARKET_REFRESH_STALE_BYPASS_SEC} decision_stale=${decision_stale_bypass_count:-0} actionable_stale=${actionable_tick_stale_count:-0} coverage_miss=${tick_coverage_miss_count:-0}" >> "$ROOT/data/market_refresh.log"
    else
      echo "$(date '+%Y-%m-%d %H:%M:%S') [market_refresh] yield to active auto_trade worker process tick_age_p90=${tick_age_p90_sec:-na}s stale_bypass=${MARKET_TICKS_STALE_BYPASS} decision_stale=${decision_stale_bypass_count:-0} actionable_stale=${actionable_tick_stale_count:-0} bypass_during_auto=${PM_MARKET_REFRESH_STALE_BYPASS_DURING_AUTO_TRADE}" >> "$ROOT/data/market_refresh.log"
      exit 0
    fi
  fi
  if _auto_trade_entry_active; then
    AUTO_TRADE_ACTIVE=1
    if [ "${MARKET_TICKS_STALE_BYPASS}" = "1" ] && [ "${PM_MARKET_REFRESH_STALE_BYPASS_DURING_AUTO_TRADE}" = "1" ]; then
      echo "$(date '+%Y-%m-%d %H:%M:%S') [market_refresh] auto_trade entry enqueue active but ticks stale/incomplete, enter guarded refresh tick_age_p90=${tick_age_p90_sec}s stale_sec=${PM_MARKET_REFRESH_STALE_BYPASS_SEC} decision_stale=${decision_stale_bypass_count:-0} actionable_stale=${actionable_tick_stale_count:-0} coverage_miss=${tick_coverage_miss_count:-0}" >> "$ROOT/data/market_refresh.log"
    else
      echo "$(date '+%Y-%m-%d %H:%M:%S') [market_refresh] yield to active auto_trade entry enqueue tick_age_p90=${tick_age_p90_sec:-na}s stale_bypass=${MARKET_TICKS_STALE_BYPASS} decision_stale=${decision_stale_bypass_count:-0} actionable_stale=${actionable_tick_stale_count:-0} bypass_during_auto=${PM_MARKET_REFRESH_STALE_BYPASS_DURING_AUTO_TRADE}" >> "$ROOT/data/market_refresh.log"
      exit 0
    fi
  fi
  queued_entry_jobs="$(_queued_entry_job_count)"
  queued_entry_yield_min="$(normalize_positive_int_max "${PM_MARKET_REFRESH_YIELD_TO_QUEUED_AUTO_TRADE_JOBS_MIN:-1}" 1 20)"
  if ! [[ "${queued_entry_jobs}" =~ ^[0-9]+$ ]]; then
    queued_entry_jobs=0
  fi
  if [ "${queued_entry_jobs}" -ge "${queued_entry_yield_min}" ]; then
    PM_MARKET_REFRESH_STALE_MAX_QUEUED_AUTO_JOBS="$(normalize_int_range "${PM_MARKET_REFRESH_STALE_MAX_QUEUED_AUTO_JOBS:-1}" 1 0 20)"
    if [ "${MARKET_TICKS_STALE_BYPASS}" = "1" ] \
      && [ "${PM_MARKET_REFRESH_STALE_BYPASS_DURING_AUTO_TRADE}" = "1" ] \
      && [ "${PM_MARKET_REFRESH_STALE_MAX_QUEUED_AUTO_JOBS}" -gt 0 ] \
      && [ "${queued_entry_jobs}" -le "${PM_MARKET_REFRESH_STALE_MAX_QUEUED_AUTO_JOBS}" ]; then
      AUTO_TRADE_ACTIVE=1
      echo "$(date '+%Y-%m-%d %H:%M:%S') [market_refresh] queued auto_trade jobs present but ticks stale/incomplete, enter guarded refresh count=${queued_entry_jobs} max_bypass=${PM_MARKET_REFRESH_STALE_MAX_QUEUED_AUTO_JOBS} tick_age_p90=${tick_age_p90_sec}s coverage_miss=${tick_coverage_miss_count:-0}" >> "$ROOT/data/market_refresh.log"
    else
      echo "$(date '+%Y-%m-%d %H:%M:%S') [market_refresh] yield to queued auto_trade jobs count=${queued_entry_jobs} min=${queued_entry_yield_min} tick_age_p90=${tick_age_p90_sec:-na}s decision_stale=${decision_stale_bypass_count:-0} actionable_stale=${actionable_tick_stale_count:-0}" >> "$ROOT/data/market_refresh.log"
      exit 0
    fi
  fi
fi

# Avoid concurrent heavy writers by default, but allow emergency refresh when ticks are stale.
if weather_refresh_legacy_peer_guards_enabled && pgrep -f "scan_weather_market.py .*--scan-weather-today.*--model-refresh-only" >/dev/null 2>&1; then
  : "${PM_MARKET_REFRESH_IGNORE_MODEL_SCAN_ACTIVE:=0}"
  if [ "${PM_MARKET_REFRESH_IGNORE_MODEL_SCAN_ACTIVE}" = "1" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [market_refresh] model_scan active ignored (separate model/market lanes)" >> "$ROOT/data/market_refresh.log"
  else
  if [ "${PM_MARKET_REFRESH_YIELD_TO_MODEL_SCAN_ACTIVE:-1}" = "1" ]; then
    if [ "${MARKET_TICKS_STALE_BYPASS}" = "1" ]; then
      echo "$(date '+%Y-%m-%d %H:%M:%S') [market_refresh] model_scan active but ticks stale, enter guarded refresh tick_age_p90=${tick_age_p90_sec}s stale_sec=${PM_MARKET_REFRESH_STALE_BYPASS_SEC}" >> "$ROOT/data/market_refresh.log"
    else
      echo "$(date '+%Y-%m-%d %H:%M:%S') [market_refresh] yield to active model_scan tick_age_p90=${tick_age_p90_sec:-na}s" >> "$ROOT/data/market_refresh.log"
      exit 0
    fi
  fi
  : "${PM_MARKET_REFRESH_MODEL_SCAN_SKIP_MAX_TICK_AGE_SEC:=120}"
  if [[ "${tick_age_p90_sec:-}" =~ ^[0-9]+$ ]] && [ "$tick_age_p90_sec" -lt "$PM_MARKET_REFRESH_MODEL_SCAN_SKIP_MAX_TICK_AGE_SEC" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [market_refresh] model_scan running, skip (policy tick_age_p90=${tick_age_p90_sec}s<th=${PM_MARKET_REFRESH_MODEL_SCAN_SKIP_MAX_TICK_AGE_SEC}s)" >> "$ROOT/data/market_refresh.log"
    exit 0
  fi
  echo "$(date '+%Y-%m-%d %H:%M:%S') [market_refresh] model_scan running but ticks stale, bypass skip (tick_age_p90=${tick_age_p90_sec:-na}s th=${PM_MARKET_REFRESH_MODEL_SCAN_SKIP_MAX_TICK_AGE_SEC}s)" >> "$ROOT/data/market_refresh.log"
  fi
fi

if pgrep -f "scan_weather_market.py .*--trade-sync-" >/dev/null 2>&1; then
  if [ "${MARKET_TICKS_STALE_BYPASS}" = "1" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [market_refresh] trade_sync active but ticks stale/incomplete, enter guarded refresh tick_age_p90=${tick_age_p90_sec}s stale_sec=${PM_MARKET_REFRESH_STALE_BYPASS_SEC} coverage_miss=${tick_coverage_miss_count:-0}" >> "$ROOT/data/market_refresh.log"
  else
    echo "$(date '+%Y-%m-%d %H:%M:%S') [market_refresh] yield to active trade_sync tick_age_p90=${tick_age_p90_sec:-na}s" >> "$ROOT/data/market_refresh.log"
    exit 0
  fi
fi

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  lock_pid=""
  if [ -f "$LOCK_PID_FILE" ]; then
    lock_pid="$(cat "$LOCK_PID_FILE" 2>/dev/null || true)"
  fi
  if _scan_pid_running "$lock_pid"; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [market_refresh] already running, skip (pid=$lock_pid)" >> "$ROOT/data/market_refresh.log"
    exit 0
  fi
  echo "$(date '+%Y-%m-%d %H:%M:%S') [market_refresh] stale lock cleared" >> "$ROOT/data/market_refresh.log"
  rm -rf "$LOCK_DIR"
  mkdir "$LOCK_DIR" 2>/dev/null || {
    echo "$(date '+%Y-%m-%d %H:%M:%S') [market_refresh] unable to acquire lock, skip" >> "$ROOT/data/market_refresh.log"
    exit 0
  }
fi
echo "$$" > "$LOCK_PID_FILE"
DB_LOCK_HELD=0
LOCK_RELEASED=0
cleanup_locks() {
  weather_scan_lane_release
  weather_refresh_db_writer_release
  if [ "$LOCK_RELEASED" != "1" ]; then
    rm -rf "$LOCK_DIR" 2>/dev/null || true
    LOCK_RELEASED=1
  fi
  if [ "$DB_LOCK_HELD" = "1" ]; then
    cur_pid=""
    if [ -f "$DB_LOCK_PID_FILE" ]; then
      cur_pid="$(cat "$DB_LOCK_PID_FILE" 2>/dev/null || true)"
    fi
    if [ -z "${cur_pid:-}" ] || [ "${cur_pid}" = "$$" ]; then
      rm -rf "$DB_LOCK_DIR" 2>/dev/null || true
    fi
  fi
  return 0
}
trap cleanup_locks EXIT
market_refresh_lane_wait_default=0
if [ "${MARKET_TICKS_STALE_BYPASS:-0}" = "1" ]; then
  market_refresh_lane_wait_default=15
fi
PM_MARKET_REFRESH_LANE_WAIT_SEC="$(normalize_int_range "${PM_MARKET_REFRESH_LANE_WAIT_SEC:-${market_refresh_lane_wait_default}}" "${market_refresh_lane_wait_default}" 0 30)"
if ! weather_scan_lane_acquire "market_refresh" "$PM_MARKET_REFRESH_LANE_WAIT_SEC" "$ROOT/data/market_refresh.log" "market_refresh"; then
  rm -rf "$LOCK_DIR" 2>/dev/null || true
  exit 0
fi

# Coordinate the two heavy archive writers by default. This lock is shared with
# model_scan; auto-trade execution does not take it, so queued jobs can still run.
: "${PM_MARKET_REFRESH_DB_LOCK_ENABLED:=1}"
if [ "${PM_MARKET_REFRESH_DB_LOCK_ENABLED}" = "1" ]; then
  : "${PM_DB_WRITER_LOCK_WAIT_SEC:=15}"
  if ! weather_refresh_db_writer_acquire "market_refresh" "$PM_DB_WRITER_LOCK_WAIT_SEC" "$ROOT/data/market_refresh.log" "market_refresh"; then
    rm -rf "$LOCK_DIR" 2>/dev/null || true
    exit 0
  fi
  DB_LOCK_HELD=1
fi

mkdir -p "$ROOT/data"
LOG="$ROOT/data/market_refresh.log"
INSECURE_ARG=""
if [ "${PM_SCAN_INSECURE:-0}" = "1" ]; then
  INSECURE_ARG="--insecure"
fi

# Market refresh should be "low latency": Gamma is only used to resolve outcome tokens,
# so keep it fast and fail-fast to avoid watchdog kills.
: "${PM_SCAN_GAMMA_TIMEOUT:=8}"
: "${PM_SCAN_GAMMA_RETRIES:=2}"
: "${PM_SCAN_GAMMA_BACKOFF:=0.6}"
: "${PM_SCAN_GAMMA_MAX_PAGES:=10}"
export PM_SCAN_GAMMA_TIMEOUT PM_SCAN_GAMMA_RETRIES PM_SCAN_GAMMA_BACKOFF
export PM_SCAN_ALLOW_INSECURE_SSL=1
# Avoid spending most of the time in repeated proxy auto-detection when local proxy is unreachable.
: "${PM_PROXY_REFRESH_UNREACHABLE_SEC:=60}"
export PM_PROXY_REFRESH_UNREACHABLE_SEC
# Minute-level refresh: never scrape the Polymarket weather page (expensive + flaky). Use cache/Gamma only.
export PM_WEATHER_EVENTS_SKIP_PAGE=1
: "${PM_WEATHER_EVENTS_CACHE_MINUTES:=60}"
export PM_WEATHER_EVENTS_CACHE_MINUTES
# Avoid per-event Gamma detail fan-out in minute loop; token cache/priority stubs are sufficient.
export PM_SCAN_FETCH_EVENT_DETAIL=0
# Skip score composer in minute-level refresh, but still materialize facts for
# the small set of snapshots touched by this run so SSOT freshness follows ticks.
export PM_MARKET_REFRESH_COMPOSE_SCORE=0
: "${PM_FACTS_MATERIALIZE_AFTER_SCAN:=0}"
export PM_FACTS_MATERIALIZE_AFTER_SCAN
export PM_SCAN_GAMMA_MAX_PAGES
# Market refresh is freshness-sensitive; give SQLite a short but meaningful
# wait window so transient writer overlap does not roll back the whole event.
export PM_DB_CONNECT_TIMEOUT_SEC="$(normalize_positive_int_max "${PM_DB_CONNECT_TIMEOUT_SEC:-${PM_MARKET_REFRESH_DB_CONNECT_TIMEOUT_SEC:-5}}" 5 5)"
export PM_DB_BUSY_TIMEOUT_MS="$(normalize_positive_int_max "${PM_DB_BUSY_TIMEOUT_MS:-${PM_MARKET_REFRESH_DB_BUSY_TIMEOUT_MS:-5000}}" 5000 5000)"

# Temperature markets have many outcomes; defaults must still cover more than a
# single stale event per minute loop, otherwise auto-trade keeps seeing stale
# ticks after refresh has already exhausted its budget on the first event.
: "${PM_MARKET_REFRESH_MAX_EVENTS_CAP:=12}"
: "${PM_MARKET_REFRESH_MAX_EVENTS_PER_RUN:=12}"
PM_MARKET_REFRESH_MAX_EVENTS_CAP="$(normalize_positive_int_max "${PM_MARKET_REFRESH_MAX_EVENTS_CAP}" 12 12)"
PM_MARKET_REFRESH_MAX_EVENTS_PER_RUN="$(normalize_positive_int_max "${PM_MARKET_REFRESH_MAX_EVENTS_PER_RUN}" 12 "${PM_MARKET_REFRESH_MAX_EVENTS_CAP}")"
PM_MARKET_REFRESH_MAX_MATCHED_PER_RUN="$(normalize_positive_int_max "${PM_MARKET_REFRESH_MAX_MATCHED_PER_RUN:-${PM_MARKET_REFRESH_MAX_EVENTS_PER_RUN}}" "${PM_MARKET_REFRESH_MAX_EVENTS_PER_RUN}" "${PM_MARKET_REFRESH_MAX_EVENTS_PER_RUN}")"
if [ "${PM_MARKET_REFRESH_MAX_EVENTS_PER_RUN}" -gt "${PM_MARKET_REFRESH_MAX_EVENTS_CAP}" ]; then
  PM_MARKET_REFRESH_MAX_EVENTS_PER_RUN="${PM_MARKET_REFRESH_MAX_EVENTS_CAP}"
fi
if [ "${PM_MARKET_REFRESH_MAX_MATCHED_PER_RUN}" -gt "${PM_MARKET_REFRESH_MAX_EVENTS_PER_RUN}" ]; then
  PM_MARKET_REFRESH_MAX_MATCHED_PER_RUN="${PM_MARKET_REFRESH_MAX_EVENTS_PER_RUN}"
fi

: "${PM_SCAN_FAST_DAYS_AHEAD:=2}"
if ! [[ "${PM_SCAN_FAST_DAYS_AHEAD}" =~ ^-?[0-9]+$ ]]; then
  PM_SCAN_FAST_DAYS_AHEAD=2
fi
: "${PM_MARKET_REFRESH_ALLOW_NEGATIVE_DAYS_AHEAD:=0}"
if [ "${PM_MARKET_REFRESH_ALLOW_NEGATIVE_DAYS_AHEAD}" != "1" ] && [ "${PM_SCAN_FAST_DAYS_AHEAD}" -lt 0 ]; then
  PM_SCAN_FAST_DAYS_AHEAD=2
fi
DAYS_ARG="--days-ahead ${PM_SCAN_FAST_DAYS_AHEAD}"

# Keep yesterday in the scan window to cover settlement-lag events that are still shown in UI freshness alerts.
: "${PM_SCAN_INCLUDE_YESTERDAY:=1}"
export PM_SCAN_INCLUDE_YESTERDAY

# Priority scan plan (SSOT) for market refresh: fixed (positions/open orders/entry_ok) + a small candidate set.
# Note: scan_weather_market.py applies priority selection before any rotation limiter, so keep these small.
: "${PM_MARKET_REFRESH_PRIORITY_ENABLED:=1}"
PM_MARKET_REFRESH_PRIORITY_CANDIDATES_PER_RUN="$(normalize_int_range "${PM_MARKET_REFRESH_PRIORITY_CANDIDATES_PER_RUN:-2}" 2 0 2)"
PM_MARKET_REFRESH_PRIORITY_OTHERS_PER_RUN="$(normalize_int_range "${PM_MARKET_REFRESH_PRIORITY_OTHERS_PER_RUN:-0}" 0 0 0)"
: "${PM_MARKET_REFRESH_PRIORITY_POSITION_TOPN:=16}"
: "${PM_MARKET_REFRESH_PRIORITY_OPEN_ORDER_TOPN:=16}"
: "${PM_MARKET_REFRESH_PRIORITY_MARKET_TICK_STALE_TOPN:=${PM_MARKET_REFRESH_MAX_EVENTS_PER_RUN}}"
if ! [[ "${PM_MARKET_REFRESH_PRIORITY_MARKET_TICK_STALE_TOPN}" =~ ^[0-9]+$ ]] || [ "${PM_MARKET_REFRESH_PRIORITY_MARKET_TICK_STALE_TOPN}" -lt 1 ]; then
  PM_MARKET_REFRESH_PRIORITY_MARKET_TICK_STALE_TOPN="${PM_MARKET_REFRESH_MAX_EVENTS_PER_RUN}"
fi
: "${PM_MARKET_REFRESH_PRIORITY_POSITION_LOOKBACK_DAYS:=${PM_SCAN_FAST_DAYS_AHEAD}}"
if ! [[ "${PM_MARKET_REFRESH_PRIORITY_POSITION_LOOKBACK_DAYS}" =~ ^-?[0-9]+$ ]] || [ "${PM_MARKET_REFRESH_PRIORITY_POSITION_LOOKBACK_DAYS}" -lt 0 ]; then
  PM_MARKET_REFRESH_PRIORITY_POSITION_LOOKBACK_DAYS="${PM_SCAN_FAST_DAYS_AHEAD}"
fi
export PM_SCAN_PRIORITY_ENABLED="${PM_MARKET_REFRESH_PRIORITY_ENABLED}"
export PM_SCAN_PRIORITY_INCLUDE_Q_MISSING="${PM_MARKET_REFRESH_INCLUDE_Q_MISSING:-0}"
export PM_SCAN_PRIORITY_INCLUDE_MARKET_TICK_STALE="${PM_MARKET_REFRESH_INCLUDE_DECISION_STALE:-1}"
export PM_SCAN_PRIORITY_MARKET_TICK_STALE_TOPN="${PM_MARKET_REFRESH_PRIORITY_MARKET_TICK_STALE_TOPN}"
export PM_SCAN_PRIORITY_CANDIDATES_PER_RUN="${PM_MARKET_REFRESH_PRIORITY_CANDIDATES_PER_RUN}"
export PM_SCAN_PRIORITY_OTHERS_PER_RUN="${PM_MARKET_REFRESH_PRIORITY_OTHERS_PER_RUN}"
export PM_SCAN_PRIORITY_POSITION_TOPN="${PM_MARKET_REFRESH_PRIORITY_POSITION_TOPN}"
export PM_SCAN_PRIORITY_OPEN_ORDER_TOPN="${PM_MARKET_REFRESH_PRIORITY_OPEN_ORDER_TOPN}"
export PM_SCAN_PRIORITY_POSITION_LOOKBACK_DAYS="${PM_MARKET_REFRESH_PRIORITY_POSITION_LOOKBACK_DAYS}"
export PM_SCAN_PRIORITY_INCLUDE_NEW_MARKETS="${PM_SCAN_PRIORITY_INCLUDE_NEW_MARKETS:-1}"
export PM_SCAN_PRIORITY_NEW_MARKET_TOPN="${PM_SCAN_PRIORITY_NEW_MARKET_TOPN:-${PM_MARKET_REFRESH_NEW_MARKET_TOPN:-4}}"
export PM_SCAN_PRIORITY_NEW_MARKET_MAX_AGE_MIN="${PM_SCAN_PRIORITY_NEW_MARKET_MAX_AGE_MIN:-240}"
export PM_SCAN_PRIORITY_INCLUDE_RECOMMENDED_PILOT_GAPS="${PM_SCAN_PRIORITY_INCLUDE_RECOMMENDED_PILOT_GAPS:-1}"
export PM_SCAN_PRIORITY_RECOMMENDED_PILOT_GAP_TOPN="${PM_SCAN_PRIORITY_RECOMMENDED_PILOT_GAP_TOPN:-4}"
: "${PM_MARKET_REFRESH_INCLUDE_SETTLEMENT_PENDING:=0}"
export PM_SCAN_PRIORITY_INCLUDE_SETTLEMENT_PENDING="${PM_MARKET_REFRESH_INCLUDE_SETTLEMENT_PENDING}"
if [ "${PM_SCAN_PRIORITY_INCLUDE_SETTLEMENT_PENDING}" = "1" ]; then
  export PM_SCAN_PRIORITY_SETTLEMENT_PENDING_TOPN="${PM_SCAN_PRIORITY_SETTLEMENT_PENDING_TOPN:-${PM_MARKET_REFRESH_MAX_EVENTS_PER_RUN}}"
else
  export PM_SCAN_PRIORITY_SETTLEMENT_PENDING_TOPN="0"
fi
export PM_SCAN_PRIORITY_SETTLEMENT_PENDING_LOOKBACK_DAYS="${PM_SCAN_PRIORITY_SETTLEMENT_PENDING_LOOKBACK_DAYS:-7}"
export PM_SCAN_PRIORITY_RESERVE_NONFIXED_SLOT="${PM_SCAN_PRIORITY_RESERVE_NONFIXED_SLOT:-1}"
export PM_SCAN_PRIORITY_RESERVE_NONFIXED_SLOTS="${PM_SCAN_PRIORITY_RESERVE_NONFIXED_SLOTS:-1}"
export PM_SCAN_PRIORITY_ROTATE_FIXED_WHEN_CAPPED="${PM_SCAN_PRIORITY_ROTATE_FIXED_WHEN_CAPPED:-0}"
# Tail refresh: when the oldest market tick becomes too stale, include it in the fixed set to keep
# the UI freshness SSOT from accumulating a long tail.
: "${PM_MARKET_REFRESH_TAIL_TOPN:=2}"
PM_MARKET_REFRESH_TAIL_TOPN="$(normalize_int_range "${PM_MARKET_REFRESH_TAIL_TOPN}" 2 0 10)"
# Chase the tail early enough to keep SSOT freshness meaningful for the active weather events set.
# This value is intentionally closer to `PM_HEALTH_SCAN_STALE_SEC` (45m) than the max-stale hard error.
: "${PM_MARKET_REFRESH_TAIL_MIN_AGE_SEC:=2700}" # 45m
# Facts stale-tail refresh: if facts are stale but ticks are still fresh, include those slugs too.
: "${PM_MARKET_REFRESH_FACT_TAIL_TOPN:=6}"
PM_MARKET_REFRESH_FACT_TAIL_TOPN="$(normalize_int_range "${PM_MARKET_REFRESH_FACT_TAIL_TOPN}" 6 0 12)"
: "${PM_MARKET_REFRESH_FACT_TAIL_MIN_AGE_SEC:=2700}" # 45m
: "${PM_MARKET_REFRESH_RESERVE_TAIL_SLOT:=1}"
if [ "${PM_MARKET_REFRESH_WS_PREFERRED:-1}" = "1" ] \
  && [ "${PM_MARKET_REFRESH_WS_DISABLE_TAIL:-1}" = "1" ] \
  && _market_ws_active; then
  PM_MARKET_REFRESH_TAIL_TOPN=0
  PM_MARKET_REFRESH_FACT_TAIL_TOPN=0
  PM_MARKET_REFRESH_RESERVE_TAIL_SLOT=0
  export PM_SCAN_PRIORITY_MARKET_TICK_STALE_FILL_ACTUAL=0
fi
if [ "${AUTO_TRADE_ACTIVE}" = "1" ] && [ "${PM_MARKET_REFRESH_AUTO_INCLUDE_TAIL:-0}" != "1" ]; then
  if [ "${PM_MARKET_REFRESH_TAIL_TOPN}" -gt 0 ] || [ "${PM_MARKET_REFRESH_FACT_TAIL_TOPN}" -gt 0 ] || [ "${PM_MARKET_REFRESH_RESERVE_TAIL_SLOT}" != "0" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [market_refresh] auto_trade active, tail refresh disabled; decision-stale priority remains enabled" >> "$ROOT/data/market_refresh.log"
  fi
  PM_MARKET_REFRESH_TAIL_TOPN=0
  PM_MARKET_REFRESH_FACT_TAIL_TOPN=0
  PM_MARKET_REFRESH_RESERVE_TAIL_SLOT=0
  export PM_SCAN_PRIORITY_MARKET_TICK_STALE_FILL_ACTUAL=0
  export PM_SCAN_PRIORITY_NO_HIT_FALLBACK=0
fi
if [ "${PM_MARKET_REFRESH_TAIL_TOPN}" -gt 0 ]; then
  export PM_SCAN_PRIORITY_INCLUDE_TAIL=1
else
  export PM_SCAN_PRIORITY_INCLUDE_TAIL=0
fi
export PM_SCAN_PRIORITY_TAIL_TOPN="${PM_MARKET_REFRESH_TAIL_TOPN}"
export PM_SCAN_PRIORITY_TAIL_MIN_AGE_SEC="${PM_MARKET_REFRESH_TAIL_MIN_AGE_SEC}"
if [ "${PM_MARKET_REFRESH_FACT_TAIL_TOPN}" -gt 0 ]; then
  export PM_SCAN_PRIORITY_INCLUDE_FACT_TAIL=1
else
  export PM_SCAN_PRIORITY_INCLUDE_FACT_TAIL=0
fi
export PM_SCAN_PRIORITY_FACT_TAIL_TOPN="${PM_MARKET_REFRESH_FACT_TAIL_TOPN}"
export PM_SCAN_PRIORITY_FACT_TAIL_MIN_AGE_SEC="${PM_MARKET_REFRESH_FACT_TAIL_MIN_AGE_SEC}"
export PM_SCAN_PRIORITY_RESERVE_TAIL_SLOT="${PM_MARKET_REFRESH_RESERVE_TAIL_SLOT}"
# Place reserved tail slug at slot 0 so timeout-prone runs still process stale-tail first.
export PM_SCAN_PRIORITY_RESERVE_TAIL_PREPEND=1

# Bound fan-out by the fast variable we actually trade on: candidate-neighborhood
# quotes plus a small stale set. Full-ladder refresh makes 0.01/0.99 tail buckets
# repeatedly hit CLOB and delays actionable prices.
: "${PM_SCAN_MARKET_REFRESH_MAX_OUTCOMES:=5}"
: "${PM_SCAN_MARKET_REFRESH_ALL_OUTCOMES_MAX:=4}"
: "${PM_SCAN_MARKET_REFRESH_STALE_OUTCOME_TOPN:=2}"
export PM_SCAN_MARKET_REFRESH_STALE_OUTCOME_TOPN
PM_MARKET_REFRESH_TIME_BUDGET_CAP_SEC="$(normalize_positive_int_max "${PM_MARKET_REFRESH_TIME_BUDGET_CAP_SEC:-${PM_MARKET_REFRESH_BURST_TIME_BUDGET_SEC:-12}}" 12 18)"
PM_SCAN_MARKET_REFRESH_TIME_BUDGET_SEC="$(normalize_positive_int_max "${PM_SCAN_MARKET_REFRESH_TIME_BUDGET_SEC:-${PM_MARKET_REFRESH_TIME_BUDGET_SEC:-8}}" 8 "${PM_MARKET_REFRESH_TIME_BUDGET_CAP_SEC}")"
if [ "${PM_SCAN_MARKET_REFRESH_TIME_BUDGET_SEC}" -gt "${PM_MARKET_REFRESH_TIME_BUDGET_CAP_SEC}" ]; then
  PM_SCAN_MARKET_REFRESH_TIME_BUDGET_SEC="${PM_MARKET_REFRESH_TIME_BUDGET_CAP_SEC}"
fi

: "${PM_MARKET_REFRESH_BURST_ENABLED:=1}"
: "${PM_MARKET_REFRESH_BURST_TICK_AGE_SEC:=2700}"
: "${PM_MARKET_REFRESH_BURST_MAX_EVENTS_PER_RUN:=${PM_MARKET_REFRESH_MAX_EVENTS_PER_RUN}}"
: "${PM_MARKET_REFRESH_BURST_MAX_MATCHED_PER_RUN:=${PM_MARKET_REFRESH_BURST_MAX_EVENTS_PER_RUN}}"
PM_MARKET_REFRESH_BURST_TIME_BUDGET_SEC="$(normalize_positive_int_max "${PM_MARKET_REFRESH_BURST_TIME_BUDGET_SEC:-12}" 12 "${PM_MARKET_REFRESH_TIME_BUDGET_CAP_SEC}")"
: "${PM_MARKET_REFRESH_BURST_PRIORITY_CANDIDATES_PER_RUN:=${PM_MARKET_REFRESH_PRIORITY_CANDIDATES_PER_RUN}}"
: "${PM_MARKET_REFRESH_BURST_PRIORITY_POSITION_TOPN:=${PM_MARKET_REFRESH_PRIORITY_POSITION_TOPN}}"
: "${PM_MARKET_REFRESH_BURST_PRIORITY_OPEN_ORDER_TOPN:=${PM_MARKET_REFRESH_PRIORITY_OPEN_ORDER_TOPN}}"
: "${PM_MARKET_REFRESH_BURST_ALLOW_AUTO_CANDIDATES:=0}"
: "${PM_MARKET_REFRESH_BURST_MAX_EVENTS_WHEN_AUTO_TRADE:=3}"
: "${PM_MARKET_REFRESH_BURST_HOT_EVENTS_CAP:=3}"
: "${PM_MARKET_REFRESH_AUTO_TRADE_HOT_EVENTS_CAP:=3}"
if ! [[ "${PM_MARKET_REFRESH_BURST_TICK_AGE_SEC}" =~ ^[0-9]+$ ]] || [ "${PM_MARKET_REFRESH_BURST_TICK_AGE_SEC}" -lt 60 ]; then
  PM_MARKET_REFRESH_BURST_TICK_AGE_SEC=2700
fi
if ! [[ "${PM_MARKET_REFRESH_BURST_HOT_EVENTS_CAP}" =~ ^[0-9]+$ ]] || [ "${PM_MARKET_REFRESH_BURST_HOT_EVENTS_CAP}" -lt 1 ]; then
  PM_MARKET_REFRESH_BURST_HOT_EVENTS_CAP=3
fi
if ! [[ "${PM_MARKET_REFRESH_AUTO_TRADE_HOT_EVENTS_CAP}" =~ ^[0-9]+$ ]] || [ "${PM_MARKET_REFRESH_AUTO_TRADE_HOT_EVENTS_CAP}" -lt 1 ]; then
  PM_MARKET_REFRESH_AUTO_TRADE_HOT_EVENTS_CAP=3
fi

if [ -z "${tick_age_p90_sec:-}" ]; then
  tick_age_p90_sec="$(_market_tick_p90_age_sec)"
fi
if [ "${PM_MARKET_REFRESH_BURST_ENABLED}" = "1" ] \
  && [[ "${tick_age_p90_sec:-}" =~ ^[0-9]+$ ]] \
  && [ "${tick_age_p90_sec}" -ge "${PM_MARKET_REFRESH_BURST_TICK_AGE_SEC}" ]; then
  burst_events="${PM_MARKET_REFRESH_BURST_MAX_EVENTS_PER_RUN}"
  if ! [[ "${burst_events}" =~ ^[0-9]+$ ]] || [ "${burst_events}" -lt 1 ]; then
    burst_events="${PM_MARKET_REFRESH_MAX_EVENTS_PER_RUN}"
  fi
  if [ "${burst_events}" -gt "${PM_MARKET_REFRESH_MAX_EVENTS_CAP}" ]; then
    burst_events="${PM_MARKET_REFRESH_MAX_EVENTS_CAP}"
  fi
  if [ "${AUTO_TRADE_ACTIVE}" = "1" ] \
    && [[ "${PM_MARKET_REFRESH_BURST_MAX_EVENTS_WHEN_AUTO_TRADE}" =~ ^[0-9]+$ ]] \
    && [ "${PM_MARKET_REFRESH_BURST_MAX_EVENTS_WHEN_AUTO_TRADE}" -gt 0 ] \
    && [ "${burst_events}" -gt "${PM_MARKET_REFRESH_BURST_MAX_EVENTS_WHEN_AUTO_TRADE}" ]; then
    burst_events="${PM_MARKET_REFRESH_BURST_MAX_EVENTS_WHEN_AUTO_TRADE}"
  fi
  if [ "${burst_events}" -gt "${PM_MARKET_REFRESH_BURST_HOT_EVENTS_CAP}" ]; then
    burst_events="${PM_MARKET_REFRESH_BURST_HOT_EVENTS_CAP}"
  fi
  PM_MARKET_REFRESH_MAX_EVENTS_PER_RUN="${burst_events}"

  burst_matched="${PM_MARKET_REFRESH_BURST_MAX_MATCHED_PER_RUN}"
  if ! [[ "${burst_matched}" =~ ^[0-9]+$ ]] || [ "${burst_matched}" -lt 1 ]; then
    burst_matched="${PM_MARKET_REFRESH_MAX_MATCHED_PER_RUN}"
  fi
  if [ "${burst_matched}" -gt "${PM_MARKET_REFRESH_MAX_EVENTS_PER_RUN}" ]; then
    burst_matched="${PM_MARKET_REFRESH_MAX_EVENTS_PER_RUN}"
  fi
  PM_MARKET_REFRESH_MAX_MATCHED_PER_RUN="${burst_matched}"

  burst_budget="${PM_MARKET_REFRESH_BURST_TIME_BUDGET_SEC}"
  if ! [[ "${burst_budget}" =~ ^[0-9]+$ ]] || [ "${burst_budget}" -lt 1 ]; then
    burst_budget="${PM_SCAN_MARKET_REFRESH_TIME_BUDGET_SEC}"
  fi
  if [ "${burst_budget}" -gt "${PM_MARKET_REFRESH_TIME_BUDGET_CAP_SEC}" ]; then
    burst_budget="${PM_MARKET_REFRESH_TIME_BUDGET_CAP_SEC}"
  fi
  PM_SCAN_MARKET_REFRESH_TIME_BUDGET_SEC="${burst_budget}"

  if ([ "${AUTO_TRADE_ACTIVE}" != "1" ] || [ "${PM_MARKET_REFRESH_BURST_ALLOW_AUTO_CANDIDATES}" = "1" ]) \
    && [[ "${PM_MARKET_REFRESH_BURST_PRIORITY_CANDIDATES_PER_RUN}" =~ ^[0-9]+$ ]] \
    && [ "${PM_MARKET_REFRESH_BURST_PRIORITY_CANDIDATES_PER_RUN}" -ge 0 ]; then
    PM_MARKET_REFRESH_PRIORITY_CANDIDATES_PER_RUN="${PM_MARKET_REFRESH_BURST_PRIORITY_CANDIDATES_PER_RUN}"
    export PM_SCAN_PRIORITY_CANDIDATES_PER_RUN="${PM_MARKET_REFRESH_PRIORITY_CANDIDATES_PER_RUN}"
  fi
  if [[ "${PM_MARKET_REFRESH_BURST_PRIORITY_POSITION_TOPN}" =~ ^[0-9]+$ ]] && [ "${PM_MARKET_REFRESH_BURST_PRIORITY_POSITION_TOPN}" -gt 0 ]; then
    export PM_SCAN_PRIORITY_POSITION_TOPN="${PM_MARKET_REFRESH_BURST_PRIORITY_POSITION_TOPN}"
  fi
  if [[ "${PM_MARKET_REFRESH_BURST_PRIORITY_OPEN_ORDER_TOPN}" =~ ^[0-9]+$ ]] && [ "${PM_MARKET_REFRESH_BURST_PRIORITY_OPEN_ORDER_TOPN}" -gt 0 ]; then
    export PM_SCAN_PRIORITY_OPEN_ORDER_TOPN="${PM_MARKET_REFRESH_BURST_PRIORITY_OPEN_ORDER_TOPN}"
  fi
  echo "$(date '+%Y-%m-%d %H:%M:%S') [market_refresh] burst enabled (tick_age_p90=${tick_age_p90_sec}s events=${PM_MARKET_REFRESH_MAX_EVENTS_PER_RUN} matched=${PM_MARKET_REFRESH_MAX_MATCHED_PER_RUN} budget=${PM_SCAN_MARKET_REFRESH_TIME_BUDGET_SEC}s auto_trade=${AUTO_TRADE_ACTIVE})" >> "$LOG"
fi
if [ "${AUTO_TRADE_ACTIVE}" = "1" ]; then
  if [ "${PM_MARKET_REFRESH_MAX_EVENTS_PER_RUN}" -gt "${PM_MARKET_REFRESH_AUTO_TRADE_HOT_EVENTS_CAP}" ]; then
    PM_MARKET_REFRESH_MAX_EVENTS_PER_RUN="${PM_MARKET_REFRESH_AUTO_TRADE_HOT_EVENTS_CAP}"
  fi
  if [ "${PM_MARKET_REFRESH_MAX_MATCHED_PER_RUN}" -gt "${PM_MARKET_REFRESH_MAX_EVENTS_PER_RUN}" ]; then
    PM_MARKET_REFRESH_MAX_MATCHED_PER_RUN="${PM_MARKET_REFRESH_MAX_EVENTS_PER_RUN}"
  fi
fi

# Keep market refresh bounded, but honor the operator-facing CLOB knobs with
# conservative caps so stale-tail repair can survive transient slow CLOB reads.
PM_MARKET_REFRESH_CLOB_TIMEOUT_CAP_SEC="$(normalize_positive_int_max "${PM_MARKET_REFRESH_CLOB_TIMEOUT_CAP_SEC:-2}" 2 5)"
PM_MARKET_REFRESH_CLOB_RETRIES_CAP="$(normalize_positive_int_max "${PM_MARKET_REFRESH_CLOB_RETRIES_CAP:-1}" 1 3)"
if [ -z "${PM_SCAN_MARKET_REFRESH_CLOB_BOOK_TIMEOUT_SEC+x}" ] && [ -n "${PM_MARKET_REFRESH_CLOB_TIMEOUT_SEC:-}" ]; then
  PM_SCAN_MARKET_REFRESH_CLOB_BOOK_TIMEOUT_SEC="${PM_MARKET_REFRESH_CLOB_TIMEOUT_SEC}"
fi
if [ -z "${PM_SCAN_MARKET_REFRESH_CLOB_BOOK_RETRIES+x}" ] && [ -n "${PM_MARKET_REFRESH_CLOB_RETRIES:-}" ]; then
  PM_SCAN_MARKET_REFRESH_CLOB_BOOK_RETRIES="${PM_MARKET_REFRESH_CLOB_RETRIES}"
fi
: "${PM_SCAN_MARKET_REFRESH_CLOB_BOOK_TIMEOUT_SEC:=1}"
: "${PM_SCAN_MARKET_REFRESH_CLOB_BOOK_RETRIES:=1}"
PM_SCAN_MARKET_REFRESH_CLOB_BOOK_TIMEOUT_SEC="$(normalize_positive_int_max "${PM_SCAN_MARKET_REFRESH_CLOB_BOOK_TIMEOUT_SEC}" 1 "${PM_MARKET_REFRESH_CLOB_TIMEOUT_CAP_SEC}")"
PM_SCAN_MARKET_REFRESH_CLOB_BOOK_RETRIES="$(normalize_positive_int_max "${PM_SCAN_MARKET_REFRESH_CLOB_BOOK_RETRIES}" 1 "${PM_MARKET_REFRESH_CLOB_RETRIES_CAP}")"

{
  echo ""
  echo "---- $(date '+%Y-%m-%d %H:%M:%S') market_refresh start ----"
  echo "PY=$PY"
  echo "DAYS=$DAYS_ARG PRIORITY=${PM_SCAN_PRIORITY_ENABLED:-0} P_CAND=${PM_MARKET_REFRESH_PRIORITY_CANDIDATES_PER_RUN} P_OTHERS=${PM_MARKET_REFRESH_PRIORITY_OTHERS_PER_RUN} P_GAP=${PM_SCAN_PRIORITY_RECOMMENDED_PILOT_GAP_TOPN:-0} P_STALE=${PM_SCAN_PRIORITY_MARKET_TICK_STALE_TOPN:-na} P_SETTLE=${PM_SCAN_PRIORITY_SETTLEMENT_PENDING_TOPN:-na} P_POS_TOPN=${PM_SCAN_PRIORITY_POSITION_TOPN:-na} P_OPEN_TOPN=${PM_SCAN_PRIORITY_INCLUDE_OPEN_ORDERS:-0} P_OPEN=${PM_SCAN_PRIORITY_INCLUDE_OPEN_ORDERS:-0} DEPTH=${PM_SCAN_DEPTH_LEVELS:-5} EVENTS=${PM_MARKET_REFRESH_MAX_EVENTS_PER_RUN} MATCHED=${PM_MARKET_REFRESH_MAX_MATCHED_PER_RUN} BUDGET=${PM_SCAN_MARKET_REFRESH_TIME_BUDGET_SEC}s TICK_P90=${tick_age_p90_sec:-na}s AUTO_TRADE=${AUTO_TRADE_ACTIVE} DB_BUSY=${PM_DB_BUSY_TIMEOUT_MS:-na}ms CLOB_RETRIES=${PM_SCAN_MARKET_REFRESH_CLOB_BOOK_RETRIES:-na} NICE=${MARKET_REFRESH_NICE}"
} >> "$LOG"
SCAN_START_UTC="$(date -u '+%Y-%m-%dT%H:%M:%S+00:00')"
PM_MARKET_REFRESH_TOTAL_TIME_BUDGET_CAP_SEC="$(normalize_positive_int_max "${PM_MARKET_REFRESH_TOTAL_TIME_BUDGET_CAP_SEC:-36}" 36 60)"
total_run_budget_default=$(( PM_SCAN_MARKET_REFRESH_TIME_BUDGET_SEC * PM_MARKET_REFRESH_MAX_EVENTS_PER_RUN ))
if [ "${total_run_budget_default}" -lt "${PM_SCAN_MARKET_REFRESH_TIME_BUDGET_SEC}" ]; then
  total_run_budget_default="${PM_SCAN_MARKET_REFRESH_TIME_BUDGET_SEC}"
fi
if [ "${total_run_budget_default}" -gt "${PM_MARKET_REFRESH_TOTAL_TIME_BUDGET_CAP_SEC}" ]; then
  total_run_budget_default="${PM_MARKET_REFRESH_TOTAL_TIME_BUDGET_CAP_SEC}"
fi
: "${PM_SCAN_MARKET_REFRESH_TOTAL_TIME_BUDGET_SEC:=${total_run_budget_default}}"
if ! [[ "${PM_SCAN_MARKET_REFRESH_TOTAL_TIME_BUDGET_SEC}" =~ ^[0-9]+$ ]] || [ "${PM_SCAN_MARKET_REFRESH_TOTAL_TIME_BUDGET_SEC}" -lt 1 ]; then
  PM_SCAN_MARKET_REFRESH_TOTAL_TIME_BUDGET_SEC="${total_run_budget_default}"
fi
if [ "${PM_SCAN_MARKET_REFRESH_TOTAL_TIME_BUDGET_SEC}" -gt "${PM_MARKET_REFRESH_TOTAL_TIME_BUDGET_CAP_SEC}" ]; then
  PM_SCAN_MARKET_REFRESH_TOTAL_TIME_BUDGET_SEC="${PM_MARKET_REFRESH_TOTAL_TIME_BUDGET_CAP_SEC}"
fi
export PM_SCAN_MARKET_REFRESH_FAST_MODE="${PM_SCAN_MARKET_REFRESH_FAST_MODE:-1}"
export PM_SCAN_MARKET_REFRESH_PRICE_AUGMENT_ENABLED="${PM_SCAN_MARKET_REFRESH_PRICE_AUGMENT_ENABLED:-0}"
export PM_SCAN_MARKET_REFRESH_ALLOW_TOKEN_REMAP="${PM_SCAN_MARKET_REFRESH_ALLOW_TOKEN_REMAP:-0}"
export PM_SCAN_MARKET_REFRESH_ALLOW_GAMMA_PRICE_FALLBACK="${PM_SCAN_MARKET_REFRESH_ALLOW_GAMMA_PRICE_FALLBACK:-0}"
export PM_SCAN_MARKET_REFRESH_ALLOW_LAST_TICK_FALLBACK="${PM_SCAN_MARKET_REFRESH_ALLOW_LAST_TICK_FALLBACK:-0}"
export PM_SCAN_MARKET_REFRESH_GLOBAL_RATE_LIMIT="${PM_SCAN_MARKET_REFRESH_GLOBAL_RATE_LIMIT:-1}"
export PM_SCAN_MARKET_REFRESH_QUOTE_PREFETCH_WORKERS="$(normalize_positive_int_max "${PM_SCAN_MARKET_REFRESH_QUOTE_PREFETCH_WORKERS:-6}" 6 6)"
export PM_MARKET_REFRESH_COMPOSE_SCORE="${PM_MARKET_REFRESH_COMPOSE_SCORE:-0}"
export PM_SCAN_MARKET_REFRESH_MAX_OUTCOMES PM_SCAN_MARKET_REFRESH_TIME_BUDGET_SEC
export PM_SCAN_MARKET_REFRESH_ALL_OUTCOMES_MAX
export PM_SCAN_MARKET_REFRESH_TOTAL_TIME_BUDGET_SEC
export PM_MARKET_REFRESH_TOTAL_TIME_BUDGET_CAP_SEC
export PM_SCAN_MARKET_REFRESH_CLOB_BOOK_TIMEOUT_SEC PM_SCAN_MARKET_REFRESH_CLOB_BOOK_RETRIES
export PM_SCAN_MARKET_REFRESH_QUOTE_PREFETCH_WORKERS
export PM_MARKET_REFRESH_CACHE_ONLY="${PM_MARKET_REFRESH_CACHE_ONLY:-1}"
export PM_ARCHIVE_WRITE_SCOPE="${PM_ARCHIVE_WRITE_SCOPE:-chengdu_zuuu}"
export PM_MARKET_REFRESH_TICK_WRITE_DB_CONNECT_TIMEOUT_SEC="${PM_MARKET_REFRESH_TICK_WRITE_DB_CONNECT_TIMEOUT_SEC:-0.25}"
export PM_MARKET_REFRESH_TICK_WRITE_DB_BUSY_TIMEOUT_MS="${PM_MARKET_REFRESH_TICK_WRITE_DB_BUSY_TIMEOUT_MS:-250}"
export PM_MARKET_REFRESH_TICK_WRITE_LOCK_RETRIES="${PM_MARKET_REFRESH_TICK_WRITE_LOCK_RETRIES:-0}"
export PM_MARKET_REFRESH_TICK_WRITE_LOCK_SLEEP_SEC="${PM_MARKET_REFRESH_TICK_WRITE_LOCK_SLEEP_SEC:-0.05}"

_run_weather_fast_discovery_once

market_refresh_timeout_cap=$(( PM_SCAN_MARKET_REFRESH_TOTAL_TIME_BUDGET_SEC + 12 ))
if [ "${market_refresh_timeout_cap}" -lt 15 ]; then
  market_refresh_timeout_cap=15
fi
if [ "${market_refresh_timeout_cap}" -gt 60 ]; then
  market_refresh_timeout_cap=60
fi
PM_SCAN_MARKET_REFRESH_TIMEOUT_SEC="$(normalize_positive_int_max "${PM_SCAN_MARKET_REFRESH_TIMEOUT_SEC:-${market_refresh_timeout_cap}}" "${market_refresh_timeout_cap}" "${market_refresh_timeout_cap}")"
cmd=(
  env
  PM_SCAN_MAX_EVENTS_PER_RUN="${PM_MARKET_REFRESH_MAX_EVENTS_PER_RUN}"
  PM_SCAN_MAX_MATCHED_PER_RUN="${PM_MARKET_REFRESH_MAX_MATCHED_PER_RUN}"
  PYTHONUNBUFFERED=1
  "$PY" "$ROOT/scan_weather_market.py"
  --scan-weather-today
  --tz "America/New_York"
)
if [ -n "${DAYS_ARG:-}" ]; then
  # Intentionally split DAYS_ARG into words such as: --days-ahead 2
  # shellcheck disable=SC2206
  days_parts=( ${DAYS_ARG} )
  cmd+=("${days_parts[@]}")
fi
cmd+=(
  --market-refresh-only
  --no-auto-trade
  --no-update-models
  --ensemble-models "${PM_SCAN_ENSEMBLE_MODELS:-gfs_seamless,icon_seamless,ecmwf_ifs025,nws_forecast}"
  --depth-levels "${PM_SCAN_DEPTH_LEVELS:-5}"
  --write-scope "${PM_ARCHIVE_WRITE_SCOPE}"
  --market-refresh-max-outcomes "${PM_SCAN_MARKET_REFRESH_MAX_OUTCOMES}"
  --market-refresh-time-budget-sec "${PM_SCAN_MARKET_REFRESH_TIME_BUDGET_SEC}"
  --market-refresh-clob-timeout-sec "${PM_SCAN_MARKET_REFRESH_CLOB_BOOK_TIMEOUT_SEC}"
  --market-refresh-clob-retries "${PM_SCAN_MARKET_REFRESH_CLOB_BOOK_RETRIES}"
)
if [ -n "${INSECURE_ARG:-}" ]; then
  cmd+=("${INSECURE_ARG}")
fi
set +e
run_with_timeout "${PM_SCAN_MARKET_REFRESH_TIMEOUT_SEC}" "$LOG" nice -n "$MARKET_REFRESH_NICE" "${cmd[@]}"
scan_rc="$?"
set -e

if [ "${scan_rc}" -ne 0 ]; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') [market_refresh] scan exit=${scan_rc}; coordinator will decide facts materialize" >> "$LOG"
fi

: "${PM_FACTS_MATERIALIZE_AFTER_SCAN:=0}"
# Keep the minute-refresh lock through facts materialization by default. The
# fact layer is the read-side SSOT for auto-trade/UI; allowing the next refresh
# cycle to overlap here can leave fresh ticks stranded in event_market_ticks
# while event_fact_latest still exposes stale market_updated_at.
: "${PM_MARKET_REFRESH_RELEASE_LANE_BEFORE_FACTS:=0}"
if [ "${PM_MARKET_REFRESH_RELEASE_LANE_BEFORE_FACTS}" = "1" ]; then
  cleanup_locks
fi
if weather_refresh_facts_materialize_allowed "market_refresh" "$LOG" "market_refresh"; then
  RUN_DATE="$(TZ=America/New_York date '+%Y-%m-%d')"
  : "${PM_MARKET_REFRESH_FACTS_CURRENT_RUN_ONLY:=0}"
  if [ "${PM_MARKET_REFRESH_FACTS_CURRENT_RUN_ONLY}" = "1" ]; then
    FACTS_END_DATE="$RUN_DATE"
    FACTS_DAYS=1
  else
    facts_window="$(_facts_materialize_window_from_scan "$RUN_DATE" "${DAYS_ARG:-}" "${PM_SCAN_INCLUDE_YESTERDAY:-0}")"
    FACTS_END_DATE="$(printf '%s\n' "$facts_window" | sed -n '1p')"
    FACTS_DAYS="$(printf '%s\n' "$facts_window" | sed -n '2p')"
    if [ -z "${FACTS_END_DATE:-}" ] || ! [[ "${FACTS_DAYS:-}" =~ ^[0-9]+$ ]]; then
      FACTS_END_DATE="$RUN_DATE"
      FACTS_DAYS=1
    fi
  fi
  PM_MARKET_REFRESH_FACTS_MATERIALIZE_TIMEOUT_SEC="$(normalize_positive_int_max "${PM_MARKET_REFRESH_FACTS_MATERIALIZE_TIMEOUT_SEC:-90}" 90 180)"
  echo "---- $(date '+%Y-%m-%d %H:%M:%S') facts_materialize start (run_date=${RUN_DATE} end=${FACTS_END_DATE} days=${FACTS_DAYS} timeout=${PM_MARKET_REFRESH_FACTS_MATERIALIZE_TIMEOUT_SEC}s current_run_only=${PM_MARKET_REFRESH_FACTS_CURRENT_RUN_ONLY}) ----" >> "$LOG"
  facts_env=(
    env
    PM_FACTS_MATERIALIZE_CURRENT_RUN_ONLY="${PM_MARKET_REFRESH_FACTS_CURRENT_RUN_ONLY}"
  )
  if [ -n "${SCAN_START_UTC:-}" ]; then
    facts_env+=(PM_FACTS_MATERIALIZE_SNAPSHOT_SINCE="${SCAN_START_UTC}")
  fi
  set +e
  run_with_timeout "$PM_MARKET_REFRESH_FACTS_MATERIALIZE_TIMEOUT_SEC" "$LOG" \
    "${facts_env[@]}" "$PY" "$ROOT/ui/server.py" --materialize-facts --facts-date "$FACTS_END_DATE" --facts-days "$FACTS_DAYS" --write-scope "${PM_ARCHIVE_WRITE_SCOPE}"
  facts_rc="$?"
  set -e
  if [ "$facts_rc" -ne 0 ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [market_refresh] facts_materialize failed rc=${facts_rc}" >> "$LOG"
  fi
fi

_kick_model_scan_if_new_market_signal

: "${PM_MARKET_REFRESH_STRICT_EXIT:=0}"
if [ "${scan_rc}" -ne 0 ] && [ "${PM_MARKET_REFRESH_STRICT_EXIT}" = "1" ]; then
  exit "${scan_rc}"
fi
