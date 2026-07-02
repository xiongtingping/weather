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

. "$ROOT/scripts/lib/resolve_project_python.sh"
PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
  PY="$(resolve_project_python "$ROOT" || true)"
fi
if [ -z "${PY:-}" ] || [ ! -x "$PY" ]; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] missing project python runtime" >> "$ROOT/data/model_scan.log"
  exit 1
fi
. "$ROOT/scripts/lib/run_with_timeout.sh"
. "$ROOT/scripts/lib/process_governance.sh"
. "$ROOT/scripts/lib/weather_refresh_coordinator.sh"
if ! weather_refresh_register_entrypoint "model_scan" "model" "${PM_MODEL_SCAN_LANE_NAME:-${PM_REFRESH_COORDINATOR_LANE_NAME:-weather_refresh_lane}}" "$ROOT/data/model_scan.log" "model_scan"; then
  exit 0
fi
if weather_refresh_skip_if_live_ssot "model_scan" "$ROOT/data/model_scan.log" "model_scan"; then
  exit 0
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
    echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] paused by fd_degrade until=${fd_note}" >> "$ROOT/data/model_scan.log"
    exit 0
  fi
fi

MODEL_SCAN_NICE="$(normalize_nice_level "${PM_MODEL_SCAN_NICE:-15}" 15)"

_facts_materialize_window_from_scan() {
  local run_date="${1:-}"
  local days_arg="${2:-}"
  local include_yesterday="${3:-0}"
  "$PY" - "$ROOT/data/archive.db" "$run_date" "$days_arg" "$include_yesterday" <<'PY'
import sqlite3
import sys
from datetime import date, timedelta

db_path, run_date_raw, days_arg_raw, include_yesterday_raw = sys.argv[1:5]
run_date_text = str(run_date_raw or "").strip().split("T", 1)[0]
try:
    run_day = date.fromisoformat(run_date_text)
except Exception:
    print("")
    print("1")
    raise SystemExit(0)

include_yesterday = str(include_yesterday_raw or "").strip().lower() in {"1", "true", "yes", "on"}
days_ahead = 0
parts = str(days_arg_raw or "").strip().split()
if len(parts) >= 2:
    try:
        days_ahead = int(parts[1])
    except Exception:
        days_ahead = 0

end_day = run_day + timedelta(days=max(0, days_ahead))
if days_ahead < 0:
    conn = None
    try:
        conn = sqlite3.connect(db_path, timeout=1.0)
        cur = conn.cursor()
        row = cur.execute(
            """
            SELECT MAX(event_date)
            FROM (
              SELECT event_date FROM model_snapshots WHERE run_date = ?
              UNION ALL
              SELECT event_date FROM market_snapshots WHERE run_date = ?
              UNION ALL
              SELECT event_date FROM event_runs WHERE run_date = ?
            )
            """,
            (run_date_text, run_date_text, run_date_text),
        ).fetchone()
        candidate_text = str((row[0] if row else "") or "").strip().split("T", 1)[0]
        if candidate_text:
            candidate_day = date.fromisoformat(candidate_text)
            if candidate_day > end_day:
                end_day = candidate_day
    except Exception:
        pass
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass

start_day = run_day - timedelta(days=1) if include_yesterday else run_day
if end_day < start_day:
    end_day = start_day
days = max(1, (end_day - start_day).days + 1)
print(end_day.isoformat())
print(days)
PY
}

_model_scan_materialize_stale_facts() {
  local reason="${1:-fact_stale_yield}"
  local live_reason
  local lane_acquired_here=0
  local db_writer_acquired_here=0
  if ! weather_refresh_facts_materialize_allowed "model_scan_yield" "$ROOT/data/model_scan.log" "model_scan"; then
    return 0
  fi
  live_reason="$(_model_scan_live_ssot_critical_reason 2>/dev/null || true)"
  if [ -n "${live_reason:-}" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] skip facts_materialize_on_yield for live_ssot reason=${reason} live=${live_reason}" >> "$ROOT/data/model_scan.log"
    return 0
  fi
  if [ "${MODEL_FACT_STALE_BYPASS:-0}" != "1" ]; then
    return 0
  fi
  if [ "${PM_MODEL_SCAN_FACTS_MATERIALIZE_ON_YIELD:-0}" != "1" ]; then
    return 0
  fi
  local stamp_path="${PM_MODEL_SCAN_FACTS_MATERIALIZE_ON_YIELD_STAMP:-$ROOT/data/model_scan.facts_yield_materialize_ts}"
  local min_sec="${PM_MODEL_SCAN_FACTS_MATERIALIZE_ON_YIELD_MIN_SEC:-180}"
  if ! [[ "${min_sec}" =~ ^[0-9]+$ ]]; then
    min_sec=180
  fi
  local now_ts last_ts age_sec
  now_ts="$(date +%s)"
  last_ts=0
  if [ -f "$stamp_path" ]; then
    last_ts="$(cat "$stamp_path" 2>/dev/null || echo 0)"
  fi
  if ! [[ "${last_ts}" =~ ^[0-9]+$ ]] || [ "${last_ts}" -gt "${now_ts}" ]; then
    last_ts=0
  fi
  age_sec=$((now_ts - last_ts))
  if [ "${last_ts}" -gt 0 ] && [ "${age_sec}" -lt "${min_sec}" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] facts_materialize_on_yield throttled reason=${reason} age_sec=${age_sec} min_sec=${min_sec} fact_age_sec=${MODEL_FACT_AGE_SEC}" >> "$ROOT/data/model_scan.log"
    return 0
  fi

  local run_date facts_days_arg facts_window facts_end_date facts_days timeout_sec facts_rc
  if [ "${WEATHER_SCAN_LANE_LOCK_ACQUIRED:-0}" != "1" ]; then
    if ! weather_scan_lane_acquire "model_scan_facts_materialize" "${PM_MODEL_SCAN_FACTS_MATERIALIZE_ON_YIELD_LANE_WAIT_SEC:-0}" "$ROOT/data/model_scan.log" "model_scan"; then
      echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] facts_materialize_on_yield skipped lane busy reason=${reason}" >> "$ROOT/data/model_scan.log"
      return 0
    fi
    lane_acquired_here=1
  fi
  if [ "${WEATHER_REFRESH_DB_WRITER_LOCK_ACQUIRED:-0}" != "1" ]; then
    if ! weather_refresh_db_writer_acquire "model_scan_facts_materialize" "${PM_MODEL_SCAN_FACTS_MATERIALIZE_ON_YIELD_DB_LOCK_WAIT_SEC:-0}" "$ROOT/data/model_scan.log" "model_scan"; then
      if [ "$lane_acquired_here" -eq 1 ]; then
        weather_scan_lane_release
      fi
      echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] facts_materialize_on_yield skipped db_writer busy reason=${reason}" >> "$ROOT/data/model_scan.log"
      return 0
    fi
    db_writer_acquired_here=1
  fi
  run_date="$(TZ=America/New_York date '+%Y-%m-%d')"
  facts_days_arg="--days-ahead ${PM_MODEL_SCAN_FACTS_ON_YIELD_DAYS_AHEAD:-${PM_SCAN_FAST_DAYS_AHEAD:-2}}"
  facts_window="$(_facts_materialize_window_from_scan "$run_date" "$facts_days_arg" "${PM_MODEL_SCAN_FACTS_INCLUDE_YESTERDAY:-0}")"
  facts_end_date="$(printf '%s\n' "$facts_window" | sed -n '1p')"
  facts_days="$(printf '%s\n' "$facts_window" | sed -n '2p')"
  if [ -z "${facts_end_date:-}" ] || ! [[ "${facts_days:-}" =~ ^[0-9]+$ ]]; then
    facts_end_date="$run_date"
    facts_days=1
  fi
  timeout_sec="$(normalize_positive_int_max "${PM_MODEL_SCAN_FACTS_MATERIALIZE_ON_YIELD_TIMEOUT_SEC:-45}" 45 90)"
  echo "$now_ts" > "$stamp_path" 2>/dev/null || true
  echo "---- $(date '+%Y-%m-%d %H:%M:%S') facts_materialize_on_yield start (reason=${reason} run_date=${run_date} end=${facts_end_date} days=${facts_days} timeout=${timeout_sec}s fact_age_sec=${MODEL_FACT_AGE_SEC}) ----" >> "$ROOT/data/model_scan.log"
  set +e
  run_with_timeout "$timeout_sec" "$ROOT/data/model_scan.log" \
    env PM_FACTS_MATERIALIZE_CURRENT_RUN_ONLY=0 \
    "$PY" "$ROOT/ui/server.py" --materialize-facts --facts-date "$facts_end_date" --facts-days "$facts_days" --write-scope "${PM_ARCHIVE_WRITE_SCOPE:-chengdu_zuuu}"
  facts_rc="$?"
  set -e
  if [ "$facts_rc" -ne 0 ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] facts_materialize_on_yield failed reason=${reason} rc=${facts_rc}" >> "$ROOT/data/model_scan.log"
  fi
  if [ "$db_writer_acquired_here" -eq 1 ]; then
    weather_refresh_db_writer_release
  fi
  if [ "$lane_acquired_here" -eq 1 ]; then
    weather_scan_lane_release
  fi
  return 0
}

_weather_likelihood_age_sec() {
  local db_path="${1:-$ROOT/data/archive.db}"
  local run_date="${2:-}"
  local event_date="${3:-}"
  local write_scope="${4:-${PM_ARCHIVE_WRITE_SCOPE:-}}"
  if [ ! -f "$db_path" ]; then
    echo "999999"
    return 0
  fi
  "$PY" - "$db_path" "$run_date" "$event_date" "$write_scope" <<'PY' 2>/dev/null || echo "999999"
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

db_path, run_date, event_date, write_scope = sys.argv[1:5]
run_date = str(run_date or "").strip().split("T", 1)[0]
event_date = str(event_date or "").strip().split("T", 1)[0]
dates = []
date_inputs = (event_date,) if event_date else (run_date,)
for value in date_inputs:
    if value and value not in dates:
        dates.append(value)
if not run_date or not dates:
    print(999999)
    raise SystemExit(0)

slug_filter = ""
params = [run_date]
date_placeholders = ",".join("?" for _ in dates)
params.extend(dates)
if str(write_scope or "").strip() == "chengdu_zuuu":
    slug_filter = " AND h.slug >= ? AND h.slug < ?"
    params.extend(["highest-temperature-in-chengdu-", "highest-temperature-in-chengdu."])

try:
    conn = sqlite3.connect(db_path, timeout=1.0)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"""
        SELECT h.slug, h.event_date, h.fact_json
        FROM event_fact_latest l
        JOIN event_fact_history h ON h.id = l.fact_id
        WHERE h.run_date = ?
          AND h.event_date IN ({date_placeholders})
          {slug_filter}
        """,
        tuple(params),
    ).fetchall()
finally:
    try:
        conn.close()
    except Exception:
        pass

if not rows:
    print(999999)
    raise SystemExit(0)

def _parse_dt(text):
    text = str(text or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        try:
            dt = datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def _payload_weather_features(payload):
    if not isinstance(payload, dict):
        return {}
    features = payload.get("weather_likelihood_features")
    if not isinstance(features, dict):
        features = payload.get("weather_likelihood_features_compact")
    return features if isinstance(features, dict) else {}

def _latest_ref(*values):
    best_text = ""
    best_dt = None
    for value in values:
        dt = _parse_dt(value)
        if dt is None:
            continue
        if best_dt is None or dt > best_dt:
            best_dt = dt
            best_text = str(value or "").strip()
    return best_text

def _weather_refs_from_payload(raw):
    try:
        payload = json.loads(str(raw or "") or "{}")
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        return []
    refs = []
    for key in (
        "weather_likelihood_refreshed_at",
        "bayes_likelihood_refreshed_at",
    ):
        value = str(payload.get(key) or "").strip()
        if value:
            refs.append(value)
            break
    observed = payload.get("observed_nowcast")
    if isinstance(observed, dict):
        value = str(observed.get("refreshed_at") or "").strip()
        if value:
            refs.append(value)
    for rows_key in ("distribution", "rows"):
        rows_value = payload.get(rows_key)
        if isinstance(rows_value, list):
            for item in rows_value:
                if isinstance(item, dict):
                    value = str(item.get("weather_likelihood_refreshed_at") or "").strip()
                    if value:
                        refs.append(value)
                        break
            if refs:
                break
    if os.environ.get("PM_MODEL_SCAN_WU_TWC_OBSERVED_STALE_GUARD", "1") == "1":
        features = _payload_weather_features(payload)
        station = str(payload.get("station_id") or payload.get("station") or "").upper()
        market_var = str(payload.get("market_var") or "")
        if market_var == "temperature_2m_max" and station == "ZUUU":
            wu_current_ref = features.get("wu_twc_observed_intraday_current_at")
            wu_max_ref = features.get("wu_twc_observed_intraday_today_max_at")
            wu_available = str(features.get("wu_twc_observed_intraday_available") or "").strip().lower()
            wu_has_observation_ref = bool(_latest_ref(wu_current_ref, wu_max_ref))
            if wu_has_observation_ref or wu_available in {"1", "true", "yes", "on"}:
                wu_ref = _latest_ref(
                    wu_current_ref,
                    wu_max_ref,
                    features.get("wu_twc_observed_intraday_fetched_at"),
                )
                if wu_ref:
                    refs.append(wu_ref)
                else:
                    refs.append("")
            elif not any(str(key).startswith("wu_twc_observed_intraday") for key in features):
                refs.append("")
    if os.environ.get("PM_MODEL_SCAN_PRECIP_NOWCAST_STALE_GUARD", "1") == "1":
        features = _payload_weather_features(payload)
        station = str(payload.get("station_id") or payload.get("station") or "").upper()
        market_var = str(payload.get("market_var") or "")
        if market_var == "temperature_2m_max" and station == "ZUUU":
            nowcast_ref = _latest_ref(
                features.get("precip_nowcast_1_3h_checked_at"),
                features.get("forecast_precip_nowcast_1_3h_checked_at"),
                features.get("precip_nowcast_1_3h_reference_time"),
                features.get("forecast_precip_nowcast_1_3h_reference_time"),
            )
            if nowcast_ref:
                refs.append(nowcast_ref)
            elif not any(
                str(key).startswith("precip_nowcast_1_3h")
                or str(key).startswith("forecast_precip_nowcast_1_3h")
                for key in features
            ):
                refs.append("")
    return refs

now = datetime.now(timezone.utc)
max_age = None
for row in rows:
    ref_ages = []
    for ref_text in _weather_refs_from_payload(row["fact_json"]):
        ref_dt = _parse_dt(ref_text)
        ref_ages.append(999999 if ref_dt is None else int(max(0, (now - ref_dt).total_seconds())))
    age = max(ref_ages) if ref_ages else 999999
    max_age = age if max_age is None else max(max_age, age)
print(max_age if max_age is not None else 999999)
PY
}

_temperature_likelihood_dynamic_stale_sec() {
  local event_date="${1:-}"
  local fallback_sec="${2:-900}"
  if [ -z "${event_date:-}" ]; then
    echo "$fallback_sec"
    return 0
  fi
  "$PY" - "$event_date" "$fallback_sec" <<'PY' 2>/dev/null || echo "$fallback_sec"
import math
import sys

event_date, fallback_raw = sys.argv[1:3]
try:
    fallback = int(float(fallback_raw))
except Exception:
    fallback = 900
try:
    from scan_weather_market import _temperature_peak_likelihood_refresh_interval_sec

    interval = _temperature_peak_likelihood_refresh_interval_sec(
        event_date,
        "Asia/Shanghai",
        "temperature_2m_max",
    )
except Exception:
    interval = None
if interval is None:
    print(fallback)
    raise SystemExit(0)
try:
    value = int(max(1, round(float(interval))))
except Exception:
    value = fallback
if value <= 0 or not math.isfinite(float(value)):
    value = fallback
print(value)
PY
}

_model_scan_event_days_ahead_from_run_date() {
  local run_date="${1:-}"
  local event_date="${2:-}"
  "$PY" - "$run_date" "$event_date" <<'PY' 2>/dev/null || echo "0"
import sys
from datetime import date

run_raw, event_raw = sys.argv[1:3]
try:
    run_day = date.fromisoformat(str(run_raw or "").strip().split("T", 1)[0])
    event_day = date.fromisoformat(str(event_raw or "").strip().split("T", 1)[0])
except Exception:
    print(0)
    raise SystemExit(0)
delta = (event_day - run_day).days
if delta < 0:
    delta = 0
if delta > 1:
    delta = 1
print(delta)
PY
}

_model_scan_awc_refresh_skip_scan_allowed() {
  [ "${PM_MODEL_SCAN_AWC_FACT_REFRESH_SKIP_SCAN:-0}" = "1" ] \
    && [ "${MODEL_WEATHER_LIKELIHOOD_STALE_BYPASS:-0}" != "1" ] \
    && [ "${WEATHER_DISCOVERY_SIGNAL_ACTIVE:-0}" != "1" ] \
    && [ "${PM_MODEL_SCAN_FORCE_RUN:-0}" != "1" ]
}

