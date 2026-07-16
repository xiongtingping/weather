#!/bin/bash
set -euo pipefail

ROOT="/Users/xiong/weather"
cd "$ROOT"

# 加载环境变量
if [ -f "$ROOT/scripts/lib/load_launchctl_env.sh" ]; then
  . "$ROOT/scripts/lib/load_launchctl_env.sh"
  load_repo_env_file "$ROOT/.env"
  load_polymarket_launchctl_env
fi

. "$ROOT/scripts/lib/run_with_timeout.sh"
. "$ROOT/scripts/lib/process_governance.sh"
. "$ROOT/scripts/lib/resolve_project_python.sh"
. "$ROOT/scripts/lib/weather_refresh_coordinator.sh"

LOG="$ROOT/data/model_scan_hourly.log"
if ! weather_refresh_register_entrypoint "model_scan_hourly" "model" "${PM_MODEL_SCAN_LANE_NAME:-${PM_REFRESH_COORDINATOR_LANE_NAME:-weather_refresh_lane}}" "$LOG" "model_scan_hourly"; then
  exit 0
fi
export PM_ARCHIVE_WRITE_SCOPE="${PM_ARCHIVE_WRITE_SCOPE:-chengdu_zuuu}"
PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
  PY="$(resolve_project_python "$ROOT" || true)"
fi
if [ -z "${PY:-}" ] || [ ! -x "$PY" ]; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan_hourly] missing project python runtime" >> "$LOG"
  exit 1
fi
if weather_refresh_skip_if_live_ssot "model_scan_hourly" "$LOG" "model_scan_hourly"; then
  exit 0
fi

_append_reason() {
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

_model_scan_hourly_live_ssot_critical_reason() {
  weather_refresh_live_ssot_critical_reason "model_scan_hourly"
}

_facts_materialize_window_from_scan() {
  local run_date="${1:-}"
  local days_ahead="${2:-2}"
  local include_yesterday="${3:-0}"
  "$PY" - "$ROOT/data/archive.db" "$run_date" "$days_ahead" "$include_yesterday" <<'PY'
import sqlite3
import sys
from datetime import date, timedelta

db_path, run_date_raw, days_ahead_raw, include_yesterday_raw = sys.argv[1:5]
run_date_text = str(run_date_raw or "").strip().split("T", 1)[0]
try:
    run_day = date.fromisoformat(run_date_text)
except Exception:
    print("")
    print("1")
    raise SystemExit(0)

try:
    days_ahead = int(str(days_ahead_raw or "2").strip())
except Exception:
    days_ahead = 2
include_yesterday = str(include_yesterday_raw or "").strip().lower() in {"1", "true", "yes", "on"}

if days_ahead >= 0:
    end_day = run_day + timedelta(days=max(0, days_ahead))
else:
    end_day = run_day
    try:
        conn = sqlite3.connect(db_path, timeout=1.0)
        cur = conn.cursor()
        row = cur.execute(
            """
            SELECT MAX(event_date)
            FROM (
              SELECT event_date FROM weather_market_discovery WHERE event_date >= ?
              UNION ALL
              SELECT event_date FROM model_snapshots WHERE run_date = ?
              UNION ALL
              SELECT event_date FROM market_snapshots WHERE run_date = ?
              UNION ALL
              SELECT event_date FROM event_runs WHERE run_date = ?
            )
            """,
            (run_date_text, run_date_text, run_date_text, run_date_text),
        ).fetchone()
        candidate = str((row[0] if row else "") or "").strip().split("T", 1)[0]
        if candidate:
            candidate_day = date.fromisoformat(candidate)
            if candidate_day > end_day:
                end_day = candidate_day
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass

start_day = run_day - timedelta(days=1) if include_yesterday else run_day
if end_day < start_day:
    end_day = start_day
print(end_day.isoformat())
print(max(1, (end_day - start_day).days + 1))
PY
}

live_ssot_reason="$(_model_scan_hourly_live_ssot_critical_reason 2>/dev/null || true)"
if [ -n "${live_ssot_reason:-}" ]; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan_hourly] live_ssot critical, skip hourly scan live=${live_ssot_reason}" >> "$LOG"
  exit 0
fi

if ! weather_scan_lane_acquire "model_scan_hourly" 0 "$LOG" "model_scan_hourly"; then
  exit 0
fi
cleanup_hourly() {
  weather_scan_lane_release
}
trap cleanup_hourly EXIT

