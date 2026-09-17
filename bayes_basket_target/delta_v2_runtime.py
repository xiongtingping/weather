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
import json
import math
from typing import Any, Callable, Collection, Mapping, Sequence

from .basket_kernel.contracts import (
    EPS,
    ContractError,
    Direction,
    TargetBasketPolicy,
    TargetBasketSnapshot,
    content_sha256,
)
from .basket_kernel import delta_v2 as _kernel_delta_v2
from .observed_high_floor import (
    CHARTER_125_OBSERVED_HIGH_FLOOR,
    observed_high_floor_enabled,
)
from .basket_kernel.delta_v2 import (
    SEAT_SHAPE_RULE_VERSION,
    CAPITAL_LAYER_RULE_VERSION,
    CONSOLIDATION_TILT_FLOOR_VERSION,
    CURVE_BANKROLL_CONVENTION,
    curve_effective_bankroll_v1,
    LEG_FLOOR_PRICE_BASIS,
    LEG_FLOOR_RULE_VERSION,
    consolidation_tilt_floor_usdc,
    leg_below_floor,
    venue_minimum_shares_v1,
    CENTER_WINDOW_CLAIM_BANDS,
    BETA_EDGE_MODE,
    BETA_EDGE_RULE_VERSION,
    BETA_PRICE_BASIS,
    BETA_TABLE_KEY,
    beta_belief_v1,
    beta_conditional_edge_v3,
    beta_lead_band,
    SIZING_CLAIM_BANDS,
    sizing_claim_factor,
    EXCHANGE_MINIMUM_ORDER_NOTIONAL_USDC,
    CENTER_WINDOW_CLAIM_DEFAULT,
    _parse_claim_bands,
    center_window_claim_factor,
    convergence_claim_factor,
    convergence_state_v1,
    CONVERGENCE_CLAIM_MODE,
    CONVERGENCE_CLAIM_FACTOR,
    CONVERGENCE_CLAIM_HORIZON_HOURS,
    CONVERGENCE_CLAIM_RULE_VERSION,
    CONVERGENCE_CLAIM_STAGES,
    CONVERGENCE_CLAIM_MIN_AGREEMENT,
    CONVERGENCE_CLAIM_MIN_CLAIM_RISE,
    CONVERGENCE_CLAIM_MIN_PRICE_FALL,
    EFFECTIVE_EDGE_P0M_STAGES,
    EFFECTIVE_EDGE_P1_STAGES,
    EFFECTIVE_EDGE_STAGES,
    effective_edge_lambda,
    effective_edge_lambda_for_stage,
    effective_edge_lambda_from_tables,
    effective_edge_lambda_resolved,
    CLAIM_EDGE_THERMOMETER_MODE,
    CLAIM_EDGE_THIN_BAND_MAX,
    effective_edge_bucket_lambda_for_stage,
    floor_first_edge_weights_v1,
    off_center_claim_factor,
    window_claim_factor,
    probability_core,
    PROBABILITY_CORE_MASS,
    ECONOMIC_HURDLE_V1,
    ECONOMIC_HURDLE_RULE_VERSION,
    clears_economic_hurdle_v1,
    economic_hurdle_usdc,
    CHARTER_93_SUPPORT_FLOOR,
    SUPPORT_SCENARIO_FLOOR_RULE_VERSION,
    support_scenario_floor_v1,
    whole_basket_edge_delta_v1,
    BUCKET_TABLE_KEY,
    MARGINAL_COVERAGE_RULE_VERSION,
    marginal_coverage_reading_v1,
    ROBUST_CLAIM_RULE_VERSION,
    robust_window_claim_v1,
    CANDIDATE_KIND_BASELINE,
    CANDIDATE_KIND_COMPLETE,
    CANDIDATE_KINDS,
    CURVE_OBJECTIVE_FORM,
    EXCHANGE_LOT_TOP_UP,
    CHAIN_OPENING_STAGE,
    execution_authorization_rule_v1,
    DECISION_MOMENT_RULE_VERSION,
    ORDER_END_MOMENT_RULE_VERSION,
    edge_state_v1,
    basket_holding_signature_v1,
    is_decision_moment_v1,
    EXECUTION_AUTHORIZATION_JOINT_VALUE,
    KELLY_CANDIDATE_ORIGIN,
    LADDER_BALANCE_CANDIDATE_ORIGIN,
    KELLY_OBJECTIVE_FORM,
    PRODUCTION_EXECUTION_AUTHORIZATION,
    VALUE_OBJECTIVE_FORM,
    chain_topup_growth_v1,
    clamp_declining_gaps_v2,
    contiguous_edge_clamp_v2,
    deflate_window_partition_v1,
    probability_vector_identity_v1,
    whole_basket_kelly_stake_v1,
    KELLY_TOPUP_BASIS,
    KELLY_TOPUP_BASIS_RULE_VERSION,
    kelly_target_budget_usdc_v1,
    LADDER_BALANCE_LEVEL_RULE_VERSION,
    ask_depth_cost_v1,
    ask_depth_walk_v1,
    ladder_balance_targets_v1,
    COMPLETION_CUTOFF_HOURS,
    completion_conditioned_claim_v1,
    completion_floor_delta_v1,
    completion_repair_exemption_v1,
    CHARTER_143_COMPLETION,
    COMPLETION_143_RULE_VERSION,
    completion_143_basket_type_v1,
    completion_143_exemption_v1,
    completion_143_targets_v1,
    COMPLETION_RISK_BUDGET_RULE_VERSION,
    completion_budget_usdc_v1,
    completion_risk_budget_v1,
    evaluate_action_v2,
    evaluate_basket_value_v2,
    raise_sub_lot_targets_v2,
    select_action_v2,
    select_complete_maker_bundle_v2,
    solve_reservation_price_v2,
    solve_target_inventory_v2,
)
from .basket_kernel.support import (
    mean_center_bucket_v1,
    mean_mode_skew_reading_v1,
    mean_mode_skew_sign_v1,
    SKEW_STATE_BY_SIGN,
    _choose_support,
    _derive_dual_risk_scope,
    probability_center_diagnostics,
)
from .basket_kernel.state_machine import resolve_basket_state
from .basket_kernel.posterior_v1 import (
    POSTERIOR_MODE,
    POSTERIOR_RULE,
    POSTERIOR_TABLE_KEY,
    posterior_draw_vectors_v1,
    posterior_vector_v1,
    window_mass as posterior_window_mass,
)
from .basket_kernel.valuation_v1 import (
    MARKET_REFERENCE_RULE as VALUATION_MARKET_REFERENCE_RULE,
    VALUATION_MODE,
    VALUATION_RULE,
    VALUATION_TABLE_KEY,
    action_value_v1 as valuation_action_value_v1,
    beta_draw_vectors as valuation_draw_vectors_v1,
    capacity_v1 as valuation_capacity_v1,
    market_reference_v1,
    valuation_vector_v1,
    window_mass as valuation_window_mass,
)
from .candidate_window_evidence import candidate_windows_v1
from .delta_v2_artifact import DeltaV2PolicyArtifact
from .shadow_objective import solve_fractional_kelly


SCHEMA = "delta_basket_score_runtime_plan_v2"

# Charter 131 (user ruling, 2026-09-12): the seat's shape is fixed by the
# served distribution's three-state reading -- mean bucket against mode
# bucket, gated at 0.35 sigma (charters 54/75/122) -- and by nothing else.
# A symmetric day seats {c-1, c, c+1}; a skewed day seats {c, c+d} or
# {c, c+d, c+2d}, c the mean bucket and d the tail side, the mode bucket
# deliberately outside; a symmetric P1 day may also seat {c, c+h}, main
# rung plus hedge, h the heavier live neighbour.  Two to three rungs at
# every stage: the three-rung window ranks first on effective mass and the
# two-rung one seats only where the three-rung fails a gate (Q6).  This
# retires charter 50's three-to-four width, charter 55's extension gate and
# its placement arms, charter 59's fallback pass, charter 79's symmetric
# seat and charter 122's width band.  The shapes are refused at
# enumeration (funnel gate ``shape``), so the audit closes on them like
# any other gate.  Read at 09-09..09-12 before landing: 61-75% of
# admissible seats already had the mean bucket in the middle; the skewed
# shapes cost 0.16-0.22 less and hit 0.15-0.22 less on 20-odd settled
# days -- the ledger is the instrument, not that reading.
SEAT_MIN_BUCKETS = 2
SEAT_MAX_BUCKETS = 3
# Charter 131 (11): P0 may not open four rungs, but a P0-M or P1 re-seat may
# leave the chain holding four -- held legs outside the new window stay
# held and are never bought (Q7) -- and charter 90's TX-entry span still
# reads the chain's four (Q9).
CHAIN_MAX_BUCKETS = 4
# Research arm (2026-09-06, user: "edge 折扣后 edge 越大份额越多，先量"): how
# the charter-72 Kelly stake is split across the seat's legs.  ``equal`` is
# the shipped rule -- one share count for every rung, so the seat pays the
# same whichever rung settles.  ``centre_tilt`` is the 2026-08-17 shape
# (``_centre_anchored_candidates``: distance decay from the centre, leaned
# by positive edge, far rungs floored) that charter 72 left generated but
# never winning.  ``edge_weighted`` is the two-段 shape (user ruling,
# 2026-09-08): an equal bed on every rung first, then the basket's own floor
# cushion ``1 - sum(c)`` tilted toward the rungs whose *realisable* edge
# ``lambda(a)*p - c`` is positive, read on the charter-68 bucket arm.  The
# kernel owns it (``floor_first_edge_weights_v1``) so both lanes run one
# implementation.  Until 2026-09-08 this arm floored each rung at a fraction
# of the best rung's weight, which says nothing about what the basket costs:
# measured on that day's book every candidate it produced was refused by the
# charter-93 floor (SAEZ -0.246 against a 0.30 premium), so the arm had never
# been able to trade.  The floor is now the shape's own construction and the
# refusal cannot recur.  The stake is the same number under every shape, only
# the split changes, so an arm measures the shape and nothing else.  The
# paper ledger imports the switch and hashes it into its rule fingerprint
# away from ``equal``.
KELLY_SHARE_SHAPES = ("equal", "centre_tilt", "edge_weighted")
KELLY_SHARE_SHAPE = (
    os.environ.get("PM_KELLY_SHARE_SHAPE", "").strip().lower() or "equal"
)
if KELLY_SHARE_SHAPE not in KELLY_SHARE_SHAPES:
    raise RuntimeError(f"unknown Kelly share shape: {KELLY_SHARE_SHAPE}")
# Companion research switch: what the Kelly candidate does with a rung the
# chain already holds at or above its target.  ``atomic`` is charter 72 as
# shipped -- any such rung and the whole basket stands pat (the 2026-08-12
# atomicity ruling).  ``short_legs`` keeps the whole-basket stake but judges
# each rung on its own gap: rungs below target are bought up to it, rungs at
# or above are left alone -- which the candidate contract admits since
# charter 84 (a complete basket adds to at least one rung and leaves nothing
# unheld).  First measured 2026-09-06 on the tilt arms, where the atomic
# rule refused roughly half the P1 top-ups; generalised to every shape the
# same day when the user asked for the whole-basket stake *and* the target
# leg to be read together.  Applies to every share shape, ``equal`` included.
KELLY_SHAPE_TOPUP_MODES = ("atomic", "short_legs")
KELLY_SHAPE_TOPUP = (
    os.environ.get("PM_KELLY_SHAPE_TOPUP", "").strip().lower() or "atomic"
)
if KELLY_SHAPE_TOPUP not in KELLY_SHAPE_TOPUP_MODES:
    raise RuntimeError(f"unknown Kelly shape top-up mode: {KELLY_SHAPE_TOPUP}")
MAKER_EXECUTION_CONTRACT = "maker_post_only"
FOK_EXECUTION_CONTRACT = "single_bucket_fok"
FillResolver = Callable[..., Mapping[str, Any]]


def _floor_to_tick(price: float, tick: float) -> float:
    steps = math.floor((price + EPS) / tick)
    return round(steps * tick, 12)


# The maker-era per-leg value clamp (lambda x claim, centre and shoulders
# exempt; charter 63) was retired on 2026-09-03.  Under charter 65 the
# taker family is the law and the taker candidate path never read it: the
# purchase ladder returned the official ask depth before consulting the
# cap, and the only other reader fed maker-only anchors and diagnostics.
# The ledger's maker_resting research arm keeps its own lambda knobs.
# Ladder completeness re-priced as risk (user ruling, 2026-08-24; twice
# corrected the same day).  The lifecycle itself is untouched: every leg --
# tails included, they are the hedge legs that flatten the variance and shape
# the odds curve -- keeps fishing, escalating and crossing inside its own value
# ceiling, which is what "try to fill" means at prices we already judge fair.
# What the budget governs is the premium alone.  While the basket's uncovered
# claim mass (the claimed probability still sitting on open rungs) exceeds it,
# a core or shoulder leg may cross above its clamp, up to COVERAGE_VALUE_LAMBDA
# times the live claim -- at 1.0 the EV-neutral maximum, never beyond, because
# paying more than a leg is worth converts risk into a certain loss.  A tail
# leg never commands the premium: its clamp is the line, filled cheaply or not
# at all.  The tiers are live, not seated: the core is wherever the belief in
# force at the re-pricing round puts the most mass, its neighbours are the
# shoulders, and the roles walk intraday with the distribution.
#
# There is deliberately no withdrawal in this contract (user ruling,
# 2026-08-24): risk the premium cannot buy down leaves the rungs resting at
# their ceilings, and the only trigger that ever withdraws an order remains
# the belief gate.  Negative budget restores the legacy contract; it is the
# control cell.
# Default OFF (negative), by measurement rather than by preference.  Fourteen
# chains carried a premium fill on the 2026-08-15..23 book: one won (+58.2),
# eleven lost, and the set ex-best sums to -48.0; the affected chains split 26
# better / 67 worse.  The premium binds exactly where the market prices a
# covered bucket above our clamp -- its most confident ground -- and this book
# has now measured three separate times that paying up there buys the miss,
# not the coverage.  The contract stays wired so a forward lane can re-price
# it under live calibration; enable with PM_MAKER_COVERAGE_RISK_BUDGET=0.10.
MAKER_COVERAGE_RISK_BUDGET = float(
    os.environ.get("PM_MAKER_COVERAGE_RISK_BUDGET", "-1") or 0.0
)
MAKER_COVERAGE_VALUE_LAMBDA = float(
    os.environ.get("PM_MAKER_COVERAGE_LAMBDA", "0.70") or 0.0
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
#
# CLOSED by user ruling 2026-08-25: both signs default off.  The numbers
# above were read on the pre-charter-54/55/58 rule set under the touch
# fill assumption, and every measurement from that era is due a re-read
# on the honest book (the fill model alone halved the book's return when
# it was corrected).  The machinery stays wired; re-light either sign
# with PM_CERTAINTY_GATE=stable,sharp (production) -- the ledger follows
# this default by construction and can diverge via
# PAPER_LEDGER_CERTAINTY_GATE for a sweep.
CERTAINTY_GATE_SIGNALS = tuple(
    part.strip() for part in
    (os.environ.get("PM_CERTAINTY_GATE", "") or "").split(",")
    if part.strip()
)
CERTAINTY_STABLE_LOOKBACK = 3
CERTAINTY_SHARP_TOP2 = 0.60
# The consolidation stake tilt, production side (user ruling 2026-08-28;
# G1 of the go-live alignment audit).  The ledger measured it twice --
# maker-era five-arm ranking, then the taker baseline at -5.32% -> -3.70%
# with drop-1/2/3 at +71.9/+61.1/+51.0 -- and the go-live audit found it
# living only there, which live trading cannot tolerate: the book would
# score stakes production never places.  Same two lights, same 0.10, same
# minimum-clip floor as the ledger.
#
# The scope is the opener's, exactly as the ledger measured it: the P0
# opener's lights set the whole chain's factor, carried in one row of
# cross-cycle state (``consolidation_chain``, since 5f7d4987).  This
# paragraph once described a stateless "stage" alternative as the shipped
# semantics; that alternative was priced twice and lost twice -- 81.8u at
# shipping time, ~95u on the 2026-08-30 sweep, both with the loss
# concentrated in dark-open chains whose profitable late top-ups the
# stage reading kept cutting -- and the stale text then sent a reviewer
# hunting a scope problem that was already solved.  What was ACTUALLY
# broken for the first two days was the opener row's writer, gated on a
# plan key that does not exist (see consolidation_chain.py).
#
# 0.10 -> 0.05 (2026-08-30 re-measure on the charter-66/67 stack, 319
# chains): depth is monotone down to a clamp plateau below 0.05; under
# the opener rule +14.58 with drop-1/2/3 and both temporal halves
# positive, and under the stage reading +24.45 with sign 164:148 -- the
# depth verdict holds under either scope.
#
# **Retired 2026-09-16 (charter 139, ruling "撤")**: the default is 1, the
# identity.  Re-read on 08-31..09-15 production decisions, the premise is
# gone and the rule had stopped biting:
#   - per dollar, no fill model, target baskets at plan cost: P0 with
#     ``stable`` lit -3.1% against unlit -10.3% (diff +7.2, CI -0.2..+14.4);
#     382 openers -0.1% against -8.1%; the sign flips between halves; the
#     standing paper book reads -15.1% lit against -37.0% unlit.  The cut
#     landed on the half that loses less.
#   - the floor had eaten the factor: a P0 allowance is ~4 USDC and three
#     legs at the venue minimum cost 3.6-7, so of 428 lit P0 trades since
#     09-08 only 48 lost any capital (mean 9.4%, 19.12 USDC in all), and no
#     inherited later-stage trade was cut.
# No on/off ledger arm was run: every group loses, so any stake cut shows a
# saving; only the per-dollar split can say whether the lamp discriminates.
# The lights are still computed and recorded; ``PM_CONSOLIDATION_TILT=0.05``
# reproduces the retired rule as a research arm.
CONSOLIDATION_TILT_FACTOR = min(1.0, float(
    os.environ.get("PM_CONSOLIDATION_TILT", "1") or 1.0
))
# **两盏灯改为只看 stable（2026-09-08 裁决："该砍的是 stable 亮的日子，
# 不该砍 sharp 亮的日子"）**。固化减仓砍的是注码，所以它砍在哪一天决定了
# 它是止损还是自伤。09-08 在新生产账本上按灯分组读 264 笔已结算：
# ``sharp`` 亮的日子回报 **+8.7%**（全库唯一为正的一组），``stable`` 亮的
# 日子 **−8.6%**。旧规则是"亮一盏即减"，于是把唯一赚钱的那一组也砍了。
#
# **证据等级须知**：这是**观察性分组**，不是 A/B。灯亮本身就意味着市场那天
# 已经定价完，那些天本来就更容易赚——多少来自减仓、多少来自"那天本来就
# 好"，这份数据分不出来。算术反事实也不能定案：不减仓的臂要投入 3815–7717
# USDC，而全簿只有 399，执行不了，故其量级不可信、只有符号可用。要真正定
# 案需要一个**注码可行**的对照组，尚未跑。裁决在证据不足的情况下作出，
# 记录于此，不得被后来的读者当作已验证结论。``stable,sharp`` 留作研究臂。
CONSOLIDATION_TILT_SIGNALS = tuple(
    name.strip()
    for name in (
        os.environ.get("PM_CONSOLIDATION_TILT_SIGNALS", "stable")
        or "stable,sharp"
    ).split(",")
    if name.strip() in ("stable", "sharp")
)
CONSOLIDATION_TILT_MIN_SIGNALS = int(
    os.environ.get("PM_CONSOLIDATION_TILT_MIN_SIGNALS", "1") or 1
)
# The floor covers the three-rung target opening width (clause 50 / charter
# 59): a tilted decision can always still afford the venue's minimum order on
# each of three legs, so the tilt reduces and never quietly refuses.  What
# "minimum" means is the kernel's (``consolidation_tilt_floor_usdc``): one
# exchange lot and at least a dollar per leg (charters 70/83), not the one
# minimum clip it was until 2026-09-06 -- 0.75 USDC against legs that cost
# 1.05 held every tilted chain at P0-M and P1 by arithmetic.
CONSOLIDATION_TILT_FLOOR_LEGS = 3


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
# Charter 94 (user ruling, 2026-09-06: "把吃单改为：best 买一价+1、best 买一价、
# best 买一价-1 这种阶梯性挂单，不主动按照卖一价吃单"): the resting ladder is
# anchored on the best bid -- one tick inside it, on it, one tick under it --
# and never crosses.  ``band`` is the pre-ruling shape (the charter-63-era
# five-to-eight rung band between bid+1t and ask-1t), kept as the arm.
MAKER_LADDER_SHAPES = ("bid_anchored", "band")
MAKER_LADDER_SHAPE = (
    os.environ.get("PM_MAKER_LADDER_SHAPE", "").strip() or "bid_anchored"
)
if MAKER_LADDER_SHAPE not in MAKER_LADDER_SHAPES:
    raise ValueError(f"unknown maker ladder shape: {MAKER_LADDER_SHAPE}")
# Charter 110 narrows charter 94(一) from three rungs to one.  Measured
# 2026-09-07 on the shadow book, the marginal population each rung buys:
# the rung inside the bid, reached because the ask merely came to it,
# -0.0067 a share; the rung the ask had to fall to the bid to reach,
# -0.0234; and the deepest rung is that population one tick further in.
# Charter 94(二) then split the gap equally across whatever rungs the lot
# allowed, so two thirds of every leg was routed into the swept population
# and the leg's own fill was fractional on top of the basket's being
# partial.  ``PM_MAKER_LADDER_ANCHOR_OFFSETS`` restores the old shape as
# the research arm.
_ANCHOR_OFFSETS_RAW = os.environ.get("PM_MAKER_LADDER_ANCHOR_OFFSETS", "").strip()
MAKER_LADDER_ANCHOR_OFFSETS = (
    tuple(
        int(part.strip())
        for part in _ANCHOR_OFFSETS_RAW.split(",")
        if part.strip()
    )
    if _ANCHOR_OFFSETS_RAW
    else (1,)
)
# The horizon a resting rung's fill is priced at.  The fill artifacts are
# fitted at 300/900/1800/3600 s from the maker shadow book; a rung posted at
# listing rests until the day resolves, and an hour is the longest horizon
# measured.  A station whose artifact has no row at this horizon falls back
# to the policy's own grid rather than failing the compile.
MAKER_RESTING_TIMEOUT_SECONDS = float(
    os.environ.get("PM_MAKER_RESTING_TIMEOUT_SECONDS", "") or 3600.0
)
# How long a rung is allowed to stand at the venue before the venue itself
# removes it (user ruling, 2026-09-11: entries go out GTD).
#
# Set to the horizon the rung's own fill probability was priced at, and for
# that reason rather than for a round number: the artifact prices this rung
# at ``MAKER_RESTING_TIMEOUT_SECONDS``, so an order living past it is one
# whose chance of filling nothing in the plan ever estimated.  A rung still
# wanted at the horizon is re-minted by the next compile, at the anchor that
# stands then rather than the one that stood an hour ago -- and the measured
# shape says that is no loss: fills arriving after fifteen minutes were worse
# than fills arriving before them (0-5 min -0.0188 a share, 15-30 -0.0247,
# 30-60 -0.0251), so the value of standing still was already negative.
#
# What it buys is the case nothing else covers: a rung this lane loses track
# of.  LFPB 21C rested 18.8 hours on 2026-09-07 at 0.061 with no bid left and
# the ask down at 0.020, because the only thing that could end it was a
# cancel from a lane that had stopped seeing it.
# Charter 127 (user ruling, 2026-09-11): the designated completion crosses as
# a limit fill-or-kill instead of resting.  ``off`` restores the resting rung,
# which is what every completion has been posted as.
CHARTER_127_COMPLETION_TAKE = (
    os.environ.get("PM_CHARTER_127_COMPLETION_TAKE", "").strip().lower() or "on"
)
if CHARTER_127_COMPLETION_TAKE not in {"on", "off"}:
    raise ValueError(
        f"unknown charter 127 completion take mode: {CHARTER_127_COMPLETION_TAKE}"
    )
MAKER_GTD_HORIZON_SECONDS = float(
    os.environ.get("PM_MAKER_GTD_HORIZON_SECONDS", "")
    or MAKER_RESTING_TIMEOUT_SECONDS
)
if MAKER_GTD_HORIZON_SECONDS < 60.0:
    # The matching engine's own grace: an order expiring inside a minute is
    # one the venue may never act on.
    raise ValueError(
        f"maker GTD horizon below the venue grace: {MAKER_GTD_HORIZON_SECONDS}"
    )


# Charter 110's completion rules, as one switch: the (四) partition and the
# (六) cutoff.  Two gates, one ruling, so they are armed together -- half of
# 110 running is a rule nobody wrote.  ``off`` is the
# pre-110 arm -- every short rung the leg floor allows is completed, which is
# what charter 84 shipped and what its tests were written against.  The
# measured difference is large: 1,971 of 2,575 short legs (77%) sit on the
# side this refuses, so the arm exists to price that refusal on the book
# rather than to leave it as an argument.
# Default ``off`` until the book prices it (user ruling 2026-09-09: "先看这
# 77% 落到钱上是多少").  Charter 110(四) is the ruling and wants this on; what
# is not yet known is what refusing three quarters of all completions does to
# the money, and the ledger can answer that offline without the live lane
# spending anything.  Flip the default, not the callers, when it answers.
CHARTER_110_COMPLETION = (
    os.environ.get("PM_CHARTER_110_COMPLETION", "").strip().lower() or "off"
)
# Charter 136 (user rulings 2026-09-15): a broken seat is completed when the
# exposure it removes outweighs the edge it gives up, inside a budget off the
# seat's own sizing and only on legs the seat already wanted
# (``completion_risk_budget_v1``).  ``off`` is the charter-130 book, where a
# completion clears the one hurdle or holds.  Default ``off`` until the
# ledger has priced it ("落地": implement, price on the book, then switch on).
CHARTER_136_COMPLETION = (
    os.environ.get("PM_CHARTER_136_COMPLETION", "").strip().lower() or "off"
)
if CHARTER_136_COMPLETION not in {"on", "off"}:
    raise RuntimeError(
        f"PM_CHARTER_136_COMPLETION must be on or off: {CHARTER_136_COMPLETION}"
    )


def bid_anchored_ladder(*, bid: float, ask: float, tick: float) -> list[float]:
    """Charter 94's rungs, cheapest first; charter 110 leaves one, bid+1t.

    Each rung is floored to the tick, must be above zero and must sit
    strictly under the ask (a rung on the ask is a take).  At a one-tick bid
    the rung under it is zero and is dropped; with no bid there is no anchor
    and no ladder.

    A rung over the highest post-only price is CLAMPED to it, not dropped
    (charter 110).  Under the three-rung shape a one-tick spread still left
    two legal rungs, so dropping the inside one cost nothing; with one rung
    it drops the leg -- the compile then reports
    ``maker_post_only_market_anchor_unavailable`` and the basket cannot be
    opened at all on a book whose spread is one tick.  Clamped, the single
    rung reads "as close to the ask as post-only allows, never more than one
    tick over the bid", which is what the measurement is about: the loss sits
    in rungs a WIDE book's ask has to collapse to reach, not in a rung that
    is one tick under the ask because the whole spread is one tick.
    """

    if bid <= 0.0 or ask <= 0.0 or tick <= 0.0:
        return []
    highest = _floor_to_tick(ask - tick, tick)
    if highest <= EPS:
        return []
    rungs = set()
    for offset in MAKER_LADDER_ANCHOR_OFFSETS:
        price = _floor_to_tick(bid + offset * tick, tick)
        price = min(price, highest)
        if price > EPS:
            rungs.add(price)
    return sorted(rungs)


def bid_anchored_rungs_for_gap(
    *,
    rungs: Sequence[float],
    gap_shares: float,
    min_lot_shares: float,
    minimum_notional_usdc: float,
) -> list[float]:
    """As many rungs, dearest first, as the gap holds whole venue lots for.

    Every rung is its own order and the venue takes no order under its lot
    (charter 70) or under a dollar (charter 83), so a five-share gap posts
    one rung -- the one inside the bid, the likeliest to fill -- a ten-share
    gap two, fifteen or more all three.  The lot is read at the cheapest
    rung, where the dollar floor asks the most shares.
    """

    ordered = sorted((float(rung) for rung in rungs), reverse=True)
    if not ordered:
        return []
    lot = max(
        venue_minimum_shares_v1(
            min_lot_shares=min_lot_shares,
            ask=price,
            minimum_notional_usdc=minimum_notional_usdc,
        )
        for price in ordered
    )
    if lot <= EPS:
        return ordered
    count = int(math.floor(float(gap_shares) / lot + 1e-9))
    return ordered[:max(1, min(len(ordered), count))]


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
    steps: int = MAKER_LADDER_STEPS,
    improve_ticks: int = MAKER_LADDER_IMPROVE_TICKS,
) -> list[float]:
    """The price band one leg rests across, cheapest rung first.

    Production offered exactly one price -- the cheapest legal post -- so it
    only ever bought the 17% rung and never the 88% one.  The ledger has
    always offered the whole band and let the book decide which rung it
    reaches, and that is the execution every ROI figure here was measured
    under.  Same construction, same constants, so the two cannot drift.

    "Cannot drift" was an aspiration until 2026-08-26: the ledger had its
    own copy of this arithmetic that rounded to six decimals instead of
    flooring to the tick, so it posted prices no exchange would accept --
    0.2864 on a tick-0.001 book -- and 37 of 64 sampled (bid, ask, tick,
    cap) combinations produced different ladders on the two sides.  The
    ledger now calls this function, which is what the docstring above
    always claimed.  ``improve_ticks`` is parameterised only so the
    ledger's research arm can vary it; the default is production's own
    constant and production never passes the argument.
    """

    if MAKER_LADDER_SHAPE == "bid_anchored":
        # Charter 94: the shape is the anchor's, whatever band the caller
        # asked for; ``steps`` and ``improve_ticks`` belong to the arm.
        return bid_anchored_ladder(bid=bid, ask=ask, tick=tick)
    if bid <= 0.0 or ask <= 0.0 or tick <= 0.0:
        return []
    high = _floor_to_tick(ask - tick, tick)
    if high <= EPS:
        return []
    low = min(_floor_to_tick(bid + int(improve_ticks) * tick, tick), high)
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


# Which price the TARGET INVENTORY is chosen against -- not which price the
# orders are placed at, which has been the maker ladder since 31a68f52.
#
# ``ask_depth`` hands the solver the cost of crossing the spread; ``lane``
# hands it the band its resting orders would sit in.  The default is
# ``ask_depth`` again (user ruling, 2026-08-27): the ask is treated as
# information, not merely as a cost the engine never pays.
#
# The 2026-08-26 lane switch had a named case -- KHOU, four legs offered at
# 1.100 against a 1.00 payout that would rest at 0.876 -- and the reversal
# has three: the same-entry ledger rebuilds measured crossing-priced gates
# worth +50.7, +55.5 and +62.8 USDC against lane-priced ones, and the
# 55-chain decomposition shows why.  Half the maker losses are bad bets the
# lane price flattered into passing: a basket that is only +EV at the price
# we hope to fill at is a basket the market's open ask has already priced
# out, and the fills that do come at the hoped-for price select for the
# times the market knew better.  Of 94 crossing-refused seats measured to
# 2026-08-26, two carried a real edge at the resting price -- KHOU's class,
# the priced cost of the gate -- and 92 were refused either way.
#
# P0-M keeps lane pricing (the ruling's one exemption): it posts once and
# takes whatever book it finds, so a crossing gate there mostly closes a
# stage that is already thin.  Execution is untouched either way -- orders
# still rest in the band; what changed is only the price the decision is
# judged on.
TARGET_PRICING_BASIS = (
    os.environ.get("PM_TARGET_PRICING_BASIS", "ask_depth").strip()
    or "ask_depth"
)
if TARGET_PRICING_BASIS not in {"lane", "ask_depth"}:
    raise ValueError(f"unknown target pricing basis: {TARGET_PRICING_BASIS}")
# Charter 127 (五): a completion that crosses is priced at its own crossing,
# whatever stage prices the other candidates on the maker band.
COMPLETION_CROSSING_PRICING_RULE_VERSION = "completion_priced_at_its_crossing_v1"
TARGET_PRICING_LANE_STAGES = tuple(
    stage.strip()
    for stage in os.environ.get(
        "PM_TARGET_PRICING_LANE_STAGES", "P0-M"
    ).split(",")
    if stage.strip()
)


class _Level:
    """A price with a size behind it, shaped like an official WS level."""

    __slots__ = ("price", "shares")

    def __init__(self, price: float, shares: float) -> None:
        self.price = float(price)
        self.shares = float(shares)


