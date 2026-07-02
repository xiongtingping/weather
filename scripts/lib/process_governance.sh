#!/bin/bash
# Intentionally safe to source from scripts running with set -u.

normalize_nice_level() {
  local raw="${1:-}"
  local default_value="${2:-0}"
  local max_value="${3:-20}"
  local value=""

  if ! [[ "${default_value}" =~ ^[0-9]+$ ]]; then
    default_value="0"
  fi
  if ! [[ "${max_value}" =~ ^[0-9]+$ ]]; then
    max_value="20"
  fi
  if [ "${max_value}" -gt 20 ]; then
    max_value="20"
  fi

  if [[ "${raw}" =~ ^[0-9]+$ ]]; then
    value="${raw}"
  else
    value="${default_value}"
  fi
  if [ "${value}" -gt "${max_value}" ]; then
    value="${max_value}"
  fi
  echo "${value}"
}

normalize_int_range() {
  local raw="${1:-}"
  local default_value="${2:-0}"
  local min_value="${3:-0}"
  local max_value="${4:-999999}"
  local value=""

  if ! [[ "${default_value}" =~ ^[0-9]+$ ]]; then
    default_value="0"
  fi
  if ! [[ "${min_value}" =~ ^[0-9]+$ ]]; then
    min_value="0"
  fi
  if ! [[ "${max_value}" =~ ^[0-9]+$ ]]; then
    max_value="${default_value}"
  fi
  if [ "${max_value}" -lt "${min_value}" ]; then
    max_value="${min_value}"
  fi

  if [[ "${raw}" =~ ^[0-9]+$ ]]; then
    value="${raw}"
  else
    value="${default_value}"
  fi
  if [ "${value}" -lt "${min_value}" ]; then
    value="${min_value}"
  fi
  if [ "${value}" -gt "${max_value}" ]; then
    value="${max_value}"
  fi
  echo "${value}"
}

normalize_positive_int_max() {
  normalize_int_range "${1:-}" "${2:-1}" 1 "${3:-999999}"
}

process_elapsed_seconds() {
  local pid="${1:-}"
  local raw=""
  if ! [[ "${pid}" =~ ^[0-9]+$ ]]; then
    return 1
  fi
  raw="$(ps -p "${pid}" -o etime= 2>/dev/null | tr -d '[:space:]' || true)"
  if [ -z "${raw}" ]; then
    return 1
  fi
  awk -v raw="${raw}" '
    BEGIN {
      days = 0
      t = raw
      if (index(raw, "-") > 0) {
        split(raw, d, "-")
        days = d[1] + 0
        t = d[2]
      }
      n = split(t, p, ":")
      if (n == 3) {
        print days * 86400 + p[1] * 3600 + p[2] * 60 + p[3]
      } else if (n == 2) {
        print days * 86400 + p[1] * 60 + p[2]
      } else {
        print days * 86400 + p[1]
      }
    }
  '
}

process_kill_tree() {
  local parent_pid="${1:-}"
  local signal_name="${2:-TERM}"
  local child_pids=""
  local child_pid=""
  if ! [[ "${parent_pid}" =~ ^[0-9]+$ ]]; then
    return 0
  fi
  child_pids="$(pgrep -P "${parent_pid}" 2>/dev/null || true)"
  for child_pid in ${child_pids}; do
    process_kill_tree "${child_pid}" "${signal_name}"
    kill -"${signal_name}" "${child_pid}" 2>/dev/null || true
  done
}

