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
. "$PROJECT_DIR/scripts/lib/single_instance.sh"
single_instance paper_trading_ledger_sweep
PY="$(resolve_project_python "$PROJECT_DIR")" || {
  echo "missing project python runtime" >&2
  exit 1
}

cd "$PROJECT_DIR"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] paper ledger sweep"
"$PY" scripts/paper_trading_ledger.py --backfill

# The render moved out (2026-08-29).  It lived here from 2026-08-16 so the
# page's age would be bounded by a timer, but a render is a reader that had
# grown to twenty minutes, and it was holding a WRITER slot: with ten-plus
# hosted members the queue starved production for two hours on 2026-08-28,
# and three "上轮 N 小时前" alarms in two days were this sweep waiting for
# a turn, not the ledger being stuck.  com.weather.dashboard_render (its
# own launchd timer, single_instance "dashboard_render") owns the page
# now; the no-clobber write in the renderer keeps any overlap harmless.

# The publishable copy, kept in step with the local page.  Building it here
# rather than at publish time means the shareable file is never a stale
# vintage of the board: whoever republishes picks up whatever the last
# render produced.  Publishing itself stays manual (the Artifact tool) --
# a sweep that pushed a public page every fifteen minutes would be
# republishing the user's positions on a timer nobody asked for.
"$PY" scripts/publish_dashboard_artifact.py \
  || echo "artifact copy failed; local dashboard above is still current" >&2