def _lane_purchase_levels(
    row: Any,
    *,
    stage: str | None = None,
) -> tuple[Sequence[Any], float | None, str]:
    """The ladder the target inventory is priced against, and at what price.

    Under ``lane`` this is one synthetic level at the resting band's mean --
    what the leg pays if the whole ladder fills, which is the number the
    ledger's sizing has used since it started posting bands.  Its size is
    the visible offer depth: not because a maker consumes it, but because it
    is the only measure of how much trades in this bucket at all, and
    letting the clip run past it reproduces the fiction where thirty shares
    at one tick fill a basket of thousands.

    Falls back to the real offer ladder, and says so, whenever the band does
    not exist -- no bid to join.  Crossing is then genuinely the only way to
    acquire the leg, so the crossing price is the honest one.
    """

    basis = (
        "lane"
        if stage is not None and str(stage) in TARGET_PRICING_LANE_STAGES
        else TARGET_PRICING_BASIS
    )
    if basis == "ask_depth" or not row.ask_available:
        return row.ask_levels, None, "official_ws_ask_depth"
    rungs = _maker_ladder(
        bid=float(row.bid_price),
        ask=float(row.ask_price),
        tick=float(row.price_tick_size),
    )
    if not rungs:
        return row.ask_levels, None, "official_ws_ask_depth"
    price = sum(rungs) / len(rungs)
    depth = sum(float(level.shares) for level in row.ask_levels)
    if price <= EPS or depth <= EPS:
        return row.ask_levels, None, "official_ws_ask_depth"
    return (_Level(price, depth),), price, "maker_resting_band"


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
#
# Overridable so the floor itself can be measured rather than assumed.  Three
# hand-written attempts to price it from the snapshot tables on 2026-08-24 each
# produced a different answer and each was wrong -- entry quotes taken before
# the book had formed, station-days whose local day had not finished counted as
# settled, and out-of-window days scored as "a cheap bucket won" when what they
# actually record is a day both the market and the model missed.  The ledger
# already has the entry clock, the freshness contract and the settlement
# vocabulary; the honest way to ask what this number is worth is to move it and
# rebuild, not to re-derive the book beside it.
# Raised 0.02 -> 0.03 by user ruling, 2026-08-24 (first to 0.05, revised to
# 0.03 minutes later).  The market-adjudicated landing table -- winners taken
# from the book's own convergence, validated 60 of 62 against the settlement
# pipeline -- puts the sub-three-cent bands at 2 wins in 1054 and 1 in 141
# (0.2% and 0.7%), dead ground the model cannot out-know the market on; the
# 3-5 cent band still settles 1.3% and stays admitted.  Five cents was tried
# first and cut the book from 102 chains to 33 by pricing sharp markets'
# shoulders out of the contiguity requirement; three keeps most of those
# seats while dropping the ground where cheapness is the market being right.
# Which order shape production forms.
#
# Charter 65 (user ruling 2026-08-27, "整篮 EV"): basket-level EV is the
# execution layer's only law, and it made taker the law.  The week that
# settled it: on same-entry rebuilds the maker book's entire deficit against
# taker sat in 33 chains whose winning legs rested unfilled (+77.1 USDC)
# while the 225 shared chains were level.
#
# That ruling shipped to the ledger on the day and never reached here, so for
# four days the ledger scored taker while production could only form maker
# orders -- 1,406 of 1,406 intents MAKER, not one FOK in the lane's whole
# history, because the FOK family was gated behind a ``confirmed_cancel_for_fok``
# parameter no caller ever passed and a 120-minute urgency window that was the
# pre-charter rule's own implementation.  Both gates were the old rule and
# both are gone.
#
# It is a named constant rather than a branch so the two sides can be checked
# against each other: ``tests/test_ledger_production_rule_binding`` could
# classify the ledger's ``EXECUTION_MODE`` but had nothing here to equate it
# to, which is exactly why the drift was silent.  The maker machinery stays,
# as arms, on the same terms the ledger keeps its resting modes.
# The blocker turned out to be two filter lines, not the reservation solver:
# the runtime handed the bundle selector maker actions only, and the selector
# skipped anything that was not MAKER even so.  Nothing beneath them was
# maker-specific -- the bundle helpers pass ``order_mode`` straight through to
# the fill contract, which already knew FOK is all-or-nothing.  Both are gone,
# so taker composes the basket and the default is the ruling.
#
# The maker machinery stays as an arm on the same terms the ledger keeps its
# resting modes, and the two sides now declare this constant under the same
# name so the binding test can equate them.
EXECUTION_MODES = ("taker", "maker")
# Charter 94 (user ruling, 2026-09-06): the order family is the resting
# ladder anchored on the best bid -- one tick inside it, on it, one tick
# under it -- and nothing crosses to the ask.  Taker (charter 65) stays as
# the research arm under the same switch.
EXECUTION_MODE = (
    os.environ.get("PM_EXECUTION_MODE", "maker").strip() or "maker"
)
if EXECUTION_MODE not in EXECUTION_MODES:
    raise ValueError(f"unknown execution mode: {EXECUTION_MODE}")
# The family name the paper ledger is held equal to (its own vocabulary has
# several resting arms; production has one).
EXECUTION_FAMILY = EXECUTION_MODE


# Charter 131 (Q8) set the flat leg floor at three cents on the rung's mid;
# user rulings 2026-09-17 ("0.03的地板价改为0.04", then "改为0.05") raised it
# to five.  The read point (mid, charter 103) is unchanged; the value is in
# both books' fingerprints, so the paper ledger rebuilds rather than mixing.
MINIMUM_LEG_ALL_IN_ASK = float(
    os.environ.get("PM_MINIMUM_LEG_ALL_IN_ASK", "0.05") or 0.05
)


def _leg_below_floor(
    row: Any, probability: float | None, *, fee_rate: float = 0.0
) -> bool:
    """Charter 91 + 103: is this rung under its belief-scaled floor?

    Measured on the rung's own book -- the mid where it is two-sided, the
    ask where nobody is bidding -- and never on whatever price the call
    site happens to be pricing the leg at.  That distinction is the whole
    point: the ledger's maker mode prices a leg at the resting band and
    production prices it at the offer, and if the floor followed the call
    site the two books would be screening different things.  One read
    point, one answer per rung.
    """

    if not getattr(row, "ask_available", False):
        return False
    return leg_below_floor(
        float(row.ask_price) * (1.0 + float(fee_rate)),
        probability,
        MINIMUM_LEG_ALL_IN_ASK,
        bid=float(getattr(row, "bid_price", 0.0) or 0.0),
    )
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
    "shape",
    # Direction against the book's read of the day (user ruling
    # 2026-08-28); sits with the structural gates because it rules on
    # placement, not price.  Counts stay zero while the gate runs in
    # shadow, which keeps the funnel arithmetic closed either way.
    "market_lean",
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


# Which bucket the seat rule pins the window to.  ``mean`` -- the
# mass-weighted centre of the full served distribution, snapped to the
# nearest admitted bucket -- is the ruling (user, 2026-08-25, charter 54)
# and the default.  It supersedes ``heaviest`` (the 2026-08-16 argmax
# ruling, charter 52a) on three measurements over 74 settled chains: the
# mode sits 1.65 buckets from the settled truth against the mean's 1.36
# (within-one-bucket 50% vs 62%); when the two split by half a bucket or
# more the truth lands on the mean's side 23:4; and the full-ledger A/B
# reads mean at -25.4% ROI against heaviest's -34.9% with every drop-1/2/3
# excision still positive.  The mechanism: argmax is only optimal when the
# per-bucket shape is trustworthy, and the claims measurably are not --
# the first moment is this model's most reliable output, and the mode
# systematically sits on the cold side of a hot-tailed day.  ``heaviest``
# and ``median`` remain as research arms; the ledger hashes the definition
# into its rule fingerprint.
LADDER_CENTER_DEFINITION = (
    os.environ.get("DELTA_V2_LADDER_CENTER_DEFINITION", "").strip().lower()
    or "mean"
)
# Whether the mean-mode gap biases which side a window may extend toward
# (user ruling, 2026-08-25, charter 55).  The gap is the distribution's own
# skew reading: mean above mode is a hot tail, below it a cold one, and
# measured on 74 settled chains the truth lands on the mean's side of the
# mode 23:4 whenever the two split by half a bucket.  Under the rule a
# window whose extension leans AGAINST that side is refused at enumeration
# (funnel gate ``skew``); balanced windows and the skew side always stay
# admissible, and a day with no split (|gap| under half a bucket) is
# unconstrained.  This is deliberately a shape-derived signal from p(t)
# itself -- charter 43's ban on weather-covariate branches at the strategy
# layer stands untouched.  ``off`` restores enumeration with no direction
# rule for the A/B arms.
# Charter 68 (user ruling, 2026-09-01): the objective reads effective edge.
# ``on`` shrinks each opening-stage window's claim by the realized fraction
# historically seen at its market agreement, so the ranking and the solver
# both price the inverted U instead of the nominal edge's left cliff.  Off
# restores the nominal reading for the A/B arms.  The ledger carries the
# same switch and the two must move together.
EFFECTIVE_EDGE_MODE = (
    os.environ.get("DELTA_V2_EFFECTIVE_EDGE", "").strip().lower() or "on"
)
# Charter 68 scope (research arm, 2026-09-03).  ``window`` -- the shipped
# rule -- deflates every rung of a window by the factor read off the whole
# window's agreement.  ``bucket`` deflates each rung by its own agreement,
# off the per-leg table in the kernel, so the mass moves between the rungs
# of a window rather than only between the window and the rest.  The
# ledger carries the same switch and the two must move together.
EFFECTIVE_EDGE_SCOPE = (
    os.environ.get("DELTA_V2_EFFECTIVE_EDGE_SCOPE", "").strip().lower()
    or "window"
)
if EFFECTIVE_EDGE_SCOPE not in {"window", "bucket"}:
    raise ValueError(
        f"unknown effective edge scope: {EFFECTIVE_EDGE_SCOPE}"
    )
# Who may refuse one leg of a taker basket (user ruling, 2026-09-03: "a
# negative-EV rung may not block a positive-EV basket").  ``joint`` hands a
# rung the reservation inversion found no positive marginal price for to
# the joint bundle selector at the ask, and only the whole basket's delta
# authorizes or refuses it -- charter 35/58/65, and what the paper ledger
# has always scored.  ``reservation`` is the FOK path as shipped on
# 2026-08-31: a rung with no positive marginal price produced no
# candidate, the bundle is all-or-none, and the basket held; measured
# after the 2026-09-02 arming, 133 such legs held 91 baskets, cheap
# positive-EV rungs included.  The maker branch had always handed its
# anchor through; only the taker branch vetoed.
TAKER_LEG_VETO = (
    os.environ.get("DELTA_V2_TAKER_LEG_VETO", "").strip().lower() or "joint"
)
if TAKER_LEG_VETO not in {"joint", "reservation"}:
    raise ValueError(f"unknown taker leg veto: {TAKER_LEG_VETO}")
# Charter 69 (user ruling, 2026-09-02: "切 guarded"): the declining-
# probability clamp of charter 51 is narrowed to what charter 49 and
# contiguity allow.  ``guarded`` clamps only falling rungs at the ends of
# the window, walking in from each side and never past the centre rung, so
# a basket the clamp has touched is still a ladder: no dropped centre, no
# hole.  ``leg`` is charter 51 as shipped on 2026-08-15 -- every falling
# rung clamped, centre included -- kept for the A/B arms.  Measured over the
# 2026-09-02 reseat replay (405 station-days): leg +15.4%, guarded +14.9%,
# within one station of each other on P&L, while leg left 5.5% of P0
# openings a single rung and missed the centre on 11.7% against guarded's
# 2.9% and 5.3%.  The ledger carries the same switch and the two must move
# together.
# Final ruling the same day (熊先生, 2026-09-02): ``after_open``.  The
# opening seat has no position to hold, so it is never clamped and the
# ladder is bought whole; from the next seat on, any rung whose probability
# fell since the previous model run is not bought this round, whether the
# book holds it or not, centre included.  Measured on one snapshot, 413
# station-days: off +8.2%, held_only +8.8%, after_open +16.4%, leg +17.7%
# (after_open and leg one station-day apart; after_open's openings are
# row-for-row off's).  ``guarded`` was that day's interim reading and stays
# as an arm.
# **Retired 2026-09-05 (user ruling)**: default ``off``.  The clamp was
# ruled in on 2026-09-02 against a paper book that priced every checkpoint
# on the book *before* it -- the ledger's own information-timing advantage,
# worth about sixteen points and removed on 2026-09-04.  Re-measured on the
# honest pairing the clamp costs: turning it off is +3.4 points alone and
# +7.3 inside the best combination (X8/X9, 2026-09-05).  ``after_open`` and
# the other arms stay switchable so the old book can be re-derived.
DECLINE_CLAMP_MODE = (
    os.environ.get("DELTA_V2_DECLINE_CLAMP", "").strip().lower()
    or "off"
)
# ``held_only`` (user semantics, 2026-09-02: "首次建仓如何评判概率下降？
# 没有对比啊"): the clamp's subject is a rung the book already holds or has
# bid for -- "hold at what you have".  A rung nothing has been committed to
# has nothing to hold, so an opening is never clamped; only a top-up is,
# and there every falling rung is held, centre included.  Measured on the
# 2026-09-02 reseat replay: of leg's +82.8 U over off, +74.5 U came from
# top-ups and +8.3 U (71 better / 46 worse, one station-day either way)
# from openings -- the clamp earns where it has a subject.
# ``after_open``: charter 51 as written (every falling rung held, centre
# included, held or not) -- but only once the book has opened.  The
# opening seat, where nothing is committed yet, is never clamped; every
# later seat is, whether the falling rung is one the book holds or one
# the re-centred window has just brought in.  Replay arm, 2026-09-02.
# ``off``: no clamp at all -- the shipped rule since 2026-09-05, and the
# state the paper book was in for every measurement before 2026-09-01.
if DECLINE_CLAMP_MODE not in {
    "off", "leg", "guarded", "held_only", "after_open",
}:
    raise ValueError(f"unknown decline clamp mode: {DECLINE_CLAMP_MODE}")
def support_width_bounds_v1() -> tuple[int, int]:
    """The bounds the enumerator is handed: charter 131's two to three.

    One function, so the production judge and the paper ledger cannot be
    running two different pairs -- which is the failure the binding test
    exists to catch.  The station policy's width fields are no longer read
    here: the seat's width is the ruling's, not the station's.
    """

    return SEAT_MIN_BUCKETS, SEAT_MAX_BUCKETS


# The centre-contain gate (charter 49), as a research arm.
#
# ``on``: the window must contain the ladder centre or it is not admissible.
# Measured 2026-09-11 on the settled enumerations, this is what narrows the
# domain: 92.5% of station-days come out of the gate with one admissible
# window, and refusals name the centre gate more often than everything else
# together.  The seat-head rule people reach for first binds on 14 of 470.
#
# ``deflate_only``: the window stays admissible and keeps its off-centre
# deflation, so the claim on a window that misses the centre is still cut by
# ``window_claim_factor`` and charter 81's lift still applies where it
# applies.  The gate stops REFUSING and goes on PRICING.  There is
# deliberately no ``off``: dropping the deflation as well would test two
# changes at once, and the deflation is separately measured.
#
# Off by default, so production is byte-identical until an arm says
# otherwise.  The paper ledger hashes a non-default value.
CENTRE_CONTAIN_GATE = (
    os.environ.get("PM_CENTRE_CONTAIN_GATE", "").strip().lower() or "on"
)
if CENTRE_CONTAIN_GATE not in {"on", "deflate_only"}:
    raise RuntimeError(
        f"unknown centre contain gate mode: {CENTRE_CONTAIN_GATE}"
    )
# Charter 90 (user rulings, 2026-09-06): the TAF TX is a point forecast, not
# a bucket distribution, but it carries information the models do not have,
# so it enters exactly once -- on the P0-M stage, as the P0 basket's second
# confirmation -- and never touches the served vector.  ``tx_envelope`` is
# the shipped rule: if the basket P0 holds already contains the TX bucket the
# TAF changes nothing (the standing rules seat as before); if it does not,
# the TX bucket must enter the basket -- the P0-M seat becomes the contiguous
# span from the centre bucket to the TX bucket, keeping as many held rungs as
# the width cap allows and padded to the seating floor, and P0-M may buy that
# leg (the charter-71 identity gate yields to it) or open the day on it when
# P0 left the chain empty.  A TX the cap cannot reach from the centre leaves
# no admissible window: the model and the forecaster disagree by more than a
# ladder can straddle, and the stage holds.  The 2026-09-04 research arms
# stay selectable; ``off`` is the pre-ruling arm.
P0M_TAF_MODE = (
    os.environ.get("PM_P0M_TAF_MODE", "").strip().lower() or "tx_envelope"
)
if P0M_TAF_MODE not in {
    "off", "tx_envelope", "tx_anchor", "confirm_hold", "confirm_override",
}:
    raise RuntimeError(f"unknown P0-M TAF mode: {P0M_TAF_MODE}")
P0M_TAF_ENVELOPE_RULE_VERSION = "p0m_taf_second_confirmation_v2"
# Charter 81 (user ruling, 2026-09-05): the centre-holding claim lift.  One
# implementation in the kernel, read here, by the paper ledger and by the
# charter-68 lambda store (whose agreement must be computed on the same
# lifted claim the solver reads).  Charter 81 was retired 2026-09-12: the
# shipped default is empty (identity) and the old table is the research arm
# behind ``PM_CENTER_WINDOW_CLAIM_BANDS`` (see CENTER_WINDOW_CLAIM_RETIRED_TABLE).
# Research arm (2026-09-04, user): which stages may open an empty chain.
# Charter 74 ships P0 alone; ``P0,P0-M`` is the arm that lets P0-M open (and
# so take a ticket) for the TAF-confirmation replays.  P1 never opens.
CHAIN_OPENING_STAGES = frozenset(
    part.strip().upper()
    for part in (os.environ.get("PM_CHAIN_OPENING_STAGES", "") or CHAIN_OPENING_STAGE).split(",")
    if part.strip()
)
if not CHAIN_OPENING_STAGES <= {"P0", "P0-M", "P1"}:
    raise RuntimeError(f"unknown chain opening stages: {sorted(CHAIN_OPENING_STAGES)}")
# The gap below which a day reads as symmetric, in units of the served
# sigma (user ruling, 2026-09-11; the charter-55 half bucket was an absolute
# gate that turned into a width reading as serving narrowed -- see the
# kernel constant).  Zero makes the reading binary -- every day leans
# whichever side its mean sits from its mode, however slightly -- which is
# the user's anchored-window concept (2026-08-25): no symmetric third state,
# the mean bucket always the anchor, the window always extended toward the
# day's own lean.  The old bucket-unit variable is refused rather than read
# in the wrong unit: a research arm set for half a bucket would silently
# become half a sigma.
if os.environ.get("DELTA_V2_SKEW_GAP_THRESHOLD", "").strip():
    raise RuntimeError(
        "DELTA_V2_SKEW_GAP_THRESHOLD is in bucket steps and retired;"
        " set DELTA_V2_SKEW_GAP_THRESHOLD_SIGMA (units of the served sigma)"
    )
SKEW_GAP_THRESHOLD_SIGMA = float(
    os.environ.get("DELTA_V2_SKEW_GAP_THRESHOLD_SIGMA", "") or 0.35
)
# The market-lean gate (user ruling, 2026-08-28): the book is information,
# and a window may not lean AGAINST it.  Measured before wiring, on 481
# morning station-days (local pre-noon snapshot vs the probability served
# at that instant): when the book's implied mean sat more than half a
# bucket WARM of the model's, settlement landed warm of the model's mean
# 94% of the time against a 56% base rate; the cold side mirrors at 82%
# vs 44%.  Both by-day bootstrap CIs clear zero by 30+ points.  That
# signal lives exactly in the consensus bridge's dead zone -- the bridge
# blends only past a 1.5C centre gap, and the mornings that carried the
# 94% sat at 0.5-1.2C -- so nothing was reading it.
#
# Same funnel family as the skew gate one block up, and the same shape:
# a *direction* rule on window placement, not a price rule.  The skew gate
# refuses windows leaning against the model's own asymmetry; this one
# refuses windows leaning against the book's disagreement with the model.
# The incumbent is exempt -- "keep what we hold" is not a lean -- and the
# gate never *creates* a buy: a window it passes still faces the leg
# floor, the pricing, and the edge test below.
#
# ``shadow`` counts what it would refuse without refusing (the wiring
# default until a ledger replay prices the on-arm); ``on`` refuses.
# **Promoted 2026-09-05 (user ruling)**: default ``on``.  The 2026-08-28
# ruling held it in shadow until a ledger replay priced the on-arm; on the
# v11 honest pairing it is +0.9 points alone, +12.4 from 08-23, and +4.3
# inside the best combination with the clamp retirement and the ticket-only
# opener (X8/X10, 2026-09-05).  ``shadow`` counts without refusing.
# Charter 131 (2) (user ruling, 2026-09-12, Q2): the market's mean bucket
# is data being collected, not yet a rule -- the gate counts and stands
# aside.  ``on`` stays a research arm.
MARKET_LEAN_GATE_MODE = (
    os.environ.get("PM_MARKET_LEAN_GATE", "").strip().lower() or "shadow"
)
# Same half-bucket convention as the skew threshold: below this the book
# and the model agree and the gate stays out of the way.
MARKET_LEAN_MIN_BUCKETS = float(
    os.environ.get("PM_MARKET_LEAN_MIN_BUCKETS", "") or 0.5
)
# A book too empty to state a mean states nothing: quoted buckets must
# carry at least this much summed mid-price mass before a lean is read.
MARKET_LEAN_MIN_QUOTED_MASS = 0.2
# How many refused windows a single enumeration writes down.  The count is
# exact either way; this bounds the plan, not the arithmetic.
MARKET_LEAN_REFUSAL_RECORD_MAX = int(
    os.environ.get("PM_MARKET_LEAN_REFUSAL_RECORD_MAX", "") or 32
)


def market_lean_ordinal(
    quotes: Mapping[str, Any],
    probabilities: Mapping[str, float],
    ordinals: Mapping[str, int],
) -> float | None:
    """Book-implied mean minus model mean, in bucket ordinals.

    Positive: the market sits warm of the model.  Mid prices where both
    sides exist, one-sided quotes as themselves; ``None`` when too little
    of the book is quoted to state a mean, which callers must treat as
    "no signal", never as "aligned".
    """

    priced: list[tuple[int, float]] = []
    for bucket, ordinal in ordinals.items():
        quote = quotes.get(bucket)
        if quote is None:
            continue
        ask = getattr(quote, "ask_price", None) if getattr(
            quote, "ask_available", False
        ) else None
        bid = getattr(quote, "bid_price", None) if getattr(
            quote, "bid_available", False
        ) else None
        if ask is not None and bid is not None:
            price = (float(ask) + float(bid)) / 2.0
        elif ask is not None:
            price = float(ask)
        elif bid is not None:
            price = float(bid)
        else:
            continue
        priced.append((int(ordinal), min(1.0, max(0.0, price))))
    quoted_mass = sum(price for _, price in priced)
    if len(priced) < 3 or quoted_mass < MARKET_LEAN_MIN_QUOTED_MASS:
        return None
    market_mean = sum(o * p for o, p in priced) / quoted_mass
    model_pairs = [
        (int(ordinals[bucket]), float(value))
        for bucket, value in probabilities.items()
        if bucket in ordinals and float(value) > 0.0
    ]
    model_mass = sum(value for _, value in model_pairs)
    if model_mass <= 0.0:
        return None
    model_mean = sum(o * v for o, v in model_pairs) / model_mass
    return market_mean - model_mean


def market_lean_opposes(
    lean: float | None,
    *,
    window_mid_ordinal: float,
    model_mean_ordinal: float,
) -> bool:
    """Whether a window leans against the book's read of the day.

    A window whose geometric midpoint sits half a bucket or more on the
    OPPOSITE side of the model mean from the market's lean is opposed.
    No signal, or a small lean, or a window near the mean: not opposed --
    the gate abstains rather than guessing.
    """

    if lean is None or abs(lean) < MARKET_LEAN_MIN_BUCKETS:
        return False
    offset = window_mid_ordinal - model_mean_ordinal
    if abs(offset) < MARKET_LEAN_MIN_BUCKETS:
        return False
    return (lean > 0) != (offset > 0)


def _skew_extension_sign(
    probabilities: Mapping[str, float],
    ordinals: Mapping[str, int],
) -> int:
    """The distribution's skew direction: +1 hot-tailed, -1 cold, 0 none.

    Charter 75: the kernel's ``mean_mode_skew_sign_v1``, read with this
    lane's signal definition and gap threshold, which are the same numbers
    the seed's direction is read with.
    """

    return mean_mode_skew_sign_v1(
        probabilities,
        ordinals,
        threshold_sigma=SKEW_GAP_THRESHOLD_SIGMA,
    )


def _alternate_ladder_center(
    definition: str,
    *,
    admitted: Sequence[str],
    probabilities: Mapping[str, float],
    ordinals: Mapping[str, int],
) -> str | None:
    """The centre under a non-default definition, snapped to an admitted
    bucket.

    ``mean``: the probability-weighted mean ordinal of the full served
    distribution, nearest admitted bucket (ties to the heavier one).
    ``median``: the bucket where the cumulative mass first reaches half,
    walked in ordinal order, then snapped the same way.  Both read the
    whole distribution, not just the admitted slice -- the question is
    where the model's mass sits, and a bucket the book declined to price
    still carries mass -- but the seat itself must be admitted, so the
    answer is snapped onto the admitted grid.  None on empty mass, which
    lets the caller keep the heaviest-bucket centre.
    """

    weighted = [
        (float(probabilities[bucket]), int(ordinals[bucket]), bucket)
        for bucket in probabilities
        if bucket in ordinals and float(probabilities[bucket]) > 0.0
    ]
    total = sum(mass for mass, _, _ in weighted)
    if not weighted or total <= 0.0 or not admitted:
        return None
    if definition == "mean":
        # Charter 75: one definition of the mean centre, the kernel's.
        return mean_center_bucket_v1(
            probabilities, ordinals, admitted=list(admitted)
        )
    elif definition == "median":
        target = None
        running = 0.0
        for mass, ordinal, _ in sorted(weighted, key=lambda row: row[1]):
            running += mass
            if running >= total / 2.0:
                target = float(ordinal)
                break
        if target is None:
            return None
    else:
        return None
    return min(
        admitted,
        key=lambda bucket: (
            abs(int(ordinals.get(bucket, 0)) - target),
            -float(probabilities.get(bucket, 0.0)),
        ),
    )


# Charter 66 clause A (user ruling, 2026-08-29): once a station holds
# inventory, a stage may only buy if the CHAIN it would leave behind still
# prices under par.  Blended cost is the same expectation-equivalent share
# arithmetic the paper ledger settles on; at or past 1.00 no outcome
# returns the stake -- the class the ledger measured at 37 hits in 38
# chains and -31.9 USDC, every one of them assembled from stages that each
# passed their own gates.  The gate reads the *undeflated* served claim:
# it is an arithmetic honesty bound, not a preference, and deflating the
# claim would only make it stricter in the direction it already errs.
CHAIN_COST_PAR_BOUND = 1.0
# Research switches for the gates that had none (2026-09-05).  Each is on
# by default -- the shipped rule -- and ``off`` is a replay arm, so the
# gate audit can price a charter clause the way it prices every other one.
# The paper ledger reads the same names through the binding test.
CHARTER_77_DECISION_MOMENTS = (
    os.environ.get("PM_CHARTER_77_DECISION_MOMENTS", "").strip().lower()
    or "on"
)
CHARTER_78_INCUMBENT_NO_EXEMPTION = (
    os.environ.get("PM_CHARTER_78_NO_EXEMPTION", "").strip().lower() or "on"
)
CHARTER_66A_CHAIN_PAR = (
    os.environ.get("PM_CHARTER_66A_CHAIN_PAR", "").strip().lower() or "on"
)
CHARTER_71A_TOPUP_IDENTITY = (
    os.environ.get("PM_CHARTER_71A_IDENTITY", "").strip().lower() or "on"
)
CHARTER_71B_CHAIN_GROWTH = (
    os.environ.get("PM_CHARTER_71B_GROWTH", "").strip().lower() or "on"
)
CHARTER_72_KELLY_SIZING = (
    os.environ.get("PM_CHARTER_72_KELLY", "").strip().lower() or "on"
)
CHARTER_70_EXCHANGE_LOT = (
    os.environ.get("PM_CHARTER_70_LOT", "").strip().lower() or "on"
)
# Charter 83 (2026-09-05): the venue's one-dollar marketable minimum, lifted
# through the same function as the lot; ``off`` sizes as before the ruling.
CHARTER_83_MIN_NOTIONAL = (
    os.environ.get("PM_CHARTER_83_MIN_NOTIONAL", "").strip().lower() or "on"
)
# Charter 84 (2026-09-05): a seat the venue left short is completed to the
# level its held rungs sit at -- offered as a candidate beside the Kelly
# seat, judged by the same floor, the same increment edge and the same lot
# and dollar lift.  ``off`` is the pre-ruling arm, under which the first
# live morning's four half-baskets stayed half (the Kelly law sized the seat
# below what was held and offered nothing; the grid's slivers failed the
# floor).
CHARTER_84_LADDER_BALANCE = (
    os.environ.get("PM_CHARTER_84_LADDER_BALANCE", "").strip().lower() or "on"
)
if CHARTER_84_LADDER_BALANCE not in {"on", "off"}:
    raise RuntimeError(
        f"unknown charter 84 ladder balance mode: {CHARTER_84_LADDER_BALANCE}"
    )
# Charter 121 (2026-09-08): charter 93 (二)'s post-lift floor refuses the
# candidate, not the round.  ``off`` is the pre-ruling arm -- one attempt, and
# a refusal holds the day -- which is what the first three weeks of charter 84
# on P0-M and P1 actually did.  Four attempts because the candidate domain is
# baseline + clip grid + Kelly + balance and the baseline is never excluded, so
# nothing is gained by a fifth.
CHARTER_121_POST_LIFT_RERANK = (
    os.environ.get("PM_CHARTER_121_POST_LIFT_RERANK", "").strip().lower() or "on"
)
if CHARTER_121_POST_LIFT_RERANK not in {"on", "off"}:
    raise RuntimeError(
        f"unknown charter 121 post-lift rerank mode: {CHARTER_121_POST_LIFT_RERANK}"
    )
CHARTER_121_POST_LIFT_ATTEMPTS = 4
# Charter 123 (user ruling, 2026-09-09: "抬手数后重新打分"): the whole-basket
# edge gate is re-read on the lifted targets, the way charter 93 re-reads the
# floor there.
#
# Relation to the clauses it touches, stated rather than left to be inferred:
# it does not restate 78/87/92, it RE-APPLIES them at a second point -- the
# belief, the arithmetic and the threshold are theirs, unchanged, and the
# function is literally the same one.  It narrows nothing about which windows
# are admissible; it only refuses a basket the lot and dollar lifts have moved
# off the shape those clauses cleared.  Charter 93 stays exactly as it is and
# answers a different question (worst in-support settlement, not expectation);
# a basket can hold a legal floor and still expect to lose.  Charter 121's
# exclude-and-retry carries both refusals, so a seat whose winner is refused
# here falls to the next candidate rather than collapsing to a hold.
CHARTER_123_POST_LIFT_EDGE = (
    os.environ.get("PM_CHARTER_123_POST_LIFT_EDGE", "").strip().lower() or "on"
)
if CHARTER_123_POST_LIFT_EDGE not in {"on", "off"}:
    raise RuntimeError(
        f"unknown charter 123 post-lift edge mode: {CHARTER_123_POST_LIFT_EDGE}"
    )
POST_LIFT_EDGE_RULE_VERSION = "whole_basket_edge_after_lift_v1"
# Charter 124 (user ruling, 2026-09-09): the belief the gate reads is the
# worst one inside a relative-entropy ball around the served partition, not
# the served partition itself.
#
# The proposal this answers asked for ``min over p in P`` of the objective.
# Because every quantity here is monotone increasing in the seated window's
# mass (see ``robust_window_claim_v1``), that minimisation is one scalar and
# it can only ever LOWER a claim -- so as a gate it can only refuse, and the
# expected effect is buying less.  That is the same shape as the only value
# charter 68's lambda gate was ever measured to have.
#
# ``shadow`` is the default and the whole point of landing it this way: the
# reading is computed and recorded on every enumerated window, and nothing
# reads it back.  What accumulates is "which seats would this have refused,
# and what did those seats settle at", which is the evidence a promotion has
# to be argued from.  ``on`` deflates the served claim through
# ``deflate_window_partition_v1``, so the robust belief reaches the gate, the
# stake and the curve as a renormalised partition and not as a set of
# separately shrunken rungs.  ``off`` computes nothing.
#
# The budget is NOT fitted yet and is deliberately not defaulted to a number
# anyone could mistake for one: 0.02 nats is a placeholder chosen to be small
# enough to be uninteresting on a well-priced window and large enough to bite
# on a thin one, and the shadow lane exists to replace it with a measurement.
# Naming it in nats keeps it on the same scale as the model-versus-market log
# score, which is where the honest number will come from.
CHARTER_124_ROBUST_CLAIM_MODES = ("off", "shadow", "on")
CHARTER_124_ROBUST_CLAIM = (
    os.environ.get("PM_CHARTER_124_ROBUST_CLAIM", "").strip().lower() or "shadow"
)
if CHARTER_124_ROBUST_CLAIM not in CHARTER_124_ROBUST_CLAIM_MODES:
    raise RuntimeError(
        f"unknown charter 124 robust claim mode: {CHARTER_124_ROBUST_CLAIM}"
    )
ROBUST_CLAIM_DIVERGENCE_BUDGET_NATS = float(
    os.environ.get("PM_ROBUST_CLAIM_DIVERGENCE_NATS", "").strip() or 0.02
)
if not (0.0 <= ROBUST_CLAIM_DIVERGENCE_BUDGET_NATS < 10.0):
    raise RuntimeError(
        "robust claim divergence budget out of range: "
        f"{ROBUST_CLAIM_DIVERGENCE_BUDGET_NATS}"
    )
# Charter 87 (user ruling, 2026-09-06: "落正确形态是一个信念"): the
# whole-basket positive-EV gate reads the belief the solver sizes on -- the
# lifted claim (charter 81) times charter 68's lambda(a) at the window's
# agreement -- and charter 74's ticket is issued only past that gate.
# Before this the gate read the lifted claim alone and lambda entered at the
# solver: a window the book priced between lambda(a) and the lift (a in
# (0.89, 1.08) on the 2026-09-06 tables) cleared the gate, sized to nothing
# under Kelly, and was bought as a ticket regardless -- 8 of the 13 live
# tickets since go-live (52.03 staked, -7.0% at the solver's own belief;
# KDAL 2026-09-07: gate 0.9999 vs 0.99, solver 0.905 vs 0.99).  ``off`` is
# the pre-ruling gate, kept as a replay arm.  The lifted claim stays in the
# row's ``win_probability`` (the lambda store fits on it; the ledger's own
# judge deflates it itself); the deflated belief is
# ``effective_win_probability`` and the gate's ``edge`` reads it.
CHARTER_87_GATE_EFFECTIVE_EDGE = (
    os.environ.get("PM_CHARTER_87_GATE_LAMBDA", "").strip().lower() or "on"
)
_EFFECTIVE_EDGE_GATE_STAGES = frozenset(
    EFFECTIVE_EDGE_STAGES | EFFECTIVE_EDGE_P0M_STAGES | EFFECTIVE_EDGE_P1_STAGES
)
for _name, _value in (
    ("PM_CHARTER_87_GATE_LAMBDA", CHARTER_87_GATE_EFFECTIVE_EDGE),
    ("PM_CHARTER_83_MIN_NOTIONAL", CHARTER_83_MIN_NOTIONAL),
    ("PM_CHARTER_77_DECISION_MOMENTS", CHARTER_77_DECISION_MOMENTS),
    ("PM_CHARTER_78_NO_EXEMPTION", CHARTER_78_INCUMBENT_NO_EXEMPTION),
    ("PM_CHARTER_66A_CHAIN_PAR", CHARTER_66A_CHAIN_PAR),
    ("PM_CHARTER_71A_IDENTITY", CHARTER_71A_TOPUP_IDENTITY),
    ("PM_CHARTER_71B_GROWTH", CHARTER_71B_CHAIN_GROWTH),
    ("PM_CHARTER_72_KELLY", CHARTER_72_KELLY_SIZING),
    ("PM_CHARTER_70_LOT", CHARTER_70_EXCHANGE_LOT),
):
    if _value not in {"on", "off"}:
        raise RuntimeError(f"unknown {_name}: {_value}")