process_is_descendant_of() {
  local child_pid="${1:-}"
  local ancestor_pid="${2:-}"
  local cur_pid=""
  local parent_pid=""
  if ! [[ "${child_pid}" =~ ^[0-9]+$ ]] || ! [[ "${ancestor_pid}" =~ ^[0-9]+$ ]]; then
    return 1
  fi
  if [ "${child_pid}" = "${ancestor_pid}" ]; then
    return 0
  fi
  cur_pid="${child_pid}"
  while [[ "${cur_pid}" =~ ^[0-9]+$ ]] && [ "${cur_pid}" -gt 1 ]; do
    parent_pid="$(ps -p "${cur_pid}" -o ppid= 2>/dev/null | tr -d '[:space:]' || true)"
    if ! [[ "${parent_pid}" =~ ^[0-9]+$ ]]; then
      return 1
    fi
    if [ "${parent_pid}" = "${ancestor_pid}" ]; then
      return 0
    fi
    cur_pid="${parent_pid}"
  done
  return 1
}

weather_scan_lane_owner_for_pid() {
  local pid="${1:-}"
  local lock_pid=""
  local lock_owner=""
  if ! [[ "${pid}" =~ ^[0-9]+$ ]]; then
    return 1
  fi
  if [ -f "$WEATHER_SCAN_LANE_PID_FILE" ]; then
    lock_pid="$(cat "$WEATHER_SCAN_LANE_PID_FILE" 2>/dev/null || true)"
  fi
  if [ -f "$WEATHER_SCAN_LANE_OWNER_FILE" ]; then
    lock_owner="$(cat "$WEATHER_SCAN_LANE_OWNER_FILE" 2>/dev/null || true)"
  fi
  if ! [[ "${lock_pid:-}" =~ ^[0-9]+$ ]] || [ -z "${lock_owner:-}" ]; then
    return 1
  fi
  if ! kill -0 "${lock_pid}" 2>/dev/null; then
    return 1
  fi
  if process_is_descendant_of "${pid}" "${lock_pid}"; then
    printf '%s\n' "${lock_owner}"
    return 0
  fi
  return 1
}

weather_scan_owner_active_max_sec() {
  local owner="${1:-}"
  local default_max="${2:-180}"
  local value=""
  case "${owner}" in
    daily_scan)
      value="${PM_DAILY_SCAN_ACTIVE_MAX_SEC:-${PM_DAILY_SCAN_TIMEOUT_SEC:-900}}"
      ;;
    *)
      value="${default_max}"
      ;;
  esac
  if ! [[ "${value}" =~ ^[0-9]+$ ]] || [ "${value}" -le 0 ]; then
    value="${default_max}"
  fi
  printf '%s\n' "${value}"
}

weather_scan_cmd_matches() {
  local cmdline="${1:-}"
  local pid="${2:-}"
  local root_dir="${ROOT:-}"
  local cwd_dir=""
  case "${cmdline}" in
    *"scan_weather_market.py"*"--scan-weather-today"*) ;;
    *) return 1 ;;
  esac
  # Stay inside this weather project. This prevents process governance from
  # touching another checkout that happens to use the same script name.
  if [ -n "${root_dir}" ]; then
    if [[ "${cmdline}" == *"${root_dir}/scan_weather_market.py"* ]]; then
      return 0
    fi
    if [[ "${pid}" =~ ^[0-9]+$ ]]; then
      cwd_dir="$(lsof -a -p "${pid}" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -n 1)"
      if [ "${cwd_dir}" = "${root_dir}" ]; then
        return 0
      fi
    fi
    return 1
  fi
  return 0
}

