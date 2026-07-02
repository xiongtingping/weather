#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ -f "$ROOT/scripts/lib/load_launchctl_env.sh" ]; then
  . "$ROOT/scripts/lib/load_launchctl_env.sh"
  load_repo_env_file "$ROOT/.env"
fi

mkdir -p "$ROOT/data"
PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') [market_ws] missing .venv python at $PY" >> "$ROOT/data/market_ws.log"
  exit 1
fi

export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
export PM_MARKET_WS_DAYS_AHEAD="${PM_MARKET_WS_DAYS_AHEAD:-2}"
# Keep the hot weather trading path from competing with posterior writes.
# Override these env vars from launchd/.env when broader market coverage is needed.
export PM_MARKET_WS_MAX_ASSETS="${PM_MARKET_WS_MAX_ASSETS:-400}"
export PM_MARKET_WS_SHARD_SIZE="${PM_MARKET_WS_SHARD_SIZE:-400}"
exec "$PY" "$ROOT/scripts/market_ws_listener.py" >> "$ROOT/data/market_ws.log" 2>&1
