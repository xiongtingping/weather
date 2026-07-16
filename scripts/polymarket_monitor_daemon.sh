#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LIVE_PAUSE_FILE="${PM_LIVE_PAUSE_FILE:-$ROOT/data/live_paused.flag}"
if [ -f "$LIVE_PAUSE_FILE" ]; then
  mkdir -p "$ROOT/data/logs"
  printf '[polymarket-monitor] live paused by %s at %s\n' "$LIVE_PAUSE_FILE" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$ROOT/data/logs/live_pause.log"
  exec sleep "${PM_LIVE_PAUSE_SLEEP_SEC:-86400}"
fi

if [ -f "$ROOT/scripts/lib/load_launchctl_env.sh" ]; then
  # shellcheck disable=SC1091
  source "$ROOT/scripts/lib/load_launchctl_env.sh"
  load_repo_env_file "$ROOT/.env"
  load_polymarket_launchctl_env
fi

PY="${PYTHON:-$ROOT/.venv/bin/python}"
if [ ! -x "$PY" ]; then
  PY="${PYTHON:-python3}"
fi

WATCHER_ENABLED="${PM_MONITOR_WATCHER_ENABLED:-1}"
ANOMALY_ENABLED="${PM_CHENGDU_ANOMALY_ENABLED:-1}"
HARD_SOURCE_DIRECT_ENABLED="${PM_HARD_SOURCE_DIRECT_ENABLED:-1}"
ARCHIVE_WAL_KEEPER_ENABLED="${PM_ARCHIVE_WAL_KEEPER_ENABLED:-1}"
ARCHIVE_WAL_KEEPER_DB="${PM_ARCHIVE_WAL_KEEPER_DB:-$ROOT/data/archive.db}"
ARCHIVE_WAL_KEEPER_STATUS_PATH="${PM_ARCHIVE_WAL_KEEPER_STATUS_PATH:-$ROOT/data/archive_wal_keeper_status_latest.json}"
ARCHIVE_WAL_KEEPER_HEARTBEAT_SEC="${PM_ARCHIVE_WAL_KEEPER_HEARTBEAT_SEC:-5}"
AMSC_ZUUU_POLL_ENABLED="${PM_AMSC_ZUUU_POLL_ENABLED:-1}"
AMSC_ZUUU_POLL_SLOT_START_WINDOW_SEC="${PM_AMSC_ZUUU_POLL_SLOT_START_WINDOW_SEC:-55}"
AMSC_ZUUU_POLL_SLOT_COMPENSATION_SEC="${PM_AMSC_ZUUU_POLL_SLOT_COMPENSATION_SEC:-480}"
AMSC_ZUUU_SCHEDULER_STATUS_PATH="${AMSC_ZUUU_SCHEDULER_STATUS_PATH:-$ROOT/data/amsc_zuuu_metar_taf/scheduler_status_latest.json}"
AMSC_ZUUU_SPECI_WATCH_ENABLED="${PM_AMSC_ZUUU_SPECI_WATCH_ENABLED:-1}"
AMSC_ZUUU_SPECI_WATCH_INTERVAL_SEC="${PM_AMSC_ZUUU_SPECI_WATCH_INTERVAL_SEC:-60}"
AMSC_ZUUU_SPECI_WATCH_REPORT_COUNT="${PM_AMSC_ZUUU_SPECI_WATCH_REPORT_COUNT:-24}"
AMSC_ZUUU_SPECI_WATCH_TIMEOUT_SEC="${PM_AMSC_ZUUU_SPECI_WATCH_TIMEOUT_SEC:-8}"
AMSC_ZUUU_SPECI_WATCH_MAX_REPORT_AGE_SEC="${PM_AMSC_ZUUU_SPECI_WATCH_MAX_REPORT_AGE_SEC:-900}"
AMSC_ZUUU_SPECI_WATCH_STATE_PATH="${PM_AMSC_ZUUU_SPECI_WATCH_STATE_PATH:-$ROOT/data/amsc_zuuu_metar_taf/speci_watch_latest.json}"
AMSC_ZUUU_SPECI_PRODUCTION_PROBABILITY_INGRESS_ENABLED="${PM_AMSC_ZUUU_SPECI_PRODUCTION_PROBABILITY_INGRESS_ENABLED:-1}"
AMSC_ZUUU_PRODUCTION_PROBABILITY_ACTIVATION_PATH="${PM_AMSC_ZUUU_PRODUCTION_PROBABILITY_ACTIVATION_PATH:-$ROOT/config/production_probability_runtime_activation.json}"
AMSC_ZUUU_SPECI_REALTIME_DB="${PM_AMSC_ZUUU_SPECI_REALTIME_DB:-$ROOT/data/archive.db}"
AMSC_ZUUU_RAW_ARCHIVE_DIR="${AMSC_ZUUU_RAW_ARCHIVE_DIR:-$ROOT/data/amsc_raw_history}"
AMSC_P10_SHADOW_SOURCE_ENABLED="${PM_AMSC_P10_SHADOW_SOURCE_ENABLED:-1}"
AMSC_P10_SHADOW_SOURCE_SLOT_START_WINDOW_SEC="${PM_AMSC_P10_SHADOW_SOURCE_SLOT_START_WINDOW_SEC:-55}"
AMSC_P10_RESIDENT_CONSUMER_ENABLED="${PM_AMSC_P10_RESIDENT_CONSUMER_ENABLED:-1}"
AMSC_P10_SHADOW_DB="${PM_AMSC_P10_SHADOW_DB:-$ROOT/data/shadow/p10_runtime.db}"
AMSC_P10_INBOX_DIR="${PM_AMSC_P10_INBOX_DIR:-$ROOT/data/shadow/p10_amsc_inbox}"
AMSC_P10_RAW_ARCHIVE_DIR="${PM_AMSC_P10_RAW_ARCHIVE_DIR:-$ROOT/data/shadow/p10_amsc_raw}"
P10_NEXT_DAY_PREPARATION_ENABLED="${PM_P10_NEXT_DAY_PREPARATION_ENABLED:-1}"
P10_NEXT_DAY_PREPARATION_INTERVAL="${PM_P10_NEXT_DAY_PREPARATION_INTERVAL_SEC:-300}"
P10_MONITOR_RUNTIME_STATUS_PATH="${PM_P10_MONITOR_RUNTIME_STATUS_PATH:-$ROOT/data/shadow/p10_monitor_runtime_status_latest.json}"
P10_MONITOR_RUNTIME_GENERATION="ssot_probability_mainline_v1"
RESOLUTION_FINAL_SHADOW_CAPTURE_ENABLED="${PM_RESOLUTION_FINAL_SHADOW_CAPTURE_ENABLED:-1}"
RESOLUTION_FINAL_SHADOW_CAPTURE_INTERVAL="${PM_RESOLUTION_FINAL_SHADOW_CAPTURE_INTERVAL_SEC:-300}"
STATION_DAY_WEAK_PHYSICAL_MATERIALIZATION_ENABLED="${PM_STATION_DAY_WEAK_PHYSICAL_MATERIALIZATION_ENABLED:-1}"
JOINT_WEAK_BIAS_RESEARCH_PIPELINE_ENABLED="${PM_JOINT_WEAK_BIAS_RESEARCH_PIPELINE_ENABLED:-1}"
STATION_DAY_STRONG_PHYSICAL_MATERIALIZATION_ENABLED="${PM_STATION_DAY_STRONG_PHYSICAL_MATERIALIZATION_ENABLED:-1}"
OFFICIAL_NWP_CURVE_COLLECTION_ENABLED="${PM_OFFICIAL_NWP_CURVE_COLLECTION_ENABLED:-0}"
OFFICIAL_NWP_CURVE_COLLECTION_INTERVAL="${PM_OFFICIAL_NWP_CURVE_COLLECTION_INTERVAL_SEC:-3600}"
OFFICIAL_NWP_DAILY_PEAK_P1_INGRESS_ENABLED="${PM_OFFICIAL_NWP_DAILY_PEAK_P1_INGRESS_ENABLED:-0}"
OFFICIAL_NWP_LIVE_SNAPSHOT_COLLECTION_ENABLED="${PM_OFFICIAL_NWP_LIVE_SNAPSHOT_COLLECTION_ENABLED:-0}"
OFFICIAL_NWP_LIVE_SNAPSHOT_COLLECTION_INTERVAL="${PM_OFFICIAL_NWP_LIVE_SNAPSHOT_COLLECTION_INTERVAL_SEC:-900}"
WATCHER_INTERVAL="${PM_WATCH_INTERVAL_SEC:-10}"
WATCHER_SMS_ENABLED="${PM_AUTO_TRADE_WATCHER_SMS_ENABLED:-1}"
ANOMALY_INTERVAL="${PM_CHENGDU_ANOMALY_INTERVAL_SEC:-300}"
CHILD_CHECK_INTERVAL="${PM_MONITOR_CHILD_CHECK_INTERVAL_SEC:-5}"
ONCE=0
NO_WATCHER=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --once)
      ONCE=1
      ;;
    --no-watcher)
      NO_WATCHER=1
      ;;
    *)
      echo "unknown arg: $1" >&2
      exit 2
      ;;
  esac
  shift