weather_scan_process_active() {
  local max_sec="${1:-${PM_WEATHER_SCAN_ACTIVE_MAX_SEC:-180}}"
  local log_path="${2:-}"
  local log_prefix="${3:-weather_scan}"
  local kill_stale="${PM_WEATHER_SCAN_STALE_KILL:-1}"
  local pids=""
  local pid=""
  local cmdline=""
  local elapsed_sec=""
  local owner=""
  local effective_max_sec=""
  local saw_active=1
  if ! [[ "${max_sec}" =~ ^[0-9]+$ ]] || [ "${max_sec}" -le 0 ]; then
    max_sec=180
  fi
  pids="$(pgrep -f "scan_weather_market.py .*--scan-weather-today" 2>/dev/null || true)"
  for pid in ${pids}; do
    if ! [[ "${pid}" =~ ^[0-9]+$ ]] || [ "${pid}" = "$$" ]; then
      continue
    fi
    cmdline="$(ps -p "${pid}" -o command= 2>/dev/null || true)"
    if ! weather_scan_cmd_matches "${cmdline}" "${pid}"; then
      continue
    fi
    if [ "${PM_WEATHER_SCAN_ACTIVE_IGNORE_MODEL_REFRESH:-0}" = "1" ] && [[ "${cmdline}" == *"--model-refresh-only"* ]]; then
      continue
    fi
    if [ "${PM_WEATHER_SCAN_ACTIVE_IGNORE_MARKET_REFRESH:-0}" = "1" ] && [[ "${cmdline}" == *"--market-refresh-only"* ]]; then
      continue
    fi
    owner="$(weather_scan_lane_owner_for_pid "${pid}" 2>/dev/null || true)"
    effective_max_sec="$(weather_scan_owner_active_max_sec "${owner:-}" "${max_sec}")"
    elapsed_sec="$(process_elapsed_seconds "${pid}" 2>/dev/null || true)"
    if [[ "${elapsed_sec}" =~ ^[0-9]+$ ]] && [ "${elapsed_sec}" -gt "${effective_max_sec}" ]; then
      if [ -n "${log_path}" ]; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') [${log_prefix}] stale weather scan ignored pid=${pid} age_sec=${elapsed_sec} max_sec=${effective_max_sec} owner=${owner:-unknown}" >> "${log_path}"
      fi
      if [ "${kill_stale}" = "1" ]; then
        process_kill_tree "${pid}" TERM
        kill -TERM "${pid}" 2>/dev/null || true
        sleep 1
        if kill -0 "${pid}" 2>/dev/null; then
          process_kill_tree "${pid}" KILL
          kill -KILL "${pid}" 2>/dev/null || true
        fi
        if [ -n "${log_path}" ]; then
          echo "$(date '+%Y-%m-%d %H:%M:%S') [${log_prefix}] stale weather scan killed pid=${pid} owner=${owner:-unknown}" >> "${log_path}"
        fi
      fi
      continue
    fi
    saw_active=0
  done
  return "${saw_active}"
}

weather_scan_terminate_for_entry() {
  local log_path="${1:-}"
  local log_prefix="${2:-weather_scan}"
  local reason="${3:-entry_backlog}"
  local pids=""
  local pid=""
  local cmdline=""
  local owner=""
  local killed=1
  pids="$(pgrep -f "scan_weather_market.py .*--scan-weather-today" 2>/dev/null || true)"
  for pid in ${pids}; do
    if ! [[ "${pid}" =~ ^[0-9]+$ ]] || [ "${pid}" = "$$" ]; then
      continue
    fi
    cmdline="$(ps -p "${pid}" -o command= 2>/dev/null || true)"
    if ! weather_scan_cmd_matches "${cmdline}" "${pid}"; then
      continue
    fi
    if [ "${PM_WEATHER_SCAN_ACTIVE_IGNORE_MODEL_REFRESH:-0}" = "1" ] && [[ "${cmdline}" == *"--model-refresh-only"* ]]; then
      continue
    fi
    if [ "${PM_WEATHER_SCAN_ACTIVE_IGNORE_MARKET_REFRESH:-0}" = "1" ] && [[ "${cmdline}" == *"--market-refresh-only"* ]]; then
      continue
    fi
    owner="$(weather_scan_lane_owner_for_pid "${pid}" 2>/dev/null || true)"
    if [ "${owner:-}" = "daily_scan" ] && [ "${PM_WEATHER_SCAN_PREEMPT_DAILY:-0}" != "1" ]; then
      if [ -n "${log_path}" ]; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') [${log_prefix}] preempt weather scan skipped pid=${pid} reason=${reason} owner=daily_scan" >> "${log_path}"
      fi
      continue
    fi
    if [ -n "${log_path}" ]; then
      echo "$(date '+%Y-%m-%d %H:%M:%S') [${log_prefix}] preempt weather scan pid=${pid} reason=${reason} owner=${owner:-unknown}" >> "${log_path}"
    fi
    process_kill_tree "${pid}" TERM
    kill -TERM "${pid}" 2>/dev/null || true
    killed=0
  done
  if [ "${killed}" = "0" ]; then
    sleep 1
    for pid in ${pids}; do
      if ! [[ "${pid}" =~ ^[0-9]+$ ]] || [ "${pid}" = "$$" ]; then
        continue
      fi
      if kill -0 "${pid}" 2>/dev/null; then
        cmdline="$(ps -p "${pid}" -o command= 2>/dev/null || true)"
        if weather_scan_cmd_matches "${cmdline}" "${pid}"; then
          process_kill_tree "${pid}" KILL
          kill -KILL "${pid}" 2>/dev/null || true
        fi
      fi
    done
  fi
  return "${killed}"
}

