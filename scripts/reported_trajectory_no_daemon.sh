#!/bin/bash
set -euo pipefail

ROOT="/Users/xiong/weather"
cd "$ROOT"

. "$ROOT/scripts/lib/bounded_service_logs.sh"
weather_prepare_bounded_service_logs \
  "$ROOT/data/logs/reported_trajectory_no.out.log" \
  "$ROOT/data/logs/reported_trajectory_no.err.log"

if [ -f "$ROOT/scripts/lib/load_launchctl_env.sh" ]; then
  . "$ROOT/scripts/lib/load_launchctl_env.sh"
  load_repo_env_file "$ROOT/.env"
fi
if [ "${WEATHER_SERVICE_DOMAIN:-}" != "user" ]; then
  echo "[reported_trajectory_no] user_domain_required" >&2
  exit 78
fi
export WEATHER_SERVICE_DOMAIN=user
export PM_TARGET_EXECUTION_MODE=production_no_live
export PM_TARGET_PRODUCTION_WRITER=1
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
  PY="$(command -v python3)"
fi
exec "$PY" "$ROOT/scripts/reported_trajectory_no_daemon.py"
