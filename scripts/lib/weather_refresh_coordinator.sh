#!/bin/bash
# Shared arbitration for weather refresh jobs. Safe to source under set -u.

weather_refresh_apply_safe_defaults() {
  : "${PM_REFRESH_COORDINATOR_ENABLED:=1}"
  : "${PM_REFRESH_COORDINATOR_LIVE_PROTECT_ENABLED:=1}"
  : "${PM_REFRESH_COORDINATOR_UNIFIED_LANE_ENABLED:=1}"
  : "${PM_REFRESH_COORDINATOR_LANE_NAME:=weather_refresh_lane}"
  : "${PM_REFRESH_COORDINATOR_ROLE_CONTRACT_VERSION:=refresh_coordinator_v3}"
  : "${PM_REFRESH_COORDINATOR_SUPPRESS_LEGACY_PEER_GUARDS:=1}"
  : "${PM_MODEL_SCAN_LANE_NAME:=${PM_REFRESH_COORDINATOR_LANE_NAME}}"
  : "${PM_MARKET_REFRESH_LANE_NAME:=${PM_REFRESH_COORDINATOR_LANE_NAME}}"
  : "${PM_DAILY_SCAN_LANE_NAME:=${PM_REFRESH_COORDINATOR_LANE_NAME}}"
  : "${PM_REFRESH_COORDINATOR_LIVE_OWNERS:=live_ssot,hard_source,awc_release,hard_source_direct_poller,posterior_target_direct,auto_trade_order,auto_worker,auto_trade_worker,auto_trade_job_worker}"
  : "${PM_REFRESH_COORDINATOR_MODEL_OWNERS:=model_scan,model_scan_hourly,model_release_watcher}"
  : "${PM_REFRESH_COORDINATOR_MARKET_OWNERS:=market_refresh,market_refresh_frequent,market_refresh_urgent,weather_discovery}"
  : "${PM_REFRESH_COORDINATOR_DATA_SOURCE_OWNERS:=weatherctl_facts,daily_scan}"
  : "${PM_REFRESH_COORDINATOR_AWC_LOCK_GUARD:=1}"
  : "${PM_REFRESH_COORDINATOR_ENFORCE_OWNER_ROLE:=1}"
  : "${PM_REFRESH_COORDINATOR_ALLOW_NONLIVE_AWC_REFRESH:=0}"
  : "${PM_REFRESH_COORDINATOR_FACTS_WRITE_OWNERS:=weatherctl_facts,daily_scan}"
  : "${PM_REFRESH_COORDINATOR_ALLOW_SCAN_FACTS_MATERIALIZE:=0}"
  : "${PM_WEATHER_DISCOVERY_COORDINATOR_LANE_ENABLED:=1}"
  : "${PM_WEATHER_DISCOVERY_LANE_WAIT_SEC:=0}"
  : "${PM_FACTS_MATERIALIZE_AFTER_SCAN:=0}"
  : "${PM_WEATHER_DISCOVERY_KICK_MODEL_SCAN:=0}"
  : "${PM_MODEL_SCAN_AWC_FACT_REFRESH_ENABLED:=0}"
  : "${PM_MODEL_SCAN_AWC_FACT_REFRESH_PRE_LANE_ENABLED:=0}"
  : "${PM_MODEL_SCAN_AWC_FACT_REFRESH_LANE_WAIT_SEC:=0}"
  : "${PM_MODEL_SCAN_AWC_FACT_REFRESH_DB_LOCK_WAIT_SEC:=0}"
  : "${PM_MODEL_SCAN_PRECIP_NOWCAST_FACT_REFRESH_ENABLED:=0}"
  : "${PM_MODEL_SCAN_PRECIP_NOWCAST_FACT_REFRESH_LANE_WAIT_SEC:=0}"
  : "${PM_MODEL_SCAN_PRECIP_NOWCAST_FACT_REFRESH_DB_LOCK_WAIT_SEC:=0}"
  : "${PM_MODEL_SCAN_FACTS_MATERIALIZE_ON_YIELD:=0}"
  : "${PM_MODEL_SCAN_FACTS_MATERIALIZE_ON_YIELD_LANE_WAIT_SEC:=0}"
  : "${PM_MODEL_SCAN_FACTS_MATERIALIZE_ON_YIELD_DB_LOCK_WAIT_SEC:=0}"
  : "${PM_MODEL_SCAN_HOURLY_FACTS_MATERIALIZE_ENABLED:=0}"
  : "${PM_MARKET_WS_DEFER_DURING_LIVE_SSOT:=1}"
  : "${PM_MARKET_WS_DB_BUSY_TIMEOUT_MS:=250}"
  : "${PM_MARKET_WS_WRITE_RETRY_ATTEMPTS:=2}"
  : "${PM_HARD_SOURCE_PREEMPT_NONCRITICAL_REFRESH_ENABLED:=1}"
  : "${PM_HARD_SOURCE_PREEMPT_NONCRITICAL_OWNERS:=model_scan,model_scan_hourly,market_refresh,market_refresh_frequent,market_refresh_urgent,weather_discovery,model_release_watcher,daily_scan,weatherctl_facts}"
  export \
    PM_REFRESH_COORDINATOR_ENABLED \
    PM_REFRESH_COORDINATOR_LIVE_PROTECT_ENABLED \
    PM_REFRESH_COORDINATOR_UNIFIED_LANE_ENABLED \
    PM_REFRESH_COORDINATOR_LANE_NAME \
    PM_REFRESH_COORDINATOR_ROLE_CONTRACT_VERSION \
    PM_REFRESH_COORDINATOR_SUPPRESS_LEGACY_PEER_GUARDS \
    PM_MODEL_SCAN_LANE_NAME \
    PM_MARKET_REFRESH_LANE_NAME \
    PM_DAILY_SCAN_LANE_NAME \
    PM_REFRESH_COORDINATOR_LIVE_OWNERS \
    PM_REFRESH_COORDINATOR_MODEL_OWNERS \
    PM_REFRESH_COORDINATOR_MARKET_OWNERS \
    PM_REFRESH_COORDINATOR_DATA_SOURCE_OWNERS \
    PM_REFRESH_COORDINATOR_AWC_LOCK_GUARD \
    PM_REFRESH_COORDINATOR_ENFORCE_OWNER_ROLE \
    PM_REFRESH_COORDINATOR_ALLOW_NONLIVE_AWC_REFRESH \
    PM_REFRESH_COORDINATOR_FACTS_WRITE_OWNERS \
    PM_REFRESH_COORDINATOR_ALLOW_SCAN_FACTS_MATERIALIZE \
    PM_WEATHER_DISCOVERY_COORDINATOR_LANE_ENABLED \
    PM_WEATHER_DISCOVERY_LANE_WAIT_SEC \
    PM_FACTS_MATERIALIZE_AFTER_SCAN \
    PM_WEATHER_DISCOVERY_KICK_MODEL_SCAN \
    PM_MODEL_SCAN_AWC_FACT_REFRESH_ENABLED \
    PM_MODEL_SCAN_AWC_FACT_REFRESH_PRE_LANE_ENABLED \
    PM_MODEL_SCAN_AWC_FACT_REFRESH_LANE_WAIT_SEC \
    PM_MODEL_SCAN_AWC_FACT_REFRESH_DB_LOCK_WAIT_SEC \
    PM_MODEL_SCAN_PRECIP_NOWCAST_FACT_REFRESH_ENABLED \
    PM_MODEL_SCAN_PRECIP_NOWCAST_FACT_REFRESH_LANE_WAIT_SEC \
    PM_MODEL_SCAN_PRECIP_NOWCAST_FACT_REFRESH_DB_LOCK_WAIT_SEC \
    PM_MODEL_SCAN_FACTS_MATERIALIZE_ON_YIELD \
    PM_MODEL_SCAN_FACTS_MATERIALIZE_ON_YIELD_LANE_WAIT_SEC \
    PM_MODEL_SCAN_FACTS_MATERIALIZE_ON_YIELD_DB_LOCK_WAIT_SEC \
    PM_MODEL_SCAN_HOURLY_FACTS_MATERIALIZE_ENABLED \
    PM_MARKET_WS_DEFER_DURING_LIVE_SSOT \
    PM_MARKET_WS_DB_BUSY_TIMEOUT_MS \
    PM_MARKET_WS_WRITE_RETRY_ATTEMPTS \
    PM_HARD_SOURCE_PREEMPT_NONCRITICAL_REFRESH_ENABLED \
    PM_HARD_SOURCE_PREEMPT_NONCRITICAL_OWNERS
}

