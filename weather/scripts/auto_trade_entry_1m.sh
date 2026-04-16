#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ -f "$ROOT/.env" ]; then
  set -a
  . "$ROOT/.env"
  set +a
fi
if [ -f "$ROOT/scripts/lib/load_launchctl_env.sh" ]; then
  . "$ROOT/scripts/lib/load_launchctl_env.sh"
  load_polymarket_launchctl_env
fi

: "${PM_NOFILE_SOFT:=8192}"
if [[ "$PM_NOFILE_SOFT" =~ ^[0-9]+$ ]]; then
  ulimit -Sn "$PM_NOFILE_SOFT" 2>/dev/null || true
fi

mkdir -p "$ROOT/data" "$ROOT/data/logs"
LOG="$ROOT/data/logs/auto_trade_entry_1m.log"

now_text() {
  date '+%Y-%m-%d %H:%M:%S'
}

# Avoid overlapping entry attempts.
LOCK_DIR="$ROOT/data/auto_trade_entry.lock"
LOCK_PID_FILE="$LOCK_DIR/pid"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  lock_pid=""
  if [ -f "$LOCK_PID_FILE" ]; then
    lock_pid="$(cat "$LOCK_PID_FILE" 2>/dev/null || true)"
  fi
  if [ -n "${lock_pid:-}" ] && kill -0 "$lock_pid" 2>/dev/null; then
    echo "$(now_text) [auto_entry] already running, skip (pid=$lock_pid)" >> "$LOG"
    exit 0
  fi
  echo "$(now_text) [auto_entry] stale lock cleared" >> "$LOG"
  rm -rf "$LOCK_DIR" 2>/dev/null || true
  mkdir "$LOCK_DIR" 2>/dev/null || {
    echo "$(now_text) [auto_entry] unable to acquire lock, skip" >> "$LOG"
    exit 0
  }
fi
echo "$$" > "$LOCK_PID_FILE"
trap 'rm -rf "$LOCK_DIR" 2>/dev/null || true' EXIT

RUN_DATE="$(TZ=America/New_York date '+%Y-%m-%d')"
PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
  PY="$(command -v python3 || true)"
fi
if [ -z "${PY:-}" ] || [ ! -x "$PY" ]; then
  echo "$(now_text) [auto_entry] python_missing" >> "$LOG"
  exit 0
fi

PRIORITY="${PM_AUTO_TRADE_JOB_PRIORITY_ENTRY:-120}"
echo "$(now_text) [auto_entry] enqueue run_date=${RUN_DATE} priority=${PRIORITY}" >> "$LOG"
if ! "$PY" "$ROOT/scan_weather_market.py" \
  --auto-trade-enqueue \
  --date "$RUN_DATE" \
  --auto-trade-source "entry_1m" \
  --auto-trade-priority "$PRIORITY" \
  >> "$LOG" 2>&1; then
  echo "$(now_text) [auto_entry] enqueue_failed" >> "$LOG"
  exit 0
fi
