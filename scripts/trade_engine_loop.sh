#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ -f "$ROOT/scripts/lib/load_launchctl_env.sh" ]; then
  . "$ROOT/scripts/lib/load_launchctl_env.sh"
  load_repo_env_file "$ROOT/.env"
  load_polymarket_launchctl_env
  wait_for_polymarket_credentials_if_required "trade_engine" "${PM_TRADE_ENGINE_CREDENTIAL_RECHECK_SEC:-60}"
fi

if [[ "${PM_DATA_ONLY_MODE:-0}" =~ ^(1|true|yes|on)$ ]] || [[ "${PM_ACCOUNT_REQUESTS_DISABLED:-0}" =~ ^(1|true|yes|on)$ ]]; then
  echo "[trade_engine] account-requests disabled; skip unified engine"
  exit 0
fi

: "${PM_NOFILE_SOFT:=8192}"
if [[ "$PM_NOFILE_SOFT" =~ ^[0-9]+$ ]]; then
  ulimit -Sn "$PM_NOFILE_SOFT" 2>/dev/null || true
fi

. "$ROOT/scripts/lib/run_with_timeout.sh"
. "$ROOT/scripts/lib/process_governance.sh"
. "$ROOT/scripts/lib/resolve_project_python.sh"
export PM_ARCHIVE_WRITE_SCOPE="${PM_ARCHIVE_WRITE_SCOPE:-chengdu_zuuu}"
export PM_WEATHER_SCAN_ACTIVE_IGNORE_MODEL_REFRESH="${PM_TRADE_ENGINE_IGNORE_MODEL_SCAN_ACTIVE:-1}"
export PM_WEATHER_SCAN_ACTIVE_IGNORE_MARKET_REFRESH="${PM_TRADE_ENGINE_IGNORE_MARKET_REFRESH_ACTIVE:-1}"

mkdir -p "$ROOT/data" "$ROOT/data/logs"
LOG="$ROOT/data/logs/trade_engine.log"

now_text() {
  date '+%Y-%m-%d %H:%M:%S'
}

MIN_FREE_MB="$(normalize_positive_int_max "${PM_TRADE_ENGINE_MIN_FREE_MB:-512}" 512 1048576)"

_disk_free_mb() {
  df -Pk "$ROOT/data" 2>/dev/null | awk 'NR==2 {print int($4 / 1024)}'
}

_disk_has_room() {
  local free_mb=""
  free_mb="$(_disk_free_mb)"
  if ! [[ "${free_mb:-}" =~ ^[0-9]+$ ]]; then
    return 0
  fi
  if [ "$free_mb" -lt "$MIN_FREE_MB" ]; then
    echo "$(now_text) [trade_engine] disk_space_low free_mb=${free_mb} min_free_mb=${MIN_FREE_MB}; pause_loop" >> "$LOG"
    return 1
  fi
  return 0
}

LOCK_DIR="$ROOT/data/trade_engine.lock"
LOCK_PID_FILE="$LOCK_DIR/pid"

kill_descendants() {
  local parent_pid="${1:-}"
  local signal_name="${2:-TERM}"
  local child_pids=""
  local child_pid=""
  if [ -z "${parent_pid}" ]; then
    return 0
  fi
  child_pids="$(pgrep -P "${parent_pid}" 2>/dev/null || true)"
  if [ -z "${child_pids}" ]; then
    return 0
  fi
  for child_pid in ${child_pids}; do
    kill_descendants "${child_pid}" "${signal_name}"
    kill -"${signal_name}" "${child_pid}" 2>/dev/null || true
  done
}

cleanup() {
  kill_descendants "$$" TERM
  sleep 1
  kill_descendants "$$" KILL
  weather_scan_lane_release
  if [ -n "${SYNC_BG_PID:-}" ]; then
    wait "${SYNC_BG_PID}" 2>/dev/null || true
  fi
  rm -rf "$LOCK_DIR" 2>/dev/null || true
}

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  lock_pid=""
  if [ -f "$LOCK_PID_FILE" ]; then
    lock_pid="$(cat "$LOCK_PID_FILE" 2>/dev/null || true)"
  fi
  if [ -n "${lock_pid:-}" ] && kill -0 "$lock_pid" 2>/dev/null; then
    echo "$(now_text) [trade_engine] already running, skip (pid=$lock_pid)" >> "$LOG"
    exit 0
  fi
  rm -rf "$LOCK_DIR" 2>/dev/null || true
  mkdir "$LOCK_DIR" 2>/dev/null || {
    echo "$(now_text) [trade_engine] unable to acquire lock, exit" >> "$LOG"
    exit 1
  }
fi
echo "$$" > "$LOCK_PID_FILE"
trap cleanup EXIT INT TERM

PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
  PY="$(resolve_project_python "$ROOT" || true)"
fi
if [ -z "${PY:-}" ] || [ ! -x "$PY" ]; then
  echo "$(now_text) [trade_engine] python_missing" >> "$LOG"
  exit 1
fi