done

WATCHER_PID=""
ARCHIVE_WAL_KEEPER_PID=""
HARD_SOURCE_DIRECT_PID=""
AMSC_ZUUU_POLL_PID=""
AMSC_ZUUU_POLL_LAST_SLOT_KEY=""
AMSC_ZUUU_POLL_LAST_STATUS_SLOT_KEY=""
AMSC_ZUUU_SPECI_WATCH_PID=""
AMSC_ZUUU_SPECI_WATCH_LAST_START_EPOCH=0
AMSC_P10_SHADOW_SOURCE_PID=""
AMSC_P10_SHADOW_SOURCE_LAST_SLOT_KEY=""
AMSC_P10_RESIDENT_CONSUMER_PID=""
AMSC_P10_RESIDENT_EVENT_DATE=""
AMSC_P10_RESIDENT_CYCLE_ID=""
P10_NEXT_DAY_PREPARATION_PID=""
P10_NEXT_DAY_PREPARATION_LAST_START_EPOCH=0
RESOLUTION_FINAL_SHADOW_CAPTURE_PID=""
RESOLUTION_FINAL_SHADOW_CAPTURE_LAST_START_EPOCH=0
OFFICIAL_NWP_CURVE_COLLECTION_PID=""
OFFICIAL_NWP_CURVE_COLLECTION_LAST_START_EPOCH=0
OFFICIAL_NWP_LIVE_SNAPSHOT_COLLECTION_PID=""
OFFICIAL_NWP_LIVE_SNAPSHOT_COLLECTION_LAST_START_EPOCH=0

