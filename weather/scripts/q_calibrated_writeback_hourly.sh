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
    mkdir -p "$ROOT/data/logs"
    echo "$(date '+%Y-%m-%d %H:%M:%S') [q_calibrated_writeback] paused by fd_degrade until=${fd_note}" >> "$ROOT/data/logs/q_calibrated_writeback_hourly.out.log"
    exit 0
  fi
fi

PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') [q_calibrated_writeback] missing .venv python at $PY" >> "$ROOT/data/q_calibrated_writeback.log"
  exit 1
fi

LOCK_DIR="$ROOT/data/q_calibrated_writeback.lock"
LOCK_PID_FILE="$LOCK_DIR/pid"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  lock_pid=""
  if [ -f "$LOCK_PID_FILE" ]; then
    lock_pid="$(cat "$LOCK_PID_FILE" 2>/dev/null || true)"
  fi
  if [ -n "$lock_pid" ] && kill -0 "$lock_pid" 2>/dev/null; then
    exit 0
  fi
  rm -rf "$LOCK_DIR" 2>/dev/null || true
  mkdir "$LOCK_DIR" 2>/dev/null || exit 0
fi
echo "$$" > "$LOCK_PID_FILE"
trap 'rm -rf "$LOCK_DIR" 2>/dev/null || true' EXIT

LOG="$ROOT/data/logs/q_calibrated_writeback_hourly.out.log"
ERR="$ROOT/data/logs/q_calibrated_writeback_hourly.err.log"
mkdir -p "$ROOT/data/logs"

{
  echo ""
  echo "---- $(date '+%Y-%m-%d %H:%M:%S') q_calibrated_writeback start ----"
  PYTHONUNBUFFERED=1 "$PY" "$ROOT/scan_weather_market.py" \
    --q-calibrated-writeback-today
} >> "$LOG" 2>> "$ERR"