weather_refresh_apply_safe_defaults

weather_refresh_append_reason() {
  local current="${1:-}"
  local addition="${2:-}"
  if [ -z "$addition" ]; then
    printf '%s\n' "$current"
  elif [ -n "$current" ]; then
    printf '%s,%s\n' "$current" "$addition"
  else
    printf '%s\n' "$addition"
  fi
}

weather_refresh_log() {
  local log_path="${1:-}"
  local prefix="${2:-refresh}"
  local message="${3:-}"
  if [ -n "$log_path" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [${prefix}] ${message}" >> "$log_path"
  fi
}

weather_refresh_configure_lane() {
  local fallback_lane="${1:-weather_scan_lane}"
  local lane_name="$fallback_lane"
  if [ "${PM_REFRESH_COORDINATOR_UNIFIED_LANE_ENABLED:-1}" = "1" ]; then
    lane_name="${PM_REFRESH_COORDINATOR_LANE_NAME:-weather_refresh_lane}"
  fi
  WEATHER_REFRESH_EFFECTIVE_LANE_NAME="$lane_name"
  PM_WEATHER_REFRESH_EFFECTIVE_LANE_NAME="$lane_name"
  export WEATHER_REFRESH_EFFECTIVE_LANE_NAME PM_WEATHER_REFRESH_EFFECTIVE_LANE_NAME
  if declare -f weather_scan_lane_set_name >/dev/null 2>&1; then
    weather_scan_lane_set_name "$lane_name"
  fi
}

weather_refresh_csv_contains() {
  local needle="${1:-}"
  local csv="${2:-}"
  local item=""
  if [ -z "$needle" ]; then
    return 1
  fi
  IFS=',' read -r -a _weather_refresh_items <<< "$csv"
  for item in "${_weather_refresh_items[@]}"; do
    item="$(printf '%s' "$item" | xargs 2>/dev/null || printf '%s' "$item")"
    if [ "$item" = "$needle" ]; then
      return 0
    fi
  done
  return 1
}

weather_refresh_role_for_owner() {
  local owner="${1:-unknown}"
  if weather_refresh_csv_contains "$owner" "${PM_REFRESH_COORDINATOR_LIVE_OWNERS:-}"; then
    printf 'live\n'
  elif weather_refresh_csv_contains "$owner" "${PM_REFRESH_COORDINATOR_MODEL_OWNERS:-}"; then
    printf 'model\n'
  elif weather_refresh_csv_contains "$owner" "${PM_REFRESH_COORDINATOR_MARKET_OWNERS:-}"; then
    printf 'market\n'
  elif weather_refresh_csv_contains "$owner" "${PM_REFRESH_COORDINATOR_DATA_SOURCE_OWNERS:-}"; then
    printf 'data_source\n'
  else
    printf 'unknown\n'
  fi
}

weather_refresh_owner_is_live() {
  local owner="${1:-unknown}"
  local role=""
  role="$(weather_refresh_role_for_owner "$owner" 2>/dev/null || true)"
  [ "$role" = "live" ]
}

weather_refresh_register_entrypoint() {
  local owner="${1:-unknown}"
  local role="${2:-unknown}"
  local fallback_lane="${3:-${PM_REFRESH_COORDINATOR_LANE_NAME:-weather_refresh_lane}}"
  local log_path="${4:-}"
  local prefix="${5:-$owner}"
  local expected_role=""
  expected_role="$(weather_refresh_role_for_owner "$owner" 2>/dev/null || true)"
  if [ -z "$expected_role" ]; then
    expected_role="unknown"
  fi
  if [ "${PM_REFRESH_COORDINATOR_ENFORCE_OWNER_ROLE:-1}" = "1" ] \
    && [ "$expected_role" != "unknown" ] \
    && [ "$role" != "$expected_role" ]; then
    weather_refresh_log "$log_path" "$prefix" "coordinator owner role mismatch owner=${owner} declared=${role} expected=${expected_role}"
    return 1
  fi
  WEATHER_REFRESH_ENTRYPOINT_OWNER="$owner"
  WEATHER_REFRESH_ENTRYPOINT_ROLE="$role"
  PM_WEATHER_REFRESH_ENTRYPOINT_OWNER="$owner"
  PM_WEATHER_REFRESH_ENTRYPOINT_ROLE="$role"
  export WEATHER_REFRESH_ENTRYPOINT_OWNER WEATHER_REFRESH_ENTRYPOINT_ROLE
  export PM_WEATHER_REFRESH_ENTRYPOINT_OWNER PM_WEATHER_REFRESH_ENTRYPOINT_ROLE
  weather_refresh_configure_lane "$fallback_lane"
  weather_refresh_log "$log_path" "$prefix" "coordinator entry owner=${owner} role=${role} lane=${WEATHER_REFRESH_EFFECTIVE_LANE_NAME:-unknown} contract=${PM_REFRESH_COORDINATOR_ROLE_CONTRACT_VERSION:-unknown}"
  return 0
}

weather_refresh_unified_lane_enabled() {
  [ "${PM_REFRESH_COORDINATOR_ENABLED:-1}" = "1" ] \
    && [ "${PM_REFRESH_COORDINATOR_UNIFIED_LANE_ENABLED:-1}" = "1" ]
}

weather_refresh_legacy_peer_guards_enabled() {
  if weather_refresh_unified_lane_enabled \
    && [ "${PM_REFRESH_COORDINATOR_SUPPRESS_LEGACY_PEER_GUARDS:-1}" = "1" ]; then
    return 1
  fi
  return 0
}

weather_refresh_active_auto_trade_jobs() {
  local db_path="${ROOT:-}/data/archive.db"
  local py_bin="${PY:-python3}"
  if [ -z "${ROOT:-}" ] || [ ! -f "$db_path" ]; then
    echo "0"
    return 0
  fi
  "$py_bin" - "$db_path" <<'PY' 2>/dev/null || echo "0"
import sqlite3
import sys

db = sys.argv[1]
try:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=0.2)
    row = conn.execute(
        """
        SELECT COUNT(*)
        FROM auto_trade_jobs
        WHERE LOWER(COALESCE(state, '')) IN ('queued', 'running', 'retrying')
        """
    ).fetchone()
    print(int(row[0] or 0))
except Exception:
    print(0)
PY
}