_model_scan_refresh_weather_likelihood_stale_state() {
  MODEL_WEATHER_LIKELIHOOD_AGE_SEC="$(_weather_likelihood_age_sec "$ROOT/data/archive.db" "$RUN_DATE_ET" "$MODEL_WEATHER_LIKELIHOOD_EVENT_DATE" "${PM_ARCHIVE_WRITE_SCOPE:-chengdu_zuuu}" | head -n 1)"
  if ! [[ "${MODEL_WEATHER_LIKELIHOOD_AGE_SEC:-}" =~ ^[0-9]+$ ]]; then
    MODEL_WEATHER_LIKELIHOOD_AGE_SEC=999999
  fi
  MODEL_WEATHER_LIKELIHOOD_STALE_BYPASS=0
  if [ "${MODEL_WEATHER_LIKELIHOOD_AGE_SEC}" -ge "${PM_MODEL_SCAN_WEATHER_LIKELIHOOD_STALE_BYPASS_SEC}" ]; then
    MODEL_WEATHER_LIKELIHOOD_STALE_BYPASS=1
  fi
}

_model_scan_run_awc_fact_refresh() {
  local phase="${1:-model_scan}"
  local live_reason=""
  local lock_dir="$ROOT/data/awc_fact_refresh.lock"
  local lock_pid_file="$lock_dir/pid"
  local lock_phase_file="$lock_dir/phase"
  local lock_pid=""
  local awc_fact_refresh_rc=0
  local lock_acquired=0
  local lane_acquired_here=0
  local db_writer_acquired_here=0
  local awc_date="${MODEL_WEATHER_LIKELIHOOD_EVENT_DATE:-}"
  local stamp_file=""
  local stamp_start_ts=""
  local min_interval_sec="${PM_MODEL_SCAN_AWC_FACT_REFRESH_MIN_INTERVAL_SEC:-300}"
  if ! weather_refresh_awc_refresh_allowed "model_scan_awc_fact_refresh" "$ROOT/data/model_scan.log" "model_scan"; then
    return 75
  fi
  live_reason="$(weather_refresh_live_ssot_critical_reason "model_scan_awc_fact_refresh" 2>/dev/null || true)"
  if [ -n "${live_reason:-}" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] awc_fact_refresh skipped phase=${phase} live=${live_reason}" >> "$ROOT/data/model_scan.log"
    return 75
  fi
  if ! [[ "${min_interval_sec}" =~ ^[0-9]+$ ]]; then
    min_interval_sec=300
  fi
  if [ "${min_interval_sec}" -gt 0 ] && [ -n "${awc_date}" ]; then
    stamp_file="$ROOT/data/awc_fact_refresh.${awc_date}.last_start"
    local now_ts
    local last_ts
    now_ts="$(date '+%s')"
    last_ts=0
    if [ -f "$stamp_file" ]; then
      last_ts="$(cat "$stamp_file" 2>/dev/null || echo 0)"
    fi
    if ! [[ "${last_ts}" =~ ^[0-9]+$ ]]; then
      last_ts=0
    fi
    if [ "$last_ts" -gt 0 ] && [ $((now_ts - last_ts)) -lt "$min_interval_sec" ]; then
      echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] awc_fact_refresh cooldown phase=${phase} age_sec=$((now_ts - last_ts)) min_sec=${min_interval_sec}; continue regular scan" >> "$ROOT/data/model_scan.log"
      return 76
    fi
    stamp_start_ts="$now_ts"
  fi
  PM_MODEL_SCAN_AWC_FACT_REFRESH_TIMEOUT_SEC="$(normalize_positive_int_max "${PM_MODEL_SCAN_AWC_FACT_REFRESH_TIMEOUT_SEC:-12}" 12 30)"
  if [ "${PM_MODEL_SCAN_AWC_FACT_REFRESH_LOCK_ENABLED:-1}" = "1" ]; then
    if ! mkdir "$lock_dir" 2>/dev/null; then
      if [ -f "$lock_pid_file" ]; then
        lock_pid="$(cat "$lock_pid_file" 2>/dev/null || true)"
      fi
      if [[ "${lock_pid:-}" =~ ^[0-9]+$ ]] && kill -0 "$lock_pid" 2>/dev/null; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] awc_fact_refresh busy phase=${phase} owner_pid=${lock_pid}" >> "$ROOT/data/model_scan.log"
        return 75
      fi
      rm -rf "$lock_dir" 2>/dev/null || true
      mkdir "$lock_dir" 2>/dev/null || {
        echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] awc_fact_refresh lock unavailable phase=${phase}" >> "$ROOT/data/model_scan.log"
        return 75
      }
    fi
    echo "$$" > "$lock_pid_file" 2>/dev/null || true
    echo "$phase" > "$lock_phase_file" 2>/dev/null || true
    lock_acquired=1
  fi
  if [ "${WEATHER_SCAN_LANE_LOCK_ACQUIRED:-0}" != "1" ]; then
    if ! weather_scan_lane_acquire "model_scan_awc_fact_refresh" "${PM_MODEL_SCAN_AWC_FACT_REFRESH_LANE_WAIT_SEC:-0}" "$ROOT/data/model_scan.log" "model_scan"; then
      if [ "$lock_acquired" -eq 1 ]; then
        rm -rf "$lock_dir" 2>/dev/null || true
      fi
      echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] awc_fact_refresh lane busy phase=${phase}" >> "$ROOT/data/model_scan.log"
      return 75
    fi
    lane_acquired_here=1
  fi
  if [ "${WEATHER_REFRESH_DB_WRITER_LOCK_ACQUIRED:-0}" != "1" ]; then
    if ! weather_refresh_db_writer_acquire "model_scan_awc_fact_refresh" "${PM_MODEL_SCAN_AWC_FACT_REFRESH_DB_LOCK_WAIT_SEC:-0}" "$ROOT/data/model_scan.log" "model_scan"; then
      if [ "$lock_acquired" -eq 1 ]; then
        rm -rf "$lock_dir" 2>/dev/null || true
      fi
      if [ "$lane_acquired_here" -eq 1 ]; then
        weather_scan_lane_release
      fi
      echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] awc_fact_refresh db_writer busy phase=${phase}" >> "$ROOT/data/model_scan.log"
      return 75
    fi
    db_writer_acquired_here=1
  fi
  if [ -n "${stamp_file:-}" ] && [ -n "${stamp_start_ts:-}" ]; then
    echo "$stamp_start_ts" > "$stamp_file" 2>/dev/null || true
  fi
  echo "---- $(date '+%Y-%m-%d %H:%M:%S') awc_fact_refresh start phase=${phase} (event_date=${MODEL_WEATHER_LIKELIHOOD_EVENT_DATE} timeout=${PM_MODEL_SCAN_AWC_FACT_REFRESH_TIMEOUT_SEC}s) ----" >> "$ROOT/data/model_scan.log"
  set +e
  run_with_timeout "${PM_MODEL_SCAN_AWC_FACT_REFRESH_TIMEOUT_SEC}" "$ROOT/data/model_scan.log" \
    env PM_AWC_FACT_REFRESH_HEARTBEAT_SEC="${PM_AWC_FACT_REFRESH_HEARTBEAT_SEC:-30}" \
    "$PY" "$ROOT/ui/server.py" --refresh-awc-facts --awc-date "$MODEL_WEATHER_LIKELIHOOD_EVENT_DATE"
  awc_fact_refresh_rc="$?"
  set -e
  if [ "$lock_acquired" -eq 1 ]; then
    rm -rf "$lock_dir" 2>/dev/null || true
  fi
  if [ "$db_writer_acquired_here" -eq 1 ]; then
    weather_refresh_db_writer_release
  fi
  if [ "$lane_acquired_here" -eq 1 ]; then
    weather_scan_lane_release
  fi
  echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] awc_fact_refresh rc=${awc_fact_refresh_rc} phase=${phase}" >> "$ROOT/data/model_scan.log"
  return "$awc_fact_refresh_rc"
}

_model_scan_run_precip_nowcast_fact_refresh() {
  local phase="${1:-model_scan}"
  local nowcast_rc=0
  local nowcast_date="${MODEL_WEATHER_LIKELIHOOD_EVENT_DATE:-}"
  local timeout_sec="${PM_MODEL_SCAN_PRECIP_NOWCAST_FACT_REFRESH_TIMEOUT_SEC:-60}"
  local live_reason=""
  local lane_acquired_here=0
  local db_writer_acquired_here=0
  live_reason="$(weather_refresh_live_ssot_critical_reason "model_scan_precip_nowcast_refresh" 2>/dev/null || true)"
  if [ -n "${live_reason:-}" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] precip_nowcast_fact_refresh skipped phase=${phase} live=${live_reason}" >> "$ROOT/data/model_scan.log"
    return 75
  fi
  if ! [[ "${timeout_sec}" =~ ^[0-9]+$ ]] || [ "${timeout_sec}" -le 0 ]; then
    timeout_sec=60
  fi
  if [ "${timeout_sec}" -gt 60 ]; then
    timeout_sec=60
  fi
  if [ -z "${nowcast_date}" ]; then
    return 2
  fi
  if [ "${WEATHER_SCAN_LANE_LOCK_ACQUIRED:-0}" != "1" ]; then
    if ! weather_scan_lane_acquire "model_scan_precip_nowcast_refresh" "${PM_MODEL_SCAN_PRECIP_NOWCAST_FACT_REFRESH_LANE_WAIT_SEC:-0}" "$ROOT/data/model_scan.log" "model_scan"; then
      echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] precip_nowcast_fact_refresh lane busy phase=${phase}" >> "$ROOT/data/model_scan.log"
      return 75
    fi
    lane_acquired_here=1
  fi
  if [ "${WEATHER_REFRESH_DB_WRITER_LOCK_ACQUIRED:-0}" != "1" ]; then
    if ! weather_refresh_db_writer_acquire "model_scan_precip_nowcast_refresh" "${PM_MODEL_SCAN_PRECIP_NOWCAST_FACT_REFRESH_DB_LOCK_WAIT_SEC:-0}" "$ROOT/data/model_scan.log" "model_scan"; then
      if [ "$lane_acquired_here" -eq 1 ]; then
        weather_scan_lane_release
      fi
      echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] precip_nowcast_fact_refresh db_writer busy phase=${phase}" >> "$ROOT/data/model_scan.log"
      return 75
    fi
    db_writer_acquired_here=1
  fi
  echo "---- $(date '+%Y-%m-%d %H:%M:%S') precip_nowcast_fact_refresh start phase=${phase} (event_date=${nowcast_date} timeout=${timeout_sec}s) ----" >> "$ROOT/data/model_scan.log"
  set +e
  run_with_timeout "${timeout_sec}" "$ROOT/data/model_scan.log" \
    "$PY" "$ROOT/ui/server.py" --refresh-precip-nowcast-facts --facts-date "$nowcast_date" --write-scope "${PM_ARCHIVE_WRITE_SCOPE:-chengdu_zuuu}"
  nowcast_rc="$?"
  set -e
  if [ "$db_writer_acquired_here" -eq 1 ]; then
    weather_refresh_db_writer_release
  fi
  if [ "$lane_acquired_here" -eq 1 ]; then
    weather_scan_lane_release
  fi
  echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] precip_nowcast_fact_refresh rc=${nowcast_rc} phase=${phase}" >> "$ROOT/data/model_scan.log"
  return "$nowcast_rc"
}

_cap_model_refresh_interval() {
  local cap_min="${1:-}"
  local reason="${2:-freshness_stale}"
  if ! [[ "${cap_min}" =~ ^[0-9]+$ ]] || [ "${cap_min}" -le 0 ]; then
    return 0
  fi
  local current="${PM_SCAN_MODEL_REFRESH_INTERVAL_MINUTES:-$cap_min}"
  if ! [[ "${current}" =~ ^-?[0-9]+$ ]]; then
    current="$cap_min"
  fi
  if [ "${current}" -le 0 ]; then
    return 0
  fi
  if [ "${current}" -gt "${cap_min}" ]; then
    export PM_SCAN_MODEL_REFRESH_INTERVAL_MINUTES="$cap_min"
    echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] cap model/weather refresh interval reason=${reason} previous=${current}m cap=${cap_min}m" >> "$ROOT/data/model_scan.log"
  fi
  local bg_current="${PM_MODEL_BACKGROUND_REFRESH_MINUTES:-$cap_min}"
  if [[ "${bg_current}" =~ ^[0-9]+$ ]] && [ "${bg_current}" -gt "${cap_min}" ]; then
    export PM_MODEL_BACKGROUND_REFRESH_MINUTES="$cap_min"
  fi
}

# Auto-trade running: do NOT stop scanning. Instead, focus the scan on Top OK (auto-trade entry_ok)
# and execution-related slugs (positions/open orders) to keep the pipeline fresh with minimal fan-out.
AUTO_LOCK_DIR="$ROOT/data/auto_trade_runtime.lock"
AUTO_LOCK_META="$AUTO_LOCK_DIR/meta.json"
: "${PM_AUTO_TRADE_RUNTIME_LOCK_STALE_SEC:=1800}"
AUTO_TRADE_ACTIVE=0
_auto_trade_worker_active() {
  auto_trade_worker_process_active \
    "${PM_AUTO_TRADE_WORKER_ACTIVE_MAX_SEC:-900}" \
    "$ROOT/data/model_scan.log" \
    "model_scan"
}
_auto_trade_entry_active() {
  pgrep -f "scripts/auto_trade_entry_1m.sh" >/dev/null 2>&1 \
    || pgrep -f "scan_weather_market.py .*--auto-trade-enqueue" >/dev/null 2>&1 \
    || pgrep -f "weather_strategy_batch.py .*--enqueue" >/dev/null 2>&1
}
_queued_entry_job_count() {
  local db_path="$ROOT/data/archive.db"
  if [ ! -f "$db_path" ]; then
    echo "0"
    return 0
  fi
  local count=""
  count="$(sqlite3 "$db_path" \
    "PRAGMA busy_timeout=100; SELECT COALESCE(COUNT(*),0) FROM auto_trade_jobs WHERE source='entry_1m' AND state='queued';" \
    2>/dev/null | tail -n 1 | tr -dc '0-9')"
  if [ -z "${count:-}" ]; then
    count="0"
  fi
  echo "$count"
}
if [ -d "$AUTO_LOCK_DIR" ]; then
  age_sec="$(AUTO_META="$AUTO_LOCK_META" "$PY" - <<'PY' 2>/dev/null || true
import json, os, datetime
path = (os.environ.get("AUTO_META") or "").strip()
try:
    with open(path, "r", encoding="utf-8") as f:
        meta = json.load(f) or {}
except Exception:
    meta = {}
started = str(meta.get("started_at") or "").strip()
if not started:
    raise SystemExit(0)
try:
    dt = datetime.datetime.fromisoformat(started.replace("Z", "+00:00"))
except Exception:
    raise SystemExit(0)
if dt.tzinfo is None:
    dt = dt.replace(tzinfo=datetime.timezone.utc)
now = datetime.datetime.now(datetime.timezone.utc)
print(int(max(0, (now - dt).total_seconds())))
PY
)"
  if [ -n "${age_sec:-}" ] && [ "${age_sec:-0}" -ge 0 ] && [ "${age_sec:-0}" -lt "${PM_AUTO_TRADE_RUNTIME_LOCK_STALE_SEC}" ]; then
    AUTO_TRADE_ACTIVE=1
  fi
  # Stale lock (UI crashed mid-run); clear it and proceed.
  if [ "${AUTO_TRADE_ACTIVE}" != "1" ]; then
    rm -rf "$AUTO_LOCK_DIR" 2>/dev/null || true
  fi
fi
RUN_DATE_ET="$(TZ=America/New_York date '+%Y-%m-%d')"
PM_MODEL_SCAN_FACT_STALE_BYPASS_SEC="$(normalize_positive_int_max "${PM_MODEL_SCAN_FACT_STALE_BYPASS_SEC:-900}" 900 3600)"
MODEL_FACT_AGE_SEC="$(weather_latest_fact_age_sec "$ROOT/data/archive.db" "$RUN_DATE_ET" | head -n 1)"
if ! [[ "${MODEL_FACT_AGE_SEC:-}" =~ ^[0-9]+$ ]]; then
  MODEL_FACT_AGE_SEC=999999
fi
MODEL_WEATHER_LIKELIHOOD_EVENT_DATE="${PM_MODEL_SCAN_WEATHER_LIKELIHOOD_EVENT_DATE:-$(TZ=Asia/Shanghai date '+%Y-%m-%d')}"
model_weather_stale_explicit=0
if [ -n "${PM_MODEL_SCAN_WEATHER_LIKELIHOOD_STALE_BYPASS_SEC+x}" ]; then
  model_weather_stale_explicit=1