# Do not hold the archive writer lock around the network/model scan.  The
# Python runtime acquires the shared queue only for each writable SQLite
# connection.  A parent-shell lock here is both far too broad and cannot be
# re-entered by the child process, which would deadlock the scan and block the
# latency-critical AMSC probability lane.

echo "---- $(date '+%Y-%m-%d %H:%M:%S') model_scan_hourly start ----" >> "$LOG"

# Future coverage sweep: model/fact all discovered current-run future markets in bounded batches.
MODEL_SCAN_HOURLY_TIMEOUT_SEC="$(normalize_positive_int_max "${PM_MODEL_SCAN_HOURLY_TIMEOUT_SEC:-900}" 900 1800)"
MODEL_SCAN_HOURLY_NICE="$(normalize_nice_level "${PM_MODEL_SCAN_HOURLY_NICE:-15}" 15)"
PM_MODEL_SCAN_HOURLY_MAX_EVENTS_PER_RUN="$(normalize_positive_int_max "${PM_MODEL_SCAN_HOURLY_MAX_EVENTS_PER_RUN:-24}" 24 60)"
PM_MODEL_SCAN_HOURLY_MAX_MATCHED_PER_RUN="$(normalize_positive_int_max "${PM_MODEL_SCAN_HOURLY_MAX_MATCHED_PER_RUN:-${PM_MODEL_SCAN_HOURLY_MAX_EVENTS_PER_RUN}}" "${PM_MODEL_SCAN_HOURLY_MAX_EVENTS_PER_RUN}" "${PM_MODEL_SCAN_HOURLY_MAX_EVENTS_PER_RUN}")"
PM_MODEL_SCAN_COMPOSE_MARKET_BOOTSTRAP="${PM_MODEL_SCAN_COMPOSE_MARKET_BOOTSTRAP:-0}"
PM_MODEL_SCAN_HOURLY_DAYS_AHEAD="${PM_MODEL_SCAN_HOURLY_DAYS_AHEAD:-2}"
if ! [[ "${PM_MODEL_SCAN_HOURLY_DAYS_AHEAD}" =~ ^-?[0-9]+$ ]]; then
  PM_MODEL_SCAN_HOURLY_DAYS_AHEAD=2
fi
if [ "${PM_MODEL_SCAN_HOURLY_DAYS_AHEAD}" -lt -1 ]; then
  PM_MODEL_SCAN_HOURLY_DAYS_AHEAD=-1
fi
if [ "${PM_MODEL_SCAN_HOURLY_DAYS_AHEAD}" -gt 7 ]; then
  PM_MODEL_SCAN_HOURLY_DAYS_AHEAD=7
fi
SCAN_START_UTC="$(date -u '+%Y-%m-%dT%H:%M:%S+00:00')"
set +e
run_with_timeout "$MODEL_SCAN_HOURLY_TIMEOUT_SEC" "$LOG" nice -n "$MODEL_SCAN_HOURLY_NICE" env \
  PM_SCAN_MAX_EVENTS_PER_RUN="$PM_MODEL_SCAN_HOURLY_MAX_EVENTS_PER_RUN" \
  PM_SCAN_MAX_MATCHED_PER_RUN="$PM_MODEL_SCAN_HOURLY_MAX_MATCHED_PER_RUN" \
  PM_SCAN_PRIORITY_ENABLED=1 \
  PM_SCAN_PRIORITY_LOOKAHEAD_DAYS="$PM_MODEL_SCAN_HOURLY_DAYS_AHEAD" \
  PM_SCAN_PRIORITY_INCLUDE_NEW_MARKETS=1 \
  PM_SCAN_PRIORITY_NEW_MARKET_TOPN="$PM_MODEL_SCAN_HOURLY_MAX_EVENTS_PER_RUN" \
  PM_SCAN_PRIORITY_INCLUDE_FUTURE_MODEL_MISSING=1 \
  PM_SCAN_PRIORITY_FUTURE_MODEL_MISSING_TOPN="$PM_MODEL_SCAN_HOURLY_MAX_EVENTS_PER_RUN" \
  PM_SCAN_PRIORITY_INCLUDE_Q_MISSING=1 \
  PM_SCAN_PRIORITY_Q_MISSING_TOPN="$PM_MODEL_SCAN_HOURLY_MAX_EVENTS_PER_RUN" \
  PM_SCAN_PRIORITY_CANDIDATES_PER_RUN=0 \
  PM_SCAN_PRIORITY_OTHERS_PER_RUN=0 \
  PM_MODEL_SCAN_COMPOSE_MARKET_BOOTSTRAP="$PM_MODEL_SCAN_COMPOSE_MARKET_BOOTSTRAP" \
  PYTHONUNBUFFERED=1 \
  "$PY" "$ROOT/scan_weather_market.py" \
  --scan-weather-today \
  --days-ahead "$PM_MODEL_SCAN_HOURLY_DAYS_AHEAD" \
  --model-refresh-only \
  --ensemble-models "${PM_SCAN_ENSEMBLE_MODELS:-gfs_seamless,icon_seamless,ecmwf_ifs025,nws_forecast}" \
  --write-scope "${PM_ARCHIVE_WRITE_SCOPE}" \
  --depth-levels 5 \
  >> "$LOG" 2>&1