weather_refresh_hard_source_recent_db_lock_count() {
  local hard_source_log="${ROOT:-}/data/logs/hard_source_direct_poller.log"
  local tail_lines="${PM_REFRESH_COORDINATOR_HARD_SOURCE_DB_LOCK_TAIL_LINES:-${PM_MODEL_SCAN_LIVE_SSOT_RECENT_DB_LOCK_TAIL_LINES:-40}}"
  local recent_sec="${PM_REFRESH_COORDINATOR_HARD_SOURCE_DB_LOCK_RECENT_SEC:-180}"
  if ! [[ "${tail_lines}" =~ ^[0-9]+$ ]] || [ "${tail_lines}" -lt 1 ]; then
    tail_lines=40
  fi
  if [ "$tail_lines" -gt 200 ]; then
    tail_lines=200
  fi
  if ! [[ "${recent_sec}" =~ ^[0-9]+$ ]] || [ "${recent_sec}" -lt 1 ]; then
    recent_sec=180
  fi
  if [ ! -f "$hard_source_log" ]; then
    echo "0"
    return 0
  fi
  "${PY:-python3}" - "$hard_source_log" "$tail_lines" "$recent_sec" <<'PY' 2>/dev/null || echo "0"
from collections import deque
import sys
from datetime import datetime, timezone

log_path, tail_lines_raw, recent_raw = sys.argv[1:4]
try:
    tail_lines = max(1, min(200, int(float(tail_lines_raw))))
except Exception:
    tail_lines = 40
try:
    recent_sec = float(recent_raw)
except Exception:
    recent_sec = 180.0
now = datetime.now(timezone.utc)
count = 0
try:
    with open(log_path, "r", encoding="utf-8", errors="replace") as handle:
        lines = deque(handle, maxlen=tail_lines)
except Exception:
    print(0)
    raise SystemExit(0)
for line in lines:
    if "OperationalError: database is locked" not in line:
        continue
    stamp = line.split(" ", 1)[0].strip()
    try:
        dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except Exception:
        continue
    if 0 <= (now - dt).total_seconds() <= recent_sec:
        count += 1
print(count)
PY
}

