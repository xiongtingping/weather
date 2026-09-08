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
# The project interpreter, through the resolver every other scheduled
# script here uses.  Bare ``python3`` is not it under launchd: this job's
# PATH puts /opt/homebrew/bin first, and the Homebrew 3.13 there has never
# had httpx, which ``bayes_basket_target`` imports at package import time.
# So every scheduled run since 2026-08-26 echoed its start line and then
# died on ModuleNotFoundError -- thirteen days in which the only refit
# that happened was one somebody ran by hand on 09-06.  The failure is
# silent by construction: the artifact does not expire, it just keeps
# serving the curve it was last fitted with.  Falling back to whatever
# PATH offers would reintroduce exactly that; the resolver's second
# choice is the Framework build, which carries the dependencies.
. "$ROOT/scripts/lib/resolve_project_python.sh"
PY="${PYTHON:-$(resolve_project_python "$ROOT")}" || {
  echo "no project python (.venv or Framework 3.13)" >&2
  exit 1
}
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
