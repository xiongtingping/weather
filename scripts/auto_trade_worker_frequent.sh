#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# 加载环境变量
if [ -f "$ROOT/scripts/lib/load_launchctl_env.sh" ]; then
  . "$ROOT/scripts/lib/load_launchctl_env.sh"
  load_repo_env_file "$ROOT/.env"
  load_polymarket_launchctl_env
  if ! polymarket_credentials_preflight; then
    echo "[auto_worker_frequent] credential preflight failed; skip worker start"
    exit 0
  fi
fi

LOG="$ROOT/data/auto_trade_worker_frequent.log"
. "$ROOT/scripts/lib/run_with_timeout.sh"
. "$ROOT/scripts/lib/process_governance.sh"
. "$ROOT/scripts/lib/resolve_project_python.sh"
export PM_ARCHIVE_WRITE_SCOPE="${PM_ARCHIVE_WRITE_SCOPE:-chengdu_zuuu}"

PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
  PY="$(resolve_project_python "$ROOT" || true)"
fi
if [ -z "${PY:-}" ] || [ ! -x "$PY" ]; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') [auto_trade_worker_frequent] missing project python runtime" >> "$LOG"
  exit 1
fi

export PM_AUTO_TRADE_EVENTS_PER_JOB_MAX="$(normalize_positive_int_max "${PM_AUTO_TRADE_EVENTS_PER_JOB_MAX:-3}" 3 3)"
PM_AUTO_TRADE_EVENT_TIMEOUT_CAP_SEC="$(normalize_int_range "${PM_AUTO_TRADE_EVENT_TIMEOUT_CAP_SEC:-120}" 120 5 120)"
if [ -z "${PM_AUTO_TRADE_EVENT_TIMEOUT_SEC:-}" ]; then
  PM_AUTO_TRADE_EVENT_TIMEOUT_SEC="${PM_AUTO_TRADE_EVENT_TIMEOUT_CAP_SEC}"
fi
if ! [[ "${PM_AUTO_TRADE_EVENT_TIMEOUT_SEC}" =~ ^[0-9]+$ ]] || [ "${PM_AUTO_TRADE_EVENT_TIMEOUT_SEC}" -lt 1 ] || [ "${PM_AUTO_TRADE_EVENT_TIMEOUT_SEC}" -gt "${PM_AUTO_TRADE_EVENT_TIMEOUT_CAP_SEC}" ]; then
  PM_AUTO_TRADE_EVENT_TIMEOUT_SEC="${PM_AUTO_TRADE_EVENT_TIMEOUT_CAP_SEC}"
fi
export PM_AUTO_TRADE_EVENT_TIMEOUT_SEC
export PM_AUTO_TRADE_EVENT_WORKER_REFRESH_ON_STALE="${PM_AUTO_TRADE_EVENT_WORKER_REFRESH_ON_STALE:-1}"
export PM_AUTO_TRADE_EVENT_WORKER_REFRESH_TIMEOUT_SEC="${PM_AUTO_TRADE_EVENT_WORKER_REFRESH_TIMEOUT_SEC:-1.5}"
export PM_AUTO_TRADE_EVENT_WORKER_REFRESH_RETRIES="${PM_AUTO_TRADE_EVENT_WORKER_REFRESH_RETRIES:-1}"
export PM_AUTO_TRADE_EVENT_WORKER_REFRESH_MAX_PER_EVENT="$(normalize_positive_int_max "${PM_AUTO_TRADE_EVENT_WORKER_REFRESH_MAX_PER_EVENT:-1}" 1 1)"
export PM_AUTO_TRADE_EVENT_WORKER_REFRESH_MAX_TOTAL_ATTEMPTS="$(normalize_positive_int_max "${PM_AUTO_TRADE_EVENT_WORKER_REFRESH_MAX_TOTAL_ATTEMPTS:-12}" 12 12)"
export PM_AUTO_TRADE_EVENT_WORKER_PREFETCH_MARKET_TICKS="${PM_AUTO_TRADE_EVENT_WORKER_PREFETCH_MARKET_TICKS:-1}"
export PM_AUTO_TRADE_ENTRY_PREFETCH_MAX_ATTEMPTS="$(normalize_positive_int_max "${PM_AUTO_TRADE_ENTRY_PREFETCH_MAX_ATTEMPTS:-4}" 4 4)"
export PM_AUTO_TRADE_ENTRY_PREFETCH_BUDGET_SEC="$(normalize_positive_int_max "${PM_AUTO_TRADE_ENTRY_PREFETCH_BUDGET_SEC:-6}" 6 6)"
export PM_AUTO_TRADE_EVENT_WORKER_CLOB_GLOBAL_RATE_LIMIT="${PM_AUTO_TRADE_EVENT_WORKER_CLOB_GLOBAL_RATE_LIMIT:-1}"
export PM_AUTO_TRADE_EVENT_WORKER_ALLOW_DIRECT_FALLBACK="${PM_AUTO_TRADE_EVENT_WORKER_ALLOW_DIRECT_FALLBACK:-0}"
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

ONCE_TIMEOUT_SEC="$(normalize_positive_int_max "${PM_AUTO_TRADE_WORKER_ONCE_TIMEOUT_SEC:-420}" 420 600)"
WORKER_NICE="$(normalize_nice_level "${PM_AUTO_TRADE_WORKER_NICE:-10}" 10)"
WEATHER_SCAN_ACTIVE_MAX_SEC="$(normalize_positive_int_max "${PM_AUTO_TRADE_WORKER_WEATHER_SCAN_ACTIVE_MAX_SEC:-180}" 180 180)"

if weather_scan_process_active "$WEATHER_SCAN_ACTIVE_MAX_SEC" "$LOG" "auto_trade_worker_frequent"; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') [auto_trade_worker_frequent] worker_deferred weather_scan_active" >> "$LOG"
  exit 0
fi

echo "---- $(date '+%Y-%m-%d %H:%M:%S') auto_trade_worker_frequent start timeout=${ONCE_TIMEOUT_SEC}s nice=${WORKER_NICE} ----" >> "$LOG"

# 运行一次 auto-trade job worker
# 处理队列中的交易任务
run_with_timeout "$ONCE_TIMEOUT_SEC" "$LOG" nice -n "$WORKER_NICE" "$PY" "$ROOT/scan_weather_market.py" \
  --auto-trade-job-worker \
  --once

exit_code=$?
echo "$(date '+%Y-%m-%d %H:%M:%S') [auto_trade_worker_frequent] completed exit=$exit_code" >> "$LOG"
exit $exit_code