weather_refresh_event_final_daily_max_locked() {
  local event_date="${1:-}"
  local db_path="${ROOT:-}/data/archive.db"
  if [ -z "${event_date:-}" ] || [ ! -f "$db_path" ]; then
    echo "0"
    return 0
  fi
  "${PY:-python3}" - "$db_path" "$event_date" <<'PY' 2>/dev/null || echo "0"
import json
import sqlite3
import sys

db_path, event_date = sys.argv[1:3]
keys = (
    "final_bucket_lock",
    "hard_source_final_bucket_lock",
    "observed_today_max_final",
    "hard_source_final_daily_max",
    "hard_source_settlement_final",
    "temperature_observation_final_daily_max",
    "official_final_result",
    "final_daily_max_bucket_lock",
)

try:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.2)
    row = conn.execute(
        """
        SELECT h.fact_json
        FROM event_fact_latest l
        JOIN event_fact_history h ON h.id = l.fact_id
        WHERE l.event_date = ?
        LIMIT 1
        """,
        (event_date,),
    ).fetchone()
except Exception:
    print(0)
    raise SystemExit(0)
finally:
    try:
        conn.close()
    except Exception:
        pass

try:
    payload = json.loads(str(row[0] or "{}")) if row else {}
except Exception:
    payload = {}
