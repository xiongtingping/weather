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
# The transition floor for the 2026-08-27 ruling that made posterior_version
# the evidence boundary and the AST fingerprint a watchdog.  It has to be
# stated once: P0 has served version v1 since 2026-07-30 while its arithmetic
# moved several times without a bump, so the content anchor alone would reach
# back across shapes nobody serves -- the exact failure the old bound existed
# to prevent.  Every stage therefore starts its eight days here, and from
# this moment on only a version bump moves the floor.  This constant is
# deliberately not maintained: it stops mattering as soon as each stage's
# posterior_version moves past it.
"$PY" scripts/refit_center_width.py --commit --summary \
  --since "2026-08-27T09:20:00Z" || RC=$?

# Shadow only: station x bucket-position band factors, fitted on the same
# serving lane as the pooled table above and served by nothing.  It rides
# this sweep so its evidence clock advances on the same cadence as the
# layer it is a candidate to refine -- the two are only comparable if
# they see the same settled days.  Measured at ~95s against the live
# store on 2026-08-28, which the 900s sweep budget absorbs.
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] band_shadow refit (shadow, unserved)"
"$PY" scripts/refit_band_shadow.py --commit --summary || RC=$?

# The delta-v2 policies pin the fill batch they were trained to execute
# against, and the pin goes stale the day a fill refit mints a new batch
# -- measured 2026-08-22..28, when every candidate reaching the execution
# compile died on the stale pin for six days.  Re-pinning is idempotent
# (a current pin is a skip) and append-only (generation + 1), so riding
# this sweep means a batch change is followed within a day instead of
# waiting for someone to notice the mismatch counter.  RKPK's own fill
# rows exist only in the placeholder batch expiring 2026-08-31: on that
# day its finalist falls to the '*' batch and this sweep re-pins it on
# the next run -- at most one day of refusals, named here so the gap
# reads as the designed cost rather than a mystery.
# Charter 68's lambda table, refitted on the rolling settled window.  The
# rule shrinks a claim by the fraction history says arrives at its market
# agreement, and a fitted constant goes stale the moment the selection
# distribution moves -- which charter 68 itself moves.  A band short of
# evidence keeps its incumbent value rather than being refitted on whatever
# few seats landed there, so committing cannot serve a number the admission
# rule did not clear.
#
# Exit 3 means a band has gone blind: no seats at all inside the window, so
# the number governing it is frozen and nothing on the book can refresh it.
# That is a human's call (deliberately sample the band, or accept the
# freeze), not a reason to fail the sweep the other refits ride on.
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] effective edge lambda refit"
"$PY" scripts/refit_effective_edge_lambda.py --commit || {
  EE_RC=$?
  if [ "$EE_RC" = "3" ]; then
    echo "effective-edge refit: a band has no observations left" >&2
  else
    RC=$EE_RC
  fi
}

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] delta_v2 policy re-pin"
"$PY" scripts/repin_delta_v2_policy_to_current_fill.py --commit || RC=$?

# Which listed markets this fleet cannot serve yet.  Read-only, and
# deliberately noisy: discovery ran days ahead of onboarding and the two
# were joined by hand, which is how Istanbul, Moscow and Tel Aviv carried
# resolved stations and full timezones for six days without anyone being
# told.  Not part of RC -- a market nobody has onboarded is not a failure
# of this sweep, and making it one would train the receipt to be ignored.
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] new market discovery"
"$PY" scripts/onboard_station.py --stage discover \
  || echo "market discovery failed; refits above still committed" >&2

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] calibration refit sweep done rc=$RC"
exit "$RC"
