#!/bin/bash
# Refit the cooling-side ceiling once a day.
#
# The serving artifact refuses every event date at or before its own
# trained_through, so a curve that stops being refitted stops applying:
# it goes inert one day at a time rather than failing loudly.  The cutoff is
# held one day behind the newest settled day so that today is servable while
# no day is ever scored against a curve its own data helped fit.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY="${PYTHON:-python3}"
OUT="$ROOT/data/runtime/bayes_basket_target_nwp"
STAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Yesterday in UTC: the newest day whose observations are complete enough to
# fit on, and one day behind today so today still passes the leakage gate.
THROUGH="$($PY -c "
from datetime import datetime, timedelta, timezone
print((datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat())")"

echo "[$STAMP] metar ceiling refit, trained_through=$THROUGH"

# Serving artifact first: production reads this one.
"$PY" scripts/fit_metar_ceiling_artifact.py \
  --through "$THROUGH" \
  --out "$OUT/metar_ceiling_artifact.json.new"
mv "$OUT/metar_ceiling_artifact.json.new" "$OUT/metar_ceiling_artifact.json"

# Replay table second: the paper ledger reads this one.  Non-fatal, because
# the serving artifact above is what live pricing depends on.
"$PY" scripts/fit_metar_ceiling_artifact.py --replay-table \
  --out "$OUT/metar_ceiling_replay_table.json.new" \
  && mv "$OUT/metar_ceiling_replay_table.json.new" \
        "$OUT/metar_ceiling_replay_table.json" \
  || echo "replay table refit failed; serving artifact above still written" >&2

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] metar ceiling refit done"