def _chain_blended_cost(
    *,
    shares_by_bucket: Mapping[str, float],
    cost_total: float,
    probabilities: Mapping[str, float],
) -> float | None:
    """The post-trade chain's cost per unit of expected payout, or None."""

    win = sum(
        float(probabilities.get(bucket, 0.0))
        for bucket, quantity in shares_by_bucket.items()
        if float(quantity) > EPS
    )
    if win <= EPS:
        return None
    expected = sum(
        float(probabilities.get(bucket, 0.0)) * float(quantity)
        for bucket, quantity in shares_by_bucket.items()
    )
    if expected / win <= EPS:
        return None
    return float(cost_total) / (expected / win)


def taf_tx_bucket_for(envelope: Any, stage: str) -> str | None:
    """The bucket the P0-M lineage's TAF TX falls in, or None.

    Read by the enumerator's ``PM_P0M_TAF_MODE`` arms.  One implementation
    on both sides (user ruling 2026-09-04): the paper ledger derived this
    for itself and handed it to ``_support_windows`` while production's
    own compile never did, so the arm could only ever act in the replay
    -- switched on live it would have changed nothing.  A TAF without a
    TX, or any stage but P0-M, hands the enumerator nothing.
    """

    import json  # noqa: PLC0415 -- the only reader of lineage JSON here

    if str(stage) != "P0-M":
        return None
    lineage = getattr(envelope, "lineage", None) or {}
    try:
        tx = float(lineage.get("taf_observed_tx_c"))
    except (TypeError, ValueError):
        return None
    try:
        intervals = json.loads(lineage.get("bucket_intervals_c_json") or "{}")
    except (TypeError, ValueError):
        return None
    for bucket, bounds in intervals.items():
        try:
            lower, upper = bounds
        except (TypeError, ValueError):
            continue
        if (lower is None or tx >= float(lower)) and (
            upper is None or tx < float(upper)
        ):
            return str(bucket)
    return None


def seat_shape_windows_v1(
    *,
    centre_ordinal: int | None,
    sign: int,
    stage: str,
    probability_by_ordinal: Mapping[int, float],
    live_ordinals: set[int],
) -> tuple[frozenset[frozenset[int]], dict[str, Any]]:
    """Charter 131: the only window shapes the seat may take today.

    Symmetric day: ``{c-1, c, c+1}``; a P1 day may also seat the two-rung
    ``{c, c+h}`` -- the main rung and its hedge, ``h`` toward the heavier
    live neighbour (the served mass, not the book: the market is data, not
    a rule).  Skewed day: ``{c, c+d, c+2d}`` and ``{c, c+d}``, ``d`` the
    tail side, the mode bucket outside by ruling.  Ordered widest first so a
    reader sees the preference; the seat itself is the head of the mass
    ranking, where the three-rung window always outranks its own two-rung
    subset and the two-rung one seats only when the wider fails a gate.
    """

    audit: dict[str, Any] = {
        "rule": SEAT_SHAPE_RULE_VERSION,
        "sign": int(sign),
        "state": SKEW_STATE_BY_SIGN[int(sign)],
        "centre_ordinal": centre_ordinal,
        "allowed_ordinals": [],
    }
    if centre_ordinal is None:
        return frozenset(), audit
    c = int(centre_ordinal)
    shapes: list[frozenset[int]] = []
    if str(stage) == "P1" and live_ordinals == {c}:
        # Late in P1 the observed high can leave one live outcome; demanding
        # a second rung then is a ban on the one basket that is still
        # correct (charter 50a), so the lone rung is the shape.
        shapes.append(frozenset({c}))
        audit["allowed_ordinals"] = [[c]]
        return frozenset(shapes), audit
    if int(sign) == 0:
        shapes.append(frozenset({c - 1, c, c + 1}))
        if str(stage) == "P1":
            live_up = (c + 1) in live_ordinals
            live_down = (c - 1) in live_ordinals
            if live_up or live_down:
                up = float(probability_by_ordinal.get(c + 1, 0.0) or 0.0)
                down = float(probability_by_ordinal.get(c - 1, 0.0) or 0.0)
                h = 1 if (live_up and (not live_down or up >= down)) else -1
                shapes.append(frozenset({c, c + h}))
    else:
        d = 1 if int(sign) > 0 else -1
        shapes.append(frozenset({c, c + d, c + 2 * d}))
        shapes.append(frozenset({c, c + d}))
    audit["allowed_ordinals"] = [sorted(shape) for shape in shapes]
    return frozenset(shapes), audit


def chain_protection_v1(
    snapshot: TargetBasketSnapshot, buckets: Sequence[str]
) -> dict[str, Any]:
    """Charter 131 (14): whether the seated basket is actually protected.

    Protected means every seated rung is FILLED -- a pending order is a
    liability the book may still have to pay, not cover -- and the
    in-support floor (charter 93, now five percent of the premium) holds on
    the filled shares and their sunk cost.  A basket with any rung still
    unfilled is BUILDING and may not be reported as protected; an empty
    seat is EMPTY.  Audit only: it names the state, it does not gate.
    """

    rows = {row.bucket: row for row in snapshot.buckets}
    filled = {
        str(b): float(getattr(rows.get(b), "filled_shares", 0.0) or 0.0)
        for b in buckets
    }
    cost = {
        str(b): float(getattr(rows.get(b), "filled_cost_usdc", 0.0) or 0.0)
        for b in buckets
    }
    pending = [
        str(b) for b in buckets
        if float(getattr(rows.get(b), "pending_shares", 0.0) or 0.0) > EPS
    ]
    unfilled = [b for b, shares in filled.items() if shares <= EPS]
    floor = (
        _kernel_delta_v2.support_scenario_floor_v1(
            shares_by_bucket=filled, cost_by_bucket=cost,
            support=[str(b) for b in buckets],
        )
        if buckets and not unfilled else None
    )
    protected = bool(floor is not None and floor["legal"])
    if protected:
        state = "PROTECTED"
    elif any(v > EPS for v in filled.values()) or pending:
        state = "BUILDING"
    else:
        state = "EMPTY"
    return {
        "schema": "chain_protection_v1",
        "state": state,
        "protected": protected,
        "unfilled_buckets": unfilled,
        "pending_buckets": pending,
        "floor": floor,
    }


# A seat bucket the snapshot knows was sent but not at what size (a caller
# that carries ``chain_seat_buckets`` alone): the held level binds.
COMPLETION_143_UNSIZED_SENT_SHARES = 1.0e9


def _completion_143_sent_shares(
    snapshot: TargetBasketSnapshot,
) -> dict[str, float] | None:
    """Charter 143 v2: the legs a completion may buy, and at most how many.

    The chain's send history off the snapshot -- per bucket, the size it was
    last sent at.  ``None`` only for a snapshot that carries no history at
    all, which production never builds for a chain that holds anything (a
    fill is a send).
    """

    sent = dict(getattr(snapshot, "chain_sent_shares", {}) or {})
    if sent:
        return {str(bucket): float(shares) for bucket, shares in sent.items()}
    seat = tuple(getattr(snapshot, "chain_seat_buckets", ()) or ())
    if seat:
        return {str(bucket): COMPLETION_143_UNSIZED_SENT_SHARES for bucket in seat}
    return None