features = payload.get("weather_likelihood_features")
if not isinstance(features, dict):
    features = payload.get("weather_likelihood_features_compact")
if not isinstance(features, dict):
    features = {}
posterior = payload.get("posterior_ssot_fact")
if not isinstance(posterior, dict):
    posterior = {}

for container in (payload, features, posterior):
    if any(bool(container.get(key)) for key in keys):
        print(1)
        raise SystemExit(0)
print(0)
PY
}

weather_refresh_live_ssot_critical_reason() {
  local owner="${1:-unknown}"
  if [ "${PM_REFRESH_COORDINATOR_ENABLED:-1}" != "1" ]; then
    return 1
  fi
  if [ "${PM_REFRESH_COORDINATOR_LIVE_PROTECT_ENABLED:-${PM_MODEL_SCAN_LIVE_SSOT_PROTECT_ENABLED:-1}}" != "1" ]; then
    return 1
  fi
  if weather_refresh_owner_is_live "$owner"; then
    return 1
  fi

  local reason=""
  local lock_dir="${ROOT:-}/data/live_ssot_writer.lock"
  local lock_pid=""
  local live_writer_event_date=""
  if [ -n "${ROOT:-}" ] && [ -d "$lock_dir" ]; then
    if [ -f "$lock_dir/pid" ]; then
      lock_pid="$(cat "$lock_dir/pid" 2>/dev/null || true)"
    fi
    if [ -z "${lock_pid:-}" ] || kill -0 "$lock_pid" 2>/dev/null; then
      if [ -f "$lock_dir/meta.json" ]; then
        live_writer_event_date="$("${PY:-python3}" - "$lock_dir/meta.json" <<'PY' 2>/dev/null || true
import json
import sys
try:
    payload = json.load(open(sys.argv[1], "r", encoding="utf-8"))
except Exception:
    payload = {}
print(str(payload.get("event_date") or "").strip())
PY
)"
      fi
      if [ "$(weather_refresh_event_final_daily_max_locked "$live_writer_event_date" | tail -n 1)" != "1" ]; then
        reason="$(weather_refresh_append_reason "$reason" "live_ssot_writer")"
      fi
    else
      rm -rf "$lock_dir" 2>/dev/null || true
    fi
  fi

  if [ "${PM_REFRESH_COORDINATOR_AWC_LOCK_GUARD:-1}" = "1" ] && [ -n "${ROOT:-}" ]; then
    local awc_lock_dir="${ROOT:-}/data/awc_fact_refresh.lock"
    local awc_lock_pid=""
    if [ -d "$awc_lock_dir" ]; then
      if [ -f "$awc_lock_dir/pid" ]; then
        awc_lock_pid="$(cat "$awc_lock_dir/pid" 2>/dev/null || true)"
      fi
      if [ -z "${awc_lock_pid:-}" ] || kill -0 "$awc_lock_pid" 2>/dev/null; then
        reason="$(weather_refresh_append_reason "$reason" "awc_fact_refresh")"
      else
        rm -rf "$awc_lock_dir" 2>/dev/null || true
      fi
    fi
  fi

  if [ "${PM_REFRESH_COORDINATOR_HARD_SOURCE_DB_LOCK_GUARD:-${PM_MODEL_SCAN_LIVE_SSOT_RECENT_DB_LOCK_GUARD:-1}}" = "1" ]; then
    local recent_lock_count
    recent_lock_count="$(weather_refresh_hard_source_recent_db_lock_count | tail -n 1)"
    if [[ "${recent_lock_count:-0}" =~ ^[0-9]+$ ]] && [ "${recent_lock_count:-0}" -gt 0 ]; then
      reason="$(weather_refresh_append_reason "$reason" "hard_source_recent_db_lock")"
    fi
  fi

  local watch_enabled="${PM_HARD_SOURCE_AWC_RELEASE_WATCH_ENABLED:-1}"
  local window_min="${PM_REFRESH_COORDINATOR_AWC_RELEASE_WATCH_WINDOW_MIN:-${PM_HARD_SOURCE_AWC_RELEASE_WATCH_WINDOW_MIN:-12}}"
  local acquire_start_min="${PM_HARD_SOURCE_AWC_ACQUIRE_WINDOW_START_MIN:-5}"
  local acquire_duration_sec="${PM_HARD_SOURCE_AWC_ACQUIRE_WINDOW_DURATION_SEC:-120}"
  local minute second
  minute="$(date -u '+%M')"
  second="$(date -u '+%S')"
  if [ "$watch_enabled" = "1" ] \
    && [[ "$window_min" =~ ^[0-9]+([.][0-9]+)?$ ]] \
    && [[ "$acquire_start_min" =~ ^[0-9]+([.][0-9]+)?$ ]] \
    && [[ "$acquire_duration_sec" =~ ^[0-9]+([.][0-9]+)?$ ]] \
    && [[ "$minute" =~ ^[0-9]+$ ]] \
    && [[ "$second" =~ ^[0-9]+$ ]] \
    && [ -n "${ROOT:-}" ] \
    && pgrep -f "$ROOT/scripts/hard_source_direct_poller.py --loop" >/dev/null 2>&1; then
    local in_release_watch
    in_release_watch="$("${PY:-python3}" - "$window_min" "$acquire_start_min" "$acquire_duration_sec" "$minute" "$second" <<'PY' 2>/dev/null || true
