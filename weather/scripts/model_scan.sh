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

FD_DEGRADE_FILE="$ROOT/data/fd_degrade_pause_noncritical.json"
if [ "${PM_FD_DEGRADE_PAUSE_NONCRITICAL:-1}" = "1" ] && [ -f "$FD_DEGRADE_FILE" ]; then
  fd_note="$(python3 - "$FD_DEGRADE_FILE" <<'PY' 2>/dev/null || true
import json, sys, time
from datetime import datetime, timezone
path = sys.argv[1]
try:
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f) or {}
except Exception:
    raise SystemExit(0)
if not isinstance(payload, dict) or not payload.get("active"):
    raise SystemExit(0)
until_text = str(payload.get("active_until") or "").strip()
if until_text:
    try:
        dt = datetime.fromisoformat(until_text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt.timestamp() <= time.time():
            raise SystemExit(0)
    except Exception:
        pass
print(until_text or "active")
PY
)"
  if [ -n "${fd_note:-}" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] paused by fd_degrade until=${fd_note}" >> "$ROOT/data/model_scan.log"
    exit 0
  fi
fi

. "$ROOT/scripts/lib/run_with_timeout.sh"

PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] missing .venv python at $PY" >> "$ROOT/data/model_scan.log"
  exit 1
fi

# Auto-trade running: do NOT stop scanning. Instead, focus the scan on Top OK (auto-trade entry_ok)
# and execution-related slugs (positions/open orders) to keep the pipeline fresh with minimal fan-out.
AUTO_LOCK_DIR="$ROOT/data/auto_trade_runtime.lock"
AUTO_LOCK_META="$AUTO_LOCK_DIR/meta.json"
: "${PM_AUTO_TRADE_RUNTIME_LOCK_STALE_SEC:=1800}"
AUTO_TRADE_ACTIVE=0
if [ -d "$AUTO_LOCK_DIR" ]; then
  age_sec="$(AUTO_META="$AUTO_LOCK_META" /usr/bin/python3 - <<'PY' 2>/dev/null || true
import json, os, datetime
path = (os.environ.get("AUTO_META") or "").strip()
try:
    with open(path, "r", encoding="utf-8") as f:
        meta = json.load(f) or {}
except Exception:
    meta = {}
started = str(meta.get("started_at") or "").strip()
if not started:
    raise SystemExit(0)
try:
    dt = datetime.datetime.fromisoformat(started.replace("Z", "+00:00"))
except Exception:
    raise SystemExit(0)
if dt.tzinfo is None:
    dt = dt.replace(tzinfo=datetime.timezone.utc)
now = datetime.datetime.now(datetime.timezone.utc)
print(int(max(0, (now - dt).total_seconds())))
PY
)"
  if [ -n "${age_sec:-}" ] && [ "${age_sec:-0}" -ge 0 ] && [ "${age_sec:-0}" -lt "${PM_AUTO_TRADE_RUNTIME_LOCK_STALE_SEC}" ]; then
    AUTO_TRADE_ACTIVE=1
  fi
  # Stale lock (UI crashed mid-run); clear it and proceed.
  if [ "${AUTO_TRADE_ACTIVE}" != "1" ]; then
    rm -rf "$AUTO_LOCK_DIR" 2>/dev/null || true
  fi
fi
if [ "${AUTO_TRADE_ACTIVE}" = "1" ]; then
  # Priority scan plan is built from facts (SSOT): entry_ok slugs come from auto_trade_ssot.
  export PM_SCAN_PRIORITY_ENABLED=1
  export PM_SCAN_PRIORITY_INCLUDE_ENTRY_OK=1
  export PM_SCAN_PRIORITY_CANDIDATES_PER_RUN="${PM_SCAN_PRIORITY_CANDIDATES_PER_RUN:-2}"
  : "${PM_MODEL_SCAN_AUTO_TRADE_OTHERS_PER_RUN:=0}"
  export PM_SCAN_PRIORITY_OTHERS_PER_RUN="${PM_MODEL_SCAN_AUTO_TRADE_OTHERS_PER_RUN}"
  export PM_SCAN_PRIORITY_INCLUDE_FACT_TAIL="${PM_SCAN_PRIORITY_INCLUDE_FACT_TAIL:-1}"
  export PM_SCAN_PRIORITY_FACT_TAIL_TOPN="${PM_SCAN_PRIORITY_FACT_TAIL_TOPN:-2}"
  export PM_SCAN_PRIORITY_FACT_TAIL_MIN_AGE_SEC="${PM_SCAN_PRIORITY_FACT_TAIL_MIN_AGE_SEC:-2700}"
  # Keep runs short to avoid the 900s watchdog.
  export PM_MODEL_SCAN_MAX_EVENTS_PER_RUN="${PM_MODEL_SCAN_MAX_EVENTS_PER_RUN:-4}"
  export PM_MODEL_SCAN_MAX_MATCHED_PER_RUN="${PM_MODEL_SCAN_MAX_MATCHED_PER_RUN:-${PM_MODEL_SCAN_MAX_EVENTS_PER_RUN}}"
  # Facts stale bypass: while auto-trade is active, allow a tiny "others" fan-out so long-tail facts can recover.
  : "${PM_MODEL_SCAN_AUTO_TRADE_STALE_BYPASS_SEC:=2700}"
  : "${PM_MODEL_SCAN_AUTO_TRADE_STALE_BYPASS_OTHERS_PER_RUN:=1}"
  stale_tail_count="$(python3 - "$ROOT/data/archive.db" "${PM_MODEL_SCAN_AUTO_TRADE_STALE_BYPASS_SEC}" <<'PY' 2>/dev/null || true
import sqlite3
import sys

db_path = sys.argv[1]
try:
    stale_sec = int(float(sys.argv[2]))
except Exception:
    stale_sec = 2700
if stale_sec < 0:
    stale_sec = 0

