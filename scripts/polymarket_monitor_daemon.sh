#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
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
HARD_SOURCE_DIRECT_PID=""

log() {
  printf '[polymarket-monitor] %s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
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
    start_hard_source_direct
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
  log "starting hard_source_direct_poller awc_release_watch=${PM_HARD_SOURCE_AWC_RELEASE_WATCH_ENABLED:-1}/${PM_HARD_SOURCE_AWC_RELEASE_WATCH_WINDOW_MIN:-8}m/${PM_HARD_SOURCE_AWC_RELEASE_WATCH_POLL_SEC:-1}s awc_dynamic_hours=${PM_HARD_SOURCE_AWC_DYNAMIC_HOURS_ENABLED:-1}/min${PM_HARD_SOURCE_AWC_DYNAMIC_MIN_HOURS:-8}/margin${PM_HARD_SOURCE_AWC_DYNAMIC_MARGIN_HOURS:-2} awc_recent_window=${PM_AWC_FACT_REFRESH_RECENT_WINDOW_ENABLED:-1}/${PM_AWC_FACT_REFRESH_RECENT_WINDOW_HOURS:-4}h awc_timeout=${PM_BAYES_LIKELIHOOD_AWC_METAR_TIMEOUT_SEC:-2.0}s/retry${PM_BAYES_LIKELIHOOD_AWC_METAR_RETRIES:-1} metar_pws_enabled=${PM_HARD_SOURCE_METAR_PWS_ENABLED:-0} metar_pws_blocking=${PM_HARD_SOURCE_METAR_PWS_BLOCKING:-0} metar_pws_background=${PM_HARD_SOURCE_METAR_PWS_BACKGROUND_REFRESH:-0} iem_peak=${PM_HARD_SOURCE_METAR_IEM_PEAK_POLL_SEC:-30}s iem_normal=${PM_HARD_SOURCE_METAR_IEM_NORMAL_POLL_SEC:-300}s avwx_peak=${PM_HARD_SOURCE_METAR_AVWX_PEAK_POLL_SEC:-15}s avwx_normal=${PM_HARD_SOURCE_METAR_AVWX_NORMAL_POLL_SEC:-60}s checkwx_peak=${PM_HARD_SOURCE_METAR_CHECKWX_PEAK_POLL_SEC:-15}s checkwx_normal=${PM_HARD_SOURCE_METAR_CHECKWX_NORMAL_POLL_SEC:-60}s pws_peak=${PM_HARD_SOURCE_TWC_PWS_RAPID_PEAK_POLL_SEC:-10}s pws_normal=${PM_HARD_SOURCE_TWC_PWS_RAPID_NORMAL_POLL_SEC:-60}s"
  "$ROOT/scripts/hard_source_direct_poller.sh" &
  HARD_SOURCE_DIRECT_PID="$!"
}

sleep_with_child_supervision() {
  local total check deadline now chunk
  total="$(positive_int_arg "${1:-$ANOMALY_INTERVAL}" "$ANOMALY_INTERVAL")"
  check="$(positive_int_arg "$CHILD_CHECK_INTERVAL" 5)"
  deadline="$(($(date +%s) + total))"
  while true; do
    start_watcher
    start_hard_source_direct
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
  if [ -n "${WATCHER_PID:-}" ] && kill -0 "$WATCHER_PID" 2>/dev/null; then
    kill "$WATCHER_PID" 2>/dev/null || true
    wait "$WATCHER_PID" 2>/dev/null || true
  fi
  if [ -n "${HARD_SOURCE_DIRECT_PID:-}" ] && kill -0 "$HARD_SOURCE_DIRECT_PID" 2>/dev/null; then
    kill "$HARD_SOURCE_DIRECT_PID" 2>/dev/null || true
    wait "$HARD_SOURCE_DIRECT_PID" 2>/dev/null || true
  fi
}

terminate() {
  cleanup
  exit 0
}

trap cleanup EXIT
trap terminate INT TERM

if [ "$ONCE" = "1" ]; then
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
  run_chengdu_anomaly
  exit 0
fi

log "daemon_loop_start anomaly_interval=${ANOMALY_INTERVAL} watcher_enabled=${WATCHER_ENABLED} anomaly_enabled=${ANOMALY_ENABLED} hard_source_direct_enabled=${HARD_SOURCE_DIRECT_ENABLED}"
while true; do
  start_watcher
  start_hard_source_direct
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