import sys

try:
    window = float(sys.argv[1])
    start = float(sys.argv[2])
    duration = float(sys.argv[3])
    minute = int(sys.argv[4])
    second = int(sys.argv[5])
except Exception:
    raise SystemExit(0)

offset = float(minute) + float(second) / 60.0
start = max(0.0, min(59.999, start))
duration = max(0.0, duration)
if duration <= 0.0:
    duration = max(0.0, window) * 60.0
end = min(60.0, start + duration / 60.0)
if start <= offset < end:
    print("1")
PY
    )"
    if [ "$in_release_watch" = "1" ]; then
      reason="$(weather_refresh_append_reason "$reason" "awc_release_watch_${minute}m")"
    fi
  fi

  if [ -n "$reason" ] && [ "${PM_REFRESH_COORDINATOR_FAST_RETURN_ON_FILE_SIGNAL:-1}" = "1" ]; then
    printf '%s\n' "$reason"
    return 0
  fi

  if [ "${PM_REFRESH_COORDINATOR_ACTIVE_JOBS_DB_GUARD:-0}" = "1" ]; then
    local active_jobs
    active_jobs="$(weather_refresh_active_auto_trade_jobs | tail -n 1)"
    if [[ "${active_jobs:-0}" =~ ^[0-9]+$ ]] && [ "${active_jobs:-0}" -gt 0 ]; then
      reason="$(weather_refresh_append_reason "$reason" "auto_trade_jobs=${active_jobs}")"
    fi
  fi

  if [ -n "$reason" ]; then
    printf '%s\n' "$reason"
    return 0
  fi
  return 1
}

