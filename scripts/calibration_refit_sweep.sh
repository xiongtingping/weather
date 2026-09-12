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

# The off-centre claim correction, which until 2026-09-09 was a kernel
# constant fitted by hand twice -- once by code that was never written down
# (2026-08-14: no script, no n on the off-centre cells, and taken the day
# before charter 49 removed the population it scored), once on claim-axis
# samples days after P0 had moved to the distance axis.  A constant only a
# person can move does not follow the layer underneath it, and that layer
# refits nightly, right above this line.
#
# It rides HERE, between the band refit and the lambda refit, because that
# is the order the dependency runs in: the band table moves every claim, the
# off-centre correction is measured on claims, and lambda is keyed on
# ``price / claim``.  Fitting it before the band table would measure the
# previous axis; fitting it after lambda would leave lambda keyed on a claim
# the correction has since moved.
#
# Never fatal: a stage with no activated band table, or with too few
# off-centre windows on the current axis, keeps the value it serves and says
# so in its receipt.
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] off-centre claim refit"
"$PY" scripts/refit_off_centre_claim.py --commit \
  --as-of "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  || echo "off-centre claim refit failed; the served table stands" >&2

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
# Charter 86 (2026-09-06 04:18Z, f5f659cb) changed what P0 serves -- the
# climate blend came out of every fitted artifact -- and did not bump
# posterior_version, so the version anchor above still says 2026-08-27 and
# the width fit kept training on the blended shape: three refits
# (09-06T15:48Z, 09-06T23:00Z, 09-08T11:35Z) proposed byte-identical
# per-station scales and the last two were refused forward.  The bound
# below is the moment the shape actually changed, which is earlier than any
# bump made today could say (a bump anchors at its own first serving), so
# the de-blended days already served are kept rather than re-earned.
# P0-M inherits P0's root and moves with it.  Nobody has to retire this: the
# evidence floor is the LATER of the caller's bound and the version anchor, so
# the moment a bump moves P0's anchor past 2026-09-06 this flag stops being
# the maximum and becomes a no-op.  It is a floor under a shape whose version
# never moved, not a date anyone has to maintain.
#
# 2026-09-10: that bump was made.  What made it affordable is that a version
# may now say when its arithmetic began rather than having it inferred from
# the day its string first appears, which is the whole reason the bump had
# been deferred -- see serving_arithmetic.STAGE_VERSION_EFFECTIVE_FROM, where
# P0 v3 and P0-M v3 both name this exact moment.  The two flags below are
# therefore already the no-op the paragraph above predicted, and they stay as
# the fallback for the hours between a deploy and the first envelope that
# carries the new version.  Delete them once that transition is not in living
# memory; the pairing is pinned by tests/test_serving_arithmetic_version_start.
"$PY" scripts/refit_center_width.py --commit --summary \
  --since "2026-08-27T09:20:00Z" \
  --since-stage "P0=2026-09-10T15:27:09Z" \
  --since-stage "P0-M=2026-09-10T15:27:11Z" || RC=$?

# Charter 122 (五): the width band charter 79's symmetric seat is confined
# to.  It rides here rather than being hand-set because the sweep that set
# its first value had to rebuild the whole book per arm -- 28 minutes each,
# and a +-15 USDC interference floor under a +-2 effect.  This lane reads
# the pair the runtime records on every symmetric day instead, so its cost
# is one indexed scan and its noise is the day's own.  Admission-gated on a
# forward window like the layers above; it holds the ruled default until it
# has eight days and sixty pairs, so the first weeks after it lands change
# nothing.
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] symmetric seat band refit"
"$PY" scripts/refit_symmetric_seat_band.py --commit --summary || RC=$?

# Whether any stage owes the version bump the width lanes above key their
# evidence off.  Read-only and deliberately NOT part of RC, for the reason
# the watchdog itself is reported and never enforced (2026-08-27): a check
# that closed the sweep would be the demoted boundary again, and the layer
# it protects would never activate.  It is here so the question is asked
# once a day instead of when somebody goes looking -- which is how charter 86
# changed P0's served width on 2026-09-06 and left its version behind.
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] serving arithmetic watchdog"
"$PY" scripts/acknowledge_serving_arithmetic.py --audit \
  || echo "watchdog audit failed; refits above still committed" >&2

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
# Charter 110: a band axis change mis-keys a settled window, it does not
# kill it.  The price the book quoted and the bucket the day settled to do
# not depend on our probabilities; only the claim does.  So the claim is
# recomputed on the table that serves today and filed under the band it now
# belongs to, and the fit below reads those rows.
#
# Both halves or neither.  The replay lane existed and the refit did not ask
# for it, so 841 hand-run rows sat unread while P0's fit ran on zero served
# rows -- its band axis moved to centre_distance on 2026-09-07 and every
# claim-axis row it had went with it, leaving the served table frozen at a
# vintage nothing could refresh.  Passing --include-replayed alone would
# have wired the daily fit to a hand-made snapshot that stops the day
# somebody last ran it (it stopped at 2026-09-07, two days before this was
# found); running the replay alone changes nothing at all.
#
# Never fatal: a replay that cannot run leaves the fit on served rows, which
# is exactly where it was before this lane existed.
#
# ``--stage all``, not P0.  This call was hard-coded to P0 because P0 was the
# only stage on the distance axis, and the day P1 moved (2026-09-09) that
# implicit coupling would have left P1's lambda reading claim-axis rows for
# a distance-axis table -- the same silence P0 spent two days in.  A
# claim-axis stage is a no-op inside the script, so 'all' costs nothing and
# stops the sweep from needing an edit the next time a stage moves.
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] candidate window replay on the current band"
"$PY" scripts/replay_candidate_windows_on_current_band.py --stage all \
  --commit --as-of "$(date -u +%Y-%m-%dT%H:%M:%SZ)" || {
  echo "candidate window replay: skipped, fit falls back to served rows" >&2
}

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] effective edge lambda refit"
"$PY" scripts/refit_effective_edge_lambda.py --commit --include-replayed || {
  EE_RC=$?
  if [ "$EE_RC" = "3" ]; then
    echo "effective-edge refit: a band has no observations left" >&2
  else
    RC=$EE_RC
  fi
}

# Charter 128 (2026-09-12): the beta lane -- one slope per stage on the
# same candidate-window population lambda reads, walk-forward as-of today,
# by-day bootstrap lower bound.  Shadow: it is recorded on every decision
# and gates nothing until ruled on.  Never fatal.
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] beta edge refit"
"$PY" scripts/refit_beta_edge.py --commit \
  --as-of "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  || echo "beta edge refit failed; the served beta stands" >&2

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

# The ledger's fill simulation, against the only measurement of it there
# is.  Read-only and deliberately not part of RC: a simulation drifting out
# of the shadow lane's observed band is a fact about the ledger, not a
# failure of this sweep, and making it fail the sweep would train the
# receipt to be ignored -- the same reasoning as discovery above.  It is
# here so the gap is re-measured daily instead of when someone remembers.
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] maker fill model calibration"
"$PY" scripts/calibrate_maker_fill_model.py \
  || echo "maker fill calibration failed; refits above still committed" >&2

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] calibration refit sweep done rc=$RC"
exit "$RC"
