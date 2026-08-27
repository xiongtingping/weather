#!/bin/bash
# Refresh the fleet regime daily residual series the next-day shift reads.
#
# Safe beside the resident for the same reason the truth sweep is: the scan
# is all reads, and the write lock is taken only around one small upsert per
# event day.  A maintenance writer that held the lock across its own scan
# would not slow the resident, it would fail-close P0 and P1 for that cycle.
#
# Idempotent by construction: a day is rewritten only when its truth has been
# revised since it was last recorded, so a pass with nothing to do skips the
# expensive bridge-history scan and writes nothing.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
. "$PROJECT_DIR/scripts/lib/resolve_project_python.sh"
. "$PROJECT_DIR/scripts/lib/single_instance.sh"
single_instance fleet_regime_refresh_sweep
PY="$(resolve_project_python "$PROJECT_DIR")" || {
  echo "missing project python runtime" >&2
  exit 1
}

cd "$PROJECT_DIR"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] fleet regime refresh sweep"
"$PY" scripts/fleet_regime_refresh_sweep.py
