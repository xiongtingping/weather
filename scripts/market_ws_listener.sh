#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Keep reconnect/diagnostic output bounded across daemon restarts.  The WS
# snapshot itself remains content-addressed and bounded separately.
. "$ROOT/scripts/lib/bounded_service_logs.sh"
weather_prepare_bounded_service_logs \
  "$ROOT/data/market_ws.log" \
  "$ROOT/data/logs/market_ws.out.log" \
  "$ROOT/data/logs/market_ws.err.log"

if [ -f "$ROOT/scripts/lib/load_launchctl_env.sh" ]; then
  . "$ROOT/scripts/lib/load_launchctl_env.sh"
  load_repo_env_file "$ROOT/.env"
fi

if [ "${WEATHER_SERVICE_DOMAIN:-}" != "user" ]; then
  echo "[market_ws] user_domain_required" >&2
  exit 78
fi
export WEATHER_SERVICE_DOMAIN=user
export PM_TARGET_EXECUTION_MODE=production_no_live

mkdir -p "$ROOT/data"
PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') [market_ws] missing .venv python at $PY" >> "$ROOT/data/market_ws.log"
  exit 1
fi

export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
export PM_MARKET_WS_DAYS_AHEAD="${PM_MARKET_WS_DAYS_AHEAD:-2}"
# Core production subscribes to both sides: NO for hard-NO entry and YES for
# basket entry plus mandatory pre-entry liquidation. Two near-even shards
# reduce connection failure area.
export PM_MARKET_WS_MAX_ASSETS="${PM_MARKET_WS_MAX_ASSETS:-4000}"
export PM_MARKET_WS_SHARD_SIZE="${PM_MARKET_WS_SHARD_SIZE:-400}"
export PM_MARKET_WS_RELOAD_SEC="${PM_MARKET_WS_RELOAD_SEC:-60}"
export PM_MARKET_WS_CATALOG_PROBE_SEC="${PM_MARKET_WS_CATALOG_PROBE_SEC:-1.0}"
export PM_MARKET_WS_SHARDED_SNAPSHOT="1"
export PM_MARKET_WS_SOURCE_LANE="amsc,awc"
exec "$PY" "$ROOT/scripts/market_ws_listener.py" >> "$ROOT/data/market_ws.log" 2>&1
