#!/bin/bash
# Capture CMA GRAPES Global point curves for the eight mainland Chinese
# stations, which had no regional coverage at all before this lane.
#
# Hourly against a six-hourly product for the ICON-EU lane's reason: the
# transport serves only the run it currently holds, so a cycle missed is a
# cycle gone, and a pass with nothing new costs eight small JSON reads.
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