try:
    conn = sqlite3.connect(db_path, timeout=1.0)
    conn.row_factory = sqlite3.Row
except Exception:
    raise SystemExit(0)

try:
    row = conn.execute("SELECT MAX(run_date) AS run_date FROM event_fact_history").fetchone()
    run_date = str((row["run_date"] if row else "") or "").strip()
    if not run_date:
        print(0)
        raise SystemExit(0)
    row2 = conn.execute(
        """
        WITH latest AS (
          SELECT slug, event_date, MAX(fact_as_of) AS ts
          FROM event_fact_history
          WHERE run_date = ?
            AND event_date >= ?
          GROUP BY slug, event_date
        )
        SELECT COUNT(*) AS cnt
        FROM latest
        WHERE ts IS NOT NULL
          AND (CAST(strftime('%s','now') AS INTEGER) - CAST(strftime('%s', ts) AS INTEGER)) >= ?
        """,
        (run_date, run_date, stale_sec),
    ).fetchone()
    cnt = int((row2["cnt"] if row2 else 0) or 0)
    print(cnt)
finally:
    conn.close()
PY
)"
  if [[ "${stale_tail_count:-}" =~ ^[0-9]+$ ]] && [ "${stale_tail_count}" -gt 0 ]; then
    export PM_SCAN_PRIORITY_OTHERS_PER_RUN="${PM_MODEL_SCAN_AUTO_TRADE_STALE_BYPASS_OTHERS_PER_RUN}"
    echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] auto_trade stale-bypass enabled (stale_tail=${stale_tail_count}, others=${PM_SCAN_PRIORITY_OTHERS_PER_RUN})" >> "$ROOT/data/model_scan.log"
  fi
  echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] auto_trade running, prioritize Top OK (others=${PM_SCAN_PRIORITY_OTHERS_PER_RUN}, max_events=${PM_MODEL_SCAN_MAX_EVENTS_PER_RUN})" >> "$ROOT/data/model_scan.log"
fi

# When auto-trade queue is heavily backlogged, let entry worker clear first.
# Avoid pausing model_scan for a single long-running/rolling job, which can starve q updates.
if [ "${PM_MODEL_SCAN_PAUSE_WHEN_AUTO_JOB_ACTIVE:-1}" = "1" ] && [ -f "$ROOT/data/archive.db" ]; then
  : "${PM_MODEL_SCAN_PAUSE_AUTO_JOB_MIN_ACTIVE:=3}"
  auto_job_active="$(python3 - "$ROOT/data/archive.db" "$(TZ=America/New_York date '+%Y-%m-%d')" <<'PY' 2>/dev/null || true
import sqlite3
import sys