WEATHER_SCAN_LANE_LOCK_ACQUIRED=0
WEATHER_SCAN_LANE_LOCK_DIR="${PM_WEATHER_SCAN_LANE_LOCK_DIR:-${ROOT:-/Users/xiong/weather}/data/weather_scan_lane.lock}"
WEATHER_SCAN_LANE_PID_FILE="$WEATHER_SCAN_LANE_LOCK_DIR/pid"
WEATHER_SCAN_LANE_OWNER_FILE="$WEATHER_SCAN_LANE_LOCK_DIR/owner"

weather_scan_lane_set_dir() {
  local lock_dir="${1:-}"
  if [ -z "${lock_dir}" ]; then
    return 1
  fi
  WEATHER_SCAN_LANE_LOCK_DIR="$lock_dir"
  WEATHER_SCAN_LANE_PID_FILE="$WEATHER_SCAN_LANE_LOCK_DIR/pid"
  WEATHER_SCAN_LANE_OWNER_FILE="$WEATHER_SCAN_LANE_LOCK_DIR/owner"
  WEATHER_SCAN_LANE_LOCK_ACQUIRED=0
  return 0
}

weather_scan_lane_set_name() {
  local lane_name="${1:-weather_scan_lane}"
  lane_name="$(printf '%s' "$lane_name" | tr -cd '[:alnum:]_.-')"
  if [ -z "${lane_name}" ]; then
    lane_name="weather_scan_lane"
  fi
  weather_scan_lane_set_dir "${ROOT:-/Users/xiong/weather}/data/${lane_name}.lock"
}

weather_scan_lane_owner() {
  if [ -f "$WEATHER_SCAN_LANE_OWNER_FILE" ]; then
    cat "$WEATHER_SCAN_LANE_OWNER_FILE" 2>/dev/null || true
  fi
}

