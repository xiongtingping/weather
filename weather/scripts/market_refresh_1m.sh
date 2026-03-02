#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ -f "$ROOT/.env" ]; then
  set -a
  . "$ROOT/.env"
  set +a
fi

: "${PM_NOFILE_SOFT:=8192}"
if [[ "$PM_NOFILE_SOFT" =~ ^[0-9]+$ ]]; then
  ulimit -Sn "$PM_NOFILE_SOFT" 2>/dev/null || true
fi

. "$ROOT/scripts/lib/run_with_timeout.sh"

PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') [market_refresh] missing .venv python at $PY" >> "$ROOT/data/market_refresh.log"
  exit 1
fi

# Auto-trade running: do NOT stop market refresh. Instead, focus refresh on Top OK candidates
# (priority-only mode) to keep quotes fresh for execution with minimal fan-out.
AUTO_LOCK_DIR="$ROOT/data/auto_trade_runtime.lock"
AUTO_LOCK_META="$AUTO_LOCK_DIR/meta.json"
: "${PM_AUTO_TRADE_RUNTIME_LOCK_STALE_SEC:=1800}"
AUTO_TRADE_ACTIVE=0
if [ -d "$AUTO_LOCK_DIR" ]; then
  lock_info="$(AUTO_META="$AUTO_LOCK_META" /usr/bin/python3 - <<'PY' 2>/dev/null || true
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
  /usr/bin/python3 - "$db_path" <<'PY' 2>/dev/null || true
import sqlite3
import sys
from datetime import datetime, timezone

