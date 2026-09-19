#!/bin/bash
# Daily refit of the two stage likelihoods (charter 150, 2026-09-19).
#
#   1. rebuild the P1 observation samples over a rolling 45-day window
#      (scripts/build_metar_phase_hazard_samples.py, regional features read
#      the way serving reads them);
#   2. refit P1's joint observation likelihood g(d | s) and its per-hours-bin
#      tempering alpha, behind the fitter's own veto (the refit must beat the
#      root under the hard bound, and must not lose to the incumbent, on a
#      held-out day split -- a vetoed refit leaves the served file alone),
#      and, in the same file, charter 161's directly fitted update behind its
#      own forward gate (it serves in g**alpha's place while that gate holds);
#   3. refit P0-M's TAF likelihood and its walk-forward serve gate.
#
# Reads the SSOT and the archive read-only and writes three files, so it
# holds no database lock ("none" in HOSTED_SWEEP_WRITE_DOMAIN).  The served
# files are replaced atomically and the serving loaders follow them by
# modification time, so a refit takes effect on the next compile without a
# restart -- and a failed step leaves yesterday's file serving.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
. "$PROJECT_DIR/scripts/lib/resolve_project_python.sh"
. "$PROJECT_DIR/scripts/lib/single_instance.sh"
single_instance p1_likelihood_refit_sweep
PY="$(resolve_project_python "$PROJECT_DIR")" || {
  echo "missing project python runtime" >&2
  exit 1
}

cd "$PROJECT_DIR"
RC=0
OUT="data/runtime/bayes_basket_target_nwp"
THROUGH="$("$PY" -c "
from datetime import datetime, timezone
print(datetime.now(timezone.utc).date().isoformat())")"
SINCE="$("$PY" -c "
from datetime import date, timedelta
print((date.fromisoformat('$THROUGH') - timedelta(days=44)).isoformat())")"

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] P1 observation samples $SINCE..$THROUGH"
if "$PY" scripts/build_metar_phase_hazard_samples.py \
    --from "$SINCE" --through "$THROUGH" \
    --out "$OUT/p1_observation_samples.db.new"; then
  mv "$OUT/p1_observation_samples.db.new" "$OUT/p1_observation_samples.db"
else
  RC=1
  rm -f "$OUT/p1_observation_samples.db.new"
  echo "sample build failed; the refit below reads yesterday's samples" >&2
fi

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] P1 joint observation likelihood refit"
"$PY" scripts/fit_p1_observation_likelihood.py \
  --since "$SINCE" --through "$THROUGH" || RC=$?

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] P0-M TAF likelihood refit"
"$PY" scripts/fit_taf_likelihood.py --through "$THROUGH" || RC=$?

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] done rc=$RC"
exit "$RC"