fi
PM_MODEL_SCAN_WEATHER_LIKELIHOOD_STALE_BYPASS_SEC="$(normalize_positive_int_max "${PM_MODEL_SCAN_WEATHER_LIKELIHOOD_STALE_BYPASS_SEC:-${PM_MARKET_REFRESH_EVENT_RUN_MAX_WEATHER_AGE_SEC:-900}}" 900 3600)"
if [ "${PM_MODEL_SCAN_DYNAMIC_WEATHER_LIKELIHOOD_STALE_SEC:-1}" = "1" ] && [ "${model_weather_stale_explicit}" != "1" ]; then
  dynamic_weather_stale_sec="$(_temperature_likelihood_dynamic_stale_sec "$MODEL_WEATHER_LIKELIHOOD_EVENT_DATE" "$PM_MODEL_SCAN_WEATHER_LIKELIHOOD_STALE_BYPASS_SEC" | head -n 1)"
  if [[ "${dynamic_weather_stale_sec:-}" =~ ^[0-9]+$ ]] \
    && [ "${dynamic_weather_stale_sec}" -gt 0 ] \
    && [ "${dynamic_weather_stale_sec}" -lt "${PM_MODEL_SCAN_WEATHER_LIKELIHOOD_STALE_BYPASS_SEC}" ]; then
    PM_MODEL_SCAN_WEATHER_LIKELIHOOD_STALE_BYPASS_SEC="${dynamic_weather_stale_sec}"
  fi
fi
_model_scan_refresh_weather_likelihood_stale_state

_model_scan_live_ssot_critical_reason() {
  weather_refresh_live_ssot_critical_reason "model_scan"
}
MODEL_FACT_STALE_BYPASS=0
if [ "${MODEL_FACT_AGE_SEC}" -ge "${PM_MODEL_SCAN_FACT_STALE_BYPASS_SEC}" ]; then
  MODEL_FACT_STALE_BYPASS=1
fi
: "${PM_MODEL_SCAN_BYPASS_ON_WEATHER_LIKELIHOOD_STALE:=1}"
if [ "${MODEL_WEATHER_LIKELIHOOD_STALE_BYPASS}" = "1" ] && [ "${PM_MODEL_SCAN_BYPASS_ON_WEATHER_LIKELIHOOD_STALE}" = "1" ]; then
  MODEL_FACT_STALE_BYPASS=1
fi

_weather_discovery_signal_active() {
  local signal_path="${PM_WEATHER_DISCOVERY_SIGNAL_PATH:-$ROOT/data/weather_discovery_new_market_signal.json}"
  local max_age="${PM_WEATHER_DISCOVERY_SIGNAL_MAX_AGE_SEC:-300}"
  if [ ! -f "$signal_path" ]; then
    echo "0"
    return 0
  fi
  "$PY" - "$signal_path" "$max_age" <<'PY' 2>/dev/null || echo "0"
import json
import os
import sys
import time

path = sys.argv[1]
try:
    max_age = float(sys.argv[2])
except Exception:
    max_age = 300.0
try:
    age = time.time() - os.path.getmtime(path)
    payload = json.load(open(path, "r", encoding="utf-8"))
except Exception:
    print("0")
    raise SystemExit(0)
if age < 0 or age > max_age:
    print("0")
    raise SystemExit(0)
if payload.get("new_slugs"):
    print("1")
else:
    print("0")
PY
}

WEATHER_DISCOVERY_SIGNAL_ACTIVE="$(_weather_discovery_signal_active | tail -n 1)"
if [ "${WEATHER_DISCOVERY_SIGNAL_ACTIVE:-0}" != "1" ]; then
  WEATHER_DISCOVERY_SIGNAL_ACTIVE=0
fi

_model_scan_cycle_schedule_state() {
  "$PY" - <<'PY' 2>/dev/null || true
import os
from datetime import datetime, timedelta, timezone

def _env_int(name: str, default: int) -> int:
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        return int(float(raw))
    except Exception:
        return default

def _parse_hours(raw: str):
    out = []
    for part in str(raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            hour = int(part)
        except Exception:
            continue
        if 0 <= hour <= 23:
            out.append(hour)
    return sorted(set(out))

defaults = {
    "gfs_seamless": [0, 6, 12, 18],
    "icon_seamless": [0, 6, 12, 18],
    "ecmwf_ifs025": [0, 6, 12, 18],
}
models = [
    m.strip()
    for m in str(
        os.environ.get(
            "PM_SCAN_ENSEMBLE_MODELS",
            "gfs_seamless,icon_seamless,ecmwf_ifs025,nws_forecast",
        )
        or ""
    ).split(",")
    if m.strip()
]
delay_hours = _env_int("PM_ENSEMBLE_CYCLE_DELAY_HOURS", 2)
pre_min = max(0, _env_int("PM_MODEL_SCAN_HOT_PRE_WINDOW_MINUTES", 30))
post_min = max(1, _env_int("PM_MODEL_SCAN_HOT_POST_WINDOW_MINUTES", _env_int("PM_MODEL_WINDOW_MINUTES", 180)))
now = datetime.now(timezone.utc)
nearest_hot = None
nearest_future = None
nearest_past = None
hot_models = []

for model in models:
    if model == "nws_forecast":
        continue
    raw = os.environ.get(f"PM_MODEL_CYCLE_HOURS_{model.upper()}") or os.environ.get("PM_ENSEMBLE_CYCLE_HOURS") or ""
    hours = _parse_hours(raw) or defaults.get(model) or []
    if not hours:
        continue
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for day_offset in range(-1, 2):
        base = today0 + timedelta(days=day_offset)
        for hour in hours:
            release_at = base + timedelta(hours=hour + delay_hours)
            start_at = release_at - timedelta(minutes=pre_min)
            end_at = release_at + timedelta(minutes=post_min)
            if start_at <= now <= end_at:
                hot_models.append(model)
                if nearest_hot is None or abs((now - release_at).total_seconds()) < abs((now - nearest_hot).total_seconds()):
                    nearest_hot = release_at
            if release_at <= now:
                if nearest_past is None or release_at > nearest_past:
                    nearest_past = release_at
            else:
                if nearest_future is None or release_at < nearest_future:
                    nearest_future = release_at

if nearest_hot is not None:
    delta_min = int(round((now - nearest_hot).total_seconds() / 60.0))
    print(
        "hot|{}|{}|{}|{}|{}".format(
            nearest_hot.isoformat(timespec="seconds"),
            delta_min,
            pre_min,
            post_min,
            ",".join(sorted(set(hot_models))),
        )
    )
elif nearest_future is not None:
    delta_min = int(round((nearest_future - now).total_seconds() / 60.0))
    print(
        "background|{}|{}|{}|{}|{}".format(
            nearest_future.isoformat(timespec="seconds"),
            delta_min,
            pre_min,
            post_min,
            "next",
        )
    )
elif nearest_past is not None:
    delta_min = int(round((now - nearest_past).total_seconds() / 60.0))
    print(
        "background|{}|{}|{}|{}|{}".format(
            nearest_past.isoformat(timespec="seconds"),
            delta_min,
            pre_min,
            post_min,
            "past",
        )
    )
else:
    print(f"background|none|0|{pre_min}|{post_min}|none")
PY
}

: "${PM_MODEL_SCAN_SCHEDULE_ENABLED:=1}"
: "${PM_MODEL_SCAN_SCHEDULE_STAMP:=$ROOT/data/model_scan.schedule_ts}"
PM_MODEL_SCAN_HOT_INTERVAL_MINUTES="$(normalize_positive_int_max "${PM_MODEL_SCAN_HOT_INTERVAL_MINUTES:-10}" 10 120)"
PM_MODEL_SCAN_BACKGROUND_INTERVAL_MINUTES="$(normalize_positive_int_max "${PM_MODEL_SCAN_BACKGROUND_INTERVAL_MINUTES:-180}" 180 1440)"
MODEL_SCAN_SCHEDULE_STATE="disabled"
MODEL_SCAN_SCHEDULE_REF="none"
MODEL_SCAN_SCHEDULE_DELTA_MIN="0"
MODEL_SCAN_SCHEDULE_MODELS="none"
MODEL_SCAN_SCHEDULE_INTERVAL_MIN="${PM_MODEL_SCAN_BACKGROUND_INTERVAL_MINUTES}"
if [ "${PM_MODEL_SCAN_SCHEDULE_ENABLED}" = "1" ] && [ "${PM_MODEL_SCAN_FORCE_RUN:-0}" != "1" ]; then
  schedule_raw="$(_model_scan_cycle_schedule_state)"
  IFS='|' read -r MODEL_SCAN_SCHEDULE_STATE MODEL_SCAN_SCHEDULE_REF MODEL_SCAN_SCHEDULE_DELTA_MIN _schedule_pre_min _schedule_post_min MODEL_SCAN_SCHEDULE_MODELS <<EOF
${schedule_raw:-background|none|0|0|0|none}
EOF
  if [ "${MODEL_SCAN_SCHEDULE_STATE:-background}" = "hot" ]; then
    MODEL_SCAN_SCHEDULE_INTERVAL_MIN="${PM_MODEL_SCAN_HOT_INTERVAL_MINUTES}"
    export PM_SCAN_MODEL_REFRESH_INTERVAL_MINUTES="${PM_SCAN_MODEL_REFRESH_INTERVAL_MINUTES:-${PM_MODEL_SCAN_HOT_INTERVAL_MINUTES}}"
  else
    MODEL_SCAN_SCHEDULE_INTERVAL_MIN="${PM_MODEL_SCAN_BACKGROUND_INTERVAL_MINUTES}"
    export PM_SCAN_MODEL_REFRESH_INTERVAL_MINUTES="${PM_SCAN_MODEL_REFRESH_INTERVAL_MINUTES:-${PM_MODEL_SCAN_BACKGROUND_INTERVAL_MINUTES}}"
    export PM_MODEL_BACKGROUND_REFRESH_MINUTES="${PM_MODEL_BACKGROUND_REFRESH_MINUTES:-${PM_MODEL_SCAN_BACKGROUND_INTERVAL_MINUTES}}"
  fi
  schedule_now_ts="$(date +%s)"
  schedule_last_ts=0
  if [ -f "$PM_MODEL_SCAN_SCHEDULE_STAMP" ]; then
    schedule_last_ts="$(cat "$PM_MODEL_SCAN_SCHEDULE_STAMP" 2>/dev/null || echo 0)"
  elif [ -f "$ROOT/data/model_scan.last_start" ]; then
    schedule_last_ts="$("$PY" - "$ROOT/data/model_scan.last_start" <<'PY' 2>/dev/null || echo 0
import sys
from datetime import datetime

path = sys.argv[1]
try:
    text = open(path, "r", encoding="utf-8").read().strip()
    dt = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    print(int(dt.timestamp()))
except Exception:
    print(0)
PY
)"
  elif [ -f "$ROOT/data/model_scan.model_refresh_ts" ]; then
    schedule_last_ts="$(cat "$ROOT/data/model_scan.model_refresh_ts" 2>/dev/null || echo 0)"
  fi
  if ! [[ "${schedule_last_ts}" =~ ^[0-9]+$ ]] || [ "${schedule_last_ts}" -gt "${schedule_now_ts}" ]; then
    schedule_last_ts=0
  fi
  schedule_age_sec=$((schedule_now_ts - schedule_last_ts))
  schedule_due_sec=$((MODEL_SCAN_SCHEDULE_INTERVAL_MIN * 60))
  if [ "${schedule_last_ts}" -gt 0 ] && [ "${schedule_age_sec}" -lt "${schedule_due_sec}" ]; then
    fact_stale_schedule_bypass=0
    if [ "${MODEL_FACT_STALE_BYPASS:-0}" = "1" ] && [ "${PM_MODEL_SCAN_BYPASS_SCHEDULE_ON_FACT_STALE:-1}" = "1" ]; then
      fact_stale_schedule_bypass=1
    fi
    if [ "${WEATHER_DISCOVERY_SIGNAL_ACTIVE}" = "1" ] && [ "${PM_MODEL_SCAN_BYPASS_SCHEDULE_ON_NEW_MARKET:-1}" = "1" ]; then
      echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] new market signal, bypass schedule state=${MODEL_SCAN_SCHEDULE_STATE} age_sec=${schedule_age_sec} due_sec=${schedule_due_sec}" >> "$ROOT/data/model_scan.log"
    elif [ "${fact_stale_schedule_bypass}" = "1" ]; then
      echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] freshness stale, bypass schedule state=${MODEL_SCAN_SCHEDULE_STATE} ref=${MODEL_SCAN_SCHEDULE_REF} delta_min=${MODEL_SCAN_SCHEDULE_DELTA_MIN} models=${MODEL_SCAN_SCHEDULE_MODELS} age_sec=${schedule_age_sec} due_sec=${schedule_due_sec} fact_age_sec=${MODEL_FACT_AGE_SEC} stale_sec=${PM_MODEL_SCAN_FACT_STALE_BYPASS_SEC} weather_age_sec=${MODEL_WEATHER_LIKELIHOOD_AGE_SEC} weather_stale_sec=${PM_MODEL_SCAN_WEATHER_LIKELIHOOD_STALE_BYPASS_SEC}" >> "$ROOT/data/model_scan.log"
    else
      echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] schedule skip state=${MODEL_SCAN_SCHEDULE_STATE} ref=${MODEL_SCAN_SCHEDULE_REF} delta_min=${MODEL_SCAN_SCHEDULE_DELTA_MIN} models=${MODEL_SCAN_SCHEDULE_MODELS} age_sec=${schedule_age_sec} due_sec=${schedule_due_sec} hot_interval=${PM_MODEL_SCAN_HOT_INTERVAL_MINUTES}m background_interval=${PM_MODEL_SCAN_BACKGROUND_INTERVAL_MINUTES}m fact_age_sec=${MODEL_FACT_AGE_SEC} weather_age_sec=${MODEL_WEATHER_LIKELIHOOD_AGE_SEC} weather_stale=${MODEL_WEATHER_LIKELIHOOD_STALE_BYPASS}" >> "$ROOT/data/model_scan.log"
      exit 0
    fi
  fi
fi

if [ "${MODEL_FACT_STALE_BYPASS}" = "1" ]; then
  PM_MODEL_SCAN_STALE_REFRESH_INTERVAL_MINUTES="$(normalize_positive_int_max "${PM_MODEL_SCAN_STALE_REFRESH_INTERVAL_MINUTES:-5}" 5 60)"
  _cap_model_refresh_interval "$PM_MODEL_SCAN_STALE_REFRESH_INTERVAL_MINUTES" "freshness_stale"
fi
if [ "${MODEL_WEATHER_LIKELIHOOD_STALE_BYPASS}" = "1" ]; then
  PM_MODEL_SCAN_WEATHER_STALE_MAX_EVENTS_PER_RUN="$(normalize_positive_int_max "${PM_MODEL_SCAN_WEATHER_STALE_MAX_EVENTS_PER_RUN:-1}" 1 2)"
  PM_MODEL_SCAN_WEATHER_STALE_DEFAULT_DAYS_AHEAD="$(_model_scan_event_days_ahead_from_run_date "$RUN_DATE_ET" "$MODEL_WEATHER_LIKELIHOOD_EVENT_DATE" | head -n 1)"
  if ! [[ "${PM_MODEL_SCAN_WEATHER_STALE_DEFAULT_DAYS_AHEAD:-}" =~ ^[0-9]+$ ]]; then
    PM_MODEL_SCAN_WEATHER_STALE_DEFAULT_DAYS_AHEAD=0
  fi
  PM_MODEL_SCAN_WEATHER_STALE_DAYS_AHEAD="$(normalize_int_range "${PM_MODEL_SCAN_WEATHER_STALE_DAYS_AHEAD:-${PM_MODEL_SCAN_WEATHER_STALE_DEFAULT_DAYS_AHEAD}}" "${PM_MODEL_SCAN_WEATHER_STALE_DEFAULT_DAYS_AHEAD}" 0 1)"
  PM_MODEL_SCAN_WEATHER_STALE_NO_UPDATE_MODELS="${PM_MODEL_SCAN_WEATHER_STALE_NO_UPDATE_MODELS:-1}"
  PM_MODEL_SCAN_WEATHER_STALE_PRIORITY_ENABLED="${PM_MODEL_SCAN_WEATHER_STALE_PRIORITY_ENABLED:-0}"
  export PM_MODEL_SCAN_MAX_EVENTS_PER_RUN="${PM_MODEL_SCAN_WEATHER_STALE_MAX_EVENTS_PER_RUN}"
  export PM_MODEL_SCAN_MAX_MATCHED_PER_RUN="${PM_MODEL_SCAN_WEATHER_STALE_MAX_EVENTS_PER_RUN}"
  export PM_SCAN_FAST_DAYS_AHEAD="${PM_MODEL_SCAN_WEATHER_STALE_DAYS_AHEAD}"
  export PM_SCAN_FULL_DAYS_AHEAD="${PM_MODEL_SCAN_WEATHER_STALE_DAYS_AHEAD}"
  export PM_SCAN_FULL_INTERVAL_MINUTES=0
  export PM_SCAN_PRIORITY_ENABLED="${PM_MODEL_SCAN_WEATHER_STALE_PRIORITY_ENABLED}"
  export PM_SCAN_ROTATE_EVENTS=0
  if [[ "${PM_MODEL_SCAN_WEATHER_STALE_NO_UPDATE_MODELS}" =~ ^(1|true|yes|on)$ ]]; then
    export PM_SCAN_NO_UPDATE_MODELS=1
  fi
  export PM_SCAN_PRIORITY_INCLUDE_LIKELIHOOD_STALE="${PM_SCAN_PRIORITY_INCLUDE_LIKELIHOOD_STALE:-1}"
  export PM_SCAN_PRIORITY_LIKELIHOOD_STALE_TOPN="${PM_SCAN_PRIORITY_LIKELIHOOD_STALE_TOPN:-${PM_MODEL_SCAN_WEATHER_STALE_MAX_EVENTS_PER_RUN}}"
  if [ -z "${PM_MARKET_SCAN_COMPOSE_MARKET_BOOTSTRAP+x}" ]; then
    export PM_MARKET_SCAN_COMPOSE_MARKET_BOOTSTRAP=1
  fi
  echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] weather likelihood stale, run current-event cached-observation refresh max_events=${PM_MODEL_SCAN_MAX_EVENTS_PER_RUN} days_ahead=${PM_SCAN_FAST_DAYS_AHEAD} priority=${PM_SCAN_PRIORITY_ENABLED} no_update_models=${PM_SCAN_NO_UPDATE_MODELS:-0} bootstrap=${PM_MARKET_SCAN_COMPOSE_MARKET_BOOTSTRAP:-0}" >> "$ROOT/data/model_scan.log"