db_path = sys.argv[1]
run_date = sys.argv[2]
try:
    conn = sqlite3.connect(db_path, timeout=1.0)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT COUNT(*)
        FROM auto_trade_jobs
        WHERE run_date = ?
          AND state IN ('queued','running')
        """,
        (run_date,),
    )
    row = cur.fetchone()
    print(int((row[0] if row else 0) or 0))
finally:
    try:
        conn.close()
    except Exception:
        pass
PY
)"
  if ! [[ "${PM_MODEL_SCAN_PAUSE_AUTO_JOB_MIN_ACTIVE:-}" =~ ^[0-9]+$ ]]; then
    PM_MODEL_SCAN_PAUSE_AUTO_JOB_MIN_ACTIVE=3
  fi
  if [[ "${auto_job_active:-}" =~ ^[0-9]+$ ]] && [ "${auto_job_active}" -ge "${PM_MODEL_SCAN_PAUSE_AUTO_JOB_MIN_ACTIVE}" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] paused for auto_trade queue active_jobs=${auto_job_active} threshold=${PM_MODEL_SCAN_PAUSE_AUTO_JOB_MIN_ACTIVE}" >> "$ROOT/data/model_scan.log"
    exit 0
  fi
fi

# If Open-Meteo 429 cooldown is active, reduce non-priority fan-out to avoid
# re-triggering rate limits before cooldown windows finish.
: "${PM_MODEL_SCAN_OPEN_METEO_COOLDOWN_ACTIVE_THRESHOLD:=1}"
: "${PM_MODEL_SCAN_OPEN_METEO_COOLDOWN_OTHERS_PER_RUN:=0}"
: "${PM_MODEL_SCAN_OPEN_METEO_COOLDOWN_MAX_EVENTS_PER_RUN:=4}"
: "${PM_MODEL_SCAN_OPEN_METEO_COOLDOWN_CACHE_ONLY:=1}"
MODEL_CACHE_ONLY_ARG=""
open_meteo_active_429="$(python3 - "$ROOT/data/archive.db" <<'PY' 2>/dev/null || true
import sqlite3
import sys
from datetime import datetime, timezone

db_path = sys.argv[1]
try:
    conn = sqlite3.connect(db_path, timeout=1.0)
    cur = conn.cursor()
    now_iso = datetime.now(timezone.utc).isoformat()
    cur.execute(
        """
        SELECT COUNT(*) FROM model_request_cooldown
        WHERE reason = 'open_meteo_429'
          AND cooldown_until > ?
        """,
        (now_iso,),
    )
    row = cur.fetchone()
    print(int((row[0] if row else 0) or 0))
finally:
    try:
        conn.close()
    except Exception:
        pass
PY
)"
if [[ "${open_meteo_active_429:-}" =~ ^[0-9]+$ ]] && [ "${open_meteo_active_429}" -ge "${PM_MODEL_SCAN_OPEN_METEO_COOLDOWN_ACTIVE_THRESHOLD}" ]; then
  export PM_SCAN_PRIORITY_ENABLED=1
  export PM_SCAN_PRIORITY_OTHERS_PER_RUN="${PM_MODEL_SCAN_OPEN_METEO_COOLDOWN_OTHERS_PER_RUN}"
  if [ "${PM_MODEL_SCAN_OPEN_METEO_COOLDOWN_CACHE_ONLY}" = "1" ]; then
    MODEL_CACHE_ONLY_ARG="--model-cache-only"
  fi
  if [[ "${PM_MODEL_SCAN_MAX_EVENTS_PER_RUN:-0}" =~ ^[0-9]+$ ]] && [ "${PM_MODEL_SCAN_MAX_EVENTS_PER_RUN:-0}" -gt "${PM_MODEL_SCAN_OPEN_METEO_COOLDOWN_MAX_EVENTS_PER_RUN}" ]; then
    export PM_MODEL_SCAN_MAX_EVENTS_PER_RUN="${PM_MODEL_SCAN_OPEN_METEO_COOLDOWN_MAX_EVENTS_PER_RUN}"
  fi
  if [[ "${PM_MODEL_SCAN_MAX_MATCHED_PER_RUN:-0}" =~ ^[0-9]+$ ]] && [ "${PM_MODEL_SCAN_MAX_MATCHED_PER_RUN:-0}" -gt "${PM_MODEL_SCAN_MAX_EVENTS_PER_RUN:-0}" ]; then
    export PM_MODEL_SCAN_MAX_MATCHED_PER_RUN="${PM_MODEL_SCAN_MAX_EVENTS_PER_RUN}"
  fi
  echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] open_meteo cooldown active=${open_meteo_active_429}, throttle fanout (others=${PM_SCAN_PRIORITY_OTHERS_PER_RUN}, max_events=${PM_MODEL_SCAN_MAX_EVENTS_PER_RUN}, cache_only=${PM_MODEL_SCAN_OPEN_METEO_COOLDOWN_CACHE_ONLY})" >> "$ROOT/data/model_scan.log"
fi

# Avoid concurrent heavy writers: market_refresh and model_scan both write SQLite snapshots.
MARKET_LOCK_DIR="$ROOT/data/market_refresh.lock"
MARKET_PID_FILE="$MARKET_LOCK_DIR/pid"
_market_pid_running() {
  local pid="${1:-}"
  if [ -z "$pid" ]; then
    return 1
  fi
  if ! kill -0 "$pid" 2>/dev/null; then
    return 1
  fi
  local cmdline
  cmdline="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  if [[ "$cmdline" == *"scan_weather_market.py"* && "$cmdline" == *"--scan-weather-today"* && "$cmdline" == *"--market-refresh-only"* ]]; then
    return 0
  fi
  return 1
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
market_pid=""
if [ -f "$MARKET_PID_FILE" ]; then
  market_pid="$(cat "$MARKET_PID_FILE" 2>/dev/null || true)"
fi
if _market_pid_running "$market_pid" || pgrep -f "scan_weather_market.py .*--scan-weather-today.*--market-refresh-only" >/dev/null 2>&1; then
  : "${PM_MARKET_REFRESH_MODEL_SCAN_SKIP_MAX_TICK_AGE_SEC:=120}"
  : "${PM_MODEL_SCAN_MARKET_REFRESH_SKIP_MAX_TICK_AGE_SEC:=${PM_MARKET_REFRESH_MODEL_SCAN_SKIP_MAX_TICK_AGE_SEC}}"
  tick_age_p90_sec="$(_market_tick_p90_age_sec)"
  if [[ "${tick_age_p90_sec:-}" =~ ^[0-9]+$ ]] && [[ "${PM_MODEL_SCAN_MARKET_REFRESH_SKIP_MAX_TICK_AGE_SEC}" =~ ^[0-9]+$ ]] && [ "${tick_age_p90_sec}" -lt "${PM_MODEL_SCAN_MARKET_REFRESH_SKIP_MAX_TICK_AGE_SEC}" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] market_refresh running, skip (policy tick_age_p90=${tick_age_p90_sec}s<th=${PM_MODEL_SCAN_MARKET_REFRESH_SKIP_MAX_TICK_AGE_SEC}s)" >> "$ROOT/data/model_scan.log"
    exit 0
  fi
  echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] market_refresh running but ticks stale/unknown, bypass skip (tick_age_p90=${tick_age_p90_sec:-na}s th=${PM_MODEL_SCAN_MARKET_REFRESH_SKIP_MAX_TICK_AGE_SEC}s)" >> "$ROOT/data/model_scan.log"
fi

# Prevent overlapping scans; clear stale lock if no scan-weather process is running.
LOCK_DIR="$ROOT/data/model_scan.lock"
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
  if [[ "$cmdline" == *"scan_weather_market.py"* && "$cmdline" == *"--scan-weather-today"* ]]; then
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
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  lock_pid=""
  if [ -f "$LOCK_PID_FILE" ]; then
    lock_pid="$(cat "$LOCK_PID_FILE" 2>/dev/null || true)"
  fi
  if _scan_pid_running "$lock_pid"; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] already running, skip (pid=$lock_pid)" >> "$ROOT/data/model_scan.log"
    exit 0
  fi
  if pgrep -f "scan_weather_market.py .*--scan-weather-today" >/dev/null 2>&1; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] already running, skip (matched scan process)" >> "$ROOT/data/model_scan.log"
    exit 0
  fi
  echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] stale lock cleared" >> "$ROOT/data/model_scan.log"
  rm -rf "$LOCK_DIR"
  mkdir "$LOCK_DIR" 2>/dev/null || {
    echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] unable to acquire lock, skip" >> "$ROOT/data/model_scan.log"
    exit 0
  }
fi
echo "$$" > "$LOCK_PID_FILE"
: "${PM_DB_WRITER_LOCK_WAIT_SEC:=90}"
: "${PM_DB_WRITER_LOCK_POLL_SEC:=1}"
: "${PM_MODEL_SCAN_DB_LOCK_ENABLED:=0}"
if ! [[ "${PM_DB_WRITER_LOCK_WAIT_SEC}" =~ ^[0-9]+$ ]] || [ "${PM_DB_WRITER_LOCK_WAIT_SEC}" -lt 0 ]; then
  PM_DB_WRITER_LOCK_WAIT_SEC=90
fi
if ! [[ "${PM_DB_WRITER_LOCK_POLL_SEC}" =~ ^[0-9]+$ ]] || [ "${PM_DB_WRITER_LOCK_POLL_SEC}" -lt 1 ]; then
  PM_DB_WRITER_LOCK_POLL_SEC=1
fi
if [ "${PM_MODEL_SCAN_DB_LOCK_ENABLED}" != "1" ]; then
  PM_MODEL_SCAN_DB_LOCK_ENABLED=0
fi
db_lock_acquired=0
cleanup_locks() {
  rm -rf "$LOCK_DIR" 2>/dev/null || true
  if [ "$db_lock_acquired" -eq 1 ]; then
    rm -rf "$DB_LOCK_DIR" 2>/dev/null || true
  fi
}
trap cleanup_locks EXIT
if [ "${PM_MODEL_SCAN_DB_LOCK_ENABLED}" = "1" ]; then
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
        echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] db_writer busy, wait (owner=${db_owner:-unknown} pid=$db_pid max=${PM_DB_WRITER_LOCK_WAIT_SEC}s)" >> "$ROOT/data/model_scan.log"
        db_wait_logged=1
      fi
      if [ "$db_waited" -ge "$PM_DB_WRITER_LOCK_WAIT_SEC" ]; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] db_writer lock timeout, skip (owner=${db_owner:-unknown} pid=$db_pid waited=${db_waited}s)" >> "$ROOT/data/model_scan.log"
        exit 0
      fi
      sleep "$PM_DB_WRITER_LOCK_POLL_SEC"
      db_waited=$((db_waited + PM_DB_WRITER_LOCK_POLL_SEC))
      continue
    fi
    rm -rf "$DB_LOCK_DIR" 2>/dev/null || true
  done
  if [ "$db_waited" -gt 0 ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] db_writer lock acquired after ${db_waited}s wait" >> "$ROOT/data/model_scan.log"
  fi
  echo "$$" > "$DB_LOCK_PID_FILE"
  echo "model_scan" > "$DB_LOCK_OWNER_FILE" 2>/dev/null || true
  db_lock_acquired=1
else
  echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] db_writer lock disabled (PM_MODEL_SCAN_DB_LOCK_ENABLED=0)" >> "$ROOT/data/model_scan.log"
fi

# Low-latency mode: prioritize fast updates (30s target) over full-scope refresh.
: "${PM_LOW_LATENCY_MODE:=0}"
if [ "${PM_LOW_LATENCY_MODE}" = "1" ]; then
  # Low-latency is intended to override .env (avoid being "stuck" with large fan-out values).
  export PM_SCAN_GAMMA_TIMEOUT="${PM_SCAN_GAMMA_TIMEOUT:-12}"
  export PM_SCAN_GAMMA_RETRIES="${PM_SCAN_GAMMA_RETRIES:-2}"
  export PM_SCAN_GAMMA_BACKOFF="${PM_SCAN_GAMMA_BACKOFF:-0.6}"
  export PM_SCAN_GAMMA_MAX_PAGES="${PM_SCAN_GAMMA_MAX_PAGES:-60}"
  # Keep low-latency responsive, but do not use an aggressive Open-Meteo cadence:
  # very small spacing can trigger repeated 429 and starve model/facts coverage.
  export PM_SCAN_OPEN_METEO_SPACING_MIN="${PM_SCAN_OPEN_METEO_SPACING_MIN:-1.5}"
  export PM_SCAN_OPEN_METEO_SPACING_MAX="${PM_SCAN_OPEN_METEO_SPACING_MAX:-3.5}"
  export PM_SCAN_OPEN_METEO_429_RETRIES="${PM_SCAN_OPEN_METEO_429_RETRIES:-2}"
  export PM_SCAN_OPEN_METEO_429_MIN_SLEEP="${PM_SCAN_OPEN_METEO_429_MIN_SLEEP:-6.0}"
  export PM_SCAN_OPEN_METEO_429_MAX_SLEEP="${PM_SCAN_OPEN_METEO_429_MAX_SLEEP:-45.0}"
  export PM_SCAN_OPEN_METEO_429_JITTER="${PM_SCAN_OPEN_METEO_429_JITTER:-0.35}"
  export PM_SCAN_OPEN_METEO_CITY_COOLDOWN_SEC="${PM_SCAN_OPEN_METEO_CITY_COOLDOWN_SEC:-1800}"
  export PM_SCAN_OPEN_METEO_GLOBAL_RATE_LIMIT="${PM_SCAN_OPEN_METEO_GLOBAL_RATE_LIMIT:-1}"
  export PM_SCAN_OPEN_METEO_BATCH_MODELS="${PM_SCAN_OPEN_METEO_BATCH_MODELS:-1}"
  export PM_WEATHER_EVENTS_CACHE_MINUTES="${PM_WEATHER_EVENTS_CACHE_MINUTES:-30}"
  # Low-latency defaults keep fan-out tight, but still allow explicit env overrides.
  export PM_SCAN_FAST_DAYS_AHEAD="${PM_SCAN_FAST_DAYS_AHEAD:-0}"
  export PM_SCAN_FULL_DAYS_AHEAD="${PM_SCAN_FULL_DAYS_AHEAD:-0}"
  export PM_SCAN_FULL_INTERVAL_MINUTES="${PM_SCAN_FULL_INTERVAL_MINUTES:-0}"
  # Model scan wrapper uses PM_MODEL_SCAN_*; set those directly.
  export PM_MODEL_SCAN_MAX_EVENTS_PER_RUN="${PM_MODEL_SCAN_MAX_EVENTS_PER_RUN:-6}"
  export PM_MODEL_SCAN_MAX_MATCHED_PER_RUN="${PM_MODEL_SCAN_MAX_MATCHED_PER_RUN:-${PM_MODEL_SCAN_MAX_EVENTS_PER_RUN}}"
  # Avoid per-event Gamma detail fan-out; rely on cached tokens/snapshots.
  export PM_SCAN_FETCH_EVENT_DETAIL="0"
  export PM_SCAN_ROTATE_EVENTS="${PM_SCAN_ROTATE_EVENTS:-1}"
  export PM_SCAN_PRIORITY_ENABLED="1"
  export PM_SCAN_PRIORITY_TOPN="${PM_SCAN_PRIORITY_TOPN:-8}"
  export PM_SCAN_PRIORITY_LOOKAHEAD_DAYS="${PM_SCAN_PRIORITY_LOOKAHEAD_DAYS:-2}"
  # Keep priority batch small so a run finishes well within the 900s watchdog.
  export PM_SCAN_PRIORITY_CANDIDATES_PER_RUN="2"
  # When no Top OK exists, scanning only "fixed" slugs causes the rest of today's events to go stale,
  # which makes facts health misleading and can delay discovery of new opportunities.
  export PM_SCAN_PRIORITY_OTHERS_PER_RUN="${PM_SCAN_PRIORITY_OTHERS_PER_RUN:-2}"
  export PM_SCAN_PRIORITY_INCLUDE_OPEN_ORDERS="1"
  # Tail-chase stale events (by market tick age) so we don't accumulate a long stale tail under priority mode.
  export PM_SCAN_PRIORITY_INCLUDE_TAIL="${PM_SCAN_PRIORITY_INCLUDE_TAIL:-1}"
  export PM_SCAN_PRIORITY_TAIL_TOPN="${PM_SCAN_PRIORITY_TAIL_TOPN:-1}"
  export PM_SCAN_PRIORITY_TAIL_MIN_AGE_SEC="${PM_SCAN_PRIORITY_TAIL_MIN_AGE_SEC:-2700}" # 45m
  export PM_SCAN_PRIORITY_INCLUDE_FACT_TAIL="${PM_SCAN_PRIORITY_INCLUDE_FACT_TAIL:-1}"
  export PM_SCAN_PRIORITY_FACT_TAIL_TOPN="${PM_SCAN_PRIORITY_FACT_TAIL_TOPN:-2}"
  export PM_SCAN_PRIORITY_FACT_TAIL_MIN_AGE_SEC="${PM_SCAN_PRIORITY_FACT_TAIL_MIN_AGE_SEC:-2700}"
  export PM_SCAN_RETRY_MAX="${PM_SCAN_RETRY_MAX:-1}"
  export PM_SCAN_AUTO_DIAG="0"
  export PM_SCAN_DEPTH_LEVELS="${PM_SCAN_DEPTH_LEVELS:-3}"
  export PM_TRADE_SYNC_AFTER_SCAN="0"
  export PM_CACHE_TTL_NWS_SEC="${PM_CACHE_TTL_NWS_SEC:-900}"
  export PM_CACHE_TTL_ENSEMBLE_SEC="${PM_CACHE_TTL_ENSEMBLE_SEC:-7200}"
  export PM_MODEL_BACKGROUND_REFRESH_MINUTES="${PM_MODEL_BACKGROUND_REFRESH_MINUTES:-20}"
  export PM_MODEL_WINDOW_REFRESH_MINUTES="${PM_MODEL_WINDOW_REFRESH_MINUTES:-2}"
  export PM_SCAN_MODEL_REFRESH_INTERVAL_MINUTES="${PM_SCAN_MODEL_REFRESH_INTERVAL_MINUTES:-20}"
fi

# Force proxy for Polymarket/Gamma and allow insecure SSL fallback when needed.
FORCE_PROXY_HOSTS_DEFAULT="polymarket.com,gamma-api.polymarket.com,clob.polymarket.com"
if [ -z "${PM_SCAN_FORCE_PROXY_HOSTS:-}" ]; then
  export PM_SCAN_FORCE_PROXY_HOSTS="$FORCE_PROXY_HOSTS_DEFAULT"
else
  export PM_SCAN_FORCE_PROXY_HOSTS="${PM_SCAN_FORCE_PROXY_HOSTS},${FORCE_PROXY_HOSTS_DEFAULT}"
fi
export PM_SCAN_ALLOW_INSECURE_SSL=1
: "${PM_SCAN_GAMMA_TIMEOUT:=12}"
: "${PM_SCAN_GAMMA_RETRIES:=2}"
: "${PM_SCAN_GAMMA_BACKOFF:=0.6}"
: "${PM_SCAN_GAMMA_MAX_PAGES:=60}"
export PM_SCAN_GAMMA_TIMEOUT PM_SCAN_GAMMA_RETRIES PM_SCAN_GAMMA_BACKOFF PM_SCAN_GAMMA_MAX_PAGES
export PM_SCAN_FETCH_EVENT_DETAIL="${PM_SCAN_FETCH_EVENT_DETAIL:-0}"
export PM_SCAN_OPEN_METEO_TIMEOUT_SEC="${PM_SCAN_OPEN_METEO_TIMEOUT_SEC:-10}"
export PM_SCAN_OPEN_METEO_RETRIES="${PM_SCAN_OPEN_METEO_RETRIES:-2}"
export PM_SCAN_OPEN_METEO_429_RETRIES="${PM_SCAN_OPEN_METEO_429_RETRIES:-2}"
export PM_DB_CONNECT_TIMEOUT_SEC="${PM_DB_CONNECT_TIMEOUT_SEC:-5}"
export PM_DB_BUSY_TIMEOUT_MS="${PM_DB_BUSY_TIMEOUT_MS:-5000}"
export PM_SCAN_OPEN_METEO_TZ_FALLBACKS="${PM_SCAN_OPEN_METEO_TZ_FALLBACKS:-1}"
: "${PM_SCAN_OPEN_METEO_SPACING_MIN:=1.5}"
: "${PM_SCAN_OPEN_METEO_SPACING_MAX:=3.5}"
: "${PM_SCAN_OPEN_METEO_429_RETRIES:=2}"
: "${PM_SCAN_OPEN_METEO_429_MIN_SLEEP:=6.0}"
: "${PM_SCAN_OPEN_METEO_429_MAX_SLEEP:=45.0}"
: "${PM_SCAN_OPEN_METEO_429_JITTER:=0.35}"
: "${PM_SCAN_OPEN_METEO_CITY_COOLDOWN_SEC:=3600}"
: "${PM_SCAN_OPEN_METEO_GLOBAL_RATE_LIMIT:=1}"
: "${PM_SCAN_OPEN_METEO_BATCH_MODELS:=1}"
: "${PM_SCAN_PRIORITY_POSITION_LOOKBACK_DAYS:=2}"
if ! [[ "${PM_SCAN_PRIORITY_POSITION_LOOKBACK_DAYS}" =~ ^-?[0-9]+$ ]] || [ "${PM_SCAN_PRIORITY_POSITION_LOOKBACK_DAYS}" -lt 0 ]; then
  PM_SCAN_PRIORITY_POSITION_LOOKBACK_DAYS=2
fi
export PM_SCAN_OPEN_METEO_SPACING_MIN PM_SCAN_OPEN_METEO_SPACING_MAX
export PM_SCAN_OPEN_METEO_429_RETRIES PM_SCAN_OPEN_METEO_429_MIN_SLEEP PM_SCAN_OPEN_METEO_429_MAX_SLEEP PM_SCAN_OPEN_METEO_429_JITTER
export PM_SCAN_OPEN_METEO_CITY_COOLDOWN_SEC PM_SCAN_OPEN_METEO_GLOBAL_RATE_LIMIT PM_SCAN_OPEN_METEO_BATCH_MODELS PM_SCAN_OPEN_METEO_TZ_FALLBACKS
export PM_WEATHER_EVENTS_CACHE_MINUTES PM_SCAN_MAX_EVENTS_PER_RUN PM_SCAN_MAX_MATCHED_PER_RUN PM_SCAN_FETCH_EVENT_DETAIL PM_SCAN_ROTATE_EVENTS
export PM_SCAN_PRIORITY_ENABLED PM_SCAN_PRIORITY_TOPN PM_SCAN_PRIORITY_LOOKAHEAD_DAYS PM_SCAN_PRIORITY_CANDIDATES_PER_RUN PM_SCAN_PRIORITY_OTHERS_PER_RUN PM_SCAN_PRIORITY_MIN_EDGE_SCORE PM_SCAN_PRIORITY_INCLUDE_OPEN_ORDERS PM_SCAN_PRIORITY_POSITION_LOOKBACK_DAYS PM_SCAN_ENSEMBLE_MODELS
: "${PM_DISABLE_OPEN_METEO_GEOCODE:=1}"
export PM_DISABLE_OPEN_METEO_GEOCODE

UPDATE_MODELS_ARG="--no-update-models"
MODEL_REFRESH_MODE="cache"
: "${PM_SCAN_MODEL_REFRESH_INTERVAL_MINUTES:=60}"
: "${PM_SCAN_MODEL_REFRESH_STAMP:=$ROOT/data/model_scan.model_refresh_ts}"
if ! [[ "$PM_SCAN_MODEL_REFRESH_INTERVAL_MINUTES" =~ ^-?[0-9]+$ ]]; then
  PM_SCAN_MODEL_REFRESH_INTERVAL_MINUTES=60
fi
if [ "${PM_SCAN_NO_UPDATE_MODELS:-0}" != "1" ]; then
  if [ "$PM_SCAN_MODEL_REFRESH_INTERVAL_MINUTES" -le 0 ]; then
    UPDATE_MODELS_ARG=""
    MODEL_REFRESH_MODE="refresh"
  else
    now_ts="$(date +%s)"
    last_ts=0
    if [ -f "$PM_SCAN_MODEL_REFRESH_STAMP" ]; then
      last_ts="$(cat "$PM_SCAN_MODEL_REFRESH_STAMP" 2>/dev/null || echo 0)"
    fi
    if ! [[ "$last_ts" =~ ^[0-9]+$ ]]; then
      last_ts=0
    fi
    if [ "$last_ts" -gt 0 ] && [ "$last_ts" -gt "$now_ts" ]; then
      last_ts=0
    fi
    if [ $((now_ts - last_ts)) -ge $((PM_SCAN_MODEL_REFRESH_INTERVAL_MINUTES * 60)) ]; then
      UPDATE_MODELS_ARG=""
      MODEL_REFRESH_MODE="refresh"
      echo "$now_ts" > "$PM_SCAN_MODEL_REFRESH_STAMP"
    fi
  fi
fi

MODEL_ONLY=1

: "${PM_SCAN_FAST_DAYS_AHEAD:=2}"
: "${PM_SCAN_FULL_DAYS_AHEAD:=2}"
: "${PM_SCAN_FULL_INTERVAL_MINUTES:=30}"
if ! [[ "${PM_SCAN_FAST_DAYS_AHEAD}" =~ ^-?[0-9]+$ ]]; then
  PM_SCAN_FAST_DAYS_AHEAD=2
fi
if ! [[ "${PM_SCAN_FULL_DAYS_AHEAD}" =~ ^-?[0-9]+$ ]]; then
  PM_SCAN_FULL_DAYS_AHEAD="${PM_SCAN_FAST_DAYS_AHEAD}"
fi
: "${PM_MODEL_SCAN_ALLOW_NEGATIVE_DAYS_AHEAD:=0}"
if [ "${PM_MODEL_SCAN_ALLOW_NEGATIVE_DAYS_AHEAD}" != "1" ] && [ "${PM_SCAN_FAST_DAYS_AHEAD}" -lt 0 ]; then
  PM_SCAN_FAST_DAYS_AHEAD=2
fi
if [ "${PM_MODEL_SCAN_ALLOW_NEGATIVE_DAYS_AHEAD}" != "1" ] && [ "${PM_SCAN_FULL_DAYS_AHEAD}" -lt 0 ]; then
  PM_SCAN_FULL_DAYS_AHEAD="${PM_SCAN_FAST_DAYS_AHEAD}"
fi

SCAN_MODE="fast"
DAYS_ARG="--days-ahead ${PM_SCAN_FAST_DAYS_AHEAD}"
if [ "${PM_SCAN_FULL_INTERVAL_MINUTES}" -gt 0 ]; then
  minute="$(date +%M)"
  minute=$((10#$minute))
  if [ $((minute % PM_SCAN_FULL_INTERVAL_MINUTES)) -eq 0 ]; then
    SCAN_MODE="full"
    DAYS_ARG="--days-ahead ${PM_SCAN_FULL_DAYS_AHEAD}"
  fi
fi

# Pick up macOS system proxy settings only when env proxy is not provided.
HAS_PROXY_ENV=0
if [ -n "${HTTP_PROXY:-}" ] || [ -n "${HTTPS_PROXY:-}" ] || [ -n "${ALL_PROXY:-}" ]; then
  HAS_PROXY_ENV=1
fi
if [ "$HAS_PROXY_ENV" = "0" ] && [ -n "${PM_PROXY_PORTS:-}" ]; then
  FIRST_PORT="$(echo "$PM_PROXY_PORTS" | tr ', ' '\n' | awk 'NF{print $1; exit}')"
  if [ -n "$FIRST_PORT" ]; then
    export HTTP_PROXY="http://127.0.0.1:$FIRST_PORT"
    export HTTPS_PROXY="http://127.0.0.1:$FIRST_PORT"
    HAS_PROXY_ENV=1
  fi
fi
if [ "$HAS_PROXY_ENV" = "0" ] && command -v scutil >/dev/null 2>&1; then
  PROXY_INFO="$(scutil --proxy)"
  HTTP_ENABLE="$(echo "$PROXY_INFO" | awk '/HTTPEnable/{print $3}')"
  HTTPS_ENABLE="$(echo "$PROXY_INFO" | awk '/HTTPSEnable/{print $3}')"
  SOCKS_ENABLE="$(echo "$PROXY_INFO" | awk '/SOCKSEnable/{print $3}')"
  HTTP_PROXY_HOST="$(echo "$PROXY_INFO" | awk '/HTTPProxy/{print $3}')"
  HTTP_PROXY_PORT="$(echo "$PROXY_INFO" | awk '/HTTPPort/{print $3}')"
  HTTPS_PROXY_HOST="$(echo "$PROXY_INFO" | awk '/HTTPSProxy/{print $3}')"
  HTTPS_PROXY_PORT="$(echo "$PROXY_INFO" | awk '/HTTPSPort/{print $3}')"
  SOCKS_PROXY_HOST="$(echo "$PROXY_INFO" | awk '/SOCKSProxy/{print $3}')"
  SOCKS_PROXY_PORT="$(echo "$PROXY_INFO" | awk '/SOCKSPort/{print $3}')"
  if [ "$HTTP_ENABLE" = "1" ] && [ -n "$HTTP_PROXY_HOST" ] && [ -n "$HTTP_PROXY_PORT" ]; then
    export HTTP_PROXY="http://$HTTP_PROXY_HOST:$HTTP_PROXY_PORT"
  fi
  if [ "$HTTPS_ENABLE" = "1" ] && [ -n "$HTTPS_PROXY_HOST" ] && [ -n "$HTTPS_PROXY_PORT" ]; then
    export HTTPS_PROXY="http://$HTTPS_PROXY_HOST:$HTTPS_PROXY_PORT"
  fi
  if [ "$SOCKS_ENABLE" = "1" ] && [ -n "$SOCKS_PROXY_HOST" ] && [ -n "$SOCKS_PROXY_PORT" ]; then
    export ALL_PROXY="socks5://$SOCKS_PROXY_HOST:$SOCKS_PROXY_PORT"
  fi
fi

# Keep proxy settings by default; set PM_SCAN_FORCE_DIRECT=1 to bypass.
if [ "${PM_SCAN_FORCE_DIRECT:-0}" = "1" ]; then
  unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
fi

mkdir -p "$ROOT/data"
LOG="$ROOT/data/model_scan.log"
INSECURE_ARG=""
if [ "${PM_SCAN_INSECURE:-0}" = "1" ]; then
  INSECURE_ARG="--insecure"
fi

: "${PM_SCAN_RETRY_MAX:=3}"
: "${PM_SCAN_RETRY_SLEEP_SEC:=10}"
if ! [[ "$PM_SCAN_RETRY_MAX" =~ ^[0-9]+$ ]]; then
  PM_SCAN_RETRY_MAX=1
fi
if ! [[ "$PM_SCAN_RETRY_SLEEP_SEC" =~ ^[0-9]+$ ]]; then
  PM_SCAN_RETRY_SLEEP_SEC=20
fi

SCAN_AUTO_DIAG_ARG=""
if [ "${PM_SCAN_AUTO_DIAG:-1}" != "1" ]; then
  SCAN_AUTO_DIAG_ARG="--no-auto-diagnose"
fi
: "${PM_SCAN_DEPTH_LEVELS:=5}"
: "${PM_SCAN_ENSEMBLE_MODELS:=gfs_seamless,icon_seamless,ecmwf_ifs025,nws_forecast}"
: "${PM_MODEL_SCAN_MAX_EVENTS_PER_RUN:=6}"
: "${PM_MODEL_SCAN_MAX_MATCHED_PER_RUN:=${PM_MODEL_SCAN_MAX_EVENTS_PER_RUN}}"

attempt=0
while true; do
  attempt=$((attempt + 1))
  {
    echo ""
    echo "---- $(date '+%Y-%m-%d %H:%M:%S') model_scan start ----"
    echo "PY=$PY"
    echo "MODE=$SCAN_MODE DAYS=$DAYS_ARG MODEL_REFRESH=$MODEL_REFRESH_MODE MODEL_ONLY=$MODEL_ONLY LOW_LATENCY=${PM_LOW_LATENCY_MODE:-0} MAX_EVENTS=${PM_MODEL_SCAN_MAX_EVENTS_PER_RUN:-0} MAX_MATCHED=${PM_MODEL_SCAN_MAX_MATCHED_PER_RUN:-0} PRIORITY=${PM_SCAN_PRIORITY_ENABLED:-0} P_CAND=${PM_SCAN_PRIORITY_CANDIDATES_PER_RUN:-0} P_OTHERS=${PM_SCAN_PRIORITY_OTHERS_PER_RUN:-0} P_OPEN=${PM_SCAN_PRIORITY_INCLUDE_OPEN_ORDERS:-0} AUTO_DIAG=${PM_SCAN_AUTO_DIAG:-1} DEPTH=${PM_SCAN_DEPTH_LEVELS:-5} MODELS=${PM_SCAN_ENSEMBLE_MODELS} TTL_NWS=${PM_CACHE_TTL_NWS_SEC:-3600}s TTL_ENS=${PM_CACHE_TTL_ENSEMBLE_SEC:-21600}s BG_REFRESH=${PM_MODEL_BACKGROUND_REFRESH_MINUTES:-45}m WIN_REFRESH=${PM_MODEL_WINDOW_REFRESH_MINUTES:-3}m W_UPDATE_EVERY=${PM_SCAN_MODEL_REFRESH_INTERVAL_MINUTES:-60}m OM_GLOBAL=${PM_SCAN_OPEN_METEO_GLOBAL_RATE_LIMIT:-1} OM_BATCH=${PM_SCAN_OPEN_METEO_BATCH_MODELS:-1} OM_TZ_FB=${PM_SCAN_OPEN_METEO_TZ_FALLBACKS:-1} OM_SPACE=${PM_SCAN_OPEN_METEO_SPACING_MIN:-1.5}-${PM_SCAN_OPEN_METEO_SPACING_MAX:-3.5}s OM_429=${PM_SCAN_OPEN_METEO_429_MIN_SLEEP:-6}-${PM_SCAN_OPEN_METEO_429_MAX_SLEEP:-45}s x${PM_SCAN_OPEN_METEO_429_RETRIES:-2} OM_CD=${PM_SCAN_OPEN_METEO_CITY_COOLDOWN_SEC:-3600}s"
  } >> "$LOG"
  date '+%Y-%m-%d %H:%M:%S' > "$ROOT/data/model_scan.last_start"
  set +e
  # Keep yesterday in the scan window to cover settlement-lag events shown in UI freshness alerts.
  : "${PM_SCAN_INCLUDE_YESTERDAY:=1}"
  export PM_SCAN_INCLUDE_YESTERDAY
  : "${PM_SCAN_MODEL_SCAN_TIMEOUT_SEC:=420}"
  run_with_timeout "${PM_SCAN_MODEL_SCAN_TIMEOUT_SEC}" "$LOG" \
    env \
      PM_SCAN_MAX_EVENTS_PER_RUN="${PM_MODEL_SCAN_MAX_EVENTS_PER_RUN}" \
      PM_SCAN_MAX_MATCHED_PER_RUN="${PM_MODEL_SCAN_MAX_MATCHED_PER_RUN}" \
      PYTHONUNBUFFERED=1 \
      "$PY" "$ROOT/scan_weather_market.py" \
      --scan-weather-today \
      --tz "America/New_York" \
      $DAYS_ARG \
      $UPDATE_MODELS_ARG \
      --model-refresh-only \
      $MODEL_CACHE_ONLY_ARG \
      --no-auto-trade \
      --ensemble-models "$PM_SCAN_ENSEMBLE_MODELS" \
      --depth-levels "$PM_SCAN_DEPTH_LEVELS" \
      $SCAN_AUTO_DIAG_ARG \
      $INSECURE_ARG
  scan_code="$?"
  set -e
  if [ "$scan_code" -eq 0 ]; then
    break
  fi
  echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] failed exit=$scan_code attempt=$attempt/$((PM_SCAN_RETRY_MAX + 1))" >> "$LOG"
  if [ "$attempt" -gt "$PM_SCAN_RETRY_MAX" ]; then
    exit "$scan_code"
  fi
  sleep "$PM_SCAN_RETRY_SLEEP_SEC"
done

: "${PM_FACTS_MATERIALIZE_AFTER_SCAN:=1}"
if [ "${PM_FACTS_MATERIALIZE_AFTER_SCAN}" = "1" ]; then
  RUN_DATE="$(TZ=America/New_York date '+%Y-%m-%d')"
  echo "---- $(date '+%Y-%m-%d %H:%M:%S') facts_materialize start (run_date=${RUN_DATE}) ----" >> "$LOG"
  "$PY" "$ROOT/ui/server.py" --materialize-facts --facts-date "$RUN_DATE" >> "$LOG" 2>&1 || true
fi

: "${PM_TRADE_SYNC_AFTER_SCAN:=1}"
  if [ "${PM_TRADE_SYNC_AFTER_SCAN}" = "1" ]; then
  if pgrep -f "scan_weather_market.py --trade-sync-loop" >/dev/null 2>&1; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] trade_sync loop running, skip after-scan trade_sync" >> "$LOG"
  else
    echo "---- $(date '+%Y-%m-%d %H:%M:%S') trade_sync start ----" >> "$LOG"
    RUN_DATE="$(TZ=America/New_York date '+%Y-%m-%d')"
    PYTHONUNBUFFERED=1 "$PY" "$ROOT/scan_weather_market.py" \
      --trade-sync-only \
      --trade-sync-date "$RUN_DATE" \
      >> "$LOG" 2>&1 &
  fi
fi
