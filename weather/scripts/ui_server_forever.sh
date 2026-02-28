#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ -f "$ROOT/.env" ]; then
  set -a
  . "$ROOT/.env"
  set +a
fi

: "${PM_NOFILE_SOFT:=8192}"
if [[ "$PM_NOFILE_SOFT" =~ ^[0-9]+$ ]]; then
  ulimit -Sn "$PM_NOFILE_SOFT" 2>/dev/null || true
fi

PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
  PY="python3"
fi

exec "$PY" "$ROOT/ui/server.py"
