#!/bin/bash
# Charter 201: ask whether the family's next global runs are out (one request
# a product, SSOT read-only) and leave the answer for the daemon, which then
# makes the deterministic capture due.  Writes no database.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
. "$PROJECT_DIR/scripts/lib/resolve_project_python.sh"
. "$PROJECT_DIR/scripts/lib/single_instance.sh"
single_instance probe_deterministic_runs
PY="$(resolve_project_python "$PROJECT_DIR")" || {
  echo "missing project python runtime" >&2
  exit 1
}

cd "$PROJECT_DIR"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] deterministic family run probe"
"$PY" scripts/probe_deterministic_runs.py