def _support_windows(
    *,
    snapshot: TargetBasketSnapshot,
    desired: set[str],
    probabilities: Mapping[str, float],
    fee_rate: float,
    maximum_support_buckets: int,
    center_bucket: str,
    minimum_support_buckets: int = 1,
    taf_tx_bucket: str | None = None,
    probability_stage: str = "",
    convergence_evidence: Mapping[str, Any] | None = None,
    effective_edge_tables: Mapping[str, Any] | None = None,
    off_centre_claim_bands: Mapping[str, Any] | None = None,
    hours_to_cutoff: float | None = None,
    completion_budget_usdc: float | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Contiguous windows admissible as a support, ranked by belief divergence.

    ``hours_to_cutoff`` is the beta lane's second axis (charter 128 v2): it
    picks the lead band whose fitted row, when one clears the lane's
    hurdles, serves in place of the stage's whole row.  None is "no clock",
    which reads the whole row, exactly as a stage without band fits does.

    The incumbent support stays in the list unconditionally.  It is what the
    continuous-core resolver admitted -- evidence, model agreement, hard
    bounds, peripheral reliability -- and this function is a price filter, not
    a second opinion on any of that.  Windows are drawn from the same admitted
    set, so nothing here can seat a bucket the core refused.

    Ranking is by the window's probability mass, and the seat is the head
    of that order -- the curve score sizes the seated window, it does not
    choose it (charter 64).
    Price enters as two floors rather than as the ranking -- the per-leg
    minimum below, and the whole-basket edge requirement -- because any
    ranking on belief-minus-price is maximised where model and market
    disagree most, which is measured to be where this model is worst.
    """

    by_bucket = {row.bucket: row for row in snapshot.buckets}
    # Charter 125: the day's own thermometer, before any price or belief.
    #
    # A bucket whose upper edge the running high has already reached cannot
    # hold the daily maximum -- it is not a bad bet, it is a settled loss --
    # so it leaves the domain here rather than being trusted to hold zero
    # probability.  Until this ruling that trust was the whole mechanism:
    # the comment below still says "a bucket the hard lower bound has
    # excluded holds zero probability", which is true of a P1 posterior and
    # false of a P0 one, because P0 never reads an observation at all.
    #
    # The incumbent is exempt from the *domain* rule below but not from
    # this one: a leg the day has killed may be held, and may not be bought
    # again.  Empty whenever the snapshot carries no running high, which is
    # every pre-day compile.
    observed_dead = (
        frozenset(snapshot.evidence.observed_dead_buckets)
        if observed_high_floor_enabled()
        else frozenset()
    )
    # Charter 136: the legs the chain has already sent to the venue, and what
    # the chain has spent -- the seat a broken basket is completed back to,
    # and the sunk cost both of its risks are measured on.
    chain_seat_buckets = tuple(getattr(snapshot, "chain_seat_buckets", ()) or ())
    chain_cost_usdc = sum(
        float(row.filled_cost_usdc) + float(row.pending_notional_usdc)
        for row in snapshot.buckets
    )
    chain_held_buckets = tuple(
        row.bucket for row in snapshot.buckets
        if float(row.filled_shares) + float(row.pending_shares) > EPS
    )
    charter_136_on = CHARTER_136_COMPLETION == "on"
    shape_exempt_seat_completion = 0
    observed_dead_admitted = sorted(
        (bucket for bucket in observed_dead if bucket in by_bucket),
        key=lambda bucket: (by_bucket[bucket].ordinal, bucket),
    )
    incumbent_buckets = sorted(
        (bucket for bucket in desired if bucket in by_bucket),
        key=lambda bucket: (by_bucket[bucket].ordinal, bucket),
    )
    if not incumbent_buckets:
        # Named apart from the enumeration's own emptiness (2026-09-08): the
        # incumbent window's buckets are not in this snapshot at all, which is
        # a book or bucket-map problem and not a verdict on any window.  It
        # carries no funnel audit because no window was ever enumerated to
        # refuse, and reading it as "every window lost on the number" is the
        # misattribution 495 rows of ``delta_v2_support_empty`` could not be
        # told apart by.
        raise ContractError("delta_v2_support_scope_unquoted")
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
        # Charter 91: the floor falls with our belief in the rung.
        # Charter 103: and it is read off the mid, not the ask.
        return not _leg_below_floor(
            row,
            probabilities.get(row.bucket, float(row.probability)),
            fee_rate=float(fee_rate),
        )

    leg_floor_excluded = sorted(
        (
            row.bucket
            for row in snapshot.buckets
            if float(row.probability) > EPS
            and row.bucket not in observed_dead
            and not _priced_above_floor(row)
        ),
        key=lambda bucket: (by_bucket[bucket].ordinal, bucket),
    )
    admitted = sorted(
        (
            row.bucket
            for row in snapshot.buckets
            if float(row.probability) > EPS
            # Charter 125.  Ahead of the price filter, because this is not a
            # judgement about price: the outcome is already decided against
            # this rung and no quote can make it a bet again.
            and row.bucket not in observed_dead
            and _priced_above_floor(row)
        ),
        key=lambda bucket: (by_bucket[bucket].ordinal, bucket),
    )
    for bucket in incumbent:
        # Charter 125: the incumbent's unconditional re-entry is what keeps
        # a held basket representable, and it stops at a rung the day has
        # already settled against.  Holding such a leg is a fact; putting it
        # back in the domain would let the same dead rung be seated and
        # bought again, which is the one thing this ruling forbids.
        if bucket not in admitted and bucket not in observed_dead:
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

    def _best_ask_sum(window: Sequence[str]) -> float | None:
        """Best-ask sum, fee-free: the basis the convergence evidence's
        earlier book is read on, so the move compares like with like."""

        total = 0.0
        for bucket in window:
            row = by_bucket[bucket]
            if not row.ask_available:
                return None
            total += float(row.ask_price)
        return total

    def _completion_increment(
        window: Sequence[str],
        *,
        quotes: Sequence[float | None],
        claim_factor: float,
    ) -> dict[str, Any] | None:
        """Charter 84 (八): what completing a partly held window would buy.

        ``None`` when the window holds nothing, holds everything, or the
        ruling is off -- the whole-window rule then stands.  Otherwise the
        missing rungs split into the market-dead (an offer under the leg
        floor: not bought, not a refusal) and the buyable; the increment is
        admissible when the buyable rungs' claim clears their all-in offer
        by the same friction the whole-window rule charges.  No buyable
        rung is an increment of nothing, which is inadmissible: the
        baseline is the only answer then.
        """

        if CHARTER_84_LADDER_BALANCE != "on":
            return None
        quote_by_bucket = dict(zip(window, quotes))
        held = [
            bucket for bucket in window
            if float(by_bucket[bucket].filled_shares)
            + float(by_bucket[bucket].pending_shares) > EPS
        ]
        if not held or len(held) == len(window):
            return None
        missing = [bucket for bucket in window if bucket not in held]
        dead = [
            bucket for bucket in missing
            # Charter 125 first: a rung the observations killed is missing
            # for a reason no quote can undo, so it joins the market-dead
            # set whether or not it carries an offer at all.
            if bucket in observed_dead
            or (
                quote_by_bucket.get(bucket) is not None
                and _leg_below_floor(
                    by_bucket[bucket],
                    probabilities.get(bucket),
                    fee_rate=float(fee_rate),
                )
            )
        ]
        buyable = [bucket for bucket in missing if bucket not in dead]
        increment_quotes = [quote_by_bucket.get(bucket) for bucket in buyable]
        increment_priced = bool(buyable) and all(
            quote is not None for quote in increment_quotes
        )
        increment_price = (
            sum(float(quote) for quote in increment_quotes)
            if increment_priced else None
        )
        increment_win = min(
            1.0 - EPS,
            sum(float(probabilities[bucket]) for bucket in buyable)
            * float(claim_factor),
        )
        # Charter 130: the increment clears the one hurdle, in dollars of
        # its own cost.  Recorded under the key the audit always read.
        increment_friction = (
            economic_hurdle_usdc(increment_price)
            if increment_price is not None else 0.0
        )
        increment_edge = (
            increment_win - increment_price
            if increment_price is not None else None
        )
        clears_hurdle = bool(
            increment_priced
            and increment_edge is not None
            and clears_economic_hurdle_v1(
                expected_payout=increment_win, cost=increment_price,
            )
        )
        # Charter 136 (五): a broken seat is completed back to itself.  A leg
        # the chain never sent is not a completion, whichever rule admits it.
        outside_seat = bool(
            charter_136_on
            and chain_seat_buckets
            and any(bucket not in chain_seat_buckets for bucket in buyable)
        )
        if outside_seat:
            clears_hurdle = False
        # Charter 136: the exposure the completion removes against the edge
        # it gives up, inside the seat's own budget.  The claim is the
        # model's own; the kernel conditions it on the market's price.
        risk_budget = (
            completion_risk_budget_v1(
                window=tuple(window),
                held_shares_by_bucket={
                    bucket: float(by_bucket[bucket].filled_shares)
                    + float(by_bucket[bucket].pending_shares)
                    for bucket in window
                },
                held_cost_by_bucket={
                    bucket: float(by_bucket[bucket].filled_cost_usdc)
                    + float(by_bucket[bucket].pending_notional_usdc)
                    for bucket in window
                },
                chain_cost_usdc=chain_cost_usdc,
                ask_by_bucket={
                    bucket: quote_by_bucket.get(bucket) for bucket in window
                },
                depth_by_bucket={
                    bucket: tuple(
                        {"price": float(level.price) * (1.0 + float(fee_rate)),
                         "shares": float(level.shares)}
                        for level in by_bucket[bucket].ask_levels
                    )
                    for bucket in window
                },
                claim_by_bucket={
                    bucket: float(probabilities.get(bucket, 0.0) or 0.0)
                    for bucket in window
                },
                budget_usdc=float(completion_budget_usdc or 0.0),
                hours_to_cutoff=hours_to_cutoff,
                chain_seat_buckets=chain_seat_buckets,
                chain_held_buckets=chain_held_buckets,
                dead_buckets=tuple(dead),
            )
            if charter_136_on and increment_priced
            else None
        )
        risk_admits = bool(risk_budget and risk_budget["admits"])
        admissible = bool(clears_hurdle or risk_admits)
        admitted_by = (
            "hurdle" if clears_hurdle
            else COMPLETION_RISK_BUDGET_RULE_VERSION if risk_admits
            else None
        )
        # Charter 143: the completion is admitted by what the window already
        # holds, and replaces both readings above (charter 130's hurdle on
        # the increment, charter 136's exposure budget).  Two held legs or
        # more: every buyable rung, priced.  One: the rungs that clear the
        # hurdle on their own offer -- the candidate then buys those alone.
        # Past the completion cutoff neither.  Which level, and whether the
        # multi-leg completion leaves the window better off, is the
        # candidate's reading (``completion_143_targets_v1``), priced down
        # the ladder it will buy.
        completion_143 = None
        if CHARTER_143_COMPLETION == "on":
            basket_type = completion_143_basket_type_v1(len(held))
            past_cutoff = bool(
                hours_to_cutoff is not None
                and float(hours_to_cutoff) <= COMPLETION_CUTOFF_HOURS
            )
            # v2: a leg the chain never sent is not missing -- it belongs to
            # a seat the label moved to (ZGGG 32C, ZSQD 27C, 2026-09-17).
            sent_143 = _completion_143_sent_shares(snapshot)
            if (
                taf_entry_window is not None
                and frozenset(int(by_bucket[b].ordinal) for b in window)
                == taf_entry_window
            ):
                # Charter 90: the TX-entry window is sanctioned whole on TAF
                # authority; its legs need no send history (the TX leg has
                # none by definition).
                sent_143 = None
            sent_buyable = [
                bucket for bucket in buyable
                if sent_143 is None or bucket in sent_143
            ]
            sent_priced = bool(sent_buyable) and all(
                quote_by_bucket.get(bucket) is not None
                for bucket in sent_buyable
            )
            cheap = [
                bucket for bucket in sent_buyable
                for quote in (quote_by_bucket.get(bucket),)
                if quote is not None
                and clears_economic_hurdle_v1(
                    expected_payout=min(
                        1.0 - EPS,
                        float(probabilities[bucket]) * float(claim_factor),
                    ),
                    cost=float(quote),
                )
            ]
            admits_143 = bool(
                not past_cutoff
                and (
                    (basket_type == "multi" and sent_priced)
                    or (basket_type == "single" and cheap)
                )
            )
            completion_143 = {
                "rule": COMPLETION_143_RULE_VERSION,
                "basket_type": basket_type,
                "past_completion_cutoff": past_cutoff,
                # What the rule read as the chain's send history -- the
                # snapshot's, or ``None`` on the TX-entry window / a snapshot
                # carrying none.  Recorded because a plan that only says
                # "unsent: []" cannot tell a leg that was sent from a history
                # that was never read (ZUUU 2026-09-18 27C, 08:43Z).
                "send_history": (
                    None if sent_143 is None
                    else {str(b): float(q) for b, q in sent_143.items()}
                ),
                "sent_buyable_buckets": sent_buyable,
                "unsent_buckets": [
                    bucket for bucket in buyable if bucket not in sent_buyable
                ],
                "hurdle_clearing_buckets": cheap,
                "admits": admits_143,
            }
            admissible = admits_143
            admitted_by = COMPLETION_143_RULE_VERSION if admits_143 else None
        return {
            "held_buckets": held,
            "missing_buckets": missing,
            "market_dead_buckets": dead,
            "buyable_buckets": buyable,
            "increment_all_in_price": increment_price,
            "increment_win_probability": increment_win,
            "increment_edge": increment_edge,
            "increment_friction": increment_friction,
            "admissible": admissible,
            "admitted_by": admitted_by,
            "outside_seat": outside_seat,
            "completion_risk_budget": risk_budget,
            **(
                {"completion_143": completion_143}
                if completion_143 is not None else {}
            ),
        }

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
    # The heaviest bucket of the combined model distribution was the ruling
    # centre from 2026-08-16 (charter 52a, open edge buckets competing on
    # the same footing; it had superseded the clause 42 point-estimate
    # anchor, accepting the ZUUU 30C -> "33C or higher" edge-bucket case).
    # Since 2026-08-25 (charter 54) it is only the fallback seed: the
    # ruling centre is the mass-weighted mean, applied just below, and the
    # argmax survives here for the ``heaviest`` research arm and for days
    # the alternate definition cannot price (empty mass).  See
    # LADDER_CENTER_DEFINITION for the measurements behind the flip.
    ladder_center = max(
        admitted,
        key=lambda bucket: (
            float(probabilities.get(bucket, 0.0)),
            -int(by_bucket[bucket].ordinal),
        ),
    ) if admitted else center_bucket
    if admitted and LADDER_CENTER_DEFINITION != "heaviest":
        ladder_center = _alternate_ladder_center(
            LADDER_CENTER_DEFINITION,
            admitted=admitted,
            probabilities=probabilities,
            ordinals={
                bucket: int(row.ordinal)
                for bucket, row in by_bucket.items()
            },
        ) or ladder_center
    # Charter 131: the day's three-state reading, once.  It fixes the
    # seat's shape below and nothing else; the market is data, not a rule.
    skew_sign = _skew_extension_sign(
        probabilities,
        {bucket: int(row.ordinal) for bucket, row in by_bucket.items()},
    )
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
    shape_refused = 0
    market_lean_refused = 0
    # What the gate refused, not just how many (user ruling 2026-09-08,
    # "market_lean 加上：在拒绝时把窗口的桶记下来").  It was promoted on
    # 09-05 and refused 60 windows in its first four days; every one of them
    # was a counter increment and nothing else, so which windows they were --
    # and whether the truth landed inside them -- is not recoverable.  A gate
    # whose refusals are not written down cannot be priced after the fact,
    # and this one was promoted on a replay rather than on its own record.
    # Bounded because a wide day enumerates thousands of windows; the count
    # beside it stays exact, so a truncated list never reads as the whole.
    market_lean_refused_windows: list[dict[str, Any]] = []
    market_lean_shadow_refused = 0
    # Computed once per solve, not per window: the book and the model do
    # not change inside the enumeration.
    market_lean_value = (
        market_lean_ordinal(
            by_bucket,
            probabilities,
            {bucket: int(row.ordinal) for bucket, row in by_bucket.items()},
        )
        if MARKET_LEAN_GATE_MODE != "off"
        else None
    )
    model_mean_ordinal_value = None
    if market_lean_value is not None:
        model_pairs = [
            (int(by_bucket[bucket].ordinal), float(value))
            for bucket, value in probabilities.items()
            if bucket in by_bucket and float(value) > 0.0
        ]
        mass = sum(value for _, value in model_pairs)
        if mass > 0.0:
            model_mean_ordinal_value = (
                sum(o * v for o, v in model_pairs) / mass
            )
    leg_floor_refused = 0
    gapped_refused = 0
    unpriced_refused = 0
    edge_refused = 0
    lambda_refused = 0
    # Charter 140: the windows the price stage refused, as the claim lanes'
    # evidence.  The list below keeps only admitted windows and refused
    # incumbents, so without this the lambda and beta populations -- "every
    # window the enumeration offered" (charter 87) -- were the gate's own
    # survivors in production and in the paper ledger alike.  Structural
    # refusals (centre, length, shape, gaps) are not offered seats and stay
    # counts only.
    refused_priced_windows: list[dict[str, Any]] = []
    # Charter 128 (shadow): what the beta gate would have done to the
    # windows the standing gate admitted and refused.
    beta_would_refuse = 0
    beta_would_admit = 0
    beta_served = (effective_edge_tables or {}).get(BETA_TABLE_KEY) or {}
    # Charter 128 v2: the market's belief, once per snapshot, from the same
    # reference charter 132 reads.  The beta lane regresses on ``p - m`` and
    # serves ``m + beta_lcb * (p - m)``; the all-in ask is only ever the
    # cost that belief is compared with.  Built once so every window sums
    # one vector.
    market_reference: dict[str, Any] | None = (
        market_reference_v1(snapshot.buckets)
        if (
            VALUATION_MODE != "off" or BETA_EDGE_MODE != "off"
            or POSTERIOR_MODE != "off"
        )
        else None
    )
    beta_market_vector: dict[str, float] | None = (
        market_reference["vector"]
        if market_reference is not None and market_reference["state"] == "complete"
        else None
    )
    beta_market_state = (
        "off" if market_reference is None
        else str(market_reference.get("state") or "")
    )
    # The band row that serves this snapshot, per stage: the lead band the
    # decision falls in when that band's row clears the lane's hurdles,
    # else the stage's whole row.  Resolved once, not per window.
    beta_lead_band_label = beta_lead_band(hours_to_cutoff)

    def _beta_row_for(stage_name: str) -> tuple[dict[str, Any] | None, str | None]:
        stage_row = beta_served.get(stage_name)
        if not isinstance(stage_row, dict):
            return None, None
        if beta_lead_band_label is not None:
            band_row = (stage_row.get("lead_bands") or {}).get(beta_lead_band_label)
            if isinstance(band_row, dict) and not band_row.get("accumulating"):
                return band_row, beta_lead_band_label
        return stage_row, None
    # Charter 132: the conditional net edge on the fit's joint draws, beside
    # charter 128's reading, so the two can be argued on one record.
    beta_v2_would_refuse = 0
    beta_v2_would_admit = 0
    # Charter 132: one valuation vector for the whole snapshot.  Built once,
    # here, so every window below sums the same vector -- no window can be
    # worth more than one that contains it, and a fee or a depth change moves
    # the action's cost, never the belief.
    valuation_served = (effective_edge_tables or {}).get(VALUATION_TABLE_KEY) or {}
    valuation_row = (
        valuation_served.get(str(probability_stage or ""))
        if isinstance(valuation_served, Mapping) else None
    )
    valuation_market: dict[str, Any] | None = None
    valuation_point_vector: dict[str, float] | None = None
    valuation_draws: list[dict[str, float]] = []
    valuation_state = "off"
    valuation_authorized = False
    if VALUATION_MODE != "off":
        valuation_market = market_reference
        if not isinstance(valuation_row, Mapping):
            valuation_state = "no_fit"
        elif valuation_row.get("accumulating") or valuation_row.get("beta") is None:
            valuation_state = "accumulating"
        elif valuation_market["state"] != "complete":
            valuation_state = "market_" + str(
                valuation_market.get("reason") or "incomplete"
            ).split(":")[0]
        else:
            valuation_point_vector = valuation_vector_v1(
                probabilities, valuation_market["vector"],
                float(valuation_row["beta"]), excluded=observed_dead,
            )
            valuation_draws = valuation_draw_vectors_v1(
                probabilities, valuation_market["vector"],
                valuation_row.get("beta_draws") or (), excluded=observed_dead,
            )
            if valuation_point_vector is None or not valuation_draws:
                valuation_state = "no_vector"
                valuation_point_vector = None
            elif valuation_row.get("verdict") != "admitted":
                # Values are computed and recorded; only the ``on`` gate
                # needs the walk-forward verdict.
                valuation_state = "unverified_" + str(
                    valuation_row.get("verdict") or "missing"
                )
            else:
                valuation_state = "admitted"
                valuation_authorized = True
    valuation_would_refuse = 0
    valuation_would_admit = 0
    valuation_refused = 0
    valuation_hold_refused = 0
    # L4 posterior lane (first-principles baseline, shadow): the belief the
    # baseline's gate would read, built once per snapshot from the served
    # ``(alpha, gamma, w)`` row -- the decision's lead band when that band's
    # row clears the hurdles, else the stage's whole row -- and its draws.
    # Wide catch on purpose: a shadow reading that cannot be taken is a
    # state on the record, never a blocked decision.
    posterior_served = (effective_edge_tables or {}).get(POSTERIOR_TABLE_KEY) or {}
    posterior_row: Mapping[str, Any] | None = None
    posterior_band: str | None = None
    posterior_point_vector: dict[str, float] | None = None
    posterior_draws: list[dict[str, float]] = []
    posterior_state = "off"
    if POSTERIOR_MODE != "off":
        try:
            _stage_row = (
                posterior_served.get(str(probability_stage or ""))
                if isinstance(posterior_served, Mapping) else None
            )
            if not isinstance(_stage_row, Mapping):
                posterior_state = "no_fit"
            else:
                posterior_row, posterior_band = _stage_row, None
                if beta_lead_band_label is not None:
                    _band_row = (_stage_row.get("lead_bands") or {}).get(
                        beta_lead_band_label
                    )
                    if isinstance(_band_row, Mapping) and not _band_row.get("accumulating"):
                        posterior_row, posterior_band = _band_row, beta_lead_band_label
                if posterior_row.get("accumulating") or posterior_row.get("w") is None:
                    posterior_state = "accumulating"
                elif market_reference is None or market_reference["state"] != "complete":
                    posterior_state = "market_" + str(
                        (market_reference or {}).get("reason") or "incomplete"
                    ).split(":")[0]
                else:
                    _params = {
                        key: posterior_row.get(key) for key in ("alpha", "gamma", "w")
                    }
                    posterior_point_vector = posterior_vector_v1(
                        probabilities, market_reference["vector"], _params,
                        excluded=observed_dead,
                    )
                    posterior_draws = posterior_draw_vectors_v1(
                        probabilities, market_reference["vector"],
                        posterior_row.get("draws") or (), excluded=observed_dead,
                    )
                    if posterior_point_vector is None or not posterior_draws:
                        posterior_state = "no_vector"
                        posterior_point_vector = None
                        posterior_draws = []
                    else:
                        posterior_state = "shadow"
        except Exception as exc:  # noqa: BLE001 -- a shadow never blocks a decision
            posterior_state = "error_" + type(exc).__name__
            posterior_point_vector = None
            posterior_draws = []
    posterior_would_refuse = 0
    posterior_would_admit = 0
    enumerated = 0
    shortlisted = 0
    # The window the price gate came closest to admitting.  The counters
    # below say how many were refused and by which gate; they cannot say
    # *which* basket the objective wanted, so a reader looking at "边际不敌
    # 摩擦" sees a number of refusals and no target.  Kept only for the
    # gate that is genuinely about price -- a structural refusal has no
    # near-miss, it has an illegal shape -- and only as a diagnostic: it is
    # a window that was refused, and nothing downstream may seat it.
    best_refused: dict[str, Any] | None = None
    # Arm B (2026-09-06): the convergence claim lift's shadow ledger.
    convergence_candidates = 0
    convergence_would_admit = 0
    convergence_admitted_by_lift = 0
    convergence_rows: list[dict[str, Any]] = []

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
    # Charter 131: two to three rungs at every stage.  The floor cannot ask
    # for more rungs than the day still has (late in P1 the observed high
    # can leave one live outcome), and the cap here is the seat's, not the
    # chain's -- charter 90's TX-entry span reads the chain's below.
    stage_min = int(minimum_support_buckets)
    policy_min = max(1, min(stage_min, max_size, len(admitted)))
    min_size = policy_min
    max_size = max(policy_min, min(max_size, SEAT_MAX_BUCKETS))
    # Charter 90 reads the policy's cap, not the dispersion-tightened one:
    # the TX entering the basket is the ruling, a sharp day does not veto it.
    policy_cap = CHAIN_MAX_BUCKETS
    # Charter 90: the TAF TX as the P0 basket's second confirmation.  Held
    # rungs are what P0 (or an earlier P0-M) actually bought; the TX bucket
    # inside them confirms the basket and the TAF constrains nothing.  Outside
    # them the TX must enter: the seat becomes the span from the centre to the
    # TX, keeps as many held rungs as the cap allows, and is padded to the
    # floor with the ladder's own rungs -- toward the heavier side.
    # Charter 131 (user, 2026-09-12 evening: "P0-M 不单独开链建仓，必须要有
    # P0"): with nothing held there is no basket for the TX to confirm or
    # enter, so on an empty chain the TAF constrains nothing and charter 74
    # holds the stage.  Charter 90's one later-stage opening is retired.
    taf_confirmed = False
    taf_entry_required = False
    taf_no_p0_basket = False
    taf_entry_window: frozenset[int] | None = None
    taf_unreachable = False
    taf_held_buckets: list[str] = []
    if (
        P0M_TAF_MODE == "tx_envelope"
        and str(probability_stage or "") == "P0-M"
        and taf_tx_bucket in by_bucket
        and ladder_center in by_bucket
    ):
        ordinal_of = {
            bucket: int(row.ordinal) for bucket, row in by_bucket.items()
        }
        held_ordinals = {
            ordinal_of[bucket]
            for bucket, row in by_bucket.items()
            if float(getattr(row, "filled_shares", 0.0) or 0.0)
            + float(getattr(row, "pending_shares", 0.0) or 0.0) > EPS
        }
        taf_held_buckets = sorted(
            (bucket for bucket in by_bucket if ordinal_of[bucket] in held_ordinals),
            key=lambda bucket: ordinal_of[bucket],
        )
        tx_ordinal = ordinal_of[taf_tx_bucket]
        centre_ordinal = ordinal_of[ladder_center]
        if not held_ordinals:
            taf_no_p0_basket = True
        elif tx_ordinal in held_ordinals:
            taf_confirmed = True
        else:
            taf_entry_required = True
            span = set(range(
                min(centre_ordinal, tx_ordinal), max(centre_ordinal, tx_ordinal) + 1
            ))
            if len(span) > policy_cap or taf_tx_bucket not in admitted:
                # Wider than the policy's cap, or a TX the observations
                # have already ruled out (charter 61): nothing to seat.
                taf_unreachable = True
            else:
                mass_of = {
                    ordinal_of[bucket]: float(probabilities.get(bucket, 0.0) or 0.0)
                    for bucket in by_bucket
                }
                ladder_ordinals = set(ordinal_of.values())

                def _grow(pool: set[int]) -> bool:
                    edges = [
                        ordinal for ordinal in (min(span) - 1, max(span) + 1)
                        if ordinal in pool
                    ]
                    if not edges:
                        return False
                    span.add(max(
                        edges,
                        key=lambda ordinal: (
                            mass_of.get(ordinal, 0.0), -abs(ordinal - centre_ordinal)
                        ),
                    ))
                    return True

                while len(span) < policy_cap and _grow(held_ordinals):
                    pass
                while len(span) < policy_min and _grow(ladder_ordinals):
                    pass
                taf_entry_window = frozenset(span)
    if taf_entry_window is not None and len(taf_entry_window) > max_size:
        # The entry seat has to be enumerated to be sanctioned.
        max_size = len(taf_entry_window)
    allowed_shapes, seat_shape_audit = seat_shape_windows_v1(
        centre_ordinal=(
            int(by_bucket[ladder_center].ordinal)
            if ladder_center in by_bucket else None
        ),
        sign=skew_sign,
        stage=str(probability_stage or ""),
        probability_by_ordinal={
            int(row.ordinal): float(probabilities.get(bucket, 0.0) or 0.0)
            for bucket, row in by_bucket.items()
        },
        live_ordinals={int(by_bucket[bucket].ordinal) for bucket in admitted},
    )
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
            #
            # How far off centre, not merely whether.  The fitted table has
            # no distance axis -- it was measured as containment, yes or no --
            # so under the default ramp of 1 any gap at all still takes the
            # whole deflation and this is the same arithmetic it always was.
            # The distance is carried so the ramp can be measured at all:
            # the claim centre and the seating centre are different buckets
            # in 16-34% of scopes depending on stage, 86% of those by exactly
            # one bucket, and a one-bucket miss currently costs the same 36%
            # as a five-bucket one.
            holds_center = center_bucket in window
            center_gap = (
                0
                if holds_center
                else min(
                    abs(
                        by_bucket[bucket].ordinal
                        - by_bucket[center_bucket].ordinal
                    )
                    for bucket in window
                )
                if center_bucket in by_bucket
                # An unknown centre keeps the pre-2026-08-27 answer rather
                # than silently reading as "on centre".
                else 1
            )
            # One implementation, read here and by the band-axis replay:
            # the off-centre deflation with charter 81's centre lift on top.
            claim_factor = window_claim_factor(
                raw_win, center_gap=center_gap,
                off_centre_bands=(off_centre_claim_bands or {}).get(
                    str(probability_stage or "")
                ),
            )
            center_lift = 1.0
            if holds_center:
                # Research arm (2026-09-05): the centre-holding mirror of
                # the deflation above; identity unless the arm is on.  Kept
                # apart because the sizing lane swaps it on its own.
                center_lift = center_window_claim_factor(raw_win)
            quotes = [_all_in(bucket) for bucket in window]
            priced = all(quote is not None for quote in quotes)
            price = sum(quote for quote in quotes if quote is not None)
            # Audit only (user ruling, 2026-09-09: "记成审计列，不当闸").  What
            # this window claims per unit of cost, and -- when the seat
            # already holds something -- what the rungs it ADDS claim per
            # unit of what they add.  Priced on the same all-in ask the gate
            # and the Kelly stake read, and on the raw claim, so the row can
            # be re-deflated afterwards by whichever lambda a reader wants to
            # test rather than being frozen at the one served today.
            coverage = marginal_coverage_reading_v1(
                claim_by_bucket=probabilities,
                cost_by_bucket={
                    bucket: quote
                    for bucket, quote in zip(window, quotes)
                    if quote is not None
                },
                window=window,
                held_buckets=[
                    bucket for bucket in window
                    if float(by_bucket[bucket].filled_shares)
                    + float(by_bucket[bucket].pending_shares) > EPS
                ],
            ) if priced else None
            # Arm B (2026-09-06): the convergence claim lift.  The state is
            # read off the served mass before any lift and the best-ask sum
            # at both ends; in shadow it is journaled and the factor stays
            # 1.0, on the arm it multiplies the claim ahead of the gate.
            convergence: dict[str, Any] | None = None
            convergence_lift = 1.0
            convergence_shadow_lift = 1.0
            if (
                convergence_evidence
                and priced
                and raw_win > EPS
                and _kernel_delta_v2.CONVERGENCE_CLAIM_MODE != "off"
            ):
                convergence = convergence_state_v1(
                    stage=probability_stage,
                    claim_now=raw_win,
                    claim_prev=_evidence_window_sum(
                        convergence_evidence.get("prior_probabilities"), window
                    ),
                    price_now=_best_ask_sum(window),
                    price_prev=_evidence_window_sum(
                        convergence_evidence.get("prior_asks"), window
                    ),
                    agreement_now=price / raw_win,
                )
                if convergence["converging"]:
                    convergence_shadow_lift = convergence_claim_factor(convergence)
                    if _kernel_delta_v2.CONVERGENCE_CLAIM_MODE == "on":
                        convergence_lift = convergence_shadow_lift
                        claim_factor *= convergence_lift
                convergence["shadow_factor"] = convergence_shadow_lift
                convergence["applied_factor"] = convergence_lift
            win = min(1.0 - EPS, raw_win * claim_factor)
            # Charter 87: one belief.  The gate reads what the solver sizes
            # on -- the lifted claim times charter 68's lambda at this
            # window's agreement (price over the lifted claim, the a the
            # solver looked up) -- so a window the book prices above what
            # lambda says arrives is refused here, instead of being
            # admitted, sized to nothing and bought as a ticket.  P1's
            # table is identity, so P1 reads as before; the bucket-scope
            # research arm deflates per rung at the solver and leaves the
            # gate alone.  ``win_probability`` stays the lifted claim.
            gate_lambda = None
            gate_agreement = None
            gate_lambda_source = None
            if (
                CHARTER_87_GATE_EFFECTIVE_EDGE == "on"
                and EFFECTIVE_EDGE_MODE == "on"
                and EFFECTIVE_EDGE_SCOPE == "window"
                and str(probability_stage or "") in _EFFECTIVE_EDGE_GATE_STAGES
                and priced
                and win > EPS
            ):
                gate_agreement = price / win
                # Charter 97: two axes, one lambda.  The thin claim-edge band
                # serves its own measurement; everywhere else this is charter
                # 68's agreement lookup unchanged.
                gate_lambda, gate_lambda_source = (
                    effective_edge_lambda_resolved(
                        str(probability_stage or ""),
                        claim=win,
                        price=price,
                        tables=effective_edge_tables,
                    )
                )
                gate_lambda = float(gate_lambda)
            # Multiply, then clamp -- the order the solver's partition uses
            # (``deflate_window_partition_v1`` clamps the corrected mass),
            # so a lifted claim the clamp had capped at one reads the same
            # number here as there: KDAL 2026-09-07 is 0.905 on both, not
            # 0.893 at the gate and 0.905 at the solver.
            effective_win = (
                min(1.0 - EPS, raw_win * claim_factor * gate_lambda)
                if gate_lambda is not None else win
            )
            # Charter 124: the worst claim inside the ambiguity ball, and what
            # the gate would read on it.  Computed on ``effective_win`` -- the
            # belief the gate actually reads -- so the shadow answers the
            # question a promotion would be argued on ("would this seat still
            # have cleared") rather than a question about an intermediate
            # number nothing gates on.
            robust_claim = None
            robust_factor = None
            robust_edge = None
            if CHARTER_124_ROBUST_CLAIM != "off" and priced and effective_win > EPS:
                robust_claim, robust_factor = robust_window_claim_v1(
                    effective_win,
                    divergence_budget=ROBUST_CLAIM_DIVERGENCE_BUDGET_NATS,
                )
                robust_edge = robust_claim - price
                if CHARTER_124_ROBUST_CLAIM == "on":
                    # The factor, not the level: it rides through the same
                    # partition deflation as charters 81 and 68, so the whole
                    # chain -- gate, stake, curve, growth -- reads one
                    # renormalised belief instead of a window whose rungs no
                    # longer sum with the rest of the day.
                    claim_factor *= float(robust_factor)
                    win = min(1.0 - EPS, raw_win * claim_factor)
                    effective_win = (
                        min(1.0 - EPS, raw_win * claim_factor * gate_lambda)
                        if gate_lambda is not None else win
                    )
            # Charter 128 (v2 basis): the beta belief, ``m + beta_lcb * (p -
            # m)`` on the raw claim and the market's belief for the window,
            # judged against the all-in ask under charter 130's hurdle.
            # Under ``shadow`` the values are recorded on the row and gate
            # nothing; under ``on`` they ARE the belief -- the gate reads it
            # and the solver's lambda becomes the ratio that reproduces it,
            # so admission and sizing still read one number.  A stage with
            # no fit serves ``beta_lcb = 0``: belief == the market's, which
            # never clears the ask.  A snapshot whose book gives no market
            # belief records nothing in shadow and refuses under ``on``: a
            # belief the lane cannot state is not a belief to buy on.
            beta_lcb = None
            beta_point = None
            beta_sha = None
            beta_belief = None
            beta_edge = None
            beta_admissible = None
            beta_market_mid = None
            beta_band = None
            beta_row = None
            if BETA_EDGE_MODE != "off" and priced:
                beta_row, beta_band = _beta_row_for(str(probability_stage or ""))
                if beta_market_vector is not None:
                    beta_market_mid = valuation_window_mass(beta_market_vector, window)
                if isinstance(beta_row, dict):
                    beta_lcb = float(beta_row.get("beta_lcb") or 0.0)
                    beta_point = beta_row.get("beta")
                    beta_sha = beta_row.get("sha256")
                elif BETA_EDGE_MODE == "on":
                    beta_lcb = 0.0
                if beta_lcb is not None and beta_market_mid is not None:
                    beta_belief = beta_belief_v1(raw_win, beta_market_mid, beta_lcb)
                    beta_edge = beta_belief - price
                    # The lane buys the DISAGREEMENT, never the book's own
                    # arithmetic: at ``beta_lcb <= 0`` the belief is the
                    # market's, and a normalised mid that happens to clear
                    # the ask (a ladder whose asks sum under one) is not
                    # evidence the model earned.  No evidence, no seat.
                    beta_admissible = beta_lcb > EPS and clears_economic_hurdle_v1(
                        expected_payout=beta_belief, cost=price,
                    )
                    if BETA_EDGE_MODE == "on":
                        if beta_lcb > EPS:
                            effective_win = beta_belief
                            gate_lambda_source = "beta_edge_lcb"
                        else:
                            effective_win = EPS
                            gate_lambda_source = "beta_edge_lcb_zero"
                        gate_lambda = (
                            effective_win / win if win > EPS else None
                        )
                        if win > EPS:
                            gate_agreement = price / win
                elif beta_lcb is not None and BETA_EDGE_MODE == "on":
                    # No market belief for this snapshot: the lane cannot
                    # say what the window is worth, so nothing is.
                    beta_admissible = False
                    effective_win = EPS
                    gate_lambda = effective_win / win if win > EPS else None
                    gate_lambda_source = "beta_edge_no_market"
                    if win > EPS:
                        gate_agreement = price / win
            # Charter 132: charter 128's slope read as a prediction on the
            # joint draws -- intercept kept, sign-safe -- recorded beside it.
            # On the v2 basis the prediction is of ``hit - m``, so the cost
            # test adds the market's belief back before charging the ask.
            beta_v2 = None
            beta_v2_admissible = None
            if (
                BETA_EDGE_MODE != "off" and priced
                and isinstance(beta_row, Mapping)
                and beta_market_mid is not None
            ):
                # v3 (user ruling, 2026-09-17): the prediction carries the
                # market's own slope, gamma * (m - m0), beside the intercept
                # and the disagreement share.  A row fitted before the third
                # knob reads gamma = 0, which is the v2 prediction exactly.
                beta_v2 = beta_conditional_edge_v3(
                    raw_win, beta_market_mid, beta_row.get("joint_draws") or (),
                    market_centre=beta_row.get("market_centre"),
                )
                if beta_v2 is not None:
                    beta_v2_admissible = clears_economic_hurdle_v1(
                        expected_payout=beta_market_mid + beta_v2["edge_lcb"],
                        cost=price,
                    )
            # Charter 132: the pooled valuation of this window as an action
            # -- one share per leg at the all-in ask -- and how much of it the
            # book can take before the lower bound stops clearing the hurdle.
            valuation_mass = None
            valuation_market_mass = None
            valuation_edge_point = None
            valuation_edge_lcb = None
            valuation_admissible = None
            valuation_capacity = None
            if valuation_point_vector is not None and priced:
                valuation_mass = valuation_window_mass(valuation_point_vector, window)
                valuation_market_mass = valuation_window_mass(
                    valuation_market["vector"], window
                )
                _value = valuation_action_value_v1(
                    valuation_draws, {bucket: 1.0 for bucket in window}, price,
                )
                valuation_edge_point = valuation_mass - price
                valuation_edge_lcb = (
                    None if _value is None else float(_value["value_lcb"])
                )
                if valuation_edge_lcb is not None:
                    valuation_admissible = (
                        valuation_edge_lcb >= economic_hurdle_usdc(price) - EPS
                    )
                    valuation_capacity = valuation_capacity_v1(
                        window=window,
                        ask_levels_by_bucket={
                            bucket: [
                                (float(level.price) * (1.0 + float(fee_rate)),
                                 float(level.shares))
                                for level in by_bucket[bucket].ask_levels
                            ]
                            for bucket in window
                        },
                        mass_lcb=valuation_edge_lcb + price,
                        hurdle=ECONOMIC_HURDLE_V1,
                        min_order_size=max(
                            float(by_bucket[bucket].min_order_size or 0.0)
                            for bucket in window
                        ),
                    )
                if VALUATION_MODE == "on" and valuation_authorized:
                    # One belief: the gate and the solver read the pooled
                    # mass; lambda becomes the ratio that reproduces it in the
                    # partition, exactly as charter 128's ``on`` did.
                    effective_win = min(1.0 - EPS, max(EPS, valuation_mass))
                    gate_lambda = effective_win / win if win > EPS else None
                    gate_lambda_source = "valuation_pool"
                    gate_agreement = price / win if win > EPS else None
            # L4 posterior lane (shadow): this window's belief as a sum over
            # the snapshot's one posterior vector, the net edge's lower bound
            # across the draws, and the verdict the baseline's gate (E8:
            # EV_q05 > hurdle) would give.  Recorded; the gate never reads it.
            posterior_mass = None
            posterior_market_mass = None
            posterior_edge_point = None
            posterior_edge_lcb = None
            posterior_admissible = None
            if posterior_point_vector is not None and priced:
                posterior_mass = posterior_window_mass(posterior_point_vector, window)
                posterior_market_mass = posterior_window_mass(
                    market_reference["vector"], window
                )
                _pvalue = valuation_action_value_v1(
                    posterior_draws, {bucket: 1.0 for bucket in window}, price,
                )
                posterior_edge_point = posterior_mass - price
                posterior_edge_lcb = (
                    None if _pvalue is None else float(_pvalue["value_lcb"])
                )
                if posterior_edge_lcb is not None:
                    posterior_admissible = (
                        posterior_edge_lcb >= economic_hurdle_usdc(price) - EPS
                    )
            edge = effective_win - price if priced else None
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
            # There are exactly two hard admission floors, and everything
            # else is ranking; the seat is the head of that ranking.  First,
            # the ladder rung: the window must contain the centre bucket.
            # Direction is deliberately not checked anywhere -- among the
            # windows holding the rung, the flanks compete on curve score
            # and the better-priced side wins its rungs automatically.
            holds_core = bool(core) and bool(set(window) & core)
            if ladder_center not in window:
                center_refused += 1
                # Under ``deflate_only`` the window is priced rather than
                # refused: its claim is already cut for the distance to the
                # centre a few lines below, and the arm asks whether that
                # cut is enough on its own.  See CENTRE_CONTAIN_GATE.
                if CENTRE_CONTAIN_GATE == "on":
                    continue
            if len(window) < min_size:
                short_refused += 1
                continue
            # Charter 131: the seat's shape is the ruling's, whatever the
            # price.  Symmetric: {c-1, c, c+1} (P1 may also seat {c, c+h});
            # skewed: {c, c+d} or {c, c+d, c+2d}.  Charter 90's TX-entry
            # span is sanctioned on its own below and passes here.
            window_ordinals = frozenset(
                int(by_bucket[bucket].ordinal) for bucket in window
            )
            # Charter 136 (五): a broken seat is completed back to the seat it
            # was, whatever today's shape label says.  Only a window holding
            # part of the chain whose every missing leg the chain already sent.
            seat_completion_window = bool(
                charter_136_on
                and chain_seat_buckets
                and chain_held_buckets
                and all(bucket in window for bucket in chain_held_buckets)
                and all(bucket in chain_seat_buckets for bucket in window)
                and any(
                    float(by_bucket[bucket].filled_shares)
                    + float(by_bucket[bucket].pending_shares) <= EPS
                    for bucket in window
                )
            )
            if (
                window_ordinals not in allowed_shapes
                and window_ordinals != taf_entry_window
            ):
                if not seat_completion_window:
                    shape_refused += 1
                    continue
                shape_exempt_seat_completion += 1
            # The market-lean gate: the constants block up top carries the
            # measurement that earned it (94%/82% directional realisation
            # in the bridge's dead zone).  Incumbent exempt -- keeping what
            # we hold is not a lean.  In shadow it counts and stands aside.
            if (
                MARKET_LEAN_GATE_MODE != "off"
                and not is_incumbent
                and market_lean_value is not None
                and model_mean_ordinal_value is not None
            ):
                window_mid = (
                    int(by_bucket[window[0]].ordinal)
                    + int(by_bucket[window[-1]].ordinal)
                ) / 2.0
                if market_lean_opposes(
                    market_lean_value,
                    window_mid_ordinal=window_mid,
                    model_mean_ordinal=model_mean_ordinal_value,
                ):
                    if len(market_lean_refused_windows) < MARKET_LEAN_REFUSAL_RECORD_MAX:
                        market_lean_refused_windows.append({
                            "buckets": list(window),
                            "window_mid_ordinal": window_mid,
                            "model_mean_ordinal": model_mean_ordinal_value,
                            "lean": market_lean_value,
                            "enforced": MARKET_LEAN_GATE_MODE == "on",
                        })
                    if MARKET_LEAN_GATE_MODE == "on":
                        market_lean_refused += 1
                        continue
                    market_lean_shadow_refused += 1
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
                _leg_below_floor(
                    by_bucket[bucket],
                    probabilities.get(bucket),
                    fee_rate=float(fee_rate),
                )
                for bucket, quote in zip(window, quotes)
            ):
                leg_floor_hit = True
                priced = False
                edge = None
            # Second, the hurdle (charter 130): the window must return
            # ECONOMIC_HURDLE_V1 on its all-in cost, the same pass mark the
            # execution layer applies to the lifted bundle.  Kept under the
            # name ``friction`` -- it is the dollars the edge must clear --
            # so the refusal audit and the ledger read the column they did.
            friction = economic_hurdle_usdc(price) if priced else 0.0
            # Charter 78 (user ruling, 2026-09-04): the incumbent has no
            # exemption from the whole-basket positive-EV gate either.
            # It used to be waived past it on the reasoning that the
            # seed admits it on evidence; measured 2026-09-04 that
            # waiver was the door for 435 of 459 negative-edge P0
            # openers (95%).  "Keep what we hold" is the baseline
            # candidate (charter 37), not a seat the price refuses.
            admissible = (
                priced
                and edge is not None
                and clears_economic_hurdle_v1(
                    expected_payout=effective_win, cost=price,
                )
            ) or (
                # Charter 78 off: the pre-2026-09-04 waiver, where an
                # incumbent window was admissible whatever the price.
                CHARTER_78_INCUMBENT_NO_EXEMPTION == "off"
                and is_incumbent
            )
            if convergence is not None and convergence["converging"] and priced:
                # Counted here, past the structural gates, so a candidate
                # is a window the price gate actually judged.  What the
                # lift changes at that gate: the edge without it and with
                # it, on the same friction.  Journaled in both modes so the
                # shadow and the arm read one column.
                convergence_candidates += 1
                _gate_lam = gate_lambda if gate_lambda is not None else 1.0
                unlifted_edge = (
                    raw_win * claim_factor / convergence_lift * _gate_lam - price
                )
                lifted_edge = (
                    raw_win * claim_factor / convergence_lift
                    * convergence_shadow_lift * _gate_lam - price
                )
                # The shadow asks the gate's own question (charter 130):
                # would the lifted claim clear the hurdle where the
                # unlifted one does not.
                flips = (
                    not clears_economic_hurdle_v1(
                        expected_payout=unlifted_edge + price, cost=price,
                    )
                    and clears_economic_hurdle_v1(
                        expected_payout=lifted_edge + price, cost=price,
                    )
                )
                if flips:
                    convergence_would_admit += 1
                    if convergence_lift != 1.0:
                        convergence_admitted_by_lift += 1
                if len(convergence_rows) < 8:
                    convergence_rows.append({
                        "buckets": list(window),
                        "claim": round(float(raw_win), 4),
                        "all_in_price": round(float(price), 4),
                        "agreement": round(float(convergence["agreement"]), 3),
                        "claim_rise": convergence["claim_rise"],
                        "price_fall": convergence["price_fall"],
                        "unlifted_edge": round(float(unlifted_edge), 4),
                        "lifted_edge": round(float(lifted_edge), 4),
                        "flips_gate": bool(flips),
                    })
            # Charter 84 (八): a window the book already holds part of is
            # admitted on what completing it would buy, not on the whole
            # window re-priced today.  The held rungs are sunk -- LTAC 32°C
            # at 0.59 against a claim of 0.40 is not a price anyone pays
            # again -- and a missing rung the market prices under the leg
            # floor is dead, not a refusal of its neighbours: it stays
            # unbought and the completion covers the rest.  The increment's
            # claim, its all-in offer and its friction are the three
            # quantities the whole-window rule reads, over the rungs being
            # bought.
            completion = _completion_increment(
                window, quotes=quotes,
                claim_factor=claim_factor * (
                    gate_lambda if gate_lambda is not None else 1.0
                ),
            )
            # Charter 90: the TX-entry seat is bought on TAF authority --
            # information the served vector does not carry -- so the
            # increment is not judged on the model's claim for the TX rung;
            # the whole-basket gate above still has to have passed.
            if (
                completion is not None
                and taf_entry_window is not None
                and frozenset(int(by_bucket[b].ordinal) for b in window)
                == taf_entry_window
            ):
                completion = dict(completion, taf_tx_entry=True,
                                  admissible=bool(admissible))
            if completion is not None:
                leg_floor_hit = False
                admissible = bool(completion["admissible"])
                if gate_lambda is not None and not admissible:
                    # Charter 87's count covers completions too: the
                    # increment that cleared its offer on the lifted claim
                    # and fell to lambda.
                    pre_lambda = _completion_increment(
                        window, quotes=quotes, claim_factor=claim_factor,
                    )
                    if pre_lambda is not None and bool(pre_lambda["admissible"]):
                        lambda_refused += 1
            if VALUATION_MODE == "on":
                if not valuation_authorized:
                    # A stage the valuation lane cannot vouch for holds: no
                    # fit, an accumulating fit, an incomplete book, or no
                    # admitted walk-forward verdict.  No fallback.
                    if admissible:
                        valuation_hold_refused += 1
                    admissible = False
                elif admissible:
                    if completion is not None and completion.get("admitted_by") in (
                        COMPLETION_RISK_BUDGET_RULE_VERSION,
                        COMPLETION_143_RULE_VERSION,
                    ):
                        # Charters 136/143: admitted on the exposure or the
                        # held count, not on value.
                        _clears = True
                    elif completion is not None:
                        _inc_price = completion.get("increment_all_in_price")
                        _inc = (
                            valuation_action_value_v1(
                                valuation_draws,
                                {b: 1.0 for b in completion["buyable_buckets"]},
                                float(_inc_price),
                            )
                            if _inc_price is not None else None
                        )
                        _clears = _inc is not None and (
                            float(_inc["value_lcb"])
                            >= economic_hurdle_usdc(float(_inc_price)) - EPS
                        )
                    else:
                        _clears = bool(valuation_admissible)
                    if not _clears:
                        valuation_refused += 1
                        admissible = False
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
                    # Charter 87: cleared the pre-lambda price test and
                    # fell to lambda -- the seats the ruling removes.
                    if (
                        gate_lambda is not None
                        and clears_economic_hurdle_v1(
                            expected_payout=win, cost=price,
                        )
                    ):
                        lambda_refused += 1
                    if edge is not None and (
                        best_refused is None
                        or float(edge) > float(best_refused["edge"])
                    ):
                        best_refused = {
                            "buckets": list(window),
                            "edge": float(edge),
                            "win_probability": float(win),
                            "effective_win_probability": float(effective_win),
                            "effective_edge_lambda": gate_lambda,
                            "effective_edge_agreement": gate_agreement,
                            "effective_edge_lambda_source": gate_lambda_source,
                            "all_in_cost": float(price),
                            "friction": float(friction),
                        }
                if not is_incumbent:
                    refused_priced_windows.append({
                        "buckets": list(window),
                        "win_probability": win,
                        "raw_win_probability": raw_win,
                        "all_in_price": price if priced else None,
                        "effective_edge_agreement": gate_agreement,
                        "admissible": False,
                        "center_gap_buckets": center_gap,
                        "leg_claim_cost": {
                            str(bucket): [
                                float(probabilities.get(bucket, 0.0) or 0.0),
                                float(quote),
                            ]
                            for bucket, quote in zip(window, quotes)
                            if quote is not None
                        },
                        "beta_market_mid": beta_market_mid,
                        "beta_hours_to_cutoff": hours_to_cutoff,
                    })
                    continue
                # Charter 78: the refused incumbent is not seated, but it
                # stays on the list flagged inadmissible so the solver has
                # a window to hold on (the baseline is the only candidate
                # it is offered) instead of failing the compile outright.
            else:
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
            if beta_admissible is not None and BETA_EDGE_MODE == "shadow":
                if admissible and not beta_admissible:
                    beta_would_refuse += 1
                elif (not admissible) and beta_admissible:
                    beta_would_admit += 1
            if beta_v2_admissible is not None:
                if admissible and not beta_v2_admissible:
                    beta_v2_would_refuse += 1
                elif (not admissible) and beta_v2_admissible:
                    beta_v2_would_admit += 1
            if valuation_admissible is not None and VALUATION_MODE == "shadow":
                if admissible and not valuation_admissible:
                    valuation_would_refuse += 1
                elif (not admissible) and valuation_admissible:
                    valuation_would_admit += 1
            if posterior_admissible is not None and POSTERIOR_MODE == "shadow":
                if admissible and not posterior_admissible:
                    posterior_would_refuse += 1
                elif (not admissible) and posterior_admissible:
                    posterior_would_admit += 1
            windows.append({
                "buckets": window,
                "win_probability": win,
                # Charter 87: the belief the gate judged and the solver
                # sizes on, and the lambda it was read through (None when
                # the gate did not apply one: arm off, unpriced, a stage
                # without a table, or the bucket-scope arm).
                "effective_win_probability": effective_win,
                "effective_edge_lambda": gate_lambda,
                "effective_edge_agreement": gate_agreement,
                "effective_edge_lambda_source": gate_lambda_source,
                # Charter 124: the worst claim in the ambiguity ball and the
                # edge the gate would read on it.  ``None`` when the reading
                # could not be taken (arm off, unpriced, no claim), which a
                # reader must be able to tell from "it passed".  Under
                # ``shadow`` these three decide nothing; under ``on`` the
                # deflation is already inside the numbers above.
                "robust_claim": robust_claim,
                "robust_claim_factor": robust_factor,
                "robust_edge": robust_edge,
                # Charter 128: the beta belief and its verdict.  ``None``
                # when the lane is off, the window unpriced, or (shadow) the
                # stage has no fit yet; under ``on`` the belief above IS
                # ``beta_belief`` and ``effective_edge_lambda`` is the ratio
                # that reproduces it in the solver's partition.
                "beta_lcb": beta_lcb,
                "beta_point": beta_point,
                "beta_belief": beta_belief,
                "beta_edge": beta_edge,
                "beta_admissible": beta_admissible,
                "beta_table_sha256": beta_sha,
                # v2 basis: the market's belief the disagreement was read
                # against (the ledger copies it onto the candidate row, and
                # the fit regresses on it), and the band row that served
                # (None when the stage's whole row did).
                "beta_market_mid": beta_market_mid,
                "beta_lead_band": beta_band,
                "beta_hours_to_cutoff": hours_to_cutoff,
                # Charter 132: the same slope as a prediction on the joint
                # draws.  A net edge per unit of cost, not a probability.
                "beta_v2_edge_point": (
                    None if beta_v2 is None else beta_v2["edge_point"]
                ),
                "beta_v2_edge_lcb": (
                    None if beta_v2 is None else beta_v2["edge_lcb"]
                ),
                "beta_v2_admissible": beta_v2_admissible,
                # Charter 132: the pooled valuation of this window as an
                # action.  ``valuation_mass`` is a probability (a sum over the
                # snapshot's one vector); the edges are net values per unit;
                # the capacity is the equal shares per leg the ask ladder can
                # take while the lower bound still clears the hurdle.
                "valuation_mass": valuation_mass,
                "valuation_market_mass": valuation_market_mass,
                "valuation_edge_point": valuation_edge_point,
                "valuation_edge_lcb": valuation_edge_lcb,
                "valuation_admissible": valuation_admissible,
                # L4 posterior lane (shadow): the belief the baseline's gate
                # would read, its lower-bound net edge and its verdict.
                "posterior_mass": posterior_mass,
                "posterior_market_mass": posterior_market_mass,
                "posterior_edge_point": posterior_edge_point,
                "posterior_edge_lcb": posterior_edge_lcb,
                "posterior_admissible": posterior_admissible,
                "valuation_capacity_shares": (
                    None if valuation_capacity is None
                    else valuation_capacity["capacity_shares"]
                ),
                "valuation_capacity_cost": (
                    None if valuation_capacity is None
                    else valuation_capacity["capacity_cost"]
                ),
                # Audit only: h/K, and the marginal Δh/ΔK of what this
                # window adds over what the seat already holds.  ``kind``
                # separates an opening from a widening from a top-up that
                # only adds shares -- the last of which the criterion is
                # silent about, and which was 29 of 33 settled top-ups.
                "marginal_coverage": coverage,
                # Per-rung claim and all-in cost, for the bucket-scope
                # lambda fit (2026-09-09).  The window row already carries
                # the settled bucket downstream, so exploding this map is
                # the whole evidence a per-leg fit needs and it costs one
                # column rather than a second table and a second sweep.
                # Raw claim, matching ``win_probability`` beside it: the
                # deflations are what the fit is measuring, so a row that
                # arrived pre-deflated could only measure itself.
                "leg_claim_cost": {
                    str(bucket): [
                        float(probabilities.get(bucket, 0.0) or 0.0),
                        float(quote),
                    ]
                    for bucket, quote in zip(window, quotes)
                    if quote is not None
                },
                "raw_win_probability": raw_win,
                "off_center_claim_factor": claim_factor,
                # Charter 81's own component of the line above, kept apart so
                # the sizing lane can swap it without re-deriving the rest.
                "center_window_claim_factor": center_lift,
                # Arm B: the lift applied (1.0 in shadow) and the state.
                "convergence_claim_factor": convergence_lift,
                "convergence": convergence,
                "center_gap_buckets": center_gap,
                "holds_probability_core": holds_core,
                "all_in_price": price if priced else None,
                "edge": edge,
                "log_growth_rank": kl,
                "is_incumbent": is_incumbent,
                "admissible": bool(admissible),
                **({"completion": completion} if completion is not None else {}),
            })
    # Charter 131 / charter 78: the held window reaches the list whatever
    # refused it.  Enumeration can miss it outright (bounded to contiguous
    # runs of at most SEAT_MAX_BUCKETS; an inherited support may be neither)
    # or a structural gate -- centre, length, shape -- can turn it away in
    # the loop, and either way the solver still needs a window to hold on.
    # A structurally refused incumbent is appended INADMISSIBLE and is not
    # counted again: the gate that refused it already did.
    structurally_refused = incumbent in seen
    if not any(row.get("is_incumbent") for row in windows):
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
        incumbent_leg_floor_hit = False
        if priced and any(
            _leg_below_floor(
                by_bucket[bucket],
                probabilities.get(bucket),
                fee_rate=float(fee_rate),
            )
            for bucket, quote in zip(incumbent, quotes)
        ):
            incumbent_leg_floor_hit = True
            priced = False
        price = sum(quote for quote in quotes if quote is not None)
        win = sum(float(probabilities[bucket]) for bucket in incumbent)
        # Charter 78: the appended incumbent passes the same gate the
        # loop applies; an incumbent the price refuses is counted as a
        # refusal, not carried through.
        incumbent_friction = economic_hurdle_usdc(price) if priced else 0.0
        # Charter 87: the same belief as the loop's gate.  An inherited
        # support is not lifted (it never was), but it is deflated by the
        # lambda its own agreement earns.
        incumbent_lambda = None
        incumbent_agreement = None
        incumbent_lambda_source = None
        if (
            CHARTER_87_GATE_EFFECTIVE_EDGE == "on"
            and EFFECTIVE_EDGE_MODE == "on"
            and EFFECTIVE_EDGE_SCOPE == "window"
            and str(probability_stage or "") in _EFFECTIVE_EDGE_GATE_STAGES
            and priced
            and win > EPS
        ):
            incumbent_agreement = price / win
            # Charter 97: the incumbent reads the same two axes as the loop.
            incumbent_lambda, incumbent_lambda_source = (
                effective_edge_lambda_resolved(
                    str(probability_stage or ""),
                    claim=win,
                    price=price,
                    tables=effective_edge_tables,
                )
            )
            incumbent_lambda = float(incumbent_lambda)
        incumbent_effective_win = (
            min(1.0 - EPS, win * incumbent_lambda)
            if incumbent_lambda is not None else win
        )
        incumbent_admissible = (
            priced
            and not structurally_refused
            and clears_economic_hurdle_v1(
                expected_payout=incumbent_effective_win, cost=price,
            )
        )
        if (
            priced
            and not structurally_refused
            and incumbent_lambda is not None
            and not incumbent_admissible
            and clears_economic_hurdle_v1(expected_payout=win, cost=price)
        ):
            lambda_refused += 1
        # Charter 84 (八): the increment is recorded for the audit, but an
        # incumbent that failed the enumeration failed it on the ladder's
        # own rules -- centre, width, skew (clauses 49/50) -- and a
        # completion does not waive those; only a window the loop admitted
        # is completed.
        incumbent_completion = _completion_increment(
            incumbent, quotes=quotes,
            claim_factor=(
                incumbent_lambda if incumbent_lambda is not None else 1.0
            ),
        )
        if not incumbent_admissible and not structurally_refused:
            if incumbent_leg_floor_hit:
                # The floor de-priced it; booking that as "no offer" reads
                # as a venue with nothing to sell when the venue is selling
                # pennies.  The loop's branch has kept the two apart since
                # 2026-08-20; this one had the same arithmetic and only the
                # one counter, and it is the branch every HELD chain takes --
                # so the mis-attribution landed precisely on the population
                # a reader consults to ask why a chain stopped adding.
                # Audit only: the window is refused either way.
                leg_floor_refused += 1
            elif not priced:
                unpriced_refused += 1
            else:
                edge_refused += 1
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
            "effective_win_probability": incumbent_effective_win,
            "effective_edge_lambda": incumbent_lambda,
            "effective_edge_agreement": incumbent_agreement,
            "effective_edge_lambda_source": incumbent_lambda_source,
            # Arm B: an inherited support is never lifted; keys kept so every
            # row reads alike.
            "convergence_claim_factor": 1.0,
            "convergence": None,
            # Charter 128 v2: the appended incumbent is not judged by the
            # beta lane (it never was); the clock rides so the ledger's copy
            # of the row reads like every other.
            "beta_market_mid": None,
            "beta_lead_band": None,
            "beta_hours_to_cutoff": hours_to_cutoff,
            "holds_probability_core": bool(core) and bool(set(incumbent) & core),
            "all_in_price": price if priced else None,
            "edge": (incumbent_effective_win - price) if priced else None,
            "log_growth_rank": incumbent_kl,
            "is_incumbent": True,
            "admissible": bool(incumbent_admissible),
            "structurally_refused": structurally_refused,
            **(
                {"completion": incumbent_completion}
                if incumbent_completion is not None else {}
            ),
        })
    # Rank by EFFECTIVE EDGE, lambda(a) * p - C (user ruling 2026-09-12
    # evening: "不应该是有效质量，改为有效 edge").  Charter 64 retired
    # edge-based ranking on 2026-08-17/22 because, over a wide domain, any
    # ranking on belief-minus-price is maximised where model and market
    # disagree most -- measured to be where this model is worst (158 seats:
    # the divergence chose the cheaper flank 96% of the time, the truth
    # landed on the mirror 83%).  Charter 131 removed the domain that
    # objection lived on: the shape is fixed by the day's reading, and the
    # only comparison left is a window against its own sub-window, so the
    # ranking now answers one question -- does the extra rung carry edge of
    # its own?  Under an equal lambda the difference between the two edges
    # IS the added rung's lambda(a) * p - c.  Ranking by effective mass
    # (this morning's reading) always took the wider window; ranking by
    # effective edge takes it only when the rung pays.  Measured on the
    # settled book's 3-rung/2-rung pairs that both clear the hurdle: 16 P0
    # pairs differ on 7 (+3.6% -> +13.2%), 15 P1 pairs differ on 11
    # (-10.1% -> -11.5%); too few to rule on, so the ledger measures it.
    # Unpriced windows carry no edge and sort last anyway; inadmissible
    # rows sort behind every admissible one (charter 78's baseline).
    windows.sort(
        key=lambda row: (
            not row.get("admissible", True),
            row["all_in_price"] is None,
            -float(row["edge"] if row.get("edge") is not None else 0.0),
            len(row["buckets"]),
            row["buckets"],
        )
    )
    def _ordinal_set(row: Mapping[str, Any]) -> frozenset[int]:
        return frozenset(int(by_bucket[b].ordinal) for b in row["buckets"])
    def _sanction(rows: list, wanted: list) -> list:
        wanted_sets = [frozenset(w) for w in wanted]
        keep = [
            row for row in rows
            if _ordinal_set(row) in wanted_sets and row.get("admissible", True)
        ]
        rest = [
            dict(row, admissible=False) for row in rows
            if not (_ordinal_set(row) in wanted_sets and row.get("admissible", True))
        ]
        return keep + rest
    def _tail_windows(centre: int, direction: int) -> list:
        if direction > 0:
            return [{centre, centre + 1, centre + 2}, {centre, centre + 1, centre + 2, centre + 3}]
        if direction < 0:
            return [{centre - 2, centre - 1, centre}, {centre - 3, centre - 2, centre - 1, centre}]
        return [{centre - 1, centre, centre + 1}]
    # Charter 131 / Q7: a held window whose shape the ruling no longer allows
    # (it reached the list past the loop's gates, as every incumbent does)
    # is held, not bought -- it stays listed for the solver's baseline and
    # is never a seat.
    windows = [
        dict(row, admissible=False, shape_retired=True)
        if (
            row.get("is_incumbent")
            and row.get("admissible", True)
            and _ordinal_set(row) not in allowed_shapes
            and _ordinal_set(row) != taf_entry_window
        )
        else row
        for row in windows
    ]
    if taf_entry_required:
        if taf_entry_window is None:
            windows = [dict(row, admissible=False) for row in windows]
        else:
            windows = _sanction(windows, [taf_entry_window])
            windows = [
                dict(row, taf_tx_entry=True)
                if _ordinal_set(row) == taf_entry_window and row.get("admissible", True)
                else row
                for row in windows
            ]
    p0m_taf_audit: dict[str, Any] = {}
    if (
        P0M_TAF_MODE != "off"
        and str(probability_stage or "") == "P0-M"
        and taf_tx_bucket in by_bucket
        and ladder_center in by_bucket
    ):
        centre_ordinal = int(by_bucket[ladder_center].ordinal)
        tx_ordinal = int(by_bucket[taf_tx_bucket].ordinal)
        taf_direction = 0 if abs(tx_ordinal - centre_ordinal) < 1 else (
            1 if tx_ordinal > centre_ordinal else -1
        )
        raw_skew = _skew_extension_sign(
            probabilities,
            {bucket: int(row.ordinal) for bucket, row in by_bucket.items()},
        )
        disagree = bool(taf_direction and raw_skew and taf_direction != raw_skew)
        if P0M_TAF_MODE == "tx_anchor":
            # The 2026-09-04 research arm: every seat holds the TX bucket,
            # held or not.  Charter 90's ``tx_envelope`` is applied above.
            windows = [
                row for row in windows
                if taf_tx_bucket in row["buckets"] and row.get("admissible", True)
            ] + [
                dict(row, admissible=False) for row in windows
                if not (taf_tx_bucket in row["buckets"] and row.get("admissible", True))
            ]
        elif P0M_TAF_MODE == "confirm_hold" and disagree:
            windows = [dict(row, admissible=False) for row in windows]
        elif P0M_TAF_MODE == "confirm_override" and disagree:
            windows = _sanction(windows, _tail_windows(centre_ordinal, taf_direction))
        p0m_taf_audit = {
            "mode": P0M_TAF_MODE, "tx_bucket": taf_tx_bucket,
            "taf_direction": taf_direction, "skew": raw_skew, "disagree": disagree,
            "envelope_rule": P0M_TAF_ENVELOPE_RULE_VERSION,
            "held_buckets": taf_held_buckets,
            "confirmed": taf_confirmed,
            "entry_required": taf_entry_required,
            # Charter 131: nothing held, nothing to confirm or enter.
            "no_p0_basket": taf_no_p0_basket,
            "entry_window": (
                None if taf_entry_window is None else sorted(
                    (b for b in by_bucket if int(by_bucket[b].ordinal) in taf_entry_window),
                    key=lambda b: int(by_bucket[b].ordinal),
                )
            ),
            "tx_unreachable": taf_unreachable,
            "seat_holds_tx": any(
                taf_tx_bucket in row["buckets"] and row.get("admissible", True)
                for row in windows
            ),
        }
    audit = {
        # Charter 140: evidence only.  Read by the compile into
        # ``delta_v2_audit.candidate_windows`` and left out of the plan's
        # support selection, which spreads this audit.
        "refused_priced_windows": refused_priced_windows,
        # Charter 131: the shape the seat was confined to, and why.
        "seat_shape": seat_shape_audit,
        "seat_shape_rule": SEAT_SHAPE_RULE_VERSION,
        "seat_min_buckets": min_size,
        "seat_max_buckets": max_size,
        "p0m_taf": p0m_taf_audit,
        "probability_core": sorted(
            core, key=lambda bucket: (by_bucket[bucket].ordinal, bucket)
        ),
        "probability_core_mass": PROBABILITY_CORE_MASS,
        "ladder_center_bucket": ladder_center,
        "center_refused_window_count": center_refused,
        "minimum_support_buckets": min_size,
        "policy_minimum_support_buckets": policy_min,
        "short_refused_window_count": short_refused,
        "shape_refused_window_count": shape_refused,
        # Charter 136 (五): broken seats completed back to themselves past a
        # shape label that no longer names them.
        "shape_exempt_seat_completion_count": shape_exempt_seat_completion,
        "market_lean_refused_window_count": market_lean_refused,
        # Shadow bookkeeping: what the gate WOULD have refused, plus the
        # signal itself so a receipt shows the lean the decision saw.
        "market_lean_shadow_refused_window_count": (
            market_lean_shadow_refused
        ),
        "market_lean_ordinal": market_lean_value,
        "market_lean_gate_mode": MARKET_LEAN_GATE_MODE,
        "skew_extension_sign": skew_sign,
        "leg_floor_refused_window_count": leg_floor_refused,
        "leg_floor_excluded_buckets": leg_floor_excluded,
        # Charter 125: which rungs the day's observed high removed from the
        # domain, and the number it was read against.  A gate whose refusals
        # are not written down cannot be priced afterwards -- the same
        # lesson the market-lean list above was added for.
        "observed_high_floor_mode": CHARTER_125_OBSERVED_HIGH_FLOOR,
        "observed_high_running_c": snapshot.evidence.observed_running_high_c,
        "observed_high_dead_buckets": observed_dead_admitted,
        "gapped_window_refused_count": gapped_refused,
        # The one gate here that is actually the market's doing: the window
        # was whole, every leg carried a real quote, and the model's own edge
        # over the all-in price did not clear tick-rounding across the legs.
        "edge_refused_window_count": edge_refused,
        # The basket the objective wanted and the price would not give it:
        # of the windows the edge gate refused, the one whose edge came
        # closest to clearing friction.  Absent when no window reached that
        # gate, which is itself the answer -- the refusal was structural,
        # and there was no target to miss.
        "edge_best_refused_window": best_refused,
        # Charter 87: the gate reads the lifted claim times lambda(a).  This
        # counts the windows that cleared the pre-lambda price test and
        # fell to lambda -- the seats the ruling removes.
        "gate_reads_effective_edge": CHARTER_87_GATE_EFFECTIVE_EDGE,
        # Charter 128: the beta lane's mode, what it served per stage, and
        # -- in shadow -- how many windows it would have moved across the
        # gate.  The per-window readings are on each row.
        "beta_edge_mode": BETA_EDGE_MODE,
        "beta_edge_rule": BETA_EDGE_RULE_VERSION,
        # v2 basis: what the disagreement was measured against, the clock
        # the band was read off, and whether the book gave a belief at all.
        "beta_price_basis": BETA_PRICE_BASIS,
        "beta_hours_to_cutoff": hours_to_cutoff,
        "beta_lead_band": beta_lead_band_label,
        "beta_market_state": beta_market_state,
        "beta_served": {
            str(scope): (
                None if not isinstance(row, dict) else {
                    "beta": row.get("beta"), "beta_lcb": row.get("beta_lcb"),
                    "sha256": row.get("sha256"),
                    "accumulating": row.get("accumulating"),
                    "lead_bands": {
                        str(label): {
                            "beta_lcb": band.get("beta_lcb"),
                            "accumulating": band.get("accumulating"),
                        }
                        for label, band in dict(row.get("lead_bands") or {}).items()
                        if isinstance(band, dict)
                    },
                }
            )
            for scope, row in dict(beta_served).items()
        },
        "beta_would_refuse_count": beta_would_refuse,
        "beta_would_admit_count": beta_would_admit,
        "beta_v2_would_refuse_count": beta_v2_would_refuse,
        "beta_v2_would_admit_count": beta_v2_would_admit,
        # Charter 132: the valuation lane's header.  ``valuation_state`` says
        # why the values below are or are not there: ``admitted`` (served and
        # verified), ``unverified_*`` (computed, no admitted verdict yet),
        # ``accumulating``, ``no_fit``, ``market_*`` (the book did not describe
        # the whole market), ``off``.
        "valuation_mode": VALUATION_MODE,
        "valuation_rule": VALUATION_RULE,
        "valuation_market_reference_rule": VALUATION_MARKET_REFERENCE_RULE,
        "valuation_state": valuation_state,
        "valuation_served": (
            None if not isinstance(valuation_row, Mapping) else {
                "beta": valuation_row.get("beta"),
                "beta_lcb": valuation_row.get("beta_lcb"),
                "beta_ucb": valuation_row.get("beta_ucb"),
                "accumulating": valuation_row.get("accumulating"),
                "verdict": valuation_row.get("verdict"),
                "sha256": valuation_row.get("sha256"),
            }
        ),
        "valuation_market": (
            None if valuation_market is None else {
                "state": valuation_market.get("state"),
                "reason": valuation_market.get("reason"),
                "mid_sum": valuation_market.get("mid_sum"),
                "max_spread": valuation_market.get("max_spread"),
                "one_sided_buckets": valuation_market.get("one_sided_buckets"),
            }
        ),
        # L4 posterior lane (shadow): what the snapshot read and what the
        # gate would have done.  ``posterior_state``: ``shadow`` (computed
        # and recorded), ``accumulating``, ``no_fit``, ``market_*``,
        # ``no_vector``, ``error_*``, ``off``.
        "posterior_mode": POSTERIOR_MODE,
        "posterior_rule": POSTERIOR_RULE,
        "posterior_state": posterior_state,
        "posterior_lead_band": posterior_band,
        "posterior_served": (
            None if not isinstance(posterior_row, Mapping) else {
                key: posterior_row.get(key)
                for key in ("alpha", "gamma", "w", "w_lcb", "w_ucb",
                            "accumulating", "sha256", "lead_band")
            }
        ),
        "posterior_would_refuse_count": posterior_would_refuse,
        "posterior_would_admit_count": posterior_would_admit,
        "valuation_would_refuse_count": valuation_would_refuse,
        "valuation_would_admit_count": valuation_would_admit,
        "valuation_refused_window_count": valuation_refused,
        "valuation_hold_refused_window_count": valuation_hold_refused,
        # Charter 97: which axis served lambda, and how wide the thin band
        # is.  The per-window source is on each row; this is the audit's
        # header so a plan says whether the second axis was live at all.
        "claim_edge_thermometer": CLAIM_EDGE_THERMOMETER_MODE,
        "claim_edge_thin_band_max": CLAIM_EDGE_THIN_BAND_MAX,
        "effective_edge_gate_refused_window_count": lambda_refused,
        "unpriced_refused_window_count": unpriced_refused,
        # The refusals themselves, so this gate can be priced on its own
        # record instead of on the replay that promoted it.
        "market_lean_refused_windows": market_lean_refused_windows,
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
            shape=shape_refused,
            market_lean=market_lean_refused,
            leg_floor=leg_floor_refused,
            unpriced=unpriced_refused,
            edge=edge_refused,
        ),
        "minimum_leg_all_in_ask": MINIMUM_LEG_ALL_IN_ASK,
        # Charter 131 (Q8): the flat floor read at the mid; rule and basis
        # ride here so a receipt proves which floor refused what.
        "leg_floor_rule": LEG_FLOOR_RULE_VERSION,
        "leg_floor_price_basis": LEG_FLOOR_PRICE_BASIS,
        "center_window_claim_bands": [
            [bound, factor] for bound, factor in CENTER_WINDOW_CLAIM_BANDS
        ],
        # Arm B (2026-09-06): the convergence claim lift's shadow receipt.
        "convergence_claim": {
            "rule": _kernel_delta_v2.CONVERGENCE_CLAIM_RULE_VERSION,
            "mode": _kernel_delta_v2.CONVERGENCE_CLAIM_MODE,
            "factor": _kernel_delta_v2.CONVERGENCE_CLAIM_FACTOR,
            "horizon_hours": _kernel_delta_v2.CONVERGENCE_CLAIM_HORIZON_HOURS,
            "evidence_present": bool(convergence_evidence),
            "prior_stage": (
                str(convergence_evidence.get("prior_stage"))
                if convergence_evidence else None
            ),
            "prior_decision_at_utc": (
                str(convergence_evidence.get("prior_decision_at_utc"))
                if convergence_evidence else None
            ),
            "candidate_count": convergence_candidates,
            "would_admit_count": convergence_would_admit,
            "admitted_by_lift_count": convergence_admitted_by_lift,
            "candidates": convergence_rows,
        },
        **(
            {"sizing_claim_bands": [
                [bound, factor] for bound, factor in SIZING_CLAIM_BANDS
            ]}
            if SIZING_CLAIM_BANDS else {}
        ),
        # Retained name from the touch-the-core era so existing readers keep
        # working; it now counts refusals under the stronger rung.
        "core_refused_window_count": center_refused,
    }
    return windows, audit


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


def _evidence_window_sum(table: Any, window: Sequence[str]) -> float | None:
    """Sum a per-bucket evidence table over a window; None if any bucket is absent."""

    if not table:
        return None
    total = 0.0
    for bucket in window:
        value = table.get(str(bucket)) if hasattr(table, "get") else None
        if value is None:
            return None
        total += float(value)
    return total


def window_gate_counts_v1(window_audit: Mapping[str, Any]) -> dict[str, Any]:
    """The enumeration funnel, keyed by the gate names the page speaks.

    Read off ``_support_windows``' audit.  Attached to the two errors the
    solver raises when the window list is empty or nothing on it solves
    (2026-09-05): until then a blocked compile recorded only the error's
    name, so a day whose seed window a structural gate threw out and whose
    every other shape the price refused was indistinguishable from a
    solver fault -- 28 P0 station-days in 2026-09-02..06 on the ledger,
    and every ``delta_v2_support_empty`` blocked scope in production.
    """

    audit = dict(window_audit or {})

    def _count(key: str) -> int:
        try:
            return int(audit.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    best = audit.get("edge_best_refused_window")
    return {
        "enumerated": _count("enumerated_window_count"),
        "shortlisted": _count("shortlisted_window_count"),
        "refused": {
            "gapped": _count("gapped_window_refused_count"),
            "center": _count("center_refused_window_count"),
            "short": _count("short_refused_window_count"),
            "shape": _count("shape_refused_window_count"),
            "market_lean": _count("market_lean_refused_window_count"),
            "leg_floor": _count("leg_floor_refused_window_count"),
            "unpriced": _count("unpriced_refused_window_count"),
            "edge": _count("edge_refused_window_count"),
        },
        "last_refusing_gate": (
            str(audit.get("last_refusing_gate") or "") or None
        ),
        "edge_best_refused_window": (
            dict(best) if isinstance(best, Mapping) else None
        ),
        # Arm B: the convergence lift's shadow receipt rides the funnel.
        "convergence_claim": (
            dict(audit["convergence_claim"])
            if isinstance(audit.get("convergence_claim"), Mapping) else None
        ),
    }


def _support_window_error(
    reason: str,
    *,
    window_audit: Mapping[str, Any],
    failures: Sequence[Mapping[str, Any]] | None = None,
) -> ContractError:
    """The solver's empty-list error, carrying the funnel that emptied it.

    The message stays the bare reason -- every consumer compares it by
    string -- and the diagnosis rides as attributes: ``window_gate_counts``
    always, ``window_failures`` (one row per window whose solve threw)
    when there were windows and none solved.
    """

    error = ContractError(reason)
    error.window_gate_counts = window_gate_counts_v1(window_audit)
    if failures is not None:
        error.window_failures = [dict(row) for row in failures]
    return error


def _solve_over_support_windows(
    *,
    hours_to_cutoff: float | None = None,
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
    probability_identity: str | None = None,
    last_acted_probability_identity: str | None = None,
    chain_growth_frame_usdc: float | None = None,
    book_equity_usdc: float | None = None,
    last_decision_signature: Mapping[str, Any] | None = None,
    completion_capital_usdc: float | None = None,
    effective_edge_tables: Mapping[str, Any] | None = None,
    off_centre_claim_bands: Mapping[str, Any] | None = None,
    excluded_candidate_ids: Sequence[str] = (),
    completion_budget_usdc: float | None = None,
) -> tuple[tuple[str, ...], dict[str, Any], dict[str, Any]]:
    """Run the objective on each shortlisted window; the head one is seated.

    ``excluded_candidate_ids`` (charter 121) is passed straight to the kernel
    for every window.  The caller re-enters this function after charter 93
    (二)'s post-lift floor has refused a winner, and the refused row has to be
    out of the running in whichever window it is proposed in -- a window sweep
    that kept it would hand the same refused shape back and the retry would
    not terminate.

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
        raise _support_window_error(
            "delta_v2_support_empty", window_audit=window_audit,
        )
    # Charter 77: is this a decision moment at all?  The model changed
    # (a new served identity) or the day's admissibility changed; a
    # quote refresh that changed neither offers no buying candidate.
    edge_state = edge_state_v1(windows)
    last_signature = dict(last_decision_signature or {})
    # Charter 90: a TAF TX outside the held basket is new evidence the
    # served vector does not carry, so it is a decision moment of its own
    # until the TX leg is held.
    taf_tx_entry = bool(
        ((window_audit or {}).get("p0m_taf") or {}).get("entry_required")
    )
    # Charter 110(七): what the chain holds is the third arm.  Read off the
    # snapshot's FILLED shares alone -- pending is our own unfilled order,
    # and letting it in would make placing a rung a reason to re-solve.
    basket_holding = basket_holding_signature_v1({
        row.bucket: row.filled_shares for row in snapshot.buckets
    })
    order_ends = (
        str(last_signature["current_order_ends"])
        if last_signature.get("current_order_ends") is not None
        else None
    )
    decision_moment = CHARTER_77_DECISION_MOMENTS == "off" or taf_tx_entry or (
        is_decision_moment_v1(
            probability_identity=probability_identity,
            edge_state=edge_state,
            last_probability_identity=(
                str(last_signature.get("probability_identity") or "")
                or None
            ),
            last_edge_state=(
                str(last_signature.get("edge_state") or "") or None
            ),
            basket_holding=basket_holding,
            # An older decision recorded no holding.  Reading its absence as
            # "held nothing" would make the first cycle after this ships a
            # moment for every scope that holds anything, so the empty string
            # is what a signature written before the arm existed says, and
            # it equals the holding of a chain that is in fact empty.
            last_basket_holding=str(
                last_signature.get("basket_holding") or ""
            ),
            # Charter 135 (一): an order that ended unfilled since the last
            # recorded decision.  ``current_order_ends`` is what the store
            # holds now, attached by ``load_last_decision_signature``;
            # ``order_ends`` is what that decision recorded.  A caller with no
            # order store (the paper ledger's judge) attaches nothing and the
            # arm stays off.
            order_ends=order_ends,
            last_order_ends=(
                str(last_signature.get("order_ends") or "") or None
            ),
        )
    )
    decision_moment_audit = {
        "rule": DECISION_MOMENT_RULE_VERSION,
        "probability_identity": probability_identity,
        "edge_state": edge_state,
        "last_probability_identity": (
            last_signature.get("probability_identity") or None
        ),
        "last_edge_state": last_signature.get("edge_state") or None,
        "taf_tx_entry": taf_tx_entry,
        "basket_holding": basket_holding,
        "last_basket_holding": last_signature.get("basket_holding") or None,
        "order_end_rule": ORDER_END_MOMENT_RULE_VERSION,
        "order_ends": order_ends,
        "last_order_ends": last_signature.get("order_ends") or None,
        "is_decision_moment": bool(decision_moment),
    }
    scored: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    stage = str(snapshot.evidence.probability_stage or "")
    for row in shortlist:
        window = tuple(row["buckets"])
        claim_factor = float(row.get("off_center_claim_factor") or 1.0)
        # Bucket scope: one factor per rung, read off that rung's own
        # agreement.  Filled only when the arm is on; the scalar path
        # below is the shipped rule.
        bucket_factors: dict[str, float] = {}
        if (
            EFFECTIVE_EDGE_MODE == "on"
            and EFFECTIVE_EDGE_SCOPE == "bucket"
            and (
                stage in EFFECTIVE_EDGE_STAGES
                or stage in EFFECTIVE_EDGE_P0M_STAGES
                or stage in EFFECTIVE_EDGE_P1_STAGES
            )
        ):
            fee = float(basket_policy.fee_rate)
            rows_by_bucket = {
                str(item.bucket): item for item in snapshot.buckets
            }
            for bucket in window:
                claimed_leg = float(probabilities.get(bucket, 0.0) or 0.0)
                leg_row = rows_by_bucket.get(str(bucket))
                factor = claim_factor
                if (
                    leg_row is not None
                    and leg_row.ask_available
                    and claimed_leg > EPS
                ):
                    factor *= effective_edge_bucket_lambda_for_stage(
                        stage,
                        float(leg_row.ask_price) * (1.0 + fee) / claimed_leg,
                        # The store's bucket fit as of this decision, when one
                        # has been activated; None falls to the kernel
                        # constant, which is what every day before the lane
                        # existed reads.
                        bucket_table=(effective_edge_tables or {}).get(
                            BUCKET_TABLE_KEY
                        ),
                    )
                bucket_factors[str(bucket)] = factor
        # Charter 68: the same deflation machinery, carrying a second and
        # differently-motivated factor.  ``off_center_claim_factor`` corrects
        # a window for sitting off the centre; this one corrects every window
        # for the market's disagreement with it, which is measured to predict
        # what fraction of the claim arrives.  Multiplying them is right --
        # they are independent shrinkages of the same claim -- and putting it
        # here rather than at the solver is what keeps the ranking and the
        # sizing reading one belief: the block below rebuilds the whole
        # partition from ``claim_factor`` and hands it to both.
        # P1 now flows through the same dispatch with its own table (identity
        # today; see EFFECTIVE_EDGE_P1_LAMBDA), so the exemption is a fitted
        # row rather than an absent branch.
        if EFFECTIVE_EDGE_MODE == "on" and (
            stage in EFFECTIVE_EDGE_STAGES
            or stage in EFFECTIVE_EDGE_P0M_STAGES
            or stage in EFFECTIVE_EDGE_P1_STAGES
        ):
            gate_lambda = row.get("effective_edge_lambda")
            if gate_lambda is not None:
                # Charter 87: the gate looked lambda up at this window's
                # agreement and recorded it; the solver reads that value
                # rather than making a second lookup, so admission and
                # sizing cannot drift apart.
                claim_factor *= float(gate_lambda)
            else:
                priced = row.get("all_in_price")
                claimed = float(row.get("win_probability") or 0.0)
                if priced is not None and claimed > EPS:
                    # Charter 68(c): the served table is the store's
                    # rolling fit, handed in by the caller as of its own
                    # moment; the kernel constant is only the bootstrap
                    # before a scope's first activation.  Reached when the
                    # gate applied no lambda (charter 87 off, or a row the
                    # gate did not price).
                    claim_factor *= effective_edge_lambda_from_tables(
                        stage, float(priced) / claimed, effective_edge_tables
                    )
        # The partition is one kernel helper (charter 71): the same
        # deflated belief feeds the candidate generator, the curve score
        # and the chain-growth gate below, and the paper ledger reads the
        # identical function.
        window_probabilities = deflate_window_partition_v1(
            probabilities,
            window,
            bucket_factors=bucket_factors or None,
            claim_factor=claim_factor,
        )
        # The admission/sizing split (user ruling, 2026-09-06).  Charter 81's
        # lift is an admission knob and stays exactly where it is; the stake
        # may read the claim's own reliability curve instead.  Only that one
        # component is swapped -- the off-centre deflation and charter 68's
        # lambda(a), including the agreement it was looked up at, are the
        # admission lane's, so the arm isolates the one variable it names.
        # Empty table: this is ``None`` and the Kelly stake reads the same
        # belief as everything else, which is charter 68's "one belief".
        sizing_probabilities = None
        if SIZING_CLAIM_BANDS and float(row.get("center_gap_buckets") or 0.0) <= 0.0:
            seated_lift = float(row.get("center_window_claim_factor") or 1.0)
            if seated_lift > EPS:
                sizing_probabilities = deflate_window_partition_v1(
                    probabilities,
                    window,
                    bucket_factors=bucket_factors or None,
                    claim_factor=(
                        claim_factor / seated_lift
                        * sizing_claim_factor(
                            float(row.get("raw_win_probability") or 0.0)
                        )
                    ),
                )
        try:
            window_candidates = _target_candidates(
                    snapshot=snapshot,
                    decision_moment=(
                        decision_moment and bool(row.get("admissible", True))
                    ),
                    # Charter 90: the TX-entry seat may open the day at P0-M.
                    taf_tx_entry=taf_tx_entry and bool(row.get("taf_tx_entry")),
                    # Charter 143: the completion of a broken seat is not
                    # held back for a decision moment.
                    completion_143_admitted=bool(
                        row.get("admissible", True)
                        and (row.get("completion") or {}).get("admitted_by")
                        == COMPLETION_143_RULE_VERSION
                    ),
                    desired=window,
                    artifact=delta_policy,
                    capital_available_usdc=capital_available_usdc,
                    # Charter 136 (四): a completion the risk budget admitted
                    # is sized against that budget, the seat's own allowance.
                    completion_capital_usdc=(
                        max(
                            float(
                                completion_capital_usdc
                                if completion_capital_usdc is not None
                                else capital_available_usdc
                            ),
                            float(completion_budget_usdc),
                        )
                        if completion_budget_usdc is not None
                        and ((row.get("completion") or {}).get("admitted_by")
                             == COMPLETION_RISK_BUDGET_RULE_VERSION)
                        else completion_capital_usdc
                    ),
                    minimum_clip_notional_usdc=(
                        basket_policy.clip_min_notional_usdc
                    ),
                    bankroll_usdc=bankroll_usdc,
                    # The deflated belief, not the snapshot's: the generator
                    # must propose against the distribution the ranking on
                    # the next line scores.
                    probabilities_by_bucket=window_probabilities,
                    sizing_probabilities_by_bucket=sizing_probabilities,
                    hours_to_cutoff=hours_to_cutoff,
                    centre_bucket=str(
                        window_audit.get("ladder_center_bucket") or ""
                    ),
                    book_equity_usdc=(
                        book_equity_usdc
                        if objective_form == KELLY_OBJECTIVE_FORM
                        else None
                    ),
            )
        except ContractError as exc:
            failures.append({
                "buckets": list(window),
                "reason": f"ContractError:{exc}",
            })
            continue
        def _chain_gate_refusal(solution: Mapping[str, Any]) -> str | None:
            """The chain law on a solved window -- 66(A), 71(a), 71(b) -- as
            the reason it refuses, or ``None``.  A function so the solve can
            be asked again with a refused row out of the running."""

            # Charter 66 clause A.  Binds only when the plan spends new money
            # on a station that already holds -- a HOLD (target == inventory)
            # is never refused here, and an opening stage has no chain to
            # protect.  Buckets the window cannot touch still pay and still
            # cost, so they join the arithmetic before the division.
            target_shares = {
                str(bucket): float(quantity)
                for bucket, quantity in (
                    solution.get("target_shares_by_bucket") or {}
                ).items()
            }
            target_cost = {
                str(bucket): float(value)
                for bucket, value in (
                    solution.get("target_cost_by_bucket") or {}
                ).items()
            }
            inventory_cost = 0.0
            holds_anything = False
            for bucket_row in snapshot.buckets:
                inventory = (
                    float(bucket_row.filled_shares)
                    + float(bucket_row.pending_shares)
                )
                sunk_here = (
                    float(bucket_row.filled_cost_usdc)
                    + float(bucket_row.pending_notional_usdc)
                )
                inventory_cost += sunk_here
                if inventory > EPS:
                    holds_anything = True
                    if bucket_row.bucket not in target_shares:
                        target_shares[bucket_row.bucket] = inventory
                        target_cost[bucket_row.bucket] = sunk_here
            planned_total = sum(target_cost.values())
            # Charter 143: a completion by held count answers to its own rule;
            # the chain law on top-ups (66(A) par, 71(a) identity, 71(b) growth)
            # is the reading it replaces -- each refuses a completion the rule
            # was measured to want, on the same negative increment.
            completion_143_winner = (
                solution.get("winner_completion_rule") == COMPLETION_143_RULE_VERSION
            )
            if (
                CHARTER_66A_CHAIN_PAR == "on"
                and not completion_143_winner
                and holds_anything
                and planned_total > inventory_cost + EPS
            ):
                chain_cost = _chain_blended_cost(
                    shares_by_bucket=target_shares,
                    cost_total=planned_total,
                    probabilities=probabilities,
                )
                if chain_cost is None or chain_cost >= CHAIN_COST_PAR_BOUND:
                    return "chain_cost_at_par:%s" % (
                        "%.4f" % chain_cost
                        if chain_cost is not None else "no_expected_payout"
                    )
            # Charter 71 (user ruling, 2026-09-03): two hard gates on a
            # top-up, judged on the increment.  (a) The probability this
            # window is served must differ from the one the chain last
            # bought under -- the book moving is not a reason to buy.  (b)
            # The buy must improve the chain's expected log wealth read on
            # the charter-68 partition, lambda*p, from the held curve.  An
            # unknown last-acted identity cannot bind (a): the gate refuses
            # on evidence, never on its absence, and the audit names it.
            if (
                holds_anything
                and not completion_143_winner
                and planned_total > inventory_cost + EPS
            ):
                # Charter 90: the TX leg entering the basket is bought on TAF
                # evidence the vector does not carry; gate (a) yields to it.
                if (
                    CHARTER_71A_TOPUP_IDENTITY == "on"
                    and not bool(row.get("taf_tx_entry"))
                    and last_acted_probability_identity
                    and probability_identity
                    and str(last_acted_probability_identity)
                    == str(probability_identity)
                ):
                    return "topup_requires_probability_change"
                inventory_by_bucket: dict[str, float] = {}
                for bucket_row in snapshot.buckets:
                    inventory = (
                        float(bucket_row.filled_shares)
                        + float(bucket_row.pending_shares)
                    )
                    if inventory > EPS:
                        inventory_by_bucket[str(bucket_row.bucket)] = inventory
                bought_by_bucket: dict[str, float] = {}
                for bucket, quantity in target_shares.items():
                    increment = float(quantity) - inventory_by_bucket.get(
                        str(bucket), 0.0
                    )
                    if increment > EPS:
                        bought_by_bucket[str(bucket)] = increment
                # (b) binds on the scope's own frame -- the book's 2%, the
                # number the ledger frames it in -- which the caller knows
                # only when it knows the book.  Without it the gate cannot
                # bind, and the audit says so.
                if (
                    CHARTER_71B_CHAIN_GROWTH == "on"
                    and chain_growth_frame_usdc is not None
                    and float(chain_growth_frame_usdc) > 0.0
                ):
                    growth = chain_topup_growth_v1(
                        held=inventory_by_bucket,
                        bought=bought_by_bucket,
                        probabilities=window_probabilities,
                        spend=planned_total - inventory_cost,
                        bankroll=float(chain_growth_frame_usdc),
                    )
                    if not growth > 0.0:
                        return "no_chain_growth:%s" % (
                            "%.4f" % growth
                            if math.isfinite(growth) else "-inf"
                        )
            return None

        # Charter 143: the kernel seats a Kelly row ahead of the completion
        # (charter 72's law), and the chain law then judges the Kelly row as
        # the top-up it is.  Refused, it used to take the window down with
        # it -- the completion the rule had certified was never asked.  The
        # solve is asked again with the refused Kelly row out of the running,
        # once; a completion by held count answers to its own rule, and the
        # chain law is not put to it.
        local_excluded = tuple(excluded_candidate_ids)
        solution = None
        refused_reason: str | None = None
        while True:
            try:
                solution = solve_target_inventory_v2(
                probabilities=window_probabilities,
                feasible_inventory_candidates=window_candidates,
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
                # Charter 84 (八): a rung the market prices under the leg
                # floor cannot be bought, so the floor cannot demand it be
                # covered; the ruling that forbids the leg forbids the demand.
                below_leg_floor_buckets=tuple(
                    (row.get("completion") or {}).get("market_dead_buckets")
                    or ()
                ),
                excluded_candidate_ids=local_excluded,
                shortfall_buckets=tuple(sorted(shortfall & set(window))),
                breakthrough_buckets=tuple(
                    sorted(breakthrough & set(window))
                ),
                weights=delta_policy.value_weights,
                objective_form=objective_form,
                bankroll_usdc=bankroll_usdc,
                )
            except ContractError as exc:
                # One infeasible window -- no legal shape at these prices, or
                # a candidate lattice too wide -- must not deny the others
                # their comparison.  It is recorded, not silently dropped.
                refused_reason = f"ContractError:{exc}"
                solution = None
                break
            refused_reason = _chain_gate_refusal(solution)
            if refused_reason is None:
                break
            winner_id = str(solution.get("winner_candidate_id") or "")
            if (
                str(solution.get("winner_candidate_origin") or "")
                == KELLY_CANDIDATE_ORIGIN
                and (row.get("completion") or {}).get("admitted_by")
                == COMPLETION_143_RULE_VERSION
                and winner_id
                and winner_id not in local_excluded
            ):
                local_excluded += (winner_id,)
                continue
            solution = None
            break
        if solution is None:
            failures.append({
                "buckets": list(window),
                "reason": str(refused_reason or "unsolved"),
            })
            continue
        score = (
            solution["target_curve_score"]
            if objective_form in (CURVE_OBJECTIVE_FORM, KELLY_OBJECTIVE_FORM)
            else solution["target_value"]
        )
        scored.append({
            "window": row,
            "solution": solution,
            "score": float(score),
            # Charter 143: Kelly rows the chain law refused before the
            # completion was seated on this window; empty on every other.
            "chain_gate_excluded_candidate_ids": list(
                local_excluded[len(tuple(excluded_candidate_ids)):]
            ),
            # The belief this window was judged on, for the execution
            # layer's edge rule (charter 73).
            "window_probabilities": {
                str(bucket): float(value)
                for bucket, value in window_probabilities.items()
            },
        })
    if not scored:
        raise _support_window_error(
            "delta_v2_no_solvable_support_window",
            window_audit=window_audit, failures=failures,
        )
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
    # Charter 68(c): which lambda table each scope was served from this
    # round -- the store's bands when one is activated, ``None`` where the
    # kernel constant still bootstraps.  Without this the dispatch leaves
    # no trace, and "did production read the table" cannot be answered from
    # a decision row.
    served_lambda_tables = {
        scope: (
            None if not bands
            else [
                [None if b == float("inf") else float(b), float(lam)]
                for b, lam in bands
            ]
        )
        for scope, bands in (effective_edge_tables or {}).items()
        if scope not in (BETA_TABLE_KEY, VALUATION_TABLE_KEY, POSTERIOR_TABLE_KEY)
    }
    selection = {
        "decision_moment": decision_moment_audit,
        "effective_edge_tables": served_lambda_tables,
        # Charter 128: a dict per stage, not a band table; kept apart so a
        # reader of the lambda tables never meets a row it cannot parse.
        "beta_edge_tables": (effective_edge_tables or {}).get(BETA_TABLE_KEY),
        # Charter 132: the valuation rows as served, draws elided -- the
        # per-window readings carry what the draws produced.
        "valuation_tables": {
            str(scope): (
                None if not isinstance(row, Mapping) else {
                    key: row.get(key)
                    for key in ("beta", "beta_lcb", "beta_ucb", "accumulating",
                                "verdict", "sha256")
                }
            )
            for scope, row in dict(
                (effective_edge_tables or {}).get(VALUATION_TABLE_KEY) or {}
            ).items()
        },
        # L4 posterior lane (shadow): the served rows, draws elided.
        "posterior_tables": {
            str(scope): (
                None if not isinstance(row, Mapping) else {
                    key: row.get(key)
                    for key in ("alpha", "gamma", "w", "w_lcb", "w_ucb",
                                "accumulating", "sha256")
                }
            )
            for scope, row in dict(
                (effective_edge_tables or {}).get(POSTERIOR_TABLE_KEY) or {}
            ).items()
        },
        # Charter 131 (2026-09-12 evening): the head of the shortlist is the
        # admissible window with the largest effective edge; Kelly sizes it.
        "rule": "EFFECTIVE_EDGE_SHORTLIST_HEAD_KELLY_SIZED_V4",
        "seat_rule_superseded": (
            "PROBABILITY_MASS_SHORTLIST_HEAD_CURVE_SOLVED_QUANTITIES_V3"
        ),
        "shortlist_head_curve_score": float(scored[0]["score"]),
        "best_curve_score_on_shortlist": float(
            max(item["score"] for item in scored)
        ),
        "economic_hurdle": ECONOMIC_HURDLE_V1,
        "economic_hurdle_rule": ECONOMIC_HURDLE_RULE_VERSION,
        # Charter 137: which budget the Kelly target level was read off.  A
        # decision row that shows no Kelly candidate on a held chain is read
        # differently depending on this, so it rides on every plan rather
        # than only on the fingerprint.
        "kelly_topup_basis": KELLY_TOPUP_BASIS,
        "kelly_topup_basis_rule": KELLY_TOPUP_BASIS_RULE_VERSION,
        # Charter 124: which arm and which ball this round was read at, so a
        # shadow row can never be mistaken for a served one and a promotion
        # argument can name the budget its evidence was gathered under.
        "robust_claim_rule": ROBUST_CLAIM_RULE_VERSION,
        "robust_claim_mode": CHARTER_124_ROBUST_CLAIM,
        "robust_claim_divergence_nats": ROBUST_CLAIM_DIVERGENCE_BUDGET_NATS,
        "admissible_window_count": len(windows),
        "solved_window_count": len(scored),
        **{
            key: value for key, value in window_audit.items()
            # Charter 140: the refused windows ride in the decision audit's
            # ``candidate_windows``; spreading them here too would store
            # every refused window twice on every plan.
            if key != "refused_priced_windows"
        },
        "unsolvable_windows": failures,
        "seated": {
            "buckets": list(best["window"]["buckets"]),
            "win_probability": best["window"]["win_probability"],
            "effective_win_probability": best["window"].get(
                "effective_win_probability"
            ),
            "effective_edge_lambda": best["window"].get("effective_edge_lambda"),
            # Charter 97: which axis served that lambda.  The gate stamps it
            # on the row; this projection is a key-by-key whitelist, so
            # omitting it here dropped it on the way out and the clause's
            # own retirement condition -- seats whose lambda came from the
            # claim-edge band -- could never have accumulated a single seat.
            # Reported by the session reading the plans, 2026-09-06.
            "effective_edge_lambda_source": best["window"].get(
                "effective_edge_lambda_source"
            ),
            "effective_edge_agreement": best["window"].get(
                "effective_edge_agreement"
            ),
            "all_in_price": best["window"]["all_in_price"],
            "edge": best["window"]["edge"],
            # Audit only.  This projection is a key-by-key whitelist, so a
            # field added to the window row and not added here is dropped on
            # the way out -- the same way charter 97's lambda source was, and
            # for the same reason its retirement condition could never have
            # accumulated a seat.
            "marginal_coverage": best["window"].get("marginal_coverage"),
            "curve_score": best["score"],
            "is_incumbent": best["window"]["is_incumbent"],
            # Charter 78: whether the seated window cleared the positive-EV
            # gate; an inadmissible incumbent is held on, never bought.
            "admissible": bool(best["window"].get("admissible", True)),
            "window_probabilities": dict(best["window_probabilities"]),
            "completion": best["window"].get("completion"),
            # Charter 143: Kelly rows the chain law refused before this
            # window's completion was seated; empty on every other seat.
            "chain_gate_excluded_candidate_ids": list(
                best.get("chain_gate_excluded_candidate_ids") or []
            ),
        },
        # Charter 131 (14): BUILDING never reads as protected.
        "chain_protection": chain_protection_v1(
            snapshot, list(best["window"]["buckets"])
        ),
        "considered": [
            {
                "buckets": list(item["window"]["buckets"]),
                "win_probability": item["window"]["win_probability"],
                "effective_win_probability": item["window"].get(
                    "effective_win_probability"
                ),
                "effective_edge_lambda": item["window"].get(
                    "effective_edge_lambda"
                ),
                "effective_edge_lambda_source": item["window"].get(
                    "effective_edge_lambda_source"
                ),
                "effective_edge_agreement": item["window"].get(
                    "effective_edge_agreement"
                ),
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
    sizing_probabilities_by_bucket: Mapping[str, float] | None = None,
    hours_to_cutoff: float | None = None,
    centre_bucket: str = "",
    book_equity_usdc: float | None = None,
    decision_moment: bool = True,
    completion_capital_usdc: float | None = None,
    taf_tx_entry: bool = False,
    completion_143_admitted: bool = False,
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
    # The ladder this lane would actually buy against, decided once and used
    # by all three generators below -- the clip grid, the centre-anchored
    # shape and the continuous optimum.  Three copies of "what does a share
    # cost" is how the target and the order stopped agreeing in the first
    # place.
    lane_levels: dict[str, Sequence[Any]] = {}
    lane_prices: dict[str, float] = {}
    bases: set[str] = set()
    for bucket in ordered:
        row = by_bucket[bucket]
        levels, price, basis = _lane_purchase_levels(
            row,
            stage=str(snapshot.evidence.probability_stage or ""),
        )
        lane_levels[bucket] = levels
        lane_prices[bucket] = (
            float(price) if price is not None
            else (float(row.ask_price) if row.ask_available else 0.0)
        )
        bases.add(basis)
    # One string for the basket, and it must not claim a basis no leg used.
    # A basket priced part at the band and part at the offer is neither, and
    # saying so is cheaper than a reader assuming the wrong one.
    pricing_basis = (
        bases.pop() if len(bases) == 1
        else "mixed_maker_band_and_ask_depth"
    )
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
                levels=lane_levels[bucket],
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
            "pricing_basis": pricing_basis,
            "candidate_kind": (
                CANDIDATE_KIND_BASELINE
                if is_baseline
                else CANDIDATE_KIND_COMPLETE
            ),
            "candidate_origin": "CLIP_GRID_PRODUCT",
        })
    chain_empty = all(
        float(current_shares[bucket]) + float(pending_shares[bucket]) <= EPS
        for bucket in current_shares
    )
    stage_name = str(snapshot.evidence.probability_stage or "")
    # Charter 131 (2026-09-12 evening): P0-M does not open a chain, TAF or
    # no TAF -- there must be a P0.  Charter 90's one later-stage opening is
    # retired; ``taf_tx_entry`` only lets an entry leg join a HELD basket,
    # which is not an opening and needs no exemption here.  Charter 143 v2
    # reads it once more: the entry window's legs need no send history.
    later_stage_on_empty_chain = (
        chain_empty and stage_name not in CHAIN_OPENING_STAGES
    )
    if (
        book_equity_usdc is not None
        and float(book_equity_usdc) > 0.0
        # Charter 74: P0-M and P1 add to what P0 holds; on an empty chain
        # they are offered nothing, so the solver holds.
        and not later_stage_on_empty_chain
    ):
        # Charter 72: the Kelly-sized seat, offered beside the grid.  Under
        # the whole-basket Kelly form it is the winner when legal.
        kelly_candidate = _whole_basket_kelly_candidate(
            ordered=ordered,
            lane_levels=lane_levels,
            lane_prices=lane_prices,
            pricing_basis=pricing_basis,
            current_shares=current_shares,
            current_cost=current_cost,
            pending_shares=pending_shares,
            pending_cost=pending_cost,
            capital_available_usdc=capital_available_usdc,
            # The one place the split is spent: the stake formula.  The
            # candidate's shape, the grid, the ranking and every gate
            # still read ``probabilities_by_bucket``.
            probabilities_by_bucket=(
                sizing_probabilities_by_bucket
                if sizing_probabilities_by_bucket is not None
                else probabilities_by_bucket
            ),
            book_equity_usdc=float(book_equity_usdc),
            quote_version=str(snapshot.quote_version),
            # The share-shape arm's inputs; unread under ``equal``.
            centre_bucket=str(centre_bucket or ""),
            ordinals={
                bucket: int(by_bucket[bucket].ordinal) for bucket in ordered
            },
            # The bucket arm reads a per-stage table; the seat's own
            # stage is the one that will settle it.
            stage=stage_name,
        )
        if kelly_candidate is not None:
            candidates.append(kelly_candidate)
    # Charter 84 (八): a missing rung the market prices under the leg floor
    # is dead -- not bought, and not a reason to leave its neighbours
    # short.  Read off the posted offer, the floor the window rule reads.
    market_dead = frozenset(
        bucket for bucket in ordered
        if float(current_shares[bucket]) + float(pending_shares[bucket]) <= EPS
        and 0.0 < float(lane_prices.get(bucket, 0.0) or 0.0)
        and _leg_below_floor(
            by_bucket[bucket], probabilities_by_bucket.get(bucket)
        )
    )
    if CHARTER_84_LADDER_BALANCE == "on" and not chain_empty:
        # Charter 84: a seat the venue left short.  The Kelly candidate above
        # is None whenever any rung already holds its target, and the grid
        # buys a clip of every rung or nothing -- so a ladder holding two
        # rungs of three had no candidate that simply bought the third at
        # the size the other two were bought at.  Any stage: completing the
        # seat is not opening one (charter 74) and not adding to a whole one
        # (charter 71).
        # Charter 127 (五): a designated completion crosses, capped at the
        # cash this candidate is authorized for -- so the candidate is priced
        # where it crosses.  ``lane_levels`` is the maker band at P0-M
        # (``TARGET_PRICING_LANE_STAGES``), always a tick or more under the
        # offer an FOK must take, and every P0-M completion was refused
        # ``completion_take_above_authorized_cost`` with nothing resting in
        # its place: LFPB 2026-09-14 29C, seventeen compiles on 2026-09-13,
        # 0.08/0.09 book, authorized at the band 0.40 against a 0.45 walk.
        completion_crosses = CHARTER_127_COMPLETION_TAKE == "on"
        balance_candidate = _ladder_balance_candidate(
            ordered=ordered,
            lane_levels=(
                {
                    bucket: by_bucket[bucket].ask_levels
                    for bucket in ordered
                }
                if completion_crosses else lane_levels
            ),
            lane_prices=(
                {
                    bucket: (
                        float(by_bucket[bucket].ask_price)
                        if by_bucket[bucket].ask_available else 0.0
                    )
                    for bucket in ordered
                }
                if completion_crosses else lane_prices
            ),
            pricing_basis=(
                COMPLETION_CROSSING_PRICING_RULE_VERSION
                if completion_crosses else pricing_basis
            ),
            current_shares=current_shares,
            current_cost=current_cost,
            pending_shares=pending_shares,
            pending_cost=pending_cost,
            # Charter 84 (八): sized against the capital the seat was
            # opened with, not the tilt-cut capital of this compile.
            capital_available_usdc=(
                float(completion_capital_usdc)
                if completion_capital_usdc is not None
                else capital_available_usdc
            ),
            quote_version=str(snapshot.quote_version),
            unbuyable=market_dead,
            # Charter 110(四)(五)(六): the completion's own three gates read
            # this claim -- it is the seated charter-68 partition
            # (``window_probabilities``), which is what the kernel requires.
            probabilities_by_bucket=probabilities_by_bucket,
            hours_to_cutoff=hours_to_cutoff,
            # Charter 143 v2: only legs the chain sent, at most at their size
            # -- except on charter 90's entry window, sanctioned whole.
            sent_shares_by_bucket=(
                None if taf_tx_entry else _completion_143_sent_shares(snapshot)
            ),
        )
        if balance_candidate is not None:
            candidates.append(balance_candidate)
    if centre_bucket:
        candidates.extend(_centre_anchored_candidates(
            snapshot=snapshot,
            ordered=ordered,
            centre_bucket=centre_bucket,
            purchase_levels=lane_levels,
            prices_by_bucket=lane_prices,
            pricing_basis=pricing_basis,
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
            purchase_levels=lane_levels,
            prices_by_bucket=lane_prices,
            pricing_basis=pricing_basis,
            current_shares=current_shares,
            current_cost=current_cost,
            pending_shares=pending_shares,
            pending_cost=pending_cost,
            capital_available_usdc=capital_available_usdc,
            minimum_clip_notional_usdc=minimum_clip_notional_usdc,
            bankroll_usdc=bankroll_usdc,
            probabilities_by_bucket=probabilities_by_bucket,
        ))
    if not decision_moment:
        # Charter 77: nothing changed that a decision reads, or the window
        # itself is inadmissible (charter 78) -- the baseline (hold what we
        # hold) is the only candidate offered.
        #
        # Charter 143: a broken basket is a standing condition, not news, so
        # its completion is offered whether or not anything moved -- on a
        # window the rule admitted, and only the candidate the rule itself
        # certified.
        candidates = [
            row for row in candidates
            if row.get("candidate_kind") == CANDIDATE_KIND_BASELINE
            or (
                completion_143_admitted
                and CHARTER_143_COMPLETION == "on"
                and row.get("candidate_origin") == LADDER_BALANCE_CANDIDATE_ORIGIN
                and (row.get("completion_143") or {}).get("exempt") is True
            )
        ]
    if not candidates:
        raise ContractError("delta_v2_target_candidate_set_empty")
    return _assert_candidate_support_contract(
        candidates, ordered_support=ordered, unbuyable=market_dead
    )



def _whole_basket_kelly_candidate(
    *,
    ordered: Sequence[str],
    lane_levels: Mapping[str, Sequence[Any]],
    lane_prices: Mapping[str, float],
    pricing_basis: str,
    current_shares: Mapping[str, float],
    current_cost: Mapping[str, float],
    pending_shares: Mapping[str, float],
    pending_cost: Mapping[str, float],
    capital_available_usdc: float,
    probabilities_by_bucket: Mapping[str, float],
    book_equity_usdc: float,
    quote_version: str,
    centre_bucket: str = "",
    ordinals: Mapping[str, int] | None = None,
    share_shape: str = KELLY_SHARE_SHAPE,
    stage: str = "",
) -> dict[str, Any] | None:
    """Charter 72's candidate: the seat sized as the ledger sizes it.

    One share of every support bucket costs ``cost_per_share``; the deflated
    claim over the support against that cost gives the Kelly stake off the
    whole book, bounded by what the scope may spend; the stake buys equal
    shares of every leg, walked down the same ladder the grid prices on.
    ``None`` when there is no edge, nothing to spend, no depth, or a leg
    already holds its target: a complete basket must add to every support
    leg (the 2026-08-12 atomicity ruling), so a chain at or above the Kelly
    target on any leg stands pat rather than buying the others.
    """

    cost_per_share = sum(float(lane_prices.get(bucket, 0.0)) for bucket in ordered)
    if cost_per_share <= EPS or cost_per_share >= 1.0 - EPS:
        return None
    claim = sum(
        float(probabilities_by_bucket.get(bucket, 0.0) or 0.0) for bucket in ordered
    )
    fraction, stake = whole_basket_kelly_stake_v1(
        win_probability=claim,
        cost=cost_per_share,
        book_equity_usdc=float(book_equity_usdc),
    )
    # Charter 137: the level is sized against ``kelly_target_budget_usdc_v1``
    # -- under the shipped ``net`` basis that is exactly this compile's room,
    # so the arithmetic below is unchanged; under ``gross`` it is the stage's
    # whole allowance, and the room comes back as a bound on the increment
    # after the per-leg gaps are known.
    stake = min(float(stake), kelly_target_budget_usdc_v1(
        book_equity_usdc=float(book_equity_usdc),
        stage=str(stage),
        capital_available_usdc=float(capital_available_usdc),
    ))
    if stake <= EPS:
        return None
    # The split (research arm; ``equal`` is charter 72 as shipped and
    # reproduces the pre-arm arithmetic exactly: every weight is 1.0, the
    # unit cost is the per-share cost, and multiplying by 1.0 is exact).
    weights = _kelly_share_weights(
        ordered=ordered,
        lane_prices=lane_prices,
        probabilities_by_bucket=probabilities_by_bucket,
        centre_bucket=centre_bucket,
        ordinals=ordinals or {},
        shape=share_shape,
        stage=stage,
    )
    unit_cost = sum(
        weights[bucket] * float(lane_prices.get(bucket, 0.0))
        for bucket in ordered
    )
    if unit_cost <= EPS:
        return None
    want = stake / unit_cost
    want_by_bucket = {bucket: want * weights[bucket] for bucket in ordered}
    shares = {
        bucket: float(current_shares[bucket]) + float(pending_shares[bucket])
        for bucket in current_shares
    }
    costs = {
        bucket: float(current_cost[bucket]) + float(pending_cost[bucket])
        for bucket in current_cost
    }
    shaped = share_shape != "equal"
    added_legs = 0
    increments: dict[str, float] = {}
    for bucket in ordered:
        increment = want_by_bucket[bucket] - shares[bucket]
        if increment <= EPS:
            if KELLY_SHAPE_TOPUP == "short_legs" and shares[bucket] > EPS:
                # Research arm (user, 2026-09-06: "既要看整篮，也要看目标
                # 腿"): the stake is still the whole basket's, but a held
                # rung already at or above its target is left as it is and
                # the rungs below theirs are bought up to theirs.  Under
                # ``atomic`` (charter 72 as shipped) any such rung stands
                # the whole basket down.  Applies to every share shape.
                increments[bucket] = 0.0
                continue
            return None
        increments[bucket] = increment
    # Charter 137: the cash bound, applied once the gaps are known.  Under
    # ``net`` this is unreachable -- the per-leg targets sum to ``stake`` at
    # the lane prices and ``stake`` is already at most the room -- so the
    # shipped basis walks the same ladder with the same numbers.  Under
    # ``gross`` the level may want more than the scope may still spend, and
    # the whole basket scales down together rather than dropping a leg:
    # dropping one is the incomplete basket charter 72's atomicity exists to
    # refuse.
    planned_cash = sum(
        increments[bucket] * float(lane_prices[bucket]) for bucket in ordered
    )
    room = max(0.0, float(capital_available_usdc))
    if planned_cash > room + EPS:
        if room <= EPS:
            return None
        cash_scale = room / planned_cash
        increments = {
            bucket: value * cash_scale for bucket, value in increments.items()
        }
    for bucket in ordered:
        increment = increments[bucket]
        if increment <= EPS:
            continue
        leg_cash = increment * float(lane_prices[bucket])
        got, spent = _ask_depth_purchase(
            levels=lane_levels[bucket], cash_limit=leg_cash,
        )
        if got <= EPS:
            return None
        shares[bucket] += got
        costs[bucket] += spent
        added_legs += 1
    if added_legs == 0:
        return None
    return {
        "candidate_id": "kelly-" + content_sha256({
            "ordered_support": tuple(ordered),
            "stake_usdc": round(stake, 6),
            "quote_version": quote_version,
            **(
                {
                    "share_shape": share_shape,
                    "share_weights": [
                        round(weights[bucket], 6) for bucket in ordered
                    ],
                }
                if shaped else {}
            ),
        })[:24],
        "shares_by_bucket": shares,
        "cost_by_bucket": costs,
        "pricing_basis": pricing_basis,
        "candidate_kind": CANDIDATE_KIND_COMPLETE,
        "candidate_origin": KELLY_CANDIDATE_ORIGIN,
        "kelly": {
            "win_probability": claim,
            "cost_per_share": cost_per_share,
            "fraction": fraction,
            "stake_usdc": stake,
            "book_equity_usdc": float(book_equity_usdc),
            "shares_per_leg": want,
            **(
                {
                    "share_shape": share_shape,
                    "share_weights": {
                        bucket: weights[bucket] for bucket in ordered
                    },
                    "shares_by_leg": want_by_bucket,
                }
                if shaped else {}
            ),
        },
    }


def _kelly_share_weights(
    *,
    ordered: Sequence[str],
    lane_prices: Mapping[str, float],
    probabilities_by_bucket: Mapping[str, float],
    centre_bucket: str,
    ordinals: Mapping[str, int],
    shape: str = KELLY_SHARE_SHAPE,
    stage: str = "",
) -> dict[str, float]:
    """Relative share count per rung under the Kelly share-shape arm.

    ``equal``: 1.0 on every rung.  ``edge_weighted``: the kernel's two-段
    shape -- an equal bed, then the basket's floor cushion tilted by each
    rung's realisable edge ``lambda(a)*p - c`` on the charter-68 bucket arm;
    ``sum(w*c)`` is unchanged so charter 72's stake reads the same cost, and
    the resulting in-support floor is non-negative by construction so charter
    93 cannot refuse it.  ``stage`` picks the lambda table; without one the
    rung lambda is 1.0 and the edge is the nominal one.  ``centre_tilt``: the
    2026-08-17 shape --
    ``CENTRE_ANCHOR_DISTANCE_DECAY`` to the power of the rung's distance
    from the centre, times one plus ``CENTRE_ANCHOR_EDGE_TILT`` scaled by
    the same edge ratio, floored at ``CENTRE_ANCHOR_MIN_RUNG_SHARE``, the
    centre never below 1; without a seated centre it splits equally.  The
    weights are relative: the candidate scales them so the seat costs the
    Kelly stake.
    """

    if shape not in KELLY_SHARE_SHAPES:
        raise ContractError("delta_v2_kelly_share_shape_invalid")
    equal = {bucket: 1.0 for bucket in ordered}
    if shape == "equal":
        return equal
    edges = {
        bucket: (
            float(probabilities_by_bucket.get(bucket, 0.0) or 0.0)
            - float(lane_prices.get(bucket, 0.0) or 0.0)
        )
        for bucket in ordered
    }
    best = max((max(edge, 0.0) for edge in edges.values()), default=0.0)
    if shape == "edge_weighted":
        # The realisable claim, not the asserted one: a rung the book prices
        # far under our claim is read at what such rungs were measured to
        # return.  ``a`` is that rung's own agreement, so this is the bucket
        # arm of charter 68 and not the window one the seat was sized on.
        realisable: dict[str, float] = {}
        for bucket in ordered:
            claim = float(probabilities_by_bucket.get(bucket, 0.0) or 0.0)
            price = float(lane_prices.get(bucket, 0.0) or 0.0)
            agreement = price / claim if claim > EPS else float("inf")
            realisable[bucket] = claim * effective_edge_bucket_lambda_for_stage(
                str(stage or ""), agreement
            )
        return floor_first_edge_weights_v1(
            ordered=ordered,
            prices={bucket: float(lane_prices.get(bucket, 0.0) or 0.0) for bucket in ordered},
            effective_claims=realisable,
            # The belief the window was admitted on, so the shape cannot
            # hand back a basket that expects less than the bed charter 78
            # cleared.  Two deflations meet here -- the window scope is
            # already inside ``probabilities_by_bucket`` and the bucket
            # scope is applied above -- and they do not have to agree on
            # the order of the rungs.
            partition_by_bucket=probabilities_by_bucket,
        )
    if centre_bucket not in ordered or centre_bucket not in ordinals:
        return equal
    if any(bucket not in ordinals for bucket in ordered):
        return equal
    centre = int(ordinals[centre_bucket])
    weights: dict[str, float] = {}
    for bucket in ordered:
        decay = CENTRE_ANCHOR_DISTANCE_DECAY ** abs(int(ordinals[bucket]) - centre)
        lean = 1.0 + CENTRE_ANCHOR_EDGE_TILT * (
            max(edges[bucket], 0.0) / best if best > EPS else 0.0
        )
        weights[bucket] = max(decay * lean, CENTRE_ANCHOR_MIN_RUNG_SHARE)
    weights[centre_bucket] = max(weights[centre_bucket], 1.0)
    return weights


def _ladder_balance_candidate(
    *,
    ordered: Sequence[str],
    lane_levels: Mapping[str, Sequence[Any]],
    lane_prices: Mapping[str, float],
    pricing_basis: str,
    current_shares: Mapping[str, float],
    current_cost: Mapping[str, float],
    pending_shares: Mapping[str, float],
    pending_cost: Mapping[str, float],
    capital_available_usdc: float,
    quote_version: str,
    unbuyable: Collection[str] = (),
    probabilities_by_bucket: Mapping[str, float] | None = None,
    hours_to_cutoff: float | None = None,
    sent_shares_by_bucket: Mapping[str, float] | None = None,
) -> dict[str, Any] | None:
    """Charter 84's candidate: the seat's short rungs raised to its held level.

    ``unbuyable`` (charter 84 八) names the short rungs the market prices
    under the leg floor: left as they are, the completion covers the rest;
    a completion with nothing buyable is no candidate.

    The level is the kernel's (``ladder_balance_targets_v1``): the cheapest
    held rung size the completed basket still clears charter 93's floor at,
    smallest first (user ruling 2026-09-08).  Each short rung buys the
    difference down the same ladder the grid prices on; a held rung is left
    exactly as it is.  ``None`` when the window holds nothing, nothing is
    short, a short rung has no depth, or the completion does not fit the
    capital -- all-or-none, because a ladder completed on one rung and not
    the other is still broken.

    First live morning, 2026-09-05: ZGSZ 2026-09-06 held 30°C and 31°C at
    five shares each after the venue refused 32°C ($0.95 under the dollar
    minimum); with the holdings finally visible the Kelly law sized the seat
    at 0.75 USDC against 3.4 held and offered nothing, four grid slivers
    failed the floor, and the plan read ``target_inventory_satisfied``.
    """

    # Charter 110(六): past the cutoff there is no completion.  Three hours
    # out the measured completion edge is -0.026, and inside the last hour
    # -0.075: what trades then is the outcome being priced in, and we are on
    # the wrong side of it.  The residual is carried, not handed back -- so
    # this refuses the BUY and says nothing about the legs already held.
    # ``None`` is a caller with no clock, which is every caller that has not
    # been taught tau yet; those keep the pre-110 behaviour rather than
    # silently completing at any hour.
    if (
        CHARTER_110_COMPLETION == "on"
        and hours_to_cutoff is not None
        and float(hours_to_cutoff) <= COMPLETION_CUTOFF_HOURS
    ):
        return None

    held = {
        bucket: float(current_shares[bucket]) + float(pending_shares[bucket])
        for bucket in ordered
    }
    # Charter 110(四): the completion ceiling is inverted, so the legs worth
    # buying are the ones the market took AWAY from us, not the ones it hands
    # us cheap.  Measured 2026-09-07 on 2,575 short legs: where the ask sits
    # at or under our claim the market is selling us its own discount and
    # those legs realise 0.28 of what we claim (-0.0383 a share, interval
    # [-0.053, -0.021], clear of zero); where the ask is above our claim they
    # realise 1.60.  The kernel owns the partition -- this only asks it and
    # widens the dead-rung set the search and the build already share, so a
    # leg refused here is not charged for either.
    #
    # The claim is the seated charter-68 partition (``window_probabilities``
    # at the call site), which is what ``completion_conditioned_claim_v1``
    # requires; a raw served vector here would price the refusal on a belief
    # the seat was never judged on.
    # The refusal is of the WHOLE completion, not of a leg, and that is the
    # correction the book made on 2026-09-09.  Routing it through
    # ``unbuyable`` -- charter 84 (八)'s dead-rung set -- inherited that
    # set's second meaning: a dead rung is not charged into ``spend`` OR
    # into the cost the floor verdict reads, because a rung the market
    # genuinely cannot supply must not refuse a level the seat can afford.
    # A leg we are DECLINING to buy is not that: it is buyable, and
    # excusing it from both bounds let the level rise on money the refusal
    # had notionally freed.  Measured over ten days: total stake barely
    # moved (865 -> 857) though three quarters of short legs were refused,
    # twelve station-days were staked HIGHER, and those twelve carried
    # -21.70 of a -39.65 swing.
    #
    # A completion is all-or-none by construction -- "a ladder completed on
    # one rung and not the other is still broken" -- so the honest reading
    # of charter 110(四) here is that a completion needing a leg the market
    # is handing us cheap is a completion not worth making.  Refusing the
    # candidate outright relaxes nothing.
    #
    # Charter 143 supersedes this refusal: a single-leg basket is completed
    # on exactly the legs it names as not worth it, and a multi-leg one
    # regardless of their price.
    if (
        CHARTER_143_COMPLETION != "on"
        and CHARTER_110_COMPLETION == "on"
        and probabilities_by_bucket is not None
    ):
        dead = {str(bucket) for bucket in unbuyable}
        for bucket in ordered:
            if held.get(bucket, 0.0) > EPS or bucket in dead:
                continue
            ask = float(lane_prices.get(bucket, 0.0) or 0.0)
            if ask <= 0.0:
                continue
            if not completion_conditioned_claim_v1(
                claim=float(probabilities_by_bucket.get(bucket, 0.0) or 0.0),
                ask=ask,
            )["buys"]:
                return None
    # User ruling 2026-09-08: the level is the cheapest held rung size the
    # completed basket is still floor-legal at, not the tallest rung -- the
    # tallest is usually the one charter 70/83 lifted to a lot or a dollar,
    # so completing to it buys the dear rungs at the cheap rung's count.
    # The kernel prices the search, so it needs what the seat paid and the
    # ladder the short rungs will actually be bought down.
    #
    # That ladder, not the top of it (user ruling 2026-09-08: "B+A").  The
    # search used to price a gap at ``lane_prices`` -- the first ask -- while
    # the build below walked the book with the cash that price implied, so a
    # rung offering one share at 0.15 and the rest at 0.90 delivered 1.7 of
    # the 5 shares the level asked for and the floor refused the
    # under-completed basket the search had just certified.  45 of the 78
    # completions offered over 2026-09-06..08 died that way.  The search now
    # pays what the build will pay, and the build below buys shares rather
    # than cash so the two cannot come apart again.
    depth_by_bucket = {
        bucket: tuple(
            {"price": float(level.price), "shares": float(level.shares)}
            for level in (lane_levels.get(bucket) or ())
        )
        for bucket in ordered
    }
    completion_143_reading: dict[str, Any] | None = None
    if CHARTER_143_COMPLETION == "on":
        # Charter 143: which rungs and to what level is decided by the held
        # count -- a multi-leg basket's missing rungs to its cheapest held
        # size while the worst in-window settlement improves; a single leg
        # buys only the rungs that clear the hurdle.  Priced down the same
        # ladder and bounded by the same capital as the build below.
        completion_143_reading = completion_143_targets_v1(
            held_by_bucket=held,
            support=ordered,
            held_cost_by_bucket={
                bucket: float(current_cost[bucket]) + float(pending_cost[bucket])
                for bucket in ordered
            },
            depth_by_bucket=depth_by_bucket,
            capital_available_usdc=max(0.0, float(capital_available_usdc)),
            claim_by_bucket=probabilities_by_bucket,
            below_leg_floor_buckets=tuple(str(bucket) for bucket in unbuyable),
            hours_to_cutoff=hours_to_cutoff,
            fee_rate=0.0,
            sent_shares_by_bucket=sent_shares_by_bucket,
        )
        if not completion_143_reading["targets"]:
            return None
    targets = dict(completion_143_reading["targets"]) if (
        completion_143_reading is not None
    ) else ladder_balance_targets_v1(
        held_by_bucket=held,
        support=ordered,
        held_cost_by_bucket={
            bucket: float(current_cost[bucket]) + float(pending_cost[bucket])
            for bucket in ordered
        },
        depth_by_bucket=depth_by_bucket,
        ask_by_bucket={
            bucket: float(lane_prices.get(bucket, 0.0) or 0.0)
            for bucket in ordered
        },
        capital_available_usdc=max(0.0, float(capital_available_usdc)),
        # Charter 84 (八)'s dead rungs: the build skips them, so the search
        # must not charge for them either -- once the cash bound existed, a
        # rung nobody buys could refuse a level the seat could afford.
        below_leg_floor_buckets=tuple(str(bucket) for bucket in unbuyable),
        # Zero, and deliberately: the increments below are priced by the same
        # walk with no fee added.  A search that charged one would judge a
        # level illegal that the candidate it sizes then builds as legal, and
        # the two would disagree about the same basket.
        fee_rate=0.0,
    )
    if not targets:
        return None
    shares = {
        bucket: float(current_shares[bucket]) + float(pending_shares[bucket])
        for bucket in current_shares
    }
    costs = {
        bucket: float(current_cost[bucket]) + float(pending_cost[bucket])
        for bucket in current_cost
    }
    short_buckets: list[str] = []
    wanted: dict[str, float] = {}
    dead = {str(bucket) for bucket in unbuyable}
    for bucket in ordered:
        increment = float(targets[bucket]) - held[bucket]
        if increment <= EPS or bucket in dead:
            continue
        price = float(lane_prices.get(bucket, 0.0) or 0.0)
        if price <= EPS:
            return None
        wanted[bucket] = increment
        short_buckets.append(bucket)
    if not short_buckets:
        return None
    capital = max(0.0, float(capital_available_usdc))
    if capital <= EPS:
        return None
    # Bought by shares, not by cash.  The old walk took a cash limit derived
    # from the first ask and kept whatever the book gave for it, which is how
    # a certified five-share completion became 1.7 shares; asking the ladder
    # for the quantity makes "the book cannot supply this" an answer rather
    # than a shortfall nobody looks at.  A completion that does not fit whole
    # -- on depth or on cash -- is refused, not scaled: a scaled completion is
    # the short ladder it was meant to repair (charter 84 (八), all-or-none).
    spend = 0.0
    for bucket in short_buckets:
        spent = ask_depth_cost_v1(
            levels=depth_by_bucket[bucket], shares=wanted[bucket],
        )
        if spent is None or spent <= EPS:
            return None
        shares[bucket] += wanted[bucket]
        costs[bucket] += spent
        spend += spent
    if spend > capital + EPS:
        return None
    # The build has to be the thing the search certified.  The two price the
    # same ladder now, so this can only fire on a defect -- which is exactly
    # why it is here and not in a comment.
    for bucket in ordered:
        if bucket in dead:
            continue
        if shares[bucket] + EPS < float(targets[bucket]):
            return None
    # The level the kernel actually chose, not ``max(held)``: the two differ
    # whenever the search walked past a rung the venue had lifted.
    level = max(float(targets[bucket]) for bucket in ordered)
    # Charter 143: the exemption the solver's floor reads, taken on the
    # shape this candidate builds -- the same function the compile re-reads
    # on the lifted bundle.
    completion_143 = None
    if completion_143_reading is not None:
        completion_143 = {
            **completion_143_exemption_v1(
                support=tuple(ordered),
                held_shares=held,
                held_cost_usdc={
                    bucket: float(current_cost[bucket])
                    + float(pending_cost[bucket])
                    for bucket in ordered
                },
                target_shares={bucket: shares[bucket] for bucket in ordered},
                target_cost_usdc={bucket: costs[bucket] for bucket in ordered},
                claim_by_bucket=probabilities_by_bucket,
                below_leg_floor_buckets=tuple(str(b) for b in unbuyable),
                hours_to_cutoff=hours_to_cutoff,
                sent_shares_by_bucket=sent_shares_by_bucket,
            ),
            "level_rule": completion_143_reading["level_rule"],
            "level_shares": completion_143_reading["level_shares"],
            # The rungs the rule leaves unbought on purpose -- a single leg's
            # that do not clear the hurdle, any leg the chain never sent --
            # which the support contract reads.
            "declined_buckets": [
                bucket for bucket in ordered
                if bucket not in dead
                and held[bucket] <= EPS
                and shares[bucket] <= EPS
            ],
        }
        if not completion_143["exempt"]:
            # The build and the rule read the same ladder, so this can only
            # fire on a defect; offered anyway, a shape with a declined leg
            # would fail the support contract and take the window with it.
            return None
    return {
        **({"completion_143": completion_143} if completion_143 else {}),
        "candidate_id": "balance-" + content_sha256({
            "ordered_support": tuple(ordered),
            "level_shares": round(level, 6),
            "short_buckets": tuple(short_buckets),
            "quote_version": quote_version,
        })[:24],
        "shares_by_bucket": shares,
        "cost_by_bucket": costs,
        "pricing_basis": pricing_basis,
        "candidate_kind": CANDIDATE_KIND_COMPLETE,
        "candidate_origin": LADDER_BALANCE_CANDIDATE_ORIGIN,
        "ladder_balance": {
            "level_shares": level,
            "short_buckets": short_buckets,
            "spend_usdc": spend,
            # The completion is all-or-none on depth and on cash from
            # 2026-09-08, so what it spends IS what the level costs and the
            # scale is always one.  Both kept, at their now-constant values,
            # because a reader comparing this receipt across the cutover
            # should see the scaling stop rather than see the field vanish.
            "full_spend_usdc": spend,
            "capital_scale": 1.0,
            "level_rule": LADDER_BALANCE_LEVEL_RULE_VERSION,
            # Recorded, never read by a gate (user ruling 2026-09-08:
            # "先只记账不进闸").  Charter 73/92 authorizes this buy on the
            # increment's own edge and charter 93 judges the shape on the
            # in-support floor; a completion is where the two habitually
            # disagree, and nobody can argue for an exemption without the
            # joint distribution on the book's own settled history.
            "floor_delta": completion_floor_delta_v1(
                support=tuple(ordered),
                held_shares=held,
                held_cost_usdc={
                    bucket: float(current_cost[bucket])
                    + float(pending_cost[bucket])
                    for bucket in ordered
                },
                target_shares=targets,
                ask_by_bucket={
                    bucket: float(lane_prices.get(bucket, 0.0) or 0.0)
                    for bucket in ordered
                },
                claim_by_bucket={
                    bucket: float(
                        (probabilities_by_bucket or {}).get(bucket, 0.0) or 0.0
                    )
                    for bucket in ordered
                },
                fee_rate=0.0,
            ),
        },
    }


def _centre_anchored_candidates(
    *,
    snapshot: TargetBasketSnapshot,
    ordered: Sequence[str],
    centre_bucket: str,
    purchase_levels: Mapping[str, Sequence[Any]] | None = None,
    prices_by_bucket: Mapping[str, float] | None = None,
    pricing_basis: str = "official_ws_ask_depth",
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
    # What a share costs THIS lane -- the price the edge lean and the share
    # split are both computed against.  Absent (a direct caller, an older
    # test) it is the offer, which is the pre-2026-08-26 basis.
    def levels_for(bucket: str) -> Sequence[Any]:
        return (purchase_levels or {}).get(bucket) or (
            by_bucket[bucket].ask_levels
        )

    def price_for(bucket: str) -> float:
        return float(
            (prices_by_bucket or {}).get(bucket)
            or by_bucket[bucket].ask_price
        )

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
                - price_for(bucket),
            )
    weights: dict[str, float] = {}
    for bucket in ordered:
        row = by_bucket[bucket]
        if not row.ask_available:
            return []
        price = price_for(bucket)
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
        # The weights size the SHARE COUNT, not the cash.  Splitting the cash
        # by weight puts the largest share count on the cheapest rung --
        # shares are cash over price -- so a shape whose whole purpose is to
        # hold its centre ends up holding its outer rung instead.  Settlement
        # pays per share, so that is where the payout curve peaks.
        #
        # Measured on the seat this shape would build for KHOU 2026-08-27
        # (92-93/94-95/96-97/98-99°F at 0.23/0.37/0.32/0.18, weights
        # 0.198/0.247/0.309/0.247, 20 USDC): cash-weighted buys 27.4 shares
        # of the 0.18 shoulder against 19.3 of the centre; share-weighted
        # buys 22.1 of the centre against 17.6 of the shoulder.  ZGSZ
        # 2026-08-17 is the original case -- 4.14 USDC on 30C bought 207
        # shares while 6.51 on the centre bought 50.
        #
        # The ledger has sized shares since 2026-08-21 and production kept
        # splitting cash, so the two built different baskets from the same
        # weights.  Same arithmetic on both sides now (user ruling,
        # 2026-08-26).  The price used to convert is top of book, which is
        # what the ledger's ``_centre_tilt_weights`` is handed; the depth
        # walk below still decides what the clip actually buys.
        unit_cost = sum(
            weights[bucket] * price_for(bucket) for bucket in ordered
        )
        if unit_cost <= EPS:
            continue
        share_scale = budget / unit_cost
        cash = []
        for bucket in ordered:
            raw = share_scale * weights[bucket] * price_for(bucket)
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
                levels=levels_for(bucket),
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
            "pricing_basis": pricing_basis,
        })
    return results

