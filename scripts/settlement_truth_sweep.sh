#!/bin/bash
# Materialize every settled station-day whose truth row nobody asked for.
#
# The resident materializes truth too, but it discovers scopes from the
# candidate member-run tables -- 55 station-days as of 2026-08-17, nearly all
# of them today and yesterday.  The days the calibration lane needs are in the
# market registry and the historical member table, and no rotation over the
# candidate tables reaches them.  A day missed had nothing retrying it: on
# 2026-08-17 the truth table held 13 of 45 stations for 08-11 while the
# archive held all 45, and one pass of this script committed 217 station-days
# in five seconds.
#
# Append-only and idempotent: a station-day already written is skipped, so a
# pass with nothing to do writes nothing.  Each scope is its own short
# transaction, so a contradictory day refuses alone instead of taking the
# settled days queued behind it down with it.
#
# Safe beside the resident: the scan is all reads, which in WAL mode do not
# block a writer, and the write lock is held from one INSERT to the commit a
# scope later.  That matters because the resident gives up on a busy database
# after five seconds rather than stall a production cadence -- a maintenance
# writer holding the lock across its own scan does not slow the resident, it
# fail-closes P0 and P1 for that whole cycle.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
. "$PROJECT_DIR/scripts/lib/resolve_project_python.sh"
. "$PROJECT_DIR/scripts/lib/single_instance.sh"
single_instance settlement_truth_sweep
PY="$(resolve_project_python "$PROJECT_DIR")" || {
  echo "missing project python runtime" >&2
  exit 1
}

cd "$PROJECT_DIR"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] settlement truth sweep"
"$PY" scripts/settlement_truth_sweep.py
