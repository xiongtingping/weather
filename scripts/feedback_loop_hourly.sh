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
  echo "$(date '+%Y-%m-%d %H:%M:%S') [feedback_loop] missing project python runtime" >> "$ROOT/data/logs/feedback_loop_hourly.err.log"
  exit 1
fi

_auto_trade_busy() {
  pgrep -f "$ROOT/scripts/auto_trade_entry_1m.sh" >/dev/null 2>&1 && return 0
  pgrep -f "weather_strategy_batch.py .*--enqueue" >/dev/null 2>&1 && return 0
  pgrep -f "$ROOT/scan_weather_market.py --auto-trade-job-worker" >/dev/null 2>&1 && return 0
  pgrep -f "scan_weather_market.py .*--auto-trade-job-worker" >/dev/null 2>&1 && return 0
  if command -v sqlite3 >/dev/null 2>&1 && [ -f "$ROOT/data/archive.db" ]; then
    local active_jobs=""
    active_jobs="$(sqlite3 "$ROOT/data/archive.db" "PRAGMA busy_timeout=100; SELECT COALESCE(COUNT(*),0) FROM auto_trade_jobs WHERE source='entry_1m' AND state IN ('queued','running');" 2>/dev/null || echo 0)"
    if [[ "${active_jobs:-0}" =~ ^[0-9]+$ ]] && [ "${active_jobs}" -gt 0 ]; then
      return 0
    fi
  fi
  return 1
}

if [ "${PM_FEEDBACK_LOOP_YIELD_TO_AUTO_TRADE:-1}" = "1" ] && _auto_trade_busy; then
  mkdir -p "$ROOT/data/logs"
  echo "$(date '+%Y-%m-%d %H:%M:%S') [feedback_loop] yield to auto_trade active" >> "$ROOT/data/logs/feedback_loop_hourly.out.log"
  exit 0
fi

if [ "${PM_FEEDBACK_LOOP_YIELD_TO_AUTO_TRADE:-1}" = "1" ]; then
  FEEDBACK_START_GRACE_SEC="${PM_FEEDBACK_LOOP_START_GRACE_SEC:-20}"
  if [[ "${FEEDBACK_START_GRACE_SEC}" =~ ^[0-9]+$ ]] && [ "${FEEDBACK_START_GRACE_SEC}" -gt 0 ]; then
    sleep "$FEEDBACK_START_GRACE_SEC"
    if _auto_trade_busy; then
      mkdir -p "$ROOT/data/logs"
      echo "$(date '+%Y-%m-%d %H:%M:%S') [feedback_loop] yield to auto_trade active after grace=${FEEDBACK_START_GRACE_SEC}s" >> "$ROOT/data/logs/feedback_loop_hourly.out.log"
      exit 0
    fi
  fi
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
    echo "$(date '+%Y-%m-%d %H:%M:%S') [feedback_loop] paused by fd_degrade until=${fd_note}" >> "$ROOT/data/logs/feedback_loop_hourly.out.log"
    exit 0
  fi
fi

FEEDBACK_TIMEOUT_SEC="${PM_FEEDBACK_LOOP_TIMEOUT_SEC:-900}"
if ! [[ "$FEEDBACK_TIMEOUT_SEC" =~ ^[0-9]+$ ]]; then
  FEEDBACK_TIMEOUT_SEC="900"
fi
FEEDBACK_NICE="$(normalize_nice_level "${PM_FEEDBACK_LOOP_NICE:-15}" 15)"

LOCK_DIR="$ROOT/data/feedback_loop.lock"
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

LOG="$ROOT/data/logs/feedback_loop_hourly.out.log"
ERR="$ROOT/data/logs/feedback_loop_hourly.err.log"
mkdir -p "$ROOT/data/logs"

set +e
{
  echo ""
  echo "---- $(date '+%Y-%m-%d %H:%M:%S') feedback_loop start timeout=${FEEDBACK_TIMEOUT_SEC}s nice=${FEEDBACK_NICE} ----"
  PYTHONUNBUFFERED=1 run_with_timeout "$FEEDBACK_TIMEOUT_SEC" "" nice -n "$FEEDBACK_NICE" "$PY" "$ROOT/scripts/feedback_loop.py"
} >> "$LOG" 2>> "$ERR"
exit_code=$?
set -e

if [ "$exit_code" = "0" ]; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') [feedback_loop] completed" >> "$LOG"
elif [ "$exit_code" = "124" ]; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') [feedback_loop] timeout after ${FEEDBACK_TIMEOUT_SEC}s" >> "$ERR"
else
  echo "$(date '+%Y-%m-%d %H:%M:%S') [feedback_loop] failed exit=${exit_code}" >> "$ERR"
fi

