#!/bin/bash
# Charter 201 step 2: fit the P0 deterministic-feature gate on settled days --
# per station x source the rolling k, the pre-registered gate readings and the
# station's candidate -- and write one JSON artifact the P0 compile reads.
# SSOT read-only; writes no database.  Niced: it is a daily fit on a busy box.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
. "$PROJECT_DIR/scripts/lib/resolve_project_python.sh"
. "$PROJECT_DIR/scripts/lib/single_instance.sh"
single_instance fit_deterministic_feature_gate
PY="$(resolve_project_python "$PROJECT_DIR")" || {
  echo "missing project python runtime" >&2
  exit 1
}

cd "$PROJECT_DIR"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] deterministic feature gate fit"
nice -n 10 "$PY" scripts/fit_deterministic_feature_gate.py
