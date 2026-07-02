#!/bin/bash
set -euo pipefail

ROOT_DIR="/Users/xiong/weather"
LOG_DIR="${ROOT_DIR}/data/logs"
LOG_PATH="${LOG_DIR}/daily_scan.out.log"
ROOT="$ROOT_DIR"

mkdir -p "${LOG_DIR}"
cd "${ROOT_DIR}"

if [ -f "${ROOT_DIR}/scripts/lib/load_launchctl_env.sh" ]; then
  . "${ROOT_DIR}/scripts/lib/load_launchctl_env.sh"
  load_repo_env_file "${ROOT_DIR}/.env"
fi

: "${PM_NOFILE_SOFT:=8192}"
if [[ "$PM_NOFILE_SOFT" =~ ^[0-9]+$ ]]; then
  ulimit -Sn "$PM_NOFILE_SOFT" 2>/dev/null || true
fi

. "${ROOT_DIR}/scripts/lib/run_with_timeout.sh"
. "${ROOT_DIR}/scripts/lib/resolve_project_python.sh"
. "${ROOT_DIR}/scripts/lib/process_governance.sh"
. "${ROOT_DIR}/scripts/lib/weather_refresh_coordinator.sh"
if ! weather_refresh_register_entrypoint "daily_scan" "data_source" "${PM_DAILY_SCAN_LANE_NAME:-${PM_REFRESH_COORDINATOR_LANE_NAME:-weather_refresh_lane}}" "$LOG_PATH" "daily_scan"; then
  exit 0
fi

DAILY_REFRESH_LANE_HELD=0
DAILY_REFRESH_DB_HELD=0

daily_refresh_release_locks() {
  if [ "${DAILY_REFRESH_DB_HELD:-0}" = "1" ]; then
    weather_refresh_db_writer_release 2>/dev/null || true
    DAILY_REFRESH_DB_HELD=0
  fi
  if [ "${DAILY_REFRESH_LANE_HELD:-0}" = "1" ]; then
    weather_scan_lane_release 2>/dev/null || true
    DAILY_REFRESH_LANE_HELD=0
  fi
}

daily_refresh_acquire_locks() {
  if [ "${DAILY_REFRESH_LANE_HELD:-0}" = "1" ]; then
    return 0
  fi
  if ! weather_scan_lane_acquire "daily_scan" "${PM_DAILY_SCAN_LANE_WAIT_SEC:-0}" "$LOG_PATH" "daily_scan"; then
    echo "=== Daily scan skipped: weather refresh lane busy at $(date -Iseconds) ==="
    return 1
  fi
  DAILY_REFRESH_LANE_HELD=1
  if [ "${PM_DAILY_SCAN_DB_LOCK_ENABLED:-1}" = "1" ]; then
    if ! weather_refresh_db_writer_acquire "daily_scan" "${PM_DAILY_SCAN_DB_LOCK_WAIT_SEC:-0}" "$LOG_PATH" "daily_scan"; then
      echo "=== Daily scan skipped: db_writer busy at $(date -Iseconds) ==="
      daily_refresh_release_locks
      return 1
    fi
    DAILY_REFRESH_DB_HELD=1
  fi
  return 0
}

daily_scan_cleanup() {
  daily_refresh_release_locks
  if [ -n "${LOCK_DIR:-}" ]; then
    rm -rf "$LOCK_DIR" 2>/dev/null || true
  fi
}

if weather_refresh_skip_if_live_ssot "daily_scan" "${LOG_DIR}/daily_scan.out.log" "daily_scan"; then
  echo "=== Daily scan skipped by refresh coordinator at $(date -Iseconds) ==="
  exit 0
fi

# 保留本地锁只用于 daily 脚本自身重入保护；跨刷新互斥统一交给 refresh coordinator。
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
trap daily_scan_cleanup EXIT

PY="${ROOT_DIR}/.venv/bin/python"
if [ ! -x "$PY" ]; then
  PY="$(resolve_project_python "$ROOT_DIR" || true)"
fi
if [ -z "${PY:-}" ] || [ ! -x "$PY" ]; then
  echo "=== Daily scan missing python runtime at $(date -Iseconds) ==="
  exit 1
fi

FD_DEGRADE_FILE="${ROOT_DIR}/data/fd_degrade_pause_noncritical.json"
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
    echo "=== Daily scan skipped by fd_degrade until=${fd_note} at $(date -Iseconds) ==="
    exit 0
  fi
fi

: "${PM_DAILY_SCAN_TIMEOUT_SEC:=900}"
: "${PM_DAILY_ARCHIVE_TIMEOUT_SEC:=600}"
export PM_ARCHIVE_WRITE_SCOPE="${PM_ARCHIVE_WRITE_SCOPE:-chengdu_zuuu}"

