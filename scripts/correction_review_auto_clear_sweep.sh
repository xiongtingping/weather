#!/bin/bash
# Clear pending METAR correction halts by standing policy, on a timer.
#
# A corrected report (``COR``) raises a halt that
# ``assert_station_day_corrections_resolved`` will not lift without a review
# receipt, and the P1 reader asserts that gate on every cycle.  The policy
# that resolves it -- CLEAR_WITH_REPLACEMENT for an on-hour report, otherwise
# CLEAR_REFERENCE_ONLY -- was written in August but only ever ran by hand, so
# in between a single COR wedged its station's P1 until somebody noticed.
# Measured 2026-08-26: RKPK took an on-hour COR at 08:00Z, and P1 for the
# station had been fail-closed on ``correction_review_required`` ever since.
#
# What this schedules is the decision, not a weakening of it.  A correction
# that genuinely contradicts another report at the same valid time still
# refuses -- ``correction_review_replacement_contradicts_peer_report`` leaves
# it pending and visible -- and a human review still supersedes any receipt
# this writes.
#
# The window is rolling and short: the halts worth clearing on a timer are the
# ones holding the live day, and older ones are a maintenance decision that
# should be made deliberately with the script's own --since.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
. "$PROJECT_DIR/scripts/lib/resolve_project_python.sh"
. "$PROJECT_DIR/scripts/lib/single_instance.sh"
single_instance correction_review_auto_clear_sweep
PY="$(resolve_project_python "$PROJECT_DIR")" || {
  echo "missing project python runtime" >&2
  exit 1
}

cd "$PROJECT_DIR"
SINCE="$("$PY" -c "from datetime import date,timedelta;print(date.today()-timedelta(days=3))")"
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] correction review auto-clear sweep since=$SINCE"
"$PY" scripts/auto_clear_pending_correction_reviews.py --since "$SINCE" --commit
