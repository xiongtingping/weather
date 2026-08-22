#!/bin/bash
# Build and stage the PR12 AWC observation facts for the live window.
#
# P1's atomic root is the observation contract and its lineage must carry the
# revision hash, so a report that has not been canonically identified cannot
# be traded on.  The lane that identifies them was never scheduled: it ran by
# hand, last on 2026-08-05, and the facts stopped on 2026-07-31.  Running it
# on a timer is what keeps the live stage rooted.
#
# Read-only against the canonical archive; the only write is the single
# public PR12 seam into the target SSOT.  Places no orders.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
. "$ROOT/scripts/lib/single_instance.sh"
single_instance pr12_awc_observation_sweep
PY="$ROOT/.venv/bin/python"
PROJECTION="$ROOT/output/audits/pr12_authority_scope_projection_current_20260805T120000Z.json"
WORK="$ROOT/data/runtime/pr12_awc_observation"
AUDIT="$ROOT/output/audits"
mkdir -p "$WORK" "$AUDIT" "$ROOT/data/logs"

if [ ! -x "$PY" ]; then
  echo "$(date '+%F %T') [pr12] missing venv python at $PY" >&2
  exit 78
fi
if [ ! -f "$PROJECTION" ]; then
  echo "$(date '+%F %T') [pr12] missing authority projection: $PROJECTION" >&2
  exit 78
fi

NOW="$("$PY" -c "from datetime import datetime,timezone;print(datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z'))")"

"$PY" "$ROOT/scripts/build_pr12_awc_observation_contract.py" \
  --archive-database "$ROOT/data/archive.db" \
  --target-database "$ROOT/data/bayes_basket_target_ssot.db" \
  --as-of-utc "$NOW" \
  --authority-projection "$PROJECTION" \
  --window-days 2 \
  --evidence-output "$WORK/evidence_latest.json" \
  --contract-output "$WORK/contract_latest.json" \
  --receipt-output "$WORK/builder_receipt_latest.json"

"$PY" "$ROOT/scripts/stage_pr12_awc_observation_contract.py" \
  --contract "$WORK/contract_latest.json" \
  --target-database "$ROOT/data/bayes_basket_target_ssot.db" \
  --receipt "$AUDIT/pr12_awc_observation_stage_latest.json" \
  --apply

echo "$(date '+%F %T') [pr12] staged as_of=$NOW"
