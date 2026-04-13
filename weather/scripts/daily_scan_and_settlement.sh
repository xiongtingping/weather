#!/bin/bash
set -euo pipefail

ROOT_DIR="/Users/xiong/weather"
LOG_DIR="${ROOT_DIR}/data/logs"

mkdir -p "${LOG_DIR}"
cd "${ROOT_DIR}"

if [ -f "${ROOT_DIR}/.env" ]; then
  set -a
  . "${ROOT_DIR}/.env"
  set +a
fi

: "${PM_NOFILE_SOFT:=8192}"
if [[ "$PM_NOFILE_SOFT" =~ ^[0-9]+$ ]]; then
  ulimit -Sn "$PM_NOFILE_SOFT" 2>/dev/null || true
fi

. "${ROOT_DIR}/scripts/lib/run_with_timeout.sh"

LOCK_DIR="${ROOT_DIR}/data/daily_scan.lock"
PID_FILE="${LOCK_DIR}/pid"
if mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "$$" > "$PID_FILE"
else
  if [ -f "$PID_FILE" ]; then
    OLD_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [ -n "${OLD_PID:-}" ] && kill -0 "$OLD_PID" 2>/dev/null; then
      echo "=== Daily scan already running (pid=$OLD_PID), skip at $(date -Iseconds) ==="
      exit 0
    fi
  fi
  rm -rf "$LOCK_DIR"
  mkdir "$LOCK_DIR"
  echo "$$" > "$PID_FILE"
fi
trap 'rm -rf "$LOCK_DIR"' EXIT

PY="${ROOT_DIR}/.venv/bin/python"
if [ ! -x "$PY" ]; then
  PY="$(command -v python3 || true)"
fi
if [ -z "${PY:-}" ] || [ ! -x "$PY" ]; then
  echo "=== Daily scan missing python runtime at $(date -Iseconds) ==="
  exit 1
fi

FD_DEGRADE_FILE="${ROOT_DIR}/data/fd_degrade_pause_noncritical.json"
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
    echo "=== Daily scan skipped by fd_degrade until=${fd_note} at $(date -Iseconds) ==="
    exit 0
  fi
fi

: "${PM_DAILY_SCAN_TIMEOUT_SEC:=900}"
: "${PM_DAILY_ARCHIVE_TIMEOUT_SEC:=600}"
LOG_PATH="${LOG_DIR}/daily_scan.out.log"

echo "=== Daily scan start: $(date -Iseconds) ==="
run_with_timeout "${PM_DAILY_SCAN_TIMEOUT_SEC}" "$LOG_PATH" \
  env PYTHONUNBUFFERED=1 "$PY" "${ROOT_DIR}/scan_weather_market.py" --scan-weather-today --tz "America/New_York" --days-ahead 0
scan_rc=$?
if [ "${scan_rc}" -ne 0 ]; then
  echo "=== Daily scan failed rc=${scan_rc} at $(date -Iseconds) ==="
  exit "${scan_rc}"
fi
run_with_timeout "${PM_DAILY_ARCHIVE_TIMEOUT_SEC}" "$LOG_PATH" \
  env PYTHONUNBUFFERED=1 "$PY" "${ROOT_DIR}/scan_weather_market.py" --archive-temps-all
archive_rc=$?
if [ "${archive_rc}" -ne 0 ]; then
  echo "=== Daily archive failed rc=${archive_rc} at $(date -Iseconds) ==="
  exit "${archive_rc}"
fi
echo "=== Daily scan end: $(date -Iseconds) ==="
