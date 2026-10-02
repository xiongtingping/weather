#!/bin/bash
# Read-only SSOT scan and verified optional cold archive; never takes a writer turn.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
. "$PROJECT_DIR/scripts/lib/resolve_project_python.sh"
. "$PROJECT_DIR/scripts/lib/single_instance.sh"
single_instance preselection_archive_sweep
PY="$(resolve_project_python "$PROJECT_DIR")"
cd "$PROJECT_DIR"
"$PY" scripts/preselection_retention_lane.py archive
