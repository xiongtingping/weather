#!/bin/bash
set -euo pipefail

ROOT="/Users/xiong/weather"
cd "$ROOT"

. "$ROOT/scripts/lib/bounded_service_logs.sh"
weather_prepare_bounded_service_logs \
  "$ROOT/data/logs/weather_basket_yes.out.log" \
  "$ROOT/data/logs/weather_basket_yes.err.log"

if [ -f "$ROOT/scripts/lib/load_launchctl_env.sh" ]; then
  . "$ROOT/scripts/lib/load_launchctl_env.sh"
  load_repo_env_file "$ROOT/.env"
fi
if [ "${WEATHER_SERVICE_DOMAIN:-}" != "user" ]; then
  echo "[weather_basket_yes] user_domain_required" >&2
  exit 78
fi
export WEATHER_SERVICE_DOMAIN=user
export PM_TARGET_EXECUTION_MODE=production_live
export PM_TARGET_PRODUCTION_WRITER=1
# The cooling-side ceiling: after the peak the day cannot warm, and
# nothing else in the chain says so.  Serving refuses any event date at
# or before the artifact's trained_through, so this is inert until the
# curve has been fitted past it.
export PM_METAR_CEILING=1
export PM_METAR_CEILING_MAX_HOURS_LEFT=10.0
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
  PY="$(command -v python3)"
fi
exec "$PY" "$ROOT/scripts/weather_basket_yes_daemon.py"
