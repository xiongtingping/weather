#!/bin/bash
# Advance the resting-order shadow probe one pass.
#
# Places nothing.  Writes an intent per live scope (throttled to one per
# quarter hour inside the probe), decides intents whose horizon has passed
# against books that arrived after they were written, and scores the filled
# ones once the station-day settles.
#
# Runs every five minutes because the shortest horizon under test is five,
# and an order that expired between two passes would be recorded as expired
# without ever having been checked against the book that might have filled
# it.  The pass is short and almost entirely reads.
#
# Safe beside the resident for the same reason the ledger sweep is: reads do
# not block a writer in WAL mode, the writes are a handful of rows at the
# end, and the probe never checkpoints -- folding this database's WAL while
# the resident writes is how a maintenance pass takes production down.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
. "$PROJECT_DIR/scripts/lib/resolve_project_python.sh"
PY="$(resolve_project_python "$PROJECT_DIR")" || {
  echo "missing project python runtime" >&2
  exit 1
}

cd "$PROJECT_DIR"
exec "$PY" scripts/maker_shadow_probe.py