log() {
  printf '[polymarket-monitor] %s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

write_p10_monitor_runtime_status() {
  "$PY" - "$P10_MONITOR_RUNTIME_STATUS_PATH" "$P10_MONITOR_RUNTIME_GENERATION" "$1" "$$" <<'PY'
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

path = Path(sys.argv[1]).expanduser().resolve()
payload = {
    "schema": "p10_monitor_runtime_status_v1",
    "runtime_generation": sys.argv[2],
    "state": sys.argv[3],
    "monitor_pid": int(sys.argv[4]),
    "observed_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    "monitor_domain": "system/com.weather.monitor.daemon",
    "resident_consumer_managed": True,
    "next_day_preparation_managed": True,
    "shadow_only": True,
    "places_orders": False,
}
path.parent.mkdir(parents=True, exist_ok=True)
temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
os.replace(temporary, path)
PY
}

positive_int_arg() {
  "$PY" - "$1" "$2" <<'PY'
import sys

try:
    value = int(float(str(sys.argv[1]).strip()))
except Exception:
    value = int(float(str(sys.argv[2]).strip()))
print(max(1, value))
PY
}

run_chengdu_anomaly() {
  if [ "$ANOMALY_ENABLED" != "1" ]; then
    return 0
  fi
  local anomaly_pid check state rc
  "$ROOT/scripts/chengdu_weather_anomaly_once.sh" &
  anomaly_pid="$!"
  check="$(positive_int_arg "$CHILD_CHECK_INTERVAL" 5)"
  while kill -0 "$anomaly_pid" 2>/dev/null; do
    state="$(ps -p "$anomaly_pid" -o state= 2>/dev/null | tr -d '[:space:]')"
    if [ -z "$state" ] || [ "$state" = "Z" ]; then
      break
    fi
    start_watcher
    start_archive_wal_keeper
    start_hard_source_direct
    start_amsc_zuuu_speci_watch
    start_amsc_zuuu_poll
    start_amsc_p10_shadow_source_capture
    start_resolution_final_shadow_capture
    sleep "$check"
  done
  rc=0
  wait "$anomaly_pid" 2>/dev/null || rc="$?"
  return "$rc"
}

chengdu_anomaly_sleep_interval() {
  if [ "$ANOMALY_ENABLED" != "1" ]; then
    printf '%s\n' "$ANOMALY_INTERVAL"
    return 0
  fi
  "$PY" - "$ROOT" "$ANOMALY_INTERVAL" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
try:
    fallback = int(float(sys.argv[2]))
except Exception:
    fallback = 300

try:
    sys.path.insert(0, str(root))
    from chengdu_intraday_summary import build_chengdu_intraday_summary

    summary = build_chengdu_intraday_summary(root / "data" / "archive.db")
    cadence = summary.get("refresh_cadence") if isinstance(summary, dict) else {}
    interval = int(float(cadence.get("recommended_interval_sec"))) if isinstance(cadence, dict) else fallback
except Exception:
    interval = fallback

print(max(30, min(3600, interval)))
PY
}

start_watcher() {
  if [ "$WATCHER_ENABLED" != "1" ] || [ "$NO_WATCHER" = "1" ]; then
    return 0
  fi
  if [ -n "${WATCHER_PID:-}" ] && kill -0 "$WATCHER_PID" 2>/dev/null; then
    return 0
  fi
  log "starting auto_trade_watcher interval=${WATCHER_INTERVAL}"
  WATCHER_SMS_ARGS=()
  if [ "$WATCHER_SMS_ENABLED" = "0" ]; then
    WATCHER_SMS_ARGS=(--no-sms)
  elif [ "$WATCHER_SMS_ENABLED" = "1" ]; then
    WATCHER_SMS_ARGS=(--sms)
  fi
  "$PY" "$ROOT/scripts/auto_trade_watcher.py" --interval "$WATCHER_INTERVAL" "${WATCHER_SMS_ARGS[@]}" &
  WATCHER_PID="$!"
}

start_archive_wal_keeper() {
  if [ "$ARCHIVE_WAL_KEEPER_ENABLED" != "1" ]; then
    return 0
  fi
  if [ -n "${ARCHIVE_WAL_KEEPER_PID:-}" ] && kill -0 "$ARCHIVE_WAL_KEEPER_PID" 2>/dev/null; then
    return 0
  fi
  # The system-domain monitor is the sole supervisor. Retire a proven orphan
  # before opening the replacement read-only connection.
  while IFS= read -r stale_pid; do
    if [ -n "$stale_pid" ] && [ "$stale_pid" != "$$" ]; then
      kill "$stale_pid" 2>/dev/null || true
      wait "$stale_pid" 2>/dev/null || true
    fi
  done < <(pgrep -f "$ROOT/scripts/archive_db_wal_keeper.py --db $ARCHIVE_WAL_KEEPER_DB" 2>/dev/null || true)
  log "starting archive_db_wal_keeper read_only=1"
  "$PY" "$ROOT/scripts/archive_db_wal_keeper.py" \
    --db "$ARCHIVE_WAL_KEEPER_DB" \
    --status "$ARCHIVE_WAL_KEEPER_STATUS_PATH" \
    --heartbeat-sec "$ARCHIVE_WAL_KEEPER_HEARTBEAT_SEC" \
    >> "$ROOT/data/logs/archive_db_wal_keeper.out.log" \
    2>> "$ROOT/data/logs/archive_db_wal_keeper.err.log" &
  ARCHIVE_WAL_KEEPER_PID="$!"
}

start_hard_source_direct() {
  if [ "$HARD_SOURCE_DIRECT_ENABLED" != "1" ]; then
    return 0
  fi
  if [ -n "${HARD_SOURCE_DIRECT_PID:-}" ] && kill -0 "$HARD_SOURCE_DIRECT_PID" 2>/dev/null; then
    return 0
  fi
  # launchd can kill this wrapper while leaving older poller grandchildren
  # re-parented to pid 1. Enforce a single loop before starting a fresh one.
  while IFS= read -r stale_pid; do
    if [ -n "$stale_pid" ] && [ "$stale_pid" != "$$" ]; then
      log "stopping stale hard_source_direct_poller pid=${stale_pid}"
      kill "$stale_pid" 2>/dev/null || true
      sleep 1
      if kill -0 "$stale_pid" 2>/dev/null; then
        log "force stopping stale hard_source_direct_poller pid=${stale_pid}"
        kill -9 "$stale_pid" 2>/dev/null || true
      fi
    fi
  done < <(pgrep -f "$ROOT/scripts/hard_source_direct_poller.py --loop" 2>/dev/null || true)
  log "starting hard_source_direct_poller awc_acquire_window=${PM_HARD_SOURCE_AWC_RELEASE_WATCH_ENABLED:-1}/start${PM_HARD_SOURCE_AWC_ACQUIRE_WINDOW_START_MIN:-5}m/duration${PM_HARD_SOURCE_AWC_ACQUIRE_WINDOW_DURATION_SEC:-120}s/poll${PM_HARD_SOURCE_AWC_RELEASE_WATCH_POLL_SEC:-1}s/hold${PM_HARD_SOURCE_AWC_ACQUIRE_HOLD_NON_AWC_UNTIL_TARGET:-1} awc_dynamic_hours=${PM_HARD_SOURCE_AWC_DYNAMIC_HOURS_ENABLED:-1}/min${PM_HARD_SOURCE_AWC_DYNAMIC_MIN_HOURS:-8}/margin${PM_HARD_SOURCE_AWC_DYNAMIC_MARGIN_HOURS:-2} awc_recent_window=${PM_AWC_FACT_REFRESH_RECENT_WINDOW_ENABLED:-1}/${PM_AWC_FACT_REFRESH_RECENT_WINDOW_HOURS:-4}h awc_timeout=${PM_BAYES_LIKELIHOOD_AWC_METAR_TIMEOUT_SEC:-2.0}s/retry${PM_BAYES_LIKELIHOOD_AWC_METAR_RETRIES:-1} metar_pws_enabled=${PM_HARD_SOURCE_METAR_PWS_ENABLED:-0} metar_pws_blocking=${PM_HARD_SOURCE_METAR_PWS_BLOCKING:-0} metar_pws_background=${PM_HARD_SOURCE_METAR_PWS_BACKGROUND_REFRESH:-0} iem_peak=${PM_HARD_SOURCE_METAR_IEM_PEAK_POLL_SEC:-30}s iem_normal=${PM_HARD_SOURCE_METAR_IEM_NORMAL_POLL_SEC:-300}s avwx_peak=${PM_HARD_SOURCE_METAR_AVWX_PEAK_POLL_SEC:-15}s avwx_normal=${PM_HARD_SOURCE_METAR_AVWX_NORMAL_POLL_SEC:-60}s checkwx_peak=${PM_HARD_SOURCE_METAR_CHECKWX_PEAK_POLL_SEC:-15}s checkwx_normal=${PM_HARD_SOURCE_METAR_CHECKWX_NORMAL_POLL_SEC:-60}s pws_peak=${PM_HARD_SOURCE_TWC_PWS_RAPID_PEAK_POLL_SEC:-10}s pws_normal=${PM_HARD_SOURCE_TWC_PWS_RAPID_NORMAL_POLL_SEC:-60}s"
  "$ROOT/scripts/hard_source_direct_poller.sh" &
  HARD_SOURCE_DIRECT_PID="$!"
}

start_amsc_zuuu_poll() {
  # AMSC is the primary hard-observation source. Its release-window poller is
  # supervised by this one monitor daemon, never a parallel GUI LaunchAgent.
  if [ "$AMSC_ZUUU_POLL_ENABLED" != "1" ]; then
    return 0
  fi
  local now_epoch slot_epoch slot_age slot_key slot_at window compensation prior_state
  now_epoch="$(date -u +%s)"
  slot_epoch="$((now_epoch - (now_epoch % 1800)))"
  slot_age="$((now_epoch - slot_epoch))"
  window="$(positive_int_arg "$AMSC_ZUUU_POLL_SLOT_START_WINDOW_SEC" 55)"
  compensation="$(positive_int_arg "$AMSC_ZUUU_POLL_SLOT_COMPENSATION_SEC" 480)"
  if [ "$compensation" -lt "$window" ]; then
    compensation="$window"
  fi
  slot_key="$(date -u -r "$slot_epoch" +%Y%m%d%H%M)"
  slot_at="$(date -u -r "$slot_epoch" '+%Y-%m-%dT%H:%M:%SZ')"
  if [ "$slot_age" -gt "$compensation" ]; then
    if [ "$slot_key" != "$AMSC_ZUUU_POLL_LAST_STATUS_SLOT_KEY" ]; then
      write_amsc_zuuu_scheduler_status "missed_current_slot_window" "slot_compensation_expired" "$slot_key" "$slot_at" "$slot_age"
      AMSC_ZUUU_POLL_LAST_STATUS_SLOT_KEY="$slot_key"
    fi
    return 0
  fi
  if [ -n "${AMSC_ZUUU_POLL_PID:-}" ] && kill -0 "$AMSC_ZUUU_POLL_PID" 2>/dev/null; then
    prior_state="$(ps -p "$AMSC_ZUUU_POLL_PID" -o state= 2>/dev/null | tr -d '[:space:]')"
    if [ "$prior_state" = "Z" ]; then
      wait "$AMSC_ZUUU_POLL_PID" 2>/dev/null || true
      AMSC_ZUUU_POLL_PID=""
    else
      if [ "$slot_key" != "$AMSC_ZUUU_POLL_LAST_STATUS_SLOT_KEY" ]; then
        write_amsc_zuuu_scheduler_status "deferred_current_slot" "previous_poll_running" "$slot_key" "$slot_at" "$slot_age" "$AMSC_ZUUU_POLL_PID"
        AMSC_ZUUU_POLL_LAST_STATUS_SLOT_KEY="$slot_key"
      fi
      return 0
    fi
  fi
  if [ "$slot_key" = "${AMSC_ZUUU_POLL_LAST_SLOT_KEY:-}" ]; then
    return 0
  fi
  AMSC_ZUUU_POLL_LAST_SLOT_KEY="$slot_key"
  write_amsc_zuuu_scheduler_status "started_current_slot" "${slot_age}s_after_boundary" "$slot_key" "$slot_at" "$slot_age"
  log "starting amsc_zuuu_metar_taf_poll slot=${slot_key} monitor_owned=1 slot_age=${slot_age}s"
  "$ROOT/scripts/run_amsc_zuuu_metar_taf_poll_immutable.sh" \
    >> "$ROOT/data/logs/amsc_zuuu_metar_taf_poll.out.log" \
    2>> "$ROOT/data/logs/amsc_zuuu_metar_taf_poll.err.log" &
  AMSC_ZUUU_POLL_PID="$!"
}

start_amsc_zuuu_speci_watch() {
  # Bounded AMSC-only real-time observation lane.  SPECI is raw-archived,
  # source-fact persisted, and directly refreshes the unique posterior.  It
  # never fills the routine-METAR final-X schedule, touches a generic queue,
  # writes bot signals, reads a market, or places an order.
  if [ "$AMSC_ZUUU_SPECI_WATCH_ENABLED" != "1" ]; then
    return 0
  fi
  if [ -n "${AMSC_ZUUU_SPECI_WATCH_PID:-}" ] && kill -0 "$AMSC_ZUUU_SPECI_WATCH_PID" 2>/dev/null; then
    return 0
  fi
  local now interval elapsed
  now="$(date +%s)"
  interval="$(positive_int_arg "$AMSC_ZUUU_SPECI_WATCH_INTERVAL_SEC" 60)"
  elapsed="$((now - AMSC_ZUUU_SPECI_WATCH_LAST_START_EPOCH))"
  if [ "$AMSC_ZUUU_SPECI_WATCH_LAST_START_EPOCH" -gt 0 ] && [ "$elapsed" -lt "$interval" ]; then
    return 0
  fi
  AMSC_ZUUU_SPECI_WATCH_LAST_START_EPOCH="$now"
  log "starting amsc_zuuu_speci_watch interval=${interval}s production_probability_ingress=${AMSC_ZUUU_SPECI_PRODUCTION_PROBABILITY_INGRESS_ENABLED}"
  "$PY" "$ROOT/scripts/watch_zuuu_speci_once.py" \
    --archive-dir "$AMSC_ZUUU_RAW_ARCHIVE_DIR" \
    --state-path "$AMSC_ZUUU_SPECI_WATCH_STATE_PATH" \
    --count "$AMSC_ZUUU_SPECI_WATCH_REPORT_COUNT" \
    --timeout "$AMSC_ZUUU_SPECI_WATCH_TIMEOUT_SEC" \
    --max-report-age-sec "$AMSC_ZUUU_SPECI_WATCH_MAX_REPORT_AGE_SEC" \
    --db "$AMSC_ZUUU_SPECI_REALTIME_DB" \
    $( [ "$AMSC_ZUUU_SPECI_PRODUCTION_PROBABILITY_INGRESS_ENABLED" = "1" ] && printf '%s' '--production-probability-ingress' ) \
    --production-activation "$AMSC_ZUUU_PRODUCTION_PROBABILITY_ACTIVATION_PATH" \
    >> "$ROOT/data/logs/amsc_zuuu_speci_watch.out.log" \
    2>> "$ROOT/data/logs/amsc_zuuu_speci_watch.err.log" &
  AMSC_ZUUU_SPECI_WATCH_PID="$!"
}

write_amsc_zuuu_scheduler_status() {
  local state="$1" reason="$2" slot_key="$3" slot_at="$4" slot_age="$5" prior_pid="${6:-}"
  "$PY" - "$AMSC_ZUUU_SCHEDULER_STATUS_PATH" "$state" "$reason" "$slot_key" "$slot_at" "$slot_age" "$prior_pid" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])
