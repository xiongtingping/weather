#!/bin/bash
# Capture ICON-EU day-maxima for the European stations the sweep admitted.
#
# Run-scoped: one download of a run's whole-domain fields serves seven
# stations and two event days.  Hourly against a six-hourly product
# because the DWD archive keeps no history -- each cycle directory holds
# only the newest run, so a missed run is permanently gone and the cost
# of a wasted pass (two directory probes) is nothing beside it.
#
# Writes to the SSOT, so it takes the sweep lock like the other writers
# and holds it only around each single-row insert.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
. "$PROJECT_DIR/scripts/lib/resolve_project_python.sh"
. "$PROJECT_DIR/scripts/lib/single_instance.sh"
single_instance capture_dwd_icon_eu_forward
PY="$(resolve_project_python "$PROJECT_DIR")" || {
  echo "missing project python runtime" >&2
  exit 1
}

cd "$PROJECT_DIR"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] dwd icon-eu forward capture"
"$PY" scripts/capture_dwd_icon_eu_forward.py
