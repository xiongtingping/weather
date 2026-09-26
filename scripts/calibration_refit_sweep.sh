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

# Charter 140 (2026-09-16): the settlement labels the claim lanes score their
# evidence against, off the truth lane.  First, so every fit below reads
# today's labels.  Never fatal: a failed pass leaves yesterday's labels, and
# a lane short of labels reads its bands as accumulating.
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] settlement labels"
"$PY" scripts/refresh_settlement_labels.py --commit \
  || echo "settlement labels failed; the lanes read the labels already written" >&2

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
# It rides HERE, after the band refit, because the band table moves every
# claim and the off-centre correction is measured on claims.  (Charter 68's
# lambda refit that used to follow it retired with charter 230.)
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
#
# Charter 150 (2026-09-19): only P0 carries a centre/width correction.  The
# refit retires whatever P0-M and P1 still had and fits nothing for them.
#
# Charter 202 (user ruling 2026-09-23, 「证据时钟不要清零，用旧的样本」): the
# per-stage clock this line used to pass (``--since-stage P0=...09-18T09:18:16Z``,
# charter 149's resident) is gone.  The floor is the LATER of the caller's
# bound and the stage's evidence anchor, so that flag held P0 at five settled
# days after the ruling moved the anchor back to the stable pool; the epoch
# now lives once, in serving_arithmetic.STAGE_EVIDENCE_EPOCH.
"$PY" scripts/refit_center_width.py --commit --summary \
  --since "2026-08-27T09:20:00Z" || RC=$?


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

# Charter 128 (2026-09-12): the beta lane -- one slope per stage on the
# production candidate-window population, walk-forward as-of today,
# by-day bootstrap lower bound.  Shadow: it is recorded on every decision
# and gates nothing until ruled on.  Never fatal.
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] beta edge refit"
"$PY" scripts/refit_beta_edge.py --commit \
  --as-of "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  || echo "beta edge refit failed; the served beta stands" >&2

# Charter 132 (2026-09-13): the valuation pool -- beta in [0, 1] between the
# market's normalised book and the served weather vector, fitted on the truth
# bucket's log score with by-event-day draws.  One stage per invocation so no
# slot exceeds the 300-second budget.  Shadow: activation serves the recorded
# values only; the gate reads them only under PM_VALUATION_MODE=on with an
# admitted walk-forward verdict.  Never fatal.
for VALUATION_STAGE in P0 P0-M P1; do
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] valuation refit $VALUATION_STAGE"
  "$PY" scripts/refit_valuation.py --commit --stage "$VALUATION_STAGE" \
    --as-of "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    || echo "valuation refit $VALUATION_STAGE failed; the served row stands" >&2
done

# L4 posterior lane (charter 230, the basket's one belief): the logit-space
# posterior ``logit m + w (logit p - logit m) + c(m)`` fitted by the multiclass
# log loss on every labelled decision's whole ladder over the rolling window,
# the stage row and its lead-band rows, with the market as prior; each row
# carries its walk-forward verdict and serves only when both scores beat the
# market (else b = m and nothing is bought).  One stage per invocation.
# Never fatal: a failed refit leaves the served row standing.
for POSTERIOR_STAGE in P0 P0-M P1; do
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] posterior refit $POSTERIOR_STAGE"
  "$PY" scripts/refit_posterior.py --commit --stage "$POSTERIOR_STAGE" \
    --as-of "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    || echo "posterior refit $POSTERIOR_STAGE failed; the served row stands" >&2
done

# The regional models' bias calibration (user ruling 2026-09-18, "和全球模型
# 方式一样"): every regional lane, CMA included, refitted daily in the global
# layer's own form -- the production artifact on every settled day and the
# per-event-date replay table beside it.  It writes two JSON files and no
# database, reading the SSOT mode=ro, ~3s.  The serving arm is off by
# measurement (regional_point_debias.FEATURE_CALIBRATION); the fit is kept
# current so it can be re-measured against P1's joint likelihood.
# Never fatal: a failed fit leaves yesterday's files.
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] regional point calibration refit"
"$PY" scripts/fit_regional_point_debias.py --quiet \
  || echo "regional calibration refit failed; yesterday's fit stands" >&2

# The delta-v2 policy re-pin moved to its own lane on 2026-09-17
# (scripts/delta_v2_authority_sweep.sh), beside the weekly fill refit whose
# batches it pins and the renewals that keep both from ending.

# Which listed markets this fleet cannot serve yet.  Read-only, and
# deliberately noisy: discovery ran days ahead of onboarding and the two
# were joined by hand, which is how Istanbul, Moscow and Tel Aviv carried
# resolved stations and full timezones for six days without anyone being
# told.  Not part of RC -- a market nobody has onboarded is not a failure
# of this sweep, and making it one would train the receipt to be ignored.
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] new market discovery"
"$PY" scripts/onboard_station.py --stage discover \
  || echo "market discovery failed; refits above still committed" >&2

# User ruling 2026-09-23 (「到期前端预警，不过期」): a P0 certificate whose
# scope has nothing fresher is renewed inside ten days of its end -- same
# evidence, new window, a receipt the board warns on.  Rules in
# bayes_basket_target/native_ensemble_certificate_clock.py.
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] native ensemble certificate renewal"
"$PY" scripts/renew_native_ensemble_certificates.py --commit \
  || echo "certificate renewal failed; served certificates stand to their end" >&2
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] calibration refit sweep done rc=$RC"
exit "$RC"
