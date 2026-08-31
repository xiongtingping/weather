#!/bin/bash
# Re-judge every station's live standing on a timer, instead of by hand.
#
# The bar and release policy of 2026-08-30 was written to run automatically
# and then ran only when somebody invoked it, which is the same failure the
# correction-review sweep beside it was built to end: a decision that exists
# but is never taken is indistinguishable from one that was never made.  A
# station that went bad stayed live until a person happened to look, and a
# station that recovered stayed barred for the same reason.
#
# Cheap and safe to run often.  It reads two aggregates over the settlement
# and position tables and writes at most a small JSON config -- no SSOT
# writes at all, so it does not contend for the single writer the way the
# other hosted sweeps do.  Hourly matches the cadence of the evidence it
# reads: settlements arrive through the ledger and truth sweeps, and a
# verdict that moves inside an hour is a verdict resting on one settlement.
#
# The blast radius is bounded by design.  Barring a station stops live orders
# and nothing else -- the paper ledger keeps seating and settling it, which
# is how a barred station accumulates the evidence to come back -- the bar is
# capped at fifteen, and stations the operator has ruled on by hand are never
# touched.  What this cannot do is arm anything: live_enabled is a separate
# flag and only a person sets it.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
. "$PROJECT_DIR/scripts/lib/resolve_project_python.sh"
. "$PROJECT_DIR/scripts/lib/single_instance.sh"
single_instance auto_station_exclusions_sweep
PY="$(resolve_project_python "$PROJECT_DIR")" || {
  echo "missing project python runtime" >&2
  exit 1
}

cd "$PROJECT_DIR"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] auto station exclusion sweep"
"$PY" scripts/auto_station_exclusions.py --apply