POLL_SEC="${PM_TRADE_ENGINE_POLL_SEC:-${PM_AUTO_TRADE_WORKER_POLL_SEC:-2}}"
if ! [[ "$POLL_SEC" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  POLL_SEC="2"
fi

WORKER_ONCE_TIMEOUT_CAP_SEC="$(normalize_positive_int_max "${PM_AUTO_TRADE_WORKER_ONCE_TIMEOUT_CAP_SEC:-420}" 420 600)"
WORKER_ONCE_TIMEOUT_SEC="$(normalize_positive_int_max "${PM_AUTO_TRADE_WORKER_ONCE_TIMEOUT_SEC:-120}" 120 "${WORKER_ONCE_TIMEOUT_CAP_SEC}")"
WORKER_NICE="$(normalize_nice_level "${PM_TRADE_ENGINE_WORKER_NICE:-${PM_AUTO_TRADE_WORKER_NICE:-10}}" 10)"

export PM_AUTO_TRADE_EVENTS_PER_JOB_MAX="$(normalize_positive_int_max "${PM_AUTO_TRADE_EVENTS_PER_JOB_MAX:-3}" 3 3)"
export PM_AUTO_TRADE_EVENT_TIMEOUT_CAP_SEC="$(normalize_int_range "${PM_AUTO_TRADE_EVENT_TIMEOUT_CAP_SEC:-120}" 120 5 120)"
if [ -z "${PM_AUTO_TRADE_EVENT_TIMEOUT_SEC:-}" ]; then
  PM_AUTO_TRADE_EVENT_TIMEOUT_SEC="${PM_AUTO_TRADE_EVENT_TIMEOUT_CAP_SEC}"
fi
if ! [[ "${PM_AUTO_TRADE_EVENT_TIMEOUT_SEC}" =~ ^[0-9]+$ ]] || [ "${PM_AUTO_TRADE_EVENT_TIMEOUT_SEC}" -lt 1 ] || [ "${PM_AUTO_TRADE_EVENT_TIMEOUT_SEC}" -gt "${PM_AUTO_TRADE_EVENT_TIMEOUT_CAP_SEC}" ]; then
  PM_AUTO_TRADE_EVENT_TIMEOUT_SEC="${PM_AUTO_TRADE_EVENT_TIMEOUT_CAP_SEC}"
fi
export PM_AUTO_TRADE_EVENT_TIMEOUT_SEC
export PM_AUTO_TRADE_EVENT_BACKOFF_SEC="${PM_AUTO_TRADE_EVENT_BACKOFF_SEC:-0}"
export PM_AUTO_TRADE_EVENT_WORKER_DB_BUSY_TIMEOUT_MS="$(normalize_positive_int_max "${PM_AUTO_TRADE_EVENT_WORKER_DB_BUSY_TIMEOUT_MS:-5000}" 5000 5000)"
# Workers use bounded tick prefetch plus on-demand stale repair; CLOB fanout stays capped below.
export PM_AUTO_TRADE_EVENT_WORKER_REFRESH_ON_STALE="${PM_AUTO_TRADE_EVENT_WORKER_REFRESH_ON_STALE:-1}"
export PM_AUTO_TRADE_EVENT_WORKER_REFRESH_TIMEOUT_SEC="${PM_AUTO_TRADE_EVENT_WORKER_REFRESH_TIMEOUT_SEC:-2.5}"
export PM_AUTO_TRADE_EVENT_WORKER_REFRESH_RETRIES="${PM_AUTO_TRADE_EVENT_WORKER_REFRESH_RETRIES:-1}"
export PM_AUTO_TRADE_EVENT_WORKER_REFRESH_MAX_PER_EVENT="$(normalize_positive_int_max "${PM_AUTO_TRADE_EVENT_WORKER_REFRESH_MAX_PER_EVENT:-2}" 2 2)"
export PM_AUTO_TRADE_EVENT_WORKER_REFRESH_MAX_TOTAL_ATTEMPTS="$(normalize_positive_int_max "${PM_AUTO_TRADE_EVENT_WORKER_REFRESH_MAX_TOTAL_ATTEMPTS:-8}" 8 12)"
export PM_AUTO_TRADE_EVENT_WORKER_CLOB_GLOBAL_RATE_LIMIT="${PM_AUTO_TRADE_EVENT_WORKER_CLOB_GLOBAL_RATE_LIMIT:-1}"
export PM_AUTO_TRADE_EVENT_WORKER_ALLOW_DIRECT_FALLBACK="${PM_AUTO_TRADE_EVENT_WORKER_ALLOW_DIRECT_FALLBACK:-0}"
export PM_AUTO_TRADE_EVENT_WORKER_ALLOW_TOKEN_REMAP="${PM_AUTO_TRADE_EVENT_WORKER_ALLOW_TOKEN_REMAP:-1}"
export PM_AUTO_TRADE_EVENT_WORKER_PREFETCH_MARKET_TICKS="${PM_AUTO_TRADE_EVENT_WORKER_PREFETCH_MARKET_TICKS:-1}"
export PM_ORDERBOOK_ASYNC_REFRESH_ENABLED=0
export PM_AUTO_TRADE_EXEC_EDGE_ORDERBOOK_TIMEOUT_SEC="${PM_AUTO_TRADE_EXEC_EDGE_ORDERBOOK_TIMEOUT_SEC:-1.2}"
export PM_AUTO_TRADE_EXEC_EDGE_ORDERBOOK_RETRIES="${PM_AUTO_TRADE_EXEC_EDGE_ORDERBOOK_RETRIES:-1}"
export PM_LCB_GLOBAL_ENABLED=0
export PM_LCB_GLOBAL_OB_REFRESH_TIMEOUT_SEC="${PM_LCB_GLOBAL_OB_REFRESH_TIMEOUT_SEC:-1.2}"
export PM_LCB_GLOBAL_OB_REFRESH_MAX=0
export PM_AUTO_TRADE_EVENT_WORKER_CLOB_PRICE_AUGMENT_ENABLED="${PM_AUTO_TRADE_EVENT_WORKER_CLOB_PRICE_AUGMENT_ENABLED:-0}"
export PM_AUTO_TRADE_ENTRY_PREFETCH_MAX_ATTEMPTS="$(normalize_positive_int_max "${PM_AUTO_TRADE_ENTRY_PREFETCH_MAX_ATTEMPTS:-6}" 6 6)"
export PM_AUTO_TRADE_ENTRY_PREFETCH_BUDGET_SEC="$(normalize_positive_int_max "${PM_AUTO_TRADE_ENTRY_PREFETCH_BUDGET_SEC:-10}" 10 10)"
export PM_AUTO_TRADE_EVENT_WORKER_DECISION_WRITE_RETRY_ATTEMPTS="${PM_AUTO_TRADE_EVENT_WORKER_DECISION_WRITE_RETRY_ATTEMPTS:-5}"
export PM_AUTO_TRADE_EVENT_WORKER_DECISION_WRITE_RETRY_SLEEP_SEC="${PM_AUTO_TRADE_EVENT_WORKER_DECISION_WRITE_RETRY_SLEEP_SEC:-0.30}"
export PM_MARKET_TOKENS_WRITE_RETRY_ATTEMPTS="${PM_MARKET_TOKENS_WRITE_RETRY_ATTEMPTS:-2}"
export PM_MARKET_TOKENS_WRITE_RETRY_SLEEP_SEC="${PM_MARKET_TOKENS_WRITE_RETRY_SLEEP_SEC:-0.05}"
export PM_MARKET_TOKENS_WRITE_RETRY_SLEEP_CAP_SEC="${PM_MARKET_TOKENS_WRITE_RETRY_SLEEP_CAP_SEC:-0.2}"
if [[ "${PM_TRADE_ENGINE_CLAIM_DIRECT_TARGET_JOBS:-0}" =~ ^(1|true|yes|on)$ ]]; then
  export PM_AUTO_TRADE_JOB_CLAIM_EXCLUDE_DIRECT_TARGET=0
else
  export PM_AUTO_TRADE_JOB_CLAIM_EXCLUDE_DIRECT_TARGET=1
fi

SYNC_ONCE_TIMEOUT_SEC="$(normalize_int_range "${PM_TRADE_ENGINE_SYNC_ONCE_TIMEOUT_SEC:-15}" 15 5 60)"
SYNC_NICE="$(normalize_nice_level "${PM_TRADE_ENGINE_SYNC_NICE:-12}" 12)"

SYNC_MAX_PAGES="$(normalize_positive_int_max "${PM_TRADE_ENGINE_SYNC_MAX_PAGES:-1}" 1 4)"

SYNC_BUDGET_MS="$(normalize_positive_int_max "${PM_TRADE_ENGINE_SYNC_BUDGET_MS:-3000}" 3000 30000)"

SYNC_TIMEOUT_MS="$(normalize_positive_int_max "${PM_TRADE_ENGINE_SYNC_TIMEOUT_MS:-4000}" 4000 30000)"

SYNC_HARD_TIMEOUT_MS="$(normalize_positive_int_max "${PM_TRADE_ENGINE_SYNC_HARD_TIMEOUT_MS:-6000}" 6000 60000)"

SYNC_RETRIES="${PM_TRADE_ENGINE_SYNC_RETRIES:-1}"
if ! [[ "$SYNC_RETRIES" =~ ^[0-9]+$ ]] || [ "$SYNC_RETRIES" -le 0 ]; then
  SYNC_RETRIES="1"
fi

SYNC_DIRECT_RETRIES="${PM_TRADE_ENGINE_SYNC_DIRECT_RETRIES:-1}"
if ! [[ "$SYNC_DIRECT_RETRIES" =~ ^[0-9]+$ ]] || [ "$SYNC_DIRECT_RETRIES" -le 0 ]; then
  SYNC_DIRECT_RETRIES="1"
fi

SYNC_AFTER_LOOKBACK_SEC="${PM_TRADE_ENGINE_SYNC_AFTER_LOOKBACK_SEC:-21600}"
if ! [[ "$SYNC_AFTER_LOOKBACK_SEC" =~ ^[0-9]+$ ]] || [ "$SYNC_AFTER_LOOKBACK_SEC" -lt 0 ]; then
  SYNC_AFTER_LOOKBACK_SEC="21600"
fi

SYNC_INTERVAL_RAW="${PM_TRADE_ENGINE_SYNC_INTERVAL_SEC:-}"
if [ -z "${SYNC_INTERVAL_RAW}" ]; then
  if [[ "${PM_LOW_LATENCY_MODE:-0}" =~ ^(1|true|yes|on)$ ]]; then
    SYNC_INTERVAL_RAW="60"
  else
    SYNC_INTERVAL_RAW="${PM_TRADE_SYNC_LOOP_INTERVAL:-300}"
  fi
fi
SYNC_INTERVAL_SEC="$(SYNC_INTERVAL_RAW="$SYNC_INTERVAL_RAW" "$PY" -c '
import math
import os
raw = os.environ.get("SYNC_INTERVAL_RAW", "")
try:
    value = float(raw)
except Exception:
    value = 10.0
if not math.isfinite(value):
    value = 10.0
print(min(900, max(60, int(math.ceil(value)))))
')"

SYNC_ASYNC_ENABLED="${PM_TRADE_ENGINE_SYNC_ASYNC_ENABLED:-1}"
if [[ "${SYNC_ASYNC_ENABLED}" =~ ^(1|true|yes|on)$ ]]; then
  SYNC_ASYNC_ENABLED=1
else
  SYNC_ASYNC_ENABLED=0
fi

SYNC_SKIP_WHEN_BACKLOG="${PM_TRADE_ENGINE_SYNC_SKIP_WHEN_BACKLOG:-1}"
if [[ "${SYNC_SKIP_WHEN_BACKLOG}" =~ ^(1|true|yes|on)$ ]]; then
  SYNC_SKIP_WHEN_BACKLOG=1
else
  SYNC_SKIP_WHEN_BACKLOG=0
fi

SYNC_BACKLOG_RETRY_SEC="${PM_TRADE_ENGINE_SYNC_BACKLOG_RETRY_SEC:-10}"
if ! [[ "$SYNC_BACKLOG_RETRY_SEC" =~ ^[0-9]+$ ]] || [ "$SYNC_BACKLOG_RETRY_SEC" -le 0 ]; then
  SYNC_BACKLOG_RETRY_SEC="10"
fi
SYNC_SKIP_WHEN_IDLE="${PM_TRADE_ENGINE_SYNC_SKIP_WHEN_IDLE:-1}"
if [[ "${SYNC_SKIP_WHEN_IDLE}" =~ ^(1|true|yes|on)$ ]]; then
  SYNC_SKIP_WHEN_IDLE=1
else
  SYNC_SKIP_WHEN_IDLE=0
fi
SYNC_IDLE_RETRY_SEC="$(normalize_positive_int_max "${PM_TRADE_ENGINE_SYNC_IDLE_RETRY_SEC:-60}" 60 300)"
SYNC_DEFER_HARD_SOURCE_D0="${PM_TRADE_ENGINE_SYNC_DEFER_HARD_SOURCE_D0:-1}"
if [[ "${SYNC_DEFER_HARD_SOURCE_D0}" =~ ^(1|true|yes|on)$ ]]; then
  SYNC_DEFER_HARD_SOURCE_D0=1
else
  SYNC_DEFER_HARD_SOURCE_D0=0
fi
HARD_SOURCE_D0_START_HOUR="$(normalize_int_range "${PM_TRADE_ENGINE_HARD_SOURCE_D0_START_HOUR:-7}" 7 0 23)"
HARD_SOURCE_D0_END_HOUR="$(normalize_int_range "${PM_TRADE_ENGINE_HARD_SOURCE_D0_END_HOUR:-20}" 20 0 23)"

SYNC_LOCK_DIR="$ROOT/data/trade_engine_sync.lock"
SYNC_LOCK_PID_FILE="$SYNC_LOCK_DIR/pid"
SYNC_BG_PID=""
WEATHER_SCAN_ACTIVE_MAX_SEC="$(normalize_positive_int_max "${PM_TRADE_ENGINE_WEATHER_SCAN_ACTIVE_MAX_SEC:-180}" 180 180)"
ENTRY_SCAN_PREEMPT_AFTER_SEC="$(normalize_positive_int_max "${PM_TRADE_ENGINE_ENTRY_SCAN_PREEMPT_AFTER_SEC:-5}" 5 90)"
FACT_STALE_GRACE_SEC="$(normalize_positive_int_max "${PM_TRADE_ENGINE_FACT_STALE_GRACE_SEC:-${PM_MODEL_SCAN_FACT_STALE_BYPASS_SEC:-900}}" 900 3600)"
ENTRY_SCAN_PREEMPT_STALE_FACTS_SEC="$(normalize_positive_int_max "${PM_TRADE_ENGINE_ENTRY_SCAN_PREEMPT_STALE_FACTS_SEC:-30}" 30 180)"
SYNC_ENTRY_PREEMPT_AFTER_SEC="$(normalize_positive_int_max "${PM_TRADE_ENGINE_ENTRY_SYNC_PREEMPT_AFTER_SEC:-5}" 5 120)"
SYNC_BACKLOG_BLOCK_AGE_SEC="$(normalize_positive_int_max "${PM_TRADE_ENGINE_SYNC_BACKLOG_BLOCK_AGE_SEC:-30}" 30 600)"
WORKER_LANE_WAIT_SEC="$(normalize_int_range "${PM_TRADE_ENGINE_WORKER_LANE_WAIT_SEC:-0}" 0 0 30)"

RUN_DATE="latest"
NEXT_SYNC_TS=0

_sync_lock_active() {
  if [ ! -d "$SYNC_LOCK_DIR" ]; then
    return 1
  fi
  local sync_pid=""
  if [ -f "$SYNC_LOCK_PID_FILE" ]; then
    sync_pid="$(cat "$SYNC_LOCK_PID_FILE" 2>/dev/null || true)"
  fi
  if [ -n "${sync_pid:-}" ] && kill -0 "$sync_pid" 2>/dev/null; then
    return 0
  fi
  rm -rf "$SYNC_LOCK_DIR" 2>/dev/null || true
  return 1
}

_queued_entry_job_stats() {
  local db_path="$ROOT/data/archive.db"
  if [ ! -f "$db_path" ]; then
    echo "0|0"
    return 0
  fi
  local stats=""
  stats="$(/usr/bin/sqlite3 -noheader -separator '|' "$db_path" "
    WITH queued AS (
      SELECT created_at
      FROM auto_trade_jobs
      WHERE (
          state = 'queued'
          OR (
            state = 'retrying'
            AND (
              COALESCE(next_retry_at, '') = ''
              OR julianday(next_retry_at) <= julianday('now')
            )
          )
          OR (
            state = 'running'
            AND COALESCE(lease_until, '') <> ''
            AND julianday(lease_until) <= julianday('now')
          )
        )
        AND (source = 'entry_1m_strategy_batch' OR substr(COALESCE(source, ''), 1, 8) = 'entry_1m')
    )
    SELECT COUNT(1) || '|' || COALESCE(MAX(0, CAST((julianday('now') - julianday(MIN(created_at))) * 86400 AS INTEGER)), 0)
    FROM queued;
  " 2>>"$LOG" || true)"
  stats="${stats%%$'\n'*}"
  if [[ "${stats}" =~ ^[0-9]+\|[0-9]+$ ]]; then
    echo "$stats"
    return 0
  fi
  echo "$(now_text) [trade_engine] queued_entry_stats_failed fail_open=1 raw=${stats:-empty}" >> "$LOG"
  echo "1|${SYNC_ENTRY_PREEMPT_AFTER_SEC:-5}"
}

_queued_entry_job_count() {
  local stats=""
  stats="$(_queued_entry_job_stats)"
  stats="${stats%%$'\n'*}"
  local count="${stats%%|*}"
  if ! [[ "${count}" =~ ^[0-9]+$ ]]; then
    count=0
  fi
  echo "$count"
}

_claimable_worker_job_count() {
  local db_path="$ROOT/data/archive.db"
  if [ ! -f "$db_path" ]; then
    echo "0"
    return 0
  fi
  local direct_filter=""
  if [[ "${PM_AUTO_TRADE_JOB_CLAIM_EXCLUDE_DIRECT_TARGET:-0}" =~ ^(1|true|yes|on)$ ]]; then
    direct_filter="AND NOT (
      LOWER(COALESCE(source, '')) IN ('hard_source_direct', 'posterior_target_direct')
      OR COALESCE(payload_json, '') LIKE '%\"hard_source_direct\"%'
      OR COALESCE(payload_json, '') LIKE '%\"posterior_target_direct\"%'
      OR COALESCE(payload_json, '') LIKE '%\"hard_source_direct_snapshot\"%'
      OR COALESCE(payload_json, '') LIKE '%\"posterior_target_direct_snapshot\"%'
    )"
  fi
  local count=""
  count="$(/usr/bin/sqlite3 -noheader "$db_path" "
    SELECT COUNT(1)
    FROM auto_trade_jobs
    WHERE (
        (
          state IN ('queued', 'retrying')
          AND (
            COALESCE(next_retry_at, '') = ''
            OR julianday(next_retry_at) <= julianday('now')
          )
        )
        OR (
          state = 'running'
          AND COALESCE(lease_until, '') <> ''
          AND julianday(lease_until) <= julianday('now')
        )
      )
      ${direct_filter};
  " 2>>"$LOG" || true)"
  count="${count%%$'\n'*}"
  if [[ "${count:-}" =~ ^[0-9]+$ ]]; then
    echo "$count"
    return 0
  fi
  echo "$(now_text) [trade_engine] claimable_worker_job_count_failed fail_open=1 raw=${count:-empty}" >> "$LOG"
  echo "1"
}

_sync_subject_stats() {
  local db_path="$ROOT/data/archive.db"
  if [ ! -f "$db_path" ]; then
    echo "1|1"
    return 0
  fi
  "$PY" -c '
import sqlite3
import sys

db_path = sys.argv[1]
conn = sqlite3.connect(db_path, timeout=5.0)
conn.execute("PRAGMA busy_timeout=5000")
try:
    auto_orders = conn.execute("SELECT COUNT(*) FROM auto_orders").fetchone()[0]
except sqlite3.Error:
    auto_orders = 1
try:
    active_trades = conn.execute(
        """
        SELECT COUNT(*)
        FROM edge_trades
        WHERE state IN ('entry', 'entry_pending', 'exit_pending')
           OR COALESCE(position_size, 0) > 0.000001
           OR COALESCE(last_order_id, '') <> ''
        """
    ).fetchone()[0]
except sqlite3.Error:
    active_trades = 1
finally:
    conn.close()
print(f"{int(auto_orders or 0)}|{int(active_trades or 0)}")
' "$db_path" 2>/dev/null || echo "1|1"
}

_weather_scan_active() {
  weather_scan_process_active "$WEATHER_SCAN_ACTIVE_MAX_SEC" "$LOG" "trade_engine"
}

_run_sync_once() {
  local sync_started_ts
  sync_started_ts="$(date +%s)"
  if (
    export PM_TRADE_SYNC_RETRIES="$SYNC_RETRIES"
    export PM_TRADE_SYNC_ASSUME_ZERO="0"
    export PM_TRADE_SYNC_MIRROR_ONLY="0"
    export PM_TRADE_SYNC_RECENT_AFTER_LOOKBACK_SEC="$SYNC_AFTER_LOOKBACK_SEC"
    export PM_CLOB_TRADES_MAX_PAGES="$SYNC_MAX_PAGES"
    export PM_CLOB_TRADES_BUDGET_MS="$SYNC_BUDGET_MS"
    export PM_CLOB_TIMEOUT_MS="$SYNC_TIMEOUT_MS"
    export PM_CLOB_TRADES_HARD_TIMEOUT_MS="$SYNC_HARD_TIMEOUT_MS"
    export PM_CLOB_RETRY_MAX="$SYNC_RETRIES"
    export PM_CLOB_DIRECT_RETRIES="$SYNC_DIRECT_RETRIES"
    run_with_timeout "$SYNC_ONCE_TIMEOUT_SEC" "$LOG" nice -n "$SYNC_NICE" "$PY" "$ROOT/scan_weather_market.py" --trade-sync-only --trade-sync-date "$RUN_DATE"
  ); then
    local sync_elapsed
    sync_elapsed="$(( $(date +%s) - sync_started_ts ))"
    echo "$(now_text) [trade_engine] sync_once_ok elapsed=${sync_elapsed}s" >> "$LOG"
  else
    local rc=$?
    local sync_elapsed
    sync_elapsed="$(( $(date +%s) - sync_started_ts ))"
    if [ "$rc" = "124" ]; then
      echo "$(now_text) [trade_engine] sync_once_timeout elapsed=${sync_elapsed}s" >> "$LOG"
    else
      echo "$(now_text) [trade_engine] sync_once_failed rc=${rc} elapsed=${sync_elapsed}s" >> "$LOG"
    fi
  fi
}

_start_sync_once_async() {
  if _sync_lock_active; then
    return 1
  fi
  mkdir "$SYNC_LOCK_DIR" 2>/dev/null || return 1
  (
    trap 'rm -rf "$SYNC_LOCK_DIR" 2>/dev/null || true' EXIT
    _run_sync_once
  ) &
  SYNC_BG_PID=$!
  echo "$SYNC_BG_PID" > "$SYNC_LOCK_PID_FILE"
  echo "$(now_text) [trade_engine] sync_once_started_async pid=${SYNC_BG_PID}" >> "$LOG"
  return 0
}

_sync_bg_active() {
  [ -n "${SYNC_BG_PID:-}" ] && kill -0 "$SYNC_BG_PID" 2>/dev/null
}

_hard_source_d0_fast_path_active() {
  if [ "$SYNC_DEFER_HARD_SOURCE_D0" != "1" ]; then
    return 1
  fi
  local hour=""
  hour="$(TZ=Asia/Shanghai date '+%H' 2>/dev/null || echo 0)"
  if ! [[ "${hour:-}" =~ ^[0-9]+$ ]]; then
    hour=0
  fi
  hour=$((10#$hour))
  if [ "$hour" -lt "$HARD_SOURCE_D0_START_HOUR" ] || [ "$hour" -ge "$HARD_SOURCE_D0_END_HOUR" ]; then
    return 1
  fi
  local db_path="$ROOT/data/archive.db"
  if [ ! -f "$db_path" ]; then
    return 1
  fi
  local local_date=""
  local_date="$(TZ=Asia/Shanghai date '+%Y-%m-%d' 2>/dev/null || date '+%Y-%m-%d')"
  local count=""
  count="$(/usr/bin/sqlite3 -cmd ".timeout 200" -noheader "$db_path" "
    SELECT COUNT(1)
    FROM event_fact_latest latest
    JOIN event_fact_history history
      ON history.id = latest.fact_id
    WHERE latest.event_date = '${local_date}'
      AND latest.slug LIKE 'highest-temperature-in-chengdu-%'
      AND COALESCE(history.fact_json, '') LIKE '%posterior_temperature_distribution%';
  " 2>>"$LOG" || true)"
  count="${count%%$'\n'*}"
  if [[ "${count:-}" =~ ^[0-9]+$ ]] && [ "$count" -gt 0 ]; then
    return 0
  fi
  return 1
}

_stop_sync_once_async() {
  local reason="${1:-entry_backlog}"
  if ! _sync_bg_active; then
    rm -rf "$SYNC_LOCK_DIR" 2>/dev/null || true
    SYNC_BG_PID=""
    return 1
  fi
  echo "$(now_text) [trade_engine] sync_preempt reason=${reason} pid=${SYNC_BG_PID}" >> "$LOG"
  kill_descendants "$SYNC_BG_PID" TERM
  kill -TERM "$SYNC_BG_PID" 2>/dev/null || true
  sleep 1
  if kill -0 "$SYNC_BG_PID" 2>/dev/null; then
    kill_descendants "$SYNC_BG_PID" KILL
    kill -KILL "$SYNC_BG_PID" 2>/dev/null || true
  fi
  wait "$SYNC_BG_PID" 2>/dev/null || true
  SYNC_BG_PID=""
  rm -rf "$SYNC_LOCK_DIR" 2>/dev/null || true
  return 0
}

echo "$(now_text) [trade_engine] start poll_sec=${POLL_SEC} worker_timeout=${WORKER_ONCE_TIMEOUT_SEC} worker_nice=${WORKER_NICE} sync_interval=${SYNC_INTERVAL_SEC} sync_timeout=${SYNC_ONCE_TIMEOUT_SEC} sync_async=${SYNC_ASYNC_ENABLED} sync_preempt_after=${SYNC_ENTRY_PREEMPT_AFTER_SEC} weather_scan_preempt_after=${ENTRY_SCAN_PREEMPT_AFTER_SEC} weather_scan_stale_fact_preempt_after=${ENTRY_SCAN_PREEMPT_STALE_FACTS_SEC} fact_stale_grace=${FACT_STALE_GRACE_SEC} sync_nice=${SYNC_NICE}" >> "$LOG"
while true; do
  if ! _disk_has_room; then
    sleep "$POLL_SEC"
    continue
  fi

  if [ -n "${SYNC_BG_PID:-}" ] && ! kill -0 "$SYNC_BG_PID" 2>/dev/null; then
    wait "$SYNC_BG_PID" 2>/dev/null || true
    SYNC_BG_PID=""
  fi
  if _sync_bg_active && _hard_source_d0_fast_path_active; then
    _stop_sync_once_async "hard_source_d0_fast_path"
  fi

  queued_stats="$(_queued_entry_job_stats)"
  queued_stats="${queued_stats%%$'\n'*}"
  queued_entry_jobs="${queued_stats%%|*}"
  queued_entry_age="${queued_stats##*|}"
  if ! [[ "${queued_entry_jobs}" =~ ^[0-9]+$ ]]; then
    queued_entry_jobs=1
  fi
  if ! [[ "${queued_entry_age}" =~ ^[0-9]+$ ]]; then
    queued_entry_age="${SYNC_ENTRY_PREEMPT_AFTER_SEC}"
  fi
  claimable_worker_jobs="$(_claimable_worker_job_count)"
  claimable_worker_jobs="${claimable_worker_jobs%%$'\n'*}"
  if ! [[ "${claimable_worker_jobs}" =~ ^[0-9]+$ ]]; then
    claimable_worker_jobs=1
  fi

  if _sync_bg_active; then
    if [ "${claimable_worker_jobs}" -gt 0 ]; then
      _stop_sync_once_async "claimable_worker_jobs count=${claimable_worker_jobs} queued_entry_jobs=${queued_entry_jobs} oldest_age_sec=${queued_entry_age} direct_excluded=${PM_AUTO_TRADE_JOB_CLAIM_EXCLUDE_DIRECT_TARGET:-0}"
    elif [ "${queued_entry_jobs}" -gt 0 ] && [ "${queued_entry_age}" -ge "${SYNC_ENTRY_PREEMPT_AFTER_SEC}" ]; then
      _stop_sync_once_async "entry_backlog count=${queued_entry_jobs} oldest_age_sec=${queued_entry_age} threshold=${SYNC_ENTRY_PREEMPT_AFTER_SEC}"
    else
      echo "$(now_text) [trade_engine] worker_deferred trade_sync_active queued_entry_jobs=${queued_entry_jobs} claimable_worker_jobs=${claimable_worker_jobs} oldest_age_sec=${queued_entry_age} preempt_after=${SYNC_ENTRY_PREEMPT_AFTER_SEC}" >> "$LOG"
      sleep "$POLL_SEC"
      continue
    fi
  fi

  if _weather_scan_active; then
    scan_preempt_after="${ENTRY_SCAN_PREEMPT_AFTER_SEC}"
    scan_lane_owner="$(weather_scan_lane_owner)"
    fact_age_sec=""
    if [ "${scan_lane_owner:-}" = "model_scan" ]; then
      fact_age_sec="$(weather_latest_fact_age_sec "$ROOT/data/archive.db" "$(TZ=America/New_York date '+%Y-%m-%d')" | head -n 1)"
      if ! [[ "${fact_age_sec:-}" =~ ^[0-9]+$ ]]; then
        fact_age_sec=999999
      fi
      if [ "${fact_age_sec}" -ge "${FACT_STALE_GRACE_SEC}" ]; then
        scan_preempt_after="${ENTRY_SCAN_PREEMPT_STALE_FACTS_SEC}"
      fi
    fi
    if [ "${claimable_worker_jobs}" -gt 0 ] && { [ "${queued_entry_jobs}" -eq 0 ] || [ "${queued_entry_age}" -ge "${scan_preempt_after}" ]; }; then
      weather_scan_terminate_for_entry \
        "$LOG" \
        "trade_engine" \
        "claimable_worker_jobs count=${claimable_worker_jobs} queued_entry_jobs=${queued_entry_jobs} oldest_age_sec=${queued_entry_age} threshold=${scan_preempt_after} owner=${scan_lane_owner:-unknown} fact_age_sec=${fact_age_sec:-na} direct_excluded=${PM_AUTO_TRADE_JOB_CLAIM_EXCLUDE_DIRECT_TARGET:-0}"
      sleep 1
    else
      echo "$(now_text) [trade_engine] worker_deferred weather_scan_active queued_entry_jobs=${queued_entry_jobs} claimable_worker_jobs=${claimable_worker_jobs} oldest_age_sec=${queued_entry_age} preempt_after=${scan_preempt_after} owner=${scan_lane_owner:-unknown} fact_age_sec=${fact_age_sec:-na}" >> "$LOG"
      sleep "$POLL_SEC"
      continue
    fi
  fi

  if [ "${claimable_worker_jobs}" -gt 0 ]; then
    if ! weather_scan_lane_acquire "auto_worker" "$WORKER_LANE_WAIT_SEC" "$LOG" "trade_engine"; then
      echo "$(now_text) [trade_engine] worker_deferred scan_lane_busy queued_entry_jobs=${queued_entry_jobs} claimable_worker_jobs=${claimable_worker_jobs} oldest_age_sec=${queued_entry_age}" >> "$LOG"
      sleep "$POLL_SEC"
      continue
    fi
    set +e
    run_with_timeout "$WORKER_ONCE_TIMEOUT_SEC" "$LOG" nice -n "$WORKER_NICE" "$PY" "$ROOT/scan_weather_market.py" --auto-trade-job-worker --once
    worker_rc="$?"
    set -e
    weather_scan_lane_release
    if [ "$worker_rc" = "0" ]; then
      :
    else
      if [ "$worker_rc" = "124" ]; then
        echo "$(now_text) [trade_engine] worker_once_timeout" >> "$LOG"
      else
        echo "$(now_text) [trade_engine] worker_once_failed rc=${worker_rc}" >> "$LOG"
      fi
    fi
  else
    echo "$(now_text) [trade_engine] worker_deferred no_claimable_job queued_entry_jobs=${queued_entry_jobs} direct_excluded=${PM_AUTO_TRADE_JOB_CLAIM_EXCLUDE_DIRECT_TARGET}" >> "$LOG"
  fi

  now_ts="$(date +%s)"
  if [ "$NEXT_SYNC_TS" -le 0 ] || [ "$now_ts" -ge "$NEXT_SYNC_TS" ]; then
    queued_entry_jobs="$(_queued_entry_job_count)"
    if [ "$SYNC_SKIP_WHEN_IDLE" = "1" ]; then
      sync_subject_stats="$(_sync_subject_stats)"
      sync_subject_stats="${sync_subject_stats%%$'\n'*}"
      sync_auto_orders="${sync_subject_stats%%|*}"
      sync_active_trades="${sync_subject_stats##*|}"
      if ! [[ "${sync_auto_orders}" =~ ^[0-9]+$ ]]; then
        sync_auto_orders=1
      fi
      if ! [[ "${sync_active_trades}" =~ ^[0-9]+$ ]]; then
        sync_active_trades=1
      fi
      if [ "${sync_auto_orders}" -le 0 ] && [ "${sync_active_trades}" -le 0 ]; then
        echo "$(now_text) [trade_engine] sync_deferred idle_no_subjects auto_orders=0 active_trades=0" >> "$LOG"
        NEXT_SYNC_TS=$((now_ts + SYNC_IDLE_RETRY_SEC))
        sleep "$POLL_SEC"
        continue
      fi
    fi
    if [ "$SYNC_SKIP_WHEN_BACKLOG" = "1" ] && [ "$claimable_worker_jobs" -gt 0 ]; then
      echo "$(now_text) [trade_engine] sync_deferred claimable_worker_jobs=${claimable_worker_jobs} queued_entry_jobs=${queued_entry_jobs} oldest_age_sec=${queued_entry_age} direct_excluded=${PM_AUTO_TRADE_JOB_CLAIM_EXCLUDE_DIRECT_TARGET:-0}" >> "$LOG"
      NEXT_SYNC_TS=$((now_ts + SYNC_BACKLOG_RETRY_SEC))
      sleep "$POLL_SEC"
      continue
    fi
    if [ "$SYNC_SKIP_WHEN_BACKLOG" = "1" ] && [ "$queued_entry_jobs" -gt 0 ]; then
      if [ "$queued_entry_jobs" -gt 1 ] || [ "$queued_entry_age" -ge "$SYNC_BACKLOG_BLOCK_AGE_SEC" ]; then
        echo "$(now_text) [trade_engine] sync_deferred queued_entry_jobs=${queued_entry_jobs} oldest_age_sec=${queued_entry_age} backlog_block_age_sec=${SYNC_BACKLOG_BLOCK_AGE_SEC}" >> "$LOG"
        NEXT_SYNC_TS=$((now_ts + SYNC_BACKLOG_RETRY_SEC))
        sleep "$POLL_SEC"
        continue
      fi
      echo "$(now_text) [trade_engine] sync_allow_light_backlog queued_entry_jobs=${queued_entry_jobs} oldest_age_sec=${queued_entry_age}" >> "$LOG"
    fi
    if _weather_scan_active; then
      echo "$(now_text) [trade_engine] sync_deferred weather_scan_active queued_entry_jobs=${queued_entry_jobs}" >> "$LOG"
      NEXT_SYNC_TS=$((now_ts + SYNC_BACKLOG_RETRY_SEC))
      sleep "$POLL_SEC"
      continue
    fi
    if _hard_source_d0_fast_path_active; then
      echo "$(now_text) [trade_engine] sync_deferred hard_source_d0_fast_path queued_entry_jobs=${queued_entry_jobs}" >> "$LOG"
      NEXT_SYNC_TS=$((now_ts + SYNC_BACKLOG_RETRY_SEC))
      sleep "$POLL_SEC"
      continue
    fi
    if [ "$SYNC_ASYNC_ENABLED" = "1" ]; then
      if _start_sync_once_async; then
        :
      elif _sync_lock_active; then
        echo "$(now_text) [trade_engine] sync_already_running queued_entry_jobs=${queued_entry_jobs}" >> "$LOG"
      else
        echo "$(now_text) [trade_engine] sync_async_start_failed queued_entry_jobs=${queued_entry_jobs}" >> "$LOG"
      fi
    else
      _run_sync_once
    fi
    now_ts="$(date +%s)"
    NEXT_SYNC_TS=$((now_ts + SYNC_INTERVAL_SEC))
  fi

  sleep "$POLL_SEC"
done