echo "=== Daily scan start: $(date -Iseconds) ==="
if [ "${PM_RECOMMENDED_PILOT_GAP_SCAN_PLAN:-1}" = "1" ]; then
  echo "=== Recommended pilot gap scan plan precheck: $(date -Iseconds) ==="
  run_with_timeout "${PM_RECOMMENDED_PILOT_GAP_PLAN_TIMEOUT_SEC:-120}" "$LOG_PATH" \
    env PYTHONUNBUFFERED=1 "$PY" "${ROOT_DIR}/scripts/recommended_pilot_gap_scan_plan.py" \
      --market-scope "${PM_SMALL_CAPITAL_MARKET_SCOPE:-highest_temperature}" \
      --json || true
fi
if ! daily_refresh_acquire_locks; then
  exit 0
fi
if [ "${PM_RECOMMENDED_PILOT_GAP_REFRESH_ONCE:-1}" = "1" ]; then
  echo "=== Recommended pilot gap refresh once: $(date -Iseconds) ==="
  run_with_timeout "${PM_RECOMMENDED_PILOT_GAP_REFRESH_TIMEOUT_SEC:-420}" "$LOG_PATH" \
    env PYTHONUNBUFFERED=1 "$PY" "${ROOT_DIR}/scripts/recommended_pilot_gap_refresh_once.py" \
      --max-slugs "${PM_RECOMMENDED_PILOT_GAP_REFRESH_MAX_SLUGS:-4}" \
      --out "${PM_RECOMMENDED_PILOT_GAP_REFRESH_ONCE_REPORT:-${ROOT_DIR}/data/reports/recommended_pilot_gap_refresh_once_latest.json}" \
      --json || true
fi
run_with_timeout "${PM_DAILY_SCAN_TIMEOUT_SEC}" "$LOG_PATH" \
  env \
    PYTHONUNBUFFERED=1 \
    PM_SCAN_PRIORITY_ENABLED="${PM_DAILY_SCAN_PRIORITY_ENABLED:-1}" \
    PM_SCAN_PRIORITY_INCLUDE_RECOMMENDED_PILOT_GAPS="${PM_SCAN_PRIORITY_INCLUDE_RECOMMENDED_PILOT_GAPS:-1}" \
    PM_SCAN_PRIORITY_RECOMMENDED_PILOT_GAP_TOPN="${PM_SCAN_PRIORITY_RECOMMENDED_PILOT_GAP_TOPN:-4}" \
    PM_SCAN_PRIORITY_CANDIDATES_PER_RUN="${PM_DAILY_SCAN_PRIORITY_CANDIDATES_PER_RUN:-1}" \
    PM_SCAN_PRIORITY_OTHERS_PER_RUN="${PM_DAILY_SCAN_PRIORITY_OTHERS_PER_RUN:-1}" \
    "$PY" "${ROOT_DIR}/scan_weather_market.py" --scan-weather-today --tz "America/New_York" --days-ahead 0 --write-scope "${PM_ARCHIVE_WRITE_SCOPE}"
scan_rc=$?
if [ "${scan_rc}" -ne 0 ]; then
  echo "=== Daily scan failed rc=${scan_rc} at $(date -Iseconds) ==="
  exit "${scan_rc}"
fi
run_with_timeout "${PM_DAILY_ARCHIVE_TIMEOUT_SEC}" "$LOG_PATH" \
  env PYTHONUNBUFFERED=1 "$PY" "${ROOT_DIR}/scan_weather_market.py" --archive-temps-all --write-scope "${PM_ARCHIVE_WRITE_SCOPE}"
archive_rc=$?
if [ "${archive_rc}" -ne 0 ]; then
  echo "=== Daily archive failed rc=${archive_rc} at $(date -Iseconds) ==="
  exit "${archive_rc}"
fi
daily_refresh_release_locks

