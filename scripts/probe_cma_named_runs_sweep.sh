#!/bin/bash
# Ask whether the next CMA cycle is out (one request, SSOT read-only) and
# leave the answer for the daemon, which then makes the CMA capture due.
# Writes no database, so it holds no write domain.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
. "$PROJECT_DIR/scripts/lib/resolve_project_python.sh"
. "$PROJECT_DIR/scripts/lib/single_instance.sh"
single_instance probe_cma_named_runs
PY="$(resolve_project_python "$PROJECT_DIR")" || {
  echo "missing project python runtime" >&2
  exit 1
}

cd "$PROJECT_DIR"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] cma named-run probe"
"$PY" scripts/probe_cma_named_runs.py
