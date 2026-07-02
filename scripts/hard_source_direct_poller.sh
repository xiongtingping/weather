#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ -f "$ROOT/scripts/lib/load_launchctl_env.sh" ]; then
  # shellcheck disable=SC1091
  . "$ROOT/scripts/lib/load_launchctl_env.sh"
  load_repo_env_file "$ROOT/.env"
  load_polymarket_launchctl_env
fi
if [ -f "$ROOT/scripts/lib/resolve_project_python.sh" ]; then
  # shellcheck disable=SC1091
  . "$ROOT/scripts/lib/resolve_project_python.sh"
fi

mkdir -p "$ROOT/data" "$ROOT/data/logs"

PY="${PYTHON:-$ROOT/.venv/bin/python}"
if [ ! -x "$PY" ] && declare -f resolve_project_python >/dev/null 2>&1; then
  PY="$(resolve_project_python "$ROOT" || true)"
fi
if [ -z "${PY:-}" ] || [ ! -x "$PY" ]; then
  PY="python3"
fi

exec "$PY" "$ROOT/scripts/hard_source_direct_poller.py" --loop "$@"