exit_code=$?
set -e
if [ "$exit_code" -ne 0 ] && [ "${PM_MODEL_SCAN_HOURLY_FACTS_AFTER_FAILED_SCAN:-0}" != "1" ]; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan_hourly] skip facts_materialize after failed scan rc=$exit_code" >> "$LOG"
  echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan_hourly] completed exit=$exit_code" >> "$LOG"
  exit $exit_code
fi
if [ "${PM_MODEL_SCAN_HOURLY_FACTS_MATERIALIZE_ENABLED:-0}" != "1" ]; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan_hourly] facts_materialize disabled; live SSOT owns fact writes" >> "$LOG"
  echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan_hourly] completed exit=$exit_code" >> "$LOG"
  exit $exit_code
fi
live_ssot_reason="$(_model_scan_hourly_live_ssot_critical_reason 2>/dev/null || true)"
if [ -n "${live_ssot_reason:-}" ]; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan_hourly] skip facts_materialize for live_ssot live=${live_ssot_reason}" >> "$LOG"
  echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan_hourly] completed exit=$exit_code" >> "$LOG"
  exit $exit_code
fi
RUN_DATE="$(TZ=America/New_York date '+%Y-%m-%d')"
facts_window="$(_facts_materialize_window_from_scan "$RUN_DATE" "$PM_MODEL_SCAN_HOURLY_DAYS_AHEAD" "${PM_MODEL_SCAN_HOURLY_FACTS_INCLUDE_YESTERDAY:-0}")"
FACTS_END_DATE="$(printf '%s\n' "$facts_window" | sed -n '1p')"
FACTS_DAYS="$(printf '%s\n' "$facts_window" | sed -n '2p')"
if [ -z "${FACTS_END_DATE:-}" ] || ! [[ "${FACTS_DAYS:-}" =~ ^[0-9]+$ ]]; then
  FACTS_END_DATE="$RUN_DATE"
  FACTS_DAYS=1
fi
PM_MODEL_SCAN_HOURLY_FACTS_MATERIALIZE_TIMEOUT_SEC="$(normalize_positive_int_max "${PM_MODEL_SCAN_HOURLY_FACTS_MATERIALIZE_TIMEOUT_SEC:-180}" 180 360)"
echo "---- $(date '+%Y-%m-%d %H:%M:%S') model_scan_hourly facts_materialize start (run_date=${RUN_DATE} end=${FACTS_END_DATE} days=${FACTS_DAYS}) ----" >> "$LOG"
set +e
run_with_timeout "$PM_MODEL_SCAN_HOURLY_FACTS_MATERIALIZE_TIMEOUT_SEC" "$LOG" \
  env \
    PM_FACTS_MATERIALIZE_CURRENT_RUN_ONLY=0 \
    PM_FACTS_MATERIALIZE_SNAPSHOT_SINCE="$SCAN_START_UTC" \
    "$PY" "$ROOT/ui/server.py" --materialize-facts --facts-date "$FACTS_END_DATE" --facts-days "$FACTS_DAYS" --write-scope "${PM_ARCHIVE_WRITE_SCOPE}" \
  >> "$LOG" 2>&1
facts_rc=$?
set -e
if [ "$facts_rc" -ne 0 ]; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan_hourly] facts_materialize failed rc=$facts_rc" >> "$LOG"
fi
echo "$(date '+%Y-%m-%d %H:%M:%S') [model_scan_hourly] completed exit=$exit_code" >> "$LOG"
exit $exit_code
