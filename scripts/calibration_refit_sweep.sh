#!/bin/bash
# Daily refit of the two served calibration layers: band reliability and
# centre/width.  Hosted by the resident's sweep scheduler (2026-08-26) --
# these two segments used to live only in the 02:00 crontab entry
# (daily_calibration_training.sh), and macOS cron silently skips a run
# when the machine sleeps through the minute: the band fit journal shows
# no cron round on 2026-08-16 or 2026-08-19 -- two silent losses in ten
# days -- and a missed round stalls the evidence clocks this repo's ETA
# math leans on.  A hosted sweep becomes due instead of scheduled, so a
# missed minute is caught up on the next idle gap.
#
# Both refits are content-addressed and admission-gated: rerunning with
# unchanged evidence commits nothing (UNCHANGED), so an overlap with a
# manual run stays harmless.  The Platt/Quote training segments remain
# in the cron script -- they have no live consumer and their ~25-minute
# runtime would starve the production cycle behind the hosted mutex.
#
# Exit code: non-zero if either refit fails, so the hosted receipt in
# status.json reads "failed" rather than burying a broken layer in a
# green light.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
. "$PROJECT_DIR/scripts/lib/resolve_project_python.sh"
. "$PROJECT_DIR/scripts/lib/single_instance.sh"
single_instance calibration_refit_sweep
PY="$(resolve_project_python "$PROJECT_DIR")" || {
  echo "missing project python runtime" >&2
  exit 1
}

cd "$PROJECT_DIR"
RC=0

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] band_reliability refit"
"$PY" scripts/refit_band_reliability.py --commit --summary || RC=$?

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] center_width refit"
# --since is the pre-AST-fingerprint fallback bound, same value the cron
# segment carries; the serving-arithmetic fingerprint supersedes it per
# stage once each stage has 8 days under the automatic anchor.
"$PY" scripts/refit_center_width.py --commit --summary \
  --since "2026-08-18T09:20:00Z" || RC=$?

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] calibration refit sweep done rc=$RC"
exit "$RC"