fi

AUTO_WORKER_PROCESS_ACTIVE=0
if _auto_trade_worker_active; then
  AUTO_WORKER_PROCESS_ACTIVE=1
fi
AUTO_ENTRY_PROCESS_ACTIVE=0
if _auto_trade_entry_active; then
  AUTO_ENTRY_PROCESS_ACTIVE=1
fi
queued_entry_jobs="$(_queued_entry_job_count)"
queued_entry_yield_min="$(normalize_positive_int_max "${PM_MODEL_SCAN_YIELD_TO_QUEUED_AUTO_TRADE_JOBS_MIN:-1}" 1 20)"
if ! [[ "${queued_entry_jobs}" =~ ^[0-9]+$ ]]; then
  queued_entry_jobs=0
fi

AUTO_TRADE_CONTEXT="${AUTO_TRADE_ACTIVE}"
if [ "${AUTO_WORKER_PROCESS_ACTIVE}" = "1" ] || [ "${AUTO_ENTRY_PROCESS_ACTIVE}" = "1" ] || [ "${queued_entry_jobs}" -gt 0 ]; then
  AUTO_TRADE_CONTEXT=1
fi
GUARDED_SCAN_CONTEXT="${AUTO_TRADE_CONTEXT}"
if [ "${MODEL_FACT_STALE_BYPASS}" = "1" ]; then
  GUARDED_SCAN_CONTEXT=1
fi
if [ "${WEATHER_DISCOVERY_SIGNAL_ACTIVE}" = "1" ]; then
  GUARDED_SCAN_CONTEXT=1
  if [ "${PM_MODEL_SCAN_BYPASS_YIELD_ON_NEW_MARKET:-1}" = "1" ]; then
    export PM_MODEL_SCAN_YIELD_TO_AUTO_TRADE_ACTIVE=0
    echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] new market signal, bypass auto-trade yield for first-pass modeling" >> "$ROOT/data/model_scan.log"
  fi
fi
: "${PM_MODEL_SCAN_FACT_STALE_BYPASS_DURING_AUTO_TRADE:=1}"
if [[ "${PM_MODEL_SCAN_FACT_STALE_BYPASS_DURING_AUTO_TRADE}" =~ ^(1|true|yes|on)$ ]]; then
  MODEL_FACT_STALE_BYPASS_DURING_AUTO_TRADE=1
else
  MODEL_FACT_STALE_BYPASS_DURING_AUTO_TRADE=0
fi

if [ "${PM_MODEL_SCAN_YIELD_TO_AUTO_TRADE_ACTIVE:-1}" = "1" ]; then
  if [ "${AUTO_TRADE_ACTIVE}" = "1" ]; then
    if [ "${MODEL_FACT_STALE_BYPASS}" = "1" ] && [ "${MODEL_FACT_STALE_BYPASS_DURING_AUTO_TRADE}" = "1" ]; then
      echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] auto_trade active but freshness stale, enter guarded refresh fact_age_sec=${MODEL_FACT_AGE_SEC} stale_sec=${PM_MODEL_SCAN_FACT_STALE_BYPASS_SEC} weather_age_sec=${MODEL_WEATHER_LIKELIHOOD_AGE_SEC} weather_stale=${MODEL_WEATHER_LIKELIHOOD_STALE_BYPASS}" >> "$ROOT/data/model_scan.log"
    else
      _model_scan_materialize_stale_facts "auto_trade_runtime"
      echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] yield to active auto_trade worker fact_age_sec=${MODEL_FACT_AGE_SEC}" >> "$ROOT/data/model_scan.log"
      exit 0
    fi
  fi
  if [ "${AUTO_WORKER_PROCESS_ACTIVE}" = "1" ]; then
    if [ "${MODEL_FACT_STALE_BYPASS}" = "1" ] && [ "${MODEL_FACT_STALE_BYPASS_DURING_AUTO_TRADE}" = "1" ]; then
      echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] auto_trade worker active but freshness stale, enter guarded refresh fact_age_sec=${MODEL_FACT_AGE_SEC} stale_sec=${PM_MODEL_SCAN_FACT_STALE_BYPASS_SEC} weather_age_sec=${MODEL_WEATHER_LIKELIHOOD_AGE_SEC} weather_stale=${MODEL_WEATHER_LIKELIHOOD_STALE_BYPASS}" >> "$ROOT/data/model_scan.log"
    else
      _model_scan_materialize_stale_facts "auto_trade_worker_process"
      echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] yield to active auto_trade worker process fact_age_sec=${MODEL_FACT_AGE_SEC}" >> "$ROOT/data/model_scan.log"
      exit 0
    fi
  fi
  if [ "${AUTO_ENTRY_PROCESS_ACTIVE}" = "1" ]; then
    if [ "${MODEL_FACT_STALE_BYPASS}" = "1" ] && [ "${MODEL_FACT_STALE_BYPASS_DURING_AUTO_TRADE}" = "1" ]; then
      echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] auto_trade entry enqueue active but freshness stale, enter guarded refresh fact_age_sec=${MODEL_FACT_AGE_SEC} stale_sec=${PM_MODEL_SCAN_FACT_STALE_BYPASS_SEC} weather_age_sec=${MODEL_WEATHER_LIKELIHOOD_AGE_SEC} weather_stale=${MODEL_WEATHER_LIKELIHOOD_STALE_BYPASS}" >> "$ROOT/data/model_scan.log"
    else
      _model_scan_materialize_stale_facts "auto_trade_entry_enqueue"
      echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] yield to active auto_trade entry enqueue fact_age_sec=${MODEL_FACT_AGE_SEC}" >> "$ROOT/data/model_scan.log"
      exit 0
    fi
  fi
  if [ "${queued_entry_jobs}" -ge "${queued_entry_yield_min}" ]; then
    PM_MODEL_SCAN_FACT_STALE_MAX_QUEUED_AUTO_JOBS="$(normalize_int_range "${PM_MODEL_SCAN_FACT_STALE_MAX_QUEUED_AUTO_JOBS:-0}" 0 0 20)"
    if [ "${MODEL_FACT_STALE_BYPASS}" = "1" ] \
      && [ "${MODEL_FACT_STALE_BYPASS_DURING_AUTO_TRADE}" = "1" ] \
      && [ "${PM_MODEL_SCAN_FACT_STALE_MAX_QUEUED_AUTO_JOBS}" -gt 0 ] \
      && [ "${queued_entry_jobs}" -le "${PM_MODEL_SCAN_FACT_STALE_MAX_QUEUED_AUTO_JOBS}" ]; then
      echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] queued auto_trade jobs present but freshness stale, enter guarded refresh count=${queued_entry_jobs} max_bypass=${PM_MODEL_SCAN_FACT_STALE_MAX_QUEUED_AUTO_JOBS} fact_age_sec=${MODEL_FACT_AGE_SEC} weather_age_sec=${MODEL_WEATHER_LIKELIHOOD_AGE_SEC} weather_stale=${MODEL_WEATHER_LIKELIHOOD_STALE_BYPASS}" >> "$ROOT/data/model_scan.log"
    else
      _model_scan_materialize_stale_facts "queued_auto_trade_jobs"
      echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] yield to queued auto_trade jobs count=${queued_entry_jobs} min=${queued_entry_yield_min} fact_age_sec=${MODEL_FACT_AGE_SEC}" >> "$ROOT/data/model_scan.log"
      exit 0
    fi
  fi
fi
if [ "${GUARDED_SCAN_CONTEXT}" = "1" ]; then
  # Priority scan plan is built from facts (SSOT): entry_ok slugs come from auto_trade_ssot.
  export PM_SCAN_PRIORITY_ENABLED=1
  export PM_SCAN_PRIORITY_INCLUDE_ENTRY_OK=1
  export PM_SCAN_PRIORITY_INCLUDE_Q_MISSING=1
  export PM_SCAN_PRIORITY_INCLUDE_NEW_MARKETS="${PM_SCAN_PRIORITY_INCLUDE_NEW_MARKETS:-1}"
  export PM_SCAN_PRIORITY_NEW_MARKET_TOPN="${PM_SCAN_PRIORITY_NEW_MARKET_TOPN:-8}"
  export PM_SCAN_PRIORITY_NEW_MARKET_MAX_AGE_MIN="${PM_SCAN_PRIORITY_NEW_MARKET_MAX_AGE_MIN:-240}"
  export PM_SCAN_PRIORITY_INCLUDE_FUTURE_MODEL_MISSING="${PM_SCAN_PRIORITY_INCLUDE_FUTURE_MODEL_MISSING:-1}"
  export PM_SCAN_PRIORITY_FUTURE_MODEL_MISSING_TOPN="${PM_SCAN_PRIORITY_FUTURE_MODEL_MISSING_TOPN:-8}"
  export PM_SCAN_PRIORITY_CANDIDATES_PER_RUN="${PM_SCAN_PRIORITY_CANDIDATES_PER_RUN:-2}"
  export PM_SCAN_PRIORITY_Q_MISSING_TOPN="${PM_SCAN_PRIORITY_Q_MISSING_TOPN:-12}"
  export PM_SCAN_PRIORITY_INCLUDE_RECOMMENDED_PILOT_GAPS="${PM_SCAN_PRIORITY_INCLUDE_RECOMMENDED_PILOT_GAPS:-1}"
  export PM_SCAN_PRIORITY_RECOMMENDED_PILOT_GAP_TOPN="${PM_SCAN_PRIORITY_RECOMMENDED_PILOT_GAP_TOPN:-4}"
  export PM_SCAN_PRIORITY_RESERVE_NONFIXED_SLOT="${PM_SCAN_PRIORITY_RESERVE_NONFIXED_SLOT:-1}"
  export PM_SCAN_PRIORITY_RESERVE_NONFIXED_SLOTS="${PM_SCAN_PRIORITY_RESERVE_NONFIXED_SLOTS:-1}"
  PM_MODEL_SCAN_AUTO_TRADE_OTHERS_PER_RUN="$(normalize_int_range "${PM_MODEL_SCAN_AUTO_TRADE_OTHERS_PER_RUN:-0}" 0 0 0)"
  export PM_SCAN_PRIORITY_OTHERS_PER_RUN="${PM_MODEL_SCAN_AUTO_TRADE_OTHERS_PER_RUN}"
  export PM_SCAN_PRIORITY_INCLUDE_FACT_TAIL="${PM_SCAN_PRIORITY_INCLUDE_FACT_TAIL:-1}"
  export PM_SCAN_PRIORITY_FACT_TAIL_TOPN="${PM_SCAN_PRIORITY_FACT_TAIL_TOPN:-2}"
  export PM_SCAN_PRIORITY_FACT_TAIL_MIN_AGE_SEC="${PM_SCAN_PRIORITY_FACT_TAIL_MIN_AGE_SEC:-2700}"
  if [ "${MODEL_FACT_STALE_BYPASS}" = "1" ]; then
    if [ "${MODEL_WEATHER_LIKELIHOOD_STALE_BYPASS}" = "1" ]; then
      PM_MODEL_SCAN_FACT_STALE_MAX_EVENTS_PER_RUN="$(normalize_positive_int_max "${PM_MODEL_SCAN_WEATHER_STALE_MAX_EVENTS_PER_RUN:-1}" 1 2)"
    else
      PM_MODEL_SCAN_FACT_STALE_MAX_EVENTS_PER_RUN="$(normalize_positive_int_max "${PM_MODEL_SCAN_FACT_STALE_MAX_EVENTS_PER_RUN:-2}" 2 2)"
    fi
    export PM_MODEL_SCAN_MAX_EVENTS_PER_RUN="${PM_MODEL_SCAN_FACT_STALE_MAX_EVENTS_PER_RUN}"
    export PM_MODEL_SCAN_MAX_MATCHED_PER_RUN="${PM_MODEL_SCAN_FACT_STALE_MAX_EVENTS_PER_RUN}"
    export PM_SCAN_PRIORITY_MARKET_TICK_STALE_TOPN="${PM_SCAN_PRIORITY_MARKET_TICK_STALE_TOPN:-0}"
    if [ "${MODEL_WEATHER_LIKELIHOOD_STALE_BYPASS}" = "1" ]; then
      export PM_SCAN_PRIORITY_FACT_TAIL_TOPN="${PM_SCAN_PRIORITY_FACT_TAIL_TOPN:-1}"
    else
      export PM_SCAN_PRIORITY_FACT_TAIL_TOPN="4"
    fi
    export PM_SCAN_PRIORITY_FACT_TAIL_MIN_AGE_SEC="${PM_MODEL_SCAN_FACT_STALE_BYPASS_SEC}"
  fi
  if [ "${WEATHER_DISCOVERY_SIGNAL_ACTIVE}" = "1" ]; then
    export PM_SCAN_PRIORITY_NEW_MARKET_TOPN="${PM_MODEL_SCAN_NEW_MARKET_TOPN:-8}"
    PM_MODEL_SCAN_NEW_MARKET_MAX_EVENTS_PER_RUN="$(normalize_positive_int_max "${PM_MODEL_SCAN_NEW_MARKET_MAX_EVENTS_PER_RUN:-2}" 2 3)"
    export PM_MODEL_SCAN_MAX_EVENTS_PER_RUN="${PM_MODEL_SCAN_NEW_MARKET_MAX_EVENTS_PER_RUN}"
    export PM_MODEL_SCAN_MAX_MATCHED_PER_RUN="${PM_MODEL_SCAN_NEW_MARKET_MAX_EVENTS_PER_RUN}"
  fi
  # Keep runs short to avoid the 900s watchdog.
  export PM_MODEL_SCAN_MAX_EVENTS_PER_RUN="$(normalize_positive_int_max "${PM_MODEL_SCAN_MAX_EVENTS_PER_RUN:-2}" 2 2)"
  export PM_MODEL_SCAN_MAX_MATCHED_PER_RUN="$(normalize_positive_int_max "${PM_MODEL_SCAN_MAX_MATCHED_PER_RUN:-${PM_MODEL_SCAN_MAX_EVENTS_PER_RUN}}" "${PM_MODEL_SCAN_MAX_EVENTS_PER_RUN}" "${PM_MODEL_SCAN_MAX_EVENTS_PER_RUN}")"
  # While auto-trade is active, avoid diagnostic CLOB fan-out and repeated
  # timeout retries; otherwise model_scan can monopolize the DB writer window
  # for multiple 420s attempts while market ticks remain stale.
  export PM_SCAN_AUTO_DIAG="${PM_SCAN_AUTO_DIAG:-${PM_MODEL_SCAN_AUTO_TRADE_AUTO_DIAG:-0}}"
  export PM_SCAN_RETRY_MAX="$(normalize_int_range "${PM_SCAN_RETRY_MAX:-${PM_MODEL_SCAN_AUTO_TRADE_RETRY_MAX:-0}}" 0 0 0)"
  export PM_SCAN_MODEL_SCAN_TIMEOUT_SEC="$(normalize_positive_int_max "${PM_SCAN_MODEL_SCAN_TIMEOUT_SEC:-${PM_MODEL_SCAN_AUTO_TRADE_TIMEOUT_SEC:-360}}" 360 900)"
  # Freshness-stale bypass: while auto-trade is active, allow a tiny "others" fan-out so long-tail facts can recover.
  : "${PM_MODEL_SCAN_AUTO_TRADE_STALE_BYPASS_SEC:=${PM_MODEL_SCAN_FACT_STALE_BYPASS_SEC}}"
  PM_MODEL_SCAN_AUTO_TRADE_STALE_BYPASS_OTHERS_PER_RUN="$(normalize_int_range "${PM_MODEL_SCAN_AUTO_TRADE_STALE_BYPASS_OTHERS_PER_RUN:-1}" 1 0 1)"
  stale_tail_count="$("$PY" - "$ROOT/data/archive.db" "${PM_MODEL_SCAN_AUTO_TRADE_STALE_BYPASS_SEC}" <<'PY' 2>/dev/null || true
