#!/bin/bash
# Charter 282: fit the P1 TX-revision shift (kappa and the walk-forward
# verdict serving requires) on settled
# station-days and write the one JSON artifact the P1 compile reads.
# SSOT read-only; writes no database.  Niced: a daily fit on a busy box.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
. "$PROJECT_DIR/scripts/lib/resolve_project_python.sh"
. "$PROJECT_DIR/scripts/lib/single_instance.sh"
single_instance fit_tx_revision_shift
PY="$(resolve_project_python "$PROJECT_DIR")" || {
  echo "missing project python runtime" >&2
  exit 1
}

cd "$PROJECT_DIR"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] P1 TX revision shift fit"
nice -n 10 "$PY" scripts/fit_tx_revision_shift.py
