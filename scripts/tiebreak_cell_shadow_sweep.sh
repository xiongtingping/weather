#!/bin/bash
# Charter 164: replay the tiebreak rule over history and score the registered
# hypotheses, once a day.
#
# A reader, not a writer: it opens the SSOT read-only and writes only
# data/tiebreak_cell_shadow.db, so it never queues behind the resident's
# writer lanes and never touches the store's own capacity (which sits about
# five GiB under its cap and burns two and a half a day).
#
# Timed for after the previous day's truth has landed: the Americas commit
# their daily maximum around 04:00-07:00Z, so a pass at 08:10Z sees a
# settled yesterday for the whole fleet rather than two thirds of it.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
. "$PROJECT_DIR/scripts/lib/resolve_project_python.sh"
. "$PROJECT_DIR/scripts/lib/single_instance.sh"
single_instance tiebreak_cell_shadow
PY="$(resolve_project_python "$PROJECT_DIR")" || {
  echo "missing project python runtime" >&2
  exit 1
}

cd "$PROJECT_DIR"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] tiebreak cell shadow refresh"
nice -n 10 "$PY" scripts/refresh_tiebreak_cell_shadow.py --commit
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] done"
