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
# The cooling-side ceiling's two exports (PM_METAR_CEILING and its window)
# left with the ceiling itself (charter 150, 2026-09-19): P1's remaining
# rise is its joint observation likelihood's question, and nothing in the
# serving path calls the ceiling any more.
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
  PY="$(command -v python3)"
fi
exec "$PY" "$ROOT/scripts/weather_basket_yes_daemon.py"