import sqlite3
import sys

db_path = sys.argv[1]
try:
    stale_sec = int(float(sys.argv[2]))
except Exception:
    stale_sec = 2700
if stale_sec < 0:
    stale_sec = 0

try:
    conn = sqlite3.connect(db_path, timeout=1.0)
    conn.row_factory = sqlite3.Row
except Exception:
    raise SystemExit(0)

try:
    row = conn.execute("SELECT MAX(run_date) AS run_date FROM event_fact_history").fetchone()
    run_date = str((row["run_date"] if row else "") or "").strip()
    if not run_date:
        print(0)
        raise SystemExit(0)
    row2 = conn.execute(
        """
        WITH latest AS (
          SELECT slug, event_date, MAX(fact_as_of) AS ts
          FROM event_fact_history
          WHERE run_date = ?
            AND event_date >= ?
          GROUP BY slug, event_date
        )
        SELECT COUNT(*) AS cnt
        FROM latest
        WHERE ts IS NOT NULL
          AND (CAST(strftime('%s','now') AS INTEGER) - CAST(strftime('%s', ts) AS INTEGER)) >= ?
        """,
        (run_date, run_date, stale_sec),
    ).fetchone()
    cnt = int((row2["cnt"] if row2 else 0) or 0)
    print(cnt)
finally:
    conn.close()
PY
)"
  if [[ "${stale_tail_count:-}" =~ ^[0-9]+$ ]] && [ "${stale_tail_count}" -gt 0 ]; then
    export PM_SCAN_PRIORITY_OTHERS_PER_RUN="${PM_MODEL_SCAN_AUTO_TRADE_STALE_BYPASS_OTHERS_PER_RUN}"
    echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] auto_trade stale-bypass enabled (stale_tail=${stale_tail_count}, others=${PM_SCAN_PRIORITY_OTHERS_PER_RUN})" >> "$ROOT/data/model_scan.log"
  fi
  if [ "${MODEL_WEATHER_LIKELIHOOD_STALE_BYPASS}" = "1" ] && [ "${PM_MODEL_SCAN_WEATHER_STALE_PRIORITY_ENABLED:-0}" != "1" ]; then
    export PM_SCAN_PRIORITY_ENABLED=0
    export PM_SCAN_ROTATE_EVENTS=0
  fi
  echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] guarded context, prioritize Top OK/tails/pilot gaps (auto_trade=${AUTO_TRADE_CONTEXT}, others=${PM_SCAN_PRIORITY_OTHERS_PER_RUN}, max_events=${PM_MODEL_SCAN_MAX_EVENTS_PER_RUN}, tick_stale_topn=${PM_SCAN_PRIORITY_MARKET_TICK_STALE_TOPN:-default}, fact_tail_topn=${PM_SCAN_PRIORITY_FACT_TAIL_TOPN:-0}, pilot_gap_topn=${PM_SCAN_PRIORITY_RECOMMENDED_PILOT_GAP_TOPN:-0}, fact_age_sec=${MODEL_FACT_AGE_SEC}, weather_age_sec=${MODEL_WEATHER_LIKELIHOOD_AGE_SEC}, weather_stale=${MODEL_WEATHER_LIKELIHOOD_STALE_BYPASS}, stale_bypass=${MODEL_FACT_STALE_BYPASS})" >> "$ROOT/data/model_scan.log"
fi

# When auto-trade queue is heavily backlogged, let entry worker clear first.
# Avoid pausing model_scan for a single long-running/rolling job, which can starve q updates.
if [ "${PM_MODEL_SCAN_PAUSE_WHEN_AUTO_JOB_ACTIVE:-1}" = "1" ] && [ -f "$ROOT/data/archive.db" ]; then
  : "${PM_MODEL_SCAN_PAUSE_AUTO_JOB_MIN_ACTIVE:=3}"
  auto_job_active="$("$PY" - "$ROOT/data/archive.db" "$(TZ=America/New_York date '+%Y-%m-%d')" <<'PY' 2>/dev/null || true
import sqlite3
import sys

db_path = sys.argv[1]
run_date = sys.argv[2]
try:
    conn = sqlite3.connect(db_path, timeout=1.0)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT COUNT(*)
        FROM auto_trade_jobs
        WHERE run_date = ?
          AND state IN ('queued','running')
        """,
        (run_date,),
    )
    row = cur.fetchone()
    print(int((row[0] if row else 0) or 0))
finally:
    try:
        conn.close()
    except Exception:
        pass
PY
)"
  if ! [[ "${PM_MODEL_SCAN_PAUSE_AUTO_JOB_MIN_ACTIVE:-}" =~ ^[0-9]+$ ]]; then
    PM_MODEL_SCAN_PAUSE_AUTO_JOB_MIN_ACTIVE=3
  fi
  if [[ "${auto_job_active:-}" =~ ^[0-9]+$ ]] && [ "${auto_job_active}" -ge "${PM_MODEL_SCAN_PAUSE_AUTO_JOB_MIN_ACTIVE}" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] paused for auto_trade queue active_jobs=${auto_job_active} threshold=${PM_MODEL_SCAN_PAUSE_AUTO_JOB_MIN_ACTIVE}" >> "$ROOT/data/model_scan.log"
    exit 0
  fi
fi

# If Open-Meteo 429 cooldown is active, reduce non-priority fan-out to avoid
# re-triggering rate limits before cooldown windows finish.
: "${PM_MODEL_SCAN_OPEN_METEO_COOLDOWN_ACTIVE_THRESHOLD:=1}"
: "${PM_MODEL_SCAN_OPEN_METEO_COOLDOWN_OTHERS_PER_RUN:=0}"
PM_MODEL_SCAN_OPEN_METEO_COOLDOWN_MAX_EVENTS_PER_RUN="$(normalize_positive_int_max "${PM_MODEL_SCAN_OPEN_METEO_COOLDOWN_MAX_EVENTS_PER_RUN:-2}" 2 2)"
: "${PM_MODEL_SCAN_OPEN_METEO_COOLDOWN_CACHE_ONLY:=1}"
MODEL_CACHE_ONLY_ARG=""
open_meteo_active_429="$("$PY" - "$ROOT/data/archive.db" <<'PY' 2>/dev/null || true
import sqlite3
import sys
from datetime import datetime, timezone

db_path = sys.argv[1]
try:
    conn = sqlite3.connect(db_path, timeout=1.0)
    cur = conn.cursor()
    now_iso = datetime.now(timezone.utc).isoformat()
    cur.execute(
        """
        SELECT COUNT(*) FROM model_request_cooldown
        WHERE reason = 'open_meteo_429'
          AND cooldown_until > ?
        """,
        (now_iso,),
    )
    row = cur.fetchone()
    print(int((row[0] if row else 0) or 0))
finally:
    try:
        conn.close()
    except Exception:
        pass
PY
)"
if [[ "${open_meteo_active_429:-}" =~ ^[0-9]+$ ]] && [ "${open_meteo_active_429}" -ge "${PM_MODEL_SCAN_OPEN_METEO_COOLDOWN_ACTIVE_THRESHOLD}" ]; then
  export PM_SCAN_PRIORITY_ENABLED=1
  export PM_SCAN_PRIORITY_OTHERS_PER_RUN="${PM_MODEL_SCAN_OPEN_METEO_COOLDOWN_OTHERS_PER_RUN}"
  if [ "${PM_MODEL_SCAN_OPEN_METEO_COOLDOWN_CACHE_ONLY}" = "1" ]; then
    MODEL_CACHE_ONLY_ARG="--model-cache-only"
  fi
  if [[ "${PM_MODEL_SCAN_MAX_EVENTS_PER_RUN:-0}" =~ ^[0-9]+$ ]] && [ "${PM_MODEL_SCAN_MAX_EVENTS_PER_RUN:-0}" -gt "${PM_MODEL_SCAN_OPEN_METEO_COOLDOWN_MAX_EVENTS_PER_RUN}" ]; then
    export PM_MODEL_SCAN_MAX_EVENTS_PER_RUN="${PM_MODEL_SCAN_OPEN_METEO_COOLDOWN_MAX_EVENTS_PER_RUN}"
  fi
  if [[ "${PM_MODEL_SCAN_MAX_MATCHED_PER_RUN:-0}" =~ ^[0-9]+$ ]] && [ "${PM_MODEL_SCAN_MAX_MATCHED_PER_RUN:-0}" -gt "${PM_MODEL_SCAN_MAX_EVENTS_PER_RUN:-0}" ]; then
    export PM_MODEL_SCAN_MAX_MATCHED_PER_RUN="${PM_MODEL_SCAN_MAX_EVENTS_PER_RUN}"
  fi
  echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] open_meteo cooldown active=${open_meteo_active_429}, throttle fanout (others=${PM_SCAN_PRIORITY_OTHERS_PER_RUN}, max_events=${PM_MODEL_SCAN_MAX_EVENTS_PER_RUN}, cache_only=${PM_MODEL_SCAN_OPEN_METEO_COOLDOWN_CACHE_ONLY})" >> "$ROOT/data/model_scan.log"
fi

live_ssot_reason="$(_model_scan_live_ssot_critical_reason 2>/dev/null || true)"
if [ -n "${live_ssot_reason:-}" ]; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] live_ssot critical, skip model_scan before pre-lane live=${live_ssot_reason}" >> "$ROOT/data/model_scan.log"
  exit 0
fi

if [ "${MODEL_WEATHER_LIKELIHOOD_STALE_BYPASS:-0}" = "1" ] \
  && [ "${PM_MODEL_SCAN_PRECIP_NOWCAST_FACT_REFRESH_ENABLED:-0}" = "1" ]; then
  if _model_scan_run_precip_nowcast_fact_refresh "pre_lane"; then
    precip_nowcast_refresh_rc=0
  else
    precip_nowcast_refresh_rc="$?"
  fi
  if [ "${precip_nowcast_refresh_rc}" -eq 0 ]; then
    _model_scan_refresh_weather_likelihood_stale_state
    if [ "${MODEL_WEATHER_LIKELIHOOD_STALE_BYPASS}" != "1" ] \
      && [ "${MODEL_FACT_AGE_SEC}" -lt "${PM_MODEL_SCAN_FACT_STALE_BYPASS_SEC}" ]; then
      MODEL_FACT_STALE_BYPASS=0
    fi
    echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] precip_nowcast_fact_refresh pre-lane complete weather_age_sec=${MODEL_WEATHER_LIKELIHOOD_AGE_SEC} weather_stale=${MODEL_WEATHER_LIKELIHOOD_STALE_BYPASS} stale_bypass=${MODEL_FACT_STALE_BYPASS}" >> "$ROOT/data/model_scan.log"
  else
    echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] precip_nowcast_fact_refresh pre-lane degraded rc=${precip_nowcast_refresh_rc}; continue regular scan" >> "$ROOT/data/model_scan.log"
  fi
fi

if [ "${MODEL_WEATHER_LIKELIHOOD_STALE_BYPASS:-0}" = "1" ] \
  && [ "${PM_MODEL_SCAN_AWC_FACT_REFRESH_ENABLED:-0}" = "1" ] \
  && [ "${PM_MODEL_SCAN_AWC_FACT_REFRESH_PRE_LANE_ENABLED:-0}" = "1" ]; then
  if _model_scan_run_awc_fact_refresh "pre_lane"; then
    awc_fact_refresh_rc=0
  else
    awc_fact_refresh_rc="$?"
  fi
  if [ "${awc_fact_refresh_rc}" -eq 0 ]; then
    MODEL_SCAN_AWC_FACT_REFRESH_DONE=1
    _model_scan_refresh_weather_likelihood_stale_state
  fi
  if [ "${awc_fact_refresh_rc}" -eq 0 ] && _model_scan_awc_refresh_skip_scan_allowed; then
    if [ "${MODEL_SCAN_SCHEDULE_STATE:-background}" = "hot" ] \
      && [ "${PM_MODEL_SCAN_AWC_FACT_REFRESH_SKIP_HOT_SCHEDULE:-1}" != "1" ]; then
      echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] awc_fact_refresh ok but hot schedule requires heavyweight scan" >> "$ROOT/data/model_scan.log"
    else
      echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] awc_fact_refresh pre-lane satisfied weather stale; skip heavyweight scan" >> "$ROOT/data/model_scan.log"
      exit 0
    fi
  elif [ "${awc_fact_refresh_rc}" -eq 75 ] && _model_scan_awc_refresh_skip_scan_allowed; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] awc_fact_refresh busy in pre-lane; skip heavyweight scan" >> "$ROOT/data/model_scan.log"
    exit 0
  elif [ "${awc_fact_refresh_rc}" -ne 0 ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] awc_fact_refresh pre-lane degraded rc=${awc_fact_refresh_rc}; continue regular scan" >> "$ROOT/data/model_scan.log"
  elif [ "${awc_fact_refresh_rc}" -eq 0 ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] awc_fact_refresh pre-lane complete; continue regular scan weather_age_sec=${MODEL_WEATHER_LIKELIHOOD_AGE_SEC} weather_stale=${MODEL_WEATHER_LIKELIHOOD_STALE_BYPASS}" >> "$ROOT/data/model_scan.log"
  fi
fi

# Avoid concurrent heavy writers: market_refresh and model_scan both write SQLite snapshots.
MARKET_LOCK_DIR="$ROOT/data/market_refresh.lock"
MARKET_PID_FILE="$MARKET_LOCK_DIR/pid"
_market_pid_running() {
  local pid="${1:-}"
  if [ -z "$pid" ]; then
    return 1
  fi
  if ! kill -0 "$pid" 2>/dev/null; then
    return 1
  fi
  local cmdline
  cmdline="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  if [[ "$cmdline" == *"scan_weather_market.py"* && "$cmdline" == *"--scan-weather-today"* && "$cmdline" == *"--market-refresh-only"* ]]; then
    return 0
  fi
  return 1
}
_market_tick_p90_age_sec() {
  local db_path="$ROOT/data/archive.db"
  if [ ! -f "$db_path" ]; then
    return 1
  fi
  "$PY" - "$db_path" <<'PY' 2>/dev/null || true
import sqlite3
import sys
from datetime import datetime, timezone

db_path = sys.argv[1]
try:
    conn = sqlite3.connect(db_path, timeout=1.0)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        WITH latest AS (
          SELECT slug, event_date, MAX(created_at) AS ts
          FROM event_market_ticks
          WHERE event_date IS NOT NULL
            AND event_date != ''
            AND event_date >= date('now', '-3 day')
            AND event_date <= date('now', '+4 day')
          GROUP BY slug, event_date
        )
        SELECT ts FROM latest WHERE ts IS NOT NULL
        """
    ).fetchall()
    conn.close()
except Exception:
    raise SystemExit(0)

if not rows:
    raise SystemExit(0)

now = datetime.now(timezone.utc)
ages = []
for row in rows:
    ts = row["ts"] if row is not None else None
    if not ts:
        continue
    text = str(ts).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except Exception:
        try:
            dt = datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except Exception:
            continue
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    ages.append(int(max(0, (now - dt).total_seconds())))

if not ages:
    raise SystemExit(0)
ages.sort()
idx = int(round(0.90 * (len(ages) - 1)))
idx = max(0, min(idx, len(ages) - 1))
print(ages[idx])
PY
}
market_pid=""
if [ -f "$MARKET_PID_FILE" ]; then
  market_pid="$(cat "$MARKET_PID_FILE" 2>/dev/null || true)"
