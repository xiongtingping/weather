#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ -f "$ROOT/.env" ]; then
  set -a
  . "$ROOT/.env"
  set +a
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

mkdir -p "$ROOT/data" "$ROOT/data/logs"
LOG="$ROOT/data/logs/trade_engine.log"

now_text() {
  date '+%Y-%m-%d %H:%M:%S'
}

LOCK_DIR="$ROOT/data/trade_engine.lock"
LOCK_PID_FILE="$LOCK_DIR/pid"
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
trap 'rm -rf "$LOCK_DIR" 2>/dev/null || true' EXIT

PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
  PY="$(command -v python3 || true)"
fi
if [ -z "${PY:-}" ] || [ ! -x "$PY" ]; then
  echo "$(now_text) [trade_engine] python_missing" >> "$LOG"
  exit 1
fi

POLL_SEC="${PM_TRADE_ENGINE_POLL_SEC:-${PM_AUTO_TRADE_WORKER_POLL_SEC:-2}}"
if ! [[ "$POLL_SEC" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  POLL_SEC="2"
fi

WORKER_ONCE_TIMEOUT_SEC="${PM_AUTO_TRADE_WORKER_ONCE_TIMEOUT_SEC:-1200}"
if ! [[ "$WORKER_ONCE_TIMEOUT_SEC" =~ ^[0-9]+$ ]]; then
  WORKER_ONCE_TIMEOUT_SEC="1200"
fi

SYNC_ONCE_TIMEOUT_SEC="${PM_TRADE_ENGINE_SYNC_ONCE_TIMEOUT_SEC:-1800}"
if ! [[ "$SYNC_ONCE_TIMEOUT_SEC" =~ ^[0-9]+$ ]]; then
  SYNC_ONCE_TIMEOUT_SEC="1800"
fi

SYNC_MAX_PAGES="${PM_TRADE_ENGINE_SYNC_MAX_PAGES:-2}"
if ! [[ "$SYNC_MAX_PAGES" =~ ^[0-9]+$ ]] || [ "$SYNC_MAX_PAGES" -le 0 ]; then
  SYNC_MAX_PAGES="2"
fi

SYNC_BUDGET_MS="${PM_TRADE_ENGINE_SYNC_BUDGET_MS:-12000}"
if ! [[ "$SYNC_BUDGET_MS" =~ ^[0-9]+$ ]] || [ "$SYNC_BUDGET_MS" -le 0 ]; then
  SYNC_BUDGET_MS="12000"
fi

SYNC_TIMEOUT_MS="${PM_TRADE_ENGINE_SYNC_TIMEOUT_MS:-10000}"
if ! [[ "$SYNC_TIMEOUT_MS" =~ ^[0-9]+$ ]] || [ "$SYNC_TIMEOUT_MS" -le 0 ]; then
  SYNC_TIMEOUT_MS="10000"
fi

SYNC_HARD_TIMEOUT_MS="${PM_TRADE_ENGINE_SYNC_HARD_TIMEOUT_MS:-20000}"
if ! [[ "$SYNC_HARD_TIMEOUT_MS" =~ ^[0-9]+$ ]] || [ "$SYNC_HARD_TIMEOUT_MS" -le 0 ]; then
  SYNC_HARD_TIMEOUT_MS="20000"
fi

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
    SYNC_INTERVAL_RAW="10"
  else
    SYNC_INTERVAL_RAW="${PM_TRADE_SYNC_LOOP_INTERVAL:-60}"
  fi
fi
SYNC_INTERVAL_SEC="$(SYNC_INTERVAL_RAW="$SYNC_INTERVAL_RAW" "$PY" - <<'PY'
import math
import os
raw = os.environ.get("SYNC_INTERVAL_RAW", "")
try:
    value = float(raw)
except Exception:
    value = 10.0
if not math.isfinite(value):
    value = 10.0
print(max(5, int(math.ceil(value))))
PY
)"

RUN_DATE="latest"
NEXT_SYNC_TS=0

echo "$(now_text) [trade_engine] start poll_sec=${POLL_SEC} worker_timeout=${WORKER_ONCE_TIMEOUT_SEC} sync_interval=${SYNC_INTERVAL_SEC} sync_timeout=${SYNC_ONCE_TIMEOUT_SEC}" >> "$LOG"
while true; do
  if run_with_timeout "$WORKER_ONCE_TIMEOUT_SEC" "$LOG" "$PY" "$ROOT/scan_weather_market.py" --auto-trade-job-worker --once; then
    :
  else
    rc=$?
    if [ "$rc" = "124" ]; then
      echo "$(now_text) [trade_engine] worker_once_timeout" >> "$LOG"
    else
      echo "$(now_text) [trade_engine] worker_once_failed rc=${rc}" >> "$LOG"
    fi
  fi

  now_ts="$(date +%s)"
  if [ "$NEXT_SYNC_TS" -le 0 ] || [ "$now_ts" -ge "$NEXT_SYNC_TS" ]; then
    sync_started_ts="$now_ts"
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
      run_with_timeout "$SYNC_ONCE_TIMEOUT_SEC" "$LOG" "$PY" "$ROOT/scan_weather_market.py" --trade-sync-only --trade-sync-date "$RUN_DATE"
    ); then
      sync_elapsed="$(( $(date +%s) - sync_started_ts ))"
      echo "$(now_text) [trade_engine] sync_once_ok elapsed=${sync_elapsed}s" >> "$LOG"
    else
      rc=$?
      sync_elapsed="$(( $(date +%s) - sync_started_ts ))"
      if [ "$rc" = "124" ]; then
        echo "$(now_text) [trade_engine] sync_once_timeout elapsed=${sync_elapsed}s" >> "$LOG"
      else
        echo "$(now_text) [trade_engine] sync_once_failed rc=${rc} elapsed=${sync_elapsed}s" >> "$LOG"
      fi
    fi
    now_ts="$(date +%s)"
    NEXT_SYNC_TS=$((now_ts + SYNC_INTERVAL_SEC))
  fi

  sleep "$POLL_SEC"
done
