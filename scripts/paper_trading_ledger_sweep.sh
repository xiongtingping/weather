#!/bin/bash
# Sweep newly-eligible stage seats into the paper ledger.
#
# The ledger dates a seat from the first stage moment that had a book, all of
# it read out of stored history, so a late sweep still records the right seat
# at the right price -- what a missing sweep costs is a page that does not
# know about it.  It had no schedule at all: cron and launchd both had
# nothing, and it was only ever invoked by hand.  On 2026-08-16 that hid 52
# seats across three event days until someone finally ran it.
#
# --backfill is append-only and idempotent; an already-seated stage scope is
# skipped, so a pass with nothing to do writes nothing.  --rebuild is the
# destructive one and is never used here.
#
# Safe to run beside the resident: the half-minute scan is all reads, which in
# WAL mode do not block a writer, and every insert is buffered into one short
# burst at the end.  That matters because the resident deliberately gives up
# on a busy database after five seconds rather than stall a production cadence
# -- so a maintenance writer that holds the lock across its own scan does not
# slow the resident down, it fail-closes P0 and P1 for that whole cycle.

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

# Render the page from what the sweep just landed.  The dashboard had no
# schedule of its own either, so it only ever showed whatever moment someone
# last rendered it by hand -- on 2026-08-16 that meant reading "最新 run 12
# 小时前" for an hour after the models had actually advanced, which is
# indistinguishable from the lane still being stuck.  Rendering here keeps the
# page's age bounded by this timer rather than by when anyone last looked.
#
# Non-fatal: the ledger write above is the thing worth protecting, and a
# render that fails must not make the sweep look like it did not run.
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] dashboard render"
"$PY" scripts/calibration_dashboard.py --html-out tmp/cal_dashboard.html \
  || echo "dashboard render failed; ledger sweep above still committed" >&2
