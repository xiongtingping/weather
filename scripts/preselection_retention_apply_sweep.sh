#!/bin/bash
# One already archived, verified batch in the resident SSOT writer rotation.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
. "$PROJECT_DIR/scripts/lib/resolve_project_python.sh"
. "$PROJECT_DIR/scripts/lib/single_instance.sh"
single_instance preselection_retention_apply_sweep
PY="$(resolve_project_python "$PROJECT_DIR")"
cd "$PROJECT_DIR"
"$PY" scripts/preselection_retention_lane.py apply
