#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ -f "$ROOT/scripts/lib/load_launchctl_env.sh" ]; then
  . "$ROOT/scripts/lib/load_launchctl_env.sh"
  load_repo_env_file "$ROOT/.env"
  load_polymarket_launchctl_env
  if ! polymarket_credentials_preflight; then
    echo "[auto_worker] credential preflight failed; skip live worker start"
    exit 0
  fi
fi

: "${PM_NOFILE_SOFT:=8192}"
if [[ "$PM_NOFILE_SOFT" =~ ^[0-9]+$ ]]; then
  ulimit -Sn "$PM_NOFILE_SOFT" 2>/dev/null || true
fi

. "$ROOT/scripts/lib/run_with_timeout.sh"
. "$ROOT/scripts/lib/process_governance.sh"
. "$ROOT/scripts/lib/resolve_project_python.sh"
export PM_WEATHER_SCAN_ACTIVE_IGNORE_MODEL_REFRESH="${PM_AUTO_WORKER_IGNORE_MODEL_SCAN_ACTIVE:-1}"
export PM_WEATHER_SCAN_ACTIVE_IGNORE_MARKET_REFRESH="${PM_AUTO_WORKER_IGNORE_MARKET_REFRESH_ACTIVE:-1}"

mkdir -p "$ROOT/data" "$ROOT/data/logs"
LOG="$ROOT/data/logs/auto_trade_worker.log"
export PM_ARCHIVE_WRITE_SCOPE="${PM_ARCHIVE_WRITE_SCOPE:-chengdu_zuuu}"

now_text() {
  date '+%Y-%m-%d %H:%M:%S'
}

LOCK_DIR="$ROOT/data/auto_trade_worker.lock"
LOCK_PID_FILE="$LOCK_DIR/pid"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  lock_pid=""
  if [ -f "$LOCK_PID_FILE" ]; then
    lock_pid="$(cat "$LOCK_PID_FILE" 2>/dev/null || true)"
  fi
  if [ -n "${lock_pid:-}" ] && kill -0 "$lock_pid" 2>/dev/null; then
    echo "$(now_text) [auto_worker] already running, skip (pid=$lock_pid)" >> "$LOG"
    exit 0
  fi
  rm -rf "$LOCK_DIR" 2>/dev/null || true
  mkdir "$LOCK_DIR" 2>/dev/null || {
    echo "$(now_text) [auto_worker] unable to acquire lock, exit" >> "$LOG"
    exit 1
  }
fi
echo "$$" > "$LOCK_PID_FILE"
trap 'rm -rf "$LOCK_DIR" 2>/dev/null || true' EXIT

PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
  PY="$(resolve_project_python "$ROOT" || true)"
fi
if [ -z "${PY:-}" ] || [ ! -x "$PY" ]; then
  echo "$(now_text) [auto_worker] python_missing" >> "$LOG"
  exit 1
fi

POLL_SEC="${PM_AUTO_TRADE_WORKER_POLL_SEC:-2}"
if ! [[ "$POLL_SEC" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  POLL_SEC="2"
fi
ONCE_TIMEOUT_SEC="$(normalize_positive_int_max "${PM_AUTO_TRADE_WORKER_ONCE_TIMEOUT_SEC:-420}" 420 600)"
WORKER_NICE="$(normalize_nice_level "${PM_AUTO_TRADE_WORKER_NICE:-10}" 10)"
WEATHER_SCAN_ACTIVE_MAX_SEC="$(normalize_positive_int_max "${PM_AUTO_TRADE_WORKER_WEATHER_SCAN_ACTIVE_MAX_SEC:-180}" 180 180)"

_weather_scan_active() {
  weather_scan_process_active "$WEATHER_SCAN_ACTIVE_MAX_SEC" "$LOG" "auto_worker"
}

# Workers use bounded tick prefetch plus on-demand stale repair; CLOB fanout stays capped below.
export PM_AUTO_TRADE_EVENTS_PER_JOB_MAX="$(normalize_positive_int_max "${PM_AUTO_TRADE_EVENTS_PER_JOB_MAX:-3}" 3 3)"
export PM_AUTO_TRADE_EVENT_TIMEOUT_CAP_SEC="$(normalize_int_range "${PM_AUTO_TRADE_EVENT_TIMEOUT_CAP_SEC:-120}" 120 5 120)"
if [ -z "${PM_AUTO_TRADE_EVENT_TIMEOUT_SEC:-}" ]; then
  PM_AUTO_TRADE_EVENT_TIMEOUT_SEC="${PM_AUTO_TRADE_EVENT_TIMEOUT_CAP_SEC}"
fi
if ! [[ "${PM_AUTO_TRADE_EVENT_TIMEOUT_SEC}" =~ ^[0-9]+$ ]] || [ "${PM_AUTO_TRADE_EVENT_TIMEOUT_SEC}" -lt 1 ] || [ "${PM_AUTO_TRADE_EVENT_TIMEOUT_SEC}" -gt "${PM_AUTO_TRADE_EVENT_TIMEOUT_CAP_SEC}" ]; then
  PM_AUTO_TRADE_EVENT_TIMEOUT_SEC="${PM_AUTO_TRADE_EVENT_TIMEOUT_CAP_SEC}"
fi
export PM_AUTO_TRADE_EVENT_TIMEOUT_SEC
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
export PM_AUTO_TRADE_EVENT_WORKER_DB_BUSY_TIMEOUT_MS="$(normalize_positive_int_max "${PM_AUTO_TRADE_EVENT_WORKER_DB_BUSY_TIMEOUT_MS:-5000}" 5000 5000)"
export PM_AUTO_TRADE_EVENT_WORKER_DECISION_WRITE_RETRY_ATTEMPTS="${PM_AUTO_TRADE_EVENT_WORKER_DECISION_WRITE_RETRY_ATTEMPTS:-5}"
export PM_AUTO_TRADE_EVENT_WORKER_DECISION_WRITE_RETRY_SLEEP_SEC="${PM_AUTO_TRADE_EVENT_WORKER_DECISION_WRITE_RETRY_SLEEP_SEC:-0.30}"
export PM_AUTO_TRADE_ENTRY_PREFETCH_MAX_ATTEMPTS="$(normalize_positive_int_max "${PM_AUTO_TRADE_ENTRY_PREFETCH_MAX_ATTEMPTS:-6}" 6 6)"
export PM_AUTO_TRADE_ENTRY_PREFETCH_BUDGET_SEC="$(normalize_positive_int_max "${PM_AUTO_TRADE_ENTRY_PREFETCH_BUDGET_SEC:-10}" 10 10)"

echo "$(now_text) [auto_worker] start poll_sec=${POLL_SEC} once_timeout=${ONCE_TIMEOUT_SEC} event_timeout=${PM_AUTO_TRADE_EVENT_TIMEOUT_SEC}s worker_nice=${WORKER_NICE}" >> "$LOG"
while true; do
  if _weather_scan_active; then
    echo "$(now_text) [auto_worker] worker_deferred weather_scan_active" >> "$LOG"
    sleep "$POLL_SEC"
    continue
  fi
  if run_with_timeout "$ONCE_TIMEOUT_SEC" "$LOG" nice -n "$WORKER_NICE" "$PY" "$ROOT/scan_weather_market.py" --auto-trade-job-worker --once; then
    :
  else
    rc=$?
    if [ "$rc" = "124" ]; then
      echo "$(now_text) [auto_worker] worker_once_timeout" >> "$LOG"
    else
      echo "$(now_text) [auto_worker] worker_once_failed rc=${rc}" >> "$LOG"
    fi
  fi
  sleep "$POLL_SEC"
done