weather_refresh_skip_if_live_ssot() {
  local owner="${1:-unknown}"
  local log_path="${2:-}"
  local prefix="${3:-$owner}"
  local reason
  reason="$(weather_refresh_live_ssot_critical_reason "$owner" 2>/dev/null || true)"
  if [ -n "${reason:-}" ]; then
    weather_refresh_log "$log_path" "$prefix" "live_ssot critical, skip owner=${owner} live=${reason}"
    return 0
  fi
  return 1
}

weather_refresh_awc_refresh_allowed() {
  local owner="${1:-unknown}"
  local log_path="${2:-}"
  local prefix="${3:-$owner}"
  local role=""
  local live_reason=""
  role="$(weather_refresh_role_for_owner "$owner" 2>/dev/null || true)"
  if [ -z "$role" ]; then
    role="unknown"
  fi
  if weather_refresh_owner_is_live "$owner"; then
    return 0
  fi
  if [ "${PM_REFRESH_COORDINATOR_ALLOW_NONLIVE_AWC_REFRESH:-0}" != "1" ]; then
    weather_refresh_log "$log_path" "$prefix" "awc_refresh blocked by coordinator owner=${owner} role=${role} live_owner_required=1"
    return 1
  fi
  live_reason="$(weather_refresh_live_ssot_critical_reason "$owner" 2>/dev/null || true)"
  if [ -n "${live_reason:-}" ]; then
    weather_refresh_log "$log_path" "$prefix" "awc_refresh blocked by live_ssot owner=${owner} live=${live_reason}"
    return 1
  fi
  return 0
}

weather_refresh_db_writer_acquire() {
  local owner="${1:-unknown}"
  local wait_sec="${2:-0}"
  local log_path="${3:-}"
  local prefix="${4:-$owner}"
  local poll_sec="${PM_DB_WRITER_LOCK_POLL_SEC:-1}"
  local lock_dir="${ROOT:-}/data/db_writer.lock"
  local lock_pid_file="$lock_dir/pid"
  local lock_owner_file="$lock_dir/owner"
  local waited=0
  local lock_pid=""
  local lock_owner=""
  local live_reason=""

  if [ -z "${ROOT:-}" ]; then
    return 1
  fi
  if [ "${WEATHER_REFRESH_DB_WRITER_LOCK_ACQUIRED:-0}" = "1" ]; then
    return 0
  fi
  if ! [[ "${wait_sec}" =~ ^[0-9]+$ ]] || [ "$wait_sec" -lt 0 ]; then
    wait_sec=0
  fi
  if ! [[ "${poll_sec}" =~ ^[0-9]+$ ]] || [ "$poll_sec" -lt 1 ]; then
    poll_sec=1
  fi

  live_reason="$(weather_refresh_live_ssot_critical_reason "$owner" 2>/dev/null || true)"
  if [ -n "${live_reason:-}" ]; then
    weather_refresh_log "$log_path" "$prefix" "db_writer blocked by live_ssot owner=${owner} live=${live_reason}"
    return 1
  fi

  while ! mkdir "$lock_dir" 2>/dev/null; do
    lock_pid=""
    lock_owner=""
    if [ -f "$lock_pid_file" ]; then
      lock_pid="$(cat "$lock_pid_file" 2>/dev/null || true)"
    fi
    if [ -f "$lock_owner_file" ]; then
      lock_owner="$(cat "$lock_owner_file" 2>/dev/null || true)"
    fi
    if [ -z "${lock_pid:-}" ] || ! kill -0 "$lock_pid" 2>/dev/null; then
      rm -rf "$lock_dir" 2>/dev/null || true
      continue
    fi
    if [ "${lock_pid:-}" = "$$" ]; then
      WEATHER_REFRESH_DB_WRITER_LOCK_ACQUIRED=1
      return 0
    fi
    live_reason="$(weather_refresh_live_ssot_critical_reason "$owner" 2>/dev/null || true)"
    if [ -n "${live_reason:-}" ]; then
      weather_refresh_log "$log_path" "$prefix" "db_writer wait aborted by live_ssot owner=${owner} live=${live_reason}"
      return 1
    fi
    if [ "$wait_sec" -le 0 ] || [ "$waited" -ge "$wait_sec" ]; then
      weather_refresh_log "$log_path" "$prefix" "db_writer busy, skip owner=${owner} held_by=${lock_owner:-unknown} pid=${lock_pid:-unknown} waited=${waited}s max=${wait_sec}s"
      return 1
    fi
    if [ "$waited" -eq 0 ]; then
      weather_refresh_log "$log_path" "$prefix" "db_writer busy, wait owner=${owner} held_by=${lock_owner:-unknown} pid=${lock_pid:-unknown} max=${wait_sec}s"
    fi
    sleep "$poll_sec"
    waited=$((waited + poll_sec))
  done
  echo "$$" > "$lock_pid_file"
  echo "$owner" > "$lock_owner_file" 2>/dev/null || true
  WEATHER_REFRESH_DB_WRITER_LOCK_ACQUIRED=1
  if [ "$waited" -gt 0 ]; then
    weather_refresh_log "$log_path" "$prefix" "db_writer acquired owner=${owner} after=${waited}s"
  fi
  return 0
}

