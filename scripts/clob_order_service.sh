#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

LIVE_PAUSE_FILE="${PM_LIVE_PAUSE_FILE:-$ROOT/data/live_paused.flag}"
if [ -f "$LIVE_PAUSE_FILE" ]; then
  mkdir -p "$ROOT/data/logs"
  printf '[clob_order_service] live paused by %s at %s\n' "$LIVE_PAUSE_FILE" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$ROOT/data/logs/live_pause.log"
  exec sleep "${PM_LIVE_PAUSE_SLEEP_SEC:-86400}"
fi

if [ -f "$ROOT/scripts/lib/load_launchctl_env.sh" ]; then
  # shellcheck disable=SC1091
  . "$ROOT/scripts/lib/load_launchctl_env.sh"
  load_repo_env_file "$ROOT/.env"
  load_polymarket_launchctl_env
fi

NODE_BIN="${NODE_BIN:-/usr/local/bin/node}"
if [ ! -x "$NODE_BIN" ]; then
  NODE_BIN="$(command -v node || true)"
fi
if [ -z "${NODE_BIN:-}" ]; then
  echo "[clob_order_service] missing node runtime" >&2
  exit 1
fi

mkdir -p "$ROOT/data" "$ROOT/data/logs"
exec "$NODE_BIN" "$ROOT/scripts/clob_order_service.mjs"