payload = {
    "schema": "amsc_zuuu_scheduler_status_v1",
    "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "state": sys.argv[2],
    "reason": sys.argv[3],
    "slot_key": sys.argv[4],
    "source_slot_expected_at": sys.argv[5],
    "slot_age_sec": int(sys.argv[6]),
    "previous_poll_pid": sys.argv[7],
    "historical_backfill": False,
}
path.parent.mkdir(parents=True, exist_ok=True)
tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
tmp.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
os.replace(tmp, path)
PY
}

start_amsc_p10_shadow_source_capture() {
  # P10 owns an independent raw/T1 archive, but its source process is still a
  # bounded child of this sole monitor daemon. It is not a LaunchAgent, queue,
  # posterior writer, or order executor.
  if [ "$AMSC_P10_SHADOW_SOURCE_ENABLED" != "1" ]; then
    return 0
  fi
  if [ -n "${AMSC_P10_SHADOW_SOURCE_PID:-}" ] && kill -0 "$AMSC_P10_SHADOW_SOURCE_PID" 2>/dev/null; then
    return 0
  fi
  local minute second slot_key window
  minute="$(date -u +%M)"
  second="$(date -u +%S)"
  window="$(positive_int_arg "$AMSC_P10_SHADOW_SOURCE_SLOT_START_WINDOW_SEC" 55)"
  if [ "$minute" != "00" ] && [ "$minute" != "30" ]; then
    return 0
  fi
  if [ "$second" -gt "$window" ]; then
    return 0
  fi
  slot_key="$(date -u +%Y%m%d%H%M)"
  if [ "$slot_key" = "${AMSC_P10_SHADOW_SOURCE_LAST_SLOT_KEY:-}" ]; then
    return 0
  fi
  AMSC_P10_SHADOW_SOURCE_LAST_SLOT_KEY="$slot_key"
  log "starting amsc_p10_shadow_source_capture slot=${slot_key} monitor_owned=1 shadow_only=1"
  "$PY" "$ROOT/scripts/run_amsc_p10_shadow_source_sidecar.py" \
    >> "$ROOT/data/logs/amsc_p10_shadow_source_sidecar.out.log" \
    2>> "$ROOT/data/logs/amsc_p10_shadow_source_sidecar.err.log" &
  AMSC_P10_SHADOW_SOURCE_PID="$!"
}