weather_latest_fact_age_sec() {
  local db_path="${1:-${ROOT:-/Users/xiong/weather}/data/archive.db}"
  local run_date="${2:-}"
  if [ ! -f "$db_path" ]; then
    echo "999999"
    return 0
  fi
  local py="${PY:-${WEATHER_PROJECT_PY:-/Users/xiong/weather/.venv/bin/python}}"
  if [ ! -x "$py" ]; then
    py="/Library/Frameworks/Python.framework/Versions/3.13/bin/python3"
  fi
  if [ ! -x "$py" ]; then
    echo "999999"
    return 0
  fi
  "$py" - "$db_path" "$run_date" <<'PY' 2>/dev/null || echo "999999"
import sqlite3
import sys
from datetime import datetime, timezone

db_path = sys.argv[1]
run_date = str(sys.argv[2] if len(sys.argv) > 2 else "").strip()
try:
    conn = sqlite3.connect(db_path, timeout=1.0)
    if run_date:
        row = conn.execute(
            """
            SELECT MAX(h.fact_as_of)
            FROM event_fact_latest l
            JOIN event_fact_history h ON h.id = l.fact_id
            WHERE h.run_date = ?
            """,
            (run_date,),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT MAX(h.fact_as_of)
            FROM event_fact_latest l
            JOIN event_fact_history h ON h.id = l.fact_id
            """
        ).fetchone()
finally:
    try:
        conn.close()
    except Exception:
        pass

text = str((row[0] if row else "") or "").strip()
if not text:
    print(999999)
    raise SystemExit(0)
text = text.replace("Z", "+00:00")
try:
    dt = datetime.fromisoformat(text)
except Exception:
    try:
        dt = datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except Exception:
        print(999999)
        raise SystemExit(0)
if dt.tzinfo is None:
    dt = dt.replace(tzinfo=timezone.utc)
print(int(max(0, (datetime.now(timezone.utc) - dt).total_seconds())))
PY
}

weather_scan_lane_acquire() {
  local owner="${1:-unknown}"
  local wait_sec="${2:-0}"
  local log_path="${3:-}"
  local log_prefix="${4:-weather_scan}"
  local poll_sec="${PM_WEATHER_SCAN_LANE_POLL_SEC:-2}"
  local waited=0
  local lock_pid=""
  local lock_owner=""
  local live_reason=""
  if ! [[ "${wait_sec}" =~ ^[0-9]+$ ]] || [ "${wait_sec}" -lt 0 ]; then
    wait_sec=0
  fi
  if ! [[ "${poll_sec}" =~ ^[0-9]+$ ]] || [ "${poll_sec}" -le 0 ]; then
    poll_sec=2
  fi
  if [ "${WEATHER_SCAN_LANE_LOCK_ACQUIRED:-0}" = "1" ]; then
    return 0
  fi
  if declare -f weather_refresh_live_ssot_critical_reason >/dev/null 2>&1; then
    live_reason="$(weather_refresh_live_ssot_critical_reason "$owner" 2>/dev/null || true)"
    if [ -n "${live_reason:-}" ]; then
      if [ -n "${log_path}" ]; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') [${log_prefix}] scan lane blocked by live_ssot owner=${owner} live=${live_reason}" >> "$log_path"
      fi
      return 1
    fi
  fi
  while ! mkdir "$WEATHER_SCAN_LANE_LOCK_DIR" 2>/dev/null; do
    lock_pid=""
    lock_owner=""
    if [ -f "$WEATHER_SCAN_LANE_PID_FILE" ]; then
      lock_pid="$(cat "$WEATHER_SCAN_LANE_PID_FILE" 2>/dev/null || true)"
    fi
    if [ -f "$WEATHER_SCAN_LANE_OWNER_FILE" ]; then
      lock_owner="$(cat "$WEATHER_SCAN_LANE_OWNER_FILE" 2>/dev/null || true)"
    fi
    if [ -z "${lock_pid:-}" ] || ! kill -0 "$lock_pid" 2>/dev/null; then
      rm -rf "$WEATHER_SCAN_LANE_LOCK_DIR" 2>/dev/null || true
      continue
    fi
    if [ "${lock_pid:-}" = "$$" ]; then
      WEATHER_SCAN_LANE_LOCK_ACQUIRED=1
      return 0
    fi
    if declare -f weather_refresh_live_ssot_critical_reason >/dev/null 2>&1; then
      live_reason="$(weather_refresh_live_ssot_critical_reason "$owner" 2>/dev/null || true)"
      if [ -n "${live_reason:-}" ]; then
        if [ -n "${log_path}" ]; then
          echo "$(date '+%Y-%m-%d %H:%M:%S') [${log_prefix}] scan lane wait aborted by live_ssot owner=${owner} live=${live_reason} held_by=${lock_owner:-unknown} pid=${lock_pid:-unknown} waited=${waited}s" >> "$log_path"
        fi
        return 1
      fi
    fi
    if [ "$wait_sec" -le 0 ] || [ "$waited" -ge "$wait_sec" ]; then
      if [ -n "${log_path}" ]; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') [${log_prefix}] scan lane busy, skip (owner=${lock_owner:-unknown} pid=${lock_pid:-unknown} waited=${waited}s max=${wait_sec}s)" >> "$log_path"
      fi
      return 1
    fi
    if [ "$waited" -eq 0 ] && [ -n "${log_path}" ]; then
      echo "$(date '+%Y-%m-%d %H:%M:%S') [${log_prefix}] waiting for scan lane (owner=${lock_owner:-unknown} pid=${lock_pid:-unknown} max=${wait_sec}s)" >> "$log_path"
    fi
    sleep "$poll_sec"
    waited=$((waited + poll_sec))
  done
  echo "$$" > "$WEATHER_SCAN_LANE_PID_FILE"
  echo "$owner" > "$WEATHER_SCAN_LANE_OWNER_FILE" 2>/dev/null || true
  WEATHER_SCAN_LANE_LOCK_ACQUIRED=1
  if [ "$waited" -gt 0 ] && [ -n "${log_path}" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [${log_prefix}] scan lane acquired after ${waited}s wait owner=${owner}" >> "$log_path"
  fi
  return 0
}

weather_scan_lane_release() {
  if [ "${WEATHER_SCAN_LANE_LOCK_ACQUIRED:-0}" != "1" ]; then
    return 0
  fi
  local cur_pid=""
  if [ -f "$WEATHER_SCAN_LANE_PID_FILE" ]; then
    cur_pid="$(cat "$WEATHER_SCAN_LANE_PID_FILE" 2>/dev/null || true)"
  fi
  if [ -z "${cur_pid:-}" ] || [ "$cur_pid" = "$$" ]; then
    rm -rf "$WEATHER_SCAN_LANE_LOCK_DIR" 2>/dev/null || true
  fi
  WEATHER_SCAN_LANE_LOCK_ACQUIRED=0
}

auto_trade_worker_process_active() {
  local max_sec="${1:-${PM_AUTO_TRADE_WORKER_ACTIVE_MAX_SEC:-900}}"
  local log_path="${2:-}"
  local log_prefix="${3:-auto_trade}"
  local kill_stale="${PM_AUTO_TRADE_STALE_WORKER_KILL:-1}"
  local pids=""
  local pid=""
  local cmdline=""
  local elapsed_sec=""
  local saw_active=1
  if ! [[ "${max_sec}" =~ ^[0-9]+$ ]] || [ "${max_sec}" -le 0 ]; then
    max_sec=900
  fi
  pids="$(pgrep -f "scan_weather_market.py .*--auto-trade-job-worker" 2>/dev/null || true)"
  for pid in ${pids}; do
    if ! [[ "${pid}" =~ ^[0-9]+$ ]] || [ "${pid}" = "$$" ]; then
      continue
    fi
    cmdline="$(ps -p "${pid}" -o command= 2>/dev/null || true)"
    case "${cmdline}" in
      *"scan_weather_market.py"*"--auto-trade-job-worker"*) ;;
      *) continue ;;
    esac
    elapsed_sec="$(process_elapsed_seconds "${pid}" 2>/dev/null || true)"
    if [[ "${elapsed_sec}" =~ ^[0-9]+$ ]] && [ "${elapsed_sec}" -gt "${max_sec}" ]; then
      if [ -n "${log_path}" ]; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') [${log_prefix}] stale auto_trade worker ignored pid=${pid} age_sec=${elapsed_sec} max_sec=${max_sec}" >> "${log_path}"
      fi
      if [ "${kill_stale}" = "1" ]; then
        kill -TERM "${pid}" 2>/dev/null || true
        sleep 1
        if kill -0 "${pid}" 2>/dev/null; then
          kill -KILL "${pid}" 2>/dev/null || true
        fi
        if [ -n "${log_path}" ]; then
          echo "$(date '+%Y-%m-%d %H:%M:%S') [${log_prefix}] stale auto_trade worker killed pid=${pid}" >> "${log_path}"
        fi
      fi
      continue
    fi
    saw_active=0
  done
  return "${saw_active}"
}