fi
if weather_refresh_legacy_peer_guards_enabled && { _market_pid_running "$market_pid" || pgrep -f "scan_weather_market.py .*--scan-weather-today.*--market-refresh-only" >/dev/null 2>&1; }; then
  : "${PM_MODEL_SCAN_IGNORE_MARKET_REFRESH_ACTIVE:=0}"
  if [ "${PM_MODEL_SCAN_IGNORE_MARKET_REFRESH_ACTIVE}" = "1" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] market_refresh active ignored (separate model/market lanes)" >> "$ROOT/data/model_scan.log"
  else
  : "${PM_MARKET_REFRESH_MODEL_SCAN_SKIP_MAX_TICK_AGE_SEC:=120}"
  : "${PM_MODEL_SCAN_MARKET_REFRESH_SKIP_MAX_TICK_AGE_SEC:=${PM_MARKET_REFRESH_MODEL_SCAN_SKIP_MAX_TICK_AGE_SEC}}"
  : "${PM_MODEL_SCAN_YIELD_TO_MARKET_REFRESH_WHEN_TICK_STALE:=1}"
  tick_age_p90_sec="$(_market_tick_p90_age_sec)"
  if [[ "${tick_age_p90_sec:-}" =~ ^[0-9]+$ ]] && [[ "${PM_MODEL_SCAN_MARKET_REFRESH_SKIP_MAX_TICK_AGE_SEC}" =~ ^[0-9]+$ ]] && [ "${tick_age_p90_sec}" -lt "${PM_MODEL_SCAN_MARKET_REFRESH_SKIP_MAX_TICK_AGE_SEC}" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] market_refresh running, skip (policy tick_age_p90=${tick_age_p90_sec}s<th=${PM_MODEL_SCAN_MARKET_REFRESH_SKIP_MAX_TICK_AGE_SEC}s)" >> "$ROOT/data/model_scan.log"
    exit 0
  fi
  if [ "${PM_MODEL_SCAN_YIELD_TO_MARKET_REFRESH_WHEN_TICK_STALE}" = "1" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] market_refresh running, yield while ticks stale/unknown (tick_age_p90=${tick_age_p90_sec:-na}s th=${PM_MODEL_SCAN_MARKET_REFRESH_SKIP_MAX_TICK_AGE_SEC}s)" >> "$ROOT/data/model_scan.log"
    exit 0
  fi
  echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] market_refresh running but ticks stale/unknown, bypass skip (tick_age_p90=${tick_age_p90_sec:-na}s th=${PM_MODEL_SCAN_MARKET_REFRESH_SKIP_MAX_TICK_AGE_SEC}s)" >> "$ROOT/data/model_scan.log"
  fi
fi

if pgrep -f "scan_weather_market.py .*--trade-sync-" >/dev/null 2>&1; then
  if [ "${MODEL_FACT_STALE_BYPASS:-0}" = "1" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] trade_sync active but freshness stale, bypass yield fact_age_sec=${MODEL_FACT_AGE_SEC} weather_age_sec=${MODEL_WEATHER_LIKELIHOOD_AGE_SEC} weather_stale=${MODEL_WEATHER_LIKELIHOOD_STALE_BYPASS}" >> "$ROOT/data/model_scan.log"
  else
    echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] yield to active trade_sync fact_age_sec=${MODEL_FACT_AGE_SEC} weather_age_sec=${MODEL_WEATHER_LIKELIHOOD_AGE_SEC} weather_stale=${MODEL_WEATHER_LIKELIHOOD_STALE_BYPASS}" >> "$ROOT/data/model_scan.log"
    exit 0
  fi
fi

# Prevent overlapping scans; clear stale lock if no scan-weather process is running.
LOCK_DIR="$ROOT/data/model_scan.lock"
LOCK_PID_FILE="$LOCK_DIR/pid"
DB_LOCK_DIR="$ROOT/data/db_writer.lock"
DB_LOCK_PID_FILE="$DB_LOCK_DIR/pid"
DB_LOCK_OWNER_FILE="$DB_LOCK_DIR/owner"
_scan_pid_running() {
  local pid="${1:-}"
  if [ -z "$pid" ]; then
    return 1
  fi
  if ! kill -0 "$pid" 2>/dev/null; then
    return 1
  fi
  local cmdline
  cmdline="$(ps -p "$pid" -o command= 2>/dev/null || true)"
  case "$cmdline" in
    *"$ROOT/scripts/model_scan.sh"*) return 0 ;;
    *"scan_weather_market.py"*"--scan-weather-today"*) return 0 ;;
  esac
  return 1
}

_pid_running() {
  local pid="${1:-}"
  if ! [[ "${pid}" =~ ^[0-9]+$ ]]; then
    return 1
  fi
  kill -0 "${pid}" 2>/dev/null
}
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  lock_pid=""
  if [ -f "$LOCK_PID_FILE" ]; then
    lock_pid="$(cat "$LOCK_PID_FILE" 2>/dev/null || true)"
  fi
  if _scan_pid_running "$lock_pid"; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] already running, skip (pid=$lock_pid)" >> "$ROOT/data/model_scan.log"
    exit 0
  fi
  if pgrep -f "scan_weather_market.py .*--scan-weather-today" >/dev/null 2>&1; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] already running, skip (matched scan process)" >> "$ROOT/data/model_scan.log"
    exit 0
  fi
  echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] stale lock cleared" >> "$ROOT/data/model_scan.log"
  rm -rf "$LOCK_DIR"
  mkdir "$LOCK_DIR" 2>/dev/null || {
    echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] unable to acquire lock, skip" >> "$ROOT/data/model_scan.log"
    exit 0
  }
fi
echo "$$" > "$LOCK_PID_FILE"
model_lock_acquired=1
: "${PM_DB_WRITER_LOCK_WAIT_SEC:=90}"
: "${PM_DB_WRITER_LOCK_POLL_SEC:=1}"
: "${PM_MODEL_SCAN_DB_LOCK_ENABLED:=1}"
if ! [[ "${PM_DB_WRITER_LOCK_WAIT_SEC}" =~ ^[0-9]+$ ]] || [ "${PM_DB_WRITER_LOCK_WAIT_SEC}" -lt 0 ]; then
  PM_DB_WRITER_LOCK_WAIT_SEC=90
fi
if ! [[ "${PM_DB_WRITER_LOCK_POLL_SEC}" =~ ^[0-9]+$ ]] || [ "${PM_DB_WRITER_LOCK_POLL_SEC}" -lt 1 ]; then
  PM_DB_WRITER_LOCK_POLL_SEC=1
fi
if [ "${PM_MODEL_SCAN_DB_LOCK_ENABLED}" != "1" ]; then
  PM_MODEL_SCAN_DB_LOCK_ENABLED=0
fi
db_lock_acquired=0
release_model_scan_lock() {
  if [ "${model_lock_acquired:-0}" -ne 1 ]; then
    return 0
  fi
  cur_pid=""
  if [ -f "$LOCK_PID_FILE" ]; then
    cur_pid="$(cat "$LOCK_PID_FILE" 2>/dev/null || true)"
  fi
  if [ -z "${cur_pid:-}" ] || [ "$cur_pid" = "$$" ]; then
    rm -rf "$LOCK_DIR" 2>/dev/null || true
  fi
  model_lock_acquired=0
}
release_db_writer_lock() {
  if [ "${db_lock_acquired:-0}" -ne 1 ]; then
    return 0
  fi
  weather_refresh_db_writer_release
  db_lock_acquired=0
}
cleanup_locks() {
  weather_scan_lane_release
  release_model_scan_lock
  release_db_writer_lock
}
trap cleanup_locks EXIT
model_scan_lane_wait_default=70
if [ "${MODEL_FACT_STALE_BYPASS:-0}" = "1" ]; then
  model_scan_lane_wait_default=90
fi
PM_MODEL_SCAN_LANE_WAIT_SEC="$(normalize_positive_int_max "${PM_MODEL_SCAN_LANE_WAIT_SEC:-${model_scan_lane_wait_default}}" "${model_scan_lane_wait_default}" 120)"
if ! weather_scan_lane_acquire "model_scan" "$PM_MODEL_SCAN_LANE_WAIT_SEC" "$ROOT/data/model_scan.log" "model_scan"; then
  exit 0
fi
if [ "${PM_MODEL_SCAN_DB_LOCK_ENABLED}" = "1" ]; then
  if ! weather_refresh_db_writer_acquire "model_scan" "$PM_DB_WRITER_LOCK_WAIT_SEC" "$ROOT/data/model_scan.log" "model_scan"; then
    exit 0
  fi
  db_lock_acquired=1
else
  echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] db_writer lock disabled for whole run; live SSOT owns write priority" >> "$ROOT/data/model_scan.log"
fi

if [ "${MODEL_WEATHER_LIKELIHOOD_STALE_BYPASS:-0}" = "1" ] \
  && [ "${PM_MODEL_SCAN_AWC_FACT_REFRESH_ENABLED:-0}" = "1" ] \
  && [ "${MODEL_SCAN_AWC_FACT_REFRESH_DONE:-0}" != "1" ]; then
  if _model_scan_run_awc_fact_refresh "locked_lane"; then
    awc_fact_refresh_rc=0
  else
    awc_fact_refresh_rc="$?"
  fi
  if [ "${awc_fact_refresh_rc}" -eq 0 ]; then
    MODEL_SCAN_AWC_FACT_REFRESH_DONE=1
    _model_scan_refresh_weather_likelihood_stale_state
  fi
  if [ "${awc_fact_refresh_rc}" -eq 0 ] && _model_scan_awc_refresh_skip_scan_allowed; then
    if [ "${MODEL_SCAN_SCHEDULE_STATE:-background}" = "hot" ] \
      && [ "${PM_MODEL_SCAN_AWC_FACT_REFRESH_SKIP_HOT_SCHEDULE:-1}" != "1" ]; then
      echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] awc_fact_refresh ok but hot schedule requires heavyweight scan" >> "$ROOT/data/model_scan.log"
    else
      echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] awc_fact_refresh satisfied weather stale; skip heavyweight scan" >> "$ROOT/data/model_scan.log"
      exit 0
    fi
  elif [ "${awc_fact_refresh_rc}" -eq 0 ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] awc_fact_refresh locked-lane complete; continue regular scan weather_age_sec=${MODEL_WEATHER_LIKELIHOOD_AGE_SEC} weather_stale=${MODEL_WEATHER_LIKELIHOOD_STALE_BYPASS}" >> "$ROOT/data/model_scan.log"
  elif [ "${awc_fact_refresh_rc}" -ne 0 ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] awc_fact_refresh locked-lane degraded rc=${awc_fact_refresh_rc}; continue regular scan" >> "$ROOT/data/model_scan.log"
  fi
fi

# Low-latency mode: prioritize fast updates (30s target) over full-scope refresh.
: "${PM_LOW_LATENCY_MODE:=0}"
if [ "${PM_LOW_LATENCY_MODE}" = "1" ]; then
  # Low-latency is intended to override .env (avoid being "stuck" with large fan-out values).
  export PM_SCAN_GAMMA_TIMEOUT="${PM_SCAN_GAMMA_TIMEOUT:-12}"
  export PM_SCAN_GAMMA_RETRIES="${PM_SCAN_GAMMA_RETRIES:-2}"
  export PM_SCAN_GAMMA_BACKOFF="${PM_SCAN_GAMMA_BACKOFF:-0.6}"
  export PM_SCAN_GAMMA_MAX_PAGES="${PM_SCAN_GAMMA_MAX_PAGES:-60}"
  # Keep low-latency responsive, but do not use an aggressive Open-Meteo cadence:
  # very small spacing can trigger repeated 429 and starve model/facts coverage.
  export PM_SCAN_OPEN_METEO_SPACING_MIN="${PM_SCAN_OPEN_METEO_SPACING_MIN:-1.5}"
  export PM_SCAN_OPEN_METEO_SPACING_MAX="${PM_SCAN_OPEN_METEO_SPACING_MAX:-3.5}"
  export PM_SCAN_OPEN_METEO_429_RETRIES="${PM_SCAN_OPEN_METEO_429_RETRIES:-2}"
  export PM_SCAN_OPEN_METEO_429_MIN_SLEEP="${PM_SCAN_OPEN_METEO_429_MIN_SLEEP:-6.0}"
  export PM_SCAN_OPEN_METEO_429_MAX_SLEEP="${PM_SCAN_OPEN_METEO_429_MAX_SLEEP:-45.0}"
  export PM_SCAN_OPEN_METEO_429_JITTER="${PM_SCAN_OPEN_METEO_429_JITTER:-0.35}"
  export PM_SCAN_OPEN_METEO_CITY_COOLDOWN_SEC="${PM_SCAN_OPEN_METEO_CITY_COOLDOWN_SEC:-1800}"
  export PM_SCAN_OPEN_METEO_GLOBAL_RATE_LIMIT="${PM_SCAN_OPEN_METEO_GLOBAL_RATE_LIMIT:-1}"
  export PM_SCAN_OPEN_METEO_BATCH_MODELS="${PM_SCAN_OPEN_METEO_BATCH_MODELS:-1}"
  export PM_WEATHER_EVENTS_CACHE_MINUTES="${PM_WEATHER_EVENTS_CACHE_MINUTES:-30}"
  # Low-latency defaults keep fan-out tight, but still allow explicit env overrides.
  export PM_SCAN_FAST_DAYS_AHEAD="${PM_SCAN_FAST_DAYS_AHEAD:-0}"
  export PM_SCAN_FULL_DAYS_AHEAD="${PM_SCAN_FULL_DAYS_AHEAD:-0}"
  export PM_SCAN_FULL_INTERVAL_MINUTES="${PM_SCAN_FULL_INTERVAL_MINUTES:-0}"
  # Model scan wrapper uses PM_MODEL_SCAN_*; set those directly.
  export PM_MODEL_SCAN_MAX_EVENTS_PER_RUN="$(normalize_positive_int_max "${PM_MODEL_SCAN_MAX_EVENTS_PER_RUN:-2}" 2 2)"
  export PM_MODEL_SCAN_MAX_MATCHED_PER_RUN="$(normalize_positive_int_max "${PM_MODEL_SCAN_MAX_MATCHED_PER_RUN:-${PM_MODEL_SCAN_MAX_EVENTS_PER_RUN}}" "${PM_MODEL_SCAN_MAX_EVENTS_PER_RUN}" "${PM_MODEL_SCAN_MAX_EVENTS_PER_RUN}")"
  # Rules-first: only fetch event detail when cached/list metadata lacks the settlement contract.
  export PM_SCAN_FETCH_EVENT_DETAIL="0"
  export PM_SCAN_RULES_FIRST="${PM_SCAN_RULES_FIRST:-1}"
  export PM_SCAN_REQUIRE_CONTRACT_PROFILE="${PM_SCAN_REQUIRE_CONTRACT_PROFILE:-1}"
  export PM_MODEL_SCAN_COMPOSE_MARKET_BOOTSTRAP="${PM_MODEL_SCAN_COMPOSE_MARKET_BOOTSTRAP:-${PM_MARKET_SCAN_COMPOSE_MARKET_BOOTSTRAP:-0}}"
  export PM_MARKET_SCAN_COMPOSE_MARKET_BOOTSTRAP="${PM_MARKET_SCAN_COMPOSE_MARKET_BOOTSTRAP:-${PM_MODEL_SCAN_COMPOSE_MARKET_BOOTSTRAP}}"
  export PM_SCAN_ROTATE_EVENTS="${PM_SCAN_ROTATE_EVENTS:-1}"
  export PM_SCAN_PRIORITY_ENABLED="1"
  export PM_SCAN_PRIORITY_TOPN="${PM_SCAN_PRIORITY_TOPN:-8}"
  export PM_SCAN_PRIORITY_LOOKAHEAD_DAYS="${PM_SCAN_PRIORITY_LOOKAHEAD_DAYS:-2}"
  export PM_SCAN_PRIORITY_INCLUDE_NEW_MARKETS="${PM_SCAN_PRIORITY_INCLUDE_NEW_MARKETS:-1}"
  export PM_SCAN_PRIORITY_NEW_MARKET_TOPN="${PM_SCAN_PRIORITY_NEW_MARKET_TOPN:-4}"
  export PM_SCAN_PRIORITY_NEW_MARKET_MAX_AGE_MIN="${PM_SCAN_PRIORITY_NEW_MARKET_MAX_AGE_MIN:-240}"
  export PM_SCAN_PRIORITY_INCLUDE_FUTURE_MODEL_MISSING="${PM_SCAN_PRIORITY_INCLUDE_FUTURE_MODEL_MISSING:-1}"
  export PM_SCAN_PRIORITY_FUTURE_MODEL_MISSING_TOPN="${PM_SCAN_PRIORITY_FUTURE_MODEL_MISSING_TOPN:-6}"
  export PM_SCAN_PRIORITY_INCLUDE_RECOMMENDED_PILOT_GAPS="${PM_SCAN_PRIORITY_INCLUDE_RECOMMENDED_PILOT_GAPS:-1}"
  export PM_SCAN_PRIORITY_RECOMMENDED_PILOT_GAP_TOPN="${PM_SCAN_PRIORITY_RECOMMENDED_PILOT_GAP_TOPN:-4}"
  # Keep priority batch small so a run finishes well within the 900s watchdog.
  export PM_SCAN_PRIORITY_CANDIDATES_PER_RUN="2"
  # When no Top OK exists, scanning only "fixed" slugs causes the rest of today's events to go stale,
  # which makes facts health misleading and can delay discovery of new opportunities.
  export PM_SCAN_PRIORITY_OTHERS_PER_RUN="$(normalize_int_range "${PM_SCAN_PRIORITY_OTHERS_PER_RUN:-1}" 1 0 1)"
  export PM_SCAN_PRIORITY_INCLUDE_OPEN_ORDERS="1"
  # Tail-chase stale events (by market tick age) so we don't accumulate a long stale tail under priority mode.
  export PM_SCAN_PRIORITY_INCLUDE_TAIL="${PM_SCAN_PRIORITY_INCLUDE_TAIL:-1}"
  export PM_SCAN_PRIORITY_TAIL_TOPN="${PM_SCAN_PRIORITY_TAIL_TOPN:-1}"
  export PM_SCAN_PRIORITY_TAIL_MIN_AGE_SEC="${PM_SCAN_PRIORITY_TAIL_MIN_AGE_SEC:-2700}" # 45m
  export PM_SCAN_PRIORITY_INCLUDE_FACT_TAIL="${PM_SCAN_PRIORITY_INCLUDE_FACT_TAIL:-1}"
  export PM_SCAN_PRIORITY_FACT_TAIL_TOPN="${PM_SCAN_PRIORITY_FACT_TAIL_TOPN:-2}"
  export PM_SCAN_PRIORITY_FACT_TAIL_MIN_AGE_SEC="${PM_SCAN_PRIORITY_FACT_TAIL_MIN_AGE_SEC:-2700}"
  export PM_SCAN_RETRY_MAX="$(normalize_int_range "${PM_SCAN_RETRY_MAX:-0}" 0 0 0)"
  export PM_SCAN_AUTO_DIAG="0"
  export PM_SCAN_DEPTH_LEVELS="${PM_SCAN_DEPTH_LEVELS:-3}"
  export PM_TRADE_SYNC_AFTER_SCAN="0"
  export PM_CACHE_TTL_NWS_SEC="${PM_CACHE_TTL_NWS_SEC:-900}"
  export PM_CACHE_TTL_ENSEMBLE_SEC="${PM_CACHE_TTL_ENSEMBLE_SEC:-7200}"
  export PM_MODEL_BACKGROUND_REFRESH_MINUTES="${PM_MODEL_BACKGROUND_REFRESH_MINUTES:-20}"
  export PM_MODEL_WINDOW_REFRESH_MINUTES="${PM_MODEL_WINDOW_REFRESH_MINUTES:-2}"
  export PM_SCAN_MODEL_REFRESH_INTERVAL_MINUTES="${PM_SCAN_MODEL_REFRESH_INTERVAL_MINUTES:-20}"
