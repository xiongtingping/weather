#!/bin/bash
# Keep the delta-v2 execution authority servable: the fill batch and the
# policy generations every candidate resolves.
#
# Both carry validity windows the resolvers enforce, and until 2026-09-17
# both were minted with thirty days by scripts nothing ran on a clock: the
# fill batch fitted 08-22 would have ended 09-21 and the policies 09-27 and
# 09-30, each end refusing every candidate at every station.  The rules are
# in bayes_basket_target/delta_v2_authority_clock.py; this is the clock.
#
# Daily, three steps, in dependency order:
#   1. refit the fill model from the maker shadow book -- weekly, gated on the
#      newest batch's start, and handed off to the policies at one instant;
#   2. renew any served fill batch inside sixty days of its end (the backstop
#      when the refit cannot run: same numbers, new year);
#   3. re-pin and renew the policies (a pin that went stale, or a window
#      inside sixty days of its end).
#
# The refit reads ~5M shadow rows and writes ~160k: a few minutes once a
# week, which is why this is its own lane and not a step of the 900-second
# calibration refit.  Every other day it is three indexed reads and a re-pin
# check.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
. "$PROJECT_DIR/scripts/lib/resolve_project_python.sh"
. "$PROJECT_DIR/scripts/lib/single_instance.sh"
single_instance delta_v2_authority_sweep
PY="$(resolve_project_python "$PROJECT_DIR")" || {
  echo "missing project python runtime" >&2
  exit 1
}

cd "$PROJECT_DIR"
RC=0

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] delta_v2 fill refit (weekly)"
"$PY" scripts/fit_delta_v2_fill_from_maker_shadow.py --commit \
  --if-older-than-days 7 || RC=$?

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] delta_v2 fill renewal"
"$PY" scripts/renew_delta_v2_fill_batch.py --commit || RC=$?

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] delta_v2 policy re-pin / renewal"
"$PY" scripts/repin_delta_v2_policy_to_current_fill.py --commit || RC=$?

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] delta_v2 authority sweep done rc=$RC"
exit "$RC"
