#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ -f "$ROOT/scripts/lib/load_launchctl_env.sh" ]; then
  . "$ROOT/scripts/lib/load_launchctl_env.sh"
  load_env_file_vars_safe "$ROOT/.env" 1
  if [[ "${PM_UI_ENV_PROXY_REACHABILITY_GUARD:-0}" =~ ^(1|true|yes|on)$ ]]; then
    ensure_launchd_proxy_env_reachable
  fi
  if [[ "${PM_UI_LOAD_POLY_ENV:-0}" =~ ^(1|true|yes|on)$ ]]; then
    load_polymarket_launchctl_env
  fi
fi

# UI must not advertise a production Edge-gate mode inherited from .env.
while IFS='=' read -r legacy_runtime_key _; do
  case "$legacy_runtime_key" in
    PM_EDGE_REALIZABLE_*|PM_EDGE_TRADEABILITY_*|PM_LCB_GLOBAL_*|PM_ENTRY_USABLE_EDGE_*)
      unset "$legacy_runtime_key"
      ;;
  esac
done < <(env)

: "${PM_NOFILE_SOFT:=8192}"
if [[ "$PM_NOFILE_SOFT" =~ ^[0-9]+$ ]]; then
  ulimit -Sn "$PM_NOFILE_SOFT" 2>/dev/null || true
fi

PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
  . "$ROOT/scripts/lib/resolve_project_python.sh"
  PY="$(resolve_project_python "$ROOT" || true)"
fi
if [ -z "${PY:-}" ] || [ ! -x "$PY" ]; then
  echo "[ui_server] missing project python runtime" >&2
  exit 1
fi

. "$ROOT/scripts/lib/process_governance.sh"
UI_SERVER_NICE="$(normalize_nice_level "${PM_UI_SERVER_NICE:-0}" 0)"
if [ "$UI_SERVER_NICE" = "0" ]; then
  exec "$PY" -m ui.server
fi
if ! nice -n "$UI_SERVER_NICE" true >/dev/null 2>&1; then
  echo "[ui_server] nice -n $UI_SERVER_NICE unavailable; starting without nice" >&2
  exec "$PY" -m ui.server
fi
exec nice -n "$UI_SERVER_NICE" "$PY" -m ui.server