fi

# Force proxy for Polymarket/Gamma and allow insecure SSL fallback when needed.
FORCE_PROXY_HOSTS_DEFAULT="polymarket.com,gamma-api.polymarket.com,clob.polymarket.com"
if [ -z "${PM_SCAN_FORCE_PROXY_HOSTS:-}" ]; then
  export PM_SCAN_FORCE_PROXY_HOSTS="$FORCE_PROXY_HOSTS_DEFAULT"
else
  export PM_SCAN_FORCE_PROXY_HOSTS="${PM_SCAN_FORCE_PROXY_HOSTS},${FORCE_PROXY_HOSTS_DEFAULT}"
fi
export PM_SCAN_ALLOW_INSECURE_SSL=1
: "${PM_SCAN_GAMMA_TIMEOUT:=12}"
: "${PM_SCAN_GAMMA_RETRIES:=2}"
: "${PM_SCAN_GAMMA_BACKOFF:=0.6}"
: "${PM_SCAN_GAMMA_MAX_PAGES:=60}"
export PM_SCAN_GAMMA_TIMEOUT PM_SCAN_GAMMA_RETRIES PM_SCAN_GAMMA_BACKOFF PM_SCAN_GAMMA_MAX_PAGES
export PM_SCAN_FETCH_EVENT_DETAIL="${PM_SCAN_FETCH_EVENT_DETAIL:-0}"
export PM_SCAN_RULES_FIRST="${PM_SCAN_RULES_FIRST:-1}"
export PM_SCAN_REQUIRE_CONTRACT_PROFILE="${PM_SCAN_REQUIRE_CONTRACT_PROFILE:-1}"
export PM_SCAN_OPEN_METEO_TIMEOUT_SEC="${PM_SCAN_OPEN_METEO_TIMEOUT_SEC:-10}"
export PM_SCAN_OPEN_METEO_RETRIES="${PM_SCAN_OPEN_METEO_RETRIES:-2}"
export PM_SCAN_OPEN_METEO_429_RETRIES="${PM_SCAN_OPEN_METEO_429_RETRIES:-2}"
export PM_DB_CONNECT_TIMEOUT_SEC="$(normalize_positive_int_max "${PM_DB_CONNECT_TIMEOUT_SEC:-2}" 2 2)"
export PM_DB_BUSY_TIMEOUT_MS="$(normalize_positive_int_max "${PM_DB_BUSY_TIMEOUT_MS:-5000}" 5000 5000)"
export PM_SCAN_OPEN_METEO_TZ_FALLBACKS="${PM_SCAN_OPEN_METEO_TZ_FALLBACKS:-1}"
: "${PM_SCAN_OPEN_METEO_SPACING_MIN:=1.5}"
: "${PM_SCAN_OPEN_METEO_SPACING_MAX:=3.5}"
: "${PM_SCAN_OPEN_METEO_429_RETRIES:=2}"
: "${PM_SCAN_OPEN_METEO_429_MIN_SLEEP:=6.0}"
: "${PM_SCAN_OPEN_METEO_429_MAX_SLEEP:=45.0}"
: "${PM_SCAN_OPEN_METEO_429_JITTER:=0.35}"
: "${PM_SCAN_OPEN_METEO_CITY_COOLDOWN_SEC:=3600}"
: "${PM_SCAN_OPEN_METEO_GLOBAL_RATE_LIMIT:=1}"
: "${PM_SCAN_OPEN_METEO_BATCH_MODELS:=1}"
: "${PM_SCAN_PRIORITY_POSITION_LOOKBACK_DAYS:=2}"
if ! [[ "${PM_SCAN_PRIORITY_POSITION_LOOKBACK_DAYS}" =~ ^-?[0-9]+$ ]] || [ "${PM_SCAN_PRIORITY_POSITION_LOOKBACK_DAYS}" -lt 0 ]; then
  PM_SCAN_PRIORITY_POSITION_LOOKBACK_DAYS=2
fi
export PM_SCAN_OPEN_METEO_SPACING_MIN PM_SCAN_OPEN_METEO_SPACING_MAX
export PM_SCAN_OPEN_METEO_429_RETRIES PM_SCAN_OPEN_METEO_429_MIN_SLEEP PM_SCAN_OPEN_METEO_429_MAX_SLEEP PM_SCAN_OPEN_METEO_429_JITTER
export PM_SCAN_OPEN_METEO_CITY_COOLDOWN_SEC PM_SCAN_OPEN_METEO_GLOBAL_RATE_LIMIT PM_SCAN_OPEN_METEO_BATCH_MODELS PM_SCAN_OPEN_METEO_TZ_FALLBACKS
export PM_WEATHER_EVENTS_CACHE_MINUTES PM_SCAN_MAX_EVENTS_PER_RUN PM_SCAN_MAX_MATCHED_PER_RUN PM_SCAN_FETCH_EVENT_DETAIL PM_SCAN_RULES_FIRST PM_SCAN_REQUIRE_CONTRACT_PROFILE PM_SCAN_ROTATE_EVENTS
export PM_SCAN_PRIORITY_INCLUDE_SETTLEMENT_PENDING="${PM_SCAN_PRIORITY_INCLUDE_SETTLEMENT_PENDING:-1}"
export PM_SCAN_PRIORITY_SETTLEMENT_PENDING_TOPN="${PM_SCAN_PRIORITY_SETTLEMENT_PENDING_TOPN:-${PM_MODEL_SCAN_MAX_EVENTS_PER_RUN:-2}}"
export PM_SCAN_PRIORITY_SETTLEMENT_PENDING_LOOKBACK_DAYS="${PM_SCAN_PRIORITY_SETTLEMENT_PENDING_LOOKBACK_DAYS:-7}"
export PM_SCAN_PRIORITY_ENABLED PM_SCAN_PRIORITY_TOPN PM_SCAN_PRIORITY_LOOKAHEAD_DAYS PM_SCAN_PRIORITY_CANDIDATES_PER_RUN PM_SCAN_PRIORITY_OTHERS_PER_RUN PM_SCAN_PRIORITY_MIN_EDGE_SCORE PM_SCAN_PRIORITY_INCLUDE_OPEN_ORDERS PM_SCAN_PRIORITY_POSITION_LOOKBACK_DAYS PM_SCAN_PRIORITY_INCLUDE_NEW_MARKETS PM_SCAN_PRIORITY_NEW_MARKET_TOPN PM_SCAN_PRIORITY_NEW_MARKET_MAX_AGE_MIN PM_SCAN_PRIORITY_INCLUDE_FUTURE_MODEL_MISSING PM_SCAN_PRIORITY_FUTURE_MODEL_MISSING_TOPN PM_SCAN_PRIORITY_INCLUDE_RECOMMENDED_PILOT_GAPS PM_SCAN_PRIORITY_RECOMMENDED_PILOT_GAP_TOPN PM_SCAN_PRIORITY_INCLUDE_SETTLEMENT_PENDING PM_SCAN_PRIORITY_SETTLEMENT_PENDING_TOPN PM_SCAN_PRIORITY_SETTLEMENT_PENDING_LOOKBACK_DAYS PM_SCAN_ENSEMBLE_MODELS
: "${PM_DISABLE_OPEN_METEO_GEOCODE:=1}"
export PM_DISABLE_OPEN_METEO_GEOCODE

UPDATE_MODELS_ARG="--no-update-models"
MODEL_REFRESH_MODE="cache"
: "${PM_SCAN_MODEL_REFRESH_INTERVAL_MINUTES:=60}"
: "${PM_SCAN_MODEL_REFRESH_STAMP:=$ROOT/data/model_scan.model_refresh_ts}"
if ! [[ "$PM_SCAN_MODEL_REFRESH_INTERVAL_MINUTES" =~ ^-?[0-9]+$ ]]; then
  PM_SCAN_MODEL_REFRESH_INTERVAL_MINUTES=60
fi
if [ "${PM_SCAN_NO_UPDATE_MODELS:-0}" != "1" ]; then
  if [ "$PM_SCAN_MODEL_REFRESH_INTERVAL_MINUTES" -le 0 ]; then
    UPDATE_MODELS_ARG=""
    MODEL_REFRESH_MODE="refresh"
  else
    now_ts="$(date +%s)"
    last_ts=0
    if [ -f "$PM_SCAN_MODEL_REFRESH_STAMP" ]; then
      last_ts="$(cat "$PM_SCAN_MODEL_REFRESH_STAMP" 2>/dev/null || echo 0)"
    fi
    if ! [[ "$last_ts" =~ ^[0-9]+$ ]]; then
      last_ts=0
    fi
    if [ "$last_ts" -gt 0 ] && [ "$last_ts" -gt "$now_ts" ]; then
      last_ts=0
    fi
    if [ $((now_ts - last_ts)) -ge $((PM_SCAN_MODEL_REFRESH_INTERVAL_MINUTES * 60)) ]; then
      UPDATE_MODELS_ARG=""
      MODEL_REFRESH_MODE="refresh"
      echo "$now_ts" > "$PM_SCAN_MODEL_REFRESH_STAMP"
    fi
  fi
fi

MODEL_ONLY=1

: "${PM_SCAN_FAST_DAYS_AHEAD:=2}"
: "${PM_SCAN_FULL_DAYS_AHEAD:=2}"
: "${PM_SCAN_FULL_INTERVAL_MINUTES:=30}"
if ! [[ "${PM_SCAN_FAST_DAYS_AHEAD}" =~ ^-?[0-9]+$ ]]; then
  PM_SCAN_FAST_DAYS_AHEAD=2
fi
if ! [[ "${PM_SCAN_FULL_DAYS_AHEAD}" =~ ^-?[0-9]+$ ]]; then
  PM_SCAN_FULL_DAYS_AHEAD="${PM_SCAN_FAST_DAYS_AHEAD}"
fi
: "${PM_MODEL_SCAN_ALLOW_NEGATIVE_DAYS_AHEAD:=0}"
if [ "${PM_MODEL_SCAN_ALLOW_NEGATIVE_DAYS_AHEAD}" != "1" ] && [ "${PM_SCAN_FAST_DAYS_AHEAD}" -lt 0 ]; then
  PM_SCAN_FAST_DAYS_AHEAD=2
fi
if [ "${PM_MODEL_SCAN_ALLOW_NEGATIVE_DAYS_AHEAD}" != "1" ] && [ "${PM_SCAN_FULL_DAYS_AHEAD}" -lt 0 ]; then
  PM_SCAN_FULL_DAYS_AHEAD="${PM_SCAN_FAST_DAYS_AHEAD}"
fi