current_zuuu_local_date() {
  TZ=Asia/Shanghai date +%Y-%m-%d
}

resident_p10_cycle_for_date() {
  "$PY" - "$AMSC_P10_SHADOW_DB" "$1" <<'PY'
import sqlite3
import sys

path, event_date = sys.argv[1], sys.argv[2]
try:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1)
    row = conn.execute(
        "SELECT prepared_cycle_id FROM resident_p10_prepared_cycles_shadow "
        "WHERE event_date=? ORDER BY prepared_at DESC, rowid DESC LIMIT 1",
        (event_date,),
    ).fetchone()
except sqlite3.Error:
    row = None
finally:
    try:
        conn.close()
    except Exception:
        pass
print(row[0] if row else "")
PY
}

stop_amsc_p10_resident_consumer() {
  if [ -n "${AMSC_P10_RESIDENT_CONSUMER_PID:-}" ] \
    && kill -0 "$AMSC_P10_RESIDENT_CONSUMER_PID" 2>/dev/null; then
    kill "$AMSC_P10_RESIDENT_CONSUMER_PID" 2>/dev/null || true
    wait "$AMSC_P10_RESIDENT_CONSUMER_PID" 2>/dev/null || true
  fi
  AMSC_P10_RESIDENT_CONSUMER_PID=""
  AMSC_P10_RESIDENT_CYCLE_ID=""
}

start_amsc_p10_resident_consumer() {
  if [ "$AMSC_P10_RESIDENT_CONSUMER_ENABLED" != "1" ]; then
    stop_amsc_p10_resident_consumer
    return 0
  fi
  local event_date cycle_id
  event_date="$(current_zuuu_local_date)"
  if [ "$event_date" = "${AMSC_P10_RESIDENT_EVENT_DATE:-}" ] \
    && [ -n "${AMSC_P10_RESIDENT_CONSUMER_PID:-}" ] \
    && kill -0 "$AMSC_P10_RESIDENT_CONSUMER_PID" 2>/dev/null; then
    return 0
  fi
  cycle_id="$(resident_p10_cycle_for_date "$event_date")"
  if [ -z "$cycle_id" ]; then
    if [ "$event_date" != "${AMSC_P10_RESIDENT_EVENT_DATE:-}" ]; then
      stop_amsc_p10_resident_consumer
      AMSC_P10_RESIDENT_EVENT_DATE="$event_date"
      log "amsc_p10_resident_waiting event_date=${event_date} reason=prepared_cycle_missing"
    fi
    return 0
  fi
  if [ "$cycle_id" != "${AMSC_P10_RESIDENT_CYCLE_ID:-}" ] \
    || [ -z "${AMSC_P10_RESIDENT_CONSUMER_PID:-}" ] \
    || ! kill -0 "$AMSC_P10_RESIDENT_CONSUMER_PID" 2>/dev/null; then
    stop_amsc_p10_resident_consumer
    mkdir -p "$AMSC_P10_INBOX_DIR" "$ROOT/data/logs"
    "$PY" "$ROOT/scripts/run_resident_p10_shadow_inbox.py" \
      --inbox-dir "$AMSC_P10_INBOX_DIR" \
      --shadow-db "$AMSC_P10_SHADOW_DB" \
      --prepared-cycle-id "$cycle_id" \
      --raw-archive-dir "$AMSC_P10_RAW_ARCHIVE_DIR" \
      --status-path "$ROOT/data/shadow/resident_p10_shadow_status_latest.json" \
      --apply --confirm RUN_RESIDENT_P10_ISOLATED_SHADOW \
      --from-now \
      --poll-interval-ms 50 \
      >> "$ROOT/data/logs/resident_p10_shadow_inbox.out.log" \
      2>> "$ROOT/data/logs/resident_p10_shadow_inbox.err.log" &
    AMSC_P10_RESIDENT_CONSUMER_PID="$!"
    AMSC_P10_RESIDENT_EVENT_DATE="$event_date"
    AMSC_P10_RESIDENT_CYCLE_ID="$cycle_id"
    log "amsc_p10_resident_started event_date=${event_date} cycle_id=${cycle_id} shadow_only=1"
  fi
}