db_path = sys.argv[1]
try:
    conn = sqlite3.connect(db_path, timeout=1.0)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        WITH latest AS (
          SELECT slug, event_date, MAX(created_at) AS ts
          FROM event_market_ticks
          WHERE event_date IS NOT NULL
            AND event_date != ''
            AND event_date >= date('now', '-3 day')
            AND event_date <= date('now', '+4 day')
          GROUP BY slug, event_date
        )
        SELECT ts FROM latest WHERE ts IS NOT NULL
        """
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

# Avoid concurrent heavy writers by default, but allow emergency refresh when ticks are stale.
if pgrep -f "scan_weather_market.py .*--scan-weather-today.*--model-refresh-only" >/dev/null 2>&1; then
  : "${PM_MARKET_REFRESH_MODEL_SCAN_SKIP_MAX_TICK_AGE_SEC:=120}"
  tick_age_p90_sec="$(_market_tick_p90_age_sec)"
  if [[ "${tick_age_p90_sec:-}" =~ ^[0-9]+$ ]] && [ "$tick_age_p90_sec" -lt "$PM_MARKET_REFRESH_MODEL_SCAN_SKIP_MAX_TICK_AGE_SEC" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [market_refresh] model_scan running, skip (policy tick_age_p90=${tick_age_p90_sec}s<th=${PM_MARKET_REFRESH_MODEL_SCAN_SKIP_MAX_TICK_AGE_SEC}s)" >> "$ROOT/data/market_refresh.log"
    exit 0
  fi
  echo "$(date '+%Y-%m-%d %H:%M:%S') [market_refresh] model_scan running but ticks stale, bypass skip (tick_age_p90=${tick_age_p90_sec:-na}s th=${PM_MARKET_REFRESH_MODEL_SCAN_SKIP_MAX_TICK_AGE_SEC}s)" >> "$ROOT/data/market_refresh.log"
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
: "${PM_DB_WRITER_LOCK_WAIT_SEC:=150}"
: "${PM_DB_WRITER_LOCK_POLL_SEC:=1}"
if ! [[ "${PM_DB_WRITER_LOCK_WAIT_SEC}" =~ ^[0-9]+$ ]] || [ "${PM_DB_WRITER_LOCK_WAIT_SEC}" -lt 0 ]; then
  PM_DB_WRITER_LOCK_WAIT_SEC=150
fi
if ! [[ "${PM_DB_WRITER_LOCK_POLL_SEC}" =~ ^[0-9]+$ ]] || [ "${PM_DB_WRITER_LOCK_POLL_SEC}" -lt 1 ]; then
  PM_DB_WRITER_LOCK_POLL_SEC=1
fi
db_waited=0
db_wait_logged=0
while ! mkdir "$DB_LOCK_DIR" 2>/dev/null; do
  db_pid=""
  db_owner=""
  if [ -f "$DB_LOCK_PID_FILE" ]; then
    db_pid="$(cat "$DB_LOCK_PID_FILE" 2>/dev/null || true)"
  fi
  if [ -f "$DB_LOCK_OWNER_FILE" ]; then
    db_owner="$(cat "$DB_LOCK_OWNER_FILE" 2>/dev/null || true)"
  fi
  if _pid_running "$db_pid"; then
    if [ "$db_wait_logged" -eq 0 ]; then
      echo "$(date '+%Y-%m-%d %H:%M:%S') [market_refresh] db_writer busy, wait (owner=${db_owner:-unknown} pid=$db_pid max=${PM_DB_WRITER_LOCK_WAIT_SEC}s)" >> "$ROOT/data/market_refresh.log"
      db_wait_logged=1
    fi
    if [ "$db_waited" -ge "$PM_DB_WRITER_LOCK_WAIT_SEC" ]; then
      echo "$(date '+%Y-%m-%d %H:%M:%S') [market_refresh] db_writer lock timeout, skip (owner=${db_owner:-unknown} pid=$db_pid waited=${db_waited}s)" >> "$ROOT/data/market_refresh.log"
      rm -rf "$LOCK_DIR" 2>/dev/null || true
      exit 0
    fi
    sleep "$PM_DB_WRITER_LOCK_POLL_SEC"
    db_waited=$((db_waited + PM_DB_WRITER_LOCK_POLL_SEC))
    continue
  fi
  rm -rf "$DB_LOCK_DIR" 2>/dev/null || true
done
if [ "$db_waited" -gt 0 ]; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') [market_refresh] db_writer lock acquired after ${db_waited}s wait" >> "$ROOT/data/market_refresh.log"
fi
echo "$$" > "$DB_LOCK_PID_FILE"
echo "market_refresh" > "$DB_LOCK_OWNER_FILE" 2>/dev/null || true
trap 'rm -rf "$LOCK_DIR" "$DB_LOCK_DIR" 2>/dev/null || true' EXIT

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
# Skip score composer in minute-level refresh; model_scan will update facts on schedule.
export PM_MARKET_REFRESH_COMPOSE_SCORE=0
export PM_FACTS_MATERIALIZE_AFTER_SCAN=0
export PM_SCAN_GAMMA_MAX_PAGES
export PM_DB_CONNECT_TIMEOUT_SEC="${PM_DB_CONNECT_TIMEOUT_SEC:-5}"
export PM_DB_BUSY_TIMEOUT_MS="${PM_DB_BUSY_TIMEOUT_MS:-5000}"

# Temperature markets have many outcomes; keep defaults conservative but allow short burst mode for stale ticks.
: "${PM_MARKET_REFRESH_MAX_EVENTS_PER_RUN:=4}"
: "${PM_MARKET_REFRESH_MAX_EVENTS_CAP:=6}"
: "${PM_MARKET_REFRESH_MAX_MATCHED_PER_RUN:=${PM_MARKET_REFRESH_MAX_EVENTS_PER_RUN}}"
if ! [[ "${PM_MARKET_REFRESH_MAX_EVENTS_CAP}" =~ ^[0-9]+$ ]] || [ "${PM_MARKET_REFRESH_MAX_EVENTS_CAP}" -lt 1 ]; then
  PM_MARKET_REFRESH_MAX_EVENTS_CAP=6
fi
if ! [[ "${PM_MARKET_REFRESH_MAX_EVENTS_PER_RUN}" =~ ^[0-9]+$ ]] || [ "${PM_MARKET_REFRESH_MAX_EVENTS_PER_RUN}" -lt 1 ]; then
  PM_MARKET_REFRESH_MAX_EVENTS_PER_RUN=4
fi
if [ "${PM_MARKET_REFRESH_MAX_EVENTS_PER_RUN}" -gt "${PM_MARKET_REFRESH_MAX_EVENTS_CAP}" ]; then
  PM_MARKET_REFRESH_MAX_EVENTS_PER_RUN="${PM_MARKET_REFRESH_MAX_EVENTS_CAP}"
fi
if ! [[ "${PM_MARKET_REFRESH_MAX_MATCHED_PER_RUN}" =~ ^[0-9]+$ ]] || [ "${PM_MARKET_REFRESH_MAX_MATCHED_PER_RUN}" -lt 1 ]; then
  PM_MARKET_REFRESH_MAX_MATCHED_PER_RUN="${PM_MARKET_REFRESH_MAX_EVENTS_PER_RUN}"
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
: "${PM_MARKET_REFRESH_PRIORITY_CANDIDATES_PER_RUN:=2}"
: "${PM_MARKET_REFRESH_PRIORITY_OTHERS_PER_RUN:=0}"
: "${PM_MARKET_REFRESH_PRIORITY_POSITION_TOPN:=16}"
: "${PM_MARKET_REFRESH_PRIORITY_OPEN_ORDER_TOPN:=16}"
: "${PM_MARKET_REFRESH_PRIORITY_POSITION_LOOKBACK_DAYS:=${PM_SCAN_FAST_DAYS_AHEAD}}"
if ! [[ "${PM_MARKET_REFRESH_PRIORITY_POSITION_LOOKBACK_DAYS}" =~ ^-?[0-9]+$ ]] || [ "${PM_MARKET_REFRESH_PRIORITY_POSITION_LOOKBACK_DAYS}" -lt 0 ]; then
  PM_MARKET_REFRESH_PRIORITY_POSITION_LOOKBACK_DAYS="${PM_SCAN_FAST_DAYS_AHEAD}"
fi
export PM_SCAN_PRIORITY_CANDIDATES_PER_RUN="${PM_MARKET_REFRESH_PRIORITY_CANDIDATES_PER_RUN}"
export PM_SCAN_PRIORITY_OTHERS_PER_RUN="${PM_MARKET_REFRESH_PRIORITY_OTHERS_PER_RUN}"
export PM_SCAN_PRIORITY_POSITION_TOPN="${PM_MARKET_REFRESH_PRIORITY_POSITION_TOPN}"
export PM_SCAN_PRIORITY_OPEN_ORDER_TOPN="${PM_MARKET_REFRESH_PRIORITY_OPEN_ORDER_TOPN}"
export PM_SCAN_PRIORITY_POSITION_LOOKBACK_DAYS="${PM_MARKET_REFRESH_PRIORITY_POSITION_LOOKBACK_DAYS}"
# Tail refresh: when the oldest market tick becomes too stale, include it in the fixed set to keep
# the UI freshness SSOT from accumulating a long tail.
: "${PM_MARKET_REFRESH_TAIL_TOPN:=1}"
# Chase the tail early enough to keep SSOT freshness meaningful for the active weather events set.
# This value is intentionally closer to `PM_HEALTH_SCAN_STALE_SEC` (45m) than the max-stale hard error.
: "${PM_MARKET_REFRESH_TAIL_MIN_AGE_SEC:=2700}" # 45m
# Facts stale-tail refresh: if facts are stale but ticks are still fresh, include those slugs too.
: "${PM_MARKET_REFRESH_FACT_TAIL_TOPN:=6}"
: "${PM_MARKET_REFRESH_FACT_TAIL_MIN_AGE_SEC:=2700}" # 45m
export PM_SCAN_PRIORITY_INCLUDE_TAIL=1
export PM_SCAN_PRIORITY_TAIL_TOPN="${PM_MARKET_REFRESH_TAIL_TOPN}"
export PM_SCAN_PRIORITY_TAIL_MIN_AGE_SEC="${PM_MARKET_REFRESH_TAIL_MIN_AGE_SEC}"
export PM_SCAN_PRIORITY_INCLUDE_FACT_TAIL=1
export PM_SCAN_PRIORITY_FACT_TAIL_TOPN="${PM_MARKET_REFRESH_FACT_TAIL_TOPN}"
export PM_SCAN_PRIORITY_FACT_TAIL_MIN_AGE_SEC="${PM_MARKET_REFRESH_FACT_TAIL_MIN_AGE_SEC}"
: "${PM_MARKET_REFRESH_RESERVE_TAIL_SLOT:=1}"
export PM_SCAN_PRIORITY_RESERVE_TAIL_SLOT="${PM_MARKET_REFRESH_RESERVE_TAIL_SLOT}"
# Place reserved tail slug at slot 0 so timeout-prone runs still process stale-tail first.
export PM_SCAN_PRIORITY_RESERVE_TAIL_PREPEND=1

# Bound fan-out: temperature markets can have many outcomes, and fetching all orderbooks can time out.
: "${PM_SCAN_MARKET_REFRESH_MAX_OUTCOMES:=4}"
: "${PM_SCAN_MARKET_REFRESH_ALL_OUTCOMES_MAX:=10}"
: "${PM_SCAN_MARKET_REFRESH_TIME_BUDGET_SEC:=6}"
if ! [[ "${PM_SCAN_MARKET_REFRESH_TIME_BUDGET_SEC}" =~ ^[0-9]+$ ]] || [ "${PM_SCAN_MARKET_REFRESH_TIME_BUDGET_SEC}" -lt 1 ]; then
  PM_SCAN_MARKET_REFRESH_TIME_BUDGET_SEC=6
fi
: "${PM_MARKET_REFRESH_TIME_BUDGET_CAP_SEC:=10}"
if ! [[ "${PM_MARKET_REFRESH_TIME_BUDGET_CAP_SEC}" =~ ^[0-9]+$ ]] || [ "${PM_MARKET_REFRESH_TIME_BUDGET_CAP_SEC}" -lt 1 ]; then
  PM_MARKET_REFRESH_TIME_BUDGET_CAP_SEC=10
fi
if [ "${PM_SCAN_MARKET_REFRESH_TIME_BUDGET_SEC}" -gt "${PM_MARKET_REFRESH_TIME_BUDGET_CAP_SEC}" ]; then
  PM_SCAN_MARKET_REFRESH_TIME_BUDGET_SEC="${PM_MARKET_REFRESH_TIME_BUDGET_CAP_SEC}"
fi

: "${PM_MARKET_REFRESH_BURST_ENABLED:=1}"
: "${PM_MARKET_REFRESH_BURST_TICK_AGE_SEC:=2700}"
: "${PM_MARKET_REFRESH_BURST_MAX_EVENTS_PER_RUN:=${PM_MARKET_REFRESH_MAX_EVENTS_PER_RUN}}"
: "${PM_MARKET_REFRESH_BURST_MAX_MATCHED_PER_RUN:=${PM_MARKET_REFRESH_BURST_MAX_EVENTS_PER_RUN}}"
: "${PM_MARKET_REFRESH_BURST_TIME_BUDGET_SEC:=8}"
: "${PM_MARKET_REFRESH_BURST_PRIORITY_CANDIDATES_PER_RUN:=${PM_MARKET_REFRESH_PRIORITY_CANDIDATES_PER_RUN}}"
: "${PM_MARKET_REFRESH_BURST_PRIORITY_POSITION_TOPN:=${PM_MARKET_REFRESH_PRIORITY_POSITION_TOPN}}"
: "${PM_MARKET_REFRESH_BURST_PRIORITY_OPEN_ORDER_TOPN:=${PM_MARKET_REFRESH_PRIORITY_OPEN_ORDER_TOPN}}"
: "${PM_MARKET_REFRESH_BURST_ALLOW_AUTO_CANDIDATES:=0}"
: "${PM_MARKET_REFRESH_BURST_MAX_EVENTS_WHEN_AUTO_TRADE:=4}"
if ! [[ "${PM_MARKET_REFRESH_BURST_TICK_AGE_SEC}" =~ ^[0-9]+$ ]] || [ "${PM_MARKET_REFRESH_BURST_TICK_AGE_SEC}" -lt 60 ]; then
  PM_MARKET_REFRESH_BURST_TICK_AGE_SEC=2700
fi

tick_age_p90_sec="$(_market_tick_p90_age_sec)"
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

{
  echo ""
  echo "---- $(date '+%Y-%m-%d %H:%M:%S') market_refresh start ----"
  echo "PY=$PY"
  echo "DAYS=$DAYS_ARG PRIORITY=${PM_SCAN_PRIORITY_ENABLED:-0} P_CAND=${PM_MARKET_REFRESH_PRIORITY_CANDIDATES_PER_RUN} P_OTHERS=${PM_MARKET_REFRESH_PRIORITY_OTHERS_PER_RUN} P_POS_TOPN=${PM_SCAN_PRIORITY_POSITION_TOPN:-na} P_OPEN_TOPN=${PM_SCAN_PRIORITY_OPEN_ORDER_TOPN:-na} P_OPEN=${PM_SCAN_PRIORITY_INCLUDE_OPEN_ORDERS:-0} DEPTH=${PM_SCAN_DEPTH_LEVELS:-5} EVENTS=${PM_MARKET_REFRESH_MAX_EVENTS_PER_RUN} MATCHED=${PM_MARKET_REFRESH_MAX_MATCHED_PER_RUN} BUDGET=${PM_SCAN_MARKET_REFRESH_TIME_BUDGET_SEC}s TICK_P90=${tick_age_p90_sec:-na}s AUTO_TRADE=${AUTO_TRADE_ACTIVE}"
} >> "$LOG"
# Keep market refresh fail-fast under jitter; next minute run will retry.
: "${PM_SCAN_MARKET_REFRESH_CLOB_BOOK_TIMEOUT_SEC:=2}"
: "${PM_SCAN_MARKET_REFRESH_CLOB_BOOK_RETRIES:=1}"
export PM_SCAN_MARKET_REFRESH_MAX_OUTCOMES PM_SCAN_MARKET_REFRESH_TIME_BUDGET_SEC
export PM_SCAN_MARKET_REFRESH_ALL_OUTCOMES_MAX
export PM_SCAN_MARKET_REFRESH_CLOB_BOOK_TIMEOUT_SEC PM_SCAN_MARKET_REFRESH_CLOB_BOOK_RETRIES
export PM_MARKET_REFRESH_CACHE_ONLY="${PM_MARKET_REFRESH_CACHE_ONLY:-1}"

: "${PM_SCAN_MARKET_REFRESH_TIMEOUT_SEC:=240}"
set +e
run_with_timeout "${PM_SCAN_MARKET_REFRESH_TIMEOUT_SEC}" "$LOG" \
  env \
    PM_SCAN_MAX_EVENTS_PER_RUN="${PM_MARKET_REFRESH_MAX_EVENTS_PER_RUN}" \
    PM_SCAN_MAX_MATCHED_PER_RUN="${PM_MARKET_REFRESH_MAX_MATCHED_PER_RUN}" \
    PYTHONUNBUFFERED=1 \
    "$PY" "$ROOT/scan_weather_market.py" \
    --scan-weather-today \
    --tz "America/New_York" \
    $DAYS_ARG \
    --market-refresh-only \
    --no-auto-trade \
    --no-update-models \
    --ensemble-models "${PM_SCAN_ENSEMBLE_MODELS:-gfs_seamless,icon_seamless,ecmwf_ifs025,nws_forecast}" \
    --depth-levels "${PM_SCAN_DEPTH_LEVELS:-5}" \
    --market-refresh-max-outcomes "${PM_SCAN_MARKET_REFRESH_MAX_OUTCOMES}" \
    --market-refresh-time-budget-sec "${PM_SCAN_MARKET_REFRESH_TIME_BUDGET_SEC}" \
    --market-refresh-clob-timeout-sec "${PM_SCAN_MARKET_REFRESH_CLOB_BOOK_TIMEOUT_SEC}" \
    --market-refresh-clob-retries "${PM_SCAN_MARKET_REFRESH_CLOB_BOOK_RETRIES}" \
    $INSECURE_ARG
scan_rc="$?"
set -e

if [ "${scan_rc}" -ne 0 ]; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') [market_refresh] scan exit=${scan_rc}, continue facts materialize" >> "$LOG"
fi

: "${PM_FACTS_MATERIALIZE_AFTER_SCAN:=1}"
if [ "${PM_FACTS_MATERIALIZE_AFTER_SCAN}" = "1" ]; then
  RUN_DATE="$(TZ=America/New_York date '+%Y-%m-%d')"
  echo "---- $(date '+%Y-%m-%d %H:%M:%S') facts_materialize start (run_date=${RUN_DATE}) ----" >> "$LOG"
  "$PY" "$ROOT/ui/server.py" --materialize-facts --facts-date "$RUN_DATE" >> "$LOG" 2>&1 || true
fi

: "${PM_MARKET_REFRESH_STRICT_EXIT:=0}"
if [ "${scan_rc}" -ne 0 ] && [ "${PM_MARKET_REFRESH_STRICT_EXIT}" = "1" ]; then
  exit "${scan_rc}"
fi
