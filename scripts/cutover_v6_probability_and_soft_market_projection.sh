#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

CONFIRMATION="${1:-}"
EXPECTED_CONFIRMATION="CUTOVER_V6_AND_ACTIVATE_SOFT_MARKET_PROJECTION"

if [ "$CONFIRMATION" != "$EXPECTED_CONFIRMATION" ]; then
  echo "exact confirmation required: $EXPECTED_CONFIRMATION" >&2
  exit 2
fi

# The runtime hotfix owns its short local-APFS rollback window and service
# pause.  It never probes or depends on an external archive disk.
bash scripts/cutover_physical_state_bayes_v6_runtime_hotfix.sh

# Bind market context to the latest committed v6 physical target rather than
# to a wall-clock value captured before the maintenance window.
EVENT_DATE="$(python3 - <<'PY'
import sqlite3
with sqlite3.connect('file:data/archive.db?mode=ro', uri=True) as conn:
    row = conn.execute(
        """SELECT substr(target_identity,-10) FROM posterior_checkpoints
           WHERE checkpoint_probability_schema='posterior_checkpoint_v4_physical_state_bayes_v6'
             AND state='committed'
           ORDER BY committed_at DESC,rowid DESC LIMIT 1"""
    ).fetchone()
if row is None or len(str(row[0] or '')) != 10:
    raise SystemExit('latest_committed_v6_event_date_missing')
print(row[0])
PY
)"

# Rebuild date-bound market authority only after the exact v6 epoch is active,
# so a command prepared before midnight cannot expose a stale market context.
python3 scripts/prepare_operational_bridge_market_release.py \
  --event-date "$EVENT_DATE" \
  --apply

python3 scripts/production_reported_x_market_no_order_drill.py \
  --db data/archive.db \
  --bridge-activation data/candidates/resolution_bridge_production_activation.json \
  --market-context-activation data/candidates/production_probability_v6_market_context_activation.json \
  --event-date "$EVENT_DATE" \
  --output data/candidates/production_probability_v6_market_no_order_drill.json

python3 scripts/activate_operational_bridge_market_release.py \
  --db data/archive.db \
  --event-date "$EVENT_DATE" \
  --apply \
  --confirm ACTIVATE_SAMPLE_FREE_OPERATIONAL_X_S_B_SOFT_PROJECTION

python3 scripts/production_probability_v6_readiness.py \
  --db data/archive.db \
  --summary

python3 scripts/production_reported_x_market_readiness.py \
  --db data/archive.db \
  --event-date "$EVENT_DATE" \
  --no-order-receipt data/logs/production_probability_v6_market_no_order_drill.json

echo "cutover_state=v6_probability_and_soft_market_projection_active"
echo "dynamic_three_leg_live=false"
echo "next_boundary=wait_for_next_canonical_observation_then_validate_ssot_market_projection"
echo "external_disk_required=false"
