#!/bin/bash
set -euo pipefail

ROOT="/Users/xiong/weather"
cd "$ROOT"
LOG="$ROOT/data/model_release_watcher.log"

if [ -f "$ROOT/scripts/lib/load_launchctl_env.sh" ]; then
  . "$ROOT/scripts/lib/load_launchctl_env.sh"
  load_repo_env_file "$ROOT/.env"
fi

. "$ROOT/scripts/lib/resolve_project_python.sh"
PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
  PY="$(resolve_project_python "$ROOT" || true)"
fi
if [ -z "${PY:-}" ] || [ ! -x "$PY" ]; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') [model_release_watcher] missing project python runtime" >> "$LOG"
  exit 1
fi

. "$ROOT/scripts/lib/weather_refresh_coordinator.sh"
if ! weather_refresh_register_entrypoint "model_release_watcher" "model" "${PM_REFRESH_COORDINATOR_LANE_NAME:-weather_refresh_lane}" "$LOG" "model_release_watcher"; then
  exit 0
fi
# Keep the coordinator guard visible for static governance checks, but source
# release probing is read-only and must continue during a live SSOT window.
# Only the downstream dependency/materialization writer is allowed to yield.
if weather_refresh_skip_if_live_ssot "model_release_watcher" "$LOG" "model_release_watcher"; then
  weather_refresh_log "$LOG" "model_release_watcher" "live_ssot guard observed; continue read_only_fixed_cycle_probe"
fi

OFFSET_DAYS="${PM_MODEL_RELEASE_WATCH_EVENT_OFFSET_DAYS:-1}"
if ! [[ "$OFFSET_DAYS" =~ ^-?[0-9]+$ ]]; then
  OFFSET_DAYS=1
fi
EVENT_DATE="$("$PY" - "$OFFSET_DAYS" <<'PY'
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

offset = int(sys.argv[1])
print((datetime.now(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=offset)).isoformat())
PY
)"

TIMEOUT="${PM_MODEL_RELEASE_WATCHER_TIMEOUT_SEC:-6}"
MODELS="${PM_MODEL_RELEASE_WATCH_MODELS:-gfs_seamless,icon_seamless,ecmwf_ifs025}"

echo "---- $(date '+%Y-%m-%d %H:%M:%S') model_release_watcher event_date=${EVENT_DATE} models=${MODELS} ----" >> "$LOG"
"$PY" "$ROOT/scripts/model_release_watcher.py" \
  --event-date "$EVENT_DATE" \
  --models "$MODELS" \
  --timeout-sec "$TIMEOUT" \
  --output "$ROOT/data/model_release_watcher_latest.json" >> "$LOG" 2>&1

# The watcher is the fixed-cycle source heartbeat.  Its stable run/file
# identity is consumed by the idempotent dependency worker, which performs
# model acquisition, source materialization, and P1 refresh only when a new
# market/source generation is present.  Run the handoff in this same launchd
# job: system launchd reaps detached descendants when this script exits.
if [ "${PM_MODEL_RELEASE_AUTO_DEPENDENCY_REFRESH_ENABLED:-1}" = "1" ] \
  && [ -f "$ROOT/data/weather_discovery_new_market_signal.json" ]; then
  mkdir -p "$ROOT/data/runtime/market_dependency_refresh" "$ROOT/data/logs"
  "$PY" "$ROOT/scripts/market_dependency_refresh.py" \
    --signal "$ROOT/data/weather_discovery_new_market_signal.json" \
    --state "$ROOT/data/runtime/market_dependency_refresh/state_latest.json" \
    --status "$ROOT/data/runtime/market_dependency_refresh/status_latest.json" \
    >> "$ROOT/data/logs/market_dependency_refresh.source.log" 2>&1
fi