SCAN_MODE="normal"
DAYS_ARG="--days-ahead ${PM_SCAN_FAST_DAYS_AHEAD}"
if [ "${PM_SCAN_FULL_INTERVAL_MINUTES}" -gt 0 ]; then
  minute="$(date +%M)"
  minute=$((10#$minute))
  if [ $((minute % PM_SCAN_FULL_INTERVAL_MINUTES)) -eq 0 ]; then
    SCAN_MODE="full"
    DAYS_ARG="--days-ahead ${PM_SCAN_FULL_DAYS_AHEAD}"
  fi
fi

# Pick up macOS system proxy settings only when env proxy is not provided.
HAS_PROXY_ENV=0
if [ -n "${HTTP_PROXY:-}" ] || [ -n "${HTTPS_PROXY:-}" ] || [ -n "${ALL_PROXY:-}" ]; then
  HAS_PROXY_ENV=1
fi
if [ "$HAS_PROXY_ENV" = "0" ] && [ -n "${PM_PROXY_PORTS:-}" ]; then
  FIRST_PORT="$(echo "$PM_PROXY_PORTS" | tr ', ' '\n' | awk 'NF{print $1; exit}')"
  if [ -n "$FIRST_PORT" ]; then
    export HTTP_PROXY="http://127.0.0.1:$FIRST_PORT"
    export HTTPS_PROXY="http://127.0.0.1:$FIRST_PORT"
    HAS_PROXY_ENV=1
  fi
fi
if [ "$HAS_PROXY_ENV" = "0" ] && command -v scutil >/dev/null 2>&1; then
  PROXY_INFO="$(scutil --proxy)"
  HTTP_ENABLE="$(echo "$PROXY_INFO" | awk '/HTTPEnable/{print $3}')"
  HTTPS_ENABLE="$(echo "$PROXY_INFO" | awk '/HTTPSEnable/{print $3}')"
  SOCKS_ENABLE="$(echo "$PROXY_INFO" | awk '/SOCKSEnable/{print $3}')"
  HTTP_PROXY_HOST="$(echo "$PROXY_INFO" | awk '/HTTPProxy/{print $3}')"
  HTTP_PROXY_PORT="$(echo "$PROXY_INFO" | awk '/HTTPPort/{print $3}')"
  HTTPS_PROXY_HOST="$(echo "$PROXY_INFO" | awk '/HTTPSProxy/{print $3}')"
  HTTPS_PROXY_PORT="$(echo "$PROXY_INFO" | awk '/HTTPSPort/{print $3}')"
  SOCKS_PROXY_HOST="$(echo "$PROXY_INFO" | awk '/SOCKSProxy/{print $3}')"
  SOCKS_PROXY_PORT="$(echo "$PROXY_INFO" | awk '/SOCKSPort/{print $3}')"
  if [ "$HTTP_ENABLE" = "1" ] && [ -n "$HTTP_PROXY_HOST" ] && [ -n "$HTTP_PROXY_PORT" ]; then
    export HTTP_PROXY="http://$HTTP_PROXY_HOST:$HTTP_PROXY_PORT"
  fi
  if [ "$HTTPS_ENABLE" = "1" ] && [ -n "$HTTPS_PROXY_HOST" ] && [ -n "$HTTPS_PROXY_PORT" ]; then
    export HTTPS_PROXY="http://$HTTPS_PROXY_HOST:$HTTPS_PROXY_PORT"
  fi
  if [ "$SOCKS_ENABLE" = "1" ] && [ -n "$SOCKS_PROXY_HOST" ] && [ -n "$SOCKS_PROXY_PORT" ]; then
    export ALL_PROXY="socks5://$SOCKS_PROXY_HOST:$SOCKS_PROXY_PORT"
  fi
fi

# Keep proxy settings by default; set PM_SCAN_FORCE_DIRECT=1 to bypass.
# PM_CLOB_FORCE_PROXY is the live-trading safety rail and wins over scan-level
# force-direct overrides.
if [ "${PM_CLOB_FORCE_PROXY:-0}" = "1" ]; then
  PM_SCAN_FORCE_DIRECT=0
fi
if [ "${PM_SCAN_FORCE_DIRECT:-0}" = "1" ]; then
  unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
fi

mkdir -p "$ROOT/data"
LOG="$ROOT/data/model_scan.log"
INSECURE_ARG=""
if [ "${PM_SCAN_INSECURE:-0}" = "1" ]; then
  INSECURE_ARG="--insecure"
fi

PM_SCAN_RETRY_MAX="$(normalize_int_range "${PM_SCAN_RETRY_MAX:-0}" 0 0 0)"
: "${PM_SCAN_RETRY_SLEEP_SEC:=10}"
if ! [[ "$PM_SCAN_RETRY_SLEEP_SEC" =~ ^[0-9]+$ ]]; then
  PM_SCAN_RETRY_SLEEP_SEC=20
fi

# Model scan owns settlement fact recovery. Market refresh may see these events, but closed markets often
# return no orderbook, so settlement-pending slugs must enter the model/archive path by default.
export PM_SCAN_PRIORITY_ENABLED="${PM_SCAN_PRIORITY_ENABLED:-1}"
export PM_SCAN_PRIORITY_INCLUDE_SETTLEMENT_PENDING="${PM_SCAN_PRIORITY_INCLUDE_SETTLEMENT_PENDING:-1}"
export PM_SCAN_PRIORITY_SETTLEMENT_PENDING_TOPN="${PM_SCAN_PRIORITY_SETTLEMENT_PENDING_TOPN:-${PM_MODEL_SCAN_MAX_EVENTS_PER_RUN:-2}}"
export PM_SCAN_PRIORITY_SETTLEMENT_PENDING_LOOKBACK_DAYS="${PM_SCAN_PRIORITY_SETTLEMENT_PENDING_LOOKBACK_DAYS:-7}"

SCAN_AUTO_DIAG_ARG=""
export PM_SCAN_AUTO_DIAG="${PM_SCAN_AUTO_DIAG:-0}"
if [ "${PM_SCAN_AUTO_DIAG:-0}" != "1" ]; then
  SCAN_AUTO_DIAG_ARG="--no-auto-diagnose"
fi
: "${PM_SCAN_DEPTH_LEVELS:=5}"
: "${PM_SCAN_ENSEMBLE_MODELS:=gfs_seamless,icon_seamless,ecmwf_ifs025,nws_forecast}"
export PM_MODEL_SCAN_COMPOSE_MARKET_BOOTSTRAP="${PM_MODEL_SCAN_COMPOSE_MARKET_BOOTSTRAP:-${PM_MARKET_SCAN_COMPOSE_MARKET_BOOTSTRAP:-0}}"
export PM_MARKET_SCAN_COMPOSE_MARKET_BOOTSTRAP="${PM_MARKET_SCAN_COMPOSE_MARKET_BOOTSTRAP:-${PM_MODEL_SCAN_COMPOSE_MARKET_BOOTSTRAP}}"
export PM_ARCHIVE_WRITE_SCOPE="${PM_ARCHIVE_WRITE_SCOPE:-chengdu_zuuu}"
PM_MODEL_SCAN_MAX_EVENTS_PER_RUN="$(normalize_positive_int_max "${PM_MODEL_SCAN_MAX_EVENTS_PER_RUN:-2}" 2 2)"
PM_MODEL_SCAN_MAX_MATCHED_PER_RUN="$(normalize_positive_int_max "${PM_MODEL_SCAN_MAX_MATCHED_PER_RUN:-${PM_MODEL_SCAN_MAX_EVENTS_PER_RUN}}" "${PM_MODEL_SCAN_MAX_EVENTS_PER_RUN}" "${PM_MODEL_SCAN_MAX_EVENTS_PER_RUN}")"
if [ "${MODEL_REFRESH_MODE:-cache}" = "refresh" ]; then
  PM_MODEL_SCAN_REFRESH_MAX_EVENTS_PER_RUN="$(normalize_positive_int_max "${PM_MODEL_SCAN_REFRESH_MAX_EVENTS_PER_RUN:-2}" 2 3)"
  if [ "${PM_MODEL_SCAN_MAX_EVENTS_PER_RUN}" -gt "${PM_MODEL_SCAN_REFRESH_MAX_EVENTS_PER_RUN}" ]; then
    PM_MODEL_SCAN_MAX_EVENTS_PER_RUN="${PM_MODEL_SCAN_REFRESH_MAX_EVENTS_PER_RUN}"
  fi
  if [ "${PM_MODEL_SCAN_MAX_MATCHED_PER_RUN}" -gt "${PM_MODEL_SCAN_MAX_EVENTS_PER_RUN}" ]; then
    PM_MODEL_SCAN_MAX_MATCHED_PER_RUN="${PM_MODEL_SCAN_MAX_EVENTS_PER_RUN}"
  fi
fi
PM_SCAN_MODEL_SCAN_TIMEOUT_SEC="$(normalize_positive_int_max "${PM_SCAN_MODEL_SCAN_TIMEOUT_SEC:-300}" 300 900)"

attempt=0
final_scan_code=0
while true; do
  attempt=$((attempt + 1))
  {
    echo ""
    echo "---- $(date '+%Y-%m-%d %H:%M:%S') model_scan start ----"
    echo "PY=$PY"
    echo "MODE=$SCAN_MODE DAYS=$DAYS_ARG MODEL_REFRESH=$MODEL_REFRESH_MODE MODEL_ONLY=$MODEL_ONLY LOW_LATENCY=${PM_LOW_LATENCY_MODE:-0} MAX_EVENTS=${PM_MODEL_SCAN_MAX_EVENTS_PER_RUN:-0} MAX_MATCHED=${PM_MODEL_SCAN_MAX_MATCHED_PER_RUN:-0} PRIORITY=${PM_SCAN_PRIORITY_ENABLED:-0} P_CAND=${PM_SCAN_PRIORITY_CANDIDATES_PER_RUN:-0} P_OTHERS=${PM_SCAN_PRIORITY_OTHERS_PER_RUN:-0} P_GAP=${PM_SCAN_PRIORITY_RECOMMENDED_PILOT_GAP_TOPN:-0} P_SETTLE=${PM_SCAN_PRIORITY_SETTLEMENT_PENDING_TOPN:-0} P_OPEN=${PM_SCAN_PRIORITY_INCLUDE_OPEN_ORDERS:-0} AUTO_DIAG=${PM_SCAN_AUTO_DIAG:-1} DEPTH=${PM_SCAN_DEPTH_LEVELS:-5} MODELS=${PM_SCAN_ENSEMBLE_MODELS} SCHED=${MODEL_SCAN_SCHEDULE_STATE:-na} SCHED_REF=${MODEL_SCAN_SCHEDULE_REF:-none} SCHED_DELTA=${MODEL_SCAN_SCHEDULE_DELTA_MIN:-na}m SCHED_MODELS=${MODEL_SCAN_SCHEDULE_MODELS:-none} FACT_AGE=${MODEL_FACT_AGE_SEC:-na}s FACT_STALE=${MODEL_FACT_STALE_BYPASS:-0} WEATHER_EVENT=${MODEL_WEATHER_LIKELIHOOD_EVENT_DATE:-na} WEATHER_AGE=${MODEL_WEATHER_LIKELIHOOD_AGE_SEC:-na}s WEATHER_STALE=${MODEL_WEATHER_LIKELIHOOD_STALE_BYPASS:-0} BOOTSTRAP=${PM_MARKET_SCAN_COMPOSE_MARKET_BOOTSTRAP:-0} TTL_NWS=${PM_CACHE_TTL_NWS_SEC:-3600}s TTL_ENS=${PM_CACHE_TTL_ENSEMBLE_SEC:-21600}s BG_REFRESH=${PM_MODEL_BACKGROUND_REFRESH_MINUTES:-45}m WIN_REFRESH=${PM_MODEL_WINDOW_REFRESH_MINUTES:-3}m W_UPDATE_EVERY=${PM_SCAN_MODEL_REFRESH_INTERVAL_MINUTES:-60}m OM_GLOBAL=${PM_SCAN_OPEN_METEO_GLOBAL_RATE_LIMIT:-1} OM_BATCH=${PM_SCAN_OPEN_METEO_BATCH_MODELS:-1} OM_TZ_FB=${PM_SCAN_OPEN_METEO_TZ_FALLBACKS:-1} OM_SPACE=${PM_SCAN_OPEN_METEO_SPACING_MIN:-1.5}-${PM_SCAN_OPEN_METEO_SPACING_MAX:-3.5}s OM_429=${PM_SCAN_OPEN_METEO_429_MIN_SLEEP:-6}-${PM_SCAN_OPEN_METEO_429_MAX_SLEEP:-45}s x${PM_SCAN_OPEN_METEO_429_RETRIES:-2} OM_CD=${PM_SCAN_OPEN_METEO_CITY_COOLDOWN_SEC:-3600}s NICE=${MODEL_SCAN_NICE}"
  } >> "$LOG"
  if [ "${PM_MODEL_SCAN_SCHEDULE_ENABLED:-1}" = "1" ]; then
    date '+%s' > "$PM_MODEL_SCAN_SCHEDULE_STAMP"
  fi
  date '+%Y-%m-%d %H:%M:%S' > "$ROOT/data/model_scan.last_start"
  SCAN_START_UTC="$(date -u '+%Y-%m-%dT%H:%M:%S+00:00')"
  set +e
  # Keep yesterday in the scan window to cover settlement-lag events shown in UI freshness alerts.
  : "${PM_SCAN_INCLUDE_YESTERDAY:=1}"
  export PM_SCAN_INCLUDE_YESTERDAY
  run_with_timeout "${PM_SCAN_MODEL_SCAN_TIMEOUT_SEC}" "$LOG" \
    nice -n "$MODEL_SCAN_NICE" \
    env \
      PM_SCAN_MAX_EVENTS_PER_RUN="${PM_MODEL_SCAN_MAX_EVENTS_PER_RUN}" \
      PM_SCAN_MAX_MATCHED_PER_RUN="${PM_MODEL_SCAN_MAX_MATCHED_PER_RUN}" \
      PYTHONUNBUFFERED=1 \
      "$PY" "$ROOT/scan_weather_market.py" \
      --scan-weather-today \
      --tz "America/New_York" \
      $DAYS_ARG \
      $UPDATE_MODELS_ARG \
      --model-refresh-only \
      $MODEL_CACHE_ONLY_ARG \
      --no-auto-trade \
      --ensemble-models "$PM_SCAN_ENSEMBLE_MODELS" \
      --depth-levels "$PM_SCAN_DEPTH_LEVELS" \
      --write-scope "${PM_ARCHIVE_WRITE_SCOPE}" \
      $SCAN_AUTO_DIAG_ARG \
      $INSECURE_ARG
  scan_code="$?"
  set -e
  if [ "$scan_code" -eq 0 ]; then
    final_scan_code=0
    break
  fi
  echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] failed exit=$scan_code attempt=$attempt/$((PM_SCAN_RETRY_MAX + 1))" >> "$LOG"
  if [ "$attempt" -gt "$PM_SCAN_RETRY_MAX" ]; then
    final_scan_code="$scan_code"
    break
  fi
  sleep "$PM_SCAN_RETRY_SLEEP_SEC"
done

: "${PM_FACTS_MATERIALIZE_AFTER_SCAN:=0}"
if [ "${final_scan_code:-0}" -ne 0 ] && [ "${PM_MODEL_SCAN_FACTS_AFTER_FAILED_SCAN:-0}" != "1" ]; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] skip facts_materialize after failed scan rc=${final_scan_code}" >> "$LOG"
  PM_FACTS_MATERIALIZE_AFTER_SCAN=0
fi
live_ssot_reason="$(_model_scan_live_ssot_critical_reason 2>/dev/null || true)"
if [ -n "${live_ssot_reason:-}" ]; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] skip facts_materialize after scan for live_ssot live=${live_ssot_reason}" >> "$LOG"
  PM_FACTS_MATERIALIZE_AFTER_SCAN=0
fi
if weather_refresh_facts_materialize_allowed "model_scan" "$LOG" "model_scan"; then
  RUN_DATE="$(TZ=America/New_York date '+%Y-%m-%d')"
  : "${PM_MODEL_SCAN_RELEASE_LANE_BEFORE_FACTS:=0}"
  if [ "${PM_MODEL_SCAN_RELEASE_LANE_BEFORE_FACTS}" = "1" ]; then
    weather_scan_lane_release
  fi
  # Model scan already looks ahead (for example --days-ahead 2).  Facts must
  # be materialized over the same horizon, otherwise newly discovered future
  # markets get modeled but never enter the current auto-trade run_date.
  : "${PM_MODEL_SCAN_FACTS_CURRENT_RUN_ONLY:=0}"
  if [ "${PM_MODEL_SCAN_FACTS_CURRENT_RUN_ONLY}" = "1" ]; then
    FACTS_END_DATE="$RUN_DATE"
    FACTS_DAYS=1
  else
    facts_window="$(_facts_materialize_window_from_scan "$RUN_DATE" "${DAYS_ARG:-}" "${PM_MODEL_SCAN_FACTS_INCLUDE_YESTERDAY:-0}")"
    FACTS_END_DATE="$(printf '%s\n' "$facts_window" | sed -n '1p')"
    FACTS_DAYS="$(printf '%s\n' "$facts_window" | sed -n '2p')"
    if [ -z "${FACTS_END_DATE:-}" ] || ! [[ "${FACTS_DAYS:-}" =~ ^[0-9]+$ ]]; then
      FACTS_END_DATE="$RUN_DATE"
      FACTS_DAYS=1
    fi
  fi
  PM_MODEL_SCAN_FACTS_MATERIALIZE_TIMEOUT_SEC="$(normalize_positive_int_max "${PM_MODEL_SCAN_FACTS_MATERIALIZE_TIMEOUT_SEC:-90}" 90 180)"
  echo "---- $(date '+%Y-%m-%d %H:%M:%S') facts_materialize start (run_date=${RUN_DATE} end=${FACTS_END_DATE} days=${FACTS_DAYS} timeout=${PM_MODEL_SCAN_FACTS_MATERIALIZE_TIMEOUT_SEC}s current_run_only=${PM_MODEL_SCAN_FACTS_CURRENT_RUN_ONLY}) ----" >> "$LOG"
  facts_env=(
    env
    PM_FACTS_MATERIALIZE_CURRENT_RUN_ONLY="${PM_MODEL_SCAN_FACTS_CURRENT_RUN_ONLY}"
  )
  if [ "${PM_MODEL_SCAN_FACTS_SNAPSHOT_SINCE_SCAN:-1}" = "1" ] && [ -n "${SCAN_START_UTC:-}" ]; then
    facts_env+=(PM_FACTS_MATERIALIZE_SNAPSHOT_SINCE="${SCAN_START_UTC}")
  fi
  set +e
  run_with_timeout "$PM_MODEL_SCAN_FACTS_MATERIALIZE_TIMEOUT_SEC" "$LOG" \
    "${facts_env[@]}" "$PY" "$ROOT/ui/server.py" --materialize-facts --facts-date "$FACTS_END_DATE" --facts-days "$FACTS_DAYS" --write-scope "${PM_ARCHIVE_WRITE_SCOPE}"
  facts_rc="$?"
  set -e
  if [ "$facts_rc" -ne 0 ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] facts_materialize failed rc=${facts_rc}" >> "$LOG"
  fi
fi

PM_TRADE_SYNC_AFTER_SCAN="${PM_TRADE_SYNC_AFTER_SCAN:-0}"
if [ "${PM_MODEL_SCAN_ALLOW_TRADE_SYNC_AFTER_SCAN:-0}" != "1" ]; then
  PM_TRADE_SYNC_AFTER_SCAN=0
fi
if [ "${PM_TRADE_SYNC_AFTER_SCAN}" = "1" ]; then
  if pgrep -f "scan_weather_market.py --trade-sync-loop" >/dev/null 2>&1; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] trade_sync loop running, skip after-scan trade_sync" >> "$LOG"
  else
    echo "---- $(date '+%Y-%m-%d %H:%M:%S') trade_sync start ----" >> "$LOG"
    RUN_DATE="$(TZ=America/New_York date '+%Y-%m-%d')"
    PYTHONUNBUFFERED=1 "$PY" "$ROOT/scan_weather_market.py" \
      --trade-sync-only \
      --trade-sync-date "$RUN_DATE" \
      >> "$LOG" 2>&1 &
  fi
fi

: "${PM_MODEL_SCAN_STRICT_EXIT:=0}"
if [ "${final_scan_code:-0}" -ne 0 ]; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan] scan failed but facts materialize was attempted final_exit=${final_scan_code} strict=${PM_MODEL_SCAN_STRICT_EXIT}" >> "$LOG"
  if [ "${PM_MODEL_SCAN_STRICT_EXIT}" = "1" ]; then
    exit "${final_scan_code}"
  fi
fi
