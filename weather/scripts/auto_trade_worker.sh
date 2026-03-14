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

mkdir -p "$ROOT/data" "$ROOT/data/logs"
LOG="$ROOT/data/logs/auto_trade_worker.log"

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
  PY="$(command -v python3 || true)"
fi
if [ -z "${PY:-}" ] || [ ! -x "$PY" ]; then
  echo "$(now_text) [auto_worker] python_missing" >> "$LOG"
  exit 1
fi

POLL_SEC="${PM_AUTO_TRADE_WORKER_POLL_SEC:-2}"
if ! [[ "$POLL_SEC" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  POLL_SEC="2"
fi
ONCE_TIMEOUT_SEC="${PM_AUTO_TRADE_WORKER_ONCE_TIMEOUT_SEC:-1200}"
if ! [[ "$ONCE_TIMEOUT_SEC" =~ ^[0-9]+$ ]]; then
  ONCE_TIMEOUT_SEC="240"
fi

echo "$(now_text) [auto_worker] start poll_sec=${POLL_SEC} once_timeout=${ONCE_TIMEOUT_SEC}" >> "$LOG"
while true; do
  if run_with_timeout "$ONCE_TIMEOUT_SEC" "$LOG" "$PY" "$ROOT/scan_weather_market.py" --auto-trade-job-worker --once; then
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