start_p10_next_day_preparation() {
  if [ "$P10_NEXT_DAY_PREPARATION_ENABLED" != "1" ]; then
    return 0
  fi
  if [ -n "${P10_NEXT_DAY_PREPARATION_PID:-}" ] \
    && kill -0 "$P10_NEXT_DAY_PREPARATION_PID" 2>/dev/null; then
    return 0
  fi
  local local_hour event_date now interval elapsed
  local_hour="$(TZ=Asia/Shanghai date +%H)"
  if [ "$((10#$local_hour))" -lt 18 ]; then
    return 0
  fi
  now="$(date +%s)"
  interval="$(positive_int_arg "$P10_NEXT_DAY_PREPARATION_INTERVAL" 300)"
  elapsed="$((now - P10_NEXT_DAY_PREPARATION_LAST_START_EPOCH))"
  if [ "$P10_NEXT_DAY_PREPARATION_LAST_START_EPOCH" -gt 0 ] && [ "$elapsed" -lt "$interval" ]; then
    return 0
  fi
  event_date="$(TZ=Asia/Shanghai "$PY" - <<'PY'
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
print((datetime.now(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=1)).isoformat())
PY
)"
  P10_NEXT_DAY_PREPARATION_LAST_START_EPOCH="$now"
  "$PY" "$ROOT/scripts/prepare_next_day_production_probability_v6.py" \
    --db "$AMSC_ZUUU_SPECI_REALTIME_DB" \
    --activation "$AMSC_ZUUU_PRODUCTION_PROBABILITY_ACTIVATION_PATH" \
    --event-date "$event_date" \
    --apply --confirm PREPARE_NEXT_DAY_PRODUCTION_PHYSICAL_STATE_BAYES_V6 \
    >> "$ROOT/data/logs/p10_next_day_preparation.out.log" \
    2>> "$ROOT/data/logs/p10_next_day_preparation.err.log" &
  P10_NEXT_DAY_PREPARATION_PID="$!"
}

start_resolution_final_shadow_capture() {
  if [ "$RESOLUTION_FINAL_SHADOW_CAPTURE_ENABLED" != "1" ]; then
    return 0
  fi
  if [ -n "${RESOLUTION_FINAL_SHADOW_CAPTURE_PID:-}" ] \
    && kill -0 "$RESOLUTION_FINAL_SHADOW_CAPTURE_PID" 2>/dev/null; then
    return 0
  fi
  local now interval elapsed
  now="$(date +%s)"
  interval="$(positive_int_arg "$RESOLUTION_FINAL_SHADOW_CAPTURE_INTERVAL" 300)"
  elapsed="$((now - RESOLUTION_FINAL_SHADOW_CAPTURE_LAST_START_EPOCH))"
  if [ "$RESOLUTION_FINAL_SHADOW_CAPTURE_LAST_START_EPOCH" -gt 0 ] && [ "$elapsed" -lt "$interval" ]; then
    return 0
  fi
  RESOLUTION_FINAL_SHADOW_CAPTURE_LAST_START_EPOCH="$now"
  log "starting resolution_final_shadow_capture interval=${interval}s shadow_only=1"
  (
    run_resolution_capture_stage() {
      local stage="$1" script="$2" confirmation="$3" rc
      log "resolution_final_shadow_capture stage=${stage} start"
      if nice -n 10 "$PY" "$ROOT/scripts/$script" --apply --confirm "$confirmation"; then
        log "resolution_final_shadow_capture stage=${stage} complete"
      else
        rc="$?"
        log "resolution_final_shadow_capture stage=${stage} failed exit=${rc}"
      fi
      return 0
    }
    run_resolution_research_stage() {
      local stage="$1" script="$2" rc
      log "resolution_final_shadow_capture stage=${stage} start"
      if nice -n 10 "$PY" "$ROOT/scripts/$script" --allow-research-write; then
        log "resolution_final_shadow_capture stage=${stage} complete"
      else
        rc="$?"
        log "resolution_final_shadow_capture stage=${stage} failed exit=${rc}"
      fi
      return 0
    }
    # Physical X is an independent station-day label. It must be captured
    # before, and without depending on, any market target/finality/S bridge.
    run_resolution_capture_stage physical_x capture_physical_final_source_shadow.py CAPTURE_FORWARD_PHYSICAL_SOURCE
    if [ "$STATION_DAY_WEAK_PHYSICAL_MATERIALIZATION_ENABLED" = "1" ]; then
      # The daily weak/X sample route is research-only and station-day based.
      # It must run after final X and before market finality; it never gates
      # AMSC ingest, SSOT refresh, or the following market-label stages.
      run_resolution_research_stage weak_physical_x materialize_station_day_weak_physical_training.py
      run_resolution_research_stage official_weak_accuracy build_official_forecast_source_accuracy_report.py
      if [ "$STATION_DAY_STRONG_PHYSICAL_MATERIALIZATION_ENABLED" = "1" ]; then
        run_resolution_research_stage strong_physical_x materialize_station_day_strong_physical_candidates.py
      fi
      if [ "$JOINT_WEAK_BIAS_RESEARCH_PIPELINE_ENABLED" = "1" ]; then
        # SHA-deduplicated shadow-only training/OOS. It reads only immutable
        # station-day candidates and writes only research artifacts; no live
        # posterior, artifact pointer, archive DB, or order path can observe it.
        run_resolution_research_stage joint_weak_bias run_joint_weak_bias_research_pipeline.py
      fi
    fi
    run_resolution_capture_stage target_rule capture_target_rule_shadow.py CAPTURE_FORWARD_TARGET_RULES
    run_resolution_capture_stage market_finality capture_market_finality_shadow.py CAPTURE_FORWARD_MARKET_FINALITY
    run_resolution_capture_stage settlement_s capture_settlement_final_source_shadow.py CAPTURE_FORWARD_SETTLEMENT_SOURCE
    run_resolution_capture_stage observed_x_s_pair capture_resolution_observed_x_s_pairs_shadow.py CAPTURE_FORWARD_OBSERVED_X_S_PAIR
  ) >> "$ROOT/data/logs/resolution_final_shadow_capture.out.log" \
    2>> "$ROOT/data/logs/resolution_final_shadow_capture.err.log" &
  RESOLUTION_FINAL_SHADOW_CAPTURE_PID="$!"
}

