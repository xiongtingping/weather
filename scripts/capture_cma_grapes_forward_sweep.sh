#!/bin/bash
# Capture CMA GRAPES Global point curves for the eight mainland Chinese
# stations, which had no regional coverage at all before this lane.
#
# Twice per cycle against a six-hourly product: the transport serves only
# the run it currently holds, so a cycle missed is a cycle gone, and one
# attempt per cycle left no second chance.  A pass whose cycle is already
# stored makes no request at all.
#
# Writes the SSOT, so it takes the sweep lock like the other writers.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
. "$PROJECT_DIR/scripts/lib/resolve_project_python.sh"
. "$PROJECT_DIR/scripts/lib/single_instance.sh"
single_instance capture_cma_grapes_forward
PY="$(resolve_project_python "$PROJECT_DIR")" || {
  echo "missing project python runtime" >&2
  exit 1
}

cd "$PROJECT_DIR"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] cma grapes global forward capture"
"$PY" scripts/capture_cma_grapes_forward.py