if [ "${PM_COUNTERFACTUAL_REVIEW_ENABLED:-1}" = "1" ]; then
  COUNTERFACTUAL_TIMEOUT_SEC="${PM_COUNTERFACTUAL_REVIEW_TIMEOUT_SEC:-300}"
  if ! [[ "$COUNTERFACTUAL_TIMEOUT_SEC" =~ ^[0-9]+$ ]]; then
    COUNTERFACTUAL_TIMEOUT_SEC="300"
  fi
  COUNTERFACTUAL_DAYS="${PM_COUNTERFACTUAL_REVIEW_DAYS:-7}"
  if ! [[ "$COUNTERFACTUAL_DAYS" =~ ^[0-9]+$ ]]; then
    COUNTERFACTUAL_DAYS="7"
  fi
  REPORT_DIR="$ROOT/data/reports"
  REPORT_PATH="$REPORT_DIR/entry_counterfactual_review_latest.json"
  mkdir -p "$REPORT_DIR"
  set +e
  {
    echo "---- $(date '+%Y-%m-%d %H:%M:%S') entry_counterfactual_review days=${COUNTERFACTUAL_DAYS} ----"
    PYTHONUNBUFFERED=1 run_with_timeout "$COUNTERFACTUAL_TIMEOUT_SEC" "" nice -n "$FEEDBACK_NICE" "$PY" "$ROOT/scripts/entry_counterfactual_review.py" --days "$COUNTERFACTUAL_DAYS" --out "$REPORT_PATH" --feedback-snapshot
  } >> "$LOG" 2>> "$ERR"
  counterfactual_exit=$?
  set -e
  if [ "$counterfactual_exit" = "0" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [entry_counterfactual_review] wrote $REPORT_PATH" >> "$LOG"
  elif [ "$counterfactual_exit" = "124" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [entry_counterfactual_review] timeout after ${COUNTERFACTUAL_TIMEOUT_SEC}s" >> "$ERR"
  else
    echo "$(date '+%Y-%m-%d %H:%M:%S') [entry_counterfactual_review] failed exit=${counterfactual_exit}" >> "$ERR"
  fi
fi

if [ "${PM_REJECT_LEDGER_ENABLED:-1}" = "1" ]; then
  REJECT_LEDGER_TIMEOUT_SEC="${PM_REJECT_LEDGER_TIMEOUT_SEC:-240}"
  if ! [[ "$REJECT_LEDGER_TIMEOUT_SEC" =~ ^[0-9]+$ ]]; then
    REJECT_LEDGER_TIMEOUT_SEC="240"
  fi
  REJECT_LEDGER_DAYS="${PM_REJECT_LEDGER_DAYS:-7}"
  if ! [[ "$REJECT_LEDGER_DAYS" =~ ^[0-9]+$ ]]; then
    REJECT_LEDGER_DAYS="7"
  fi
  REPORT_DIR="$ROOT/data/reports"
  REPORT_PATH="$REPORT_DIR/reject_ledger_latest.json"
  mkdir -p "$REPORT_DIR"
  set +e
  {
    echo "---- $(date '+%Y-%m-%d %H:%M:%S') reject_ledger days=${REJECT_LEDGER_DAYS} ----"
    PYTHONUNBUFFERED=1 run_with_timeout "$REJECT_LEDGER_TIMEOUT_SEC" "" nice -n "$FEEDBACK_NICE" "$PY" "$ROOT/scripts/reject_ledger.py" --days "$REJECT_LEDGER_DAYS" --out "$REPORT_PATH"
  } >> "$LOG" 2>> "$ERR"
  reject_ledger_exit=$?
  set -e
  if [ "$reject_ledger_exit" = "0" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [reject_ledger] wrote $REPORT_PATH" >> "$LOG"
  elif [ "$reject_ledger_exit" = "124" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [reject_ledger] timeout after ${REJECT_LEDGER_TIMEOUT_SEC}s" >> "$ERR"
  else
    echo "$(date '+%Y-%m-%d %H:%M:%S') [reject_ledger] failed exit=${reject_ledger_exit}" >> "$ERR"
  fi
fi

if [ "${PM_EDGE_EXECUTABLE_AUDIT_ENABLED:-1}" = "1" ]; then
  EDGE_EXEC_AUDIT_TIMEOUT_SEC="${PM_EDGE_EXECUTABLE_AUDIT_TIMEOUT_SEC:-240}"
  if ! [[ "$EDGE_EXEC_AUDIT_TIMEOUT_SEC" =~ ^[0-9]+$ ]]; then
    EDGE_EXEC_AUDIT_TIMEOUT_SEC="420"
  fi
  EDGE_EXEC_AUDIT_DAYS="${PM_EDGE_EXECUTABLE_AUDIT_DAYS:-14}"
  if ! [[ "$EDGE_EXEC_AUDIT_DAYS" =~ ^[0-9]+$ ]]; then
    EDGE_EXEC_AUDIT_DAYS="60"
  fi
  REPORT_DIR="$ROOT/data/reports"
  REPORT_PATH="$REPORT_DIR/edge_executable_audit_live.json"
  mkdir -p "$REPORT_DIR"
  set +e
  {
    echo "---- $(date '+%Y-%m-%d %H:%M:%S') edge_executable_audit days=${EDGE_EXEC_AUDIT_DAYS} ----"
    PYTHONUNBUFFERED=1 run_with_timeout "$EDGE_EXEC_AUDIT_TIMEOUT_SEC" "" nice -n "$FEEDBACK_NICE" "$PY" "$ROOT/scripts/edge_executable_audit.py" \
      --db "$ROOT/data/archive.db" \
      --since-days "$EDGE_EXEC_AUDIT_DAYS" \
      --walk-proxy-max-rows "${PM_EDGE_EXECUTABLE_AUDIT_WALK_PROXY_MAX_ROWS:-8000}" \
      --max-records "${PM_EDGE_EXECUTABLE_AUDIT_MAX_RECORDS:-200}" \
      --gate-min-link-ratio "${PM_EDGE_AUDIT_GATE_MIN_LINK_RATIO:-0.65}" \
      --gate-min-proxy-days "${PM_EDGE_AUDIT_GATE_MIN_PROXY_DAYS:-1}" \
      --gate-max-tick-age-p90-sec "${PM_EDGE_AUDIT_GATE_MAX_TICK_AGE_P90_SEC:-14400}" \
      --out "$REPORT_PATH"
  } >> "$LOG" 2>> "$ERR"
  edge_exec_audit_exit=$?
  set -e
  if [ "$edge_exec_audit_exit" = "0" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [edge_executable_audit] wrote $REPORT_PATH" >> "$LOG"
  elif [ "$edge_exec_audit_exit" = "124" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [edge_executable_audit] timeout after ${EDGE_EXEC_AUDIT_TIMEOUT_SEC}s" >> "$ERR"
  else
    echo "$(date '+%Y-%m-%d %H:%M:%S') [edge_executable_audit] failed exit=${edge_exec_audit_exit}" >> "$ERR"
  fi
fi

if [ "${PM_EXIT_POLICY_AUDIT_ENABLED:-1}" = "1" ]; then
  EXIT_POLICY_AUDIT_TIMEOUT_SEC="${PM_EXIT_POLICY_AUDIT_TIMEOUT_SEC:-240}"
  if ! [[ "$EXIT_POLICY_AUDIT_TIMEOUT_SEC" =~ ^[0-9]+$ ]]; then
    EXIT_POLICY_AUDIT_TIMEOUT_SEC="240"
  fi
  EXIT_POLICY_AUDIT_SINCE_HOURS="${PM_EXIT_POLICY_AUDIT_SINCE_HOURS:-24}"
  if ! [[ "$EXIT_POLICY_AUDIT_SINCE_HOURS" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
    EXIT_POLICY_AUDIT_SINCE_HOURS="24"
  fi
  EXIT_POLICY_AUDIT_LIMIT="${PM_EXIT_POLICY_AUDIT_LIMIT:-5000}"
  if ! [[ "$EXIT_POLICY_AUDIT_LIMIT" =~ ^[0-9]+$ ]]; then
    EXIT_POLICY_AUDIT_LIMIT="5000"
  fi
  REPORT_DIR="$ROOT/data/reports"
  REPORT_PATH="$REPORT_DIR/exit_policy_audit_live.json"
  mkdir -p "$REPORT_DIR"
  set +e
  {
    echo "---- $(date '+%Y-%m-%d %H:%M:%S') exit_policy_audit since_hours=${EXIT_POLICY_AUDIT_SINCE_HOURS} ----"
    PYTHONUNBUFFERED=1 run_with_timeout "$EXIT_POLICY_AUDIT_TIMEOUT_SEC" "" nice -n "$FEEDBACK_NICE" "$PY" "$ROOT/scripts/weather_decision_core_audit.py" \
      --since-hours "$EXIT_POLICY_AUDIT_SINCE_HOURS" \
      --limit "$EXIT_POLICY_AUDIT_LIMIT" \
      --no-scan-terms \
      --exit-policy-only \
      --feedback-snapshot \
      --out "$REPORT_PATH"
  } >> "$LOG" 2>> "$ERR"
  exit_policy_audit_exit=$?
  set -e
  if [ "$exit_policy_audit_exit" = "0" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [exit_policy_audit] wrote $REPORT_PATH" >> "$LOG"
  elif [ "$exit_policy_audit_exit" = "124" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [exit_policy_audit] timeout after ${EXIT_POLICY_AUDIT_TIMEOUT_SEC}s" >> "$ERR"
  else
    echo "$(date '+%Y-%m-%d %H:%M:%S') [exit_policy_audit] failed exit=${exit_policy_audit_exit}" >> "$ERR"
  fi
fi
exit "$exit_code"