start_official_nwp_curve_collection() {
  if [ "$OFFICIAL_NWP_CURVE_COLLECTION_ENABLED" != "1" ]; then
    return 0
  fi
  if [ -n "${OFFICIAL_NWP_CURVE_COLLECTION_PID:-}" ] \
    && kill -0 "$OFFICIAL_NWP_CURVE_COLLECTION_PID" 2>/dev/null; then
    return 0
  fi
  local now interval elapsed
  now="$(date +%s)"
  interval="$(positive_int_arg "$OFFICIAL_NWP_CURVE_COLLECTION_INTERVAL" 3600)"
  elapsed="$((now - OFFICIAL_NWP_CURVE_COLLECTION_LAST_START_EPOCH))"
  if [ "$OFFICIAL_NWP_CURVE_COLLECTION_LAST_START_EPOCH" -gt 0 ] && [ "$elapsed" -lt "$interval" ]; then
    return 0
  fi
  OFFICIAL_NWP_CURVE_COLLECTION_LAST_START_EPOCH="$now"
  log "starting official_nwp_daily_curve_monitor_job interval=${interval}s isolated_curve_root=1 p1_ingress=${OFFICIAL_NWP_DAILY_PEAK_P1_INGRESS_ENABLED}"
  (
    "$PY" "$ROOT/scripts/run_official_nwp_daily_curve_monitor_job.py" --execute || true
    if [ "$OFFICIAL_NWP_DAILY_PEAK_P1_INGRESS_ENABLED" = "1" ]; then
      "$PY" "$ROOT/scripts/materialize_official_nwp_daily_peak_p1_ingress.py" \
        --apply --confirm MATERIALIZE_OFFICIAL_NWP_DAILY_PEAK_P1 || true
    fi
  ) >> "$ROOT/data/logs/official_nwp_daily_curve_monitor.out.log" \
    2>> "$ROOT/data/logs/official_nwp_daily_curve_monitor.err.log" &
  OFFICIAL_NWP_CURVE_COLLECTION_PID="$!"
}

start_official_nwp_live_snapshot_collection() {
  if [ "$OFFICIAL_NWP_LIVE_SNAPSHOT_COLLECTION_ENABLED" != "1" ]; then
    return 0
  fi
  if [ -n "${OFFICIAL_NWP_LIVE_SNAPSHOT_COLLECTION_PID:-}" ] \
    && kill -0 "$OFFICIAL_NWP_LIVE_SNAPSHOT_COLLECTION_PID" 2>/dev/null; then
    return 0
  fi
  local now interval elapsed
  now="$(date +%s)"
  interval="$(positive_int_arg "$OFFICIAL_NWP_LIVE_SNAPSHOT_COLLECTION_INTERVAL" 900)"
  elapsed="$((now - OFFICIAL_NWP_LIVE_SNAPSHOT_COLLECTION_LAST_START_EPOCH))"
  if [ "$OFFICIAL_NWP_LIVE_SNAPSHOT_COLLECTION_LAST_START_EPOCH" -gt 0 ] && [ "$elapsed" -lt "$interval" ]; then
    return 0
  fi
  OFFICIAL_NWP_LIVE_SNAPSHOT_COLLECTION_LAST_START_EPOCH="$now"
  log "starting official_nwp_live_snapshot_monitor interval=${interval}s live_root=1"
  "$PY" "$ROOT/scripts/run_official_nwp_live_snapshot_monitor_job.py" --execute \
    >> "$ROOT/data/logs/official_nwp_live_snapshot_monitor.out.log" \
    2>> "$ROOT/data/logs/official_nwp_live_snapshot_monitor.err.log" &
  OFFICIAL_NWP_LIVE_SNAPSHOT_COLLECTION_PID="$!"
}

sleep_with_child_supervision() {
  local total check deadline now chunk
  total="$(positive_int_arg "${1:-$ANOMALY_INTERVAL}" "$ANOMALY_INTERVAL")"
  check="$(positive_int_arg "$CHILD_CHECK_INTERVAL" 5)"
  deadline="$(($(date +%s) + total))"
  while true; do
    start_watcher
    start_archive_wal_keeper
    start_hard_source_direct
    start_amsc_zuuu_speci_watch
    start_amsc_zuuu_poll
    start_amsc_p10_shadow_source_capture
    start_amsc_p10_resident_consumer
    start_p10_next_day_preparation
    start_resolution_final_shadow_capture
    start_official_nwp_curve_collection
    start_official_nwp_live_snapshot_collection
    now="$(date +%s)"
    if [ "$now" -ge "$deadline" ]; then
      break
    fi
    chunk="$((deadline - now))"
    if [ "$chunk" -gt "$check" ]; then
      chunk="$check"
    fi
    sleep "$chunk"
  done
}