def _continuous_optimum_candidates(
    *,
    snapshot: TargetBasketSnapshot,
    ordered: Sequence[str],
    purchase_levels: Mapping[str, Sequence[Any]] | None = None,
    prices_by_bucket: Mapping[str, float] | None = None,
    pricing_basis: str = "official_ws_ask_depth",
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

    # The ranking's own risk capital, from the one function that defines it,
    # so the generator cannot drift from the score that judges it.  This read
    # ``bankroll / DEFAULT_KELLY_FRACTION`` until 2026-09-09 and therefore
    # asked the solver for four times full Kelly; see
    # ``curve_effective_bankroll_v1``.
    effective_bankroll_usdc = curve_effective_bankroll_v1(
        float(bankroll_usdc), None
    )
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
                levels=(
                    (purchase_levels or {}).get(bucket)
                    or by_bucket[bucket].ask_levels
                ),
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
            "pricing_basis": pricing_basis,
            "candidate_kind": CANDIDATE_KIND_COMPLETE,
            "candidate_origin": "CONTINUOUS_LOG_OPTIMUM",
        })
    return results




def _assert_candidate_support_contract(
    candidates: Sequence[Mapping[str, Any]],
    *,
    ordered_support: Sequence[str],
    unbuyable: Collection[str] = (),
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

    - a complete basket leaves every support bucket held and adds to at
      least one, and every bucket it leaves unbought was already held:
      activation is atomic at the *result* (charter 84, 2026-09-05 -- the
      2026-08-12 reading, "a positive increment on every bucket", refused the
      one candidate that completes a ladder the venue left short);
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
        if kind == CANDIDATE_KIND_COMPLETE:
            increments = {
                bucket: float(shares.get(bucket, 0.0))
                - float(baseline_shares.get(bucket, 0.0))
                for bucket in support
            }
            # Charter 143: a completion buys a single leg's rungs that clear
            # the hurdle and only legs the chain sent; the rest are declined
            # by the rule, not left short by accident.  Only a candidate the
            # rule itself certified.
            completion_143 = row.get("completion_143") or {}
            declined = (
                {str(item) for item in completion_143.get("declined_buckets") or ()}
                if CHARTER_143_COMPLETION == "on"
                and completion_143.get("exempt") is True
                and str(row.get("candidate_origin") or "")
                == LADDER_BALANCE_CANDIDATE_ORIGIN
                else set()
            )
            if (
                not any(value > EPS for value in increments.values())
                or any(
                    increments[bucket] <= EPS
                    and float(baseline_shares.get(bucket, 0.0)) <= EPS
                    # Charter 84 (八): a rung the market prices under the
                    # leg floor is dead and may stay unbought.
                    and bucket not in {str(item) for item in unbuyable}
                    and bucket not in declined
                    for bucket in support
                )
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
    previous_probabilities: Mapping[str, float] | None = None,
    prior_centre_buckets: Sequence[str] = (),
    chain_consolidation_tilt: float | None = None,
    last_acted_probability_identity: str | None = None,
    chain_growth_frame_usdc: float | None = None,
    book_equity_usdc: float | None = None,
    last_decision_signature: Mapping[str, Any] | None = None,
    taf_tx_bucket: str | None = None,
    effective_edge_tables: Mapping[str, Any] | None = None,
    off_centre_claim_bands: Mapping[str, Any] | None = None,
    convergence_evidence: Mapping[str, Any] | None = None,
    hours_to_cutoff: float | None = None,
    completion_budget_usdc: float | None = None,
    capital_ceiling_usdc: float | None = None,
) -> dict[str, Any]:
    """Compile the single-event q* and best no-live action under v2.

    ``capital_available_usdc`` is what sizing may target and
    ``capital_ceiling_usdc`` what may actually go out (charter 141): the
    seat's candidates are sized inside the first, and everything after the
    seat -- the venue-minimum lift, a completion, the execution candidates
    and the bundle's budget gate -- is bounded by the second.  Absent, the
    ceiling is the sizing capital, which is every pre-141 caller's arithmetic.

    ``chain_consolidation_tilt`` is the factor the chain's P0 opener earned,
    handed back by the caller that recorded it; present it wins at every
    stage.  Absent, a P0 decision earns its own from its own lights, and a
    later stage runs untilted -- never its own lights, which the ledger
    priced at -81.8 USDC against opener inheritance (drop-1/2/3 all
    negative): late top-ups are the profitable adds, and lights that came
    on after the opener must not cut them.

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
        # The caller handed no support at all, so there was nothing to
        # enumerate, price or refuse.  Distinct from the enumeration finding
        # nothing admissible (2026-09-08).
        raise ContractError("delta_v2_support_desired_empty")
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
    # The diagnostic center is the probability argmax; the runner-up, lead and
    # stability it reports stay informative.  The anchor itself is no longer
    # the argmax under any stage: charter 75 makes it the mean bucket of the
    # served distribution (the model centre and the argmax are only the
    # fallbacks for a vector with no mass), so the consistency this guards
    # is that the seed's centre is the kernel's mean bucket -- an open-ended
    # edge bucket outranking the 1°C buckets it is compared against (ZUUU:
    # argmax "35°C or higher") is exactly what the mean does not follow.
    probabilities = {row.bucket: row.probability for row in snapshot.buckets}
    expected_center = mean_center_bucket_v1(
        probabilities, {row.bucket: row.ordinal for row in snapshot.buckets}
    )
    if expected_center is None and snapshot.evidence.model_center_ordinal is None:
        expected_center = diagnosed_center
    if expected_center is not None and expected_center != center.bucket:
        raise ContractError("delta_v2_center_diagnostic_mismatch")
    # Charter 71(a): the identity of the belief this compile is served,
    # recorded at commit when it buys and compared here when it holds.
    probability_identity = probability_vector_identity_v1(probabilities)
    chain_empty = all(
        float(row.filled_shares) + float(row.pending_shares) <= EPS
        for row in snapshot.buckets
    )
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
    # The consolidation tilt: a lit light scales this decision's capital to
    # a tenth, floored at one minimum clip per target leg so the cut never
    # quietly becomes a refusal, and never lifted above the untilted
    # allowance.  A stake decision, not a gate -- the enumeration and every
    # window below see the same book either way.  Whose lights decide is
    # the opener's, exactly as the ledger measured it: an inherited factor
    # wins outright, a P0 decision with none earns its own, and a later
    # stage with none runs untilted.
    consolidation_tilt_lit = [
        name for name in CONSOLIDATION_TILT_SIGNALS
        if lit_certainty.get(name)
    ]
    if chain_consolidation_tilt is not None:
        # Charter 139: a factor an opener recorded while the rule was live
        # does not outlive the rule.  With the default at the identity no
        # chain inherits a cut -- KSEA and KAUS 2026-09-17 opened at 0.05
        # the evening before the retirement.
        consolidation_tilt = (
            min(1.0, float(chain_consolidation_tilt))
            if CONSOLIDATION_TILT_FACTOR < 1.0
            else 1.0
        )
        consolidation_tilt_basis = "opener_inherited"
    elif str(snapshot.evidence.probability_stage or "") == "P0":
        consolidation_tilt = (
            CONSOLIDATION_TILT_FACTOR
            if len(consolidation_tilt_lit) >= CONSOLIDATION_TILT_MIN_SIGNALS
            else 1.0
        )
        consolidation_tilt_basis = "p0_own_lights"
    else:
        consolidation_tilt = 1.0
        consolidation_tilt_basis = "no_opener_record"
    capital_before_tilt_usdc = float(capital_available_usdc)
    tilt_floor_usdc: float | None = None
    if consolidation_tilt < 1.0 and capital_before_tilt_usdc > 0.0:
        # The venue's smallest order on each candidate leg, priced at its
        # ask; a leg without an ask is not a leg the seat can buy.
        tilt_floor_usdc = consolidation_tilt_floor_usdc(
            minimum_clip_usdc=(
                min(delta_policy.cash_clip_grid)
                if delta_policy.cash_clip_grid else 0.0
            ),
            legs=CONSOLIDATION_TILT_FLOOR_LEGS,
            leg_minimum_costs_usdc=[
                venue_minimum_shares_v1(
                    min_lot_shares=by_bucket[bucket].min_order_size,
                    ask=by_bucket[bucket].ask_price,
                    minimum_notional_usdc=(
                        EXCHANGE_MINIMUM_ORDER_NOTIONAL_USDC
                        if CHARTER_83_MIN_NOTIONAL == "on" else 0.0
                    ),
                ) * float(by_bucket[bucket].ask_price)
                for bucket in desired
                if by_bucket[bucket].ask_available
            ],
        )
        capital_available_usdc = min(
            capital_before_tilt_usdc,
            max(
                capital_before_tilt_usdc * consolidation_tilt,
                tilt_floor_usdc,
            ),
        )
    # Charter 141: the ceiling that bounds what goes out.  Never below the
    # sizing capital before the tilt, so a caller that passes none keeps its
    # arithmetic, and a completion is never sized under the capital the seat
    # itself was sized from (charter 84 (八)).
    execution_capital_usdc = max(
        capital_before_tilt_usdc,
        float(capital_ceiling_usdc)
        if capital_ceiling_usdc is not None else capital_before_tilt_usdc,
    )
    bankroll_usdc = float(basket_policy.total_budget_usdc)
    # Charter 72: with the book known the size is the whole-basket Kelly
    # stake and the curve keeps only its floor.  Without the book -- a
    # caller that does not know its equity -- the curve form stands, and
    # the audit names which law sized the plan.
    objective_form = (
        KELLY_OBJECTIVE_FORM
        if CHARTER_72_KELLY_SIZING == "on"
        and bankroll_usdc > 0.0
        and book_equity_usdc is not None
        and float(book_equity_usdc) > 0.0
        # Charter 72 off: the pre-2026-09-03 curve, which sized by the
        # log-wealth score rather than the whole-basket Kelly stake.
        else CURVE_OBJECTIVE_FORM
        if bankroll_usdc > 0.0
        else VALUE_OBJECTIVE_FORM
    )
    # The objective sees every window; it seats none of them.
    #
    # Support used to be handed over already chosen, and widening the domain
    # so the objective could see more than one window was worth it: over 37
    # station-days the single window it had been given carried our
    # probability 0.688 against a market price of 0.883, an expected value of
    # -0.195, because the buckets we rate highest are the ones the market
    # prices richest.  Letting it choose among them then went the other way.
    # At its optimal stake the log-wealth score *is* the divergence between
    # belief and price (correlation 0.833 against KL), so choosing on it
    # means choosing where model and market disagree most -- and the settled
    # book is an inverted U in edge, worst exactly at the top.  Two ledger
    # replays put that at ROI +9.7% -> -20.5%.  So: the domain stays wide,
    # the shortlist is ordered by probability mass, its head is the seat, and
    # the objective decides quantities inside it (charter 64).
    support_windows, support_window_audit = _support_windows(
        snapshot=snapshot,
        desired=desired,
        probabilities=probabilities,
        fee_rate=basket_policy.fee_rate,
        **dict(zip(
            ("minimum_support_buckets", "maximum_support_buckets"),
            support_width_bounds_v1(),
        )),
        center_bucket=center.bucket,
        probability_stage=str(snapshot.evidence.probability_stage or ""),
        # The P0-M TAF TX bucket, for the enumerator's TAF arms.  Handed in
        # by the caller that holds the envelope; the paper ledger hands the
        # same value from the same function.
        taf_tx_bucket=taf_tx_bucket,
        # Arm B: the belief and book H hours earlier, handed in by the
        # caller that holds the database; the paper ledger hands the same
        # loader's output.
        convergence_evidence=convergence_evidence,
        # Charter 87: the gate reads lambda off the same served tables the
        # solver used to read it off, as of the same moment.
        effective_edge_tables=effective_edge_tables,
        # Charter 128 v2: the beta lane's lead band, off the same clock the
        # solver's completion cutoff reads.
        hours_to_cutoff=hours_to_cutoff,
        off_centre_claim_bands=off_centre_claim_bands,
        # Charter 122 (五): and the seat band off the table the lane fitted,
        # read the same way.
        # Charter 136 (四): what a broken seat may spend to complete itself.
        completion_budget_usdc=completion_budget_usdc,
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
    # Charter 121 (user ruling, 2026-09-08): charter 93 (二)'s post-lift floor
    # refuses a candidate, not the round.  The floor is re-read after charters
    # 70/83 lift rungs to the venue's lot and dollar minimums, which is work
    # the solver cannot do -- so a winner it certified can fail here, and until
    # now that collapsed ``final_support`` to empty and the day held with the
    # rest of the legal set never asked.  Measured 2026-09-06..08: of 78
    # compiles that offered a completion, 3 were a Kelly row taking a seat a
    # floor-legal completion had also asked for, and those 3 are exactly this
    # shape -- the Kelly row wins the chain (kelly > balance, charter 72), the
    # lifted shape fails the floor, and the completion that would have traded
    # is never reached.
    #
    # The winner chain itself is not touched.  The refused row is excluded and
    # the seat is solved again, so ``kelly_rows`` empties on its own and
    # charter 84's ``elif balance_rows`` becomes reachable for the first time.
    # Bounded, and every attempt re-enters from the same capital: the tilt
    # exemption below mutates it, and a retry that inherited the mutation
    # would size the next candidate against a number no ruling gave it.
    capital_at_seat_entry = capital_available_usdc
    execution_capital_at_seat_entry = execution_capital_usdc
    excluded_candidate_ids: tuple[str, ...] = ()
    restoring_first_post_lift_refusal = False
    completion_143_after_lift: dict[str, Any] | None = None
    for _post_lift_attempt in range(CHARTER_121_POST_LIFT_ATTEMPTS + 1):
        capital_available_usdc = capital_at_seat_entry
        execution_capital_usdc = execution_capital_at_seat_entry
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
                probability_identity=probability_identity,
                last_acted_probability_identity=last_acted_probability_identity,
                chain_growth_frame_usdc=chain_growth_frame_usdc,
                book_equity_usdc=book_equity_usdc,
                last_decision_signature=last_decision_signature,
                effective_edge_tables=effective_edge_tables,
                off_centre_claim_bands=off_centre_claim_bands,
                # Charter 84 (八) as charter 141 bounds it: a completion is
                # sized against the station-day's ceiling -- restoring a seat
                # is a ticket, not a new stake.
                completion_capital_usdc=execution_capital_usdc,
                # Charter 121: rows this seat has already had refused by the
                # post-lift floor, so the winner chain picks among the rest.
                excluded_candidate_ids=excluded_candidate_ids,
                hours_to_cutoff=hours_to_cutoff,
                completion_budget_usdc=completion_budget_usdc,
            )
        )
        # Charter 84 (八): the consolidation tilt cut this seat once, when it
        # opened, and the venue's lot lifted the opener's legs past the cut
        # anyway; cutting the completion again leaves a sliver that cannot
        # cover the sunk rungs (LTAC 31°C and 33°C at 1.7 shares each against
        # 5 held, floor-refused).  Recorded for the audit; since charter 141
        # everything after the seat is bounded by ``execution_capital_usdc``,
        # which is never below the capital the seat was sized from, so the
        # exemption no longer has to move a number.
        completion_tilt_exempt = (
            CHARTER_84_LADDER_BALANCE == "on"
            and str(target_solution.get("winner_candidate_origin") or "")
            == LADDER_BALANCE_CANDIDATE_ORIGIN
            and capital_before_tilt_usdc > float(capital_available_usdc)
        )
        # Charter 136 (四): a completion the risk budget admitted is judged
        # against that budget at the kernel's budget gate too, or the seat
        # the window admitted is refused one step later for the same cash.
        # (In production the budget IS the ceiling since charter 141; a
        # caller that hands a budget without a ceiling still gets it.)
        if (
            completion_budget_usdc is not None
            and str(target_solution.get("winner_candidate_origin") or "")
            == LADDER_BALANCE_CANDIDATE_ORIGIN
            and (
                ((support_selection.get("seated") or {}).get("completion") or {})
                .get("admitted_by")
            ) == COMPLETION_RISK_BUDGET_RULE_VERSION
        ):
            execution_capital_usdc = max(
                float(execution_capital_usdc), float(completion_budget_usdc)
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
        # Charter 70: a seated rung solved to fewer shares than the exchange will
        # fill is raised to one lot when the basket can afford it.  Applied to
        # the *targets*, before any gap is derived from them, so the candidate
        # loop, the kernel's inventory-gap gate and the emitted target rows all
        # see the same lifted number; raising only the clip would trip
        # ``clip_exceeds_inventory_gap`` and refuse the very leg being raised.
        lot_top_up = raise_sub_lot_targets_v2(
            mode=EXCHANGE_LOT_TOP_UP if CHARTER_70_EXCHANGE_LOT == "on" else "off",
            target_shares_by_bucket=target_shares,
            support=seated_support,
            min_lot_by_bucket={
                bucket: by_bucket[bucket].min_order_size
                for bucket in seated_support
            },
            # A rung with no ask cannot be bought, so it cannot be lifted; the
            # kernel leaves a zero-priced rung alone rather than pricing a lot
            # nobody is offering.  ``ask_price`` raises on a bid-only book.
            ask_by_bucket={
                bucket: (
                    by_bucket[bucket].ask_price
                    if by_bucket[bucket].ask_available
                    else 0.0
                )
                for bucket in seated_support
            },
            capital_available_usdc=execution_capital_usdc,
            # Charter 83: the dollar floor rides with the lot; ``off`` is the
            # pre-ruling arm.
            minimum_notional_usdc=(
                EXCHANGE_MINIMUM_ORDER_NOTIONAL_USDC
                if CHARTER_83_MIN_NOTIONAL == "on" else 0.0
            ),
            # Charter 84: the minimum binds on what a rung would *buy*.  A held
            # rung at its target is not an order and is not raised, and the
            # held rungs are not charged against the capital a second time.
            held_shares_by_bucket={
                bucket: (
                    float(by_bucket[bucket].filled_shares)
                    + float(by_bucket[bucket].pending_shares)
                )
                for bucket in seated_support
            },
        )
        target_shares = dict(lot_top_up["target_shares_by_bucket"])
        current_shares = {row.bucket: row.filled_shares for row in snapshot.buckets}
        current_cost = {row.bucket: row.filled_cost_usdc for row in snapshot.buckets}
        pending_shares = {row.bucket: row.pending_shares for row in snapshot.buckets}
        pending_cost = {
            row.bucket: row.pending_notional_usdc for row in snapshot.buckets
        }
        # Charter 93: the floor is re-read on the lifted targets.  The solver
        # judged the equal-share candidate; charters 70/83 then raise rungs to
        # the venue's lot and dollar floor one at a time, and the shape that
        # results was never judged -- KDAL 2026-09-07 went out at 5 / 5 / 6
        # shares for 5.12 and would have lost 0.12 on two of its three rungs
        # while winning.  Judged here on the lifted totals -- the solver's own
        # costs plus each raise at its ask -- and a basket that fails holds by
        # name rather than buying a shape no gate has seen.
        solved_targets = dict(target_solution["target_shares_by_bucket"])
        lifted_cost = dict(target_solution.get("target_cost_by_bucket") or {})
        # The lifted cost is what both post-lift readings charge, so it is
        # derived once here rather than inside either of them.  It used to
        # live inside the charter-93 branch, which meant the edge reading
        # below could not exist without either duplicating the arithmetic or
        # depending on the floor being switched on.
        # Charter 127 (五): a completion that crosses is bought by walking the
        # offer, so its raise is priced on the same walk.  Priced at the top of
        # book, the lifted cost authorized less than the FOK then had to pay on
        # any ladder that steps up, and the completion was refused
        # ``completion_take_above_authorized_cost`` with nothing resting in its
        # place: a 32C rung short five shares, raised to seven for the dollar
        # floor, on 3 @ 0.15 + 100 @ 0.16 authorized 1.07 against a 1.09 walk
        # (audit 2026-09-14; the same shape 71f74f83 fixed for the candidate
        # itself at P0-M).  The post-lift floor and edge below then judge the
        # cost the take will actually be charged; a book too dear there is
        # refused by those gates, by name, not by a price nobody pays.
        lift_walks_the_offer = (
            CHARTER_127_COMPLETION_TAKE == "on"
            and str(target_solution.get("winner_candidate_origin") or "")
            == LADDER_BALANCE_CANDIDATE_ORIGIN
        )
        for bucket in seated_support:
            extra = max(
                0.0,
                float(target_shares[bucket])
                - float(solved_targets.get(bucket, 0.0)),
            )
            if extra <= EPS:
                continue
            ask = (
                by_bucket[bucket].ask_price
                if by_bucket[bucket].ask_available else 0.0
            )
            if lift_walks_the_offer and by_bucket[bucket].ask_available:
                row = by_bucket[bucket]
                held_shares = float(row.filled_shares) + float(row.pending_shares)
                walked = ask_depth_cost_v1(
                    levels=row.ask_levels,
                    shares=max(0.0, float(target_shares[bucket]) - held_shares),
                )
                if walked is not None:
                    lifted_cost[bucket] = (
                        float(row.filled_cost_usdc)
                        + float(row.pending_notional_usdc)
                        + float(walked)
                    )
                    continue
            lifted_cost[bucket] = (
                float(lifted_cost.get(bucket, 0.0))
                + extra * float(ask) * (1.0 + float(basket_policy.fee_rate))
            )
        support_floor_after_lift = None
        # Charter 84 (八): a rung the market prices under the leg floor is dead
        # -- unbought and outside the floor's protected set, exactly as the
        # solver read it.  Read off the seated window, which carries them.
        floor_support = tuple(
            bucket for bucket in seated_support
            if bucket not in set(
                ((support_selection.get("seated") or {}).get("completion") or {})
                .get("market_dead_buckets") or ()
            )
        )
        if CHARTER_93_SUPPORT_FLOOR == "on" and floor_support:
            support_floor_after_lift = support_scenario_floor_v1(
                shares_by_bucket={
                    bucket: float(target_shares[bucket]) for bucket in floor_support
                },
                cost_by_bucket=lifted_cost,
                support=floor_support,
            )
        # Charter 123: the gate that authorized the basket is re-read on the
        # basket that will actually be sent.
        #
        # Charters 78/87/92 clear a window on the whole-basket edge -- the
        # increment's expected payout on the seated lambda*p partition, less
        # the cash it spends -- and they read the SOLVER's shape.  Charters
        # 70/83 then raise rungs to the venue's lot and its dollar minimum,
        # so the basket that goes out is not the basket that was judged.
        #
        # The lift is not one-directional and this clause does not assume it
        # is: it adds shares as well as cost, so raising a rung whose
        # realisable claim beats its ask IMPROVES the edge (measured on the
        # charter-84 fixture: 0.190 solved against 0.265 lifted, raising one
        # rung from five shares to seven).  What the re-read guarantees is
        # only that the number the gate judges is the number the book will be
        # charged for -- which is exactly what charter 93 already guarantees
        # for the floor, and a floor says nothing about the expectation: a
        # basket can keep a legal floor and still expect to lose.
        #
        # This is the same repair charter 121 made on the floor and the maker
        # bundle re-score made on execution sizing: the number that authorizes
        # has to be computed on the number that is sent.  Reusing
        # ``whole_basket_edge_delta_v1`` rather than restating the arithmetic
        # is the point -- an audit that reimplements the rule invents its own
        # findings, and the gate and this re-read must be the same function or
        # they will drift.
        post_lift_edge = None
        # The threshold is the policy's authorization epsilon, not a bare
        # zero, because that is the number the execution layer already
        # refuses below -- one rule, one constant.  Reading it from the same
        # artifact is what keeps this from becoming a second, slightly
        # different edge gate.
        post_lift_edge_epsilon = float(
            delta_policy.execution_weights.epsilon_score
        )
        edge_belief = dict(
            (support_selection.get("seated") or {}).get(
                "window_probabilities"
            ) or {}
        )
        if CHARTER_123_POST_LIFT_EDGE == "on" and edge_belief and floor_support:
            held_now = {
                bucket: (
                    float(by_bucket[bucket].filled_shares)
                    + float(by_bucket[bucket].pending_shares)
                )
                for bucket in seated_support
            }
            held_cost_now = {
                bucket: (
                    float(by_bucket[bucket].filled_cost_usdc)
                    + float(by_bucket[bucket].pending_notional_usdc)
                )
                for bucket in seated_support
            }
            post_lift_edge = whole_basket_edge_delta_v1(
                edge_probabilities=edge_belief,
                current_shares=held_now,
                current_cost=held_cost_now,
                resulting_shares={
                    bucket: float(target_shares[bucket])
                    for bucket in seated_support
                },
                resulting_cost={
                    bucket: float(lifted_cost.get(bucket, 0.0))
                    for bucket in seated_support
                },
            )
        # Charter 143: the completion by held count is re-read on the lifted
        # bundle -- the lot and dollar lifts change what it buys -- by the
        # same function the candidate was certified with.  Exempt, it answers
        # to neither the floor nor the post-lift edge: it fails both by
        # construction wherever the rule was measured to want it.
        completion_143_after_lift = None
        if (
            target_solution.get("winner_completion_rule")
            == COMPLETION_143_RULE_VERSION
        ):
            completion_143_after_lift = completion_143_exemption_v1(
                support=tuple(seated_support),
                held_shares={
                    bucket: float(by_bucket[bucket].filled_shares)
                    + float(by_bucket[bucket].pending_shares)
                    for bucket in seated_support
                },
                held_cost_usdc={
                    bucket: float(by_bucket[bucket].filled_cost_usdc)
                    + float(by_bucket[bucket].pending_notional_usdc)
                    for bucket in seated_support
                },
                target_shares={
                    bucket: float(target_shares[bucket])
                    for bucket in seated_support
                },
                target_cost_usdc={
                    bucket: float(lifted_cost.get(bucket, 0.0) or 0.0)
                    for bucket in seated_support
                },
                claim_by_bucket=edge_belief,
                below_leg_floor_buckets=tuple(
                    ((support_selection.get("seated") or {}).get("completion") or {})
                    .get("market_dead_buckets") or ()
                ),
                hours_to_cutoff=hours_to_cutoff,
                sent_shares_by_bucket=(
                    None
                    if ((support_selection.get("seated") or {}).get("completion") or {})
                    .get("taf_tx_entry")
                    else _completion_143_sent_shares(snapshot)
                ),
            )
        completion_143_exempt = bool(
            completion_143_after_lift and completion_143_after_lift["exempt"]
        )
        # Charter 136: the completion the window admitted on the exposure is
        # not re-refused on the increment's edge after the lift -- the same
        # negative edge it was admitted despite.  Charter 93's floor above
        # still binds: the exposure is what it measures.
        post_lift_edge_exempt = bool(
            str(target_solution.get("winner_candidate_origin") or "")
            == LADDER_BALANCE_CANDIDATE_ORIGIN
            and (
                ((support_selection.get("seated") or {}).get("completion") or {})
                .get("admitted_by")
            ) == COMPLETION_RISK_BUDGET_RULE_VERSION
        ) or completion_143_exempt
        basket_target_activated = (
            target_solution.get("winner_candidate_kind")
            == "COMPLETE_BASKET_TARGET"
            and (
                support_floor_after_lift is None
                or bool(support_floor_after_lift["legal"])
                or completion_143_exempt
            )
            and (
                post_lift_edge is None
                or post_lift_edge_exempt
                or post_lift_edge > post_lift_edge_epsilon + EPS
            )
        )
        if basket_target_activated or CHARTER_121_POST_LIFT_RERANK != "on":
            break
        if restoring_first_post_lift_refusal:
            break
        refused_id = str(target_solution.get("winner_candidate_id") or "")
        if (
            # The baseline takes the seat on its own score; there is no
            # refusal to re-rank around, and excluding a baseline is what
            # charter 93's held-inventory exemption forbids.
            target_solution.get("winner_candidate_kind")
            == "COMPLETE_BASKET_TARGET"
            # The two post-lift readings, either of which is a refusal this
            # retry exists to route around.  Charter 121 was written when the
            # floor was the only one; charter 123 adds the edge, and a seat
            # refused on either should fall to the next candidate rather than
            # collapse to a hold -- the whole argument for the retry.
            and (
                (
                    support_floor_after_lift is not None
                    and not bool(support_floor_after_lift["legal"])
                    and not completion_143_exempt
                )
                or (
                    post_lift_edge is not None
                    and not post_lift_edge_exempt
                    and not post_lift_edge > post_lift_edge_epsilon + EPS
                )
            )
            and refused_id
            and refused_id not in excluded_candidate_ids
            and _post_lift_attempt + 1 < CHARTER_121_POST_LIFT_ATTEMPTS
        ):
            excluded_candidate_ids += (refused_id,)
            continue
        if excluded_candidate_ids:
            # Nothing the exclusions reached could trade either, so the seat
            # holds -- and the record has to be the refusal that started it,
            # not a baseline no rule ever preferred.  Without this the plan
            # would say a completion was never in the running on exactly the
            # days one was offered and the floor turned it down, which is the
            # reading charter 120's ``completion_floor_refused`` was separated
            # out to make possible (LTAC 2026-09-06: 5 / 5 / 15 for 5.40,
            # losing 0.40 inside its own window).  One extra pass, and it
            # reproduces the first attempt exactly because nothing between
            # here and the solve carries state.
            excluded_candidate_ids = ()
            restoring_first_post_lift_refusal = True
            continue
        break
    post_lift_refused_candidate_ids = excluded_candidate_ids
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
    # Hold, do not add to, a bucket whose probability just fell.  The rule
    # lives in the kernel so the paper ledger runs the one implementation
    # rather than a second copy of it -- it was inline here until
    # 2026-09-01, which is why the ledger never ran it at all.
    raw_gap_by_bucket = dict(gap_by_bucket)
    gap_by_bucket, declining_tuple = (
        clamp_declining_gaps_v2(
            gap_by_bucket=gap_by_bucket,
            support=final_support,
            probabilities=probabilities,
            previous_probabilities=previous_probabilities,
        )
        if DECLINE_CLAMP_MODE != "off"
        else (gap_by_bucket, ())
    )
    declining_buckets = list(declining_tuple)
    # Charter 69: under ``guarded`` only the falling rungs at the ends of
    # the window may be held back, and never the centre.  A falling rung in
    # the middle, or the centre itself, keeps its gap -- the basket stays a
    # contiguous ladder that touches its core, which the complete-basket
    # check downstream assumes but does not enforce on this path.
    if DECLINE_CLAMP_MODE == "guarded" and declining_tuple:
        clamped_tuple = contiguous_edge_clamp_v2(
            declining=declining_tuple,
            support=final_support,
            centre_bucket=str(
                support_selection.get("ladder_center_bucket") or ""
            ),
        )
    elif DECLINE_CLAMP_MODE == "held_only" and declining_tuple:
        # Only a rung the book has already committed to -- filled or
        # resting -- can be held at what it has.  An opening commits to
        # nothing yet, so nothing is clamped there.
        clamped_tuple = tuple(
            bucket
            for bucket in declining_tuple
            if current_shares[bucket] + pending_shares[bucket] > EPS
        )
    elif DECLINE_CLAMP_MODE == "after_open" and declining_tuple:
        # Nothing committed anywhere on this station-day = the opening
        # seat: no clamp.  Anything committed = a later seat: charter 51
        # in full.
        book_is_open = any(
            row.filled_shares + row.pending_shares > EPS
            for row in snapshot.buckets
        )
        clamped_tuple = declining_tuple if book_is_open else ()
    else:
        clamped_tuple = declining_tuple
    if tuple(clamped_tuple) != tuple(declining_tuple):
        gap_by_bucket = {
            bucket: (
                gap_by_bucket[bucket]
                if bucket in clamped_tuple
                else raw_gap_by_bucket[bucket]
            )
            for bucket in final_support
        }
    clamped_buckets = list(clamped_tuple)
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
    # Charter 127 (user ruling, 2026-09-11): the designated completion is the
    # one action that crosses, and it crosses as a limit fill-or-kill.
    #
    # A completion exists because the ladder is already broken: the seat holds
    # some legs and not others, and until the short one is bought the basket
    # loses on the very outcome it was built to cover.  A resting rung answers
    # that with "perhaps, eventually", and the measured shape of perhaps is
    # brutal -- of the legs a seat wanted, 60.7% of settled truth landed
    # inside the intended basket against 39.8% inside the one that actually
    # filled, and the whole 20.9 point gap is legs that never came.  So the
    # completion asks for the whole quantity now or not at all.
    #
    # The price it may pay is the price its own authorization was struck on:
    # ``_ladder_balance_candidate`` walked this ladder for exactly this
    # quantity and the solver judged the resulting shape on exactly that
    # cost.  Paying more is buying a basket no gate saw, which is what
    # charters 120 and 123 exist to stop; so a book that has moved against us
    # since the solve does not get a worse fill, it gets no fill.
    completion_take = (
        CHARTER_127_COMPLETION_TAKE == "on"
        and str(target_solution.get("winner_candidate_origin") or "")
        == LADDER_BALANCE_CANDIDATE_ORIGIN
    )
    # The cash each leg is authorized to spend is the LIFTED cost, not the
    # solved one: charters 70/83 raise rungs to the venue's lot after the
    # solve, and charter 123 re-reads the whole-basket edge on the raised
    # shape, so the lifted number is the last one a gate judged and the gap
    # below is the lifted gap.  Capping on the solved cost would refuse every
    # completion the lot raised, which is most of them.
    authorized_cost = dict(lifted_cost)
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
        if completion_take:
            # The cash this leg's completion was authorized to spend, and the
            # walk the venue would charge for it now.
            authorized = (
                float(authorized_cost.get(bucket, 0.0) or 0.0)
                - float(row.filled_cost_usdc or 0.0)
                - float(row.pending_notional_usdc or 0.0)
            )
            walk = ask_depth_walk_v1(levels=row.ask_levels, shares=gap)
            if walk is None:
                maker_candidate_diagnostics[bucket] = (
                    "completion_take_depth_short_of_gap"
                )
                continue
            if authorized <= EPS or float(walk["cash"]) > authorized + 1e-9:
                # Above what authorized it.  Refused by name rather than
                # bought at the new price: this is the whole point of asking
                # for a kill.
                maker_candidate_diagnostics[bucket] = (
                    "completion_take_above_authorized_cost"
                )
                continue
            limit = float(walk["marginal_price"])
            if limit <= EPS or limit >= 1.0:
                maker_candidate_diagnostics[bucket] = (
                    "completion_take_limit_invalid"
                )
                continue
            # The candidate is the GAP, in shares, at the limit -- so its cash
            # is ``gap * limit``, what an FOK for that quantity may pay, not
            # the walk's cash.  Every reader downstream turns a candidate back
            # into shares as ``cash / limit`` (the kernel's maximum shares,
            # the sizing step's order quantity), and on a walk that crosses
            # levels the walk's cash divided by its deepest price is fewer
            # shares than the gap: 5 @ 0.20 + 5 @ 0.30 walks 2.50 for ten
            # shares, and 2.50 / 0.30 went out as an eight-share FOK -- the
            # certified completion that buys short, which charter 120 removed
            # from the search and this descriptor put back (audit 2026-09-14).
            # On a single level the two numbers are the same, so nothing that
            # takes one price moves.  The authorization above still reads the
            # walk: that is what the book charges now; the limit is the most a
            # book that moves in the submit window can charge, and the gates
            # after the lift already judge a take there (charter 127 (六之二)).
            candidate_descriptors.append(
                (bucket, gap * limit, limit, "FOK:0")
            )
            continue
        reservation_grid = _reservation_grid(row=row, artifact=delta_policy)
        maker_rungs = _maker_ladder(
            bid=row.bid_price,
            ask=row.ask_price,
            tick=row.price_tick_size,
        )
        maker_anchor = maker_rungs[0] if maker_rungs else None
        if maker_anchor is None:
            maker_candidate_diagnostics[bucket] = (
                "maker_post_only_price_unavailable_at_min_tick"
                if row.ask_price <= row.price_tick_size + EPS
                else "maker_post_only_market_anchor_unavailable"
            )
        elif not reservation_grid:
            maker_candidate_diagnostics[bucket] = (
                "maker_reservation_grid_empty"
            )
        if (
            EXECUTION_MODE == "maker"
            and reservation_grid
            and maker_anchor is not None
            and MAKER_LADDER_SHAPE == "bid_anchored"
        ):
            # Charter 94: as many rungs as the gap holds whole lots for,
            # the one inside the bid first, each an equal share of the gap,
            # each priced at the resting horizon.  One order per rung and
            # nothing else: the clip grid is the band arm's.
            anchored = bid_anchored_rungs_for_gap(
                rungs=maker_rungs,
                gap_shares=gap,
                min_lot_shares=row.min_order_size,
                minimum_notional_usdc=(
                    EXCHANGE_MINIMUM_ORDER_NOTIONAL_USDC
                    if CHARTER_83_MIN_NOTIONAL == "on" else 0.0
                ),
            )
            for rung in anchored:
                action_clip = (gap / len(anchored)) * rung
                if action_clip <= EPS or action_clip > execution_capital_usdc + EPS:
                    continue
                candidate_descriptors.append(
                    (bucket, action_clip, rung,
                     f"MAKER:{MAKER_RESTING_TIMEOUT_SECONDS:g}")
                )
        for clip in delta_policy.cash_clip_grid:
            if (
                EXECUTION_MODE != "maker"
                or MAKER_LADDER_SHAPE == "bid_anchored"
                or clip > execution_capital_usdc + EPS
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
        # FOK is a single zero-time candidate family, and under charter 65 it
        # is the family production forms.  It was previously admitted only
        # after a confirmed maker cancel and only inside a 120-minute urgency
        # window; both were the pre-charter rule -- FOK as a close-out tool --
        # and neither survives a ruling that makes taker the law.  The cancel
        # gate is doubly spent: it existed so an FOK could not double-buy
        # against a resting maker, and in taker mode nothing rests.
        #
        # What remains are the two conditions that are about the book rather
        # than about policy: the clip has to be absorbable at the ask
        # (``spent >= clip``), and it may not overshoot the inventory gap.
        if EXECUTION_MODE == "taker":
            for clip in delta_policy.cash_clip_grid:
                if clip > execution_capital_usdc + EPS:
                    continue
                # Scale the clip to the gap rather than reject for exceeding
                # it -- the same thing the maker branch above does with
                # ``min(clip, gap * rung)``, and the same rule: never buy
                # past the gap.  FOK expressed that rule by refusing, which
                # was survivable while it was a close-out tool used inside a
                # two-hour window and is not survivable now that it is the
                # family production forms.  The smallest clip on the grid
                # buys 1.25 shares at a 0.20 ask, so on any gap under that
                # the refusal produced no taker candidate at all and the
                # scope simply held.
                action_clip = min(float(clip), gap * row.ask_price)
                if action_clip <= EPS:
                    continue
                shares, spent = _ask_depth_purchase(
                    levels=row.ask_levels,
                    cash_limit=action_clip,
                )
                if spent + EPS >= action_clip and shares <= gap + EPS:
                    candidate_descriptors.append(
                        (bucket, action_clip, row.ask_price, "FOK:0")
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
            try:
                cached = fill_resolver(**kwargs)
            except Exception as exc:  # noqa: BLE001 -- retried once below
                # Charter 94: a resting rung is priced at the resting
                # horizon; an artifact with no row there falls back to the
                # policy's own grid rather than failing the compile.  Any
                # other failure, or a failure of the fallback, is raised.
                fallback = float(delta_policy.timeout_grid_seconds[0])
                if (
                    str(kwargs.get("order_mode") or "").upper() != "MAKER"
                    or abs(float(kwargs.get("timeout_seconds") or 0.0) - fallback) < 1e-9
                ):
                    raise
                try:
                    cached = fill_resolver(
                        **{**kwargs, "timeout_seconds": fallback}
                    )
                except Exception:  # noqa: BLE001
                    raise exc
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
                budget_ok=clip <= execution_capital_usdc + EPS,
                resource_probe_ok=resource_probe_ok,
            ),
            "capital_available_usdc": execution_capital_usdc,
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
        # A crossing leg is judged at the price it will pay.  For the taker
        # family that is the best ask, already on the grid; a charter-127
        # completion's limit is the deepest level its walk consumes, which the
        # grid never held, so on any ladder that steps up the solver answered
        # at the top of book, found a reservation a tick under the limit and
        # dropped the leg (``leg_candidate_rejected_by_reservation_or_size``)
        # -- the second half of the same wrong refusal the lifted cost made
        # (audit 2026-09-14).  Adding the order's own limit changes nothing
        # where the limit is the ask.
        price_grid = (
            _reservation_grid(row=row, artifact=delta_policy)
            if mode == "MAKER"
            else tuple(
                sorted(
                    set(
                        grid_price
                        for grid_price in (
                            *delta_policy.price_grid, row.ask_price, price,
                        )
                        if grid_price + EPS >= row.ask_price
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
        if reserve is None and mode != "MAKER" and TAKER_LEG_VETO != "joint":
            continue
        # ``joint``: no grid price improved the basket on this rung's own
        # marginal reading, and that is not the rung's decision to make.
        # It goes to the joint selector at the ask, where the whole basket's
        # delta -- floor, CVaR, concentration and all -- authorizes or
        # refuses it (charter 35/58/65), exactly as the maker anchor always
        # has.
        # A Maker leg may be individually negative while the complete basket
        # is jointly positive because floor/CVaR/concentration are nonlinear.
        # Keep the non-crossing market anchor as a bounded bundle candidate;
        # only the joint bundle selector may authorize it.
        reservation_price = (
            float(reserve) if reserve is not None else float(price)
        )
        if mode == "MAKER":
            if MAKER_LADDER_SHAPE == "bid_anchored":
                # Charter 94: the rung is the price.  The reservation solve
                # rides along for the audit; charters 65/92 make the
                # whole-basket edge the only law at this layer, and the
                # rung under the bid is a legal deep resting bid.
                maker_limit = _floor_to_tick(float(price), row.price_tick_size)
                bid_floor = row.bid_price - row.price_tick_size
            else:
                maker_limit = min(
                    float(price),
                    _floor_to_tick(reservation_price, row.price_tick_size),
                )
                bid_floor = row.bid_price
            clip = min(float(clip), gap * maker_limit)
            if (
                clip <= EPS
                or maker_limit + EPS < bid_floor
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
    # The complete basket is composed from whichever family was enumerated.
    # This read ``== "MAKER"`` until 2026-08-31 and was the first of the two
    # filters that kept charter 65 out of production: a taker candidate could
    # be built and then never reach the selector at all.
    action_buckets = {
        str(dict(action.get("action") or {}).get("bucket") or "")
        for action in actions
    }
    for bucket in execution_gap_support:
        if bucket not in action_buckets:
            maker_candidate_diagnostics.setdefault(
                bucket,
                "leg_candidate_rejected_by_reservation_or_size",
            )
    # Charter 73: under the Kelly sizing law the bundle is authorized on
    # the whole-basket edge of its increment, read on the seated window's
    # partition; under the curve form the joint value rule stands.
    winner_candidate_origin = str(
        target_solution.get("winner_candidate_origin") or ""
    )
    # Charter 92: one rule for every increment, the ticket included, read
    # off the kernel so the re-price (``runtime.py``) cannot drift from the
    # compile; and the belief it reads is the seated partition alone.  No
    # fallback to the snapshot vector, which no gate has deflated: a plan
    # without a seated partition is refused at the kernel by name.
    execution_authorization_rule = execution_authorization_rule_v1(
        winner_candidate_origin=winner_candidate_origin,
        objective_form=objective_form,
    )
    edge_probabilities = dict(
        (support_selection.get("seated") or {}).get("window_probabilities")
        or {}
    )
    # Charter 116: whether this bundle is a repair of an illegal floor rather
    # than a buy.  Computed here because this is where the held shape, the
    # lifted target and the lane's offers all exist; the kernel only reads
    # the verdict.
    completion_repair = completion_repair_exemption_v1(
        support=tuple(ordered_support),
        held_shares={b: float(current_shares.get(b, 0.0) or 0.0) for b in ordered_support},
        held_cost_usdc={b: float(current_cost.get(b, 0.0) or 0.0) for b in ordered_support},
        target_shares={b: float(target_shares.get(b, 0.0) or 0.0) for b in ordered_support},
        ask_by_bucket={
            b: (float(by_bucket[b].ask_price) if by_bucket[b].ask_available else 0.0)
            for b in ordered_support if b in by_bucket
        },
        # The same belief the authorization reads, six lines above.  Its
        # absence is why every charter 116 exemption recorded a floor gain
        # against a null edge: the clause exists because the increment
        # prices negative where the floor gain is real, and "先只记账不进闸"
        # cannot be honoured by an accounting that books one side.
        claim_by_bucket=edge_probabilities,
        fee_rate=float(basket_policy.fee_rate),
    ) if ordered_support else None
    # Charter 143: a completion by held count carries the same waiver past
    # the bundle's score here and the 5% execution target in ``runtime.py``
    # that a charter-116 repair does -- read on the lifted bundle above, and
    # carried on the plan so the re-score at execution cannot re-judge it on
    # a book that has moved.
    if (
        completion_repair is not None
        and completion_143_after_lift is not None
        and completion_143_after_lift["exempt"]
        and basket_target_activated
    ):
        completion_repair = {
            **completion_repair,
            "exempt": True,
            "exempt_rule": COMPLETION_143_RULE_VERSION,
            "completion_143": completion_143_after_lift,
        }
    selection = select_complete_maker_bundle_v2(
        actions,
        completion_repair=completion_repair,
        required_buckets=execution_gap_support,
        probabilities=probabilities,
        authorization_rule=execution_authorization_rule,
        edge_probabilities=edge_probabilities,
        current_shares_by_bucket=current_shares,
        current_cost_by_bucket=current_cost,
        target_shares_by_bucket=target_shares,
        protected_buckets=ordered_support,
        shortfall_buckets=scored_shortfall,
        breakthrough_buckets=scored_breakthrough,
        value_weights=delta_policy.value_weights,
        execution_weights=delta_policy.execution_weights,
        capital_available_usdc=execution_capital_usdc,
        minimum_profit_floor=minimum_profit_floor,
        fee_rate=basket_policy.fee_rate,
    )
    # The single-bucket FOK close-out that used to sit here is gone with the
    # rule that created it.  It fired only when the maker bundle failed, on a
    # one-bucket gap, after a confirmed cancel -- FOK as an escape hatch from
    # a maker world.  Under charter 65 taker composes the basket itself and
    # answers to the same basket-level EV gate every other family does, so an
    # escape hatch that bypasses the complete-basket requirement is exactly
    # the thing that must not exist.
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
            # Nothing rests under charter 65, so there is never a maker
            # order to have cancelled before this one.
            "cancel_acknowledged": False,
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
    if (
        support_floor_after_lift is not None
        and not support_floor_after_lift["legal"]
    ):
        # Charter 93: the lifted basket would lose inside its own window.
        reason_codes.append("support_floor_after_lift")
    if (
        post_lift_edge is not None
        and not post_lift_edge_exempt
        and not post_lift_edge > post_lift_edge_epsilon + EPS
    ):
        # Charter 123: the lot and dollar lifts moved the basket off the
        # shape charters 78/87/92 cleared, and at the size that would go out
        # the increment no longer pays.  Named apart from the floor: they
        # refuse different baskets and the repairs are different (this one is
        # answered by a cheaper rung or a narrower window, the floor by a
        # different shape).
        reason_codes.append("support_edge_after_lift")
    if (
        objective_form == KELLY_OBJECTIVE_FORM
        and chain_empty
        and str(snapshot.evidence.probability_stage or "") not in CHAIN_OPENING_STAGES
    ):
        # Charter 74: the day was never opened by P0; this stage adds
        # to a position and there is none.
        reason_codes.append("chain_not_opened_by_p0")
    decision_moment_audit = dict(
        support_selection.get("decision_moment") or {}
    )
    if decision_moment_audit.get("is_decision_moment") is False:
        # Charter 77: same model, same admissibility as the last
        # recorded decision -- the quote moved, nothing to decide.
        reason_codes.append("no_decision_moment")
    if not center_stable:
        reason_codes.append("center_probability_lead_unstable")
    delta_v2_audit = {
        "schema": "delta_basket_score_authorization_v2",
        # Charter 140: every window the enumeration offered, refused ones
        # included, projected onto what the lambda and beta lanes fit on.
        # The plan's ``basket_support_selection`` keeps only the shortlist,
        # so until this the whole enumeration survived only in the paper
        # ledger's replay of it; the atomic ledger appends these rows to
        # ``basket_candidate_window_evidence`` with the decision fact.
        "candidate_windows": candidate_windows_v1(
            [
                *support_windows,
                *(support_window_audit.get("refused_priced_windows") or ()),
            ],
            seated_buckets=(support_selection.get("seated") or {}).get(
                "buckets"
            ),
        ),
        # The stake tilt this decision compiled under, so the ledger and a
        # reviewer can prove the sizing basis without re-deriving lights.
        "consolidation_tilt": {
            "factor": consolidation_tilt,
            "basis": consolidation_tilt_basis,
            "signals_lit": consolidation_tilt_lit,
            "capital_before_usdc": capital_before_tilt_usdc,
            "capital_after_usdc": float(capital_available_usdc),
            # Charter 141: what sizing targeted is ``capital_after_usdc``;
            # what the lift, a completion and the budget gate were bounded by.
            "execution_capital_usdc": float(execution_capital_usdc),
            "capital_layer_rule": CAPITAL_LAYER_RULE_VERSION,
            "completion_exempt": bool(completion_tilt_exempt),
            "floor_usdc": tilt_floor_usdc,
            "floor_rule": CONSOLIDATION_TILT_FLOOR_VERSION,
        },
        "selected_actions": selected_evaluations,
        "selected_bundle_sha256": selection.get("selected_bundle_sha256"),
        "authorized": selection.get("authorized") is True,
        "blockers": list(selection.get("blockers", ())),
        "delta_basket_score_v2": float(
            selection.get("selected_score", 0.0)
        ),
        "policy_artifact_id": delta_policy.artifact_id,
        # Charter 71: the belief this compile was served (recorded at
        # commit when it buys), the one the chain last bought under, and
        # the frame the growth gate read -- None means the gate could
        # not bind, which the reader must be able to tell from a pass.
        "served_probability_identity": probability_identity,
        # Charter 72: which law sized this plan, and the book it read.
        "sizing_law": objective_form,
        "winner_candidate_origin": winner_candidate_origin,
        "chain_empty": chain_empty,
        "book_equity_usdc": book_equity_usdc,
        # Charter 77: the signature this decision is recorded under and
        # the one it was compared against.
        "decision_moment": {
            **decision_moment_audit,
            "stage": str(snapshot.evidence.probability_stage or ""),
        },
        # Charter 68(c): the lambda tables this plan was sized under, per
        # scope; None where the kernel constant still bootstraps.
        "effective_edge_tables": dict(
            support_selection.get("effective_edge_tables") or {}
        ),
        # Arm B: the convergence lift's receipt for this compile.
        "convergence_claim": dict(
            support_window_audit.get("convergence_claim") or {}
        ),
        # Charter 118 (一): the market-lean gate's own record, projected
        # where a reader can reach it.  The ruling published the refused
        # windows into ``_support_windows``' audit, and that audit is not
        # persisted -- the plan lifts three keys off it and drops the rest,
        # so the list, the lean the decision saw, and the shadow arm's count
        # all died with the dict.  ``window_gate_counts`` below carries the
        # ENFORCED count only, which is zero whenever the gate is in shadow:
        # the arm that most needs a record was the one leaving none, which
        # is the failure 118 (一) was written to end.
        "market_lean": {
            "mode": str(
                support_window_audit.get("market_lean_gate_mode") or ""
            ),
            "min_buckets": MARKET_LEAN_MIN_BUCKETS,
            "lean_ordinal": support_window_audit.get("market_lean_ordinal"),
            "refused_window_count": int(
                support_window_audit.get(
                    "market_lean_refused_window_count"
                ) or 0
            ),
            "shadow_refused_window_count": int(
                support_window_audit.get(
                    "market_lean_shadow_refused_window_count"
                ) or 0
            ),
            # Bounded by MARKET_LEAN_REFUSAL_RECORD_MAX at the gate; the
            # counts above stay exact, so a truncated list never reads as
            # the whole.
            "refused_windows": [
                dict(row)
                for row in (
                    support_window_audit.get("market_lean_refused_windows")
                    or ()
                )
            ],
        },
        # Charter 91/103: the floor this compile screened its legs with,
        # and the funnel saying how many windows each gate refused.
        #
        # Both already existed -- in ``_support_windows``' own audit, which
        # the plan does not persist.  Measured 2026-09-07 while trying to
        # prove charter 103 was live: 0 of the 400 most recent preselection
        # plans carry ``leg_floor_rule``, and the ``window_gate_counts``
        # written beside a blocked scope is null in all 253 rows that have
        # the key, because it is read off an exception the audit-blocker
        # path never raises.  So the belief scaling (charter 91), the price
        # basis (charter 103) and the count of what they refused were all
        # unprovable from a production receipt; the ledger's fingerprint
        # carried them and production's did not, which is exactly the
        # asymmetry the two books are supposed to not have.
        "leg_floor": {
            "rule": LEG_FLOOR_RULE_VERSION,
            "price_basis": LEG_FLOOR_PRICE_BASIS,
            "minimum_all_in_ask": MINIMUM_LEG_ALL_IN_ASK,
            "refused_window_count": int(
                support_window_audit.get("leg_floor_refused_window_count") or 0
            ),
            "excluded_buckets": list(
                support_window_audit.get("leg_floor_excluded_buckets") or ()
            ),
        },
        "window_gate_counts": window_gate_counts_v1(support_window_audit),
        "charter_71": {
            "last_acted_probability_identity": (
                last_acted_probability_identity
            ),
            "chain_growth_frame_usdc": chain_growth_frame_usdc,
        },
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
        # The day's shape, written down (user ruling, 2026-09-06).  Charter
        # 75 reads it to place the window and charter 79 seats a symmetric
        # day at centre+-1, so it already governs; it just left no trace,
        # and a board that wanted to show it would have had to recompute the
        # mean and the mode from the served vector -- re-deriving a rule the
        # plan applied, which is how an audit invents its own findings.  The
        # sign is the same number the gates consume: one implementation in
        # the kernel, read here through its reading form.
        #
        # Audit only.  Nothing downstream branches on this field, and the
        # economic decision key is a projection of named fields that does
        # not include it, so recording it cannot move a decision.
        "skew_reading": mean_mode_skew_reading_v1(
            probabilities,
            {row.bucket: int(row.ordinal) for row in snapshot.buckets},
                threshold_sigma=SKEW_GAP_THRESHOLD_SIGMA,
        ),
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
        # Charter 116/73: the re-score authorizes on the state the compile
        # authorized on, so the repair verdict travels with the plan rather
        # than being recomputed against a book that has since moved.
        "completion_repair": completion_repair,
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
        "execution_authorization_rule": execution_authorization_rule,
        "basket_winner_candidate_origin": winner_candidate_origin,
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
        "probability_clamp_mode": DECLINE_CLAMP_MODE,
        "probability_clamped_buckets": tuple(clamped_buckets),
        # Charter 70: which rungs were lifted to one exchange lot, and what
        # the lift cost, or why it was refused.
        "exchange_lot_top_up": EXCHANGE_LOT_TOP_UP,
        "lot_raised_buckets": tuple(lot_top_up["raised_buckets"]),
        "lot_raise_usdc": float(lot_top_up["lot_raise_usdc"]),
        "lot_raise_refused": lot_top_up["refused_reason"],
        # Charter 93: the floor read again on the lifted targets, and the
        # verdict that held the plan when it failed.
        "support_scenario_floor_rule": SUPPORT_SCENARIO_FLOOR_RULE_VERSION,
        "support_scenario_floor_after_lift": support_floor_after_lift,
        # Charter 123: the whole-basket edge read again on the same lifted
        # targets.  Recorded whether it passed or failed, so "how much did the
        # lift cost this basket" is answerable from a plan -- ``None`` means
        # the reading could not be taken (no seated belief, or the switch is
        # off), which a reader must be able to tell from a pass.
        "post_lift_edge_rule": POST_LIFT_EDGE_RULE_VERSION,
        "post_lift_edge_mode": CHARTER_123_POST_LIFT_EDGE,
        "post_lift_edge_exempt_rule": (
            (
                COMPLETION_143_RULE_VERSION
                if completion_143_after_lift is not None
                and completion_143_after_lift["exempt"]
                else COMPLETION_RISK_BUDGET_RULE_VERSION
            )
            if post_lift_edge_exempt else None
        ),
        # Charter 143: the completion by held count, as read on the lifted
        # bundle; ``None`` when the winner was not one.
        "completion_143_mode": CHARTER_143_COMPLETION,
        "completion_143_after_lift": completion_143_after_lift,
        "completion_budget_usdc": completion_budget_usdc,
        "completion_risk_budget": (
            ((support_selection.get("seated") or {}).get("completion") or {})
            .get("completion_risk_budget")
        ),
        "whole_basket_edge_after_lift": post_lift_edge,
        "post_lift_edge_epsilon": post_lift_edge_epsilon,
        # Charter 83: the dollar floor the lift read, and which rungs it
        # (rather than the lot) lifted.
        "exchange_minimum_order_notional_usdc": float(
            lot_top_up.get("minimum_notional_usdc", 0.0)
        ),
        "notional_raised_buckets": tuple(
            lot_top_up.get("notional_raised_buckets", ())
        ),
        "probability_direction_source": (
            "previous_checkpoint" if previous_probabilities else "absent"
        ),
        "execution_mode": EXECUTION_MODE,
        # Charter 94: the ladder's shape and the horizon its fills are priced at.
        "maker_ladder_shape": MAKER_LADDER_SHAPE,
        "maker_resting_timeout_seconds": MAKER_RESTING_TIMEOUT_SECONDS,
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
