#!/bin/bash
# Sweep newly-eligible stage seats into the paper ledger.
#
# The ledger opens a seat the first moment a stage can be entered with a book
# to trade against, so the sweep has to run while the day is running.  It had
# no schedule at all -- cron and launchd both had nothing -- and was only ever
# invoked by hand.  On 2026-08-16 that left three stations (RKPK, CYYZ, KATL)
# holding an eligible P1 moment with a fresh book and no seat, and 52 seats
# across three event days appeared the moment it was finally run.
#
# --backfill is append-only and idempotent: an already-recorded stage scope is
# skipped, so re-running costs a scan and writes nothing.  --rebuild is the
# destructive one and is never used here.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
. "$PROJECT_DIR/scripts/lib/resolve_project_python.sh"
PY="$(resolve_project_python "$PROJECT_DIR")" || {
  echo "missing project python runtime" >&2
  exit 1
}

cd "$PROJECT_DIR"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] paper ledger sweep"
"$PY" scripts/paper_trading_ledger.py --backfill
