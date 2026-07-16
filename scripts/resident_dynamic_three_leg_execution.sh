#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Deliberately do not load .env here.  Production/live authorization must be
# injected by the managed service definition, never inherited implicitly.
export PYTHONUNBUFFERED=1
. "$ROOT/scripts/lib/resolve_project_python.sh"
PY="$(resolve_project_python "$ROOT" || true)"
if [ -z "${PY:-}" ] || [ ! -x "$PY" ]; then
  echo "dynamic-three-leg resident: controlled project Python unavailable" >&2
  exit 2
fi
exec "$PY" "$ROOT/resident_dynamic_three_leg_execution_loop.py" "$@"