cleanup() {
  trap - EXIT INT TERM
  write_p10_monitor_runtime_status "stopping" 2>/dev/null || true
  if [ -n "${WATCHER_PID:-}" ] && kill -0 "$WATCHER_PID" 2>/dev/null; then
    kill "$WATCHER_PID" 2>/dev/null || true
    wait "$WATCHER_PID" 2>/dev/null || true
  fi
  if [ -n "${ARCHIVE_WAL_KEEPER_PID:-}" ] && kill -0 "$ARCHIVE_WAL_KEEPER_PID" 2>/dev/null; then
    kill "$ARCHIVE_WAL_KEEPER_PID" 2>/dev/null || true
    wait "$ARCHIVE_WAL_KEEPER_PID" 2>/dev/null || true
  fi
  if [ -n "${HARD_SOURCE_DIRECT_PID:-}" ] && kill -0 "$HARD_SOURCE_DIRECT_PID" 2>/dev/null; then
    kill "$HARD_SOURCE_DIRECT_PID" 2>/dev/null || true
    wait "$HARD_SOURCE_DIRECT_PID" 2>/dev/null || true
  fi
  if [ -n "${AMSC_ZUUU_POLL_PID:-}" ] && kill -0 "$AMSC_ZUUU_POLL_PID" 2>/dev/null; then
    kill "$AMSC_ZUUU_POLL_PID" 2>/dev/null || true
    wait "$AMSC_ZUUU_POLL_PID" 2>/dev/null || true
  fi
  if [ -n "${AMSC_ZUUU_SPECI_WATCH_PID:-}" ] && kill -0 "$AMSC_ZUUU_SPECI_WATCH_PID" 2>/dev/null; then
    kill "$AMSC_ZUUU_SPECI_WATCH_PID" 2>/dev/null || true
    wait "$AMSC_ZUUU_SPECI_WATCH_PID" 2>/dev/null || true
  fi
  if [ -n "${AMSC_P10_SHADOW_SOURCE_PID:-}" ] && kill -0 "$AMSC_P10_SHADOW_SOURCE_PID" 2>/dev/null; then
    kill "$AMSC_P10_SHADOW_SOURCE_PID" 2>/dev/null || true
    wait "$AMSC_P10_SHADOW_SOURCE_PID" 2>/dev/null || true
  fi
  stop_amsc_p10_resident_consumer
  if [ -n "${P10_NEXT_DAY_PREPARATION_PID:-}" ] && kill -0 "$P10_NEXT_DAY_PREPARATION_PID" 2>/dev/null; then
    kill "$P10_NEXT_DAY_PREPARATION_PID" 2>/dev/null || true
    wait "$P10_NEXT_DAY_PREPARATION_PID" 2>/dev/null || true
  fi
  if [ -n "${RESOLUTION_FINAL_SHADOW_CAPTURE_PID:-}" ] && kill -0 "$RESOLUTION_FINAL_SHADOW_CAPTURE_PID" 2>/dev/null; then
    kill "$RESOLUTION_FINAL_SHADOW_CAPTURE_PID" 2>/dev/null || true
    wait "$RESOLUTION_FINAL_SHADOW_CAPTURE_PID" 2>/dev/null || true
  fi
  if [ -n "${OFFICIAL_NWP_CURVE_COLLECTION_PID:-}" ] && kill -0 "$OFFICIAL_NWP_CURVE_COLLECTION_PID" 2>/dev/null; then
    kill "$OFFICIAL_NWP_CURVE_COLLECTION_PID" 2>/dev/null || true
    wait "$OFFICIAL_NWP_CURVE_COLLECTION_PID" 2>/dev/null || true
  fi
  if [ -n "${OFFICIAL_NWP_LIVE_SNAPSHOT_COLLECTION_PID:-}" ] && kill -0 "$OFFICIAL_NWP_LIVE_SNAPSHOT_COLLECTION_PID" 2>/dev/null; then
    kill "$OFFICIAL_NWP_LIVE_SNAPSHOT_COLLECTION_PID" 2>/dev/null || true
    wait "$OFFICIAL_NWP_LIVE_SNAPSHOT_COLLECTION_PID" 2>/dev/null || true
  fi
}

terminate() {
  cleanup
  exit 0
}

trap cleanup EXIT
trap terminate INT TERM

if [ "$ONCE" = "1" ]; then
  start_archive_wal_keeper
  if [ "$NO_WATCHER" != "1" ] && [ "$WATCHER_ENABLED" = "1" ]; then
    WATCHER_SMS_ARGS=()
    if [ "$WATCHER_SMS_ENABLED" = "0" ]; then
      WATCHER_SMS_ARGS=(--no-sms)
    elif [ "$WATCHER_SMS_ENABLED" = "1" ]; then
      WATCHER_SMS_ARGS=(--sms)
    fi
    "$PY" "$ROOT/scripts/auto_trade_watcher.py" --interval "$WATCHER_INTERVAL" "${WATCHER_SMS_ARGS[@]}" --once || true
  fi
  if [ "$HARD_SOURCE_DIRECT_ENABLED" = "1" ]; then
    "$ROOT/scripts/hard_source_direct_poller.sh" --once || true
  fi
  start_amsc_zuuu_speci_watch
  start_amsc_zuuu_poll
  start_amsc_p10_shadow_source_capture
  start_amsc_p10_resident_consumer
  start_p10_next_day_preparation
  if [ "$OFFICIAL_NWP_CURVE_COLLECTION_ENABLED" = "1" ]; then
    "$PY" "$ROOT/scripts/run_official_nwp_daily_curve_monitor_job.py" --execute || true
  fi
  if [ "$OFFICIAL_NWP_LIVE_SNAPSHOT_COLLECTION_ENABLED" = "1" ]; then
    "$PY" "$ROOT/scripts/run_official_nwp_live_snapshot_monitor_job.py" --execute || true
  fi
  run_chengdu_anomaly
  exit 0
fi

log "daemon_loop_start anomaly_interval=${ANOMALY_INTERVAL} watcher_enabled=${WATCHER_ENABLED} anomaly_enabled=${ANOMALY_ENABLED} hard_source_direct_enabled=${HARD_SOURCE_DIRECT_ENABLED} archive_wal_keeper_enabled=${ARCHIVE_WAL_KEEPER_ENABLED}"
write_p10_monitor_runtime_status "running"
while true; do
  start_watcher
  start_archive_wal_keeper
  start_hard_source_direct
  start_amsc_zuuu_speci_watch
  start_amsc_zuuu_poll
  start_amsc_p10_shadow_source_capture
  start_amsc_p10_resident_consumer
  start_p10_next_day_preparation
  start_resolution_final_shadow_capture
  start_official_nwp_curve_collection
  start_official_nwp_live_snapshot_collection
  if run_chengdu_anomaly; then
    :
  else
    rc="$?"
    log "chengdu_anomaly_failed exit=${rc}"
  fi
  SLEEP_INTERVAL="$(chengdu_anomaly_sleep_interval || printf '%s\n' "$ANOMALY_INTERVAL")"
  log "chengdu_anomaly_sleep interval=${SLEEP_INTERVAL}"
  sleep_with_child_supervision "$SLEEP_INTERVAL"
done
