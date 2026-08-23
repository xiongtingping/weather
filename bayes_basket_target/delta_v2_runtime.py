"""Pure production adapter for the unique DeltaBasketScore v2 objective.

The adapter deliberately reuses only the probability/support diagnostics from
the basket state machine.  It never calls the v1 optimizer or strict-delta
authorization path.  Every target and action candidate is bounded by the
activated v2 artifact, official WS depth, confirmed inventory/order state and
the caller-provided fill-distribution authority.
"""

from __future__ import annotations

import os
from itertools import product
import math
from typing import Any, Callable, Mapping, Sequence

from .basket_kernel.contracts import (
    EPS,
    ContractError,
    Direction,
    TargetBasketPolicy,
    TargetBasketSnapshot,
    content_sha256,
)
from .basket_kernel.delta_v2 import (
    dispersion_support_size,
    off_center_claim_factor,
    probability_core,
    PROBABILITY_CORE_MASS,
    SUPPORT_EDGE_FRICTION_TICKS_PER_LEG,
    CANDIDATE_KIND_BASELINE,
    CANDIDATE_KIND_COMPLETE,
    CANDIDATE_KINDS,
    CURVE_OBJECTIVE_FORM,
    DEFAULT_KELLY_FRACTION,
    VALUE_OBJECTIVE_FORM,
    evaluate_action_v2,
    evaluate_basket_value_v2,
    select_action_v2,
    select_complete_maker_bundle_v2,
    solve_reservation_price_v2,
    solve_target_inventory_v2,
)
from .basket_kernel.support import (
    _choose_support,
    _derive_dual_risk_scope,
    probability_center_diagnostics,
)
from .basket_kernel.state_machine import resolve_basket_state
from .delta_v2_artifact import DeltaV2PolicyArtifact
from .shadow_objective import solve_fractional_kelly


SCHEMA = "delta_basket_score_runtime_plan_v2"

# A margin on the whole-basket edge, above tick friction (2026-08-23).
# The friction term only removes quantisation noise, so the floor has been
# effectively zero -- and it could afford to be, because ``win`` under-claimed
# and that under-claim was doing the work of a margin.  Measured on 17 forward
# days at P0: claimed 73.2% against 83.9% realised, a hidden 10.7 points of
# safety.  A calibration that makes ``win`` honest removes that safety without
# replacing it, and the marginal baskets it had been excluding turn out to
# lose: the centre/width scalars opened 37 extra chains and took the book from
# +41.4% to +4.3%.  The margin is what makes the floor mean the same thing
# after the claim stops under-claiming.  Zero keeps the pre-existing behaviour
# byte-identical.
SUPPORT_EDGE_MARGIN = float(
    os.environ.get("PM_SUPPORT_EDGE_MARGIN", "") or 0.0
)
# P1 is the converging stage: the ladder is allowed to walk down 4 -> 3 -> 2
# as the day resolves, and stops at two because the second rung is the tail
# one still carrying the insurance (user ruling, 2026-08-16).  The opening
# stages keep the policy's three -- see clause 50.
P1_MINIMUM_SUPPORT_BUCKETS = 2
MAKER_EXECUTION_CONTRACT = "maker_post_only"
FOK_EXECUTION_CONTRACT = "single_bucket_fok"
FillResolver = Callable[..., Mapping[str, Any]]


def _floor_to_tick(price: float, tick: float) -> float:
    steps = math.floor((price + EPS) / tick)
    return round(steps * tick, 12)


# The model's own ceiling on a hedge rung's price, as a fraction of what
# the model says that rung is worth.  The centre rung is exempt: it is the
# position, not the insurance around it, and refusing to pay the market for
# it is refusing the trade.
#
# This existed only in the paper ledger, which is where every ROI number in
# this system comes from, so production was trading the one setting the
# ledger measured worst.  The ledger's three-cell sweep, 2026-08-21:
# lambda 0.53 gave -6.9%, lambda 0.75 gave -10.6%, and exempting the hedge
# rungs entirely -- which is what production was doing by having no ceiling
# at all -- gave -15.3%, the worst of the three, even though it lifted the
# hit rate from 25.8% to 36.1%.  Hit rate is purchasable; the odds are not.
MAKER_HEDGE_VALUE_LAMBDA = float(
    os.environ.get("PM_MAKER_HEDGE_VALUE_LAMBDA", "0.53") or 0.0
)
# The shoulder -- the rung one bucket either side of the centre -- gets its
# own, tighter ceiling.
#
# The clamp had only ever had two settings, centre-exempt and
# everything-else-at-lambda, so the shoulder was priced as insurance
# although it is the centre's nearest neighbour and carries two thirds of
# the clamped rungs (320 of 476 posted non-centre legs sit one bucket out).
# Swept alone on the ledger, 2026-08-22, eight cells:
#
#     0.20  +15.4%    0.46  +15.7%    0.85   -1.2%
#     0.30  +18.2%    0.53  +12.7%    exempt -17.3%
#     0.40  +18.9%    0.70   +3.0%
#
# Single-peaked with both flanks monotone, so 0.40 is an interior optimum
# rather than a boundary.  It is +6.2 points of ROI over pricing the
# shoulder like a tail, and the direction is the opposite of the intuition
# that motivated the sweep: the shoulder wanted a *tighter* ceiling, not a
# looser one.  Exempting it buys nine points of hit rate (26% -> 35%) and
# loses thirty of ROI, which is the sixth time this system has measured
# that same trade.
MAKER_SHOULDER_VALUE_LAMBDA = float(
    os.environ.get("PM_MAKER_SHOULDER_VALUE_LAMBDA", "0.40") or 0.0
)
# Do not open into a market that has finished pricing the day.
#
# Two signs, read at the moment of entry: the mass centre has not moved
# over the last CERTAINTY_STABLE_LOOKBACK decisions, and the distribution's
# top two buckets already carry CERTAINTY_SHARP_TOP2 of the mass.  Either
# one lit means our own evidence and the market's have converged, and a
# converged market has already collected the premium -- there is nothing
# left in the price for us.
#
# Measured on the settled book by rebuilding the ledger under each gate:
#
#     gate off (shipped)   ROI +18.9%   hit 25%   ex-best-two  -8.7%
#     stable only          ROI +37.0%   hit 28%   ex-best-two  +8.1%
#     sharp only           ROI +35.0%   hit 29%   ex-best-two  +8.4%
#     both                 ROI +47.5%   hit 33%   ex-best-two +18.5%
#
# Three things move the right way at once, which nothing else measured in
# this system has done: the return, the hit rate, and the robustness --
# the shipped rule needs its best two days to stay positive and the gated
# one does not.  Stake is nearly unchanged (1374u -> 1314u); the seats fall
# from 388 to 182 because the same money moves off the days it was losing
# on.  A skipped cycle is not a skipped day: the walk goes on to the next
# decision, so the position opens later, at a moment the market is less
# sure of.
CERTAINTY_GATE_SIGNALS = tuple(
    part.strip() for part in
    (os.environ.get("PM_CERTAINTY_GATE", "stable,sharp") or "").split(",")
    if part.strip()
)
CERTAINTY_STABLE_LOOKBACK = 3
CERTAINTY_SHARP_TOP2 = 0.60


def certainty_signals(
    *,
    probabilities: Mapping[str, float],
    prior_centre_buckets: Sequence[str] = (),
) -> dict[str, bool]:
    """Which certainty signs are lit for this entry.

    ``prior_centre_buckets`` is the argmax bucket of each earlier decision
    on the same station-day, newest first.  Fewer than the lookback means
    the day has not produced enough readings to call the centre stable,
    which is the honest answer rather than a default of True.
    """

    if not probabilities:
        return {"stable": False, "sharp": False}
    centre = max(probabilities, key=lambda bucket: probabilities[bucket])
    recent = [str(centre), *(str(item) for item in prior_centre_buckets)]
    stable = (
        len(recent) >= CERTAINTY_STABLE_LOOKBACK + 1
        and len(set(recent[: CERTAINTY_STABLE_LOOKBACK + 1])) == 1
    )
    top2 = sum(sorted(probabilities.values(), reverse=True)[:2])
    return {"stable": stable, "sharp": top2 >= CERTAINTY_SHARP_TOP2}
# Whether the continuous log-optimum shape is offered alongside the
# centre-anchored one.  It is the correct Kelly split for a basket whose
# quantities are free, and it is off.
#
# Measured 2026-08-22 by rebuilding the paper ledger on each allocation with
# the seat rule held identical on both sides, so the only variable is the
# shape of the money inside the seated window:
#
#     centre-anchored tilt   ROI +12.7%   hit 26%   (the ledger's shape)
#     continuous log optimum ROI  -9.4%   hit 28%   (production's shape)
#
# 275 USDC apart over 187 paired station-days, and the same trade as every
# other measurement in this system: two points of hit rate bought with
# twenty of ROI.  The optimum spreads money toward the cheap outer rungs
# because that is where its own concavity says the curve flattens most per
# dollar; the tilt keeps the money on the centre, and the centre is the
# rung this model is actually reliable about.
#
# Left as a switch rather than deleted: the shape is not wrong, it is wrong
# *at this bankroll and these fills*, and the generator is the only thing
# that can express a split the clip grid cannot.
CONTINUOUS_OPTIMUM_CANDIDATES_ENABLED = (
    os.environ.get("PM_DELTA_V2_CONTINUOUS_OPTIMUM", "0").strip()
    not in {"0", "false", "FALSE", "no", ""}
)


def _hedge_value_cap(
    *,
    bucket: str,
    centre_bucket: str,
    probability: float | None,
    window: Sequence[str] | None = None,
) -> float | None:
    """The value ceiling for one rung, or None when it is uncapped.

    Kept as one function so the ledger's ``_leg_value_cap`` and this cannot
    drift into two different definitions of the same red line.
    """

    if MAKER_HEDGE_VALUE_LAMBDA <= 0.0:
        return None
    if centre_bucket and str(bucket) == str(centre_bucket):
        return None
    if probability is None:
        return None
    value = float(probability)
    if not math.isfinite(value) or value <= 0.0:
        return None
    lam = MAKER_HEDGE_VALUE_LAMBDA
    if window and centre_bucket:
        ordered = [str(item) for item in window]
        if str(bucket) in ordered and str(centre_bucket) in ordered:
            distance = abs(
                ordered.index(str(bucket)) - ordered.index(str(centre_bucket))
            )
            if distance == 1 and MAKER_SHOULDER_VALUE_LAMBDA > 0.0:
                lam = MAKER_SHOULDER_VALUE_LAMBDA
    return lam * value


# How many rungs a leg is offered across, and how far inside the bid the
# cheapest one sits.  Both mirror the paper ledger's ``_resting_ladder``,
# which is the lifecycle every measured ROI figure in this system was
# produced under -- and the ledger now takes this number from here, so the
# two are the same by construction rather than by agreement.
#
# Eight, not five.  A single-variable sweep over one 186-position book
# (2026-08-22) measured the rung count as the only lever that raises the
# fill rate without paying for it:
#
#   rungs   share fill   ROI
#     1        73.1%    +37.0%     <- what production posted before 31a68f52
#     3        76.2%    +39.8%
#     5        77.6%    +40.0%
#     8        79.0%    +40.0%
#
# ROI plateaus at five and the fill rate keeps climbing, so eight buys 1.4
# points of fill for nothing.  Past that it stops being free: twelve rungs
# with a faster convergence reaches 81.6% but gives back 5.5 points of ROI.
#
# Everything else that looks like a fill-rate lever is not one.  Raising the
# value clamp from 0.53 to 1.00 moves the fill rate by -0.5 points and costs
# 8 points of ROI -- the unfilled legs are not unfilled because the price is
# too low.  Re-pricing more often is worse, not better (60/30/15 minutes:
# 77.6/76.4/71.0), because a re-post loses queue position.  Releasing the
# shoulder clamp is the worst of all: 67.0% fill and -5.3% ROI, with the hit
# count *up*, which is the same fingerprint this system has now measured ten
# times -- the hit rate is purchasable and the odds are not.
MAKER_LADDER_STEPS = int(
    os.environ.get("PM_MAKER_LADDER_STEPS", "8") or 8
)
MAKER_LADDER_IMPROVE_TICKS = int(
    os.environ.get("PM_MAKER_LADDER_IMPROVE_TICKS", "1") or 1
)


def _rung_offset_ticks(*, price: float, bid: float, tick: float) -> float:
    """Where a limit stands in the book, in ticks above the bid.

    This is the variable that separates a ladder's rungs, and the one the
    fill artifact conditions on.  Measured over the maker shadow book at a
    60 minute horizon: at the bid 12.1%, one tick inside 17.1%, two ticks
    20.1%, mid 20.9%, at the ask 88.3%.
    """

    if tick <= 0.0:
        return 0.0
    return round((float(price) - float(bid)) / float(tick), 6)


