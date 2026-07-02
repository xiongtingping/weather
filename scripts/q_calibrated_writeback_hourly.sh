#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ -f "$ROOT/scripts/lib/load_launchctl_env.sh" ]; then
  . "$ROOT/scripts/lib/load_launchctl_env.sh"
  load_repo_env_file "$ROOT/.env"
fi

: "${PM_NOFILE_SOFT:=8192}"
if [[ "$PM_NOFILE_SOFT" =~ ^[0-9]+$ ]]; then
  ulimit -Sn "$PM_NOFILE_SOFT" 2>/dev/null || true
fi
. "$ROOT/scripts/lib/run_with_timeout.sh"
. "$ROOT/scripts/lib/process_governance.sh"
. "$ROOT/scripts/lib/resolve_project_python.sh"

PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
  PY="$(resolve_project_python "$ROOT" || true)"
fi
if [ -z "${PY:-}" ] || [ ! -x "$PY" ]; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') [q_calibrated_writeback] missing project python runtime" >> "$ROOT/data/q_calibrated_writeback.log"
  exit 1
fi

FD_DEGRADE_FILE="$ROOT/data/fd_degrade_pause_noncritical.json"
if [ "${PM_FD_DEGRADE_PAUSE_NONCRITICAL:-1}" = "1" ] && [ -f "$FD_DEGRADE_FILE" ]; then
  fd_note="$("$PY" - "$FD_DEGRADE_FILE" <<'PY' 2>/dev/null || true
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

QCAL_WRITEBACK_TIMEOUT_SEC="$(normalize_positive_int_max "${PM_Q_CALIBRATED_WRITEBACK_TIMEOUT_SEC:-180}" 180 300)"
QCAL_WRITEBACK_NICE="$(normalize_nice_level "${PM_Q_CALIBRATED_WRITEBACK_NICE:-15}" 15)"
qcal_log_dir="$ROOT/data/logs"
mkdir -p "$qcal_log_dir"
QCAL_LOG="$qcal_log_dir/q_calibrated_writeback_hourly.out.log"

_auto_trade_entry_active() {
  pgrep -f "scripts/auto_trade_entry_1m.sh" >/dev/null 2>&1 \
    || pgrep -f "scan_weather_market.py .*--auto-trade-enqueue" >/dev/null 2>&1 \
    || pgrep -f "weather_strategy_batch.py .*--enqueue" >/dev/null 2>&1
}

_auto_trade_job_active_count() {
  local db_path="$ROOT/data/archive.db"
  if [ ! -f "$db_path" ]; then
    echo "0"
    return 0
  fi
  sqlite3 "$db_path" \
    "PRAGMA busy_timeout=100; SELECT COALESCE(COUNT(*),0) FROM auto_trade_jobs WHERE source='entry_1m' AND state IN ('queued','running');" \
    2>/dev/null | tail -n 1 | tr -dc '0-9'
}

if [ "${PM_Q_CALIBRATED_WRITEBACK_YIELD_TO_AUTO_TRADE:-1}" = "1" ]; then
  qcal_auto_jobs="$(_auto_trade_job_active_count)"
  if ! [[ "${qcal_auto_jobs:-}" =~ ^[0-9]+$ ]]; then
    qcal_auto_jobs=0
  fi
  if auto_trade_worker_process_active "${PM_AUTO_TRADE_WORKER_ACTIVE_MAX_SEC:-900}" "$QCAL_LOG" "q_calibrated_writeback" \
    || _auto_trade_entry_active \
    || [ "${qcal_auto_jobs}" -gt 0 ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [q_calibrated_writeback] yield to auto_trade active_jobs=${qcal_auto_jobs}" >> "$QCAL_LOG"
    exit 0
  fi
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

LOG="$QCAL_LOG"
ERR="$ROOT/data/logs/q_calibrated_writeback_hourly.err.log"
mkdir -p "$ROOT/data/logs"

set +e
{
  echo ""
  echo "---- $(date '+%Y-%m-%d %H:%M:%S') q_calibrated_writeback start timeout=${QCAL_WRITEBACK_TIMEOUT_SEC}s nice=${QCAL_WRITEBACK_NICE} ----"
  PYTHONUNBUFFERED=1 run_with_timeout "$QCAL_WRITEBACK_TIMEOUT_SEC" "" nice -n "$QCAL_WRITEBACK_NICE" "$PY" "$ROOT/scan_weather_market.py" \
    --q-calibrated-writeback-today
} >> "$LOG" 2>> "$ERR"
exit_code=$?
set -e

if [ "$exit_code" = "0" ]; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') [q_calibrated_writeback] completed" >> "$LOG"
elif [ "$exit_code" = "124" ]; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') [q_calibrated_writeback] timeout after ${QCAL_WRITEBACK_TIMEOUT_SEC}s" >> "$ERR"
else
  echo "$(date '+%Y-%m-%d %H:%M:%S') [q_calibrated_writeback] failed exit=${exit_code}" >> "$ERR"
fi
exit "$exit_code"