if [ "${PM_RECOMMENDED_PILOT_SETTLEMENT_WAKEUP:-1}" = "1" ]; then
  echo "=== Recommended pilot settlement wakeup check: $(date -Iseconds) ==="
  run_with_timeout "${PM_RECOMMENDED_PILOT_WAKEUP_TIMEOUT_SEC:-600}" "$LOG_PATH" \
    env PYTHONUNBUFFERED=1 "$PY" "${ROOT_DIR}/scripts/recommended_pilot_settlement_wakeup.py" \
      --since-days "${PM_SMALL_CAPITAL_VALIDATION_SINCE_DAYS:-42}" \
      --market-scope "${PM_SMALL_CAPITAL_MARKET_SCOPE:-highest_temperature}" \
      --refresh \
      --json || true
  echo "=== Settlement actual repair plan: $(date -Iseconds) ==="
  REPAIR_ARGS=(--json)
  if [ "${PM_SETTLEMENT_ACTUAL_REPAIR_FETCH_PREVIEW:-1}" = "1" ]; then
    REPAIR_ARGS+=(--fetch-preview)
  fi
  run_with_timeout "${PM_SETTLEMENT_ACTUAL_REPAIR_PLAN_TIMEOUT_SEC:-120}" "$LOG_PATH" \
    env PYTHONUNBUFFERED=1 "$PY" "${ROOT_DIR}/scripts/settlement_actual_repair_plan.py" \
      "${REPAIR_ARGS[@]}" || true
  echo "=== Settlement actual apply dry-run: $(date -Iseconds) ==="
  APPLY_ARGS=(--json)
  if [ "${PM_SETTLEMENT_ACTUAL_APPLY_REFRESH_PREVIEW:-0}" = "1" ]; then
    APPLY_ARGS+=(--refresh-preview)
  fi
  run_with_timeout "${PM_SETTLEMENT_ACTUAL_APPLY_DRY_RUN_TIMEOUT_SEC:-120}" "$LOG_PATH" \
    env PYTHONUNBUFFERED=1 "$PY" "${ROOT_DIR}/scripts/apply_settlement_actual_repair_plan.py" \
      "${APPLY_ARGS[@]}" || true
  echo "=== Settlement calibration label dry-run: $(date -Iseconds) ==="
  run_with_timeout "${PM_SETTLEMENT_LABEL_DRY_RUN_TIMEOUT_SEC:-180}" "$LOG_PATH" \
    env PYTHONUNBUFFERED=1 "$PY" "${ROOT_DIR}/scripts/build_settlement_calibration_labels.py" \
      --since-days "${PM_SMALL_CAPITAL_VALIDATION_SINCE_DAYS:-42}" \
      --market-scope "${PM_SMALL_CAPITAL_MARKET_SCOPE:-highest_temperature}" \
      --json || true
  echo "=== Settlement validation pipeline: $(date -Iseconds) ==="
  run_with_timeout "${PM_SETTLEMENT_VALIDATION_PIPELINE_TIMEOUT_SEC:-240}" "$LOG_PATH" \
    env PYTHONUNBUFFERED=1 "$PY" "${ROOT_DIR}/scripts/settlement_validation_pipeline.py" \
      --since-days "${PM_SMALL_CAPITAL_VALIDATION_SINCE_DAYS:-42}" \
      --market-scope "${PM_SMALL_CAPITAL_MARKET_SCOPE:-highest_temperature}" \
      --no-fetch-preview \
      --simulate-after-actual-apply \
      --json || true
  echo "=== Settlement validation apply flow dry-run: $(date -Iseconds) ==="
  run_with_timeout "${PM_SETTLEMENT_APPLY_FLOW_DRY_RUN_TIMEOUT_SEC:-180}" "$LOG_PATH" \
    env PYTHONUNBUFFERED=1 "$PY" "${ROOT_DIR}/scripts/apply_settlement_validation_flow.py" \
      --since-days "${PM_SMALL_CAPITAL_VALIDATION_SINCE_DAYS:-42}" \
      --market-scope "${PM_SMALL_CAPITAL_MARKET_SCOPE:-highest_temperature}" \
      --json || true
  echo "=== Settlement confirmation packet: $(date -Iseconds) ==="
  run_with_timeout "${PM_SETTLEMENT_CONFIRMATION_PACKET_TIMEOUT_SEC:-120}" "$LOG_PATH" \
    env PYTHONUNBUFFERED=1 "$PY" "${ROOT_DIR}/scripts/settlement_confirmation_packet.py" \
      --json || true
  echo "=== Settlement closed-loop audit: $(date -Iseconds) ==="
  run_with_timeout "${PM_SETTLEMENT_CLOSED_LOOP_AUDIT_TIMEOUT_SEC:-240}" "$LOG_PATH" \
    env PYTHONUNBUFFERED=1 "$PY" "${ROOT_DIR}/scripts/settlement_closed_loop_audit.py" \
      --since-days "${PM_SMALL_CAPITAL_VALIDATION_SINCE_DAYS:-42}" \
      --market-scope "${PM_SMALL_CAPITAL_MARKET_SCOPE:-highest_temperature}" \
      --json || true
  echo "=== Strategy goal completion matrix: $(date -Iseconds) ==="
  run_with_timeout "${PM_STRATEGY_GOAL_COMPLETION_MATRIX_TIMEOUT_SEC:-120}" "$LOG_PATH" \
    env PYTHONUNBUFFERED=1 "$PY" "${ROOT_DIR}/scripts/strategy_goal_completion_matrix.py" \
      --json || true
fi
if [ "${PM_RECOMMENDED_PILOT_GAP_SCAN_PLAN:-1}" = "1" ]; then
  echo "=== Recommended pilot gap scan plan postcheck: $(date -Iseconds) ==="
  run_with_timeout "${PM_RECOMMENDED_PILOT_GAP_PLAN_TIMEOUT_SEC:-120}" "$LOG_PATH" \
    env PYTHONUNBUFFERED=1 "$PY" "${ROOT_DIR}/scripts/recommended_pilot_gap_scan_plan.py" \
      --market-scope "${PM_SMALL_CAPITAL_MARKET_SCOPE:-highest_temperature}" \
      --json || true
fi
echo "=== Daily scan end: $(date -Iseconds) ==="
