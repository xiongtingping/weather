#!/bin/bash
# Daily refit of P0-M's TAF likelihood and its walk-forward serve gate
# (charter 150; its own lane since 2026-10-12, when it ran last in
# p1_likelihood_refit_sweep.sh and was killed by that lane's timeout after
# the P1 steps had spent it).  ~20 s with the case cache warm.
#
# Reads the SSOT read-only and writes its case cache and one artifact under
# data/runtime, no shared database ("none" in HOSTED_SWEEP_WRITE_DOMAIN).
# The artifact is replaced atomically and the serving loader follows it by
# modification time; a failed refit leaves yesterday's file serving.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
. "$PROJECT_DIR/scripts/lib/resolve_project_python.sh"
. "$PROJECT_DIR/scripts/lib/single_instance.sh"
single_instance taf_likelihood_refit_sweep
PY="$(resolve_project_python "$PROJECT_DIR")" || {
  echo "missing project python runtime" >&2
  exit 1
}

cd "$PROJECT_DIR"
THROUGH="$("$PY" -c "
from datetime import datetime, timezone
print(datetime.now(timezone.utc).date().isoformat())")"

# Charter 281 补记一: the line is fitted on the regression's roots -- the
# rebuilt history plus the roots served since (copied first, each day).
REPLAY="data/runtime/bayes_basket_target_nwp/p0_ngr_root_replay.db"
"$PY" scripts/build_p0_ngr_root_replay.py --live || echo "root replay copy failed; fitting on the store as it was" >&2

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] P0-M TAF likelihood refit through $THROUGH"
"$PY" scripts/fit_taf_likelihood.py --through "$THROUGH" --root-replay "$REPLAY"
RC=$?
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] done rc=$RC"
exit "$RC"