def _maker_ladder(
    *,
    bid: float,
    ask: float,
    tick: float,
    value_cap: float | None = None,
    steps: int = MAKER_LADDER_STEPS,
) -> list[float]:
    """The price band one leg rests across, cheapest rung first.

    Production offered exactly one price -- the cheapest legal post -- so it
    only ever bought the 17% rung and never the 88% one.  The ledger has
    always offered the whole band and let the book decide which rung it
    reaches, and that is the execution every ROI figure here was measured
    under.  Same construction, same constants, so the two cannot drift.
    """

    if bid <= 0.0 or ask <= 0.0 or tick <= 0.0:
        return []
    ceiling = ask if value_cap is None else min(ask, float(value_cap))
    high = _floor_to_tick(ceiling - tick, tick)
    if high <= EPS:
        return []
    low = min(_floor_to_tick(bid + MAKER_LADDER_IMPROVE_TICKS * tick, tick), high)
    if low <= EPS:
        return []
    if high <= low + tick / 2 or steps <= 1:
        return [low]
    span = high - low
    rungs = sorted({
        _floor_to_tick(low + span * index / (steps - 1), tick)
        for index in range(steps)
    })
    return [rung for rung in rungs if rung > EPS and rung + EPS >= bid]


def _maker_market_anchor(
    *,
    bid: float,
    ask: float,
    tick: float,
    value_cap: float | None = None,
) -> float | None:
    """Return the most competitive non-crossing official-WS Maker price."""

    highest_post_only = _floor_to_tick(ask - tick, tick)
    if highest_post_only <= EPS:
        return None
    improve_bid = _floor_to_tick(bid + tick, tick)
    anchor = min(highest_post_only, improve_bid)
    if value_cap is not None:
        # A ceiling the market's own prices can never lift.  Flooring to the
        # tick keeps the post legal; a ceiling below one tick means the rung
        # is not worth its minimum quote and nothing is posted.
        anchor = min(anchor, _floor_to_tick(float(value_cap), tick))
        if anchor <= EPS:
            return None
    if anchor + EPS < bid:
        return None
    return anchor


def _ask_depth_purchase(
    *,
    levels: Sequence[Any],
    cash_limit: float,
) -> tuple[float, float]:
    """Conservatively price target inventory against official ask depth."""

    remaining = cash_limit
    shares = 0.0
    spent = 0.0
    for level in levels:
        if remaining <= EPS:
            break
        level_cash = float(level.price) * float(level.shares)
        take_cash = min(remaining, level_cash)
        shares += take_cash / float(level.price)
        spent += take_cash
        remaining -= take_cash
    return shares, spent


def _reservation_grid(*, row: Any, artifact: DeltaV2PolicyArtifact) -> tuple[float, ...]:
    """Bind economic inversion to the live spread without making it a quote."""

    tick = float(row.price_tick_size)
    highest_post_only = _floor_to_tick(float(row.ask_price) - tick, tick)
    if highest_post_only <= EPS:
        return ()
    market_points = {
        _floor_to_tick(float(row.bid_price), tick),
        _floor_to_tick(float(row.bid_price) + tick, tick),
        highest_post_only,
    }
    values = {
        _floor_to_tick(float(price), tick)
        for price in (*artifact.price_grid, *market_points)
        if float(price) > EPS and float(price) <= highest_post_only + EPS
    }
    return tuple(sorted(price for price in values if price > EPS))


# The cheapest all-in ask a leg may carry and still be bought.  Below this a
# quote is a one-sided tail offer rather than a price: it states no
# probability, and its payout dominates whatever P&L it lands in.
#
# It excludes the rung, not the window (user ruling, 2026-08-17).  Voiding
# the whole window for one cheap leg threw away the rungs beside it that
# were priced perfectly well -- the objective lost a seat it could have
# taken on three good rungs because a fourth was quoted at a tenth of a
# cent.  Excluding the bucket from the candidate domain instead keeps the
# windows contiguous by construction (a bucket that is not admitted cannot
# leave a hole in a run grown from what is), and the remaining rungs are
# enumerated normally.
MINIMUM_LEG_ALL_IN_ASK = 0.02
# The shape a basket is asked to take inside itself (user ruling,
# 2026-08-17): a base holding on the centre rung, tilted toward legs whose
# own edge is positive, with the far rungs held deliberately small.
#
# It sits between the two shapes the objective would otherwise choose from.
# Equal payout across the window is the flat curve the log score likes best
# on its own terms, but it spends the same on a rung the model barely
# believes as on the centre.  All-in on the cheapest positive-edge leg is
# the lottery, whose payout the tail dominates -- measured earlier, every
# window whose cheapest leg was under a cent returned +487% while all four
# dearer strata returned between -12% and -100%.  This shape keeps the
# centre as the position and lets edge lean it, so the convexity comes from
# the basket's own geometry rather than from a tail ticket; the far rungs
# remain as the hedge they are meant to be.
CENTRE_ANCHOR_EDGE_TILT = 0.6
CENTRE_ANCHOR_DISTANCE_DECAY = 0.8
CENTRE_ANCHOR_MIN_RUNG_SHARE = 0.15

# The gates an enumerated support window has to clear, shallowest first.  A
# window refused by one of these has already cleared every gate before it, so
# the deepest gate that fired is the one that refused the last window still
# standing.  The order is the order of the checks in ``_support_windows``;
# the two names in the last pair sit at the same depth and are ordered so the
# fully-priced refusal wins, because a window that was priced and lost on the
# number is a stronger statement than one that could not be priced at all.
WINDOW_REFUSAL_FUNNEL = (
    "gapped",
    "center",
    "short",
    "leg_floor",
    "unpriced",
    "edge",
)


def _last_refusing_gate(**counts: int) -> str | None:
    """The deepest gate in the funnel that turned a window away.

    ``None`` when nothing was refused.  This is deliberately not "the gate
    with the largest count": a shallow gate refuses the many windows that
    were never candidates -- on a three-bucket day the centre rung alone
    refuses two of the six enumerable windows before length is even
    considered -- so counting votes hands the verdict to whichever gate sits
    earliest, every time.  Measured on the 2026-08-20 book, the three most
    common P0 shapes (2,3,1), (1,1,1) and (3,2,1) are each a whole domain
    enumerated away, and in all three it is the leg floor that kills the one
    window that could still have been seated.
    """

    for gate in reversed(WINDOW_REFUSAL_FUNNEL):
        if int(counts.get(gate) or 0) > 0:
            return gate
    return None


def _window_audit_closes(audit: Mapping[str, Any]) -> bool:
    """Whether the audit's counters account for every window enumerated.

    The refusal the ledger prints is explained by these counters and nothing
    else, so if they do not sum to the enumeration the page is stating causes
    that cannot produce the effect.  Kept as a predicate rather than an
    assertion inside the hot path so tests can hold the invariant without the
    runtime paying for it on every scope.
    """

    refused = sum(
        int(audit.get("%s_refused_window_count" % gate) or 0)
        if gate != "gapped"
        else int(audit.get("gapped_window_refused_count") or 0)
        for gate in WINDOW_REFUSAL_FUNNEL
    )
    return int(audit.get("enumerated_window_count") or 0) == (
        refused + int(audit.get("shortlisted_window_count") or 0)
    )