weather_refresh_db_writer_release() {
  if [ "${WEATHER_REFRESH_DB_WRITER_LOCK_ACQUIRED:-0}" != "1" ]; then
    return 0
  fi
  local lock_dir="${ROOT:-}/data/db_writer.lock"
  local lock_pid_file="$lock_dir/pid"
  local cur_pid=""
  if [ -f "$lock_pid_file" ]; then
    cur_pid="$(cat "$lock_pid_file" 2>/dev/null || true)"
  fi
  if [ -z "${cur_pid:-}" ] || [ "$cur_pid" = "$$" ]; then
    rm -rf "$lock_dir" 2>/dev/null || true
  fi
  WEATHER_REFRESH_DB_WRITER_LOCK_ACQUIRED=0
}

weather_refresh_facts_materialize_allowed() {
  local owner="${1:-unknown}"
  local log_path="${2:-}"
  local prefix="${3:-$owner}"
  local enabled="${PM_FACTS_MATERIALIZE_AFTER_SCAN:-0}"
  local live_reason=""
  local role=""
  if [ "$enabled" != "1" ]; then
    weather_refresh_log "$log_path" "$prefix" "facts_materialize disabled by coordinator owner=${owner}"
    return 1
  fi
  if ! weather_refresh_csv_contains "$owner" "${PM_REFRESH_COORDINATOR_FACTS_WRITE_OWNERS:-weatherctl_facts,daily_scan}"; then
    role="$(weather_refresh_role_for_owner "$owner" 2>/dev/null || true)"
    if [ -z "$role" ]; then
      role="unknown"
    fi
    if [ "${PM_REFRESH_COORDINATOR_ALLOW_SCAN_FACTS_MATERIALIZE:-0}" != "1" ]; then
      weather_refresh_log "$log_path" "$prefix" "facts_materialize blocked by coordinator owner=${owner} role=${role} allowed_owners=${PM_REFRESH_COORDINATOR_FACTS_WRITE_OWNERS:-weatherctl_facts,daily_scan}"
      return 1
    fi
  fi
  live_reason="$(weather_refresh_live_ssot_critical_reason "$owner" 2>/dev/null || true)"
  if [ -n "${live_reason:-}" ]; then
    weather_refresh_log "$log_path" "$prefix" "facts_materialize blocked by live_ssot owner=${owner} live=${live_reason}"
    return 1
  fi
  return 0
}
