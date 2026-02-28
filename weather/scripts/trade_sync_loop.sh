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
  echo "[trade_sync_loop] account-requests disabled; skip trade sync loop"
  exit 0
fi

: "${PM_NOFILE_SOFT:=8192}"
if [[ "$PM_NOFILE_SOFT" =~ ^[0-9]+$ ]]; then
  ulimit -Sn "$PM_NOFILE_SOFT" 2>/dev/null || true
fi

. "$ROOT/scripts/lib/run_with_timeout.sh"

LOCK_DIR="$ROOT/data/trade_sync_loop.lock"
PID_FILE="$LOCK_DIR/pid"
if mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "$$" > "$PID_FILE"
else
  if [ -f "$PID_FILE" ]; then
    OLD_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [ -n "${OLD_PID:-}" ] && kill -0 "$OLD_PID" 2>/dev/null; then
      echo "[trade_sync_loop] already running (pid=$OLD_PID), exit."
      exit 0
    fi
  fi
  rm -rf "$LOCK_DIR"
  mkdir "$LOCK_DIR"
  echo "$$" > "$PID_FILE"
fi
trap 'rm -rf "$LOCK_DIR"' EXIT

PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
  PY="python3"
fi

: "${PM_TRADE_SYNC_PREFETCH_EVENT_DETAIL:=1}"
: "${PM_TRADE_SYNC_PREFETCH_MINUTES:=60}"
PREFETCH_STAMP="$ROOT/data/trade_sync_loop.prefetch_ts"
should_prefetch=1
if [ "$PM_TRADE_SYNC_PREFETCH_EVENT_DETAIL" = "0" ]; then
  should_prefetch=0
elif [ "${PM_MIRROR_REALTIME_ONLY:-0}" = "1" ]; then
  # Realtime mirror mode: spend resources on mirror_follow, skip prefetch.
  should_prefetch=0
elif [ "$PM_TRADE_SYNC_PREFETCH_MINUTES" != "0" ] && [ -f "$PREFETCH_STAMP" ]; then
  last_ts="$(cat "$PREFETCH_STAMP" 2>/dev/null || echo 0)"
  now_ts="$(date +%s)"
  if ! [[ "$last_ts" =~ ^[0-9]+$ ]]; then
    last_ts=0
  fi
  if ! [[ "$now_ts" =~ ^[0-9]+$ ]]; then
    now_ts=0
  fi
  if [ "$last_ts" -gt 0 ] && [ "$now_ts" -gt 0 ]; then
    if [ $((now_ts - last_ts)) -lt $((PM_TRADE_SYNC_PREFETCH_MINUTES * 60)) ]; then
      should_prefetch=0
    fi
  fi
fi

if [ "$should_prefetch" = "1" ]; then
  (
    echo "[trade_sync_loop] prefetching event detail before trade sync (background)"
    set +e
    if pgrep -f "scan_weather_market.py .*--scan-weather-today" >/dev/null 2>&1; then
      echo "[trade_sync_loop] prefetch skipped (scan busy)"
      exit 0
    fi
    : "${PM_TRADE_SYNC_PREFETCH_TIMEOUT_SEC:=120}"
    : "${PM_TRADE_SYNC_PREFETCH_MAX_EVENTS:=2}"
    : "${PM_TRADE_SYNC_PREFETCH_TIME_BUDGET_SEC:=12}"
    : "${PM_TRADE_SYNC_PREFETCH_CLOB_TIMEOUT_SEC:=3}"
    : "${PM_TRADE_SYNC_PREFETCH_CLOB_RETRIES:=1}"
    run_with_timeout "${PM_TRADE_SYNC_PREFETCH_TIMEOUT_SEC}" "$ROOT/data/logs/trade_sync_loop.out.log" \
      env \
        PM_DISABLE_PLAYWRIGHT=1 \
        PM_SCAN_FETCH_EVENT_DETAIL=1 \
        PM_SCAN_MAX_EVENTS_PER_RUN="${PM_TRADE_SYNC_PREFETCH_MAX_EVENTS}" \
        PM_SCAN_MAX_MATCHED_PER_RUN="${PM_TRADE_SYNC_PREFETCH_MAX_EVENTS}" \
        PYTHONUNBUFFERED=1 \
        "$PY" "$ROOT/scan_weather_market.py" \
        --scan-weather-today \
        --days-ahead 0 \
        --market-refresh-only \
        --no-auto-trade \
        --no-update-models \
        --market-refresh-time-budget-sec "${PM_TRADE_SYNC_PREFETCH_TIME_BUDGET_SEC}" \
        --market-refresh-clob-timeout-sec "${PM_TRADE_SYNC_PREFETCH_CLOB_TIMEOUT_SEC}" \
        --market-refresh-clob-retries "${PM_TRADE_SYNC_PREFETCH_CLOB_RETRIES}"
    prefetch_rc=$?
    if [ "${prefetch_rc:-0}" -eq 0 ]; then
      date +%s > "$PREFETCH_STAMP" 2>/dev/null || true
    else
      echo "[trade_sync_loop] prefetch failed (rc=${prefetch_rc})"
    fi
  ) &
fi

if [ "${PM_LOW_LATENCY_MODE:-0}" = "1" ]; then
  : "${PM_TRADE_SYNC_LOOP_INTERVAL:=10}"
  : "${PM_TRADE_SYNC_BACKFILL:=0}"
else
  : "${PM_TRADE_SYNC_LOOP_INTERVAL:=120}"
fi
# If the shell exports an empty value, Python will treat it as "disabled" (os.environ.get != default).
# Default backfill to enabled for the normal loop; low-latency mode explicitly defaults to 0 above.
: "${PM_TRADE_SYNC_BACKFILL:=1}"
if [ -n "${PM_TRADE_SYNC_NODE_TIMEOUT:-}" ]; then
  PM_AUTO_TRADE_TIMEOUT="$PM_TRADE_SYNC_NODE_TIMEOUT"
fi
export PM_TRADE_SYNC_BACKFILL
RUN_DATE="latest"

echo "[trade_sync_loop] starting trade sync loop (run_date=$RUN_DATE interval=${PM_TRADE_SYNC_LOOP_INTERVAL}s)"
PYTHONUNBUFFERED=1 "$PY" "$ROOT/scan_weather_market.py" \
  --trade-sync-loop \
  --trade-sync-date "$RUN_DATE" \
  --trade-sync-interval "$PM_TRADE_SYNC_LOOP_INTERVAL"