def _support_windows(
    *,
    snapshot: TargetBasketSnapshot,
    desired: set[str],
    probabilities: Mapping[str, float],
    fee_rate: float,
    maximum_support_buckets: int,
    center_bucket: str,
    minimum_support_buckets: int = 1,
    probability_stage: str = "",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Contiguous windows admissible as a support, ranked by belief divergence.

    The incumbent support stays in the list unconditionally.  It is what the
    continuous-core resolver admitted -- evidence, model agreement, hard
    bounds, peripheral reliability -- and this function is a price filter, not
    a second opinion on any of that.  Windows are drawn from the same admitted
    set, so nothing here can seat a bucket the core refused.

    Ranking is by the window's probability mass.  It only orders the
    shortlist; the seat is decided by the objective itself on the full curve.
    Price enters as two floors rather than as the ranking -- the per-leg
    minimum below, and the whole-basket edge requirement -- because any
    ranking on belief-minus-price is maximised where model and market
    disagree most, which is measured to be where this model is worst.
    """

    by_bucket = {row.bucket: row for row in snapshot.buckets}
    incumbent_buckets = sorted(
        (bucket for bucket in desired if bucket in by_bucket),
        key=lambda bucket: (by_bucket[bucket].ordinal, bucket),
    )
    if not incumbent_buckets:
        raise ContractError("delta_v2_support_empty")
    incumbent = tuple(incumbent_buckets)
    # The domain is every outcome the day can still reach, not the window
    # where the probability happens to pile up.
    #
    # Drawing candidates from the probability core made the optimizer a
    # formality: the core is chosen by mass, mass is what the market prices
    # richest, and no sub-window of four expensive buckets clears the win
    # rail -- 34 of 40 production plans had exactly one admissible window,
    # their own incumbent.  ZUUU 2026-08-14 seated a support whose four
    # buckets priced at 0.93 against our 0.585, while 33C (edge +0.092),
    # 38C (+0.048) and 32C (+0.052) sat outside it unconsidered.  Over 53
    # station-days a full-domain search finds a window clearing both rails on
    # 24 of them at a mean edge of +0.318; ZUUU's own was {33,34,35,36} at
    # +0.154, one bucket below the incumbent.
    #
    # The safety property the core carried is preserved by the filter below:
    # a bucket the hard lower bound has excluded holds zero probability and
    # is not a candidate.  Evidence still decides what is possible; the
    # objective decides only what is worth buying among the possible.
    def _priced_above_floor(row: Any) -> bool:
        """A rung whose quote is a price, not a tail offer.

        An unquoted bucket stays admitted: it was let in on evidence and is
        held or waited on, not bought now, which is the pre-existing
        behaviour the price filter must not change.
        """

        if not row.ask_available:
            return True
        return (
            float(row.ask_price) * (1.0 + float(fee_rate))
            >= MINIMUM_LEG_ALL_IN_ASK
        )

    leg_floor_excluded = sorted(
        (
            row.bucket
            for row in snapshot.buckets
            if float(row.probability) > EPS and not _priced_above_floor(row)
        ),
        key=lambda bucket: (by_bucket[bucket].ordinal, bucket),
    )
    admitted = sorted(
        (
            row.bucket
            for row in snapshot.buckets
            if float(row.probability) > EPS and _priced_above_floor(row)
        ),
        key=lambda bucket: (by_bucket[bucket].ordinal, bucket),
    )
    for bucket in incumbent:
        if bucket not in admitted:
            admitted.append(bucket)
    admitted.sort(key=lambda bucket: (by_bucket[bucket].ordinal, bucket))

    def _all_in(bucket: str) -> float | None:
        """All-in ask, or None when the bucket has no offer to price.

        A support may legitimately contain a bucket with no ask -- it was
        admitted on evidence and is held or waited on, not bought now.  Such a
        window cannot be scored on price, so it is not *ranked* against the
        priced ones; the incumbent still passes through unconditionally, which
        keeps a bid-only bucket exactly as available as it was before.
        """

        row = by_bucket[bucket]
        if not row.ask_available:
            return None
        return float(row.ask_price) * (1.0 + float(fee_rate))

    # The domain stays the whole day (above); what the seat must do is hold
    # the rung the whole ladder hangs from.  Ladder ruling (2026-08-15,
    # charter 49): the seat window must CONTAIN the core's centre bucket,
    # and which side the remaining buckets extend toward is not ruled at
    # all -- the curve score prices both sides from calibrated mass against
    # the ask, so whichever flank holds more under-priced probability wins
    # the extension by argmax, never by a direction rule.  This supersedes
    # the weaker touch-the-core gate (containing the centre implies
    # touching the run grown from it); the measured shoulder evidence that
    # motivated that gate -- off-centre windows realize 0.77-0.83 of what
    # they claim -- now backs the stronger rung.
    # The centre is the heaviest bucket of the combined model distribution
    # (user ruling, 2026-08-16), open edge buckets included and competing on
    # the same footing.  This supersedes the clause 42 seeding anchor, which
    # used the point estimate precisely to keep an unbounded edge bucket from
    # winning the argmax on width rather than conviction -- 8/17 measured 17
    # of 30 stations moving their centre under the new definition and 6
    # landing on an edge bucket, ZUUU from 30C to "33C or higher" (0.359
    # against 0.059).  The ruling is aware of that case; the code follows it.
    ladder_center = max(
        admitted,
        key=lambda bucket: (
            float(probabilities.get(bucket, 0.0)),
            -int(by_bucket[bucket].ordinal),
        ),
    ) if admitted else center_bucket
    core = probability_core(
        probabilities,
        ordinals={
            bucket: int(row.ordinal) for bucket, row in by_bucket.items()
        },
        # One anchor for both layers, still: the run has to be grown from the
        # same bucket the seat rule requires, or the two disagree about where
        # the middle is and a window can hold the rung while missing the core.
        center_bucket=ladder_center,
    )
    # Every gate that can turn an enumerated window away keeps a counter, and
    # exactly one of them fires per refused window.  This is not bookkeeping
    # for its own sake: the ledger reports the refusal to the page in terms of
    # these numbers, so a gate without a counter is a refusal the page has to
    # blame on some other gate.  It has already happened twice -- the leg
    # floor was unlisted on 2026-08-17 and 112 P0 refusals were attributed to
    # a centre-bucket count that could not explain them, and the edge/friction
    # gate below was unlisted until 2026-08-20, which put 308 station-days
    # under "the market priced us out" when the structural floors were what
    # refused them.  The invariant that stops the third occurrence is
    # closure: ``enumerated`` equals the sum of the refusal counters plus
    # ``shortlisted``, and ``_window_audit_closes`` asserts it.
    center_refused = 0
    short_refused = 0
    leg_floor_refused = 0
    gapped_refused = 0
    unpriced_refused = 0
    edge_refused = 0
    enumerated = 0
    shortlisted = 0

    windows: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    # Contiguous and at most ``maximum_support_buckets`` -- the same structural
    # cap the continuous core enforces.  Widening the *domain* must not widen
    # the basket: a five-bucket support would break the atomicity contract
    # downstream and the clip lattice it feeds.
    max_size = max(1, int(maximum_support_buckets))
    # The floor is a seating rule, not a search rule: narrower windows are
    # still enumerated so the audit can see what was refused, but only
    # windows at or above it can hold the seat.  Basket completeness, the
    # envelopes and the TAF direction correction are hard requirements that
    # a one- or two-rung ladder cannot carry (user ruling, 2026-08-15).
    # The floor is the policy's; the size the day actually asks for comes
    # from its own dispersion (user ruling, 2026-08-15) and is bounded by
    # that floor and the cap.  A sharp distribution seats fewer rungs, a
    # diffuse one more, and neither is a rule about direction.
    # P1 converges rather than opens: the ladder walks 4 -> 3 -> 2 as the day
    # resolves, and two is the floor because one of the two is still the tail
    # rung carrying the insurance (user ruling, 2026-08-16).  Opening stages
    # keep the policy's three, because that is where the five hard
    # requirements of clause 50 have to be reachable.  P1 may also add a
    # bucket the ladder did not open with -- the floor bounds the seat, it
    # does not forbid a new rung.
    stage_min = (
        P1_MINIMUM_SUPPORT_BUCKETS
        if str(probability_stage) == "P1"
        else int(minimum_support_buckets)
    )
    # The floor cannot ask for more rungs than the day still has.  Late in P1
    # the hard bound prunes every bucket the reported high has passed, and a
    # station can arrive here with one live outcome left; demanding two then
    # is not a floor but a ban on the one basket that is still correct.  The
    # floor binds where there is a choice to constrain, which is the whole of
    # its purpose in clauses 50/50a.
    policy_min = max(1, min(stage_min, max_size, len(admitted)))
    wanted_size, dispersion_receipt = dispersion_support_size(
        probabilities,
        ordinals={
            bucket: int(row.ordinal) for bucket, row in by_bucket.items()
        },
        minimum=policy_min,
        maximum=max_size,
    )
    # Dispersion tightens the cap, never the floor.  Raising the floor on a
    # diffuse day would forbid the narrow seats the objective is entitled to
    # prefer; lowering the cap on a sharp day stops it buying rungs the
    # model does not believe in.  The floor stays the policy's, which is
    # what clause 50 made a hard requirement.
    min_size = policy_min
    max_size = max(policy_min, min(max_size, wanted_size))
    for size in range(1, min(max_size, len(admitted)) + 1):
        for start in range(0, len(admitted) - size + 1):
            window = tuple(admitted[start:start + size])
            if window in seen:
                continue
            seen.add(window)
            enumerated += 1
            # Contiguous in ordinals, not merely adjacent in the admitted
            # list.  Once the leg floor can remove a bucket from the domain,
            # the two stop being the same thing: with 34C excluded, the slice
            # (32C, 33C, 35C) looks like a run and is a basket with a hole in
            # it, which the settlement partition and the clip lattice both
            # assume cannot happen.
            span = (
                by_bucket[window[-1]].ordinal
                - by_bucket[window[0]].ordinal
            )
            if span != size - 1:
                gapped_refused += 1
                continue
            raw_win = sum(
                float(probabilities[bucket]) for bucket in window
            )
            # A window that does not hold the model's own centre is
            # over-claimed by a measured amount; the objective must rank and
            # size on the corrected number, not on the raw one.
            holds_center = center_bucket in window
            claim_factor = (
                1.0
                if holds_center
                else off_center_claim_factor(raw_win)
            )
            win = min(1.0 - EPS, raw_win * claim_factor)
            quotes = [_all_in(bucket) for bucket in window]
            priced = all(quote is not None for quote in quotes)
            price = sum(quote for quote in quotes if quote is not None)
            edge = win - price if priced else None
            is_incumbent = window == incumbent
            # The incumbent has no exemption left.  It used to be waived past
            # both floors on the reasoning that the continuous-core resolver
            # admits it on evidence and this function is only a price filter;
            # the 2026-08-16 ruling ends that for the centre rung as well as
            # the length, because "must contain the centre bucket" was ruled
            # with no exemption attached.  What the resolver admits still
            # bounds what can be seated -- windows are drawn from the same
            # admitted set -- it just no longer gets a seat the rules refuse.
            #
            # The length floor likewise.  A two-bucket core is
            # still a two-rung ladder, and the five hard requirements clause
            # 50 names -- basket completeness, the model envelope, the centre
            # envelope, the TAF direction correction, whole-basket positive
            # EV -- are exactly as unreachable from one whether the two rungs
            # arrived by argmax or by inheritance.  The exemption was written
            # for a market that can only quote two live buckets; measured on
            # 2026-08-17 that is not the case it was serving.  RKSI, RPLL and
            # ZHHH each seated a two-bucket incumbent while three-bucket
            # windows sat admissible beside it on the same plan, so the
            # exemption was not keeping a starved day tradeable, it was
            # letting the curve score buy the narrower ladder it prefers.
            # Those three now seat the three-rung window instead of the two.
            #
            # There are exactly two hard admission floors, and everything else
            # is ranking -- the seat belongs to the curve score alone.  First,
            # the ladder rung: the window must contain the centre bucket.
            # Direction is deliberately not checked anywhere -- among the
            # windows holding the rung, the flanks compete on curve score
            # and the better-priced side wins its rungs automatically.
            holds_core = bool(core) and bool(set(window) & core)
            if ladder_center not in window:
                center_refused += 1
                continue
            if len(window) < min_size:
                short_refused += 1
                continue
            # Third floor: every leg must carry a price a market could
            # plausibly have made.  A leg offered at a tenth of a cent is the
            # known one-sided tail quote -- it does not state a probability,
            # and buying it converts the basket into a lottery ticket whose
            # payout dominates any P&L it appears in.  Measured over the
            # settled book, every window whose cheapest leg was under 0.01
            # returned +487% while all four dearer strata returned between
            # -12% and -100%: the entire apparent edge of this strategy lived
            # on those legs.  The floor is on the leg, not the basket,
            # because a basket average hides exactly the case that matters --
            # three fair legs plus one penny leg prices like a fair basket.
            # No window-level check any more: the floor is applied to the
            # candidate domain above, so an enumerated window cannot contain
            # a sub-floor rung.  An inherited support still can -- it comes
            # from the resolver, not from this enumeration -- and is
            # de-priced rather than dropped, because dropping it would take
            # "keep what we hold" off the menu for a scope that holds one.
            leg_floor_hit = False
            if priced and any(
                quote < MINIMUM_LEG_ALL_IN_ASK for quote in quotes
            ):
                leg_floor_hit = True
                priced = False
                edge = None
            # Second, friction: the all-in price already carries the fee, so an
            # edge smaller than tick-rounding across the legs is quantisation
            # noise, not signal.
            friction = SUPPORT_EDGE_FRICTION_TICKS_PER_LEG * sum(
                float(by_bucket[bucket].price_tick_size)
                for bucket in window
            ) + SUPPORT_EDGE_MARGIN
            admissible = is_incumbent or (
                priced and edge is not None and edge > friction + EPS
            )
            if not admissible:
                # Name which of the three ways this branch can refuse actually
                # refused.  Until 2026-08-20 it had no counter at all, so the
                # only gate that is genuinely about price -- the edge one --
                # was invisible, and the ledger printed three structural
                # counts that could not add up to the refusal they were
                # offered as the explanation of.
                if leg_floor_hit:
                    # The quote was the refusal; the edge test never got a
                    # number to look at.  Counted here rather than at the
                    # floor above so that the floor's own counter stays a
                    # count of *refusals*: an incumbent resting on a penny leg
                    # is de-priced but still seated, and calling that a
                    # refusal is the same misattribution one level down.
                    leg_floor_refused += 1
                elif not priced:
                    unpriced_refused += 1
                else:
                    edge_refused += 1
                continue
            shortlisted += 1
            kl = None
            if priced:
                # Clipping implements the 0*log(0)=0 limits: a full-partition
                # window (win=1) is the certainty-harvest shape and must rank
                # by log(1/cost), not fall off the shortlist as unrankable.
                clipped_win = min(max(win, EPS), 1.0 - EPS)
                cost = min(max(price, EPS), 1.0 - EPS)
                kl = (
                    clipped_win * math.log(clipped_win / cost)
                    + (1.0 - clipped_win)
                    * math.log((1.0 - clipped_win) / (1.0 - cost))
                )
            windows.append({
                "buckets": window,
                "win_probability": win,
                "raw_win_probability": raw_win,
                "off_center_claim_factor": claim_factor,
                "holds_probability_core": holds_core,
                "all_in_price": price if priced else None,
                "edge": edge,
                "log_growth_rank": kl,
                "is_incumbent": is_incumbent,
            })
    if incumbent not in seen:
        # Enumeration can miss the incumbent outright: it is bounded to
        # contiguous runs of at most ``maximum_support_buckets``, and an
        # inherited support may be neither -- a live ZHHH core sat on
        # {31C, 33C or higher} with 32C alive between them.  Relying on the
        # ``is_incumbent`` branch inside the loop to carry it through is
        # therefore not enough; that branch only fires on a window the loop
        # actually produced.  Dropping it would take "keep what we hold" off
        # the menu, which is the same escape the floor's live-bucket scope
        # exists to close, so it is appended here instead.
        quotes = [_all_in(bucket) for bucket in incumbent]
        priced = all(quote is not None for quote in quotes)
        # The incumbent reaches the list without passing the loop's floors,
        # but the leg floor is about what the quote *means*, not about which
        # window earned its place -- an inherited support resting on a penny
        # leg is the same lottery as a freshly enumerated one.
        if priced and any(
            quote < MINIMUM_LEG_ALL_IN_ASK for quote in quotes
        ):
            priced = False
        price = sum(quote for quote in quotes if quote is not None)
        win = sum(float(probabilities[bucket]) for bucket in incumbent)
        incumbent_kl = None
        if priced:
            clipped_win = min(max(win, EPS), 1.0 - EPS)
            cost = min(max(price, EPS), 1.0 - EPS)
            incumbent_kl = (
                clipped_win * math.log(clipped_win / cost)
                + (1.0 - clipped_win)
                * math.log((1.0 - clipped_win) / (1.0 - cost))
            )
        windows.append({
            "buckets": incumbent,
            "win_probability": win,
            "holds_probability_core": bool(core) and bool(set(incumbent) & core),
            "all_in_price": price if priced else None,
            "edge": (win - price) if priced else None,
            "log_growth_rank": incumbent_kl,
            "is_incumbent": True,
        })
    # Rank by belief alone.  KL replaced raw edge here because raw edge
    # over-ranks penny lotteries, and it does price the whole win/lose
    # geometry -- but both are divergences between our belief and the
    # market's, so both are maximised exactly where the two disagree most,
    # and that is measured to be where this model is least reliable.  Over
    # 158 settled seats the flank the divergence chose was cheaper than its
    # mirror 96% of the time and the truth landed on the mirror 83%; of 123
    # misses, 101 fell on the side the window had declined to extend toward
    # (p = 3e-13).  Split by whether the model agreed with the direction the
    # price implied, the same book hit 35% when it did and 13% when it did
    # not.  Ordering by probability mass instead makes the ladder extend
    # toward what the model believes, and leaves price to the two things
    # that are properly price's job: the leg floor above, and the whole-basket
    # positive-EV requirement below.  An unpriced window still sorts last --
    # it has no basket to admit -- and the curve score still decides the seat
    # among those shortlisted.
    windows.sort(
        key=lambda row: (
            row["all_in_price"] is None,
            -float(row["win_probability"]),
            len(row["buckets"]),
            row["buckets"],
        )
    )
    return windows, {
        "probability_core": sorted(
            core, key=lambda bucket: (by_bucket[bucket].ordinal, bucket)
        ),
        "probability_core_mass": PROBABILITY_CORE_MASS,
        "ladder_center_bucket": ladder_center,
        "center_refused_window_count": center_refused,
        "minimum_support_buckets": min_size,
        "policy_minimum_support_buckets": policy_min,
        "dispersion_support_size": dispersion_receipt,
        "short_refused_window_count": short_refused,
        "leg_floor_refused_window_count": leg_floor_refused,
        "leg_floor_excluded_buckets": leg_floor_excluded,
        "gapped_window_refused_count": gapped_refused,
        # The one gate here that is actually the market's doing: the window
        # was whole, every leg carried a real quote, and the model's own edge
        # over the all-in price did not clear tick-rounding across the legs.
        "edge_refused_window_count": edge_refused,
        "unpriced_refused_window_count": unpriced_refused,
        # The friction the edge gate measured against is not repeated here:
        # ``_solve_over_support_windows`` already publishes it into the same
        # receipt as ``edge_friction_ticks_per_leg``, and two names for one
        # number in one dict is how a reader ends up comparing a value with
        # itself.
        "enumerated_window_count": enumerated,
        "shortlisted_window_count": shortlisted,
        # Which gate refused the last window still standing.  The gates form
        # a strict funnel -- gapped, then centre, then length, then the leg
        # floor, then price -- so a window refused by a later gate had
        # already cleared every earlier one, and the deepest gate that fired
        # is the one that took the final candidate off the table.  Derived
        # here because the funnel order is this function's own structure; a
        # consumer that re-derives it is guessing, and guessing is what put
        # 308 station-days under the wrong heading.
        "last_refusing_gate": _last_refusing_gate(
            gapped=gapped_refused,
            center=center_refused,
            short=short_refused,
            leg_floor=leg_floor_refused,
            unpriced=unpriced_refused,
            edge=edge_refused,
        ),
        "minimum_leg_all_in_ask": MINIMUM_LEG_ALL_IN_ASK,
        # Retained name from the touch-the-core era so existing readers keep
        # working; it now counts refusals under the stronger rung.
        "core_refused_window_count": center_refused,
    }


# Enumerating quantities is exponential in the window's width, so the shortlist
# is bounded rather than the whole lattice being expanded.  The ordering above
# is by the same quantity the objective maximises -- but that ranking is
# depth-blind: on a listing-thin book the highest-KL windows can all contain
# a leg whose entire visible book is smaller than the smallest cash clip, and
# such a window solves to exactly the baseline.  At four, ZUUU 2026-08-15's
# top three alternatives all carried the 0.75-USDC 33C book and scored zero
# while the depth-feasible {34,35} pair (edge +0.18) sat unsolved at rank
# five -- a HOLD manufactured by the cutoff, not by the objective.  Twelve
# keeps the solve bounded (each window's own candidate lattice is already
# capped at 4096) while letting the first depth-feasible alternatives reach
# the solver.
MAX_SUPPORT_WINDOWS_SOLVED = 12


def _solve_over_support_windows(
    *,
    windows: Sequence[Mapping[str, Any]],
    window_audit: Mapping[str, Any],
    live_buckets: Sequence[str],
    snapshot: TargetBasketSnapshot,
    probabilities: Mapping[str, float],
    shortfall: set[str],
    breakthrough: set[str],
    delta_policy: DeltaV2PolicyArtifact,
    basket_policy: TargetBasketPolicy,
    capital_available_usdc: float,
    objective_form: str,
    bankroll_usdc: float,
) -> tuple[tuple[str, ...], dict[str, Any], dict[str, Any]]:
    """Run the objective on each shortlisted window; the best curve is seated.

    Scores are comparable across windows because every candidate's scenario
    curve spans the whole bucket space -- an unheld bucket settles at minus the
    basket's cost -- so two windows are scored on the same outcome partition
    rather than on their own supports.
    """

    shortlist = list(windows[:MAX_SUPPORT_WINDOWS_SOLVED])
    incumbent = next(
        (row for row in windows if row["is_incumbent"]), None
    )
    if incumbent is not None and incumbent not in shortlist:
        shortlist.append(incumbent)
    if not shortlist:
        raise ContractError("delta_v2_support_empty")

    scored: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for row in shortlist:
        window = tuple(row["buckets"])
        claim_factor = float(row.get("off_center_claim_factor") or 1.0)
        if abs(claim_factor - 1.0) > EPS:
            # The same correction the ranking used, expressed on the
            # distribution this window is solved against: the window's own
            # buckets are deflated and the mass they give up returns to the
            # buckets outside it, so the scenario space stays a partition and
            # the curve score is computed on one consistent belief.
            inside = sum(
                float(probabilities[bucket]) for bucket in window
            )
            outside = max(0.0, 1.0 - inside)
            corrected_inside = min(1.0 - EPS, inside * claim_factor)
            outside_scale = (
                (1.0 - corrected_inside) / outside if outside > EPS else 0.0
            )
            window_probabilities = {
                bucket: (
                    float(value) * claim_factor
                    if bucket in set(window)
                    else float(value) * outside_scale
                )
                for bucket, value in probabilities.items()
            }
            total = sum(window_probabilities.values())
            window_probabilities = {
                bucket: value / total
                for bucket, value in window_probabilities.items()
            }
        else:
            window_probabilities = dict(probabilities)
        try:
            solution = solve_target_inventory_v2(
                probabilities=window_probabilities,
                feasible_inventory_candidates=_target_candidates(
                    snapshot=snapshot,
                    desired=window,
                    artifact=delta_policy,
                    capital_available_usdc=capital_available_usdc,
                    minimum_clip_notional_usdc=(
                        basket_policy.clip_min_notional_usdc
                    ),
                    bankroll_usdc=bankroll_usdc,
                    # The deflated belief, not the snapshot's: the generator
                    # must propose against the distribution the ranking on
                    # the next line scores.
                    probabilities_by_bucket=window_probabilities,
                    centre_bucket=str(
                        window_audit.get("ladder_center_bucket") or ""
                    ),
                ),
                # The floor guards the whole live set exactly when full
                # coverage is structurally possible (charter 38, amended
                # 2026-08-14).  While the hard bound still holds more live
                # buckets than a window may span, demanding every one be
                # protected is not a floor but a ban: eleven live buckets
                # against a four-bucket support means some uncovered bucket
                # always settles at minus the whole cost, so every spending
                # candidate was illegal and the opening stage could never
                # buy -- a direct collision with charters 46/48.  The risk
                # the floor was double-charging is already priced inside the
                # objective: an uncovered live bucket enters the log-wealth
                # curve at minus the basket's cost with its own probability,
                # and the fractional-Kelly stake shrinks accordingly.  So:
                # live set coverable (<= the window width cap) -> protect
                # all of it, which keeps the anti-narrowing rule exactly
                # where it can bind; wider than coverable -> protect the
                # window's own tail, and let sizing price the rest.  The
                # hard bound pruning live buckets flips this back to
                # full-scope automatically, which is clause 38's original
                # "the floor relaxes as certainty arrives" reading run in
                # both directions.
                protected_buckets=(
                    live_buckets
                    if len(live_buckets)
                    <= max(1, int(basket_policy.maximum_support_buckets))
                    else window
                ),
                shortfall_buckets=tuple(sorted(shortfall & set(window))),
                breakthrough_buckets=tuple(
                    sorted(breakthrough & set(window))
                ),
                weights=delta_policy.value_weights,
                objective_form=objective_form,
                bankroll_usdc=bankroll_usdc,
            )
        except ContractError as exc:
            # One infeasible window -- no legal shape at these prices, or a
            # candidate lattice too wide -- must not deny the others their
            # comparison.  It is recorded, not silently dropped.
            failures.append({
                "buckets": list(window),
                "reason": f"ContractError:{exc}",
            })
            continue
        score = (
            solution["target_curve_score"]
            if objective_form == CURVE_OBJECTIVE_FORM
            else solution["target_value"]
        )
        scored.append({
            "window": row,
            "solution": solution,
            "score": float(score),
        })
    if not scored:
        raise ContractError("delta_v2_no_solvable_support_window")
    # Seat the head of the shortlist, not the best score on it.
    #
    # The shortlist is ordered by probability mass on purpose -- the ladder
    # extends toward what the model believes, and price is left to the leg
    # floor and the whole-basket positive-EV requirement.  Ranking the
    # shortlist a second time by any score that rises with the edge undoes
    # that ruling, because it seeks the window where model and market
    # disagree most, and that is measured to be where this model is least
    # reliable: over 158 settled seats the flank the divergence chose was
    # cheaper than its mirror 96% of the time and the truth landed on the
    # mirror 83%.
    #
    # Measured twice, independently, by rebuilding the paper ledger on each
    # rule over the same station-days.  2026-08-20 with a hand-rolled log
    # wealth score: seats 249 -> 318, ROI -10.4% -> -20.2%.  2026-08-22
    # calling this function itself with the promoted policy artifact -- the
    # version the first measurement named as the only one worth attempting:
    # seats 386 -> 504, ROI +9.7% -> -20.5%.  Stratified by edge the settled
    # book is an inverted U (<0: -91.2%, 0-0.10: +16.1%, 0.10-0.25: +25.2%,
    # >=0.25: -68.7%), and a monotone score walks straight into the right
    # hand band.  ``curve_score`` is monotone in the edge over the bettable
    # range, so being the correct Kelly form for *quantities* -- which it
    # still is, and still supplies below -- does not make it a selector.
    #
    # The ledger was moved to the shortlist head on 2026-08-20; production
    # was not, so the rule measured at -20% kept running live while the one
    # measured at +9.7% existed only in the replay.  This closes that gap.
    best = scored[0]
    selection = {
        "rule": "PROBABILITY_MASS_SHORTLIST_HEAD_CURVE_SOLVED_QUANTITIES_V3",
        "seat_rule_superseded": (
            "MAX_CURVE_SCORE_OVER_CONTIGUOUS_SUPPORT_WINDOWS_V2"
        ),
        "shortlist_head_curve_score": float(scored[0]["score"]),
        "best_curve_score_on_shortlist": float(
            max(item["score"] for item in scored)
        ),
        "edge_friction_ticks_per_leg": SUPPORT_EDGE_FRICTION_TICKS_PER_LEG,
        "admissible_window_count": len(windows),
        "solved_window_count": len(scored),
        **dict(window_audit),
        "unsolvable_windows": failures,
        "seated": {
            "buckets": list(best["window"]["buckets"]),
            "win_probability": best["window"]["win_probability"],
            "all_in_price": best["window"]["all_in_price"],
            "edge": best["window"]["edge"],
            "curve_score": best["score"],
            "is_incumbent": best["window"]["is_incumbent"],
        },
        "considered": [
            {
                "buckets": list(item["window"]["buckets"]),
                "win_probability": item["window"]["win_probability"],
                "all_in_price": item["window"]["all_in_price"],
                "edge": item["window"]["edge"],
                "curve_score": item["score"],
            }
            for item in sorted(
                scored, key=lambda item: -item["score"]
            )
        ],
    }
    return tuple(best["window"]["buckets"]), best["solution"], selection


def _target_candidates(
    *,
    snapshot: TargetBasketSnapshot,
    desired: Sequence[str],
    artifact: DeltaV2PolicyArtifact,
    capital_available_usdc: float,
    minimum_clip_notional_usdc: float,
    bankroll_usdc: float,
    probabilities_by_bucket: Mapping[str, float],
    centre_bucket: str = "",
) -> list[dict[str, Any]]:
    """Build a bounded q-grid priced from official WS ask depth.

    ``probabilities_by_bucket`` is the belief this window is *scored* against,
    which is not always the snapshot's: an off-centre window has its claim
    deflated by the measured factor before the objective ever reads it.  The
    generator has to be handed the same numbers, or the continuous candidate
    it offers is the optimum of a distribution nobody ranks.
    """

    by_bucket = {row.bucket: row for row in snapshot.buckets}
    current_shares = {row.bucket: row.filled_shares for row in snapshot.buckets}
    current_cost = {row.bucket: row.filled_cost_usdc for row in snapshot.buckets}
    pending_shares = {row.bucket: row.pending_shares for row in snapshot.buckets}
    pending_cost = {
        row.bucket: row.pending_notional_usdc for row in snapshot.buckets
    }
    ordered = tuple(sorted(desired, key=lambda bucket: (by_bucket[bucket].ordinal, bucket)))
    per_bucket_levels: list[tuple[float, ...]] = []
    quote_levels: dict[str, dict[float, tuple[float, float]]] = {}
    for bucket in ordered:
        row = by_bucket[bucket]
        quote_levels[bucket] = {}
        affordable: list[float] = []
        for clip in artifact.cash_clip_grid:
            if clip > capital_available_usdc + EPS:
                continue
            # The clip floor is deliberately *not* applied here.  Dropping a
            # sub-floor clip from the grid does stop an unfillable q*, but it
            # also removes the bucket from the sizing decisions downstream,
            # and ``clip_below_minimum`` is raised there -- so filtering early
            # trades one silent hold for another while destroying the only
            # reason code that explains it.  ``cash_phase`` owns this refusal
            # and says so per bucket; leave the candidate visible to it.
            shares, spent = _ask_depth_purchase(
                levels=row.ask_levels,
                cash_limit=float(clip),
            )
            if (
                shares <= EPS
                or spent + EPS < float(clip)
            ):
                continue
            quote_levels[bucket][float(clip)] = (shares, spent)
            affordable.append(float(clip))
        per_bucket_levels.append((0.0, *affordable))
    candidate_count = math.prod(len(levels) for levels in per_bucket_levels)
    if candidate_count > 4096:
        raise ContractError("delta_v2_target_candidate_bound_exceeded")
    candidates: list[dict[str, Any]] = []
    for clips in product(*per_bucket_levels):
        if sum(clips) > capital_available_usdc + EPS:
            continue
        is_baseline = all(clip <= EPS for clip in clips)
        if not is_baseline and any(clip <= EPS for clip in clips):
            # A basket activation is atomic at the target level: either every
            # selected support bucket receives a positive target, or the
            # optimizer compares against the no-new-action baseline and
            # HOLDS.  Every positive gap is emitted in one complete Maker
            # intent bundle; exchange fills may arrive independently, but no
            # proper subset is an authorized initial action.
            #
            # This used to exempt a leg the station already held, on the
            # reading that inventory already satisfies it.  The contract the
            # candidates are then held to measures increments against the
            # baseline -- every candidate carries the same inventory and none
            # of them chose it -- so an exempted combination arrived at
            # ``_assert_candidate_support_contract`` as a complete basket with
            # a zero increment and was rejected as incomplete, taking the
            # whole window's solve down with it.  Every window failing that
            # way is ``delta_v2_no_solvable_support_window``, which is a
            # compile refusal for the entire station.
            #
            # It never fired in production because production has held
            # nothing since 2026-07-29: measured on manufactured positions,
            # a station with inventory in a seated bucket refused to compile
            # at every positive capital while the same station flat compiled
            # at all of them.  That is the whole three-stage flow -- P0 opens,
            # P0-M confirms, P1 adjusts -- unable to reach its second stage.
            continue
        shares = {
            bucket: current_shares[bucket] + pending_shares[bucket]
            for bucket in current_shares
        }
        costs = {
            bucket: current_cost[bucket] + pending_cost[bucket]
            for bucket in current_cost
        }
        for bucket, clip in zip(ordered, clips):
            if clip <= EPS:
                continue
            acquired_shares, acquired_cost = quote_levels[bucket][float(clip)]
            shares[bucket] += acquired_shares
            costs[bucket] += acquired_cost
        candidates.append({
            "candidate_id": "q-" + content_sha256({
                "ordered_support": ordered,
                "clips": clips,
                "quote_version": snapshot.quote_version,
            })[:24],
            "shares_by_bucket": shares,
            "cost_by_bucket": costs,
            "pricing_basis": "official_ws_ask_depth",
            "candidate_kind": (
                CANDIDATE_KIND_BASELINE
                if is_baseline
                else CANDIDATE_KIND_COMPLETE
            ),
            "candidate_origin": "CLIP_GRID_PRODUCT",
        })
    if centre_bucket:
        candidates.extend(_centre_anchored_candidates(
            snapshot=snapshot,
            ordered=ordered,
            centre_bucket=centre_bucket,
            current_shares=current_shares,
            current_cost=current_cost,
            pending_shares=pending_shares,
            pending_cost=pending_cost,
            capital_available_usdc=capital_available_usdc,
            minimum_clip_notional_usdc=minimum_clip_notional_usdc,
            probabilities_by_bucket=probabilities_by_bucket,
        ))
    if CONTINUOUS_OPTIMUM_CANDIDATES_ENABLED:
        candidates.extend(_continuous_optimum_candidates(
            snapshot=snapshot,
            ordered=ordered,
            current_shares=current_shares,
            current_cost=current_cost,
            pending_shares=pending_shares,
            pending_cost=pending_cost,
            capital_available_usdc=capital_available_usdc,
            minimum_clip_notional_usdc=minimum_clip_notional_usdc,
            bankroll_usdc=bankroll_usdc,
            probabilities_by_bucket=probabilities_by_bucket,
        ))
    if not candidates:
        raise ContractError("delta_v2_target_candidate_set_empty")
    return _assert_candidate_support_contract(
        candidates, ordered_support=ordered
    )



def _centre_anchored_candidates(
    *,
    snapshot: TargetBasketSnapshot,
    ordered: Sequence[str],
    centre_bucket: str,
    current_shares: Mapping[str, float],
    current_cost: Mapping[str, float],
    pending_shares: Mapping[str, float],
    pending_cost: Mapping[str, float],
    capital_available_usdc: float,
    minimum_clip_notional_usdc: float,
    probabilities_by_bucket: Mapping[str, float],
) -> list[dict[str, Any]]:
    """Base holding on the centre, leaned toward the legs with edge.

    The weight of a rung is its distance decay from the centre, multiplied
    by a tilt that only positive edge can raise.  A rung the market prices
    above our belief keeps the floor share and no more -- it is carried for
    the hedge, not bought for the return -- so the basket stays complete
    while its money concentrates where the model and the book disagree in
    our favour.

    Offered as candidates, not imposed: the curve score still ranks these
    against the grid and the continuous optimum on the same partition, so
    the shape wins where it is actually better and the audit can say how
    often that is.
    """

    by_bucket = {row.bucket: row for row in snapshot.buckets}
    if centre_bucket not in by_bucket or centre_bucket not in ordered:
        return []
    ordinals = {
        bucket: int(by_bucket[bucket].ordinal) for bucket in ordered
    }
    centre_ordinal = ordinals[centre_bucket]
    best_edge = 0.0
    for bucket in ordered:
        row = by_bucket[bucket]
        if row.ask_available:
            best_edge = max(
                best_edge,
                float(probabilities_by_bucket.get(bucket, 0.0))
                - float(row.ask_price),
            )
    weights: dict[str, float] = {}
    for bucket in ordered:
        row = by_bucket[bucket]
        if not row.ask_available:
            return []
        price = float(row.ask_price)
        if price <= 0.0:
            return []
        edge = float(probabilities_by_bucket.get(bucket, 0.0)) - price
        distance = abs(ordinals[bucket] - centre_ordinal)
        decay = CENTRE_ANCHOR_DISTANCE_DECAY ** distance
        # Only positive edge leans the rung, and it leans on the edge
        # itself -- not on edge over price.  The ratio explodes on a cheap
        # rung: measured on ZGSZ 2026-08-17, 30C at 0.020 with edge 0.082
        # scored 4.1 against the centre's 0.65 and took the largest clip in
        # the basket, two rungs out from the centre.  That is the lottery
        # this shape exists to avoid, rebuilt one layer down.  Normalising
        # by the window's best edge keeps the lean bounded by the tilt and
        # leaves the distance decay in charge of the far rungs.
        lean = 1.0 + CENTRE_ANCHOR_EDGE_TILT * (
            max(edge, 0.0) / best_edge if best_edge > EPS else 0.0
        )
        weights[bucket] = max(
            decay * lean,
            CENTRE_ANCHOR_MIN_RUNG_SHARE,
        )
    weights[centre_bucket] = max(weights[centre_bucket], 1.0)
    total_weight = sum(weights.values())
    if total_weight <= EPS:
        return []
    floor = float(minimum_clip_notional_usdc)
    results: list[dict[str, Any]] = []
    seen: set[tuple[float, ...]] = set()
    for scale in (1.0, 0.6, 0.35):
        budget = float(capital_available_usdc) * scale
        if budget <= EPS:
            continue
        cash = []
        for bucket in ordered:
            raw = budget * weights[bucket] / total_weight
            # Never below the clip floor: a rung priced under it cannot be
            # bought at all, and a basket missing a rung is not this basket.
            cash.append(max(floor, math.floor(raw * 100.0) / 100.0))
        if sum(cash) > capital_available_usdc + EPS:
            continue
        key = tuple(cash)
        if key in seen:
            continue
        seen.add(key)
        shares = {
            bucket: current_shares[bucket] + pending_shares[bucket]
            for bucket in current_shares
        }
        costs = {
            bucket: current_cost[bucket] + pending_cost[bucket]
            for bucket in current_cost
        }
        usable = True
        for bucket, clip in zip(ordered, cash):
            acquired, spent = _ask_depth_purchase(
                levels=by_bucket[bucket].ask_levels,
                cash_limit=float(clip),
            )
            if acquired <= EPS or spent + EPS < float(clip):
                usable = False
                break
            shares[bucket] += acquired
            costs[bucket] += spent
        if not usable:
            continue
        results.append({
            "candidate_id": "ca-" + content_sha256({
                "ordered_support": tuple(ordered),
                "cash": key,
                "centre": centre_bucket,
                "quote_version": snapshot.quote_version,
            })[:24],
            # The kind is the closed class the state machine compares
            # against; provenance goes in ``candidate_origin``.  Naming the
            # shape here instead would turn every trade it wins into a hold.
            "candidate_kind": CANDIDATE_KIND_COMPLETE,
            "candidate_origin": "CENTRE_ANCHORED_EDGE_TILT",
            "shares_by_bucket": shares,
            "cost_by_bucket": costs,
        })
    return results

def _continuous_optimum_candidates(
    *,
    snapshot: TargetBasketSnapshot,
    ordered: Sequence[str],
    current_shares: Mapping[str, float],
    current_cost: Mapping[str, float],
    pending_shares: Mapping[str, float],
    pending_cost: Mapping[str, float],
    capital_available_usdc: float,
    minimum_clip_notional_usdc: float,
    bankroll_usdc: float,
    probabilities_by_bucket: Mapping[str, float],
) -> list[dict[str, Any]]:
    """Offer the split the clip grid cannot express.

    An exhaustive product over the grid costs ``(levels+1) ** buckets``, so
    six support buckets exhaust the 4096 bound at three levels and the
    objective is left choosing between a handful of corners.  Resolution
    cannot come from a denser grid; it comes from solving the continuous
    problem, which is concave here, and offering the answer alongside.

    These are ordinary complete-basket candidates -- same kind, same audit,
    same contract -- so the argmax is still exact over the set it is given.
    They differ only in provenance, which is why provenance is a separate
    field: a generator must be able to say where a candidate came from
    without changing what the state machine reads.

    What the solve maximizes is the curve of the *whole* post-trade position,
    from the position already held as its initial condition -- the same
    object ``curve_score`` ranks candidates by.  Solving from flat and adding
    the answer to inventory optimizes a curve nobody scores: on a rebalance
    it re-buys coverage the station already owns and under-buys the outcomes
    still bare, because the marginal log value of a share depends on what
    that outcome already pays.

    ``curve_score`` expresses its risk aversion as an effective bankroll
    ``W/f``, so that is the bankroll the solve is handed and the fraction it
    is asked for is one.  The first candidate is then exactly the ranking's
    own continuous optimum rather than an approximation of it, and the second
    spans a smaller size beneath it.  Serving the serving fraction *here*
    instead would count the same aversion twice and the smallest candidate
    would always win; serving the unspent remainder as ``W`` -- what this did
    before -- made the generator's risk preference drift within the day while
    the score judging it did not.

    The minimum clip is a *lower bound on the decision*, and is solved as
    one.  Basket activation is atomic, so every seated leg is bought at the
    floor whatever the solve prefers; pasting that floor on afterwards spent
    money the optimum had not allocated and then rescaled the whole vector to
    fit, which could re-breach the floor and silently drop the candidate
    entirely.  Seating the floor as inventory the position already carries --
    the machinery the initial condition gave us -- and solving the remaining
    budget on top of it yields the constrained optimum directly, with no
    rescale and nothing to drop.
    """

    by_bucket = {row.bucket: row for row in snapshot.buckets}
    prices: list[float] = []
    caps: list[float] = []
    probabilities: list[float] = []
    held: list[float] = []
    for bucket in ordered:
        row = by_bucket[bucket]
        levels = list(row.ask_levels)
        if not levels:
            return []
        prices.append(float(levels[0].price))
        caps.append(sum(float(level.shares) for level in levels))
        probabilities.append(float(probabilities_by_bucket.get(bucket, 0.0)))
        held.append(
            float(current_shares.get(bucket, 0.0))
            + float(pending_shares.get(bucket, 0.0))
        )
    if any(price <= EPS or price >= 1.0 for price in prices):
        return []
    floor = max(float(minimum_clip_notional_usdc), 0.0)
    seat_cost_usdc = floor * len(ordered)
    if seat_cost_usdc > capital_available_usdc + EPS:
        return []
    seat_shares = [floor / price for price in prices]
    if any(
        seat > cap + EPS for seat, cap in zip(seat_shares, caps)
    ):
        # The offer cannot even seat the floor on some leg; the depth walk
        # below would refuse every candidate anyway.
        return []
    # Selling is forbidden and the seat window moves, so a station routinely
    # holds buckets this window cannot add to.  They still settle, so they
    # still shape the curve; dropping them would price their probability mass
    # as paying nothing and buy their coverage a second time.
    seated = set(ordered)
    outside_position: list[tuple[float, float]] = []
    for row in snapshot.buckets:
        if row.bucket in seated:
            continue
        inventory = (
            float(current_shares.get(row.bucket, 0.0))
            + float(pending_shares.get(row.bucket, 0.0))
        )
        if inventory > EPS:
            outside_position.append((
                float(probabilities_by_bucket.get(row.bucket, 0.0)),
                inventory,
            ))
    sunk_cost_usdc = sum(current_cost.values()) + sum(pending_cost.values())

    effective_bankroll_usdc = float(bankroll_usdc) / DEFAULT_KELLY_FRACTION
    free_budget_usdc = max(0.0, capital_available_usdc - seat_cost_usdc)

    results: list[dict[str, Any]] = []
    seen: set[tuple[float, ...]] = set()
    for fraction in (1.0, 0.5):
        try:
            stakes = solve_fractional_kelly(
                probabilities=probabilities,
                prices=prices,
                depth_caps=[
                    max(0.0, cap - seat) for cap, seat in zip(caps, seat_shares)
                ],
                budget_usdc=free_budget_usdc,
                bankroll_usdc=effective_bankroll_usdc,
                kelly_fraction=fraction,
                held_shares=[
                    hold + seat for hold, seat in zip(held, seat_shares)
                ],
                sunk_cost_usdc=sunk_cost_usdc + seat_cost_usdc,
                held_outside=outside_position,
            )
        except ContractError:
            continue
        # Round the free part down so the floor is never breached from below
        # and the budget never from above; the leftover cents stay unspent.
        cash = [
            floor + math.floor(stake * price * 100.0) / 100.0
            for stake, price in zip(stakes, prices)
        ]
        if sum(cash) > capital_available_usdc + EPS:
            continue
        key = tuple(cash)
        if key in seen:
            continue
        seen.add(key)
        shares = {
            bucket: current_shares[bucket] + pending_shares[bucket]
            for bucket in current_shares
        }
        costs = {
            bucket: current_cost[bucket] + pending_cost[bucket]
            for bucket in current_cost
        }
        usable = True
        for bucket, clip in zip(ordered, cash):
            acquired_shares, spent = _ask_depth_purchase(
                levels=by_bucket[bucket].ask_levels,
                cash_limit=float(clip),
            )
            if acquired_shares <= EPS or spent + EPS < float(clip):
                usable = False
                break
            shares[bucket] += acquired_shares
            costs[bucket] += spent
        if not usable:
            continue
        results.append({
            "candidate_id": "k-" + content_sha256({
                "ordered_support": tuple(ordered),
                "cash": key,
                "quote_version": snapshot.quote_version,
            })[:24],
            "shares_by_bucket": shares,
            "cost_by_bucket": costs,
            "pricing_basis": "official_ws_ask_depth",
            "candidate_kind": CANDIDATE_KIND_COMPLETE,
            "candidate_origin": "CONTINUOUS_LOG_OPTIMUM",
        })
    return results




def _assert_candidate_support_contract(
    candidates: Sequence[Mapping[str, Any]],
    *,
    ordered_support: Sequence[str],
) -> list[dict[str, Any]]:
    """Hold every candidate to what the stages downstream assume of it.

    ``candidate_kind`` is not a label.  ``basket_target_activated`` is a
    literal comparison against ``COMPLETE_BASKET_TARGET``, and the support
    that the gap, the Maker bundle and the emitted target rows are all
    computed over collapses to empty when that comparison fails.  A generator
    that produced a structurally perfect basket under a new name would
    therefore not be rejected -- it would silently turn a trade into a hold,
    which is what happened the first time a second generator was tried.

    So the kind stays a closed semantic class that the state machine reads,
    provenance moves to ``candidate_origin`` where it can vary freely, and
    the three invariants the rest of the pipeline relies on are checked here,
    at the boundary where a new generator is introduced, rather than assumed:

    - a complete basket gives every support bucket a positive increment,
      because activation is atomic;
    - no candidate moves a bucket outside the support, because the gap is
      only ever computed over the support and anything bought outside it
      would never be reconciled;
    - exactly one baseline exists, because it is the anchor every other
      candidate is compared against.

    Increments are measured against the baseline rather than read absolutely:
    the absolute figures carry existing inventory, which every candidate
    shares and none of them chose.
    """

    support = tuple(ordered_support)
    baselines = [
        row for row in candidates
        if row.get("candidate_kind") == CANDIDATE_KIND_BASELINE
    ]
    if len(baselines) != 1:
        raise ContractError("delta_v2_candidate_baseline_not_unique")
    baseline_shares = dict(baselines[0].get("shares_by_bucket") or {})
    for row in candidates:
        kind = str(row.get("candidate_kind") or "")
        if kind not in CANDIDATE_KINDS:
            raise ContractError("delta_v2_candidate_kind_invalid")
        shares = dict(row.get("shares_by_bucket") or {})
        for bucket, value in shares.items():
            increment = float(value) - float(
                baseline_shares.get(bucket, 0.0)
            )
            if bucket not in support and increment > EPS:
                raise ContractError(
                    "delta_v2_candidate_moves_bucket_outside_support"
                )
        if kind == CANDIDATE_KIND_COMPLETE and any(
            float(shares.get(bucket, 0.0))
            - float(baseline_shares.get(bucket, 0.0))
            <= EPS
            for bucket in support
        ):
            raise ContractError(
                "delta_v2_candidate_complete_basket_incomplete"
            )
        if kind == CANDIDATE_KIND_BASELINE and any(
            abs(
                float(shares.get(bucket, 0.0))
                - float(baseline_shares.get(bucket, 0.0))
            )
            > EPS
            for bucket in shares
        ):
            raise ContractError("delta_v2_candidate_baseline_not_flat")
    return [dict(row) for row in candidates]


def _hard_gates(
    snapshot: TargetBasketSnapshot,
    *,
    positive_gap: bool,
    budget_ok: bool,
    reservation_price_ok: bool = True,
    resource_probe_ok: bool,
) -> dict[str, bool]:
    return {
        "unique_ssot": True,
        "lineage_complete": bool(
            snapshot.evidence.posterior_sha256
            and snapshot.quote_version
            and snapshot.inventory_version
            and snapshot.order_state_version
        ),
        "registry_timezone_matches": True,
        "bucket_contract_matches": snapshot.contract_matches,
        "official_ws_fresh": snapshot.evidence_fresh,
        "inventory_known": True,
        "order_state_known": snapshot.order_state_known,
        "positive_inventory_gap": positive_gap,
        "budget_ok": budget_ok,
        "profit_floor_ok": True,
        "resource_probe_ok": resource_probe_ok,
        "reservation_price_ok": reservation_price_ok,
    }


def _market_center_ordinal(
    snapshot: TargetBasketSnapshot,
) -> tuple[int | None, str | None]:
    """The bucket the book prices richest, or None on an unquoted market.

    Prices are implied probabilities, so the richest mid is where the market
    puts the day.  A one-sided book still carries an opinion (its one side);
    a book with no quotes at all carries none, and the gate that consumes
    this cannot evaluate -- which is harmless, because there is also nothing
    to buy.
    """

    mids: list[tuple[float, int, str]] = []
    for row in snapshot.buckets:
        bid = float(row.bid_price) if row.bid_levels else None
        ask = float(row.ask_price) if row.ask_available else None
        if bid is not None and ask is not None:
            mid = (bid + ask) / 2.0
        elif bid is not None:
            mid = bid
        elif ask is not None:
            mid = ask
        else:
            continue
        if mid > EPS:
            mids.append((mid, int(row.ordinal), row.bucket))
    if not mids:
        return None, None
    top = max(mid for mid, _, _ in mids)
    leaders = [item for item in mids if item[0] >= top - EPS]
    if len(leaders) != 1:
        # A tied top is a market without a unique opinion, not a market
        # disagreeing with the model; the gate has nothing to act on.
        return None, None
    return leaders[0][1], leaders[0][2]


def compile_delta_v2_runtime_plan(
    *,
    snapshot: TargetBasketSnapshot,
    basket_policy: TargetBasketPolicy,
    delta_policy: DeltaV2PolicyArtifact,
    fill_resolver: FillResolver,
    capital_available_usdc: float,
    minimum_profit_floor: float,
    resource_probe_ok: bool,
    regime_direction_allowed: bool = True,
    confirmed_cancel_for_fok: bool = False,
    previous_probabilities: Mapping[str, float] | None = None,
    prior_centre_buckets: Sequence[str] = (),
) -> dict[str, Any]:
    """Compile the single-event q* and best no-live action under v2.

    ``previous_probabilities`` is the same station-day's probability at the
    previous checkpoint.  Rebalancing follows the probability, not the
    calendar: a bucket p(t) has moved toward may be added to, a bucket it
    has moved away from is held but never added to, and nothing is ever
    sold (user ruling, 2026-08-15).
    """

    basket_policy.validate()
    if not resource_probe_ok:
        raise ContractError("delta_v2_resource_probe_failed")
    if delta_policy.station_id not in {"*", snapshot.station_id}:
        raise ContractError("delta_v2_policy_station_mismatch")
    if delta_policy.stage not in {"*", snapshot.evidence.probability_stage}:
        raise ContractError("delta_v2_policy_stage_mismatch")
    (
        center,
        direction,
        base,
        bias,
        edge,
        desired,
        scores,
        candidate_lower,
        candidate_upper,
        expansion_layers,
        core_transition,
    ) = _choose_support(snapshot, basket_policy)
    market_center_ordinal, market_center_bucket = _market_center_ordinal(
        snapshot
    )
    # Diagnostic only, by the 2026-08-15 ruling: the objective may read
    # market prices and the market center, and the market center leaving the
    # model envelope no longer locks new money.  The capital gate this used
    # to be (established 2026-08-13 on the ZUUU two-system-starvation
    # specimen) fired exactly once in its whole life -- RCSS 2026-08-15,
    # unsettled -- so it retired with no measurable protective record.  The
    # failure it guarded, buying phantom edge against a market that knows
    # something, is covered where it was actually measured: the seat must
    # touch the probability core (31 of 40 live seats refused), and an
    # off-centre window's claim is deflated by the measured factor.  The
    # disagreement itself stays on the plan for audit.
    market_center_outside_envelope = (
        market_center_ordinal is not None
        and not (
            int(candidate_lower)
            <= market_center_ordinal
            <= int(candidate_upper)
        )
    )
    shortfall, breakthrough = _derive_dual_risk_scope(snapshot, center)
    # Risk families classify already-admitted support; they never bypass the
    # continuous-core candidate boundary or its evidence/price/Maker gates.
    shortfall &= desired
    breakthrough &= desired
    if not desired:
        raise ContractError("delta_v2_support_empty")
    (
        diagnosed_center,
        runner_up,
        center_lead,
        center_stable,
    ) = probability_center_diagnostics(
        {row.bucket: row.probability for row in snapshot.buckets},
        {row.bucket: row.ordinal for row in snapshot.buckets},
        basket_policy,
    )
    # The diagnostic center is the probability argmax.  It is the anchor only
    # for stages that publish no point estimate of their own; when one exists
    # the anchor is deliberately not the argmax, because an open-ended edge
    # bucket spans an unbounded interval and outranks the 1°C buckets it is
    # compared against without the forecast pointing anywhere near it.  Keeping
    # the equality unconditional would reject exactly the cases the anchor was
    # introduced to fix (ZUUU: anchor 33°C, argmax "35°C or higher").  The
    # runner-up/lead/stability diagnostics below remain informative either way.
    if (
        snapshot.evidence.model_center_ordinal is None
        and diagnosed_center != center.bucket
    ):
        raise ContractError("delta_v2_center_diagnostic_mismatch")
    probabilities = {row.bucket: row.probability for row in snapshot.buckets}
    by_bucket = {row.bucket: row for row in snapshot.buckets}
    ordered_support = tuple(
        sorted(desired, key=lambda bucket: (by_bucket[bucket].ordinal, bucket))
    )
    # Rank by the settled-outcome curve rather than by expected value; see
    # ``curve_score`` for why a symmetric variance charge flattens the curve
    # where log-wealth alone would sharpen it.
    #
    # The bankroll is the station's allotted risk capital, not the budget
    # still unspent.  Using the remainder would make the charge grow as the
    # book fills and diverge as it empties -- the last dollar priced as
    # infinitely precious -- so the risk preference would drift within a day
    # for no reason the evidence supports.  A depleted budget still compiles:
    # the feasible set collapses to the zero basket on its own, and the
    # ranking simply has nothing left to choose between.
    # The certainty gate, before any window is solved: if the market has
    # finished pricing this day, the cheapest thing to do is wait for the
    # next decision rather than pay for a consensus we helped form.
    lit_certainty = certainty_signals(
        probabilities=probabilities,
        prior_centre_buckets=prior_centre_buckets,
    )
    gated_by = [
        name for name in CERTAINTY_GATE_SIGNALS if lit_certainty.get(name)
    ]
    if gated_by:
        raise ContractError(
            "delta_v2_certainty_gate_lit:" + ",".join(gated_by)
        )
    bankroll_usdc = float(basket_policy.total_budget_usdc)
    objective_form = (
        CURVE_OBJECTIVE_FORM
        if bankroll_usdc > 0.0
        else VALUE_OBJECTIVE_FORM
    )
    # One argmax over (support, quantities), not two stages.
    #
    # The support used to be fixed by probability mass alone and handed to the
    # optimizer already chosen, which threw the book away before the objective
    # ever saw it.  Measured over 37 station-days with live asks, that support
    # carried our probability 0.688 against a market price of 0.883 -- an
    # expected value of -0.195, because the buckets we rate highest are the
    # ones the market prices richest.  The objective never needed a new term
    # to fix this: at its optimal stake the log-wealth score *is* the
    # divergence between our belief and the price (measured correlation 0.833
    # against KL), so simply letting it see more than one window makes it
    # choose on edge.  Given the choice it agreed with the mispricing-maximal
    # window on 31 of 37 days and with the incumbent probability-maximal one
    # on 3.
    support_windows, support_window_audit = _support_windows(
        snapshot=snapshot,
        desired=desired,
        probabilities=probabilities,
        fee_rate=basket_policy.fee_rate,
        maximum_support_buckets=basket_policy.maximum_support_buckets,
        minimum_support_buckets=basket_policy.minimum_support_buckets,
        center_bucket=center.bucket,
        probability_stage=str(snapshot.evidence.probability_stage or ""),
    )
    # The floor's live set is the evidence's, not the candidate support's.
    # ``ordered_support`` here is the continuous-core candidate boundary --
    # four buckets on an ordinary day -- and handing it to the solver as
    # "live" made the coverability test at the floor always pass in
    # full-protection mode: every window that did not blanket the whole
    # candidate core was floor-illegal, which re-manufactured the listing-day
    # ban that the clause 38 amendment removed.  Live means what the hard
    # bound still holds reachable: every bucket with probability mass.
    evidence_live_buckets = tuple(
        row.bucket
        for row in snapshot.buckets
        if row.probability > EPS
    )
    seated_support, target_solution, support_selection = (
        _solve_over_support_windows(
            windows=support_windows,
            window_audit=support_window_audit,
            live_buckets=evidence_live_buckets,
            snapshot=snapshot,
            probabilities=probabilities,
            shortfall=shortfall,
            breakthrough=breakthrough,
            delta_policy=delta_policy,
            basket_policy=basket_policy,
            capital_available_usdc=capital_available_usdc,
            objective_form=objective_form,
            bankroll_usdc=bankroll_usdc,
        )
    )
    ordered_support = seated_support
    desired = set(seated_support)
    # The risk scopes as intersected with the *seated* support -- the exact
    # tuples every action, the bundle and the realised value are scored
    # against.  Derived once and published on the plan, because a re-score
    # after execution sizing has to reproduce the authorization's inputs
    # byte for byte, and the diagnostic ``underreach_risk_buckets`` below is
    # the pre-seat intersection: close enough to look right in an audit and
    # wrong enough to score a different basket.
    scored_shortfall = tuple(sorted(shortfall & desired))
    scored_breakthrough = tuple(sorted(breakthrough & desired))
    target_shares = dict(target_solution["target_shares_by_bucket"])
    current_shares = {row.bucket: row.filled_shares for row in snapshot.buckets}
    current_cost = {row.bucket: row.filled_cost_usdc for row in snapshot.buckets}
    pending_shares = {row.bucket: row.pending_shares for row in snapshot.buckets}
    pending_cost = {
        row.bucket: row.pending_notional_usdc for row in snapshot.buckets
    }
    basket_target_activated = (
        target_solution.get("winner_candidate_kind")
        == "COMPLETE_BASKET_TARGET"
    )
    final_support = ordered_support if basket_target_activated else ()
    # The positive gap is a per-bucket quantity and every consumer below must
    # read the same one.  Deriving it once keeps candidate admission, Maker
    # clip sizing and the emitted target rows from drifting apart.
    gap_by_bucket = {
        bucket: (
            target_shares[bucket]
            - current_shares[bucket]
            - pending_shares[bucket]
        )
        for bucket in final_support
    }
    # Adding to a bucket the evidence has just moved away from is buying
    # into a fading claim: the objective re-solves from scratch each cycle
    # and would happily top up a leg whose probability fell, because the
    # solve only sees today's number, not its direction.  Selling is a
    # separate matter and stays forbidden -- the clamp below is one-sided,
    # so a faded leg is simply held at the size it already has.
    declining_buckets: list[str] = []
    if previous_probabilities:
        for bucket in final_support:
            before = previous_probabilities.get(bucket)
            if before is None:
                continue
            now = float(probabilities.get(bucket, 0.0))
            if now + EPS < float(before):
                declining_buckets.append(bucket)
                gap_by_bucket[bucket] = min(0.0, gap_by_bucket[bucket])
    execution_gap_support = tuple(
        bucket for bucket in final_support if gap_by_bucket[bucket] > EPS
    )
    event_id = f"{snapshot.station_id}-{snapshot.event_date}"
    actions: list[dict[str, Any]] = []
    candidate_descriptors: list[tuple[str, float, float, str]] = []
    maker_candidate_diagnostics: dict[str, str] = {
        bucket: "official_ws_ask_unavailable"
        for bucket in ordered_support
        if not by_bucket[bucket].ask_available
    }
    for bucket in execution_gap_support:
        row = by_bucket[bucket]
        if not row.ask_available:
            maker_candidate_diagnostics[bucket] = (
                "official_ws_ask_unavailable"
            )
            continue
        gap = gap_by_bucket[bucket]
        if gap <= EPS:
            continue
        reservation_grid = _reservation_grid(row=row, artifact=delta_policy)
        hedge_cap = _hedge_value_cap(
            bucket=bucket,
            centre_bucket=str(
                support_selection.get("ladder_center_bucket") or ""
            ),
            probability=probabilities.get(bucket),
            window=ordered_support,
        )
        maker_rungs = _maker_ladder(
            bid=row.bid_price,
            ask=row.ask_price,
            tick=row.price_tick_size,
            value_cap=hedge_cap,
        )
        maker_anchor = maker_rungs[0] if maker_rungs else None
        if maker_anchor is None:
            maker_candidate_diagnostics[bucket] = (
                "maker_hedge_value_cap_below_min_tick"
                if hedge_cap is not None
                and _floor_to_tick(hedge_cap, row.price_tick_size) <= EPS
                else "maker_post_only_price_unavailable_at_min_tick"
                if row.ask_price <= row.price_tick_size + EPS
                else "maker_post_only_market_anchor_unavailable"
            )
        elif not reservation_grid:
            maker_candidate_diagnostics[bucket] = (
                "maker_reservation_grid_empty"
            )
        for clip in delta_policy.cash_clip_grid:
            if (
                clip > capital_available_usdc + EPS
                or not reservation_grid
                or maker_anchor is None
            ):
                continue
            for rung in maker_rungs:
                action_clip = min(float(clip), gap * rung)
                if action_clip <= EPS:
                    continue
                for timeout in delta_policy.timeout_grid_seconds:
                    candidate_descriptors.append(
                        (bucket, action_clip, rung, f"MAKER:{timeout}")
                    )
        # FOK is intentionally a single zero-time candidate family and is
        # admitted only in the existing urgency window with sufficient depth.
        if (
            confirmed_cancel_for_fok
            and snapshot.time_left_minutes
            <= basket_policy.taker_time_threshold_minutes + EPS
        ):
            for clip in delta_policy.cash_clip_grid:
                if clip > capital_available_usdc + EPS:
                    continue
                shares, spent = _ask_depth_purchase(
                    levels=row.ask_levels,
                    cash_limit=float(clip),
                )
                if spent + EPS >= clip and shares <= gap + EPS:
                    candidate_descriptors.append(
                        (bucket, clip, row.ask_price, "FOK:0")
                    )
    if len(candidate_descriptors) > delta_policy.execution_weights.maximum_candidates:
        raise ContractError("delta_v2_action_candidate_bound_exceeded")
    # The fill distribution does not depend on how much cash a candidate
    # spends -- the resolver conditions on bucket, price, timeout, queue,
    # spread, depth and rung, and every one of those is fixed before the clip
    # is chosen.  So the eleven clips in the live grid asked the resolver the
    # same question eleven times.  At the store's measured 0.53ms per
    # resolution that is 1,408 lookups per station where 128 distinct ones
    # exist, and it is what made the candidate ceiling look like a limit on
    # ladder rungs rather than on clips.
    #
    # Memoised for this call only.  The resolver reads content-addressed
    # artifacts that a cycle cannot change under itself, so two identical
    # questions inside one solve have one answer by construction.
    resolved_fills: dict[tuple[Any, ...], Mapping[str, Any]] = {}

    def _resolve_fill(**kwargs: Any) -> Mapping[str, Any]:
        key = tuple(sorted(
            (name, value) for name, value in kwargs.items()
        ))
        cached = resolved_fills.get(key)
        if cached is None:
            cached = fill_resolver(**kwargs)
            resolved_fills[key] = cached
        return cached

    for bucket, clip, price, mode_timeout in candidate_descriptors:
        mode, timeout_raw = mode_timeout.split(":", 1)
        timeout = float(timeout_raw)
        row = by_bucket[bucket]
        gap = gap_by_bucket[bucket]
        initial_fill = _resolve_fill(
            station_id=snapshot.station_id,
            stage=snapshot.evidence.probability_stage,
            bucket=bucket,
            order_mode=mode,
            limit_price=price,
            timeout_seconds=timeout,
            queue_ahead_shares=row.maker_queue_ahead_shares,
            spread=max(0.0, row.ask_price - row.bid_price),
            depth=row.executable_depth_shares,
            quote_version=snapshot.quote_version,
            rung_offset_ticks=_rung_offset_ticks(
                price=price, bid=row.bid_price, tick=row.price_tick_size
            ),
        )
        distribution = initial_fill.get("fill_distribution")
        if not isinstance(distribution, Sequence):
            raise ContractError("delta_v2_fill_distribution_missing")
        costs = initial_fill.get("execution_costs")
        if not isinstance(costs, Mapping):
            raise ContractError("delta_v2_execution_costs_missing")
        action_kwargs = {
            "event_id": event_id,
            "bucket": bucket,
            "cash_clip": clip,
            "timeout_seconds": timeout,
            "order_mode": mode,
            "fill_distribution": distribution,
            "probabilities": probabilities,
            "current_shares_by_bucket": current_shares,
            "current_cost_by_bucket": current_cost,
            "confirmed_pending_shares_by_bucket": pending_shares,
            "confirmed_pending_cost_by_bucket": pending_cost,
            "target_shares_by_bucket": target_shares,
            "protected_buckets": ordered_support,
            "shortfall_buckets": scored_shortfall,
            "breakthrough_buckets": scored_breakthrough,
            "value_weights": delta_policy.value_weights,
            "execution_weights": delta_policy.execution_weights,
            "hard_gates": _hard_gates(
                snapshot,
                positive_gap=True,
                budget_ok=clip <= capital_available_usdc + EPS,
                resource_probe_ok=resource_probe_ok,
            ),
            "capital_available_usdc": capital_available_usdc,
            "minimum_profit_floor": minimum_profit_floor,
            "reservation_price": max(delta_policy.price_grid),
            "wait_cost": costs.get("wait", 0.0),
            "queue_risk": costs.get("queue", 0.0),
            "adverse_selection": costs.get("adverse_selection", 0.0),
            "reprice_risk": costs.get("reprice", 0.0),
            "capital_lock_cost": costs.get("capital_lock", 0.0),
            "fill_artifact_content_sha256": initial_fill.get(
                "fill_artifact_content_sha256"
            ),
            "fee_rate": basket_policy.fee_rate,
        }
        price_grid = (
            _reservation_grid(row=row, artifact=delta_policy)
            if mode == "MAKER"
            else tuple(
                sorted(
                    set(
                        price
                        for price in (*delta_policy.price_grid, row.ask_price)
                        if price + EPS >= row.ask_price
                    )
                )
            )
        )
        reservation = solve_reservation_price_v2(
            cash_clip=clip,
            price_grid=price_grid,
            action_kwargs={
                key: value
                for key, value in action_kwargs.items()
                if key not in {"cash_clip", "limit_price"}
            },
        )
        reserve = reservation.get("reservation_price")
        if reserve is None and mode != "MAKER":
            continue
        # A Maker leg may be individually negative while the complete basket
        # is jointly positive because floor/CVaR/concentration are nonlinear.
        # Keep the non-crossing market anchor as a bounded bundle candidate;
        # only the joint bundle selector may authorize it.
        reservation_price = (
            float(reserve) if reserve is not None else float(price)
        )
        if mode == "MAKER":
            maker_limit = min(
                float(price),
                _floor_to_tick(reservation_price, row.price_tick_size),
            )
            clip = min(float(clip), gap * maker_limit)
            if (
                clip <= EPS
                or maker_limit + EPS < row.bid_price
                or maker_limit >= row.ask_price - EPS
            ):
                continue
            price = maker_limit
            action_kwargs["cash_clip"] = clip
        elif (
            price > reservation_price + EPS
            or clip / price > gap + EPS
        ):
            continue
        resolved_fill = _resolve_fill(
            station_id=snapshot.station_id,
            stage=snapshot.evidence.probability_stage,
            bucket=bucket,
            order_mode=mode,
            limit_price=price,
            timeout_seconds=timeout,
            queue_ahead_shares=row.maker_queue_ahead_shares,
            spread=max(0.0, row.ask_price - row.bid_price),
            depth=row.executable_depth_shares,
            quote_version=snapshot.quote_version,
            rung_offset_ticks=_rung_offset_ticks(
                price=price, bid=row.bid_price, tick=row.price_tick_size
            ),
        )
        final_distribution = resolved_fill.get("fill_distribution")
        final_costs = resolved_fill.get("execution_costs")
        if not isinstance(final_distribution, Sequence):
            raise ContractError("delta_v2_fill_distribution_missing")
        if not isinstance(final_costs, Mapping):
            raise ContractError("delta_v2_execution_costs_missing")
        action_kwargs.update({
            "fill_distribution": final_distribution,
            "reservation_price": reservation_price,
            "limit_price": price,
            "wait_cost": final_costs.get("wait", 0.0),
            "queue_risk": final_costs.get("queue", 0.0),
            "adverse_selection": final_costs.get("adverse_selection", 0.0),
            "reprice_risk": final_costs.get("reprice", 0.0),
            "capital_lock_cost": final_costs.get("capital_lock", 0.0),
            "fill_artifact_content_sha256": resolved_fill.get(
                "fill_artifact_content_sha256"
            ),
        })
        actions.append(evaluate_action_v2(**action_kwargs))
    maker_actions = [
        action for action in actions
        if str(dict(action.get("action") or {}).get("order_mode") or "").upper()
        == "MAKER"
    ]
    maker_action_buckets = {
        str(dict(action.get("action") or {}).get("bucket") or "")
        for action in maker_actions
    }
    for bucket in execution_gap_support:
        if bucket not in maker_action_buckets:
            maker_candidate_diagnostics.setdefault(
                bucket,
                "maker_candidate_rejected_by_reservation_or_size",
            )
    selection = select_complete_maker_bundle_v2(
        maker_actions,
        required_buckets=execution_gap_support,
        probabilities=probabilities,
        current_shares_by_bucket=current_shares,
        current_cost_by_bucket=current_cost,
        target_shares_by_bucket=target_shares,
        protected_buckets=ordered_support,
        shortfall_buckets=scored_shortfall,
        breakthrough_buckets=scored_breakthrough,
        value_weights=delta_policy.value_weights,
        execution_weights=delta_policy.execution_weights,
        capital_available_usdc=capital_available_usdc,
        minimum_profit_floor=minimum_profit_floor,
        fee_rate=basket_policy.fee_rate,
    )
    # Urgent FOK remains a single-bucket close-out mechanism.  It is never
    # allowed to coexist with a Maker bundle or bypass an incomplete basket.
    if (
        selection.get("authorized") is not True
        and len(execution_gap_support) == 1
        and confirmed_cancel_for_fok
    ):
        fok_selection = select_action_v2(
            [
                action for action in actions
                if str(
                    dict(action.get("action") or {}).get("order_mode") or ""
                ).upper() == "FOK"
            ],
            execution_weights=delta_policy.execution_weights,
        )
        fok_selected = fok_selection["selected_action"]
        if fok_selected.get("authorized") is True:
            selection = {
                "schema": "delta_basket_bundle_selection_v2",
                "required_buckets": list(execution_gap_support),
                "selected_actions": [fok_selected],
                "selected_bundle": None,
                "selected_action_sha256s": [
                    str(fok_selected.get("content_sha256") or "")
                ],
                "selected_score": float(
                    fok_selected.get("delta_basket_score_v2") or 0.0
                ),
                "capital_reserved_usdc": float(
                    fok_selected.get("capital_reserved_usdc") or 0.0
                ),
                "basket_complete": True,
                "authorized": True,
                "blockers": [],
                "hold_selected": False,
                "execution_mode": "SINGLE_BUCKET_FOK",
                "calls_order_gateway": False,
                "places_orders": False,
                "orders_submitted": 0,
                "live_submit_authorized": False,
                "content_sha256": content_sha256({
                    "schema": "delta_basket_bundle_selection_v2",
                    "selected_action_sha256s": [
                        str(fok_selected.get("content_sha256") or "")
                    ],
                    "execution_mode": "SINGLE_BUCKET_FOK",
                }),
            }
    if not regime_direction_allowed and selection.get("authorized") is True:
        selection = {
            **selection,
            "selected_actions": [],
            "selected_bundle": None,
            "selected_action_sha256s": [],
            "selected_score": 0.0,
            "capital_reserved_usdc": 0.0,
            "basket_complete": False,
            "authorized": False,
            "blockers": ["regime_direction_not_allowed"],
            "hold_selected": True,
        }
    if snapshot.terminal and selection.get("authorized") is True:
        selection = {
            **selection,
            "selected_actions": [],
            "selected_bundle": None,
            "selected_action_sha256s": [],
            "selected_score": 0.0,
            "capital_reserved_usdc": 0.0,
            "basket_complete": False,
            "authorized": False,
            "blockers": ["terminal_stop_adding"],
            "hold_selected": True,
        }
    realised = evaluate_basket_value_v2(
        probabilities=probabilities,
        shares_by_bucket=current_shares,
        cost_by_bucket=current_cost,
        protected_buckets=ordered_support,
        shortfall_buckets=scored_shortfall,
        breakthrough_buckets=scored_breakthrough,
        weights=delta_policy.value_weights,
        target_shares_by_bucket=target_shares,
    )
    targets: list[dict[str, Any]] = []
    selected_evaluations = [
        dict(row) for row in selection.get("selected_actions", ())
        if isinstance(row, Mapping)
    ]
    selected_actions_by_bucket = {
        str(dict(row.get("action") or {}).get("bucket") or ""): dict(
            row.get("action") or {}
        )
        for row in selected_evaluations
    }
    selected_buckets = tuple(sorted(
        bucket for bucket in selected_actions_by_bucket if bucket
    ))
    if set(selected_buckets) - set(execution_gap_support):
        raise ContractError(
            "delta_v2_selected_action_outside_execution_gap_support"
        )
    if selection.get("authorized") is True and set(selected_buckets) != set(
        execution_gap_support
    ):
        raise ContractError("delta_v2_selected_bundle_incomplete")
    for bucket in final_support:
        row = by_bucket[bucket]
        is_selected = (
            selection.get("authorized") is True
            and bucket in selected_actions_by_bucket
        )
        action = selected_actions_by_bucket.get(bucket, {}) if is_selected else {}
        targets.append({
            "bucket": bucket,
            "ordinal": row.ordinal,
            "role": (
                "BASE" if bucket in base else "BIAS" if bucket in bias else "EDGE"
            ),
            "base_score": scores.get(bucket, (0.0, 0.0, 0.0))[0],
            "bias_score": scores.get(bucket, (0.0, 0.0, 0.0))[1],
            "edge_score": scores.get(bucket, (0.0, 0.0, 0.0))[2],
            "target_shares": target_shares[bucket],
            "filled_shares": row.filled_shares,
            "filled_cost_usdc": row.filled_cost_usdc,
            "pending_shares": row.pending_shares,
            "pending_notional_usdc": row.pending_notional_usdc,
            "delta_shares": max(0.0, gap_by_bucket[bucket]),
            "clip_shares": float(action.get("maximum_shares") or 0.0),
            "clip_notional_usdc": float(action.get("cash_clip") or 0.0),
            "candidate_price": float(action.get("limit_price") or 0.0),
            "limit_price": float(action.get("limit_price") or 0.0),
            "effective_price": float(action.get("limit_price") or 0.0),
            "execution_candidate": (
                "TAKER_FOK"
                if action.get("order_mode") == "FOK"
                else "MAKER"
                if action.get("order_mode") == "MAKER"
                else "NONE"
            ),
            "execution_contract": (
                FOK_EXECUTION_CONTRACT
                if action.get("order_mode") == "FOK"
                else MAKER_EXECUTION_CONTRACT
                if action.get("order_mode") == "MAKER"
                else ""
            ),
            "post_only": action.get("order_mode") == "MAKER",
            "cancel_acknowledged": (
                bool(confirmed_cancel_for_fok)
                if action.get("order_mode") == "FOK"
                else False
            ),
            "target_price_id": (
                "delta_v2_price_"
                + str(
                    next(
                        (
                            value.get("content_sha256")
                            for value in selected_evaluations
                            if str(
                                dict(value.get("action") or {}).get("bucket")
                                or ""
                            )
                            == bucket
                        ),
                        "",
                    )
                )[:32]
                if is_selected
                else ""
            ),
            "execution_reason_codes": (
                [] if is_selected else ["delta_v2_action_not_selected"]
            ),
        })
    reason_codes: list[str] = []
    if selection.get("authorized") is not True:
        reason_codes.extend(str(value) for value in selection.get("blockers", ()))
        reason_codes.extend(
            f"{reason}:{bucket}"
            for bucket, reason in sorted(
                maker_candidate_diagnostics.items()
            )
        )
        reason_codes.append("delta_v2_complete_basket_hold_selected")
    if not center_stable:
        reason_codes.append("center_probability_lead_unstable")
    if not regime_direction_allowed:
        reason_codes.append("regime_direction_not_allowed")
    delta_v2_audit = {
        "schema": "delta_basket_score_authorization_v2",
        "selected_actions": selected_evaluations,
        "selected_bundle_sha256": selection.get("selected_bundle_sha256"),
        "authorized": selection.get("authorized") is True,
        "blockers": list(selection.get("blockers", ())),
        "delta_basket_score_v2": float(
            selection.get("selected_score", 0.0)
        ),
        "policy_artifact_id": delta_policy.artifact_id,
        "policy_artifact_sha256": delta_policy.content_sha256,
        "fill_distribution_artifact_sha256": (
            delta_policy.fill_distribution_artifact_sha256
        ),
        "realised_value_sha256": realised["content_sha256"],
        "calls_order_gateway": False,
        "places_orders": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }
    delta_v2_audit["content_sha256"] = content_sha256(delta_v2_audit)
    selected_before = tuple(
        row.bucket for row in snapshot.buckets if row.selected_before
    )
    ordered_core = tuple(
        str(bucket)
        for bucket in core_transition["next_core_buckets"]
    )
    basket_state = resolve_basket_state(
        bucket_ordinals={
            row.bucket: row.ordinal for row in snapshot.buckets
        },
        desired_support=final_support,
        selected_before=selected_before,
        positive_gap_buckets=execution_gap_support,
        execution_authorized=selection.get("authorized") is True,
        order_state_known=snapshot.order_state_known,
        stop_adding=snapshot.terminal,
    )
    identity = {
        "schema": SCHEMA,
        "snapshot_sha256": snapshot.content_sha256,
        "quote_version": snapshot.quote_version,
        "inventory_version": snapshot.inventory_version,
        "order_state_version": snapshot.order_state_version,
        "station_id": snapshot.station_id,
        "event_date": snapshot.event_date,
        "observed_at": snapshot.observed_at,
        "market_id": snapshot.market_id,
        "state": "TRADE" if selection.get("authorized") is True else "HOLD",
        "basket_state": basket_state.state.value,
        "basket_state_reason": basket_state.reason,
        "basket_state_transition": basket_state.to_dict(),
        "reason_codes": list(dict.fromkeys(reason_codes)),
        "center_bucket": center.bucket,
        "runner_up_bucket": runner_up,
        "center_lead_probability": center_lead,
        "center_lead_threshold_probability": (
            basket_policy.center_lead_min_probability
        ),
        "center_stable": center_stable,
        "direction": direction.value if isinstance(direction, Direction) else str(direction),
        "diagnostic_lower_ordinal": snapshot.evidence.model_envelope_lower_ordinal,
        "diagnostic_upper_ordinal": snapshot.evidence.model_envelope_upper_ordinal,
        "candidate_lower_ordinal": candidate_lower,
        "candidate_upper_ordinal": candidate_upper,
        "market_center_ordinal": market_center_ordinal,
        "market_center_bucket": market_center_bucket,
        "envelope_entry_gate": {
            "outside": market_center_outside_envelope,
            "market_center_ordinal": market_center_ordinal,
            "market_center_bucket": market_center_bucket,
            "candidate_lower_ordinal": int(candidate_lower),
            "candidate_upper_ordinal": int(candidate_upper),
            "enforced": False,
            "retired": "2026-08-15_ruling_diagnostic_only",
        },
        "disagreement_expansion_layers": expansion_layers,
        "underreach_risk_buckets": sorted(shortfall),
        "upside_breakthrough_risk_buckets": sorted(breakthrough),
        "current_core_buckets": list(snapshot.current_core_buckets),
        "current_direction": (
            snapshot.current_direction.value
            if snapshot.current_direction is not None
            else None
        ),
        "core_buckets": list(ordered_core),
        "core_transition": core_transition,
        "candidate_support": list(ordered_support),
        "final_support": list(final_support),
        # Exactly what the bundle was scored against, so a re-score at
        # execution sizing reproduces the authorization instead of
        # approximating it.
        "bundle_scored_scopes": {
            "protected_buckets": list(ordered_support),
            "shortfall_buckets": list(scored_shortfall),
            "breakthrough_buckets": list(scored_breakthrough),
        },
        "basket_completion_required": True,
        "basket_target_activated": basket_target_activated,
        "execution_gap_support": list(execution_gap_support),
        "selected_buckets": list(selected_buckets),
        "excluded_candidate_buckets": [
            {
                "bucket": bucket,
                "reason": "zero_joint_target_inventory",
            }
            for bucket in ordered_support
            if bucket not in final_support
        ],
        "selected_before": list(selected_before),
        "targets": targets,
        "target_solution": target_solution,
        "delta_v2_action_selection": selection,
        "maker_candidate_diagnostics": dict(
            sorted(maker_candidate_diagnostics.items())
        ),
        "delta_v2_audit": delta_v2_audit,
        "scenario_pnl_usdc": realised["scenario_pnl"],
        "expected_pnl_usdc": realised["expected_pnl"],
        "profit_floor_usdc": realised["profit_floor"],
        "cvar_usdc": realised["cvar"],
        "basket_objective_form": objective_form,
        "basket_curve_variance": target_solution.get("target_variance"),
        "basket_curve_score": target_solution.get("target_curve_score"),
        # The curve dials the charter is steered by.  They exist in the solve
        # identity either way; carrying them onto the plan is what makes the
        # win-rate target an auditable daily number instead of an intention --
        # the whole reason the 21-plan measurement that exposed the lottery
        # shape had to be reconstructed by hand from scenario curves.
        "basket_win_probability": target_solution.get(
            "target_win_probability"
        ),
        "basket_odds_ratio": target_solution.get("target_odds_ratio"),
        "basket_support_floor_pnl_usdc": target_solution.get(
            "target_support_floor_pnl"
        ),
        "basket_support_floor_fraction": target_solution.get(
            "support_scenario_floor_fraction"
        ),
        "basket_floor_rejected_candidate_count": target_solution.get(
            "floor_rejected_candidate_count"
        ),
        # Which windows were on the table, what each was worth, and why this
        # one was seated.  Without it a support decision is unreviewable: the
        # incumbent rule was wrong for months precisely because nothing
        # recorded the alternatives it never considered.
        "basket_support_selection": support_selection,
        "total_target_shares": sum(target_shares.values()),
        "total_delta_notional_usdc": float(
            selection.get("capital_reserved_usdc") or 0.0
        ),
        "basket_optimizer_id": "DeltaBasketScoreV2",
        "basket_optimizer_converged": True,
        # Held at their existing size because p(t) moved away from them this
        # cycle: no top-up, and never a sale.
        "probability_declined_buckets": tuple(declining_buckets),
        "probability_direction_source": (
            "previous_checkpoint" if previous_probabilities else "absent"
        ),
        "fok_cancel_acknowledged": bool(confirmed_cancel_for_fok),
        "target_only": True,
        "places_orders": False,
        "writes_production_db": False,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
    }
    return {
        **identity,
        "plan_id": "delta_v2_plan_" + content_sha256(identity)[:32],
        "content_sha256": content_sha256(identity),
    }


__all__ = ["SCHEMA", "compile_delta_v2_runtime_plan"]
