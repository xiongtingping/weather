#!/usr/bin/env python3
"""Paper-trade every station daily and score it after settlement.

What this answers: if the seat the strategy actually picks were bought with
real money, what would the book look like?  It is a measurement lane, not a
trading lane -- it authorizes nothing, submits nothing, and writes only its
own two tables.

Faithfulness is the whole point, so the selection is not re-implemented
here.  Each scope is priced from the same ``basket_official_ws_snapshot``
rows production reads, run through the same ``_support_windows`` ladder
enumeration, and seated on the same ordering -- whatever that ordering
currently is.  It was the KL divergence between belief and the market's
implied odds until 2026-08-17, when the measured cost of ranking on any
belief-minus-price quantity retired it in favour of probability mass; the
head of the list is the seat either way, and this module deliberately does
not know which key produced it.

Each stage is scored as an independent entry rather than as one position
adjusted three times.  Production opens at P0 and adjusts at P0-M and P1
(charter 46), so a single running position would mix "was the P0 decision
good" with "was the adjustment good".  Scoring them separately answers both:
the P0 lane is the headline account, and the P0-M/P1 lanes say whether the
later evidence would have entered better.  Probability drift across the
stages is recorded on every row so the movement itself can be read.

Sizing is fractional Kelly on the basket as a binary bet.  A basket costing
C all-in and paying 1 when the truth lands inside it has Kelly fraction
(p - C)/(1 - C); the served fraction is a quarter of that (the same f the
live objective uses), capped per scope so no single station-day can take a
large bite of the book.

    python3 scripts/paper_trading_ledger.py --backfill   # score settled days
    python3 scripts/paper_trading_ledger.py --report     # print the book
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
import math
import os
import sqlite3
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, NamedTuple
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import metar_ceiling  # noqa: E402
from bayes_basket_target import regional_path_prior  # noqa: E402
from bayes_basket_target import regional_point_debias  # noqa: E402
from bayes_basket_target import metar_four_layer  # noqa: E402
from bayes_basket_target.center_width_calibration import (  # noqa: E402
    apply_center_width_calibration,
    bucket_centers_from_intervals,
)
from bayes_basket_target.center_width_store import EDGE_WIDTH_C  # noqa: E402
from bayes_basket_target.convergence_evidence import (  # noqa: E402
    load_convergence_evidence,
)
from bayes_basket_target.off_centre_claim_store import (  # noqa: E402
    load_off_centre_tables,
)
from bayes_basket_target.effective_edge_store import (  # noqa: E402
    load_effective_edge_tables,
)
from bayes_basket_target.basket_kernel.contracts import (  # noqa: E402
    TargetBasketSnapshot,
)
from bayes_basket_target.basket_kernel.delta_v2 import (  # noqa: E402
    EFFECTIVE_EDGE_P1_STAGES,
    EFFECTIVE_EDGE_P0M_STAGES,
    EFFECTIVE_EDGE_STAGES,
    ORDER_SHARE_GRID,
    ORDER_SHARE_STEP_SHARES,
    clamp_declining_gaps_v2,
    contiguous_edge_clamp_v2,
    effective_edge_lambda,
    floor_to_order_share_grid_v1,
    effective_edge_lambda_for_stage,
    effective_edge_lambda_from_tables,
    effective_edge_lambda_resolved,
    CLAIM_EDGE_THERMOMETER_MODE,
    CLAIM_EDGE_THIN_BAND_MAX,
    CLAIM_EDGE_THIN_BAND_LAMBDA,
    effective_edge_bucket_lambda_for_stage,
    probability_core,
)
from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.pass_deadline import writer_lease_seconds
from bayes_basket_target.settlement_time import (  # noqa: E402
    hours_to_settlement_if_known,
    settlement_day_window,
)
from bayes_basket_target.delta_v2_runtime import (  # noqa: E402
    CENTRE_CONTAIN_GATE as _CENTRE_CONTAIN_GATE,
    support_width_bounds_v1 as _support_width_bounds_v1,
    CHARTER_127_COMPLETION_TAKE as _CHARTER_127_COMPLETION_TAKE,
    TARGET_PRICING_BASIS as _TARGET_PRICING_BASIS,
    TARGET_PRICING_LANE_STAGES as _TARGET_PRICING_LANE_STAGES,
    COMPLETION_CROSSING_PRICING_RULE_VERSION as _COMPLETION_CROSSING_PRICING,
    CHARTER_136_COMPLETION as _CHARTER_136_COMPLETION,
    # Charter 143 v2: the send history a completion reads, off the snapshot.
    _completion_143_sent_shares,
    CENTRE_ANCHOR_DISTANCE_DECAY,
    CENTRE_ANCHOR_EDGE_TILT,
    CENTRE_ANCHOR_MIN_RUNG_SHARE,
    CONTINUOUS_OPTIMUM_CANDIDATES_ENABLED,
    MAKER_COVERAGE_RISK_BUDGET,
    MAKER_COVERAGE_VALUE_LAMBDA,
    LADDER_CENTER_DEFINITION,
    MAKER_LADDER_IMPROVE_TICKS,
    MAKER_LADDER_STEPS,
    # Charter 94: the ladder's shape and its two builders are production's.
    MAKER_LADDER_SHAPE,
    bid_anchored_ladder,
    bid_anchored_rungs_for_gap,
    CERTAINTY_GATE_SIGNALS,
    MINIMUM_LEG_ALL_IN_ASK,
    SEAT_MAX_BUCKETS,
    SEAT_MIN_BUCKETS,
    SEAT_SHAPE_RULE_VERSION,
    MARKET_LEAN_GATE_MODE,
    SKEW_GAP_THRESHOLD_SIGMA,
    KELLY_SHARE_SHAPE,
    KELLY_SHAPE_TOPUP,
    P0M_TAF_ENVELOPE_RULE_VERSION,
    P0M_TAF_MODE,
    CENTER_WINDOW_CLAIM_BANDS,
    SIZING_CLAIM_BANDS,
    CONVERGENCE_CLAIM_MODE,
    CONVERGENCE_CLAIM_FACTOR,
    CONVERGENCE_CLAIM_HORIZON_HOURS,
    CONVERGENCE_CLAIM_RULE_VERSION,
    CONVERGENCE_CLAIM_STAGES,
    CONVERGENCE_CLAIM_MIN_AGREEMENT,
    CONVERGENCE_CLAIM_MIN_CLAIM_RISE,
    CONVERGENCE_CLAIM_MIN_PRICE_FALL,
    CHAIN_OPENING_STAGES,
    DECLINE_CLAMP_MODE as _PRODUCTION_DECLINE_CLAMP_MODE,
    # The charter clauses that gained a research switch on 2026-09-05.
    # Read from production so a replay arm flips one rule on both sides.
    CHARTER_66A_CHAIN_PAR,
    CHARTER_70_EXCHANGE_LOT,
    CHARTER_83_MIN_NOTIONAL,
    CHARTER_84_LADDER_BALANCE,
    CHARTER_71A_TOPUP_IDENTITY,
    CHARTER_71B_CHAIN_GROWTH,
    CHARTER_72_KELLY_SIZING,
    CHARTER_77_DECISION_MOMENTS,
    CHARTER_78_INCUMBENT_NO_EXEMPTION,
    # Charter 87 (2026-09-06): the gate reads the lifted claim times
    # lambda(a); read from production so an arm flips it on both sides.
    CHARTER_87_GATE_EFFECTIVE_EDGE,
    WINDOW_REFUSAL_FUNNEL,
    # The pieces of arithmetic the two sides must not each own a copy of.
    # Every one of them used to be duplicated here, and the ladder copy had
    # measurably drifted (see ``_resting_ladder``).  The per-leg value clamp
    # is no longer among them: production retired it on 2026-09-03 (charter
    # 63, dead under charter 65); the maker_resting research arm below keeps
    # its own lambda.
    _floor_to_tick,
    _maker_ladder,
    _support_windows,
    # The production judge arm (2026-09-03): the objective that seats a
    # window and sizes it in one pass, and the tilt floor it spends under.
    _solve_over_support_windows,
    CHARTER_121_POST_LIFT_RERANK,
    EFFECTIVE_EDGE_SCOPE as _PRODUCTION_EDGE_SCOPE,
    CHARTER_123_POST_LIFT_EDGE,
    CHARTER_124_ROBUST_CLAIM,
    ROBUST_CLAIM_DIVERGENCE_BUDGET_NATS,
    CHARTER_121_POST_LIFT_ATTEMPTS,
    CONSOLIDATION_TILT_FLOOR_LEGS,
    # The P0-M TAF TX bucket the enumerator's TAF arms read: one function,
    # handed to ``_support_windows`` by both compiles (2026-09-04).
    taf_tx_bucket_for as _taf_tx_bucket,
)
from bayes_basket_target.observed_high_floor import (  # noqa: E402
    # Charter 125: the day's observed high as a floor on what may be bought,
    # at every stage.  Production and this ledger read the same predicate.
    CHARTER_125_OBSERVED_HIGH_FLOOR,
    dead_buckets_for_upper_bounds,
    observed_high_floor_enabled,
)
from bayes_basket_target.resting_ladder_lifecycle import (  # noqa: E402
    # Charter 94: the cadence production re-anchors resting rungs on.
    RESTING_REPRICE_DWELL_SECONDS,
    RESTING_REPRICE_FAR_TICKS,
)
from bayes_basket_target.basket_kernel.delta_v2 import (  # noqa: E402
    # Charter 110 (三): the per-rung decision is one kernel function, called
    # by production's lifecycle and by this lane's re-post alike.
    LEG_ACTION_CANCEL,
    LEG_ACTION_REPRICE_UP,
    RESTING_RUNG_ACTION_RULE_VERSION,
    resting_rung_action_v1,
    CONSOLIDATION_TILT_FLOOR_VERSION,
    LEG_FLOOR_PRICE_BASIS,
    LEG_FLOOR_RULE_VERSION,
    consolidation_tilt_floor_usdc,
    leg_below_floor,
    venue_minimum_shares_v1,
    DEFAULT_KELLY_FRACTION,
    DEFAULT_P1_ENTRY_LOCAL_HOUR,
    EXCHANGE_LOT_TOP_UP as _KERNEL_EXCHANGE_LOT_TOP_UP,
    EXCHANGE_LOT_TOP_UP_MODES,
    MAX_STAKE_FRACTION_PER_SCOPE,
    OFF_CENTER_CLAIM_RAMP_BUCKETS,
    OFF_CENTRE_CLAIM_MODE,
    BETA_EDGE_MODE,
    BETA_EDGE_RULE_VERSION,
    STATION_DAY_HARD_FRACTION,
    CAPITAL_LAYER_RULE_VERSION,
    station_day_ceiling_usdc,
    ECONOMIC_HURDLE_V1,
    ECONOMIC_HURDLE_RULE_VERSION,
    EXCHANGE_MINIMUM_ORDER_NOTIONAL_USDC,
    raise_sub_lot_targets_v2,
    scope_stake_cap_usdc,
    # Charter 137: the basis the Kelly target level is sized against.  One
    # definition, read by production's candidate generator and by this
    # lane's own sizer, so the two cannot drift onto different arms.
    KELLY_TOPUP_BASIS,
    KELLY_TOPUP_BASIS_RULE_VERSION,
    kelly_target_budget_usdc_v1,
    # Charter 71: the top-up gates' partition and growth number are the
    # kernel's, and the version is hashed into the rule fingerprint.
    CHARTER_71_TOPUP_GATE_VERSION,
    chain_topup_growth_v1,
    deflate_window_partition_v1,
    probability_vector_identity_v1,
)
from bayes_basket_target.basket_kernel.valuation_v1 import (  # noqa: E402
    LCB_QUANTILE as VALUATION_LCB_QUANTILE,
    MARKET_REFERENCE_RULE as VALUATION_MARKET_REFERENCE_RULE,
    VALUATION_MODE,
    VALUATION_RULE,
)
# What the production judge arm resolves the way production's compile does:
# the station's basket policy, the delta-v2 policy artifact, and the scope
# bins both are keyed by.  Every name here is production's own resolver.
from bayes_basket_target.basket_kernel.contracts import (  # noqa: E402
    ContractError as _KernelContractError,
    EPS as _KERNEL_EPS,
    TargetBasketPolicy,
)
from bayes_basket_target.basket_kernel.delta_v2 import (  # noqa: E402
    CURVE_BANKROLL_CONVENTION as _CURVE_BANKROLL_CONVENTION,
    LADDER_BALANCE_CANDIDATE_ORIGIN as _LADDER_BALANCE_ORIGIN,
    CURVE_OBJECTIVE_FORM as _CURVE_OBJECTIVE_FORM,
    KELLY_OBJECTIVE_FORM as _KELLY_OBJECTIVE_FORM,
    VALUE_OBJECTIVE_FORM as _VALUE_OBJECTIVE_FORM,
    whole_basket_kelly_stake_v1,
    EDGE_STATE_ADMISSIBLE,
    EFFECTIVE_EDGE_BAND_BOUNDS,
    DECISION_MOMENT_RULE_VERSION,
    claim_transform_fingerprint,
    basket_holding_signature_v1,
    edge_state_v1,
    is_decision_moment_v1,
    # Charter 73: the increment's edge on the belief the seat was judged
    # on, the number production's bundle re-score authorizes on.
    whole_basket_edge_delta_v1,
    # Charter 116/136: the repair reading, and the rule a window records when
    # it admitted a completion on the exposure rather than on its edge.
    completion_repair_exemption_v1 as _completion_repair_exemption_v1,
    COMPLETION_RISK_BUDGET_RULE_VERSION as _COMPLETION_RISK_BUDGET_RULE,
    completion_budget_usdc_v1 as _completion_budget_usdc_v1,
    # Charter 143: the completion by held count -- the rule a window records
    # when it admitted one, and the exemption read on the lifted legs.
    CHARTER_143_COMPLETION,
    COMPLETION_143_RULE_VERSION as _COMPLETION_143_RULE,
    completion_143_exemption_v1 as _completion_143_exemption_v1,
    # Charter 92: the ticket is authorized on that same edge; ``off`` is
    # charter 74's structural authorization, the replay arm.
    # Charter 93: the in-support floor, re-read on the lifted legs with the
    # kernel's own function, and the fraction it reads.
    CHARTER_93_SUPPORT_FLOOR,
    SUPPORT_SCENARIO_FLOOR_FRACTION,
    support_scenario_floor_v1,
    EFFECTIVE_EDGE_LAMBDA_P0_OVERRIDE,
    DUST_FILL_FLOOR_USDC,
    # User ruling 2026-09-08: the two-段 share shape.  Imported, never
    # recomputed -- ``_centre_tilt_weights`` below is the standing example of
    # what a second copy costs.
    floor_first_edge_weights_v1,
    effective_edge_bucket_lambda_for_stage,
    KELLY_EDGE_TILT_FRACTION,
)
from bayes_basket_target.basket_kernel.support import (  # noqa: E402
    _choose_support,
    _derive_dual_risk_scope,
    mean_center_bucket_v1,
)
from bayes_basket_target.contracts import ProbabilityEnvelope  # noqa: E402
from bayes_basket_target.contracts import probability_knowable_at  # noqa: E402
from bayes_basket_target.contracts import ContractError  # noqa: E402
from bayes_basket_target.metar_trajectory import (  # noqa: E402
    assert_p1_execution_evidence,
)
from bayes_basket_target.delta_v2_artifact import (  # noqa: E402
    resolve_delta_v2_policy,
)
from bayes_basket_target.policy import resolve_station_policy  # noqa: E402
from bayes_basket_target.production import (  # noqa: E402
    # Charter 127 (二): the venue's own floor on a GTD's life.  Production
    # names it because a belief with seconds left would otherwise mint an
    # order the gateway refuses; this lane reads the same number so the two
    # cannot come to disagree about what the venue accepts.  The signing
    # margin rides with it: production signs the floor plus the margin, so
    # the order this lane books lives exactly as long as the one production
    # would have sent.
    GTD_SIGNING_MARGIN_SECONDS,
    VENUE_GTD_MINIMUM_HORIZON_SECONDS,
)
from bayes_basket_target.production_io import (  # noqa: E402
    derive_policy_maturity_bin,
    load_production_artifacts,
)
from bayes_basket_target.runtime import (  # noqa: E402
    _DEFAULT_POLICY as _PRODUCTION_DEFAULT_POLICY,
    _capital_layer_policy_bounds,
    snapshot_from_probability_envelope,
)
from bayes_basket_target.station_season import station_season_bin  # noqa: E402
from bayes_basket_target.center_width_calibration import (  # noqa: E402
    apply_center_width_calibration,
    bucket_centers_from_intervals,
)

DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
STAGES = ("P0", "P0-M", "P1")
INITIAL_CAPITAL_USDC = float(
    os.environ.get("PAPER_LEDGER_INITIAL_CAPITAL", "") or 1000.0
)
# The live objective's fractional Kelly.  A quarter of full Kelly is the
# stake that made the curve score concave enough to price variance without
# refusing bets that cannot hurt the book.  Taken from the kernel rather
# than written again here: it is the same quarter production's curve score
# uses, and two copies of one number is how they stop being one number.
KELLY_FRACTION = DEFAULT_KELLY_FRACTION
# Charter 70 (2026-09-02): a rung solved under the exchange's lot is lifted to
# one lot when the basket can pay.  Kernel-owned and read by production; the
# binding test asserts the two lanes equal.  ``off`` is the pre-charter-70
# book, kept as a replay arm so it can be re-derived, never run in production.
EXCHANGE_LOT_TOP_UP = (
    os.environ.get("PAPER_LEDGER_EXCHANGE_LOT_TOP_UP", "").strip()
    or _KERNEL_EXCHANGE_LOT_TOP_UP
)
# The exchange's stated minimum order, in shares, for a snapshot that does
# not carry ``min_order_size`` (user ruling 2026-09-04).  No official WS
# snapshot carried the field before 2026-08-31; the market registry has
# stated 5.0 on every one of its 602,756 rows since the sizing rule landed
# on 2026-08-30, and the venue enforced the same five shares on the days
# before it.  Production fails closed on an unstated lot; a replay of
# those days must not read them as lot-free, which is what a zero here
# would do (the kernel leaves an unstated rung alone).  The snapshot's own
# value wins whenever it is present.
EXCHANGE_MINIMUM_LOT_SHARES_FALLBACK = 5.0
# Which allowance charter 70's lift reads under the production judge.
# ``untilted`` is production: ``cash_phase`` lifts a sub-lot leg against the
# scope's remaining allowance, which the consolidation tilt never touches
# -- so a ticket the tilt cut to 0.75 is lifted to ~3.5 anyway.  ``tilted``
# is the replay arm that asks what the tilt would be worth if the lift
# respected it; measured, not run (open ruling P1, 2026-09-04).
LOT_LIFT_BUDGET = (
    os.environ.get("PAPER_LEDGER_LOT_LIFT_BUDGET", "").strip().lower()
    or "untilted"
)
if LOT_LIFT_BUDGET not in {"untilted", "tilted"}:
    raise SystemExit(f"unknown lot lift budget arm: {LOT_LIFT_BUDGET}")
if EXCHANGE_LOT_TOP_UP not in EXCHANGE_LOT_TOP_UP_MODES:
    raise ValueError(f"unknown exchange lot top-up mode: {EXCHANGE_LOT_TOP_UP}")
# Charter 66 (user ruling, 2026-08-29): a top-up is sized and admitted on
# the CHAIN, not on itself.  Stage-level positive EV built 38 chains whose
# blended cost crossed 1.0 -- guaranteed losses that hit 37 times out of 38
# and still returned -31.9 USDC -- because each leg passed its own gate and
# nobody priced the whole book they were building.  Two clauses:
#
#   A. the chain's blended cost after the buy must stay under par.  This
#      gate does not read the model's claim at all: at or above 1.0 no
#      outcome returns the stake, which is arithmetic, and it is the only
#      clause that survives the claim being wrong (these chains claimed
#      ~0.99 and realized 0.974).
#   B. the buy must raise the chain's expected log growth from here --
#      sunk cost excluded, the held payout curve as the initial condition.
#      Sized by the same solver production's generator uses, so the two
#      lanes cannot drift: the log-optimal *increment* from inventory,
#      which is zero exactly when the chain's curve cannot be improved.
#
# The bankroll the top-up curve is shaped against is the scope's own risk
# capital (the per-scope fraction of the book's equity), overridable for
# replay arms; the chain's spend rides inside it as sunk, per the solver's
# contract.  A miss-mass floor is carried as a replay arm and off by
# default: gate A already caps what a wrong near-1 claim can cost.
CHAIN_TOPUP_BANKROLL_USDC = float(
    os.environ.get("PAPER_LEDGER_TOPUP_BANKROLL", "") or 0.0
)  # 0 = the station-day's ceiling, equity * STATION_DAY_HARD_FRACTION
CHAIN_TOPUP_MISS_FLOOR = float(
    os.environ.get("PAPER_LEDGER_TOPUP_MISS_FLOOR", "") or 0.0
)
CHAIN_COST_PAR_BOUND = 1.0
# Charter 67 (user ruling, 2026-08-30): P1 may not take its seat before the
# day has anything to say.  The P1 lane starts compiling at local midnight
# and the entry walk consumed its one top-up at the first cycle whose
# objective seated -- ZSQD 2026-08-29 bought 30+31 six minutes after local
# midnight on one midnight observation, and the truth (28) was being priced
# by the market from mid-morning.  Measured over the 117 settled multi-stage
# chains: P1 seats taken before local 11:00 contributed -49.5 USDC net of
# the P0-only counterfactual (54 overnight -12.2, 26 morning -25.0), seats
# taken 11:00-16:00 local contributed +99.8 (+38.7 with the best chain
# removed; permutation p=0.006 on the mean split).  The gate holds P1's
# budget until the local hour the daily maximum actually forms in, spending
# the stage's information advantage where it exists.  0 disables.
# Research arm only (2026-08-30): hold P0 for this many hours after its
# lane's first cycle -- "how long after listing is the best entry" is the
# question, asked the only way this house accepts answers: by replaying the
# book under each candidate delay.  0 disables and is the default; the
# production rule (enter at the first cycle whose objective seats) is
# unchanged until an arm wins and is ruled on.
P0_ENTRY_DELAY_HOURS = float(
    os.environ.get("PAPER_LEDGER_P0_ENTRY_DELAY_HOURS", "") or 0.0
)
P1_ENTRY_LOCAL_HOUR = float(
    os.environ.get("PAPER_LEDGER_P1_ENTRY_LOCAL_HOUR", "")
    or DEFAULT_P1_ENTRY_LOCAL_HOUR
)
# The per-scope cap and the per-stage shares both live in the kernel now
# (user ruling, 2026-08-26): production read a flat 30 USDC per station while
# this book read 2% of its own equity, so the same station-day was sized two
# different ways.  Imported above, defined in exactly one place.
# One book for the whole network and one position per station-day (user
# ruling, 2026-08-18).  P0 opens it, P0-M and P1 top it up, and it settles
# once.
#
# Read as three books -- a bankroll each, a settlement each -- the ledger
# answered "which moment's evidence would have entered best", which is a
# real question and not this one.  It also charged the same event two and
# three times: of 152 settled station-days, 40 were scored more than once
# against a single truth, and the three books together staked 2220 USDC out
# of a nominal 1000.
#
# Top-ups only add (user ruling, 2026-08-18).  A later stage may widen the
# ladder or deepen a rung it already holds; it may not sell one back.  What
# that costs is honesty in the other direction: when P1's ladder converges
# from four rungs to two, the book keeps carrying the rungs the hard bound
# has since ruled out, all the way to settlement.  What it buys is that no
# fill is ever priced against a bid this lane cannot measure.
BOOK_MODEL = "one_book_per_station_day_top_up_only"
# How much of the station-day's cap each stage may have spent by the time it
# is done (user ruling, 2026-08-19).
#
# One cap for the chain is right -- three stages each taking 2% is a 6%
# position in one outcome -- but a single running total lets the opening
# spend all of it, and it does: ZUUU 2026-08-19 opened at 10.63 against a
# 10.6 cap and P1 was refused for having no room left, before its own
# numbers were reached at all.  That is the opening deciding there will be
# no adjustment, which is not a decision it is entitled to make.
#
# So the cap is staged.  P0 may commit most of it and no more; each later
# stage unlocks the rest, and the chain total is unchanged -- what changes
# is that a later stage cannot be starved by the earlier one.  Room a stage
# does not use is left for the next.
# P0's slice is overridable so it can be measured.  The 2026-08-22 taker A/B
# put the question on the table: P0 loses money under both execution modes
# (maker -68.2u, taker -145.7u) while P0-M and P1 carry the whole book, and
# P0 spends at the moment of least information -- so how much of the bankroll
# should the opening stage be allowed to burn for its seat?
# Refuse to act at a cycle where these certainty signs are lit, and walk on
# to the next one.  Empty means off.  Production owns this
# (``delta_v2_runtime.CERTAINTY_GATE_SIGNALS``, ("stable","sharp") as of the
# 2026-08-22 sweep).  With the env unset the two are the same tuple by
# construction; a sweep can still move it here.
#
# "Act", not "open": the live gate carries no stage condition, so it refuses
# an opening at P0 and a top-up at P0-M/P1 alike.  The ledger matched only
# the opening half until 2026-08-24 -- see the gate site for what that cost
# the measurement.
CERTAINTY_GATE = tuple(
    part.strip() for part in
    (os.environ.get("PAPER_LEDGER_CERTAINTY_GATE", "") or "").split(",")
    if part.strip()
) or CERTAINTY_GATE_SIGNALS
# Refuse where the market never made a book on the window we want (research
# knob, 2026-08-24).  The other two certainty signs refuse where the market
# has finished pricing; this asks the opposite question -- whether it ever
# started -- and it is a REQUIREMENT, not a sign: the refusal fires when
# ``quoted`` is dark, not when it is lit.
#
# It exists because the sign's own measurement inverted.  On 2026-08-21 a
# fully-quoted ideal window scored -39.7% against +15.6% for an unquoted
# one, and that reading is void: it was taken under the ``touch`` fill
# assumption and over a book that still carried the pre-charter-50 chains.
# Re-measured on the honest book with the gate in force, the ordering is
# the other way and far starker -- 94 quoted chains at -8.5% and 26.6% hit
# against 9 unquoted ones at -100.0% and no hit at all, every one of them
# settling for a total loss.  The mechanism is in the price, not the shape:
# the two strata seat almost identical windows (mean 2.21 vs 2.11 buckets)
# but the unquoted baskets cost half as much (0.115 vs 0.245), so what
# looks like a cheap seat is a seat in buckets the book declined to price.
# Nine chains cannot carry a rule on their own (all-miss at the quoted
# stratum's hit rate has p ~= 0.066), which is why this ships off.
#
# Off in code, on by evidence: the forward lane
# (``scripts/quoted_gate_forward_probe.py``) promotes it by writing its
# verdict to the activation file, and the env var stays as the sweep's
# override.  Promotion is data so that the thing which turns a rule on is
# the same artefact that records why -- and so turning it off again is an
# edit to a file rather than a deploy.
_QUOTED_ACTIVATION = ROOT / "data" / "quoted_gate_activation.json"


def _quoted_gate_promoted() -> bool:
    try:
        record = json.loads(_QUOTED_ACTIVATION.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(record.get("active")) and not bool(record.get("frozen"))


_REQUIRE_QUOTED_ENV = os.environ.get("PAPER_LEDGER_REQUIRE_QUOTED", "").strip()
REQUIRE_QUOTED_BOOK = (
    _REQUIRE_QUOTED_ENV.lower() in {"1", "true", "yes", "on"}
    if _REQUIRE_QUOTED_ENV
    else _quoted_gate_promoted()
)
# Charter 141 retired the per-stage share of the station-day allowance, and
# with it this lane's ``PAPER_LEDGER_P0_BUDGET_SHARE`` research arm: there is
# no share left to move.
# Research arm only: the production cap is charter 50's and lives in the
# kernel's policy.  This exists so a width sweep can pin the window to an
# exact size (min=max=k) without editing the file per arm; the default is the
# production cap and the value is already part of the rule identity above, so
# every non-default arm fingerprints itself.
MAXIMUM_SUPPORT_BUCKETS = int(
    os.environ.get("PAPER_LEDGER_MAX_SUPPORT_BUCKETS", "4") or 4
)
# Production holds, rather than adds to, a bucket whose probability fell since
# the previous cycle (``clamp_declining_gaps_v2``).  This ledger never ran that
# rule -- it was inline in delta_v2_runtime until 2026-09-01 -- which means the
# entire recorded book is the arm where the clamp is OFF, and nobody declared
# that.  Measured on production's own SEED decisions 2026-08-25..09-01: it cut a
# leg off 54 of 194 openings (28%), and it was the only thing that cut any.
#
# Default stays "off" so this knob does not silently rewrite what the book has
# always meant; "on" is the arm that reproduces production.  Which one should be
# the default is the question the replay exists to answer.
# Three arms, because there are three coherent answers and only one of them
# is currently running anywhere:
#   off  -- buy the whole target; what this ledger has always done
#   leg  -- drop the declining leg, keep the rest; what production does
#   hold -- if any target leg is declining, seat nothing this cycle
# "hold" is the one nobody has run.  It is what the complete-basket rule
# would imply if it applied to the solver's target instead of to what
# survives the clamp: production checks completeness *after* the cut, so a
# basket that lost a leg still passes as complete.
#   guarded -- three conditions, all required, before a leg may be dropped:
#              (1) the fall clears DECLINE_MIN_DROP -- not any fall at all;
#              (2) the bucket sits at an end of the window, so what survives
#                  is still one run; (3) it is not the centre rung.
#
#              (1) was added 2026-09-02 after the shape-only version had
#              been measured.  P0 probabilities follow the model run, not
#              the clock -- only 6.5% of 138,116 adjacent P0 checkpoints
#              differ at all -- so this rule fires in the moment a run
#              lands, and 18% of the falls it then sees are under 0.001
#              against a median of 0.0167.  Without a threshold it treats a
#              1e-9 reshuffle and a 0.5 collapse as the same event.  The
#              restatement suppression that shipped 2026-09-01 makes this
#              sharper still: it removes the 87% of checkpoints that
#              repeated the previous one, so adjacent pairs are now almost
#              always genuinely different and the rule fires far more often.
#
#              The three conditions guard different things and none
#              subsumes another: (1) noise, (2) a hole in the middle of the
#              basket, (3) throwing away the rung the model likes best.  The plain clamp runs before the
#              complete-basket check and is therefore free of both: measured
#              over 581 replay openings it dropped the centre 48 times and
#              left the support non-contiguous 10 times, and neither is a
#              violation of the rules as written -- both are outside the
#              path that enforces them.
DECLINE_CLAMP_MODES = ("off", "leg", "hold", "guarded", "held_only", "after_open")
# How far a probability must fall before the clamp calls it falling.  The
# shipped rule has no threshold at all -- a drop of 1e-9 clamps a leg
# exactly as a drop of 0.5 does -- and that has never been measured.
#
# Two measurements make it worth measuring.  P0 probabilities follow the
# model run: over 138,116 adjacent P0 checkpoints from 2026-08-15 only
# 6.5% differ at all, so the rule fires only in the moment a run lands and
# a station-day is clamped or not depending on where its first seatable
# cycle falls relative to that moment.  And 18% of the drops that do occur
# are under 0.001, against a median of 0.0167.
# A stage seats as many times as its cycles allow, adding to itself on
# each later one -- what production does (user ruling, 2026-09-02: "把账本
# 改为和生产一样").  Off is the one-seat-per-stage walk this ledger ran
# until then, kept only so an old arm can be reproduced; every number
# measured before 2026-09-02 describes that world, not production's.
#
# Safe to turn on only once the position identity is economic rather than
# clocked: under the old {station, day, stage, time, window, stake} id a
# second seat in one stage either collided with the first or left its
# settlement orphaned (38 of 405 on the first attempt).  Under the four-leg
# identity a later cycle that believes, prices or holds something different
# is its own row, and one that is identical is idempotent.
RESEAT_WITHIN_STAGE = (
    os.environ.get("PAPER_LEDGER_RESEAT_WITHIN_STAGE", "on").strip().lower()
    in {"1", "on", "true", "yes"}
)
DECLINE_MIN_DROP = float(
    os.environ.get("PAPER_LEDGER_DECLINE_MIN_DROP", "") or 0.0
)
# Default = production.  The clamp is production's own function (imported
# above) and production has run it on every cycle since 2026-08-15, so a
# book that skips it scores a strategy production never traded.  The default
# stayed "off" until 2026-09-02 only because no replay had been run with it
# on; that replay now exists (reseat_six_arms_20260902.md: leg +15.4% vs
# off +8.3% over 405 station-days) and the reason has expired.  "off" is
# kept as an arm so the pre-09-02 book can still be reproduced.
# Charter 69 (2026-09-02, final ruling "切 after_open"): the opening seat
# is never clamped, every later seat clamps every falling rung, held or
# not.  This is the same switch production carries, defaulting the same
# way.  "leg" is charter 51 as first shipped, "guarded" the day's interim
# reading, "off" reproduces the pre-09-02 book.
DECLINE_CLAMP = (
    os.environ.get("PAPER_LEDGER_DECLINE_CLAMP", "").strip().lower()
    or _PRODUCTION_DECLINE_CLAMP_MODE
)
if DECLINE_CLAMP in {"1", "on", "true", "yes"}:
    DECLINE_CLAMP = "leg"
if DECLINE_CLAMP not in DECLINE_CLAMP_MODES:
    raise ValueError(f"unknown decline clamp mode: {DECLINE_CLAMP}")
# The earliest event day this book admits.  Charter 50's ladder floor landed on
# 2026-08-15; every chain before it was seated under a minimum basket width the
# live engine no longer trades, so those rows score a strategy that does not
# exist.  They were not merely old, they were misleading: fourteen chains from
# 2026-08-01 to 08-14 carried 82% of the book's profit on 11% of its stake, two
# of them alone (ZGSZ 08-01 at 18.6x, RJTT 08-12) -- so every parameter sweep run
# across the whole table was really being decided by two seats no current rule
# would take.  The page already opens its equity at 2026-08-15; this is the
# ledger agreeing with it rather than quietly carrying more.
LEDGER_EVENT_DATE_FLOOR = os.environ.get(
    "PAPER_LEDGER_EVENT_DATE_FLOOR", "2026-08-15"
).strip()
# The ladder floor (charter 50).  The ledger exists to reproduce what
# production seats, so it must carry production's structural rules; left at
# the function default of 1 it recorded single-bucket seats the live rule
# forbids.
# Overridable so the floor itself can be measured.  The question it exists
# to answer, 2026-08-22: per-leg attribution says the non-centre rungs lose
# money (shoulder -49.9%, tail -100%) while the centre makes +50.5%, yet the
# shoulder value-clamp sweep peaks at 0.40 rather than at zero -- buying
# *no* shoulder is worse than buying it cheaply.  The floor is the obvious
# suspect: a basket must be contiguous and at least this wide, so refusing
# the shoulder can cost the seat entirely, taking the profitable centre rung
# with it.  If that is the mechanism, the shoulder's loss is an entry fee.
# Exempt the rung the market prices highest from the value clamp (A/B knob,
# 2026-08-23).  Measured on 101 settled station-days: the posted window held
# the settled bucket 44.6% of the time, but on 21.8% of all station-days that
# rung never filled -- the clamp caps at lambda times our own claim, and a
# bucket the market also likes is quoted above that, so the one we most wanted
# is the one we structurally cannot buy.  Loosening lambda globally was swept
# and loses (0.53 -> +39.9%, 0.70 -> +31.2%): it buys more losers too.  The
# market's price ORDER is the part that carries direction -- when a shoulder
# settles at all, the dearer side is the one 21 times out of 22 -- so this
# lifts the clamp on that rung alone and leaves every other rung clamped.
# The core rung's own ceiling (A/B knob, 2026-08-23).  The centre is exempt
# from the hedge clamp by user ruling 2026-08-20 -- pricing it away turns
# every basket into a shoulders-only bet -- but exempt has never meant
# unbounded: the red line falls back to the leg's own claimed probability,
# which is a lambda of 1.0.  Whether 1.0 is the right number has not been
# measured; every other rung's lambda has been swept and none of them sits
# at 1.0.
MAKER_RESTING_CENTRE_LAMBDA = float(
    os.environ.get("PAPER_LEDGER_MAKER_CENTRE_LAMBDA", "") or 1.0
)
MAKER_MARKET_TOP_EXEMPT = (
    os.environ.get("PAPER_LEDGER_MARKET_TOP_EXEMPT", "0").strip()
    not in {"0", "false", "FALSE", "no", ""}
)
MINIMUM_SUPPORT_BUCKETS = int(
    os.environ.get("PAPER_LEDGER_MIN_SUPPORT_BUCKETS", "3") or 3
)

# Research arm G (2026-09-08): refuse a leg the market's own book has ruled
# out, measured in rungs from the bucket the book prices highest.
#
# Measured over 45,393 two-sided quotes and 625,246 shadow orders
# (docs/research/VENUE_PRICE_CALIBRATION_2026-09-08.md): a cheap leg ADJACENT
# to the market's centre returns -21.7% bought at the ask, and the same price
# three or four rungs out returns -66.7%.  The separating variable is the
# distance, not the price -- which is why the earlier reading that keyed on
# price alone (-30.9% for "the tail") was a mixture of the two populations
# and could not be acted on.
#
# 0 disables the arm and leaves every recorded row byte-identical, which is
# the default: the live book has 78 of 82 filled legs inside two rungs, so
# this gate is worth about 4 USDC of notional there and its value has to be
# measured rather than assumed.
MARKET_DISTANCE_MAX = int(
    os.environ.get("PAPER_LEDGER_MARKET_DISTANCE_MAX", "") or 0
)

FEE_RATE = 0.0  # all-in asks in the snapshot already carry the fee
# Below this all-in cost a basket is almost certainly a dust offer on an
# empty side rather than a price anyone would fill.
DUST_COST_FLOOR = DUST_FILL_FLOOR_USDC
# How far down the funnel each refusal got.  The walk keeps the DEEPEST answer
# it reached, not the last one, because the last cycle is almost always the
# converged end-of-day book and says nothing about the day.
#
# Measured on 2026-08-20 over the 316 P0 station-days recorded as
# ``no_priced_window``: at the last cycle the market quoted a median of 2
# buckets at or above the leg floor and 55% of the books had a bucket at
# 0.90 or better -- a finished race.  At the first cycle the same days had a
# median of 5 quotable buckets and only 16% had converged, and 85% of them
# held a legal-width book at some point.  Replaying every cycle, 136 of the
# 316 did produce a priceable window earlier in the day and were refused on
# the objective (median best edge -0.353); the page attributed all 316 to
# "no window could be priced", which was true of 180 of them.
#
# Equal depths keep the FIRST arrival: once the walk reaches a depth, the
# earliest book that got there is the widest one that did, and therefore the
# most informative about why the day was declined.
REFUSAL_DEPTH = {
    "no_book_at_decision": 0,
    "incomplete_book": 1,
    "selection_failed": 2,
    # The production judge arm could not run production's resolvers on
    # this book -- no policy for the scope, or the solver failed closed.
    # Same layer as ``selection_failed``: nothing was priced.
    "production_judge_blocked": 2,
    # Charter 67's clock refusal is NOT on the certainty gate's layer,
    # though both are "our own clock".  The certainty gate fires INSTEAD of
    # judging on its cycle, so it can honestly be the day's deepest event.
    # A pre-window cycle never entered the funnel at all -- and later
    # cycles exist and DO get judged, so any judged refusal must outrank
    # this one.  At depth 5 it shadowed them: ZGGG 2026-08-30 was priced
    # and refused on edge all afternoon and the page said "窗未开" at
    # 14:50 local, because midnight's clock refusal outranked the
    # afternoon's economics.
    "entry_window_not_open": 2,
    # Charter 119, and it sits here for charter 67's exact reason: a P1
    # whose outermost layer left no receipt never entered the funnel, and
    # a later cycle on the same day does get judged once one of the three
    # claimants speaks.  Any judged refusal must outrank this one.
    "p1_execution_evidence": 2,
    # One layer: all three mean the enumeration produced no basket to price.
    # Which of the three is reported is decided by the gate that took the
    # last window standing, not by depth -- the funnel order lives in
    # ``_support_windows`` and reaches here as ``last_refusing_gate``.  They
    # share a depth because none of them got a price to judge, which is the
    # only thing depth is measuring.
    "no_legal_window": 3,
    "leg_below_floor": 3,
    "no_priced_window": 3,
    # Its own layer, between "no price at all" and "the objective judged it".
    # A window that cleared every structural floor and carried a real quote
    # on every leg did reach a price; it was refused against that price, by
    # the window gate rather than by the objective.  Deeper than the three
    # above and shallower than the two below, which is exactly where it sat
    # in the funnel.
    "negative_edge_window": 4,
    # One layer of our own: a basket cleared every structural floor, carried
    # a price on every leg and beat the friction -- and a rule of ours
    # declined to act on it anyway, before the objective was ever consulted.
    # Deeper than the edge gate, which judged a price, and shallower than the
    # objective, which never saw one.  These two are the only refusals in the
    # table whose owner is neither the book nor the model but our own clock,
    # and putting them on the objective's layer is what let 251 waits be
    # reported as "the odds were not worth it" until 2026-08-24.
    "unquoted_ideal_window": 5,
    "certainty_gate_lit": 5,
    # Same own-clock family: the seat rule declining a shape it cannot
    # cover, judged before price is even consulted.
    "shape_gap_too_wide": 5,
    # One layer: a basket was assembled, priced and put to the objective, and
    # no stake followed.  Which of the two is reported depends on whether the
    # price left the objective anything to judge.
    "no_positive_kelly": 6,
    "cost_exceeds_payout": 6,
    # The production judge arm seated a window and sized it to nothing the
    # chain does not already hold.  The objective's own layer.
    "production_judge_hold": 6,
    "no_depth": 7,
    # Charter 127 (五): the take walked past the cash the gates authorized.
    # Same layer as ``no_depth`` -- the basket was assembled, priced and
    # sized, and what refused it is the book, not the model.
    "completion_take_above_authorized_cost": 7,
    "no_top_up_needed": 7,
    # The atomic top-up arm: the chain is short of some legs but not all.
    "topup_not_complete_basket": 7,
    # The exchange's lot and production's station dial: the basket was
    # assembled, priced and sized, and the venue's own rules -- not ours
    # and not the model's -- would not take the order.  Same layer as
    # ``no_depth``, which is the same kind of answer.
    "exchange_minimum_lot_unaffordable": 7,
    "station_loss_floor_exceeded": 7,
    # One layer: the order was posted and bought nothing.  Which of the two
    # is reported turns on whether we cancelled it or the book never came.
    "resting_unfilled": 8,
    "belief_withdrew_order": 8,
    # One layer, the deepest judgement a top-up can receive: the buy was
    # fully sized, walked against the book, and refused by chain law --
    # charter 66's growth clause or its par clause.  Deeper than the layers
    # above because everything was known when it was refused; which of the
    # two is reported turns on which clause fired.
    "no_chain_growth": 8,
    "chain_cost_at_par": 8,
    # Charter 71(a): the chain's own law too -- everything was known,
    # and the reason it stood pat is that nothing had changed.
    "topup_requires_probability_change": 8,
    # Charter 77 is NOT a refusal of the day.  A cycle that is not a
    # decision moment stands on the stage's last recorded decision, and
    # that decision's own refusal is why the day has no seat.  Ranked at
    # 8 it outranked every judged refusal below the chain law --
    # ``negative_edge_window``, ``no_positive_kelly``,
    # ``production_judge_hold`` -- so from 2026-09-02 some twenty P0
    # station-days a day were filed as "不是决策时刻", on a page whose
    # reader had just asked why P0 never opened (2026-09-05).  Depth 0
    # keeps it out of the slot for good; the walk also skips it before
    # ``_deeper_refusal`` and only counts it per cycle.
    "no_decision_moment": 0,
    # Charter 73 at the execution layer: sized, walked, and refused on the
    # increment's edge.  The chain-law layer.
    "execution_edge_not_authorized": 8,
    "support_floor_after_lift": 8,
    # Charter 71(b) tightening arm: the increment itself does not pay.
    "topup_increment_edge_negative": 8,
    # The declining-probability clamp, when it is armed.  Sits at the
    # top-up layer because everything was known when it fired: the basket
    # was assembled, priced, sized against real depth, and then held back
    # by a rule of ours reading the distribution's direction.  It runs
    # BEFORE the chain clauses, so it never competes with them for the
    # report -- only one of the two can happen on a cycle.
    #
    # Registered at all because an unregistered reason resolves to depth 0
    # via ``REFUSAL_DEPTH.get(..., 0)`` and can therefore never be kept:
    # the arm would refuse correctly and then report some shallower cause,
    # which is the misattribution this whole table exists to prevent.
    "decline_clamp_hold_basket": 8,
    "decline_clamp_emptied_basket": 8,
    "chain_never_opened": 9,
}
# Which bucket-selection rule this sweep runs.  ``live`` is production's own
# ``_support_windows`` ordering; the rest exist so a strategy question can be
# answered against real ask depth and the kernel's own fill bound instead of
# a notebook.  Four separate simulations of the live strategy returned
# -17.2%, +24.3%, +73.5% and +146% against this ledger's -0.2% -- all wrong
# the same way, because a penny leg's payout dominates any P&L it enters.
WINDOW_STRATEGY = (
    os.environ.get("PAPER_LEDGER_WINDOW_STRATEGY", "live").strip() or "live"
)
# How a chosen basket is assumed to be filled.  ``taker`` crosses the spread
# and is the only one that is not a hypothesis: it is what the live path
# does and what every number before 2026-08-17 was measured under.
#
# The maker modes answer "what if we posted instead", and they are an upper
# bound, not a forecast.  A resting bid fills only when someone chooses to
# sell into it, which is adverse by construction -- the fills you get are
# the ones the other side wanted to give you.  Nothing in a depth snapshot
# says which of our orders would have filled, so these runs assume all of
# them do and should be read as "the best this could possibly be worth".
# They are worth measuring because the spread is large: 27.3% of ask at the
# median, and the whole of the book's 8.6% vig.
# Taker by charter 65 (user ruling, 2026-08-27: "整篮 EV" -- basket-level
# EV is the execution layer's only law).  The week that settled it: on
# same-entry rebuilds the maker book's entire deficit against taker sat in
# 33 chains whose winning legs rested unfilled (+77.1 USDC) while the 225
# shared chains were level -- the spread saving exactly paid for the
# adverse fills.  Every hybrid died the same death: taking earlier
# (drop-k flips), taking on confirmation (the ask has left the value
# ceiling by the time a rise confirms), taking the centre at entry
# (rescues 1 of 33) -- because on winner days the ask already sits above
# the leg's own claimed value at entry, and any mode bound by the per-leg
# red line caps below it and waits for a market that never comes down.
# Taker answers only to the basket-level EV gate, which is what lets it
# pay a leg more than the model claims and buy the winners.  The per-leg
# value line retires from execution constraint to maker-arm pricing
# reference; the resting machinery below stays as research arms.
# Charter 94 (2026-09-06): production posts the bid-anchored resting ladder,
# and this book scores it as ``maker_resting`` with the same ladder; taker
# (charter 65) is the arm on both sides.
EXECUTION_MODE = (
    os.environ.get("PAPER_LEDGER_EXECUTION", "maker_resting").strip()
    or "maker_resting"
)
EXECUTION_MODES = (
    "taker", "maker_bid", "maker_improve", "mid", "maker_resting",
)
# The family production is held equal to: one taker, one maker.
EXECUTION_FAMILY = "taker" if EXECUTION_MODE == "taker" else "maker"
# How long a resting order is left on the book before the basket gives up
# on it.  The probe's four horizons top out at an hour because it is
# measuring how fill rate grows; a basket entering at listing has until the
# day resolves, and the ruling is to let it wait (user ruling, 2026-08-17).
# 0 means "until the day resolves", which is the standing ruling (user
# ruling, 2026-08-17, restated 2026-08-18) and is now what the code does.
# Six hours was a leftover from the probe's measurement horizons, and it is
# not a horizon a basket entering at listing has any reason to accept: it
# gives up in the middle of the night before the event and books the miss.
MAKER_RESTING_HORIZON_MINUTES = float(
    os.environ.get("PAPER_LEDGER_MAKER_HORIZON_MIN", "0") or 0.0
)
# Whose resting lifecycle this book simulates (charter 127 (七)(甲)).
#
# ``ledger``: the order rests until the station's peak-heat cutoff, being
# re-decided by the kernel's rung action every dwell.  That half already
# matches production -- the dwell IS production's poll cadence and the
# action IS ``resting_rung_action_v1`` -- and the half that does not is the
# horizon: production's order is a GTD whose expiry is the SERVED BELIEF'S
# OWN freshness, median 5.5 hours at P0 and exactly 60 minutes at P1, while
# this book's rung rests into the afternoon.  An order that outlives the
# belief that posted it is buying on a claim nobody is serving any more.
#
# ``production_v127``: the belief's ``fresh_until_utc`` becomes a fourth
# bound beside the peak cutoff, the day end and the configured horizon, and
# the earliest one stops the order.  Nothing else moves: this is not a new
# execution policy, it is the same policy given production's horizon.
#
# Default stays ``ledger`` so the standing book does not move before the
# arm is read; the fingerprint carries the choice when it leaves the
# default.
# Whether a pass repairs a book that holds more than one rule set (user
# ruling, 2026-09-12: "账本做自动重推").
#
# Charter 129's outstanding item.  ``_drop_superseded_positions`` is bounded
# by the window a pass can re-derive -- never delete what you will not
# rebuild -- so after a rule change the scheduled pass re-derives four days
# and every older row keeps the digest it was seated under.  Three identity
# changes landed on 2026-09-12 and the book came to be 224 settled chains of
# one strategy and 83 of another, published as a single -28.3% while the
# served rule set alone read -34.5%.
#
# ``on``: a pass that finds positions under any other digest walks the whole
# history instead of the recent window, which is exactly what the reason-
# vocabulary rebuild beside it already does.  It converges in one pass --
# the foreign rows are dropped and re-derived, so none can remain.
#
# Measured 2026-09-12 (the lifecycle flip, 30 days of history, a live
# resident beside it): 55 minutes, 02:29:30Z to 03:24:30Z, not the fourteen
# first written here.  It does not block production -- the pass buffers
# its inserts into one closing transaction and ``basket_policy_training_fact``
# kept writing throughout -- and launchd does not start a StartInterval job
# over its running predecessor, so the single-instance guard never has to
# fire.  What it costs is that the book's position and settlement tables
# are EMPTY for the whole pass and every reader of them sees nothing for an
# hour after a rule change; the incremental sweeps queue behind it.
#
# It does NOT enter the selection fingerprint.  Two books produced by the
# same rules are the same book whether or not a pass had to repair one of
# them; hashing this would give them different digests.
#
# The cost scales with the book's span, which grows, and it already does
# not fit inside a sweep interval (55 minutes against 900 seconds).  If
# that hour of blankness becomes a problem, this wants a bound -- oldest
# days first, so the served window is back before the history is -- not a
# bigger timeout.
AUTO_REDERIVE_ON_RULE_CHANGE = (
    os.environ.get("PAPER_LEDGER_AUTO_REDERIVE", "").strip().lower() or "on"
)
if AUTO_REDERIVE_ON_RULE_CHANGE not in {"on", "off"}:
    raise SystemExit(
        f"unknown auto re-derive mode: {AUTO_REDERIVE_ON_RULE_CHANGE}"
    )
# The bound that note asked for, landed 2026-09-14 because the hour of
# blankness became a hole.  Since 2026-09-13 the ledger runs only as the
# resident's hosted sweep, whose timeout is 1800 seconds; the whole-history
# re-derive needs about 55 minutes, so on 2026-09-14 the 11:32Z pass dropped
# every foreign row, walked for thirty minutes and was killed before its
# closing write.  The book was left with no positions and no settlements --
# and nothing foreign left to trigger a repair, so the next scheduled pass,
# a four-day incremental window, would have re-seated four days and called
# that the book.
#
# So a repair takes the oldest ``REDERIVE_SLICE_DAYS`` event days that need
# it, drops foreign rows only inside that slice, walks it and commits; the
# next slice follows while the pass has budget, and the next pass carries
# on from wherever the book stops.  "Needs it" is either kind of hole: rows
# under another rule set, or a book whose newest answer under this one is
# older than the incremental window (the state that killed pass left).  A
# slice is killed at worst, never the book: the rows it dropped are older
# than the foreign rows it did not, so the next pass starts no later.
REDERIVE_SLICE_DAYS = int(
    os.environ.get("PAPER_LEDGER_REDERIVE_SLICE_DAYS", "").strip() or 5
)
# The line a hosted sweep prints when it stopped with work left; the resident
# (``weather_basket_yes_daemon.HOSTED_SWEEP_CONTINUE_MARKER``) brings the
# member due again after a short delay instead of its whole interval.
HOSTED_SWEEP_CONTINUE_MARKER = "hosted_sweep:continue"
# One pass holds the resident's SSOT write domain for as long as it runs, and
# production waits behind it: 2026-09-15 04:13Z, a 1500s repair pass kept a
# due production cycle waiting 1025s and counting.  Once a repair pass asks
# for the next one five minutes later (``HOSTED_SWEEP_CONTINUE_MARKER``), the
# budget is no longer how much repair a three-hourly slot can buy -- it is how
# long production is made to wait each time -- so it is held under the
# resident's production overdue line (``PRODUCTION_CYCLE_OVERDUE_SEC``, 720s)
# with a slice's worth of slack.  The repair takes the same number of slices;
# production gets its turns between them.
# ...and never past the writer lease the resident granted this pass
# (``pass_deadline.writer_lease_seconds``): the lease is the one number that
# says what any sweep slot may cost production, and this budget is this
# lane's share of it.
REDERIVE_PASS_BUDGET_SEC = writer_lease_seconds(float(
    os.environ.get("PAPER_LEDGER_REDERIVE_BUDGET_SEC", "").strip() or 420
))
if REDERIVE_SLICE_DAYS <= 0 or REDERIVE_PASS_BUDGET_SEC <= 0:
    raise SystemExit("re-derive slice days and pass budget must be positive")
# Whose resting lifecycle the book simulates.  Charter 127 (七)(丙), landed
# 2026-09-12 (user ruling on v2.1: implement, no arms): ``production_v127``
# expires a rung where production's GTD expires -- the served belief's own
# ``fresh_until_utc`` -- so the book stops buying on a claim nobody serves
# any more.  ``ledger`` reproduces the pre-09-12 book byte for byte: the
# rung rests to the peak cutoff, the day end or the configured horizon,
# whichever is first, and the belief is never consulted.
RESTING_LIFECYCLE = (
    os.environ.get("PAPER_LEDGER_RESTING_LIFECYCLE", "").strip().lower()
    or "production_v127"
)
if RESTING_LIFECYCLE not in {"ledger", "production_v127"}:
    raise SystemExit(f"unknown resting lifecycle: {RESTING_LIFECYCLE}")
# How often an unfilled leg is re-posted at the prices then on the book.
#
# A resting order that never moves is not what a maker does and not what
# this lane can honestly claim: the band is derived from the spread, and a
# spread from twenty hours ago is not a price anyone was offering.  Left
# static, a ladder posted at listing spends the day a long way from a market
# that has since moved, and the leg records a miss that says more about the
# staleness of the quote than about the book's willingness to sell.
#
# Each round re-derives the band for the legs still open, from the book at
# that moment, and leaves the rungs already bought alone.  0 disables
# re-pricing and reproduces the single-shot behaviour.
MAKER_RESTING_REPRICE_MINUTES = float(
    os.environ.get("PAPER_LEDGER_MAKER_REPRICE_MIN", "60") or 0.0
)
# A resting leg posts across a band, not at a point (user ruling,
# 2026-08-17).  The probe measured the two ends 14x apart on fill rate --
# joining the bid filled 5.8% in five minutes, a tick under the ask filled
# 82.8% -- and a single price has to pick one of those and live with it.
# A ladder takes both: the cheap rungs save the spread when the market
# comes all the way down, the dear rungs make sure the leg fills at all.
#
# The band runs from one tick above the bid (the best price worth waiting
# for) to one tick below the ask (the dearest price that is still a maker
# order rather than a crossing trade).  Size is split evenly, because
# weighting it toward the cheap end reintroduces the same "hope the market
# collapses" bet the shape work just removed.
# How far above the bid the cheapest rung sits.  Production owns this number
# (``delta_v2_runtime.MAKER_LADDER_IMPROVE_TICKS``); it lived here as a bare
# literal that happened to equal production's, which is agreement rather than
# construction -- the same divergence the ladder-steps note above describes.
#
# It is the free side of the value line: the band's high is still
# min(ask, value_cap) - tick, so lifting the low end never pays above what a
# leg is worth to us.  It only decides how much of the band is spent fishing
# the bottom, and the bottom is where a fill means the market came down onto
# a price we had left standing -- the same free option a slow re-pricing
# cadence hands the counterparty, measured on the time axis rather than the
# price axis.
MAKER_RESTING_IMPROVE_TICKS = int(
    os.environ.get("PAPER_LEDGER_MAKER_IMPROVE_TICKS", "")
    or MAKER_LADDER_IMPROVE_TICKS
)
# Where the band's dear end sits.  Unset: one tick under the ask, which is
# the resting contract -- at the ask the order is crossing, not resting.
# ``ask`` is the research arm for "post at the ask so the winning leg
# cannot be missed": under the touch fill model the top rung fills the
# moment it posts, so this arm buys completion at taker prices while the
# lower rungs still fish the dips.  It exists to price the 21-of-36
# settled chains (2026-08-24) whose winning leg sat posted and never
# filled; it is not a maker economics claim.
MAKER_RESTING_BAND_TOP = (
    os.environ.get("PAPER_LEDGER_MAKER_BAND_TOP", "").strip().lower()
)
# Research arm (2026-08-25): apply the centre/width scalars the calibration
# layer fitted but never activated, per stage, to the replayed distribution
# before any selection reads it.  JSON of {stage: [delta_c, width_lambda]}.
# The transform is production's own ``apply_center_width_calibration`` --
# the audit must not reimplement the rule -- but the placement is an
# approximation the measurement has to own: the stored bridge rows were
# served with identity centre/width and the band factors already applied,
# so this applies the axis correction after banding where production would
# apply it before.  Empty means off, which is the shipped behaviour.
_CENTER_WIDTH_ENV = os.environ.get("PAPER_LEDGER_CENTER_WIDTH_JSON", "").strip()
CENTER_WIDTH_OVERRIDE: dict[str, tuple[float, float]] = {}
if _CENTER_WIDTH_ENV:
    for _stage, _pair in json.loads(_CENTER_WIDTH_ENV).items():
        CENTER_WIDTH_OVERRIDE[str(_stage)] = (
            float(_pair[0]), float(_pair[1]),
        )
# Production owns this number (``delta_v2_runtime.MAKER_LADDER_STEPS``), which
# carries the sweep that chose it.  The env override stays so a sweep can move
# it here, but with the variable unset the two are the same number by
# construction rather than by agreement.
MAKER_RESTING_LADDER_STEPS = int(
    os.environ.get("PAPER_LEDGER_MAKER_LADDER_STEPS", "")
    or MAKER_LADDER_STEPS
)
# How fast an unfilled rung converges on the top of the band.  The misses
# concentrate at the cheap end -- the rungs that only fill when the market
# comes all the way down -- and a band that never narrows keeps betting on
# that collapse until the day ends.  Each re-pricing round lifts the floor
# of the band toward its top, linearly over this share of the resting
# window, so an order placed at listing spends its first hours asking for
# the spread and its last hours asking only for a fill.  1.0 reaches the
# top exactly at the deadline (when there is no time left to be hit there),
# 0.5 reaches it halfway through; 0 disables and reproduces the flat band.
# Measured on the full ledger (2026-08-20): 0.5, with re-posts allowed to
# stack owed rungs at one price, lifts the closed-day share fill rate to
# 91.7% / 90.8% / 91.9% across P0 / P0-M / P1 from 76.7% / 84.2% / 69.3%
# flat, with the saving against crossing still positive on every stage.
# Under the value clamp the schedule matters less -- the clamp, not the
# clock, decides the ceiling -- and 0.35 measured a point of ROI better
# than 0.5 with identical fill, so the faster schedule is the default.
MAKER_RESTING_ESCALATE_SHARE = float(
    os.environ.get("PAPER_LEDGER_MAKER_ESCALATE_SHARE", "0.35") or 0.0
)
# The model's part in the price (user ruling, 2026-08-20: the book gives
# the skeleton, the model gives the value, and market moves never trigger
# a withdrawal).  A rung never asks for more than the leg is worth to us:
# every posted price is capped at ``lambda * p_i(t) - one tick``, where
# ``p_i(t)`` is our own freshest in-force probability -- entry's at first,
# re-read from the bridge history at every re-pricing round.  A belief
# that collapses pulls the cap down and the rungs above it are withdrawn;
# a market that collapses while the belief holds is, by our own valuation,
# a discount, and the order stays.
#
# ``lambda`` discounts the claimed probability before it caps anything.
# 1.0 takes the model at its word; 0.53 is the measured calibration ratio
# (realised hits over claimed win probability), i.e. what the claim is
# actually worth.  Measured on the full ledger (2026-08-20): the clamp is
# where the ROI comes from and where the raw fill rate goes -- lambda=1
# bought +7.5 points of ROI, lambda=0.53 bought +17 (-17.6% vs -34.6%
# total).  The objective was re-weighed on those numbers (user ruling,
# 2026-08-20): ROI/EV is the goal, fill rate is a diagnostic with an 80%
# floor, so the honest lambda is the default.  The constant is a
# snapshot of a moving measurement; re-read the board's calibration
# section before trusting it to a new regime.
#
# The centre bucket is exempt (user ruling, 2026-08-20): it is the
# basket's base holding -- the one leg the position cannot be built
# without -- and a clamp that prices it away turns every basket into a
# shoulders-only bet the charter forbids.  Its rungs stand on the book
# skeleton alone; the whole-basket belief gate still covers it.
# A maker_resting research-arm number only.  Production carried the same
# clamp from 2026-08-22 to 2026-09-03 and then retired it: under charter 65
# the taker path never read it.  This lane's taker path does not either
# (``_execution_price`` returns the fee-adjusted ask before any cap), so the
# value below moves nothing unless PAPER_LEDGER_EXECUTION=maker_resting.
# Which stages the replay applies it to.  Production gates each stage
# independently -- one stage's forward window can admit while another's
# refuses -- so a measurement that switches all three together cannot say
# which one carried the result.
# How a resting rung is judged to have filled (A/B knob, 2026-08-23).
#
#   touch    best_ask reaches the rung.  The pre-existing rule, and the
#            optimistic one: an ask arriving at our price says a trade can
#            happen there, not that ours was the bid that got it.  Everything
#            already resting at that price is in front of us.
#   through  best_ask goes strictly below the rung.  A seller who pushed past
#            our level consumed everything bid at it, so the fill is implied
#            rather than assumed.  Needs no data the book does not carry.
#   queue    through, and the depth resting at our price when the rung was
#            posted must also have been worked off.  Depletion is credited
#            from the level's own shrinking depth, which counts a cancel as a
#            fill and so still errs generous.
#
# This matters more than any parameter in this file: every strategy that
# rests was measured positive and every one that crossed was measured
# negative (2026-08-17 sweep), so the whole result rides on whether these
# fills are real.
# Default moved to ``through`` on 2026-08-23.  Measured on the same book:
# touch +36.6%, through +20.2%, queue +20.1% -- so roughly 45% of the paper
# return was the touch assumption rather than the strategy, and every
# parameter conclusion drawn under touch was drawn against an objective
# inflated by that much.  ``queue`` adds almost nothing on top because these
# books are thin: only 42% of quoted buckets report a queue at all and 59%
# of those report none, so the binding question was never queue depth, it
# was whether an ask arriving at our price means our bid was the one filled.
# Served since 2026-09-11 (user ruling, "升"): ``adverse``.  What the other
# three answer is whether the PRINTED price reached our rung, and that read
# this book's maker family about twenty points better than the venue reads
# it -- measured, on the arms in
# docs/research/ADVERSE_FILL_AND_FLOOR_ARMS_2026-09-11.md: the same window
# and judge returns -5.70% when a crossing always fills and -26.34% when it
# fills at the rate the exchange actually filled at, against -18.9% live.
#
# Every maker figure taken before this date is on the old model and reads
# high.  ``through`` reproduces it byte for byte for a rebuild of a historic
# reading.
MAKER_FILL_MODEL = (
    os.environ.get("PAPER_LEDGER_FILL_MODEL", "adverse").strip() or "adverse"
)
if MAKER_FILL_MODEL not in {"touch", "through", "queue", "adverse"}:
    raise SystemExit(f"unknown maker fill model: {MAKER_FILL_MODEL}")
# ``adverse``: a rung the book crossed fills only some of the time, and how
# often depends on whether this leg is the one that wins.
#
# The other three models all answer the same question -- did the PRINTED
# price reach my rung -- and the venue answers a different one: did anybody
# choose to sell to me here.  Those come apart systematically, because the
# people willing to sell are the ones who think we are wrong.  Measured on
# the live maker book from 2026-09-05, per leg: the leg that turned out to
# win filled 12 of 32 times, the legs that lost 58 of 101.  Nineteen points
# (z = -2.02).  This book's own crossing rates over 2026-08-28..09-10 are
# 106/153 and 371/517, and it fills on every crossing, so its conditioning
# is -2.5 points and its maker family reads about six points better than
# the venue's.
#
# So the served numbers are CONDITIONAL on a crossing:
#
#     p(fill | crossed, wins)  = 0.3750 / 0.6928 = 0.5413
#     p(fill | crossed, loses) = 0.5743 / 0.7176 = 0.8002
#
# Two windows are being divided here -- the live rates start 09-05 and the
# crossing rates start 08-28 -- so these are a first calibration and the
# arm's own win/lose rates are the check on them, not a formality.
#
# ``PAPER_LEDGER_ADVERSE_P_WIN`` / ``_P_LOSE`` move them for a sensitivity
# arm.  Both enter the fingerprint whenever this model is selected.
#
# Second iteration, 2026-09-11.  The first pair was the live rates divided by
# the crossing rates, and the arm it produced came back at 44.3% and 69.1%
# per leg rather than 37.5% and 57.4% -- high, because a rung refused at one
# price gets a fresh draw when it is repriced, so a leg is asked more than
# once.  Scaled by the ratio the arm measured (0.847 and 0.831), which is the
# check the first pair was shipped to receive:
#
#     p(fill | crossed, wins)  = 0.5413 * (12/32) / (70/158)  = 0.4581
#     p(fill | crossed, loses) = 0.8002 * (58/101) / (369/534) = 0.6650
ADVERSE_FILL_P_WIN = float(
    os.environ.get("PAPER_LEDGER_ADVERSE_P_WIN", "") or 0.4581
)
ADVERSE_FILL_P_LOSE = float(
    os.environ.get("PAPER_LEDGER_ADVERSE_P_LOSE", "") or 0.6650
)
for _name, _value in (
    ("PAPER_LEDGER_ADVERSE_P_WIN", ADVERSE_FILL_P_WIN),
    ("PAPER_LEDGER_ADVERSE_P_LOSE", ADVERSE_FILL_P_LOSE),
):
    if not 0.0 <= _value <= 1.0:
        raise SystemExit(f"{_name} out of range: {_value}")

_CW_RAW = os.environ.get("PAPER_LEDGER_CENTER_WIDTH", "0").strip()
PAPER_LEDGER_CENTER_WIDTH = _CW_RAW not in {"0", "false", "FALSE", "no", ""}
PAPER_LEDGER_CENTER_WIDTH_STAGES = (
    frozenset({"P0", "P0-M", "P1"})
    if _CW_RAW in {"1", "true", "TRUE", "yes", "all"}
    else frozenset(
        part.strip() for part in _CW_RAW.split(",") if part.strip()
    )
)
_CW_REPLAY: dict[str, Any] | None = None

# Charter 68(c): the lambda tables production served, read as of each
# decision's own moment so a promotion never rewrites the book behind it.
# Preloaded once per backfill from the store's journal; None per scope
# before its first fit, which the kernel constant covers in the dispatch --
# the same fallback production has.
_EE_CONN: sqlite3.Connection | None = None
_EE_AS_OF_CACHE: dict[str, dict[str, Any]] = {}
_OFF_CENTRE_CACHE: dict[str, dict[str, Any]] = {}


# Research arm: ``off`` reads the kernel constants for every decision, which
# is what the book priced before charter 68(c); the default reads the store
# as of each decision, which is what production serves now.
EFFECTIVE_EDGE_TABLES_MODE = (
    os.environ.get("PAPER_LEDGER_EFFECTIVE_EDGE_TABLES", "").strip().lower()
    or "as_of"
)
if EFFECTIVE_EDGE_TABLES_MODE not in {"as_of", "off"}:
    raise SystemExit(
        f"paper_ledger_effective_edge_tables_mode_invalid: {EFFECTIVE_EDGE_TABLES_MODE}"
    )


def _off_centre_claim_bands() -> dict[str, Any]:
    """The off-centre claim table production compiles against.

    Standing ruling: a selection or sizing input moves on both sides or on
    neither.  This one arrived on production's side alone -- the callers and
    the kernel took it on 2026-09-09 and the two functions between them did
    not, so production raised TypeError on every scope until the chain was
    completed, and the ledger was never given it at all.  A replay judging a
    book production assembled under a claim correction it did not apply is
    the divergence that ruling exists to prevent.

    Read once and cached, for the same reason the seat band above is: it is
    the RULE the whole replay is judged under, not a per-decision quantity.
    """

    if _EE_CONN is None:
        return {}
    if "bands" not in _OFF_CENTRE_CACHE:
        try:
            _OFF_CENTRE_CACHE["bands"] = load_off_centre_tables(_EE_CONN)
        except Exception:  # noqa: BLE001
            _OFF_CENTRE_CACHE["bands"] = {}
    return _OFF_CENTRE_CACHE["bands"]


def _effective_edge_tables_as_of(as_of_utc: str | None) -> dict[str, Any]:
    if _EE_CONN is None or EFFECTIVE_EDGE_TABLES_MODE == "off":
        return {}
    key = str(as_of_utc or "")
    if key not in _EE_AS_OF_CACHE:
        _EE_AS_OF_CACHE[key] = load_effective_edge_tables(
            _EE_CONN, as_of_utc=as_of_utc
        )
    return _EE_AS_OF_CACHE[key]

# Which stages may add a leg to a chain.  Defaults to all three, because all
# three are one book.  It exists because they have not always been one book in
# practice: production could not compile a P0-M decision between 2026-07-30 and
# 2026-08-23 -- its station policy was left on the previous strategy version --
# while this replay, which never resolves a station policy, went on adding P0-M
# legs anyway.  Twenty-three of them, on chains carrying 171.2 of the book's
# 377.9.  Excluding a stage is how you ask what the book would have been
# without legs production could not have opened; it is a diagnostic, not a
# setting to leave changed.
_STAGES_RAW = os.environ.get("PAPER_LEDGER_STAGES", "").strip()
LEDGER_STAGES = (
    STAGES
    if not _STAGES_RAW or _STAGES_RAW in {"all", "1", "true", "TRUE"}
    else tuple(
        stage
        for stage in STAGES
        if stage in {part.strip() for part in _STAGES_RAW.split(",") if part.strip()}
    )
)
if "P0" not in LEDGER_STAGES:
    # Only P0 opens; without it every later stage is a leg on a chain that was
    # never opened, which is the three-book accounting this ledger left behind.
    raise SystemExit("PAPER_LEDGER_STAGES must include P0")

PAPER_LEDGER_CEILING = (
    os.environ.get("PAPER_LEDGER_METAR_CEILING", "1").strip()
    not in {"0", "false", "FALSE", "no"}
)

MAKER_RESTING_VALUE_LAMBDA = float(
    os.environ.get("PAPER_LEDGER_MAKER_VALUE_LAMBDA", "") or "0.53"
)
# The whole-basket belief gate: at each re-pricing round, if the order's
# claimed expectation under p(t) -- rungs already bought plus rungs still
# posted, at their own prices -- has gone negative, every open rung is
# withdrawn for good.  Continuing to buy a basket our own evidence has
# turned against is the one thing this lane must never do.  1 enables.
# Measured alone on the full ledger (2026-08-20): it withdraws 6-20
# points of fill and earns +2.3 points of ROI on its own -- modest,
# because the belief that turns against a basket usually turns after the
# cheap fills already happened.  On by default under the ROI-first
# objective (user ruling, 2026-08-20): it is the rule that stops buying
# a basket our own evidence has abandoned, which the value clamp alone
# does not do for the exempt centre bucket.
MAKER_RESTING_BELIEF_GATE = int(
    os.environ.get("PAPER_LEDGER_MAKER_BELIEF_GATE", "1") or 0
)
# Experiment knob: a separate value clamp for the basket's tail insurance
# leg -- the end of the posted window farther from the centre.  Unset means
# the tail is a shoulder like any other (current rule).  "exempt" prices it
# off the book skeleton like the centre; a number is its own lambda.
#
# Why it exists: measured 2026-08-20 at leg level, the legs the clamp kept
# from filling went on to realise 115% of their claimed probability while
# the filled legs realised 36% -- the clamp was refusing to pay for
# insurance that was worth more than lambda times its claim.  The sweep
# that chose lambda 0.53 measured whole-book ROI, which nets that loss
# against the overpriced legs the clamp correctly refused; this knob asks
# whether the tail rung specifically should be spared.
MAKER_RESTING_TAIL_LAMBDA = str(
    os.environ.get("PAPER_LEDGER_MAKER_TAIL_LAMBDA", "") or ""
).strip().lower()
# The shoulder's own clamp: the rungs one bucket either side of the centre.
#
# The clamp has only ever had two settings -- the centre is exempt and
# everything else is clamped at lambda -- so the shoulder has always been
# priced as insurance even though it is the centre's nearest neighbour and
# carries two thirds of the clamped legs (measured 2026-08-22: 320 of 476
# posted non-centre rungs sit one bucket out).  The 2026-08-21 sweep that
# chose 0.53, and the tail-exemption experiment beside it, both moved the
# tail and the shoulder together or moved the single farthest rung; neither
# asked what the shoulder alone is worth.
#
# Unset means the shoulder is clamped like any other non-centre rung, which
# is the current rule.  "exempt" prices it off the book like the centre; a
# number is its own lambda.
# maker_resting research arm only; "exempt" is the charter 63 verdict as it
# stood when production retired the clamp (2026-09-03).
MAKER_RESTING_SHOULDER_LAMBDA = str(
    os.environ.get("PAPER_LEDGER_MAKER_SHOULDER_LAMBDA", "") or "exempt"
).strip().lower()
# The fill-information lifecycle (research arm A5, 2026-08-26).  The user's
# ruling made fill status a calibration input for EXECUTION decisions only:
# the market does not define value, it provides information, and p(t) claims
# are never rewritten.  Multipliers fitted on first-half legs (decision date
# before 08-21) and direction-verified on the second half (0.39->0.51,
# 1.18->1.57, 0.32->0.37, 0.06->0.00): a leg the market has already sold to
# us realises x0.39 of its claim (x0.32 when claimed >=0.35), a leg the
# market will not fill realises x1.18, and a leg asking under five cents
# realises nothing regardless of claim.  Wired into four lifecycle
# decisions: replenishment stops on high-claim filled legs and sub-nickel
# asks, the late-phase crossing ceiling carries the unfilled boost (the
# EHAM 08-19 winning shoulder crosses at 0.270 under 0.224*1.18), crossing
# refuses sub-nickel asks, and the belief gate weighs each leg's claim by
# its own fill state.  '' = off; fingerprint carries the arm key only when
# enabled.
_PPOST_RAW = str(os.environ.get("PAPER_LEDGER_PPOST", "") or "").strip().lower()
# Component vocabulary: w1 replenish stop, w2 unfilled crossing boost,
# w3 sub-nickel ask veto, w4 belief-gate weighting.  "1"/"on"/"all" is the
# whole bundle; a comma list picks components -- the bundled first run
# measured W4's kept-alive baskets at -46.5% ROI while W2/W3's thesis
# stayed untested under the noise, so decomposition is the honest follow-up.
MAKER_RESTING_PPOST_COMPONENTS = (
    frozenset({"w1", "w2", "w3", "w4"})
    if _PPOST_RAW in {"1", "on", "true", "all"}
    else frozenset(
        part.strip() for part in _PPOST_RAW.split(",") if part.strip()
    )
)
MAKER_RESTING_PPOST = bool(MAKER_RESTING_PPOST_COMPONENTS)
PPOST_FILLED_MULT = 0.39
PPOST_UNFILLED_MULT = 1.18
PPOST_HIGH_CLAIM_FILLED_MULT = 0.32
PPOST_HIGH_CLAIM_THRESHOLD = 0.35
PPOST_LOW_ASK_VETO = 0.05


def _ppost_multiplier(claim: float, has_fill: bool) -> float:
    """The fill-state correction for one leg's claim, execution-side only."""

    if has_fill:
        return (
            PPOST_HIGH_CLAIM_FILLED_MULT
            if claim >= PPOST_HIGH_CLAIM_THRESHOLD
            else PPOST_FILLED_MULT
        )
    return PPOST_UNFILLED_MULT


# The toxic-band gate (research arm, 2026-08-26).  A non-centre leg whose
# claimed probability stands this far above the market's ask is not a
# bargain, it is the market ruling the leg out: measured on the settled
# book, legs claimed >=0.30 over their price filled 94.4% and realised
# 5.6% against 46.6% claimed, and the shadow probe reproduced it forward
# at n=1139 (0.98% realised).  With the gate set, such a leg prices as
# unpostable -- which, through the same mechanism as the three-cent leg
# floor, refuses any window that needs it; the curve score seats a window
# without the leg instead.  A standing leg that turns toxic at a reprice
# withdraws its open rungs and re-enters when the gap closes.  The centre
# bucket is exempt (the 2026-08-20 base-holding ruling).  '' = off, the
# production default; the fingerprint carries the key only when set.
MAKER_RESTING_MAX_CLAIM_EDGE = str(
    os.environ.get("PAPER_LEDGER_MAKER_MAX_CLAIM_EDGE", "") or ""
).strip()


def _is_shoulder(
    bucket: str,
    centre_bucket: str | None,
    window: Sequence[str] | None,
) -> bool:
    """One bucket either side of the centre, on the posted window."""

    if not window or centre_bucket is None:
        return False
    ordered = [str(b) for b in window]
    if str(centre_bucket) not in ordered or str(bucket) not in ordered:
        return False
    return abs(
        ordered.index(str(bucket)) - ordered.index(str(centre_bucket))
    ) == 1
# Completion-first execution (user redesign, 2026-08-21).  The order's job
# is to build the target position inside the value ceiling, not to win the
# spread: measured at leg level the legs that never filled realised 115%
# of their claimed probability against 36% for the ones that did, so every
# unfilled rung is, in expectation, the expensive part of the day.
#
# Three phases over the order's own window: before GAP_MID the rungs fish
# the band bottom (price priority); from GAP_MID the escalation floor also
# rises with the leg's own unfilled fraction (a filled leg feels no
# pressure); from GAP_LATE a still-open leg may cross the spread and take
# the ask -- but only inside its value ceiling.  The ceiling is the red
# line the pressure can never move: a hedge leg's clamp (lambda p) and the
# core leg's own claimed probability.  A market asking more than the model
# says the leg is worth stays unbought, at any completion gap, by design.
# Research knobs (2026-08-27): the crossing-priced gate changed what the
# late phase means.  Every seated basket now clears +EV at the ask, so a
# late-phase take pays a price the entry gate already endorsed -- which
# was not true under lane pricing, and is why an earlier GAP_LATE is worth
# measuring now and was not before.  Non-default values enter the
# fingerprint; the defaults reproduce the shipped lifecycle bit for bit.
MAKER_RESTING_GAP_MID = float(
    os.environ.get("PAPER_LEDGER_GAP_MID", "") or (1.0 / 3.0)
)
MAKER_RESTING_GAP_LATE = float(
    os.environ.get("PAPER_LEDGER_GAP_LATE", "") or (2.0 / 3.0)
)
# Research arm (2026-08-27): direction-confirmed take.  The 2026-08-20
# ruling says market moves trigger nothing in this lifecycle; this cell
# exists to price that ruling, exactly as MAKER_RESTING_CRASH_TICKS prices
# its withdrawal half.  From GAP_MID onward, a leg still open whose best
# ask has RISEN at least this many ticks above the first book its resting
# order was re-priced against is read as the market moving toward the
# claim, and may take the ask inside its value ceiling -- the same ceiling,
# floor and sub-nickel refusals as the late-phase take, only the clock
# differs.  A falling ask triggers nothing either way: the leg keeps
# resting, never withdrawn.  What this arm is aimed at, measured
# 2026-08-27: the maker book's entire deficit against taker sits in 33
# chains whose winning legs rested unfilled while the price ran away
# (+77.1 USDC); on the 225 chains both books opened the two were level.
# Empty disables and reproduces the shipped lifecycle bit for bit.
MAKER_RESTING_CONFIRM_TAKE_TICKS = float(
    os.environ.get("PAPER_LEDGER_CONFIRM_TAKE_TICKS", "") or 0.0
)
# Research arm (2026-08-27): the hybrid -- centre leg takes at entry,
# every other leg keeps the full resting lifecycle.  The centre is the one
# leg whose value ceiling is its own claimed probability (no lambda
# discount), so an entry ask the crossing gate admitted is an ask the
# centre may pay by this book's own law; and the missed-winner mechanism
# this aims at concentrates where the claim mass does.  Implemented as
# the centre's rungs all posted at min(entry ask, value cap): inside
# value they fill on the first tick, which is what taking means under
# the touch model; above value they rest at the cap and the red line
# holds.  Empty disables, bit-identical.
MAKER_CENTRE_TAKER = bool(
    (os.environ.get("PAPER_LEDGER_CENTRE_TAKER", "") or "").strip()
)
# Completeness re-priced as risk (user ruling, 2026-08-24; twice corrected the
# same day).  The lifecycle is untouched for every leg -- the tail included, it
# is the hedge leg and fills at its clamp like everyone else.  The budget
# governs the premium alone: while the basket's uncovered claim mass (claimed
# probability still sitting on open rungs) exceeds it, a core or shoulder
# leg's crossing ceiling relaxes from its clamp toward COVERAGE_LAMBDA times
# the live claim, never above the claim; a tail's never relaxes.  The roles
# are live, re-read from the belief in force at each re-pricing round.  There
# is deliberately no withdrawal in this contract (user ruling, 2026-08-24):
# unpurchasable risk leaves the rungs resting at their ceilings, and the only
# thing that ever withdraws an order remains the belief gate.  Production owns
# both numbers (``delta_v2_runtime``); negative budget restores the legacy
# contract as the control cell.
MAKER_RESTING_COVERAGE_BUDGET = float(
    os.environ.get("PAPER_LEDGER_COVERAGE_BUDGET", "")
    or MAKER_COVERAGE_RISK_BUDGET
)
MAKER_RESTING_COVERAGE_LAMBDA = float(
    os.environ.get("PAPER_LEDGER_COVERAGE_LAMBDA", "")
    or MAKER_COVERAGE_VALUE_LAMBDA
)
# Consolidation annotation (research lane, 2026-08-21): three signs that
# the market has already done its pricing work by the time we enter --
# the mass centre unchanged over the prior K P0 decisions, the top-two
# mass at or above the sharpness line, and the ideal centre+-1 window
# fully quoted at or above the leg floor.  Measured on 159 settled
# chains, the 45 with NONE of the three returned +77% (+42% with the
# best win removed) while every stratum with at least one returned -35%
# to -44%: certainty is what the market charges for.  ANNOTATION ONLY --
# recorded on the row, never consulted by entry, sizing or execution;
# the forward sample decides whether it ever becomes a gate.
CONSOLIDATION_STABLE_LOOKBACK = 3
CONSOLIDATION_SHARP_TOP2 = 0.60
CONSOLIDATION_QUOTED_FRACTION = 0.80
# Selection-aware claim discount (research knob, 2026-08-21).  Betting only
# when the claim clears the price harvests our own claim errors -- the
# promise is honest unconditionally (realised/claimed 1.03-1.22 at every
# decision age) and under-delivers only conditional on trading.  Measured
# on the posted basket, the basis the entry gate actually judges: traded
# seats realise 0.60 of their claim (159 settled).  The gate and the
# sizing multiply the claim by this factor; the recorded row keeps the raw
# claim so the calibration accounts stay honest.
#
# Deliberately FLAT, not stratified by consolidation count, although the
# stratified form was the design ask: measured per stratum the factor runs
# 0.38 / 0.64 / 0.56 / 0.70 -- non-monotone, and the stratum that
# motivated stratification holds five chains.  The strata separate on
# PRICE, not on claim honesty; a factor the data cannot rank has no
# business being a table.  0 disables, and the replay verdict is in: at
# 0.60 the book shrank to 77 seats realising 0.19 of their claims at
# -54% ROI.  Demanding claim/price above 1/0.60 turns the gate into a
# maximum-disagreement selector -- the organ the reverted curve-score
# seat grew -- and the discount is endogenous to selection intensity, so
# the fixed point runs away: tighten, the survivors worsen, and the
# factor you measured no longer holds.  Measurement lane only; this knob
# must not become a gate.
SELECTION_DISCOUNT = float(
    os.environ.get("PAPER_LEDGER_SELECTION_DISCOUNT", "0") or 0.0
)
# Research arm (2026-09-01): refuse a seat whose served distribution's
# mean sits further than this many bucket steps from its mode, measured at
# the entry snapshot.  0 disables.  The motivating measurement: through
# late August the pool's mean-mode gap narrowed while the *bought* sample's
# doubled -- the market priced the well-shaped days out of reach and the
# funnel slid into the wide-gap days, where a mean-centred window
# structurally cannot cover the mode and the extension direction ran 17%
# aligned with truth.  Both direction-rule arms lost the whole book
# (off -6.8%, toward_mode -13.2% against -2.3%), so the candidate fix is
# admission, not placement: a shape this split is a day this seat rule
# has no edge on.
SHAPE_GAP_LIMIT_BUCKETS = float(
    os.environ.get("PAPER_LEDGER_SHAPE_GAP_LIMIT", "0") or 0.0
)
# Companion arm: past the limit, scale the opening stake instead of
# refusing the seat.  The refusal family was measured 2026-09-01 and every
# threshold lost to the baseline -- not because split days are good (they
# are the worst days P0 has) but because only P0 opens the chain, so a
# refused P0 seat also forfeits the P1 top-ups that carry the book's
# profit (37 seats, +186u, dead of chain_never_opened under the 0.5 arm).
# Scaling keeps the chain alive at a token stake; 0 keeps the refusal
# behaviour when the limit alone is set.
SHAPE_GAP_TILT = float(
    os.environ.get("PAPER_LEDGER_SHAPE_GAP_TILT", "0") or 0.0
)
# Research arm (2026-09-01, user's Edge -> Effective Edge insight): shrink
# the claimed probability by the realized/claimed ratio HISTORICALLY seen at
# this seat's market-agreement level, so every downstream consumer -- Kelly,
# the EV gate, the chain-growth clause -- inherits the measured inverted-U of
# effective edge instead of the monotone preference for nominal edge.  The
# nominal edge is largest exactly where the model is most wrong (agreement
# < 0.5 realizes 0.14 of its claim), and the only positive strip sits at
# agreement 0.70-0.85; e_eff = p*(lambda(a) - a) says so and the per-band P0
# ROI (-88.6 / -33.0 / -5.0 / -27.8) traces the same curve.  The table below
# is fitted on the EARLY window only (settlements before 2026-08-27) --
# fitting it on the full sample would let the late collapse grade its own
# homework.  Opening stages only: P1's lambda runs above 1 where the market
# disagrees (1.09-1.23, the observation advantage), and shrinking it would
# destroy the one lane that earns.  Endogeneity warning inherited from
# SELECTION_DISCOUNT: the ratios are conditional on the current selection
# distribution, so this may only ever ship after a forward split and a whole
# -book replay, never from the full-sample fit alone.
# Charter 68: the same switch production carries, defaulting the same way.
# The table itself lives in the kernel so the two lanes cannot drift -- a
# local copy here is exactly how a "shared" rule becomes two rules.
EFFECTIVE_EDGE_MODE = (
    os.environ.get("PAPER_LEDGER_EFFECTIVE_EDGE", "").strip().lower() or "on"
)
# Charter 68 scope (research arm, 2026-09-03), production's
# DELTA_V2_EFFECTIVE_EDGE_SCOPE: ``window`` deflates the seat's claim by the
# whole window's agreement; ``bucket`` sums each rung's claim deflated by
# its own agreement off the kernel's per-leg table.  Asserted equal to
# production by the binding test.
EFFECTIVE_EDGE_SCOPE = (
    os.environ.get("PAPER_LEDGER_EFFECTIVE_EDGE_SCOPE", "").strip().lower()
    or "window"
)
if EFFECTIVE_EDGE_SCOPE not in {"window", "bucket"}:
    raise SystemExit(f"unknown effective edge scope: {EFFECTIVE_EDGE_SCOPE}")
# Production's per-leg reservation veto, replayed here as an arm (2026-09-03).
# ``off`` is this lane's standing rule and production's ``joint``: the seat
# is judged as one basket.  ``on`` refuses the seat whenever any rung's
# price exceeds that rung's deflated claim -- the taker path as it shipped
# on 2026-08-31, which held 91 baskets after the 2026-09-02 arming.
LEG_VETO = (
    os.environ.get("PAPER_LEDGER_LEG_VETO", "").strip().lower() or "off"
)
if LEG_VETO not in {"off", "on"}:
    raise SystemExit(f"unknown leg veto: {LEG_VETO}")
# The whole-basket judge (research arm, 2026-09-03).  ``ledger`` is this
# lane's standing rule: production's window shortlist, its head seat priced
# at the offer, charter 68 applied as a gate on the whole claim, and Kelly
# sizing the stake.  ``production`` hands the same book, holdings and
# capital to production's own solver -- the objective that seats a window
# by its curve score and sizes it in one pass, charter 68 folded into the
# claim it optimises -- and books what that returns.  The two are not
# hypotheses about each other: production runs the second, the ledger has
# always measured under the first, and until this arm nothing had put the
# two judges on one book.  What the arm cannot carry over is written on
# ``_production_judge``.
# ``production_seat`` splits the gap: production's solver chooses the window,
# this lane's gate and Kelly then size it -- so seat choice and sizing can
# be told apart in the measurement.
# Since 2026-09-04 (user ruling) the standing book is judged by production's
# own solver: ``production`` is the default and ``ledger`` -- this lane's
# original head-seat-plus-Kelly judge -- is kept as a research arm.  On the
# replay book the two read +49.8% and +44.2%; the book is here to measure
# what production runs, and since charters 72-74 that is the production
# judge with the ticket and atomic top-ups.
JUDGE = os.environ.get("PAPER_LEDGER_JUDGE", "").strip().lower() or "production"
if JUDGE not in {"ledger", "production", "production_seat"}:
    raise SystemExit(f"unknown judge: {JUDGE}")
# Whether this lane's leak-free replay layers -- the P1 cooling ceiling and
# the centre/width correction, both read from per-event-date tables fitted
# only on earlier days -- reach the belief that seats.  They can only when
# this lane is the judge: production's ``snapshot_from_probability_envelope``
# takes every bucket's probability from the envelope itself, so under the
# production judge a corrected vector handed in as the market mapping is
# overwritten before the solver reads it.  Until 2026-09-04 the layers were
# applied anyway, so the row recorded a corrected claim the solver had never
# seen (SBGR 2026-08-17 P1: 0.1995 recorded against a 0.52 cost) and the
# two arms measured nothing.  Under the production judge the walk now
# carries the envelope's own vector everywhere -- row, clamp, consolidation,
# settlement -- which is what production judged on.  Feeding the layers
# into the solver would mean writing them into the envelope, a deliberate
# departure from production that has to be ruled on, not slipped in.
REPLAY_LAYERS_APPLIED = JUDGE == "ledger"
# Charter 71(b) tightening, replay arm (2026-09-03): ``on`` refuses a top-up
# whose own increment does not pay -- its expected payout on the lambda*p
# partition must exceed what it costs -- on top of the chain-growth gate.
# The growth gate is a portfolio criterion and let 75 of 118 P0 top-ups
# through with a window-level deflated edge at or below zero; this asks the
# increment itself.  Production spells nothing until it is ruled.
TOPUP_INCREMENT_EDGE = (
    os.environ.get("PAPER_LEDGER_TOPUP_INCREMENT_EDGE", "").strip().lower()
    or "off"
)
if TOPUP_INCREMENT_EDGE not in {"off", "on"}:
    raise SystemExit(f"unknown top-up increment edge arm: {TOPUP_INCREMENT_EDGE}")
# The bankroll the production judge's curve reads (replay arm, 2026-09-03).
# ``station`` is production's rule: the station cap (15 USDC), which the
# kernel widens by 1/f to 60, so at stakes under 6 the curve is a line and
# the objective is expected value.  ``scope`` is the scope's own allowance
# (what the solver may spend), where the curve bends; ``book`` is the whole
# book, where it is flatter still.  Which bankroll the curve should read is
# the question this arm measures.
JUDGE_BANKROLL = (
    os.environ.get("PAPER_LEDGER_JUDGE_BANKROLL", "").strip().lower()
    or "station"
)
if JUDGE_BANKROLL not in {"station", "scope", "book"}:
    raise SystemExit(f"unknown judge bankroll: {JUDGE_BANKROLL}")
# Whether a later stage may open a station-day the P0 never opened (replay
# arm, 2026-09-03).  ``off`` is this book's rule and the user's: only P0
# opens, P0-M and P1 add to what P0 holds, and the chain ticket exists so a
# P0 with no edge still leaves P1 something to add to.  ``on`` lets P0-M/P1
# open on their own when the chain is empty -- the alternative production
# was briefly described as running -- so the two can be measured.
STAGE_SELF_OPEN = (
    os.environ.get("PAPER_LEDGER_STAGE_SELF_OPEN", "").strip().lower() or "off"
)
if STAGE_SELF_OPEN not in {"off", "on"}:
    raise SystemExit(f"unknown stage self-open arm: {STAGE_SELF_OPEN}")
# Whether a top-up must add to every leg of the seat (replay arm, 2026-09-03).
# ``off`` is this book's rule: a top-up buys the legs the chain is short of
# and leaves the rest.  ``on`` is production's atomicity ruling (2026-08-12,
# a complete basket gives every support bucket a positive increment): a
# top-up that would touch only some legs is refused instead.  Measured so
# the two can be ruled between.
# ``on`` since 2026-09-04: the standing book mirrors production's atomicity
# (charter 74 measured it at +49.8% against +48.7% for deficit-leg top-ups
# under the production judge); ``off`` is the research arm.
TOPUP_ATOMIC = (
    os.environ.get("PAPER_LEDGER_TOPUP_ATOMIC", "").strip().lower() or "on"
)
if TOPUP_ATOMIC not in {"off", "on"}:
    raise SystemExit(f"unknown top-up atomic arm: {TOPUP_ATOMIC}")
# A P1-specific effective-edge table, for the replay arms only.  Charter 68
# exempts P1 from the kernel's lambda on purpose -- its edge is the
# observation, and where the market disagrees most the observation is right --
# and nothing here changes that exemption for production or for the default
# ledger run.  What this admits is a table fitted on P1's own settled rows,
# keyed by event date so each day reads only the days before it, handed in by
# path.  Unset means no table and the exemption stands exactly as it did.
# Deliberately a separate table: the kernel's rolling fit pools every stage it
# covers into one set of bands, and adding P1 to that pool would move P0 and
# P0-M's sizing along with it.
P1_EFFECTIVE_EDGE_TABLE_PATH = (
    os.environ.get("PAPER_LEDGER_P1_EFFECTIVE_EDGE_TABLE", "").strip()
)


def _load_p1_effective_edge_table() -> dict[str, list[tuple[float, float]]]:
    if not P1_EFFECTIVE_EDGE_TABLE_PATH:
        return {}
    try:
        raw = json.loads(Path(P1_EFFECTIVE_EDGE_TABLE_PATH).read_text())
    except (OSError, ValueError) as exc:
        raise SystemExit(
            f"paper_ledger_p1_effective_edge_table_unreadable: {exc}"
        ) from exc
    tables = raw.get("by_event_date") if isinstance(raw, dict) else None
    if not isinstance(tables, dict):
        raise SystemExit("paper_ledger_p1_effective_edge_table_shape_invalid")
    return {
        str(day): [(float(bound), float(lam)) for bound, lam in bands]
        for day, bands in tables.items()
    }


P1_EFFECTIVE_EDGE_TABLE = _load_p1_effective_edge_table()
# Consolidation stake tilt (user ruling, 2026-08-21): the one discriminator
# that survived every robustness cut.  Chains entered with two or more of
# the three consolidation signs on returned -41.6% (ex-best -46.0%,
# ex-top-two -50.1%, and -46.5%/-35.9% in a temporal split-half) while the
# 0-1 stratum held +24.0% after removing its best win.  A GATE on the same
# split would change the selected population and invalidate the numbers --
# that failure mode is measured twice above -- so this is a stake scale
# only: same entries, same baskets, same prices, less money where the
# certainty premium was measured to eat the edge.  The factor is decided
# by the P0 opener's own entry signals and inherited by the whole chain,
# which is the basis the strata were measured on.  Never above 1.
# "稳∨尖 ×0.10" (user ruling 2026-08-28, measured twice).  ``quoted`` no
# longer counts: it fires on nearly every chain and says only that a book
# exists -- the discrimination lives entirely in the two model-side
# lights.  Either of them lit at the P0 opener tilts the whole chain to a
# tenth.  Ranked first among five same-entry arms on the maker-era book,
# then re-measured on the taker baseline the day charter 65 shipped:
# -5.32% -> -3.70%, paired +39.9 USDC with drop-1/2/3 at +71.9/+61.1/
# +51.0 and 138:113 of changed chains improved -- the rare shape where
# every robustness cut strengthens the verdict (the one big chain the
# rule hurts, SBGR 08-26 at -32, is the drop-1).
# Re-measured 2026-08-30 on the charter-66/67 stack (319 chains, 11 arms,
# full rebuild each): the threshold shape is confirmed hard -- dropping
# either light or requiring both costs -111 to -295 -- and the depth is
# monotone down to a plateau: 0.05 beats 0.10 by +14.58 with drop-1/2/3
# all positive, sign 170:142, and both temporal halves positive, while
# 0.02 and the floor sit within 1.5u of 0.05 (the minimum-clip clamp
# dominates below 0.05).  At full stake the strata run +5.5% / -18.3% /
# -39.8% for 0/1/2 lights lit, so the certainty premium is monotone and
# a graduated cut stays worth measuring.  0.10 -> 0.05.
CONSOLIDATION_TILT_MIN_SIGNALS = int(
    os.environ.get("PAPER_LEDGER_CONSOLIDATION_TILT_MIN_SIGNALS", "1") or 1
)
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
CONSOLIDATION_TILT_SIGNALS: tuple[str, ...] = tuple(
    name.strip()
    for name in (
        os.environ.get(
            "PAPER_LEDGER_CONSOLIDATION_TILT_SIGNALS", "stable"
        ) or "stable,sharp"
    ).split(",")
    if name.strip() in ("stable", "sharp", "quoted")
)
# **固化减仓已退役（宪章第 139 条，2026-09-16 裁决「撤」）**：默认 1＝恒等，
# 灯仍照算照记（``consolidation_json``），``PAPER_LEDGER_CONSOLIDATION_TILT=0.05``
# 复现旧规则作研究臂。生产 ``PM_CONSOLIDATION_TILT`` 同步。退役依据见宪章第
# 139 条：stable 灯亮的 P0 目标篮每美元 −3.1% 对不亮 −10.3%、前后半翻号，
# 本账本现役书亮 −15.1% 对不亮 −37.0%——砍的是亏得轻的那一半；生产里保底
# ≈额度，灯亮下单只有 11% 真被砍到。
CONSOLIDATION_TILT_FACTOR = min(1.0, float(
    os.environ.get("PAPER_LEDGER_CONSOLIDATION_TILT", "1") or 1.0
))
# Whose lights decide a top-up's tilt.  ``opener`` is the measured rule:
# the P0 opener's lights set the whole chain's factor.  ``stage`` asks the
# lights at each stage's own decision moment -- the only semantics a
# distributed production walk can compute without cross-cycle state, so it
# is the semantics production runs, and this knob exists to price the
# difference between the two on the same book before the two layers are
# declared aligned.
CONSOLIDATION_TILT_SCOPE = (
    os.environ.get("PAPER_LEDGER_TILT_SCOPE", "opener").strip() or "opener"
)
if CONSOLIDATION_TILT_SCOPE not in ("opener", "stage"):
    raise ValueError(f"unknown tilt scope: {CONSOLIDATION_TILT_SCOPE}")
# The tilt clamps at the smallest order the venue takes (user ruling,
# 2026-08-28: 把减仓档钳在最低单之上).  A tenth of a small chain falls
# under the minimum order size, and a sub-minimum order is a zero order
# -- which would quietly turn the tilt into the gate it is chartered
# never to become.  So a tilted chain's stake never drops below one
# minimum clip per leg (and never rises above what the untilted budget
# would have allowed).  The clip comes from production's own
# cash_clip_grid at run time, the same number ``clip_below_minimum``
# enforces in sizing; the rule travels in the fingerprint, the value
# deliberately does not -- the same looseness the peak cutoffs accept.
_tilt_min_clip_cache: list[float] = []


def _tilt_minimum_clip(conn: sqlite3.Connection) -> float:
    if not _tilt_min_clip_cache:
        env = os.environ.get("PAPER_LEDGER_TILT_MIN_CLIP", "").strip()
        if env:
            value = float(env)
        else:
            try:
                row = conn.execute(
                    "SELECT artifact_json FROM basket_delta_v2_policy_artifact"
                    " ORDER BY rowid DESC LIMIT 1"
                ).fetchone()
                value = (
                    min(json.loads(str(row[0]))["cash_clip_grid"])
                    if row else 0.25
                )
            except (sqlite3.Error, KeyError, ValueError, TypeError):
                value = 0.25
        _tilt_min_clip_cache.append(float(value))
    return _tilt_min_clip_cache[0]
# When the order stops trying: past the hours in which this station's
# daily maximum actually lands, not at settlement (user ruling,
# 2026-08-20, revised from a fixed local hour).  Per station: the P-th
# percentile of the local time-of-day at which the running maximum was
# last touched, over the trajectory lane's observed days, plus a buffer.
# Stations with too few observed days fall back to the pooled percentile
# across all stations.  0 disables the gate and restores "until the day
# resolves".  The derived cutoffs move as observation days accrue; the
# parameters travel in the fingerprint, the values deliberately do not --
# same looseness the band refits already accept.
MAKER_RESTING_CUTOFF_PERCENTILE = float(
    os.environ.get("PAPER_LEDGER_MAKER_CUTOFF_PERCENTILE", "95") or 0.0
)
MAKER_RESTING_CUTOFF_BUFFER_MINUTES = float(
    os.environ.get("PAPER_LEDGER_MAKER_CUTOFF_BUFFER_MIN", "60") or 0.0
)
MAKER_RESTING_CUTOFF_MIN_DAYS = int(
    os.environ.get("PAPER_LEDGER_MAKER_CUTOFF_MIN_DAYS", "15") or 0
)
# Control cell only, not a candidate default: withdraw a leg whose ask has
# gapped *through* its lowest rung by at least this many ticks -- a
# market-triggered rule the standing ruling forbids.  It exists to price
# that ruling: one sweep cell with it on measures what "the market has no
# say" costs in ROI, and the number goes to the user.  0 keeps it off.
MAKER_RESTING_CRASH_TICKS = float(
    os.environ.get("PAPER_LEDGER_MAKER_CRASH_TICKS", "0") or 0.0
)
# Charter 94 (2026-09-06): under the bid-anchored ladder the resting order is
# what production posts -- rungs on the bid, re-anchored as the bid moves.
# Production's lifecycle (``resting_ladder_lifecycle``) cancels a rung that
# fell out of the ladder after the dwell and the next compile re-posts the
# ladder that stands; this book re-posts every open rung at the current
# anchored ladder on the same cadence.  No escalation, no value cap on the
# rungs, no belief withdrawal and never a take: those arms are switched off
# here so this book scores the order production actually forms.
MAKER_RESTING_ANCHORED = MAKER_LADDER_SHAPE == "bid_anchored"
# The band arm's own values, kept so a test (or a research run flipping the
# shape in-process) can put the arm back exactly as the environment set it.
MAKER_RESTING_BAND_ARM_DEFAULTS = {
    "MAKER_RESTING_REPRICE_MINUTES": MAKER_RESTING_REPRICE_MINUTES,
    "MAKER_RESTING_ESCALATE_SHARE": MAKER_RESTING_ESCALATE_SHARE,
    "MAKER_RESTING_VALUE_LAMBDA": MAKER_RESTING_VALUE_LAMBDA,
    "MAKER_RESTING_BELIEF_GATE": MAKER_RESTING_BELIEF_GATE,
    "MAKER_RESTING_GAP_LATE": MAKER_RESTING_GAP_LATE,
    "MAKER_RESTING_CONFIRM_TAKE_TICKS": MAKER_RESTING_CONFIRM_TAKE_TICKS,
    "MAKER_RESTING_COVERAGE_BUDGET": MAKER_RESTING_COVERAGE_BUDGET,
    "MAKER_RESTING_CRASH_TICKS": MAKER_RESTING_CRASH_TICKS,
    "MAKER_RESTING_CUTOFF_PERCENTILE": MAKER_RESTING_CUTOFF_PERCENTILE,
    "MAKER_CENTRE_TAKER": MAKER_CENTRE_TAKER,
}
if MAKER_RESTING_ANCHORED:
    MAKER_RESTING_REPRICE_MINUTES = RESTING_REPRICE_DWELL_SECONDS / 60.0
    MAKER_RESTING_ESCALATE_SHARE = 0.0
    MAKER_RESTING_VALUE_LAMBDA = 0.0
    MAKER_RESTING_BELIEF_GATE = 0
    MAKER_RESTING_GAP_LATE = 2.0
    MAKER_RESTING_CONFIRM_TAKE_TICKS = 0.0
    MAKER_RESTING_COVERAGE_BUDGET = -1.0
    MAKER_RESTING_CRASH_TICKS = 0.0
    MAKER_RESTING_CUTOFF_PERCENTILE = 0.0
    MAKER_CENTRE_TAKER = False
# How the stake is split across the basket's rungs.  ``equal_shares`` is
# what the ledger has always done -- one share count for the whole basket,
# which is what atomicity means for a complete basket and is why allocation
# shape never reached these numbers.  ``centre_tilt`` reproduces
# production's ``_centre_anchored_candidates``: a base holding on the centre
# rung, leaned toward positive edge, with the far rungs floored as hedges.
# ``centre_tilt`` is not a ledger approximation of production any more: as of
# 2026-08-22 it is the only shape production offers, because the continuous
# log optimum was measured 275 USDC behind it over 187 paired station-days
# and switched off (``delta_v2_runtime.CONTINUOUS_OPTIMUM_CANDIDATES_ENABLED``).
# If that switch is ever turned back on, this default has to move with it or
# the ledger is once again scoring a shape the live engine does not trade.
ALLOCATION_SHAPE = (
    os.environ.get("PAPER_LEDGER_ALLOCATION", "").strip()
    or ("curve_solved" if CONTINUOUS_OPTIMUM_CANDIDATES_ENABLED else "centre_tilt")
)
ALLOCATION_SHAPES = (
    "equal_shares", "centre_tilt", "curve_solved", "edge_weighted",
)
# Which price the stake rule and the payout test are struck against.
#
# ``crossing`` -- the standing rule again since 2026-08-27 (user ruling) --
# judges the seat at the offer: the ask is information, not merely a cost
# this mode never pays.  ``lane`` judged it at what the mode would actually
# pay (the resting band under maker), which held for one day (2026-08-26)
# and was reversed by three same-entry rebuilds: crossing-priced gates
# measured +50.7, +55.5 and +62.8 USDC against lane-priced ones.  The
# mechanism is the 55-chain decomposition's bad-bet half: a basket that is
# only +EV at the price we hope to fill at has already been priced out by
# the market's open ask, and the hoped-for fills select for the times the
# market knew better.  Execution is untouched: orders still rest in the
# band, and whatever they save against the crossing price is now a bonus
# on top of the decision rather than the assumption underneath it.
#
# ``SEAT_PRICING_LANE_STAGES`` is the ruling's one exemption: P0-M posts
# once and takes whatever book it finds, so a crossing gate there mostly
# closes a stage that is already thin.
SEAT_PRICING_BASIS = (
    os.environ.get("PAPER_LEDGER_SEAT_PRICING", "crossing").strip()
    or "crossing"
)
SEAT_PRICING_BASES = ("lane", "crossing")
if SEAT_PRICING_BASIS not in SEAT_PRICING_BASES:
    raise ValueError(f"unknown seat pricing basis: {SEAT_PRICING_BASIS}")
SEAT_PRICING_LANE_STAGES = tuple(
    stage.strip()
    for stage in os.environ.get(
        "PAPER_LEDGER_SEAT_PRICING_LANE_STAGES", "P0-M"
    ).split(",")
    if stage.strip()
)
# Every structural rule a recorded seat depends on.  Positions are derived rows,
# so a change to any of these makes the ones already in the table describe a
# strategy that no longer runs -- the ladder floor landed on 2026-08-15 and the
# five two-bucket P0 seats written before it stayed on the page for a day,
# because the pass skips a stage it has already recorded.  Rows stamped with a
# different rule set are dropped and re-derived instead.
# The fingerprint identifies a rule set exactly; this names it in a way a
# reader can hold.  The page shows one per basket, so a day compiled under a
# retired rule is visibly different from a day compiled under the current
# one rather than silently comparable to it.  Kept short and mechanical --
# it is derived from the same constants the fingerprint hashes, so the two
# cannot drift.
SELECTION_RULE_LABEL = "·".join(filter(None, (
    {"probability_mass": "D-质量", "kl": "K-散度"}.get(
        "probability_mass", "probability_mass"
    ),
    "整篮EV",
    # Charter 66: the two chain-level clauses every top-up answers to.
    # Always on, so always in the label -- a book compiled without them is
    # a different strategy and must not read as comparable.
    "加仓链增益>0·链本<%g" % CHAIN_COST_PAR_BOUND,
    None
    if P1_ENTRY_LOCAL_HOUR <= 0
    else "P1≥本地%g时" % P1_ENTRY_LOCAL_HOUR,
    # Charter 69 / user ruling 2026-09-02: which clamp this book ran and
    # whether a stage may seat itself again.  Both change every row a book
    # records -- the 2026-09-02 reseat replay moved the same 405 station
    # -days from +8.3% (off) to +14.9% (guarded) -- so a book compiled
    # without them must not read as comparable to one compiled with them.
    None if DECLINE_CLAMP == "off" else "钳位%s" % DECLINE_CLAMP,
    None if DECLINE_MIN_DROP <= 0 else "降幅≥%g" % DECLINE_MIN_DROP,
    "反复入座" if RESEAT_WITHIN_STAGE else None,
    None
    if P0_ENTRY_DELAY_HOURS <= 0
    else "P0挂牌后%g时" % P0_ENTRY_DELAY_HOURS,
    None
    if CHAIN_TOPUP_MISS_FLOOR <= 0
    else "加仓漏桶地板%g" % CHAIN_TOPUP_MISS_FLOOR,
    None
    if CHAIN_TOPUP_BANKROLL_USDC <= 0
    else "加仓本金%g" % CHAIN_TOPUP_BANKROLL_USDC,
    "中心底仓",
    f"腿({LEG_FLOOR_PRICE_BASIS})≥{MINIMUM_LEG_ALL_IN_ASK:g}",
    None if EXCHANGE_LOT_TOP_UP == "off" else "补足一手",
    None if WINDOW_STRATEGY == "live" else WINDOW_STRATEGY,
    None if EXECUTION_MODE == "taker" else EXECUTION_MODE,
    None
    if not MAKER_RESTING_ANCHORED
    else "买一±1跳三档挂单·不吃单·每%g分钟随买一重挂" % (
        RESTING_REPRICE_DWELL_SECONDS / 60.0
    ),
    None
    if EXECUTION_MODE != "maker_resting" or MAKER_RESTING_ANCHORED
    else f"阶梯{MAKER_RESTING_LADDER_STEPS}档·逐步建仓",
    None
    if EXECUTION_MODE != "maker_resting"
    else (
        f"挂{MAKER_RESTING_HORIZON_MINUTES:g}分钟"
        if MAKER_RESTING_HORIZON_MINUTES > 0
        else "挂至达峰P%g+%g分" % (
            MAKER_RESTING_CUTOFF_PERCENTILE,
            MAKER_RESTING_CUTOFF_BUFFER_MINUTES,
        )
        if MAKER_RESTING_CUTOFF_PERCENTILE > 0
        else "挂到结算"
    ),
    None
    if EXECUTION_MODE != "maker_resting" or MAKER_RESTING_REPRICE_MINUTES <= 0
    else f"每{MAKER_RESTING_REPRICE_MINUTES:g}分钟重挂",
    None
    if EXECUTION_MODE != "maker_resting" or MAKER_RESTING_ESCALATE_SHARE <= 0
    else f"未成交档{MAKER_RESTING_ESCALATE_SHARE:g}窗程收拢",
    None
    if EXECUTION_MODE != "maker_resting"
    else "缺口驱压三段·晚段值内吃单·核芯对冲分账",
    None
    if EXECUTION_MODE != "maker_resting" or (
        abs(MAKER_RESTING_GAP_MID - 1.0 / 3.0) < 1e-9
        and abs(MAKER_RESTING_GAP_LATE - 2.0 / 3.0) < 1e-9
    )
    else "相位%.2f/%.2f" % (
        MAKER_RESTING_GAP_MID, MAKER_RESTING_GAP_LATE
    ),
    None
    if EXECUTION_MODE != "maker_resting"
    or MAKER_RESTING_CONFIRM_TAKE_TICKS <= 0.0
    else "同向确认吃单≥%g跳" % MAKER_RESTING_CONFIRM_TAKE_TICKS,
    None
    if EXECUTION_MODE != "maker_resting" or not MAKER_CENTRE_TAKER
    else "中心桶入场吃单",
    None
    if SELECTION_DISCOUNT <= 0
    else f"选择折扣λs{SELECTION_DISCOUNT:g}",
    None
    if EFFECTIVE_EDGE_MODE != "on"
    else "有效边际λ(a)收缩·P0",
    None
    if EFFECTIVE_EDGE_MODE != "on" or EFFECTIVE_EDGE_SCOPE == "window"
    else "有效边际按桶",
    None if LEG_VETO != "on" else "逐腿否决",
    None if CLAIM_EDGE_THERMOMETER_MODE != "on"
    else "薄声称边际≤%g直服" % CLAIM_EDGE_THIN_BAND_MAX,
    # The only opening rule there is, now that the ticket is retired (user
    # ruling 2026-09-08): P0 sizes on the whole-basket Kelly stake or opens
    # nothing at all.
    "P0整篮EV按Kelly定注",
    None
    if SHAPE_GAP_LIMIT_BUCKETS <= 0
    else (
        "形状撕裂门%g桶" % SHAPE_GAP_LIMIT_BUCKETS
        if SHAPE_GAP_TILT <= 0
        else "撕裂减仓%g桶×%g" % (SHAPE_GAP_LIMIT_BUCKETS, SHAPE_GAP_TILT)
    ),
    "固化减仓已退役(恒等)"
    if CONSOLIDATION_TILT_FACTOR >= 1.0
    else "固化≥%d减仓%g" % (
        CONSOLIDATION_TILT_MIN_SIGNALS, CONSOLIDATION_TILT_FACTOR
    ) + (
        ""
        if CONSOLIDATION_TILT_SIGNALS == ("stable", "sharp", "quoted")
        else "·灯" + "+".join(CONSOLIDATION_TILT_SIGNALS)
    ) + "·钳交易所最小单×%d腿" % CONSOLIDATION_TILT_FLOOR_LEGS + (
        "" if CONSOLIDATION_TILT_SCOPE == "opener" else "·逐段灯"
    ),
    None
    if EXECUTION_MODE != "maker_resting" or MAKER_RESTING_VALUE_LAMBDA <= 0
    else f"价值钳λ{MAKER_RESTING_VALUE_LAMBDA:g}·中心桶豁免",
    # Which price the stake rule and the refusal copy judge on.  Taker has
    # only one, so it says nothing; a maker run says so, because rows on
    # either side of 2026-08-26 -- and again 2026-08-27 -- were sized
    # against different numbers.
    None
    if EXECUTION_MODE == "taker"
    else (
        "席位按挂单价定价(对照)"
        if SEAT_PRICING_BASIS == "lane"
        else "闸按吃单价" + (
            ""
            if not SEAT_PRICING_LANE_STAGES
            else "·%s按挂单价" % "+".join(SEAT_PRICING_LANE_STAGES)
        )
    ),
    {"off": None, "shadow": "β闸影子", "on": "β闸正式(q+β_lcb·(p−q))"}[BETA_EDGE_MODE],
    "离心折扣已退役(恒等)" if OFF_CENTRE_CLAIM_MODE == "off" else (
        None
        if OFF_CENTER_CLAIM_RAMP_BUCKETS == 1.0
        else f"离心折扣按距离{OFF_CENTER_CLAIM_RAMP_BUCKETS:g}桶爬坡"
    ),
    None if not CERTAINTY_GATE else "确定性闸" + "+".join(CERTAINTY_GATE),
    None
    if EXECUTION_MODE != "maker_resting" or not MAKER_RESTING_SHOULDER_LAMBDA
    else (
        "肩腿豁免"
        if MAKER_RESTING_SHOULDER_LAMBDA == "exempt"
        else f"肩腿λ{MAKER_RESTING_SHOULDER_LAMBDA}"
    ),
    None
    if EXECUTION_MODE != "maker_resting" or not MAKER_RESTING_TAIL_LAMBDA
    else (
        "尾腿豁免"
        if MAKER_RESTING_TAIL_LAMBDA == "exempt"
        else f"尾腿λ{MAKER_RESTING_TAIL_LAMBDA}"
    ),
    None
    if EXECUTION_MODE != "maker_resting" or not MAKER_RESTING_BELIEF_GATE
    else "信念EV闸",
    None
    if EXECUTION_MODE != "maker_resting" or MAKER_RESTING_CRASH_TICKS <= 0
    else f"砸穿{MAKER_RESTING_CRASH_TICKS:g}跳撤腿(对照)",
    "生产裁判" if JUDGE == "production" else (
        "生产选窗" if JUDGE == "production_seat" else "账本裁判"
    ),
    "整篮加仓" if TOPUP_ATOMIC == "on" else "只买缺腿",
    "不足一手不入座",
    "均值中心",
    "按checkpoint决策",
    "在任窗不免检",
    "席位形状2–3桶(对称±1/偏斜顺尾)",
    "整股下单" if ORDER_SHARE_GRID == "whole" else None,
    "含中心窗抬价" if CENTER_WINDOW_CLAIM_BANDS else None,
    "双向收敛抬价" if CONVERGENCE_CLAIM_MODE == "on" else None,
    "定注另读校准曲线" if SIZING_CLAIM_BANDS else None,
    (
        "Kelly份额·%s" % KELLY_SHARE_SHAPE
        if KELLY_SHARE_SHAPE != "equal" else None
    ),
    "生产种子" if JUDGE != "ledger" else None,
    "一本账·只加不减",
)))
# Charter 110 (extended 2026-09-08): the claim-transform vintage written
# beside every candidate window so the lambda fit can partition on it.
# Two fingerprints, not one: a window holding the centre goes through
# charter 81's lift and never touches the off-centre table, and the
# reverse, so a single union key would retire the whole sample whenever
# either table moved.  Read once each -- kernel constants cannot move
# mid-run.
CLAIM_TRANSFORM_SHA256_CENTRE = claim_transform_fingerprint(holds_centre=True)
CLAIM_TRANSFORM_SHA256_OFF_CENTRE = claim_transform_fingerprint(
    holds_centre=False
)
_SELECTION_RULE_IDENTITY: dict[str, Any] = {
    "schema": "paper_trade_selection_rule_v6",
    "compounding": "credible_fills_only_from_settlements",
    # Charter 70: sub-lot rungs lifted to one exchange lot, or not.  Kernel
    # constant, read by production too; v6 so the pre-charter-70 book
    # re-derives instead of pooling with rows sized under it.
    "exchange_lot_top_up": EXCHANGE_LOT_TOP_UP,
    "dust_cost_floor": DUST_COST_FLOOR,
    "entry": "first_cycle_whose_objective_seats",
    "pre_listing_decision": "first_book_after_listing",
    "fee_rate": FEE_RATE,
    # The ledger replays production's own ``_support_windows``, so a change
    # to how that ranks or filters is a change to what the ledger records --
    # and rows written under the old one describe a strategy that no longer
    # runs.  These two travel with the fingerprint for the same reason the
    # ladder floor does.
    # 2026-09-12 evening (charter 131, user ruling "改为有效 edge"): the
    # shortlist is ordered by the effective edge lambda(a)*p - C.  The
    # morning's effective-mass ordering and the pre-noon raw-claim one are
    # both other books.
    "window_ranking": "effective_edge",
    # The label rides in the fingerprint after all.  Left out of it, a rule
    # change that only renames itself leaves old rows in place and the
    # equity curve compounds two rule sets together -- measured: 2026-08-17
    # seated LEMD at 16.88 (2% of 844) beside RKSI at 14.52 (2% of 726) on
    # the same day, because half the day was re-derived and half was not.
    "selection_rule_label": SELECTION_RULE_LABEL,
    "allocation_shape": ALLOCATION_SHAPE,
    "allocation_basis": "share_count",
    # The divisor of ``all_in_cost`` under weighted allocation.  It is a
    # recorded quantity, so changing how it is computed changes what every
    # row means -- and leaving it out of the fingerprint let a corrected
    # ledger keep serving rows derived under the old arithmetic, which is
    # how a "verification" ended up reading the numbers it was meant to
    # check.
    "cost_basis": "expected_payout_equivalent_shares",
    # Charter 66: how a top-up is sized and what it must clear.  Both are
    # rules about what the ledger records, so both travel in the identity.
    "chain_topup_sizing": "tilt_delta_gated_by_chain_growth_and_par_v2",
    "chain_cost_par_bound": CHAIN_COST_PAR_BOUND,
    "chain_topup_bankroll": (
        CHAIN_TOPUP_BANKROLL_USDC or "scope_allowance"
    ),
    "chain_topup_miss_floor": CHAIN_TOPUP_MISS_FLOOR,
    # Charter 71: both top-up gates, versioned in the kernel.
    "topup_gates": CHARTER_71_TOPUP_GATE_VERSION,
    **(
        {"topup_increment_edge": TOPUP_INCREMENT_EDGE}
        if TOPUP_INCREMENT_EDGE != "off" else {}
    ),
    # P0 sizing is hashed unconditionally above (charter 96); the old
    # conditional entry named the same switch and is retired with it.
    # Research arm of 2026-09-06: how the Kelly stake is split across the
    # seat's rungs.  Production's switch, read from production; hashed only
    # away from ``equal`` so the standing book keeps its digest.
    **(
        {
            "kelly_share_shape": KELLY_SHARE_SHAPE,
            "kelly_edge_tilt_fraction": KELLY_EDGE_TILT_FRACTION,
        }
        if KELLY_SHARE_SHAPE != "equal" else {}
    ),
    # The per-leg top-up gap applies to every shape, equal included, so it
    # is hashed on its own whenever it leaves charter 72's atomic rule.
    **(
        {"kelly_shape_topup": KELLY_SHAPE_TOPUP}
        if KELLY_SHAPE_TOPUP != "atomic" else {}
    ),
    # The share grid an order is written on (user ruling, 2026-09-06),
    # hashed unconditionally like charter 79's seat: it is not a research
    # arm sitting on the standing behaviour, it changes what every seat
    # buys.  Rows sized on the old continuous grid and rows sized on whole
    # shares are not the same book, and a digest that could not tell them
    # apart is a digest that lets them be summed.
    "order_share_grid": ORDER_SHARE_GRID,
    # Charter 131 (2026-09-12): the seat's shape and width are the ruling's,
    # hashed unconditionally -- rows seated under charters 50/55/79/122 are
    # not this book.
    "seat_shape": SEAT_SHAPE_RULE_VERSION,
    "seat_width": "%d..%d" % (SEAT_MIN_BUCKETS, SEAT_MAX_BUCKETS),
    # Charter 131 (2): the market-lean gate's mode is a rule of the book
    # (shadow by ruling; ``on`` is the arm).  Hashed unconditionally.
    "market_lean_gate": MARKET_LEAN_GATE_MODE,
    # Charter 81: the centre-holding claim lift, hashed unconditionally --
    # production's constant, read from production; [] is the off arm.
    "center_window_claim": [
        [bound, factor] for bound, factor in CENTER_WINDOW_CLAIM_BANDS
    ],
    # The admission/sizing split (2026-09-06), hashed only when on.
    **(
        {"sizing_claim": [
            [bound, factor] for bound, factor in SIZING_CLAIM_BANDS
        ]}
        if SIZING_CLAIM_BANDS else {}
    ),
    # Arm B (2026-09-06): the convergence claim lift, hashed only when on --
    # shadow changes no decision and must not re-address the book.
    **(
        {"convergence_claim": {
            "rule": CONVERGENCE_CLAIM_RULE_VERSION,
            "factor": CONVERGENCE_CLAIM_FACTOR,
            "horizon_hours": CONVERGENCE_CLAIM_HORIZON_HOURS,
            "stages": sorted(CONVERGENCE_CLAIM_STAGES),
            "min_agreement": CONVERGENCE_CLAIM_MIN_AGREEMENT,
            "min_claim_rise": CONVERGENCE_CLAIM_MIN_CLAIM_RISE,
            "min_price_fall": CONVERGENCE_CLAIM_MIN_PRICE_FALL,
        }}
        if CONVERGENCE_CLAIM_MODE == "on" else {}
    ),
    # Charter 90 (2026-09-06): the P0-M seat holds the TAF TX bucket.  A rule
    # of the standing book, hashed unconditionally -- a book seated without
    # it may not share a fingerprint with one seated under it.
    "p0m_taf": P0M_TAF_MODE,
    "p0m_taf_rule": P0M_TAF_ENVELOPE_RULE_VERSION,
    **(
        {"chain_opening_stages": sorted(CHAIN_OPENING_STAGES)}
        if CHAIN_OPENING_STAGES != {"P0"} else {}
    ),
    **(
        {"effective_edge_lambda_p0": EFFECTIVE_EDGE_LAMBDA_P0_OVERRIDE}
        if EFFECTIVE_EDGE_LAMBDA_P0_OVERRIDE else {}
    ),
    "p1_entry_local_hour": P1_ENTRY_LOCAL_HOUR,
    # Charter 69 (v5): the clamp arm, its threshold and the seat cadence.
    "decline_clamp": DECLINE_CLAMP,
    "decline_min_drop": DECLINE_MIN_DROP,
    "reseat_within_stage": RESEAT_WITHIN_STAGE,
    # Research arms of 2026-09-03, hashed only away from their defaults so
    # the standing book keeps its fingerprint.
    **(
        {"effective_edge_scope": EFFECTIVE_EDGE_SCOPE}
        if EFFECTIVE_EDGE_SCOPE != "window" else {}
    ),
    # The same arm on the OTHER side of the fence, and the reason it needs
    # its own key.  Charter 68's scope is two constants with two environment
    # names -- ``DELTA_V2_EFFECTIVE_EDGE_SCOPE`` in the runtime and
    # ``PAPER_LEDGER_EFFECTIVE_EDGE_SCOPE`` here -- and the runtime's comment
    # says "the ledger carries the same switch and the two must move
    # together" while nothing made them.  Under the default production judge
    # it is the RUNTIME's constant that deflates the seat, so setting only
    # that one changed every book and moved no digest; setting only this one
    # moved the digest and left the production judge deflating by window.
    # Both directions produce an arm that cannot be told from the baseline,
    # which is the defect this file already carries a scar from (charter 72,
    # 2026-09-09).  Hashing what actually decides makes the mismatch visible
    # instead of silent.
    **(
        {"production_effective_edge_scope": _PRODUCTION_EDGE_SCOPE}
        if _PRODUCTION_EDGE_SCOPE != "window" else {}
    ),
    **({"leg_veto": LEG_VETO} if LEG_VETO != "off" else {}),
    # The judge and the top-up shape are the book's identity now, not arms:
    # hashed unconditionally so a flip either way re-derives the book.
    "judge": JUDGE,
    **(
        {"judge_bankroll": JUDGE_BANKROLL}
        if JUDGE_BANKROLL != "station" else {}
    ),
    **(
        {"stage_self_open": STAGE_SELF_OPEN}
        if STAGE_SELF_OPEN != "off" else {}
    ),
    "topup_atomic": TOPUP_ATOMIC,
    # Charter 137, arm.  Hashed only while the basis is away from ``net``,
    # so today's digest -- and the whole book behind it -- is untouched.
    # The literal is deliberate: it names the PRE-CLAUSE book, not "the
    # current default".  Written as ``!= KELLY_TOPUP_BASIS_DEFAULT`` it
    # would move with the flip and the two books would share a digest, so
    # the charter-129 auto re-import would never fire -- the trap charter
    # 127(七丙) walked into with ``resting_lifecycle``.  Pinned by
    # tests/test_charter_137_kelly_topup_basis.py.
    **(
        {
            "kelly_topup_basis": KELLY_TOPUP_BASIS,
            "kelly_topup_basis_rule": KELLY_TOPUP_BASIS_RULE_VERSION,
        }
        if KELLY_TOPUP_BASIS != "net" else {}
    ),
    # 2026-09-04 (user ruling): the solver carries the chain law and the
    # ticket, the exchange lot refuses a fraction, and the station dial is
    # a per-leg execution gate.  Hashed unconditionally: every row before
    # it was sized and filled under rules production does not run.
    # v4 (2026-09-04): the walk carries the envelope's own vector under the
    # production judge, the consolidation lookback and the clamp's previous
    # checkpoint span every stage.  Rows before it recorded a claim the
    # solver never saw.
    # v6 (2026-09-04): a checkpoint is judged only while its stage holds
    # the day's authority, as ``stages.select_current_stage`` defines it.
    # Rows before it include 141 P0 seats production could not have taken.
    # v7 (2026-09-04): a taker fills only at the top of the offer and is
    # cut to what is there; a pre-listing checkpoint is not paired with a
    # book a newer checkpoint owns; charter 73 authorizes the increment.
    # v8 (2026-09-04): a checkpoint is priced on the book production
    # compiled it on -- the first at or after it -- never on one before it.
    # v9 (2026-09-04): every book while the checkpoint is current is a
    # cycle, as production runs them; charter 77 picks the decisions.
    # v10 (2026-09-04): the chain's tilt is the opener's, recorded once and
    # inherited by every later seat, P0 reseats included.
    # v11 (2026-09-04): the row says whether it is the solver's ticket.
    # v12 (2026-09-05): the gate audit's three rulings -- the clamp
    # retired, the market-lean gate promoted, P0 sized at a ticket -- plus
    # the seven charter clauses that gained a research switch.  Each
    # clause is hashed only away from its shipped value, so the standing
    # book keeps its digest when nothing is flipped.
    "production_judge": "solver_chain_gates_exchange_lot_centre_v12",
    **{
        key: value
        for key, value in (
            ("charter_66a_chain_par", CHARTER_66A_CHAIN_PAR),
            ("charter_70_exchange_lot", CHARTER_70_EXCHANGE_LOT),
            ("charter_83_min_notional", CHARTER_83_MIN_NOTIONAL),
            # Charter 84 never binds on this book -- its fills are whole,
            # so no seat is ever venue-short -- but the switch is read from
            # production so an arm that flips it flips it on both sides.
            ("charter_84_ladder_balance", CHARTER_84_LADDER_BALANCE),
            ("charter_71a_topup_identity", CHARTER_71A_TOPUP_IDENTITY),
            ("charter_71b_chain_growth", CHARTER_71B_CHAIN_GROWTH),
            ("charter_72_kelly_sizing", CHARTER_72_KELLY_SIZING),
            ("charter_77_decision_moments", CHARTER_77_DECISION_MOMENTS),
            ("charter_78_no_exemption", CHARTER_78_INCUMBENT_NO_EXEMPTION),
        )
        if value != "on"
    },
    # Charter 87 (2026-09-06): the whole-basket positive-EV gate reads the
    # lifted claim times lambda(a), and charter 74's ticket waits for it.  A
    # change to the standing rule, so the key is unconditional -- rows
    # judged under the old gate must not share a book with rows judged
    # under this one; ``off`` is the pre-ruling gate and hashes away.
    "gate_belief": (
        "lifted_claim_times_lambda_v1"
        if CHARTER_87_GATE_EFFECTIVE_EDGE == "on" else "lifted_claim_v0"
    ),
    # Charter 128 (2026-09-12): the beta lane.  ``shadow`` records and gates
    # nothing; ``on`` replaces the gate's and the solver's belief.  Hashed
    # unconditionally -- rows judged on beta and rows judged on lambda are
    # not one book, and the shadow rows carry readings the others lack.
    "beta_edge_mode": BETA_EDGE_MODE,
    "beta_edge_rule": BETA_EDGE_RULE_VERSION,
    "stage_authority": "highest_committed_stage_v1",
    "checkpoint_books": "every_committed_book_while_current_v1",
    "taker_fill": "top_of_offer_cut_to_depth_v1",
    "execution_authorization": "whole_basket_edge_v1",
    # Charter 94 (2026-09-06): the order family and the ladder's shape.
    # Unconditional: the standing family changed.
    "execution_family": EXECUTION_FAMILY,
    "ladder_shape": MAKER_LADDER_SHAPE,
    # Charter 96 (2026-09-06): the ticket is retired and P0 sizes by the
    # whole-basket Kelly stake.  Unconditional on both counts: a book whose
    # opens were flat tickets is not the same book as one whose opens were
    # sized on the deflated edge, and the lambda partition the stake reads
    # gained a boundary the same day.
    # Charter 97 (2026-09-06): the claim-edge thermometer serves as lambda's
    # second axis.  Unconditional -- a book judged on the agreement axis
    # alone is not the same book, and the thin band's width and served value
    # are the rule, not a tuning knob.
    "claim_edge_thermometer": CLAIM_EDGE_THERMOMETER_MODE,
    "claim_edge_thin_band": "%g|%g" % (
        CLAIM_EDGE_THIN_BAND_MAX, CLAIM_EDGE_THIN_BAND_LAMBDA
    ),
    "p0_opening_sizing": "kelly",
    # 2026-09-09: which way the Kelly fraction enters the curve's bankroll.
    # Hashed only away from the corrected ``w_times_f``, so the standing book
    # keeps its digest: under charter 72 the curve decides nothing, so the
    # correction moves recorded scores and not a single seat.  An arm that
    # reproduces the shipped ``W/f`` arithmetic -- and any curve-form arm run
    # under it -- must not share a book with one that does not.
    **(
        {"curve_bankroll_convention": _CURVE_BANKROLL_CONVENTION}
        if _CURVE_BANKROLL_CONVENTION != "w_times_f" else {}
    ),
    # Charter 94 (五): the price the target inventory is chosen against.
    # Production reads it and the production judge sizes under it, so it
    # decides what this book records -- and it was never hashed: a replay
    # under ``lane`` (2026-09-10) produced a book whose fingerprint was byte
    # for byte the standard one.  Off-default only, so the standard book
    # keeps its sha; the lane-stage exemption travels with it.
    **(
        {"target_pricing_basis": _TARGET_PRICING_BASIS}
        if _TARGET_PRICING_BASIS != "ask_depth" else {}
    ),
    # Charter 127 (五): how a designated completion is executed.  Hashed
    # unconditionally, because this book now SIMULATES the difference -- a
    # crossing completion and a resting one are two strategies, and 392 of
    # the 924 seats in the 08-28..09-08 window are completions.  A rule this
    # lane reads and does not hash is how an arm comes to be indistinguish-
    # able from its own baseline.
    # Charter 84: a designated completion buys only the short rungs, and
    # this lane's atomic top-up arm no longer refuses it for that.  Recorded
    # unconditionally because it changes what the book BUYS in every arm --
    # the 392 completions the judge seats over 08-28..09-08 were all refused
    # 整篮不动 before it.
    "completion_topup_rule": "short_legs_only",
    "completion_execution": (
        "limit_fok" if _CHARTER_127_COMPLETION_TAKE == "on" else "resting"
    ),
    # Charter 127 (五), 2026-09-14: the crossing completion is priced at its
    # crossing at every stage.  At P0-M it was priced on the maker band and
    # refused at every compile; the judge this book seats under is
    # production's own solver, so the seats it produces change with it.
    # Charter 136 (user rulings 2026-09-15): a broken seat is completed when
    # the exposure it removes outweighs the edge it gives up, inside a budget
    # off the seat's own sizing; the judge's windows admit it, its solver
    # sizes it on that budget and this book's edge mirror exempts it.
    "charter_136_completion": (
        _COMPLETION_RISK_BUDGET_RULE if _CHARTER_136_COMPLETION == "on" else "off"
    ),
    # Charter 143 (user ruling 2026-09-17): a broken seat is completed by its
    # held count -- multi-leg in full while the worst in-window settlement
    # improves, single-leg on the legs that clear the hurdle.
    "charter_143_completion": (
        _COMPLETION_143_RULE if CHARTER_143_COMPLETION == "on" else "off"
    ),
    "completion_pricing": (
        _COMPLETION_CROSSING_PRICING
        if _CHARTER_127_COMPLETION_TAKE == "on" else "lane"
    ),
    **(
        {"target_pricing_lane_stages": "|".join(_TARGET_PRICING_LANE_STAGES)}
        if tuple(_TARGET_PRICING_LANE_STAGES) != ("P0-M",) else {}
    ),
    "effective_edge_band_bounds": "|".join(
        "%g" % bound for bound in EFFECTIVE_EDGE_BAND_BOUNDS
    ),
    # Charter 92 (2026-09-06): the ticket is authorized on the same
    # whole-basket edge as every other increment.  Unconditional: the
    # standing rule changed, and rows judged under the structural rule must
    # not share a book with these.
    "ticket_authorization": (
        "whole_basket_edge_v1"
    ),
    # Charter 49's centre gate as an arm: refusing versus pricing.  Hashed
    # away from the default only, so the standing book keeps its sha.
    **(
        {"centre_contain_gate": _CENTRE_CONTAIN_GATE}
        if _CENTRE_CONTAIN_GATE != "on" else {}
    ),
    # Charter 127 (七)(丙): whose resting lifecycle this book simulated.
    # Hashed unconditionally.  It was hashed only away from the default
    # while ``ledger`` was the default; flipping the default under that
    # rule would have left the old book and the new one sharing a digest
    # -- the inverse of the rehash-without-behaviour trap, and the worse
    # half, since a shared digest is what lets two strategies be summed.
    "resting_lifecycle": RESTING_LIFECYCLE,
    # Charter 110 (三), 2026-09-14: which per-rung rule that lifecycle ran.
    # v2 clamps the anchor to the post-only ceiling, so a rung on a one-tick
    # book rests instead of re-posting at the same price every dwell -- a
    # different fill population, hence a different book.
    "resting_rung_action_rule": RESTING_RUNG_ACTION_RULE_VERSION,
    # Charter 93 (2026-09-06): the in-support floor is zero of the premium
    # and re-read on the lifted legs.  Unconditional, since the standing
    # rule changed.
    "support_scenario_floor": (
        "%g|after_lift:%s"
        % (SUPPORT_SCENARIO_FLOOR_FRACTION, CHARTER_93_SUPPORT_FLOOR)
    ),
    **(
        {"lot_lift_budget": LOT_LIFT_BUDGET}
        if LOT_LIFT_BUDGET != "untilted" else {}
    ),
    "exchange_minimum_lot_fallback_shares": (
        EXCHANGE_MINIMUM_LOT_SHARES_FALLBACK
    ),
    # Charter 83 (2026-09-05): the venue's dollar floor changes how every
    # cheap rung is sized, so the standing book re-keys on it.
    "exchange_minimum_order_notional_usdc": (
        EXCHANGE_MINIMUM_ORDER_NOTIONAL_USDC
    ),
    # Charter 75: the centre and, under the production judge, the seed.
    "centre": "mean_bucket_v1",
    # Charter 77/78: decision moments and the incumbent's (absent)
    # exemption from the positive-EV gate.
    "decision_moments": DECISION_MOMENT_RULE_VERSION,
    "incumbent_edge_exemption": "off",
    "seed": (
        "production_choose_support_v2"
        if JUDGE != "ledger" else "entry_support_core_v1"
    ),
    "window_strategy": WINDOW_STRATEGY,
    "execution_mode": EXECUTION_MODE,
    # Which price the Kelly stake and the payout test are struck against.
    # ``_support_windows`` is production's and prices at the offer, so
    # before 2026-08-26 every maker row was sized on a crossing total it
    # would never pay -- and 57 refusals said the market asked more than a
    # full payout when this lane's own price did not.  Hashed only away
    # from taker, where the two prices are the same number and the standing
    # taker book must keep its fingerprint.
    **(
        {
            "seat_pricing_basis": SEAT_PRICING_BASIS,
            "seat_pricing_lane_stages": list(SEAT_PRICING_LANE_STAGES),
        }
        if EXECUTION_MODE != "taker" else {}
    ),
    # The ladder's rungs are now floored to the exchange's tick, because
    # they are now production's own ``_maker_ladder`` rather than a copy of
    # it that rounded to six decimals.  Rows written before 2026-08-26 were
    # filled against prices no order could have carried, so they are not
    # comparable to rows written after.
    "maker_ladder_price_grid": "exchange_tick",
    "maker_ladder_steps": MAKER_RESTING_LADDER_STEPS,
    # How long the order rests and how often it is re-posted.  Both change
    # which fills a recorded row is built on, so rows written under the old
    # values describe a different execution.
    "maker_horizon_minutes": MAKER_RESTING_HORIZON_MINUTES,
    "maker_reprice_minutes": MAKER_RESTING_REPRICE_MINUTES,
    "maker_improve_ticks": MAKER_RESTING_IMPROVE_TICKS,
    # Conditional so the default book's fingerprint is untouched: the knob
    # is a research arm, and hashing its empty default would re-derive
    # every production row for a rule nothing runs.
    **(
        {"maker_band_top": MAKER_RESTING_BAND_TOP}
        if MAKER_RESTING_BAND_TOP else {}
    ),
    # Unconditional since the 2026-08-25 charter 54 flip to ``mean``: the
    # definition is now a live rule, not a research arm, and rows either
    # side of any future change must not share a fingerprint.  The
    # definition itself lives in ``delta_v2_runtime`` so production and
    # this ledger move together.
    "ladder_center_definition": LADDER_CENTER_DEFINITION,
    # How far off centre buys the whole claim deflation.  1 is the fitted
    # two-value rule and the standing book's; hashed only when it departs,
    # so the default fingerprint is untouched by the knob existing.
    **(
        {"off_center_claim_ramp_buckets": OFF_CENTER_CLAIM_RAMP_BUCKETS}
        if OFF_CENTER_CLAIM_RAMP_BUCKETS != 1.0 else {}
    ),
    # Retired 2026-09-12: the off-centre deflation is identity by default.
    # Hashed unconditionally -- rows judged under the deflation and rows
    # judged on the raw claim are not one book.
    "off_centre_claim_mode": OFF_CENTRE_CLAIM_MODE,
    # The skew gate's unit changed on 2026-09-11 from half a bucket to 0.35
    # of the served sigma; rows written under the bucket gate describe a
    # different rule at the default, so the basis is hashed always and the
    # value only away from its charter default.
    "skew_gap_threshold_basis": "sigma",
    **(
        {"skew_gap_threshold_sigma": SKEW_GAP_THRESHOLD_SIGMA}
        if SKEW_GAP_THRESHOLD_SIGMA != 0.35 else {}
    ),
    # Charter 130 (2026-09-12): the one economic hurdle, read at admission,
    # after the lift, and on every increment.  It replaced charter 58's
    # zero-friction gate (tick friction and margin, both hashed here until
    # then); a book seated at h=0 is not this book.
    "economic_hurdle": ECONOMIC_HURDLE_V1,
    "economic_hurdle_rule": ECONOMIC_HURDLE_RULE_VERSION,
    # The toxic-band gate changes which windows can be priced at all, so a
    # book with it on cannot share a fingerprint with the default book; the
    # key rides only when the research arm enables it.
    **(
        {"maker_max_claim_edge": MAKER_RESTING_MAX_CLAIM_EDGE}
        if MAKER_RESTING_MAX_CLAIM_EDGE else {}
    ),
    # The fill-information lifecycle changes what every reprice decision is,
    # so its book cannot share a fingerprint with the default one.
    **(
        {"maker_ppost": "v1_firsthalf_0.39_1.18_0.32_askveto0.05:"
         + ",".join(sorted(MAKER_RESTING_PPOST_COMPONENTS))}
        if MAKER_RESTING_PPOST else {}
    ),
    **(
        {"center_width_override": {
            stage: list(pair)
            for stage, pair in sorted(CENTER_WIDTH_OVERRIDE.items())
        }}
        if CENTER_WIDTH_OVERRIDE else {}
    ),
    # How the band moves while the order rests.  The floor's climb, the
    # price cap on a re-post, and the fact that a bought-out ladder is
    # replenished up to its posted depth all change which rungs fill, and
    # therefore what every resting row records.
    "maker_escalate_share": MAKER_RESTING_ESCALATE_SHARE,
    "maker_reprice_replenish": "to_posted_depth_same_price_rungs",
    # What counts as a fill.  Rows recorded under a different answer to
    # that describe a different strategy, not a differently-priced one.
    "maker_fill_model": MAKER_FILL_MODEL,
    # The two fitted numbers, recorded only when the model that reads them
    # is the one running -- a calibration that moves is a different book.
    **(
        {
            "adverse_fill_p_win": ADVERSE_FILL_P_WIN,
            "adverse_fill_p_lose": ADVERSE_FILL_P_LOSE,
        }
        if MAKER_FILL_MODEL == "adverse" else {}
    ),
    # The model's part in the price and in the withdrawal.  All of these
    # change which rungs stand where, so rows written under other values
    # describe an execution that no longer runs.  The per-station cutoff
    # *values* deliberately do not ride here (they drift as observation
    # days accrue); the parameters that derive them do.
    "maker_value_lambda": MAKER_RESTING_VALUE_LAMBDA,
    "maker_centre_lambda": MAKER_RESTING_CENTRE_LAMBDA,
    # Rows priced with the dearest rung exempt describe a different red line.
    "maker_market_top_exempt": MAKER_MARKET_TOP_EXEMPT,
    "maker_value_centre_exempt": True,
    "maker_shoulder_lambda": MAKER_RESTING_SHOULDER_LAMBDA,
    "maker_belief_gate": MAKER_RESTING_BELIEF_GATE,
    "maker_belief_source": "bridge_history_observation_stage_precedence_at_reprice",
    # Production applies the cooling-side ceiling inside P1 as of 2026-08-23.
    # A row priced against the raw posterior describes a red line the live
    # engine no longer draws, so it cannot share a fingerprint with these.
    # Charter 99 (2026-09-06): the regional curve stands in the ceiling's
    # place when it speaks.  A book priced with it may not share a
    # fingerprint with one priced without.
    # Charter 99 (2026-09-07): admission is per system and by measurement,
    # so the admitted set belongs in the label.  A book priced with the
    # layer serving every lane and one priced with it serving only the MSM
    # are different rules, and before this they shared a fingerprint.
    # Charter 95 (2026-09-07): the curve is debiased before it is fused, so
    # the correction's rule, strength and admitted set ride in the same
    # label.  A book priced on a debiased curve is not the book priced on
    # the raw one, and the two must not share a fingerprint.
    "regional_path_prior": (
        "%s:w=%g:sys=%s:debias=%s" % (
            regional_path_prior.RULE_VERSION, regional_path_prior.WEIGHT,
            "+".join(sorted(regional_path_prior.SERVING_SYSTEMS)) or "none",
            (
                "%s:x=%g:%s" % (
                    regional_point_debias.RULE_VERSION,
                    regional_point_debias.SCALE,
                    "+".join(sorted(regional_point_debias.DEBIAS_SYSTEMS)) or "none",
                )
                if regional_point_debias.ENABLED else "off"
            ),
        )
        if regional_path_prior.ENABLED and REPLAY_LAYERS_APPLIED
        # Under the production judge the ledger scores the envelope production
        # committed, which already carries this layer; the replay does not
        # apply it a second time and does not claim it as its own rule.  The
        # ceiling's entry above says the same thing in the same words.
        else "off:judge_reads_envelope" if regional_path_prior.ENABLED
        else "off"
    ),
    # Charter 95 step two (2026-09-07): the four-layer chain now stands in the
    # ceiling's place at P1's outermost point, so a book priced with it may
    # not share a fingerprint with one priced by the clock rate.  As with the
    # ceiling and the curve, under the production judge the ledger scores the
    # envelope production committed, which already carries this layer.
    "p1_four_layer_serving": (
        "regression_phase_remaining_no_reanchor_v1"
        if metar_four_layer.SERVE_ENABLED and REPLAY_LAYERS_APPLIED
        else "off:judge_reads_envelope" if metar_four_layer.SERVE_ENABLED
        else "off"
    ),
    "metar_ceiling": (
        # The window the ceiling may speak in rides in the label: a book
        # priced with the ceiling from 10:00 local (14h left, 2026-09-06)
        # is not the book priced with it from 14:00 (10h), and the two must
        # not share a fingerprint.
        # v2 (2026-09-06): the rate also reads how far the newest formal
        # report sits below the running high; the axis version rides here.
        "replay_table_oos_by_event_date:max_hours_left=%g:%s"
        % (
            metar_ceiling.CEILING_MAX_HOURS_LEFT,
            metar_ceiling.CEILING_GAP_DIMENSION_VERSION,
        )
        if PAPER_LEDGER_CEILING and REPLAY_LAYERS_APPLIED
        else "off:judge_reads_envelope" if PAPER_LEDGER_CEILING
        else "off"
    ),
    # The lane name carries the width floor unconditionally.  A book priced
    # on floored posteriors is not the book priced on unfloored ones, and
    # two books that disagree about that must not share a fingerprint.
    "center_width": (
        "replay_table_oos_by_event_date_floored_v2:"
        + ",".join(sorted(PAPER_LEDGER_CENTER_WIDTH_STAGES))
        if PAPER_LEDGER_CENTER_WIDTH and REPLAY_LAYERS_APPLIED
        else "off:judge_reads_envelope" if PAPER_LEDGER_CENTER_WIDTH
        else "off"
    ),
    # Same axis, different clock.  A book built on posteriors that reacted to
    # yesterday is not the book built on ones that did not, so the two must
    # not share a fingerprint and compound together in one equity curve.
    # Which stages were allowed to add a leg.  A chain built from two stages is
    # not the same chain as one built from three, so they must not share a
    # fingerprint and silently mix in one book.
    "ledger_stages": ",".join(LEDGER_STAGES),
    "maker_edge_escalation": "value_headroom_over_band",
    "maker_completion_contract": (
        "coverage_premium_over_clamp_v3"
        if MAKER_RESTING_COVERAGE_BUDGET >= 0.0
        else "legacy_completeness"
    ),
    "maker_coverage_risk_budget": MAKER_RESTING_COVERAGE_BUDGET,
    "maker_coverage_value_lambda": MAKER_RESTING_COVERAGE_LAMBDA,
    "maker_cutoff_percentile": MAKER_RESTING_CUTOFF_PERCENTILE,
    "maker_cutoff_buffer_minutes": MAKER_RESTING_CUTOFF_BUFFER_MINUTES,
    "maker_cutoff_min_days": MAKER_RESTING_CUTOFF_MIN_DAYS,
    "maker_crash_ticks": MAKER_RESTING_CRASH_TICKS,
    # Whether a partly-filled resting basket is a position or a refusal.
    # It changes what every resting row *is*, not merely its price.
    "resting_entry": "progressive_per_leg",
    "minimum_leg_all_in_ask": MINIMUM_LEG_ALL_IN_ASK,
    "leg_floor_price_basis": LEG_FLOOR_PRICE_BASIS,
    # Charter 131 (2026-09-12, Q8): the flat floor read at the mid; the rule
    # string keeps charter 91's belief-scaled book apart from this one.
    "leg_floor_rule": LEG_FLOOR_RULE_VERSION,
    "initial_capital_usdc": INITIAL_CAPITAL_USDC,
    "kelly_fraction": KELLY_FRACTION,
    "max_stake_fraction_per_scope": MAX_STAKE_FRACTION_PER_SCOPE,
    # Charter 141: the ceiling the lift, a completion and the budget gate
    # read, and the rule string that names the two-number station-day.
    "station_day_hard_fraction": STATION_DAY_HARD_FRACTION,
    "capital_layer_rule": CAPITAL_LAYER_RULE_VERSION,
    "certainty_gate": list(CERTAINTY_GATE),
    # Which decisions the gate judges.  It stopped being the opener's alone
    # on 2026-08-24, when the ledger was aligned to the live rule, and that
    # changes what a chain *is* -- a refused top-up leaves a held position
    # unchanged where the old scope would have added to it.  Rows either
    # side of the change describe different strategies and must not share a
    # fingerprint.
    "certainty_gate_scope": "every_stage",
    # Whether an unpriced ideal window is a refusal.  It selects a different
    # population, so rows either side of it are different books.
    "require_quoted_book": REQUIRE_QUOTED_BOOK,
    "maximum_support_buckets": MAXIMUM_SUPPORT_BUCKETS,
    "minimum_support_buckets": MINIMUM_SUPPORT_BUCKETS,
    # Arm G: how many rungs from the book's own centre a leg may sit.  0 is
    # off and is the default, so rows recorded before this key existed keep
    # their fingerprint only while the arm is off -- which is the point of
    # putting it here rather than leaving it silent.
    "market_distance_max": MARKET_DISTANCE_MAX,
    "stage_floor": "per_stage",
    # One book, one position per station-day, settled once.  This changes
    # what every recorded row *is* -- a stage row is now a top-up of a
    # standing position rather than a bet of its own -- so rows written
    # under the three-book reading describe a strategy that no longer runs.
    "book_model": BOOK_MODEL,
    # Charter 121: whether a seat whose lifted shape the support floor refuses
    # is re-asked with that row excluded, or the payload is dropped.  It
    # selects a different seat on the station-days where it fires -- seven in
    # the book as of 2026-09-08 -- so rows either side of it describe
    # different strategies and must not share a fingerprint.
    "post_lift_rerank": CHARTER_121_POST_LIFT_RERANK,
    # Charter 123 (2026-09-09): production re-reads the whole-basket edge on
    # the lifted targets.  This walk already applied that test at its own
    # execution layer, so the book does not move -- but the seat production
    # hands back can, because the refusal feeds charter 121's re-solve.
    # Hashed away from ``on`` only, so the standing digest is unchanged.
    **(
        {"post_lift_edge": CHARTER_123_POST_LIFT_EDGE}
        if CHARTER_123_POST_LIFT_EDGE != "on" else {}
    ),
    # Charter 124 (2026-09-09): the robust claim.  ``shadow`` is the default
    # and decides nothing, so it hashes away; ``on`` deflates the belief every
    # window is gated and sized on and is a different strategy, and ``off``
    # stops recording the reading.  Hashing the budget with the mode matters
    # as much as hashing the mode: two ``on`` arms at different ball radii are
    # two strategies, and a fingerprint that moved on the switch but not on
    # its number would repeat the defect of 2026-09-09 exactly.
    **(
        {
            "robust_claim": CHARTER_124_ROBUST_CLAIM,
            "robust_claim_divergence_nats": (
                ROBUST_CLAIM_DIVERGENCE_BUDGET_NATS
            ),
        }
        if CHARTER_124_ROBUST_CLAIM != "shadow" else {}
    ),
    # Charter 125 (2026-09-09): a bucket the day's observed high has already
    # passed leaves the domain at every stage, not only at P1.  Recorded
    # unconditionally, unlike 123/124 above: this one moves the book -- a
    # P0/P0-M replay on any station-day whose local midnight falls inside the
    # checkpoint's own window can no longer seat the rungs the thermometer
    # killed -- so every row written before it describes a strategy that no
    # longer runs and must be repushed rather than compared against.
    "observed_high_floor": CHARTER_125_OBSERVED_HIGH_FLOOR,
    # Charter 132 (2026-09-13): the valuation lane.  Same form as charter
    # 124: ``shadow`` is the default and decides nothing, so the standing
    # digest does not move and no book is re-derived for a reading; ``on``
    # gates and sizes on the pooled vector's action bound and holds any stage
    # without an admitted verdict -- a different strategy, hashed with every
    # number that defines it; ``off`` stops recording and hashes too.
    **(
        {
            "valuation_mode": VALUATION_MODE,
            "valuation_rule": VALUATION_RULE,
            "valuation_market_reference_rule": VALUATION_MARKET_REFERENCE_RULE,
            "valuation_lcb_quantile": VALUATION_LCB_QUANTILE,
        }
        if VALUATION_MODE != "shadow" else {}
    ),
}
SELECTION_RULE_SHA256 = content_sha256(_SELECTION_RULE_IDENTITY)
# Why a station-day has no seat, in the ledger's own words.  The page used to
# infer this from the absence of a row and could only ever say "the objective
# looked and did not sit" -- true of one of these and wrong about the other
# four, which are four different problems with four different owners.
REFUSAL_REASONS = (
    "completion_take_above_authorized_cost",
    "no_book_at_decision",
    "incomplete_book",
    "selection_failed",
    "production_judge_blocked",
    # Charter 119: the P1 did not carry the evidence its own mode claims --
    # the hard bound plus whichever of the three outermost claimants
    # answered for the step.  Production refuses it before its compile and
    # so does the walk, off the same function.
    "p1_execution_evidence",
    # No window survived long enough to be shown a price: the structural
    # floors -- contiguity, the charter-49 centre rung, the length floor --
    # took the whole enumeration.  Split out of ``no_priced_window`` on
    # 2026-08-20, which had been carrying it under a note that said the
    # market had outbid us.  Measured over the P0 book at that date, 316 of
    # 316 rows under that reason had at least one structural counter above
    # zero and none had all of them at zero: not one of them was a scope the
    # market ever got to price.
    "no_legal_window",
    # A window cleared every structural floor and still could not be priced:
    # some rung has no ask at all.  That is the literal reading of the name
    # and now its only one.
    "no_priced_window",
    # Priced end to end, and the model's edge over the all-in cost did not
    # clear tick-rounding across the legs.  This -- not ``no_priced_window``
    # -- is the refusal that means "the market is dearer than the model" at
    # window level, and it needs its own name because it is the only one of
    # the four whose fix is a better model rather than a wider book.
    "negative_edge_window",
    # Our own floor, not the market's doing.  Folding it into
    # ``no_priced_window`` said "the market is dearer than the model" about
    # scopes where the market never got a say -- every window held a leg
    # quoted under a cent and we declined to price it.  The two have
    # different owners and different fixes, so they are different reasons.
    "leg_below_floor",
    # A resting basket that never filled whole.  Distinct from "no depth":
    # the size was there, the price simply never came to us.
    "resting_unfilled",
    # We cancelled it ourselves.  The belief gate re-reads our own
    # probabilities at every re-pricing round and withdraws the whole order
    # once its claimed expectation goes negative -- the market never got a
    # say.  Folded into ``resting_unfilled`` it read as the market failing
    # to reach us, which points at execution when the cause was evidence.
    "belief_withdrew_order",
    "no_positive_kelly",
    "production_judge_hold",
    # We declined to act because the market had finished pricing the day --
    # the centre had held for three decisions, or the top two buckets already
    # carried 60% of the mass.  Folded into ``no_positive_kelly`` this read as
    # "the objective priced it and refused", which is the opposite of what
    # happened: the gate fires before any Kelly stake is computed, so the
    # model was never asked.  Measured on 2026-08-24, 251 of the 400 rows
    # carrying that code -- 63% of them -- were this, and the page was
    # calling every one of them "赔率不划算".  Its owner is our own clock:
    # the fix is to wait for a less-consolidated moment, not a better model
    # and not a wider book.
    "certainty_gate_lit",
    "shape_gap_too_wide",
    # The market never made a book on the window we want: less than 80% of
    # the mass in the ideal centre+-1 window sits on buckets asked at or
    # above the leg floor.  Distinct from ``no_priced_window``, which is
    # about a rung inside an already-legal window having no ask at all --
    # this one is about the shape of the book around the centre, is measured
    # before any window is enumerated, and ships off by default behind a
    # forward lane.
    "unquoted_ideal_window",
    # The basket asks more than the 1.00 it can ever pay.  Folded into
    # ``no_positive_kelly`` this read as "the objective looked and declined",
    # which credits a judgement to what is a loss by arithmetic: at a cost of
    # 1.04 no probability makes the bet win, so the model never got a say.
    # Measured on 2026-08-20 it was 82 of the 253 P0 refusals carrying that
    # code -- a third of them -- at a median cost of 1.040 and a maximum of
    # 2.997.  Same owner as ``no_priced_window``: the book, not us.
    "cost_exceeds_payout",
    "no_depth",
    # A later stage that wanted nothing this position does not already hold.
    # Under one book a stage is a top-up, so "the ladder it chose is inside
    # what P0 already bought" is not a refusal to trade -- it is the chain
    # standing pat, and it has to read as that rather than as no seat.
    "no_top_up_needed",
    "topup_not_complete_basket",
    # The exchange's own floor, under production's ``cash_phase`` name for
    # it: a leg sized under one lot that neither the untilted allowance
    # nor the depth can lift.  Production stands the whole basket down;
    # this book recorded the fraction as filled until 2026-09-04.
    "exchange_minimum_lot_unaffordable",
    # Where the operator's station dial binds in production: the kernel's
    # ``profit_floor_ok`` -- the worst settled outcome inside the seated
    # support may not lose more than the dial.  (Not a per-leg budget
    # gate: ``execution_gates.evaluate_execution_gate`` spells one, and
    # nothing on the live path calls it; the live ``budget_ok`` reads the
    # 2% capital the solver sized inside.  Corrected 2026-09-04.)
    "station_loss_floor_exceeded",
    # Charter 73: production's bundle re-score refused the increment on
    # its edge over the belief the seat was judged on.  A ticket is
    # exempt (charter 74).
    "execution_edge_not_authorized",
    # Charter 93: the lifted legs would lose inside their own window.
    "support_floor_after_lift",
    # A later stage whose station-day never opened.  Only P0 opens a
    # position; a top-up with nothing to top up is not a position of its own.
    # The declining-probability clamp's two refusals (arm-gated; the "off"
    # default never emits them).  See REFUSAL_DEPTH for why both tables
    # must list every reason.
    "decline_clamp_hold_basket",
    "decline_clamp_emptied_basket",
    "chain_never_opened",
    # Charter 66 clause B: the log-optimal increment from the chain's own
    # held curve is zero -- either the window is already covered, or every
    # buyable rung is priced past what it adds to the curve.  Distinct from
    # ``no_top_up_needed`` (the old want-minus-held reading) in that the
    # judge is the chain's growth, not the stage's shopping list.
    "no_chain_growth",
    # Charter 67: every cycle this stage was offered fell before its entry
    # window opened -- for P1, before the local hour the gate names.  Its
    # owner is our own clock, exactly like ``certainty_gate_lit``: the
    # market was there, the model was asked nothing.
    "entry_window_not_open",
    # Charter 66 clause A: the buy would put the whole chain's blended cost
    # at or past 1.00 -- a position that loses even when it wins.  Named
    # separately from ``cost_exceeds_payout`` because that one is a single
    # basket's price and this one is the book the chain has been building;
    # the stage's own basket usually passes every stage-level gate.
    "chain_cost_at_par",
    "topup_requires_probability_change",
    "topup_increment_edge_negative",
    # Charter 77: same model, same admissibility as the last recorded
    # decision of this stage -- the quote moved, nothing to decide.
    "no_decision_moment",
)
# Which refusal each gate of the enumeration funnel is.  Keyed by the gate
# names ``_support_windows`` publishes, so adding a gate there without a
# reason here fails loudly at import rather than quietly reattributing the
# refusal to whichever neighbour happens to be listed -- which is exactly how
# the leg floor (2026-08-17) and then the edge gate (2026-08-20) came to be
# explained by a centre-bucket count that had nothing to do with them.
GATE_REFUSAL_REASON = {
    "gapped": "no_legal_window",
    "center": "no_legal_window",
    "short": "no_legal_window",
    # Charter 131: a window that is not one of the day's ruled shapes is
    # refused as a shape, the same structural family as the centre rung and
    # the length floor.
    "shape": "no_legal_window",
    # Direction against the book's read (2026-08-28): structural family,
    # same reason bucket as the other placement rules.
    "market_lean": "no_legal_window",
    "leg_floor": "leg_below_floor",
    "unpriced": "no_priced_window",
    "edge": "negative_edge_window",
}
# What to call each gate on the page.  Same keys, same reason.
GATE_LABEL = {
    "gapped": "断桶",
    # The windows this gate refuses are the ones that do NOT hold the
    # ladder centre (the mean bucket, charter 54); the label says so
    # (user, 2026-09-05) -- "中心桶 6" read as six windows that did.
    "center": "不含中心桶",
    "short": "桶数不足",
    "shape": "席位形状不允许",
    "market_lean": "逆市场方向",
    "leg_floor": "单腿低于下限",
    "unpriced": "无法定价（视同非正EV）",
    "edge": "整篮非正EV",
}
assert set(GATE_REFUSAL_REASON) == set(WINDOW_REFUSAL_FUNNEL)
assert set(GATE_LABEL) == set(WINDOW_REFUSAL_FUNNEL)
assert set(GATE_REFUSAL_REASON.values()) <= set(REFUSAL_REASONS)
# A refusal reason has to be registered in BOTH places or it fails in one of
# two silent ways: missing from REFUSAL_DEPTH it resolves to depth 0 and can
# never be kept by ``_deeper_refusal`` (the walk reports a shallower cause);
# missing from REFUSAL_REASONS the table's CHECK rejects the whole write
# burst at the end of the run.  Both were hit on 2026-09-01, in that order,
# by the same two reasons -- the second only after a full replay arm had
# walked every station-day.  Asserted at import so the second one cannot
# wait for the write.
assert set(REFUSAL_DEPTH) == set(REFUSAL_REASONS), (
    "refusal vocabularies disagree: "
    f"depth-only={sorted(set(REFUSAL_DEPTH) - set(REFUSAL_REASONS))} "
    f"reasons-only={sorted(set(REFUSAL_REASONS) - set(REFUSAL_DEPTH))}"
)


def _gate_count(audit: Mapping[str, Any], gate: str) -> int:
    """One gate's refusal count, under whichever key the audit files it."""

    key = (
        "gapped_window_refused_count"
        if gate == "gapped"
        else "%s_refused_window_count" % gate
    )
    return int(audit.get(key) or 0)


def _refusal_reason_for_gate(audit: Mapping[str, Any]) -> str:
    """The refusal named after the gate that took the last window standing.

    Not the gate with the biggest count.  The gates form a funnel, so the
    shallow ones refuse the many windows that were never candidates -- on a
    three-bucket day the centre rung alone refuses two of six before length
    is even asked about -- and ranking by count hands every verdict to
    whichever gate sits earliest.  The old test here was
    ``leg_floor > centre + short``, which the leg floor can essentially never
    win: in the three commonest P0 shapes, (2,3,1), (1,1,1) and (3,2,1), it
    is the leg floor that kills the one window still standing and 1 > 2+3 is
    false in all of them.
    """

    gate = str(audit.get("last_refusing_gate") or "")
    if gate in GATE_REFUSAL_REASON:
        return GATE_REFUSAL_REASON[gate]
    # No gate fired at all.  Either the day admitted nothing to enumerate, or
    # the only thing on the shortlist is an unpriced incumbent carried
    # through by the "keep what we hold" exemption.
    if int(audit.get("shortlisted_window_count") or 0) > 0:
        return "no_priced_window"
    return "no_legal_window"


def _window_refusal_detail(audit: Mapping[str, Any]) -> str:
    """The refusal written as an equation the reader can check.

    Every gate appears, including the ones that did not fire, and the two
    sides balance: a line whose parts do not sum to the enumeration is a
    stated cause that cannot produce the effect, which is the failure this
    whole shape exists to make visible.
    """

    parts = [_gate_count(audit, gate) for gate in WINDOW_REFUSAL_FUNNEL]
    shortlisted = int(audit.get("shortlisted_window_count") or 0)
    enumerated = int(audit.get("enumerated_window_count") or 0)
    body = " + ".join(
        "%s %d" % (GATE_LABEL[gate], count)
        for gate, count in zip(WINDOW_REFUSAL_FUNNEL, parts)
    )
    gate = str(audit.get("last_refusing_gate") or "")
    # Which basket the objective was reaching for.  The funnel counts say
    # how many windows died and at which gate; they never said *what* the
    # engine wanted, so a page reporting "边际不敌摩擦" showed a refusal
    # with no target beside it.  Only the price gate has a near-miss to
    # name -- a structural refusal is an illegal shape, not a basket the
    # market priced out of reach -- so this appears exactly when the edge
    # gate is what refused.
    target = ""
    best = audit.get("edge_best_refused_window")
    if isinstance(best, Mapping) and best.get("buckets"):
        target = (
            "目标篮 %s：胜率 %.3f 成本 %.3f 边际 %+.4f（摩擦 %.4f）" % (
                "+".join(str(bucket) for bucket in best["buckets"]),
                float(best.get("win_probability") or 0.0),
                float(best.get("all_in_cost") or 0.0),
                float(best.get("edge") or 0.0),
                float(best.get("friction") or 0.0),
            )
        )
    funnel = "枚举 %d = %s + 入围 %d · 最后出局：%s%s" % (
        enumerated, body, shortlisted,
        # No gate fired and still no seat: what is on the shortlist is an
        # inherited support carried past the floors with no ask to price it.
        GATE_LABEL.get(gate, "无闸拒绝"),
        "" if enumerated == sum(parts) + shortlisted else " · 计数不闭合",
    )
    # The basket goes first when there is one (user ruling, 2026-08-28).  A
    # reader asking why a station did not seat wants the shape it wanted and
    # what it cost; the funnel answers a different question -- how many
    # windows died where -- and it is the same eight terms on every station
    # of every day, so leading with it buries the one clause that is about
    # this station.  The funnel keeps its checkable equation, one clause
    # later, and the page shows it on hover.
    return f"{target} · {funnel}" if target else funnel


_POSITION_DDL = """
CREATE TABLE IF NOT EXISTS paper_trade_position (
  position_id TEXT PRIMARY KEY,
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  stage TEXT NOT NULL CHECK(stage IN ('P0','P0-M','P1')),
  decision_at_utc TEXT NOT NULL,
  quote_observed_at_utc TEXT NOT NULL,
  ladder_center_bucket TEXT NOT NULL,
  window_buckets_json TEXT NOT NULL,
  window_size INTEGER NOT NULL,
  win_probability REAL NOT NULL,
  all_in_cost REAL NOT NULL,
  edge REAL NOT NULL,
  curve_rank REAL,
  kelly_fraction REAL NOT NULL,
  stake_usdc REAL NOT NULL,
  shares REAL NOT NULL,
  probabilities_json TEXT NOT NULL,
  prices_json TEXT NOT NULL,
  center_c REAL,
  refused_window_count INTEGER NOT NULL,
  content_sha256 TEXT NOT NULL,
  selection_rule_label TEXT NOT NULL DEFAULT '',
  leg_shares_json TEXT NOT NULL DEFAULT '',
  resting_fill_json TEXT NOT NULL DEFAULT '',
  posted_buckets_json TEXT NOT NULL DEFAULT '',
  intended_legs_json TEXT NOT NULL DEFAULT '',
  marginal_coverage_json TEXT NOT NULL DEFAULT '',
  intended_stake_usdc REAL,
  -- The two numbers the entry gate actually judged, recorded before the
  -- book narrowed the seat.  ``win_probability``/``all_in_cost`` above are
  -- what SETTLED -- the served mass over the legs that filled, and the cash
  -- that position cost per unit of payout -- and under charter 65's taker
  -- FOK the two pairs were the same number, so nothing downstream had to
  -- say which it meant.  Charter 94 made the order a resting ladder and
  -- they came apart: a basket can post three legs and keep two.  Anything
  -- fitted for a decision that has not executed yet -- lambda(a) above all
  -- -- must read the pair below, or it is answering "what did the legs the
  -- market sold us realise" when it was asked "what does a claim of this
  -- size realise".
  intended_win_probability REAL,
  intended_all_in_cost REAL,
  intended_leg_stake_json TEXT NOT NULL DEFAULT '',
  leg_stake_json TEXT NOT NULL DEFAULT '',
  held_before_json TEXT NOT NULL DEFAULT '',
  chain_shares_json TEXT NOT NULL DEFAULT '',
  chain_stake_usdc REAL
)
"""
# ``UNIQUE(station_id, event_date, stage)`` used to close the table above.
# It encoded "one seat per stage", which is the walk this ledger ran until
# 2026-09-02 and not the one production runs: production re-decides on
# every model update and a stage adds to itself on each later cycle.  Under
# that walk the constraint did not refuse the second seat -- the write is
# ``INSERT OR IGNORE`` -- it swallowed it, after ``held``, ``chain_stake``
# and ``chain_position_id`` had already moved on.  Measured on the first
# reseating run: 38 of 405 settlements pointed at a position row that was
# never written.  Uniqueness of a position is now its economic identity
# (the PRIMARY KEY), and a stage may hold several.
_REFUSAL_DDL = """
CREATE TABLE IF NOT EXISTS paper_trade_refusal (
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  stage TEXT NOT NULL CHECK(stage IN ('P0','P0-M','P1')),
  reason TEXT NOT NULL CHECK(reason IN (%s)),
  detail TEXT NOT NULL,
  cycles_examined INTEGER NOT NULL,
  last_decision_at_utc TEXT NOT NULL,
  recorded_at_utc TEXT NOT NULL,
  -- Which rule set answered.  A position row has carried this since the
  -- ledger learned to run arms; a refusal never did, and a refusal is an
  -- ANSWER -- ``_scope_answer_is_final`` reads it to skip a settled
  -- station-day rather than re-derive it.  So an arm run against a
  -- database some other arm has already walked inherited that arm's
  -- refusals and never solved those scopes at all: measured 2026-09-11,
  -- the second arm of a shared-database pair answered 783 scopes out of
  -- the first arm's table and finished a slice in 59 seconds that takes
  -- two and a half minutes.  The reading looked like a measurement and
  -- was the previous rule set wearing this one's name.
  --
  -- Empty means "written before this column existed", which is not this
  -- rule set and is therefore not a final answer -- the same treatment a
  -- NULL band axis gets in the candidate table.
  selection_rule_sha256 TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(station_id, event_date, stage)
)
""" % ",".join("'%s'" % reason for reason in REFUSAL_REASONS)
_CANDIDATE_WINDOW_DDL = """
CREATE TABLE IF NOT EXISTS paper_trade_candidate_window (
  candidate_id TEXT PRIMARY KEY,
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  stage TEXT NOT NULL CHECK(stage IN ('P0','P0-M','P1')),
  decision_at_utc TEXT NOT NULL,
  buckets_json TEXT NOT NULL,
  window_size INTEGER NOT NULL,
  -- The enumeration's own two numbers, straight off ``_support_windows``:
  -- production's function, not a copy of it.  ``all_in_price`` is the
  -- crossing ask total, which is the denominator charter 94(五) keeps the
  -- gate and Kelly on and therefore the one lambda(a) is looked up at.
  -- ``win_probability`` is the LIFTED claim -- ``raw_win * claim_factor``,
  -- charter 81's centre lift and the off-centre deflation already in it --
  -- because that is the number the gate divides the price by.  The seated
  -- path recorded the raw mass and the fit applied the lift itself; here
  -- the enumeration has already done it, so a fit reading this column must
  -- NOT lift again.  The raw mass rides along so the two can be compared
  -- without re-deriving either.
  win_probability REAL NOT NULL,
  raw_win_probability REAL,
  all_in_price REAL NOT NULL,
  -- Which band table produced this row's claim, and on which axis.
  --
  -- ``band_axis`` is the PARTITION key: lambda is fitted per axis, because
  -- moving the band axis moves every claim and therefore moves ``a = price
  -- / claim``, which is the axis lambda itself is keyed on.  Measured
  -- 2026-09-08 on 757 live candidate windows, switching P0 to the distance
  -- axis moved ``a`` on 100% of them and pushed 6.1% across a lambda band
  -- boundary into a different lambda value.
  --
  -- ``band_table_sha256`` is AUDIT ONLY and deliberately not the partition
  -- key.  Band tables are content-addressed and refitted daily, so the sha
  -- changes whenever a factor moves in its third decimal: P0's 662 scored
  -- windows carry 28 distinct shas, the largest holding 61 -- fewer than
  -- the 8 lambda bands need at 12 seats each -- and P1's 77 windows carry
  -- 21 shas of which exactly one clears the threshold.  Partitioning on it
  -- would not make the fit purer, it would end the fit.  The axis has two
  -- values and splits P0 into two usable heaps; the sha stays so drift
  -- inside an axis is visible to an audit rather than invisible.
  band_axis TEXT,
  band_table_sha256 TEXT,
  -- Charter 110 (extended 2026-09-08): the SECOND partition key.  The band
  -- table is only the first layer that moves a claim; charter 81's centre
  -- lift, the off-centre deflation and the convergence lift multiply the
  -- same number after it, and ``a = price / claim`` is keyed on the result.
  -- ``claim_transform_fingerprint()`` hashes all of them, so a fit never
  -- pools rows whose claims were computed by different tables.  NULL means
  -- "written before the column existed", which is not the current vintage
  -- and is excluded exactly as a NULL band_axis is.
  claim_transform_sha256 TEXT,
  -- Charter 131 (2026-09-12): the THIRD partition key.  The seat's shape --
  -- symmetric centre+-1, or the mean-anchored tail window on a skewed day --
  -- decides which rungs a window holds, and rungs realise by their claim
  -- size, so two windows at one agreement realise differently by shape.
  -- NULL means "enumerated under an older shape rule" and the fit excludes
  -- it exactly as it excludes a NULL band_axis.
  seat_shape_rule TEXT,
  -- Per-rung claim and all-in cost at this decision, as
  -- {bucket: [claim, cost]}.  The bucket-scope lambda fit explodes
  -- this into legs and dedupes on (scope, decision, bucket); the
  -- settled bucket is already on the row, so a per-leg fit needs
  -- no second table and no second scoring sweep.
  leg_claim_cost_json TEXT,
  -- Charter 128 v2 (2026-09-14): the market's belief for the window -- the
  -- normalised mid of the whole ladder summed over its buckets, straight off
  -- the enumeration's ``beta_market_mid`` -- and the hours from the
  -- decision to the settlement cutoff, the beta lane's two regressors.
  -- NULL means the book gave no belief at that decision (one-sided, empty,
  -- crossed, skewed) or the row predates the columns; the fit counts such
  -- rows out and never prices them at the ask instead.
  market_mid REAL,
  hours_to_cutoff REAL,
  -- The agreement the gate itself computed, when it computed one.  Not the
  -- fit's input -- price over claim above is -- but the cheapest possible
  -- check that the two agree.
  gate_agreement REAL,
  -- Whether the gate would have taken it, and whether the walk did.  Both
  -- recorded and NEITHER used to filter the fit: a window is evidence about
  -- what a claim of its size realises whether or not we bought it, and
  -- fitting only on the ones we bought is how the gate came to grade its
  -- own homework.
  admissible INTEGER NOT NULL,
  seated INTEGER NOT NULL,
  settled_bucket TEXT,
  hit INTEGER,
  settled_at_utc TEXT,
  truth_revision_no INTEGER,
  truth_content_sha256 TEXT NOT NULL DEFAULT '',
  selection_rule_sha256 TEXT NOT NULL DEFAULT '',
  -- One enumeration per stage per station-day: the cycle the walk stopped
  -- on, which is the seating cycle when it seated and the last one it
  -- looked at when it did not.  Recording every cycle would multiply the
  -- rows by ten and the information by nothing -- consecutive cycles on one
  -- station-day are the same book an hour apart.
  UNIQUE(station_id, event_date, stage, decision_at_utc, buckets_json)
)
"""
_SETTLEMENT_DDL = """
CREATE TABLE IF NOT EXISTS paper_trade_settlement (
  settlement_id TEXT PRIMARY KEY,
  position_id TEXT NOT NULL REFERENCES paper_trade_position(position_id),
  station_id TEXT NOT NULL,
  event_date TEXT NOT NULL,
  stage TEXT NOT NULL,
  settled_tmax_c REAL NOT NULL,
  settled_bucket TEXT NOT NULL,
  hit INTEGER NOT NULL CHECK(hit IN (0,1)),
  -- Did the truth land in the basket that was CHOSEN, as opposed to the one
  -- that filled.  ``hit`` answers the money question and must keep reading
  -- the position, because that is what pays; this answers the claim
  -- question, and a claim is made before any of it fills.  Under taker FOK
  -- the two are identical by construction.  Under charter 94's resting
  -- ladder they are not, and a fit that wants one and reads the other is
  -- measuring execution while calling it calibration.
  intended_hit INTEGER,
  stake_usdc REAL NOT NULL,
  payout_usdc REAL NOT NULL,
  pnl_usdc REAL NOT NULL,
  claimed_win_probability REAL NOT NULL,
  center_error_c REAL,
  bucket_distance INTEGER,
  settled_at_utc TEXT NOT NULL,
  -- Which truth this score was struck against.  Settled truth is revisable
  -- by design: the table is append-only under ``revision_no``, guarded by
  -- the ``calibration_truth_no_update``/``calibration_truth_no_delete``
  -- triggers, so a correction can only ever arrive as a *new* revision.  A
  -- settlement that does not say which one it read cannot know it has gone
  -- stale, and this one could not: on 2026-08-18 192 revisions landed at
  -- once -- 156 changing the value, 35 moving the winning bucket -- and the
  -- incremental sweep kept scoring the superseded ones because a recorded
  -- row is a recorded row.  The sha is the staleness key; the revision
  -- number rides along so a reader can see how far the row has moved
  -- without joining back to the truth table.
  truth_revision_no INTEGER,
  truth_content_sha256 TEXT NOT NULL DEFAULT '',
  -- What the whole position cost per unit of payout, across every stage that
  -- topped it up.  The dust floor is a statement about the basket that
  -- settled, and no stage row carries that number: each holds the cost of
  -- its own top-up.
  basket_all_in_cost REAL,
  -- Kept keyed by stage rather than migrated to the station-day: one chain
  -- writes one settlement now, and the stage it carries is whichever one
  -- last topped it up, so the wider key never binds.  Deletes key on the
  -- station-day alone so a chain whose last stage moved still replaces its
  -- own row.
  UNIQUE(station_id, event_date, stage)
)
"""


def _connect(db: Path, *, writable: bool) -> sqlite3.Connection:
    if writable:
        conn = sqlite3.connect(str(db), timeout=60.0)
    else:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=60.0)
    conn.execute("PRAGMA busy_timeout=60000")
    if writable:
        # Never checkpoint from here.  The WAL on this database sits past the
        # 1000-page autocheckpoint threshold essentially always, so with the
        # default every one of this pass's commits would try to fold a
        # fifty-megabyte WAL into a ten-gigabyte database -- while the
        # resident, whose own lock patience is five seconds, is trying to
        # write.  That is how a read-only scorer took the P1 stage down with
        # ``database is locked``.  Appending to the WAL and letting the owner
        # of the database checkpoint on its own schedule costs nothing here.
        conn.execute("PRAGMA wal_autocheckpoint=0")
    return conn


def _position_scope_unique_present(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table'"
        " AND name='paper_trade_position'"
    ).fetchone()
    return bool(row) and "UNIQUE(station_id, event_date, stage)" in str(row[0])


def _rebuild_position_without_scope_unique(conn: sqlite3.Connection) -> None:
    """Drop the one-seat-per-stage constraint by rebuilding the table.

    SQLite cannot drop a table constraint in place.  Same shape as the
    refusal rebuild below: rename, recreate from the current DDL, copy the
    shared columns, drop the old.  Rows are carried verbatim -- the
    constraint only ever refused writes, it never shaped the rows it let
    through.
    """

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "ALTER TABLE paper_trade_position RENAME TO paper_trade_position_old"
        )
        conn.execute(_POSITION_DDL)
        old_columns = [
            (str(row[1]), str(row[2]), row[3], row[4])
            for row in conn.execute(
                "PRAGMA table_info(paper_trade_position_old)"
            )
        ]
        new_cols = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(paper_trade_position)")
        }
        # Carry every column the old table had, including the ones
        # ``_POSITION_DDL`` does not spell.
        #
        # Copying only the intersection silently drops them, and three of
        # this table's columns are added by ALTER after the DDL runs
        # (``content_sha256``, ``consolidation_json``,
        # ``selection_rule_sha256``).  Measured 2026-09-02, the hard way:
        # this rebuild blanked the rule fingerprint on all 460 rows that
        # predated it, and every basket on the page then read "chosen under
        # a retired rule" because the empty string matches no rule set at
        # all.  The data is derivable, so nothing was lost for good -- but
        # nothing said it had happened either.
        for name, decl, not_null, default in old_columns:
            if name in new_cols:
                continue
            suffix = ""
            if int(not_null or 0):
                suffix = f" NOT NULL DEFAULT {default if default is not None else "''"}"
            elif default is not None:
                suffix = f" DEFAULT {default}"
            conn.execute(
                "ALTER TABLE paper_trade_position"
                f" ADD COLUMN {name} {decl or 'TEXT'}{suffix}"
            )
        # Ask the table what it has, rather than trusting the loop above to
        # have added what it meant to.  An ALTER that does not take is
        # exactly the case this guard is for, and bookkeeping that agrees
        # with itself would miss it.
        present = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(paper_trade_position)")
        }
        carried = [name for name, _, _, _ in old_columns]
        missing = [name for name in carried if name not in present]
        if missing:
            raise RuntimeError(
                "position rebuild would drop columns: " + ",".join(missing)
            )
        shared = ",".join(carried)
        conn.execute(
            f"INSERT INTO paper_trade_position({shared})"
            f" SELECT {shared} FROM paper_trade_position_old"
        )
        moved = conn.execute(
            "SELECT COUNT(*) FROM paper_trade_position"
        ).fetchone()[0]
        held = conn.execute(
            "SELECT COUNT(*) FROM paper_trade_position_old"
        ).fetchone()[0]
        if int(moved) != int(held):
            raise RuntimeError(
                f"position rebuild carried {moved} of {held} rows"
            )
        conn.execute("DROP TABLE paper_trade_position_old")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# The tables this script owns and is therefore allowed to discard.  Ordered
# so a DROP sequence never leaves a foreign key pointing at nothing.
PAPER_TRADE_TABLES = (
    "paper_trade_settlement",
    "paper_trade_position",
    "paper_trade_refusal",
    "paper_trade_candidate_window",
)


def reset_paper_tables(conn: sqlite3.Connection) -> int:
    """Drop this script's tables so the next pass starts from nothing.

    For arms.  Partitioning by fingerprint (the position and refusal rows)
    keeps one rule set from reading another's answers, but an arm that
    shares a replay database still inherits the other's candidate windows
    and pays to re-derive everything anyway -- and the two protections have
    to be gotten right separately every time.  Dropping is one line and
    cannot be half-right.

    Never reach for this on the production SSOT: the standing book lives in
    these tables and every ROI figure in the research record comes out of
    them.  ``--reset-paper-tables`` refuses the default database for that
    reason.
    """

    dropped = 0
    for table in PAPER_TRADE_TABLES:
        if conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
            " AND name=?",
            (table,),
        ).fetchone()[0]:
            conn.execute(f"DROP TABLE {table}")
            dropped += 1
    conn.commit()
    return dropped


def ensure_schema(conn: sqlite3.Connection) -> bool:
    """Create or widen the ledger's tables.

    Returns True when the refusal table had to be rebuilt for a widened
    reason CHECK -- the caller is expected to re-derive refusals over the
    *whole* history that pass, not just the recent window, so the table
    comes back uniformly coded rather than half-translated.
    """

    conn.execute(_POSITION_DDL)
    if _position_scope_unique_present(conn):
        _rebuild_position_without_scope_unique(conn)
    conn.execute(_SETTLEMENT_DDL)
    conn.execute(_CANDIDATE_WINDOW_DDL)
    # ``CREATE TABLE IF NOT EXISTS`` will not widen an existing table, and
    # every row already in one predates the band lineage columns.  They stay
    # NULL rather than being guessed: a row whose band vintage is unknown is
    # not a row from the current axis, and charter 104's rule against
    # silently borrowing a basis applies to a partition key as much as to a
    # claim.  ``_backfill_candidate_band_lineage`` fills what the envelopes
    # can still answer for and counts what they cannot.
    _candidate_cols = {
        str(row[1])
        for row in conn.execute(
            "PRAGMA table_info(paper_trade_candidate_window)"
        )
    }
    for _name in (
        "band_axis", "band_table_sha256", "claim_transform_sha256",
        "leg_claim_cost_json", "seat_shape_rule",
    ):
        if _name not in _candidate_cols:
            conn.execute(
                "ALTER TABLE paper_trade_candidate_window"
                f" ADD COLUMN {_name} TEXT"
            )
    # Charter 128 v2: the beta lane's regressors.  Rows already in the table
    # stay NULL until the book is re-imported under the new fingerprint --
    # a NULL is "no belief recorded", which the fit excludes, not zero.
    for _name in ("market_mid", "hours_to_cutoff"):
        if _name not in _candidate_cols:
            conn.execute(
                "ALTER TABLE paper_trade_candidate_window"
                f" ADD COLUMN {_name} REAL"
            )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_candidate_window_scope"
        " ON paper_trade_candidate_window(stage, event_date)"
    )
    # The fit reads (stage, axis, event_date); without this it reads the
    # whole table and filters in Python, which is what made the first
    # lambda refit take ten minutes.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_candidate_window_axis"
        " ON paper_trade_candidate_window(stage, band_axis, event_date)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_candidate_window_claim_vintage"
        " ON paper_trade_candidate_window("
        "  stage, band_axis, claim_transform_sha256, event_date)"
    )
    conn.execute(_REFUSAL_DDL)
    # Before the reason rebuild below, because that rebuild copies this
    # column by name.
    _refusal_cols = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(paper_trade_refusal)")
    }
    if "selection_rule_sha256" not in _refusal_cols:
        conn.execute(
            "ALTER TABLE paper_trade_refusal"
            " ADD COLUMN selection_rule_sha256 TEXT NOT NULL DEFAULT ''"
        )
    # A new reason cannot be recorded through an existing CHECK, and
    # ``CREATE TABLE IF NOT EXISTS`` will not widen one.  Rebuild in place
    # when the live constraint is missing a reason this code can emit.
    #
    # Rebuild by migration, not by DROP.  The old shape dropped the table
    # and trusted the sweep to repopulate it, but the scheduled sweep only
    # walks the recent window (four days) -- every reason added since
    # 2026-08-16 silently erased whatever closed history lay beyond it,
    # which is why this table has never accumulated more than a few days
    # and why the dashboards' whole-table funnel has always been a
    # few-day funnel wearing an all-time label.  Rows whose reason left
    # the vocabulary are the one thing still dropped: they cannot pass
    # the new CHECK and re-deriving them is the caller's job.
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table'"
        " AND name='paper_trade_refusal'"
    ).fetchone()
    live = str((row or [""])[0] or "")
    refusal_rebuilt = False
    if live and any(f"'{name}'" not in live for name in REFUSAL_REASONS):
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "ALTER TABLE paper_trade_refusal"
                " RENAME TO paper_trade_refusal_old"
            )
            conn.execute(_REFUSAL_DDL)
            conn.execute(
                "INSERT INTO paper_trade_refusal ("
                " station_id, event_date, stage, reason, detail,"
                " cycles_examined, last_decision_at_utc, recorded_at_utc,"
                " selection_rule_sha256)"
                " SELECT station_id, event_date, stage, reason, detail,"
                " cycles_examined, last_decision_at_utc, recorded_at_utc,"
                " selection_rule_sha256"
                " FROM paper_trade_refusal_old WHERE reason IN (%s)"
                % ",".join("'%s'" % reason for reason in REFUSAL_REASONS)
            )
            conn.execute("DROP TABLE paper_trade_refusal_old")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        refusal_rebuilt = True
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(paper_trade_position)")
    }
    if "leg_shares_json" not in columns:
        conn.execute(
            "ALTER TABLE paper_trade_position"
            " ADD COLUMN leg_shares_json TEXT NOT NULL DEFAULT ''"
        )
    if "posted_buckets_json" not in columns:
        conn.execute(
            "ALTER TABLE paper_trade_position"
            " ADD COLUMN posted_buckets_json TEXT NOT NULL DEFAULT ''"
        )
    if "resting_fill_json" not in columns:
        conn.execute(
            "ALTER TABLE paper_trade_position"
            " ADD COLUMN resting_fill_json TEXT NOT NULL DEFAULT ''"
        )
    if "marginal_coverage_json" not in columns:
        # Audit only.  Recorded so "did the coverage this seat added pay for
        # the cost it added" is answerable from a row rather than from a join
        # written by hand -- and, more usefully, so a widening and a top-up
        # that only adds shares stop looking alike: 29 of 33 settled top-ups
        # measured on 2026-09-09 were shares-only, which the criterion has
        # nothing to say about and which no column could previously separate.
        conn.execute(
            "ALTER TABLE paper_trade_position"
            " ADD COLUMN marginal_coverage_json TEXT NOT NULL DEFAULT ''"
        )
    if "intended_legs_json" not in columns:
        # The order as it was posted, per leg, before the book answered.
        # ``leg_shares_json`` holds what came back, so without this the
        # shortfall on a leg that filled nothing is unrecoverable -- its
        # kept share is zero and the weighted allocation that sized it
        # cannot be inverted from the survivors.
        conn.execute(
            "ALTER TABLE paper_trade_position"
            " ADD COLUMN intended_legs_json TEXT NOT NULL DEFAULT ''"
        )
    if "intended_stake_usdc" not in columns:
        conn.execute(
            "ALTER TABLE paper_trade_position"
            " ADD COLUMN intended_stake_usdc REAL"
        )
    if "intended_win_probability" not in columns:
        conn.execute(
            "ALTER TABLE paper_trade_position"
            " ADD COLUMN intended_win_probability REAL"
        )
    if "intended_all_in_cost" not in columns:
        conn.execute(
            "ALTER TABLE paper_trade_position"
            " ADD COLUMN intended_all_in_cost REAL"
        )
    if "intended_leg_stake_json" not in columns:
        # What each posted leg was priced to cost, and what each leg the
        # book answered actually cost.  Share counts alone cannot answer
        # "which rung is the money in": the rungs are bought at different
        # prices, so the widest leg is routinely not the dearest one, and
        # reconstructing the cash from the top-of-book price understates it
        # by exactly the amount the clip had to walk -- 2% on LFPB
        # 2026-08-30, and silently, which is the worst way for a money
        # number to be wrong.  Recorded leg by leg where it is spent; the
        # legs sum to ``stake_usdc`` by construction.
        conn.execute(
            "ALTER TABLE paper_trade_position"
            " ADD COLUMN intended_leg_stake_json TEXT NOT NULL DEFAULT ''"
        )
    if "leg_stake_json" not in columns:
        conn.execute(
            "ALTER TABLE paper_trade_position"
            " ADD COLUMN leg_stake_json TEXT NOT NULL DEFAULT ''"
        )
    if "held_before_json" not in columns:
        # What the chain already held when this stage looked.  A top-up buys
        # the difference, so without this a one-leg P1 row is unreadable:
        # it looks like a one-rung basket rather than the two rungs the
        # position already had plus the one it added.
        conn.execute(
            "ALTER TABLE paper_trade_position"
            " ADD COLUMN held_before_json TEXT NOT NULL DEFAULT ''"
        )
    if "chain_shares_json" not in columns:
        # The whole position after this stage acted, which is what settles.
        conn.execute(
            "ALTER TABLE paper_trade_position"
            " ADD COLUMN chain_shares_json TEXT NOT NULL DEFAULT ''"
        )
    if "chain_stake_usdc" not in columns:
        conn.execute(
            "ALTER TABLE paper_trade_position"
            " ADD COLUMN chain_stake_usdc REAL"
        )
    if "consolidation_json" not in columns:
        # Research annotation only (2026-08-21): how consolidated the
        # market already was at entry.  Never read by any decision.
        conn.execute(
            "ALTER TABLE paper_trade_position"
            " ADD COLUMN consolidation_json TEXT NOT NULL DEFAULT ''"
        )
    if "selection_rule_label" not in columns:
        conn.execute(
            "ALTER TABLE paper_trade_position"
            " ADD COLUMN selection_rule_label TEXT NOT NULL DEFAULT ''"
        )
    if "selection_rule_sha256" not in columns:
        # Rows written before the stamp existed were written under a rule set
        # nothing records, so they carry the empty string and the sweep below
        # re-derives them.
        #
        # Added before the label backfill below, which reads this column.  The
        # other order only works on a database that already has it: on a fresh
        # one ``_POSITION_DDL`` creates ``selection_rule_label`` but not this,
        # so the backfill ran first and ``ensure_schema`` could not initialise
        # an empty database at all.
        conn.execute(
            "ALTER TABLE paper_trade_position"
            " ADD COLUMN selection_rule_sha256 TEXT NOT NULL DEFAULT ''"
        )
    # The label is derived from the same constants the fingerprint hashes,
    # so a row already stamped with the current fingerprint was chosen under
    # the current label by definition -- backfill it rather than forcing a
    # re-derivation of every position to carry a descriptive string.  Rows
    # under any other fingerprint keep whatever they were written with, which
    # is exactly the distinction the page needs to draw.
    if "selection_rule_label" in columns:
        conn.execute(
            "UPDATE paper_trade_position SET selection_rule_label=?"
            " WHERE selection_rule_sha256=? AND"
            " (selection_rule_label IS NULL OR selection_rule_label='')",
            (SELECTION_RULE_LABEL, SELECTION_RULE_SHA256),
        )
        conn.commit()
    settlement_columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(paper_trade_settlement)")
    }
    # Settlements written before the binding existed carry the empty sha,
    # which no standing truth can equal, so ``_rescore_settlements`` picks
    # every one of them up once.  It re-binds rather than re-scores the ones
    # whose recorded value still matches, so the migration costs an UPDATE of
    # two columns per row and leaves the numbers on the page untouched.
    if "basket_all_in_cost" not in settlement_columns:
        conn.execute(
            "ALTER TABLE paper_trade_settlement"
            " ADD COLUMN basket_all_in_cost REAL"
        )
    if "truth_revision_no" not in settlement_columns:
        conn.execute(
            "ALTER TABLE paper_trade_settlement"
            " ADD COLUMN truth_revision_no INTEGER"
        )
    if "truth_content_sha256" not in settlement_columns:
        conn.execute(
            "ALTER TABLE paper_trade_settlement"
            " ADD COLUMN truth_content_sha256 TEXT NOT NULL DEFAULT ''"
        )
    if "intended_hit" not in settlement_columns:
        conn.execute(
            "ALTER TABLE paper_trade_settlement"
            " ADD COLUMN intended_hit INTEGER"
        )
    _backfill_intended_basis(conn)
    # Charter 110: the lambda fit partitions on band_axis and excludes NULL,
    # so a migration that adds the column without filling it hands the next
    # fit an empty sample.  Runs beside the sibling backfill, in the same
    # place and for the same reason.
    _backfill_candidate_band_lineage(conn)
    return refusal_rebuilt



def _backfill_candidate_band_lineage(
    conn: sqlite3.Connection, *, limit: int = 20000
) -> dict[str, int]:
    """Give existing candidate rows the band vintage their claim came from.

    Charter 110 partitions the lambda fit on ``band_axis``, and every row
    written before the column existed carries NULL -- which the fit excludes,
    so without this the first fit after the migration reads nothing at all.

    Two hops, both facts.  The envelope the row was enumerated under names
    its band table (``lineage.band_reliability_json``), and the store says
    what axis that table's cells are keyed on.  The receipt itself only
    started carrying ``axis`` on 2026-09-07, so every historical row has to
    come by the second hop; a sha the store no longer holds leaves the row
    NULL rather than guessing, and so does an envelope with no band receipt
    at all.  Measured on the live book the day it landed: 774 of 827 scored
    rows resolved (all to ``claim``), 53 carried no band receipt.
    """

    from bayes_basket_target.probability_bridge import (  # noqa: PLC0415
        history_envelope_payload,
    )

    # A ledger-only database -- a replay extract, a fixture, a fresh file --
    # has the candidate table and neither of the two this reads.  That is not
    # an error and must not fail the schema step it runs inside: without both
    # halves there is no vintage to resolve, so it resolves none and says so.
    def _has(table: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone() is not None

    if not (
        _has("calibration_band_reliability_table")
        and _has("basket_probability_bridge_history")
    ):
        return {
            "examined": 0, "resolved": 0, "no_band_receipt": 0,
            "sha_not_in_store": 0, "envelope_unreadable": 0,
            "skipped": "band_lineage_sources_absent",
        }
    axis_by_sha = {
        str(row[0]): str(row[1] or "claim")
        for row in conn.execute(
            "SELECT band_table_sha256, COALESCE(axis,'claim')"
            " FROM calibration_band_reliability_table"
        )
    }
    rows = conn.execute(
        "SELECT candidate_id, station_id, event_date, stage, decision_at_utc"
        " FROM paper_trade_candidate_window"
        " WHERE band_axis IS NULL LIMIT ?",
        (int(limit),),
    ).fetchall()
    counts = {
        "examined": len(rows), "resolved": 0,
        "no_band_receipt": 0, "sha_not_in_store": 0, "envelope_unreadable": 0,
    }
    cache: dict[tuple[str, str, str, str], str | None] = {}
    updates: list[tuple[str, str, str]] = []
    for candidate_id, station, event, stage, decided in rows:
        key = (str(station), str(event), str(stage), str(decided))
        if key not in cache:
            found = conn.execute(
                "SELECT checkpoint_id, envelope_content_sha256, envelope_json"
                " FROM basket_probability_bridge_history"
                " WHERE station_id=? AND event_date=? AND stage=?"
                "   AND decision_at_utc=? LIMIT 1",
                key,
            ).fetchone()
            sha = None
            if found is not None:
                try:
                    envelope = history_envelope_payload(
                        conn,
                        checkpoint_id=str(found[0]),
                        envelope_content_sha256=str(found[1]),
                        stored=str(found[2]) if found[2] else None,
                    )
                    receipt = (envelope.get("lineage") or {}).get(
                        "band_reliability_json"
                    )
                    if isinstance(receipt, str):
                        receipt = json.loads(receipt)
                    if isinstance(receipt, dict):
                        sha = receipt.get("band_table_sha256")
                except Exception:  # noqa: BLE001 -- unreadable stays unknown
                    sha = "\0unreadable"
            cache[key] = sha
        sha = cache[key]
        if sha == "\0unreadable":
            counts["envelope_unreadable"] += 1
            continue
        if not sha:
            counts["no_band_receipt"] += 1
            continue
        axis = axis_by_sha.get(str(sha))
        if axis is None:
            counts["sha_not_in_store"] += 1
            continue
        updates.append((axis, str(sha), str(candidate_id)))
    if updates:
        conn.executemany(
            "UPDATE paper_trade_candidate_window"
            " SET band_axis=?, band_table_sha256=? WHERE candidate_id=?",
            updates,
        )
        conn.commit()
        counts["resolved"] = len(updates)
    return counts


def _backfill_intended_basis(conn: sqlite3.Connection) -> dict[str, int]:
    """Give the rows written before the columns existed their chosen basis.

    Not a re-derivation: ``intended_legs_json`` already records which legs
    were posted, and ``probabilities_json``/``prices_json`` already record
    the served vector and the all-in ask ladder the seat was priced against.
    The chosen basket's claim and price are those two ladders summed over
    those legs, which is the same arithmetic ``_priced_window`` did when it
    built the candidate -- so the backfilled value equals the recorded one
    for every row written from here on, and a test pins that.

    A row whose posted legs were never recorded is left NULL rather than
    guessed at, and every consumer must exclude it and say how many it
    excluded: a lambda band quietly fitted on nine rows because forty had no
    basis is exactly the failure this whole column exists to end.
    """

    counts = {"positions": 0, "settlements": 0}
    rows = conn.execute(
        "SELECT position_id, intended_legs_json, probabilities_json,"
        " prices_json FROM paper_trade_position"
        " WHERE intended_win_probability IS NULL"
        "   AND intended_legs_json NOT IN ('', '{}')"
    ).fetchall()
    updates: list[tuple[float, float, str]] = []
    for position_id, legs_json, probabilities_json, prices_json in rows:
        try:
            legs = json.loads(legs_json or "{}") or {}
            probabilities = json.loads(probabilities_json or "{}") or {}
            prices = json.loads(prices_json or "{}") or {}
        except (TypeError, ValueError):
            continue
        if not isinstance(legs, dict) or not legs:
            continue
        claim = sum(float(probabilities.get(b, 0.0) or 0.0) for b in legs)
        price = sum(float(prices.get(b, 0.0) or 0.0) for b in legs)
        if price <= 0.0:
            # A posted leg the price ladder does not carry cannot be summed
            # into a basket price, and a partial sum is worse than none.
            continue
        updates.append((claim, price, str(position_id)))
    if updates:
        conn.executemany(
            "UPDATE paper_trade_position"
            " SET intended_win_probability=?, intended_all_in_cost=?"
            " WHERE position_id=?",
            updates,
        )
        counts["positions"] = len(updates)
    # The chain's chosen basket is what it already held plus what this stage
    # posted -- identical to ``held_before | intended`` because the fills
    # this stage kept are a subset of what it posted.
    settled = conn.execute(
        "SELECT s.settlement_id, s.settled_bucket, p.intended_legs_json,"
        " p.chain_shares_json FROM paper_trade_settlement s"
        " JOIN paper_trade_position p USING(position_id)"
        " WHERE s.intended_hit IS NULL"
        "   AND p.intended_legs_json NOT IN ('', '{}')"
    ).fetchall()
    hits: list[tuple[int, str]] = []
    for settlement_id, settled_bucket, legs_json, chain_json in settled:
        try:
            legs = json.loads(legs_json or "{}") or {}
            chain = json.loads(chain_json or "{}") or {}
        except (TypeError, ValueError):
            continue
        if not isinstance(legs, dict) or not legs:
            continue
        window = set(map(str, legs)) | set(map(str, chain or {}))
        hits.append((
            1 if str(settled_bucket) in window else 0, str(settlement_id),
        ))
    if hits:
        conn.executemany(
            "UPDATE paper_trade_settlement SET intended_hit=?"
            " WHERE settlement_id=?",
            hits,
        )
        counts["settlements"] = len(hits)
    if updates or hits:
        conn.commit()
    return counts


def _window_floor(
    conn: sqlite3.Connection, *, recent_days: int | None
) -> str:
    """The earliest event day an incremental pass re-derives.

    Empty string for a rebuild, which has no floor.  One definition, used by
    everything that has to agree on where the window starts -- the walk, the
    superseded-row sweep and the opening equity.
    """

    if recent_days is None:
        # A rebuild has no rolling window, but it still has the absolute one:
        # the floor is a structural rule, not a performance bound.
        return LEDGER_EVENT_DATE_FLOOR
    floor = conn.execute(
        "SELECT date((SELECT max(event_date)"
        " FROM basket_probability_bridge_history), '-' || ?1 || ' days')",
        (recent_days,),
    ).fetchone()
    return max(str((floor or [""])[0] or ""), LEDGER_EVENT_DATE_FLOOR)


def _opening_equity(conn: sqlite3.Connection, *, earliest: str) -> float:
    """The book an incremental window opens with, not the book it started life
    with.

    An incremental pass walks only the last few days, and the equity it
    compounds used to start at the initial capital on every run.  So a seat
    created by a sweep was sized against a thousand USDC whatever the book had
    actually done -- 2026-08-16 through 08-18 seated 24 positions over the 2%
    per-scope cap that way, sized off 1000 while the P1 book stood at 724.
    The rebuild that walks everything got this right and hid it: only the
    incremental path was wrong, and the incremental path is the one that runs
    every fifteen minutes.

    Same filter as the day-by-day advance below, for the same reason: a
    basket bought under the dust floor is a one-sided quote, and its win
    bankrolls nothing.  Rule fingerprints are deliberately *not* filtered
    here -- rows outside the window are never re-derived, so excluding them
    would open the window on a book the page does not show.

    One number, not one per stage: the three stages are one position and
    draw on one bankroll (see ``BOOK_MODEL``).
    """

    if not earliest:
        return INITIAL_CAPITAL_USDC
    realised = conn.execute(
        "SELECT SUM(s.pnl_usdc)"
        " FROM paper_trade_settlement s"
        " JOIN paper_trade_position p USING(position_id)"
        " WHERE s.event_date < ? AND p.all_in_cost>=?",
        (earliest, DUST_COST_FLOOR),
    ).fetchone()
    return max(
        0.0, INITIAL_CAPITAL_USDC + float((realised or [0.0])[0] or 0.0)
    )


def _purge_below_floor(conn: sqlite3.Connection) -> int:
    """Remove chains seated before the ledger's absolute event-day floor.

    Separate from ``_drop_superseded_positions`` on purpose: that one drops rows
    whose rule fingerprint moved and re-derives them, while these are never
    re-derived at all.  Nothing else would ever delete them.
    """

    conn.execute(
        "DELETE FROM paper_trade_settlement WHERE position_id IN ("
        "  SELECT position_id FROM paper_trade_position WHERE event_date<?)",
        (LEDGER_EVENT_DATE_FLOOR,),
    )
    cursor = conn.execute(
        "DELETE FROM paper_trade_position WHERE event_date<?",
        (LEDGER_EVENT_DATE_FLOOR,),
    )
    dropped = int(cursor.rowcount or 0)
    conn.execute(
        "DELETE FROM paper_trade_refusal WHERE event_date<?",
        (LEDGER_EVENT_DATE_FLOOR,),
    )
    if dropped:
        conn.commit()
    return dropped


def _positions_under_other_rule_sets(conn: sqlite3.Connection) -> int:
    """How many recorded positions belong to a rule set that is not served.

    The question ``AUTO_REDERIVE_ON_RULE_CHANGE`` answers, asked of the
    positions rather than the settlements: an unsettled seat under a stale
    digest is just as much another strategy's, and it will be scored under
    this one the day its truth lands.
    """

    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM paper_trade_position"
            " WHERE selection_rule_sha256<>?",
            (SELECTION_RULE_SHA256,),
        ).fetchone()
    except sqlite3.DatabaseError:
        return 0
    return int((row or [0])[0] or 0)



def _repair_slice_through(
    conn: sqlite3.Connection, *, recent_days: int | None
) -> str | None:
    """The last event day a bounded repair pass walks, or None if none is due.

    Only for an incremental pass under ``AUTO_REDERIVE_ON_RULE_CHANGE``: a
    rebuild or an explicitly bounded walk already says what it covers.  The
    slice starts at the oldest day under another rule set when there is one,
    otherwise the day after the newest answer (seat or refusal) this rule set
    has recorded -- the book's own cursor -- when that answer is older than
    the incremental window; a book that reaches the window needs nothing.
    """

    if AUTO_REDERIVE_ON_RULE_CHANGE != "on" or recent_days is None:
        return None
    try:
        foreign = conn.execute(
            "SELECT MIN(event_date) FROM paper_trade_position"
            " WHERE selection_rule_sha256<>?",
            (SELECTION_RULE_SHA256,),
        ).fetchone()[0]
        newest = max(
            (
                str(row[0]) for row in (
                    conn.execute(
                        f"SELECT MAX(event_date) FROM {table}"
                        " WHERE selection_rule_sha256=?",
                        (SELECTION_RULE_SHA256,),
                    ).fetchone()
                    for table in ("paper_trade_position", "paper_trade_refusal")
                )
                if row and row[0]
            ),
            default="",
        )
        history = conn.execute(
            "SELECT MAX(event_date) FROM basket_probability_bridge_history"
        ).fetchone()[0]
    except sqlite3.OperationalError:
        return None
    if not history:
        return None
    if foreign:
        start = str(foreign)
    else:
        window = _window_floor(conn, recent_days=recent_days)
        if newest and newest >= window:
            return None
        start = (
            (datetime.fromisoformat(newest) + timedelta(days=1)).date().isoformat()
            if newest else LEDGER_EVENT_DATE_FLOOR
        )
    start = max(start, LEDGER_EVENT_DATE_FLOOR)
    through = (
        datetime.fromisoformat(start) + timedelta(days=REDERIVE_SLICE_DAYS - 1)
    ).date().isoformat()
    return min(through, str(history))


def _drop_superseded_positions(
    conn: sqlite3.Connection,
    *,
    recent_days: int | None,
    through_date: str | None = None,
) -> tuple[int, int]:
    """Discard the answers recorded under a rule set that no longer runs.

    Both kinds of answer, because both are read as one: a seat short-circuits
    the walk on any day and a refusal short-circuits it on a settled one
    (``_scope_answer_is_final``), so leaving the refusals of a superseded rule
    set behind makes the next pass skip precisely the scopes that rule set
    said no to.  Seats alone were dropped until 2026-09-11 and the shared
    database that exposed it is written up in the refusal DDL.

    Only within the window this pass can re-derive: a row deleted outside it
    would leave the page emptier than the ledger, which is the opposite of what
    the stamp is for.  That window has an upper edge too when the walk has
    one (``through_date``): a slice re-derives only the days up to it.
    """

    earliest = _window_floor(conn, recent_days=recent_days)
    conn.execute(
        "DELETE FROM paper_trade_settlement WHERE position_id IN ("
        "  SELECT position_id FROM paper_trade_position"
        "  WHERE selection_rule_sha256<>?1 AND event_date>=?2"
        "  AND (?3 IS NULL OR event_date<=?3))",
        (SELECTION_RULE_SHA256, earliest, through_date),
    )
    cursor = conn.execute(
        "DELETE FROM paper_trade_position"
        " WHERE selection_rule_sha256<>?1 AND event_date>=?2"
        " AND (?3 IS NULL OR event_date<=?3)",
        (SELECTION_RULE_SHA256, earliest, through_date),
    )
    dropped = int(cursor.rowcount or 0)
    refusals = conn.execute(
        "DELETE FROM paper_trade_refusal"
        " WHERE selection_rule_sha256<>?1 AND event_date>=?2"
        " AND (?3 IS NULL OR event_date<=?3)",
        (SELECTION_RULE_SHA256, earliest, through_date),
    )
    dropped_refusals = int(refusals.rowcount or 0)
    if dropped or dropped_refusals:
        conn.commit()
    return dropped, dropped_refusals


class _Truth(NamedTuple):
    """The settled value that stands, and which revision said so."""

    tmax_c: float
    revision_no: int
    content_sha256: str


def _truth(conn: sqlite3.Connection) -> dict[tuple[str, str], _Truth]:
    """Latest revision per station-day (charter 44).

    The revision's identity travels with its value.  A settlement scored
    against a number alone cannot tell a corrected day from an unchanged one,
    which is the whole of why an incremental pass used to need a rebuild.
    """

    out: dict[tuple[str, str], _Truth] = {}
    for station, day, value, revision, sha in conn.execute(
        "SELECT station_id, local_date, official_tmax_c, revision_no,"
        " content_sha256"
        " FROM calibration_station_daily_high_truth t"
        " WHERE revision_no = ("
        "   SELECT MAX(revision_no)"
        "   FROM calibration_station_daily_high_truth x"
        "   WHERE x.station_id=t.station_id AND x.local_date=t.local_date)"
    ):
        out[(str(station), str(day))] = _Truth(
            float(value), int(revision), str(sha)
        )
    return out


def _previous_checkpoint_probabilities(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    decision_at_utc: str,
) -> dict[str, float] | None:
    """The served vector of the checkpoint just before this one, any stage.

    Charter 69's clamp compares this cycle's distribution with the one
    before it, and production's "before" is the previous committed
    envelope on the station-day whatever stage served it
    (``_compile_target_plan``: ``_earlier[-1]``).  This lane walked one
    stage at a time and reset the carry at each, so the first cycle of
    P0-M and P1 compared against nothing and the clamp could not bind
    there (aligned 2026-09-04).  None when nothing precedes it, which the
    kernel reads as "unmeasured, leave alone".
    """

    row = conn.execute(
        "SELECT history_id FROM basket_probability_bridge_history"
        " WHERE station_id=? AND event_date=? AND state='COMMITTED'"
        "   AND decision_at_utc<?"
        " ORDER BY decision_at_utc DESC LIMIT 1",
        (station_id, event_date, decision_at_utc),
    ).fetchone()
    if row is None:
        return None
    return {
        str(bucket): float(probability)
        for bucket, probability in conn.execute(
            "SELECT bucket, probability"
            " FROM basket_probability_bridge_bucket_probability"
            " WHERE history_id=?",
            (row[0],),
        )
    } or None


# Charter 121: the marker that says a payload is being seen for the first
# time, as opposed to a re-entry carrying a restored ``previous_probabilities``
# -- which may legitimately be ``None`` on the first cycle of a stage, so the
# sentinel cannot be ``None`` itself.
_PAYLOAD_FIRST_ENTRY = object()


def _deeper_refusal(current: tuple, candidate: tuple) -> tuple:
    """Keep whichever refusal got further down the funnel.

    The walk used to overwrite unconditionally, so a station-day that was
    priced and declined on the objective at noon was reported as "no window
    could be priced" because its 23:59 book had resolved to one bucket at
    0.999 and the rest at 0.001.  Depth, not recency, is what carries the
    information: reaching ``no_positive_kelly`` means a legal basket existed
    and was judged, which a later ``no_priced_window`` cannot unsay.

    Ties keep the incumbent, so the earliest -- widest -- book at that depth
    is the one reported.
    """

    if REFUSAL_DEPTH.get(candidate[0], 0) > REFUSAL_DEPTH.get(current[0], 0):
        return candidate
    return current


def _instant(value: str) -> datetime:
    """A stored UTC stamp as an instant, whatever its fractional precision.

    Decision clocks carry microseconds and commit clocks milliseconds, and
    their byte order disagrees inside one second ('...:05.123Z' sorts after
    '...:05.123456Z').  Knowable times mix the two, so they are compared as
    instants.
    """

    moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _stage_authority_horizon(
    conn: sqlite3.Connection, station_id: str, event_date: str, stage: str
) -> str | None:
    """When this stage stopped being the day's authority, or None.

    Production compiles exactly one stage per cycle and picks it with
    ``stages.select_current_stage``, which walks ``reversed(STAGES)`` and
    returns the newest servable checkpoint of the *highest* stage that has
    one at or before the clock.  A higher stage that is present but stale
    fails the cycle closed; it never falls back.  So a stage is the day's
    authority only until the first checkpoint of a higher stage exists,
    and from that instant production cannot compile it again.

    This lane walks one stage at a time, oldest first, and so kept
    offering P0 books long after the day's TAF had taken over: measured
    2026-09-04 on the standing book, 141 of 393 P0 seats (36%, 489.87
    USDC) were taken at a clock where a P0-M or P1 checkpoint already
    existed, against 6 of 306 P0 compiles in production over the same
    days.  Those seats are not a version of production's book -- charter
    74 says only P0 opens a chain, so a day whose P0 never seated inside
    its own authority is a day production never opened at all.  They also
    settled worse: ROI -25.1% over 110 chains against -17.1% over the 179
    seated inside authority.

    Strict ``<``: production picks the higher stage on a tie, because
    ``select_current_stage`` tries the higher stage first and admits any
    checkpoint at or before the clock.
    """

    higher = LEDGER_STAGES[LEDGER_STAGES.index(str(stage)) + 1:]
    if not higher:
        return None
    # When the higher stage could first be *read*, not the instant it names.
    # A P1 new-high step is dated at its report's commit and can be written
    # minutes later; production kept compiling the lower stage until the row
    # existed (``probability_knowable_at``).  Rows written before 2026-09-13
    # stored their decision clock as their commit time, so for them this is
    # the old MIN(decision_at_utc) exactly.
    knowable = [
        probability_knowable_at(str(decided), created)
        for decided, created in conn.execute(
            "SELECT decision_at_utc, created_at_utc"
            " FROM basket_probability_bridge_history"
            " WHERE station_id=? AND event_date=? AND state='COMMITTED'"
            "   AND stage IN (%s)" % ",".join("?" for _ in higher),
            (station_id, event_date, *higher),
        )
    ]
    return min(knowable, key=_instant) if knowable else None


def _books_for_checkpoint(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    stage: str,
    decision_at_utc: str,
    envelope: Any,
    created_at_utc: str | None = None,
) -> list[tuple[dict[str, dict[str, Any]], str]]:
    """Every book production compiled this checkpoint on, oldest first.

    A checkpoint lands at its decision clock and production compiles it at
    its *next* cycle, on the quote that cycle holds -- fresh, and later
    than the checkpoint by the cycle's cadence (measured 2026-09-04: p50
    six to seven minutes, p90 25-38).  It keeps compiling it, cycle after
    cycle, until a newer checkpoint of the same or a higher stage lands
    and takes the book from it, or until the envelope stops being
    servable (``ProbabilityEnvelope.is_servable``: inside ``fresh_until``,
    or for P1 within the stale allowance past it).

    This lane paired a checkpoint with the *last* book before it instead
    -- a quote production had paired with the previous checkpoint's
    cycles, up to thirty minutes older than the belief it was priced
    against -- every P1 seat on the 2026-09-04 book and 127 of 229 P0
    seats.  Now: every committed book at or after the checkpoint, before
    the next same-or-higher checkpoint, while the envelope is still
    servable at that book's clock -- production judges each of those
    cycles, and charter 77 in the judge decides which is a decision
    moment.  Nothing before the checkpoint is ever used.  Pairing only the
    first such book (the 2026-09-04 v8 book) under-sampled P1 by a third
    against production, and the judgments it made were not the ones
    production's later cycles acted on.
    """

    higher_or_same = LEDGER_STAGES[LEDGER_STAGES.index(str(stage)):]
    # Both ends on the knowable clock (``probability_knowable_at``): a
    # checkpoint is compiled from the first book after it could be read, and
    # gives the book up once a newer same-or-higher checkpoint can be.  From
    # the decision clock alone, a P1 step written at 09:05 for a report that
    # committed at 08:52 was paired with thirteen minutes of books production
    # never compiled it on (2026-09-13 review).
    knowable_at = probability_knowable_at(decision_at_utc, created_at_utc)
    later = [
        probability_knowable_at(str(decided), created)
        for decided, created in conn.execute(
            "SELECT decision_at_utc, created_at_utc"
            " FROM basket_probability_bridge_history"
            " WHERE station_id=? AND event_date=? AND state='COMMITTED'"
            "   AND stage IN (%s) AND decision_at_utc>?"
            % ",".join("?" for _ in higher_or_same),
            (station_id, event_date, *higher_or_same, decision_at_utc),
        )
    ]
    next_at = min(later, key=_instant) if later else None
    books: list[tuple[dict[str, dict[str, Any]], str]] = []
    for observed_at, buckets_json in conn.execute(
        "SELECT observed_at, buckets_json FROM basket_official_ws_snapshot"
        " WHERE station_id=? AND event_date=? AND state='COMMITTED'"
        "   AND observed_at >= ?"
        "   AND (?4 IS NULL OR observed_at < ?4)"
        " ORDER BY observed_at ASC",
        (station_id, event_date, knowable_at, next_at),
    ):
        observed_at = str(observed_at)
        try:
            if not envelope.is_servable(observed_at):
                # Servability only lapses; once it has, no later book is.
                break
        except (AttributeError, ValueError):
            break
        quotes = _decode_quotes(buckets_json)
        if quotes:
            books.append((quotes, observed_at))
    return books


def _stage_entry_candidates(
    conn: sqlite3.Connection,
    station_id: str,
    event_date: str,
    stage: str,
):
    """Every book this stage was offered while it held authority, oldest first.

    A stage is entered the first moment it *can* be, and the earliest decision
    with a book is only the first thing that could count as that moment -- not
    the only one.  Production re-decides on every model update (charter 46) and
    opens whenever its objective seats a window, so a listing-window book too
    wide to trade is a cycle it declines, not a day it forfeits.  Stopping at
    the first priced decision made the ledger forfeit: measured on 2026-08-17 it
    seated 12 of 43 stations, while walking the same sequence seats 23, and
    every one of the eleven it had been dropping was a later cycle of a station
    whose listing book alone was unaffordable -- ZGGG's whole book asked 2.09
    for a partition worth 1.00 at 21:15, and 0.50 for the seated four rungs
    nineteen minutes later.

    There is no hindsight in that: each cycle is priced against the book as it
    stood at that cycle, and the walk stops at the first one that seats rather
    than choosing among them.  Taking the *last* decision would be the cheat --
    WMKK's P0 ran 205 times on 2026-08-15 and the last landed 23:36 local,
    against a book that had already resolved to 34C.

    Consecutive decisions that resolve to the same book and the same
    distribution cannot answer differently, so they are yielded once.
    """

    from bayes_basket_target.probability_bridge import (  # noqa: PLC0415
        history_envelope_payload,
    )

    seen: set[tuple] = set()
    horizon = _stage_authority_horizon(conn, station_id, event_date, stage)
    for history_id, decided, stored, checkpoint, sha, created in conn.execute(
        "SELECT history_id, decision_at_utc, envelope_json,"
        " checkpoint_id, envelope_content_sha256, created_at_utc"
        " FROM basket_probability_bridge_history"
        # ``state`` is pinned to COMMITTED by the table's own CHECK, so this
        # predicate selects nothing extra -- it is here for the planner.
        # Without it SQLite picks the (stage) index, which matches every row
        # of that stage in the whole table, and then reads ``envelope_json``
        # off each candidate: measured 2026-08-26, 16.4s for one
        # station-day-stage against 288078 rows, which is 7.3 hours for a
        # full pass and is why the sweep stopped finishing.  With it the
        # covering (station_id,event_date,state,...) index answers directly.
        " WHERE station_id=? AND event_date=? AND state='COMMITTED'"
        "   AND stage=?"
        # Only while this stage is the day's authority; see
        # ``_stage_authority_horizon``.
        "   AND (?4 IS NULL OR decision_at_utc < ?4)"
        " ORDER BY decision_at_utc ASC",
        (station_id, event_date, stage, horizon),
    ).fetchall():
        # The SQL bound is on the decision clock, a superset; authority ends
        # when the higher stage could be read, so the checkpoint has to have
        # been readable before that too.
        if horizon is not None and not (
            _instant(probability_knowable_at(str(decided), created))
            < _instant(horizon)
        ):
            continue
        envelope_json = history_envelope_payload(
            conn,
            checkpoint_id=str(checkpoint),
            envelope_content_sha256=str(sha),
            stored=str(stored) if stored else None,
        )
        # The seat is chosen off this ladder, so it has to be the ladder
        # production would have shown -- ceiling included on the P1 stage.
        ceiling_p = None
        # The books production compiled this checkpoint on: every one at
        # or after it, before a newer same-or-higher checkpoint takes over,
        # while the envelope is still servable.  See _books_for_checkpoint.
        # (This subsumes the charter-48 pre-listing rule and the 2026-09-04
        # "a newer checkpoint owns the book" rule alike.)
        books = _books_for_checkpoint(
            conn,
            station_id=station_id,
            event_date=event_date,
            stage=stage,
            decision_at_utc=str(decided),
            envelope=ProbabilityEnvelope.from_mapping(dict(envelope_json)),
            created_at_utc=str(created) if created else None,
        )
        if not books:
            continue
        buckets = conn.execute(
            "SELECT bucket, ordinal, lower_c, upper_c, probability"
            " FROM basket_probability_bridge_bucket_probability"
            " WHERE history_id=? ORDER BY ordinal",
            (history_id,),
        ).fetchall()
        if not buckets:
            continue
        # Charter 71(a): the identity of the served belief, before any
        # ceiling or centre-width correction -- those move with the
        # clock, the served vector moves only with the model.
        served_identity = probability_vector_identity_v1({
            str(row[0]): float(row[4]) for row in buckets
        })
        if stage == "P1" and REPLAY_LAYERS_APPLIED:
            ceiling_p = _ceiling_corrected(
                conn,
                {str(row[0]): float(row[4]) for row in buckets},
                stage=stage,
                envelope_json=envelope_json,
                station_id=str(station_id),
                event_date=str(event_date),
                as_of_utc=str(decided),
            )
            buckets = [
                (row[0], row[1], row[2], row[3],
                 ceiling_p.get(str(row[0]), row[4]))
                for row in buckets
            ]
        if REPLAY_LAYERS_APPLIED:
            cw_p = _center_width_corrected(
                {str(row[0]): float(row[4]) for row in buckets},
                stage=stage,
                envelope_json=envelope_json,
                event_date=str(event_date),
            )
            buckets = [
                (row[0], row[1], row[2], row[3],
                 cw_p.get(str(row[0]), row[4]))
                for row in buckets
            ]
        for quotes, observed_at in books:
            signature = (
                observed_at,
                tuple(
                    (str(row[0]), round(float(row[4]), 6)) for row in buckets
                ),
            )
            if signature in seen:
                continue
            seen.add(signature)
            lineage = envelope_json.get("lineage") or {}
            # Charter 105/110: which band table shaped this claim, and on
            # which axis.  Read here because this is where the envelope is
            # already open; the axis becomes the lambda fit's partition key
            # and the sha rides along for audit.  A body with no band
            # receipt leaves both None -- charter 104's rule against
            # silently borrowing a basis -- rather than defaulting to the
            # axis that happens to be active now.
            _band = lineage.get("band_reliability_json")
            try:
                _band = json.loads(_band) if isinstance(_band, str) else _band
            except (TypeError, ValueError):
                _band = None
            _band = _band if isinstance(_band, dict) else {}
            yield {
                "decision_at_utc": str(decided),
                "band_axis": (
                    str(_band.get("axis")) if _band.get("axis") else None
                ),
                "band_table_sha256": (
                    str(_band["band_table_sha256"])
                    if _band.get("band_table_sha256") else None
                ),
                "quotes": quotes,
                "observed_at": observed_at,
                "buckets": buckets,
                "center_bucket": str(lineage.get("model_center_bucket") or ""),
                # Charter 75: the production judge rebuilds production's own
                # snapshot from this body, so the seed is production's.
                "envelope_payload": envelope_json,
                "center_c": lineage.get("model_center_c"),
                "probability_identity": served_identity,
                # The day's reported high the P1 envelope stood on, for the
                # production-judge arm: production's P1 snapshot contract
                # requires it, and its risk families are read off it.
                "running_high_c": lineage.get("metar_trajectory_running_high_c"),
                # The probability leg of the economic identity (user ruling,
                # 2026-09-02): envelope content hash + posterior hash + the
                # exclusion mask.  Queried all along -- the SELECT above reads
                # both shas -- and simply never carried out of here, which is
                # why a seat could not be told apart by what it believed.
                "envelope_content_sha256": str(sha or ""),
                "checkpoint_id": str(checkpoint or ""),
                "posterior_sha256": str(
                    envelope_json.get("posterior_sha256") or ""
                ),
                "exclusion_mask": json.dumps(
                    {
                        "excluded_systems": lineage.get(
                            "excluded_observed_system_ids_json"
                        ),
                        "reason": lineage.get("excluded_observed_system_reason"),
                        "hard_excluded_buckets": envelope_json.get(
                            "hard_excluded_buckets"
                        ),
                    },
                    sort_keys=True, ensure_ascii=False,
                ),
                # The policy artefacts the compile stood on (identity leg 4).
                "policy_artifacts": json.dumps(
                    {
                        "posterior_version": envelope_json.get(
                            "posterior_version"
                        ),
                        "cross_activation": lineage.get(
                            "native_cross_activation_sha256"
                        ),
                        "cross_artifact": lineage.get(
                            "native_cross_ensemble_artifact_sha256"
                        ),
                        "system_activations": lineage.get(
                            "system_activation_shas_json"
                        ),
                        "system_artifacts": lineage.get(
                            "system_artifact_shas_json"
                        ),
                    },
                    sort_keys=True, ensure_ascii=False,
                ),
            }


def _decode_quotes(payload: Any) -> dict[str, dict[str, Any]]:
    quotes: dict[str, dict[str, Any]] = {}
    try:
        rows = json.loads(payload)
    except (TypeError, ValueError, json.JSONDecodeError):
        return quotes
    for item in rows:
        if not isinstance(item, dict):
            continue
        bucket = str(item.get("bucket") or "")
        if bucket:
            quotes[bucket] = item
    return quotes


def _entry_support_core(
    *,
    probabilities: Mapping[str, float],
    ordinals: Mapping[str, int],
    center: str,
) -> frozenset[str]:
    """The evidence support an entry cycle offers the enumeration.

    The production resolver converges its support to the cap before anything
    downstream sees it; the raw core has no such trim, and an over-wide one
    entered here as an incumbent -- which is exempt from the cap -- and seated
    a five-bucket basket the live rule forbids.  Trim from the far end of the
    centre, which is the order the resolver drops buckets in.

    It is a function rather than a paragraph inside ``backfill`` because the
    audit that holds recorded seats to this rule has to trim identically to
    be reading the same rule at all.  Copied out by hand it did not: the copy
    fell back to the argmax whenever the centre's ordinal was *falsy*, and
    ordinal 0 is the lowest bucket on every ladder.  KSEA 2026-08-13 P1 is
    centred on ``73°F or below`` at ordinal 0, so the copy trimmed toward
    80-81°F instead, dropped the centre from the core, and enumerated a
    74-81°F window costing 0.95 against a 0.46 claim.  That window outranks
    the seat on probability mass, so the audit read a correctly recorded row
    as a seat the gate could never have taken and allowlisted it as stale.
    One definition, one caller-visible name, and the two cannot drift again.
    """

    core = probability_core(
        probabilities, ordinals=ordinals, center_bucket=center
    )
    if len(core) <= MAXIMUM_SUPPORT_BUCKETS:
        return frozenset(core)
    centre_ordinal = ordinals.get(center)
    if centre_ordinal is None:
        centre_ordinal = ordinals[
            max(probabilities, key=lambda b: probabilities[b])
        ]
    return frozenset(
        sorted(
            core,
            key=lambda b: (
                abs(ordinals[b] - centre_ordinal),
                -probabilities.get(b, 0.0),
            ),
        )[:MAXIMUM_SUPPORT_BUCKETS]
    )


def _paper_snapshot_mapping(
    *,
    station_id: str,
    event_date: str,
    observed_at: str,
    buckets: list[tuple],
    quotes: dict[str, dict[str, Any]],
    stage: str = "P0",
    held: Mapping[str, float] | None = None,
    held_cost: Mapping[str, float] | None = None,
    reported_high: tuple[str, int] | None = None,
    observed_running_high_c: float | None = None,
    chain_seat_buckets: Sequence[str] = (),
    chain_sent_shares: Mapping[str, float] | None = None,
) -> tuple[
    Any, dict[str, float], dict[str, float],
    dict[str, list[dict[str, float]]]
] | None:
    rows = []
    probabilities: dict[str, float] = {}
    prices: dict[str, float] = {}
    depth: dict[str, list[dict[str, float]]] = {}
    for bucket, ordinal, _lower, _upper, probability in buckets:
        quote = quotes.get(str(bucket))
        if quote is None:
            return None
        # The book itself is the only source for the offer.  Reading a
        # scalar ``best_ask`` beside it split the price in two: eight
        # stations never write that key, so every bucket of theirs was
        # scored unpriceable here while the kernel priced them from the
        # levels -- and the capacity guard below, which compares levels
        # against this map, then admitted the entire book at a price only
        # the top level had.  One source, and the guard binds again.
        levels = sorted(
            (
                {"price": float(l["price"]), "shares": float(l["shares"])}
                for l in (quote.get("ask_levels") or [])
                if l.get("price") is not None and l.get("shares") is not None
                and float(l["shares"]) > 0.0
            ),
            key=lambda l: l["price"],
        )
        ask = levels[0]["price"] if levels else None
        bid = quote.get("bid_price", quote.get("best_bid"))
        # A bucket with no offer is unpriceable, not a reason to drop the
        # station: the enumeration already refuses to rank a window that
        # contains one, so the remaining windows stay available.  Refusing
        # the whole scope here is what kept 2026-08-16 off the page after
        # production had opened it -- eleven buckets quoted, one of them
        # empty, and the ledger discarded all eleven.
        priceable = (
            ask is not None and bool(quote.get("ask_available", True))
        )
        tick_size = float(quote.get("price_tick_size") or 0.001)
        # A bucket with no offer used to be dressed as one: the kernel
        # rejected an empty book outright, so this carried a ceiling ask of
        # max(tick, 1-tick) on a tick-sized level -- unfillable, which was
        # the intent, but summed into basket costs it produced 2.997 and
        # 3.996 on the page for markets that never quoted.  The kernel now
        # admits the honest shape (unmaterialized_book when both sides are
        # empty, plain ask_available=False when a bid stands), so what the
        # market said is what the kernel sees.
        bid_levels = [
            {"price": float(l["price"]), "shares": float(l["shares"])}
            for l in (quote.get("bid_levels") or [])
            if l.get("price") is not None and l.get("shares") is not None
        ]
        empty_book = not levels and not bid_levels
        # A bare bid_price on an otherwise empty book has no size behind it;
        # zeroing it is what lets the empty book name itself.  A priced or
        # bid-carrying bucket keeps whatever the quote said.
        bid = 0.0 if empty_book else float(bid or 0.0)
        probabilities[str(bucket)] = float(probability)
        if priceable:
            # Only a real offer earns a price; downstream lookups use
            # ``.get`` and treat an absent key as unfillable, which it is.
            prices[str(bucket)] = float(ask)
        # Real depth, not an assumed slab.  A penny bucket whose whole book
        # is twelve shares must not be fillable for hundreds: that fiction
        # is precisely what turns a thin tail into an infinite-return
        # lottery on paper.
        depth[str(bucket)] = levels
        rows.append({
            "bucket": str(bucket),
            "ordinal": int(ordinal),
            "probability": float(probability),
            # What the chain already holds here.  Production's objective
            # treats held shares as sunk and solves for the target on top
            # of them; the ledger's own path never reads these two.
            "filled_shares": float((held or {}).get(str(bucket), 0.0)),
            "filled_cost_usdc": float(
                (held_cost or {}).get(str(bucket), 0.0)
            ),
            "bid_price": bid,
            "bid_levels": bid_levels,
            "ask_levels": levels,
            "unmaterialized_book": empty_book,
            "price_tick_size": tick_size,
            "maker_queue_ahead_shares": 2.0,
            "maker_queue_known": True,
        })
    mapping = {
        "schema": "target_basket_snapshot_v6",
        "snapshot_id": "paper-" + content_sha256({
            "s": station_id, "e": event_date, "o": observed_at
        })[:24],
        "sequence": 1,
        "observed_at": observed_at,
        "market_id": "paper",
        "station_id": station_id,
        "event_date": event_date,
        "quote_version": "paper",
        "quote_observed_at": observed_at,
        "quote_source": "OFFICIAL_CLOB_WS",
        "inventory_version": "paper",
        "order_state_version": "paper",
        "evidence": {
            "probability_stage": str(stage or "P0"),
            **(
                {
                    "reported_high_bucket": str(reported_high[0]),
                    "reported_high_ordinal": int(reported_high[1]),
                    "reported_high_observation_id": "paper-running-high",
                    "hard_bound_id": "paper-running-high",
                }
                if reported_high is not None else {}
            ),
            "posterior_version": "paper",
            "posterior_sha256": "a" * 64,
            # Charter 125: the floor every stage is judged against, derived
            # from this book's own bucket edges with production's predicate.
            # Omitted, not nulled, when the day has reported nothing yet, so
            # a pre-day replay hashes exactly as it did before the ruling.
            **(
                {
                    "observed_running_high_c": float(
                        observed_running_high_c
                    ),
                    "observed_dead_buckets": list(
                        dead_buckets_for_upper_bounds(
                            ordered_buckets=[
                                str(bucket)
                                for bucket, _o, _l, _u, _p in sorted(
                                    buckets, key=lambda row: int(row[1])
                                )
                            ],
                            upper_bounds={
                                str(bucket): (
                                    float("inf") if upper is None
                                    else float(upper)
                                )
                                for bucket, _o, _l, upper, _p in buckets
                            },
                            running_high_c=float(observed_running_high_c),
                        )
                    ),
                }
                if observed_running_high_c is not None
                and observed_high_floor_enabled()
                else {}
            ),
        },
        "buckets": rows,
        "checkpoint_changed": True,
        "quote_changed": True,
        "fill_changed": True,
        "time_gate_changed": True,
        "evidence_fresh": True,
        "contract_matches": True,
        "order_state_known": True,
        "risk_inputs_complete": True,
        "time_left_minutes": 1440.0,
        # Charter 136 (五): the legs this chain has posted -- production's
        # "sent to the venue" -- when it has posted any.
        **(
            {"chain_seat_buckets": [
                str(bucket) for bucket, _o, _l, _u, _p in sorted(
                    buckets, key=lambda row: int(row[1])
                )
                if str(bucket) in {str(b) for b in chain_seat_buckets}
            ]}
            if chain_seat_buckets else {}
        ),
        # Charter 143 v2: the size each posted leg was posted at.
        **(
            {"chain_sent_shares": {
                str(bucket): float((chain_sent_shares or {})[str(bucket)])
                for bucket, _o, _l, _u, _p in buckets
                if str(bucket) in (chain_sent_shares or {})
            }}
            if chain_sent_shares else {}
        ),
    }
    return mapping, probabilities, prices, depth


def _build_snapshot(**kwargs: Any) -> tuple[
    Any, dict[str, float], dict[str, float],
    dict[str, list[dict[str, float]]]
] | None:
    """The paper snapshot, as this lane's own judge reads it."""

    built = _paper_snapshot_mapping(**kwargs)
    if built is None:
        return None
    mapping, probabilities, prices, depth = built
    return (
        TargetBasketSnapshot.from_mapping(mapping),
        probabilities, prices, depth,
    )


def _production_envelope_for(
    conn: sqlite3.Connection,
    payload: Mapping[str, Any],
) -> ProbabilityEnvelope:
    """The committed envelope production compiled this checkpoint from.

    The decision walk already carries the body (``envelope_payload``,
    lineage rehydrated by ``history_envelope_payload``); a payload
    without it is resolved through that same function, keyed by the
    checkpoint, so ``_choose_support`` and
    ``snapshot_from_probability_envelope`` read exactly the evidence
    production read: model centre, dispersion, rung width, formal
    members, per-bucket reliability.
    """

    body = payload.get("envelope_payload")
    if not isinstance(body, Mapping):
        from bayes_basket_target.probability_bridge import (  # noqa: PLC0415
            history_envelope_payload,
        )

        body = history_envelope_payload(
            conn,
            checkpoint_id=str(payload.get("checkpoint_id") or ""),
            envelope_content_sha256=str(
                payload.get("envelope_content_sha256") or ""
            ),
            stored=None,
        )
    return ProbabilityEnvelope.from_mapping(dict(body))


_PRODUCTION_POLICY_SCOPE_CACHE: dict[int, dict[str, str]] = {}


def _production_policy_scope(conn: sqlite3.Connection) -> dict[str, str]:
    """The scope bins production's compile loop hands its resolvers.

    Read from the strategy activation's ``target_scope`` -- the same row the
    worker reads -- and cached per connection.  A store without the row
    (a replay database cut before it was carried) falls back to the values
    that row has held since the delta-v2 lane opened.
    """

    key = id(conn)
    cached = _PRODUCTION_POLICY_SCOPE_CACHE.get(key)
    if cached is not None:
        return cached
    scope = {
        "season_bin": "summer",
        "liquidity_bin": "liquid",
        "strategy_version": "target-v2",
    }
    try:
        row = conn.execute(
            "SELECT activation_json FROM basket_strategy_activation_state"
            " ORDER BY generation DESC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    if row is not None:
        try:
            target = (json.loads(row[0]) or {}).get("target_scope") or {}
        except (TypeError, ValueError):
            target = {}
        for name in scope:
            if target.get(name):
                scope[name] = str(target[name])
    _PRODUCTION_POLICY_SCOPE_CACHE[key] = scope
    return scope


def _production_hold_refusal(
    verdict: Mapping[str, Any],
    *,
    held: Mapping[str, float],
    chain_stake: float,
) -> tuple[str, str]:
    """What the solver's hold means for this chain, in the day's own words.

    ``delta_cost <= 0`` is one verdict -- target shares not above what is
    held -- with two readings.  On a chain that holds, it is the top-up the
    objective declined.  On an empty chain at the opening stage it was
    reported the same way, "不加仓" on a day nothing had been bought (user,
    2026-09-05), when the verdict can tell two things apart: no window
    cleared the whole-basket positive-EV gate, so charter 74's ticket was
    never offered -- the market is dearer than the model, named by the gate
    that took the last window, with the basket that came closest -- or a
    window did clear it and the ticket still did not stand, because its
    cash was over the (tilted) capital or a leg had no depth at the ask.
    """

    buckets = "+".join(str(b) for b in verdict.get("buckets") or ())
    capital = float(verdict.get("capital_usdc") or 0.0)
    holds_anything = any(float(v or 0.0) > 0.0 for v in held.values())
    if holds_anything:
        return (
            "production_judge_hold",
            "生产裁判：目标篮 %s · 曲线分 %.4f ·"
            " 目标份额不高于持仓（资本 %.2f · 本链已投 %.2f）"
            % (
                buckets,
                float(verdict.get("curve_score") or 0.0),
                capital,
                chain_stake,
            ),
        )
    signature = dict(verdict.get("decision_signature") or {})
    audit = dict(verdict.get("window_audit") or {})
    if str(signature.get("edge_state") or "") != EDGE_STATE_ADMISSIBLE:
        return (
            _refusal_reason_for_gate(audit) if audit else "negative_edge_window",
            "生产裁判：没有窗口过整篮正EV闸，宪章 74 的门票不发，P0 不开 · "
            + (_window_refusal_detail(audit) if audit else "漏斗未记录"),
        )
    ticket = float(verdict.get("ticket_cash_usdc") or 0.0)
    if ticket > capital + 1e-9:
        why = "门票 %.2f 超出资本 %.2f（固化减仓）" % (ticket, capital)
    else:
        why = (
            "门票 %.2f 在资本 %.2f 之内仍没成：某腿卖一档无量，或整篮成本≥1"
            % (ticket, capital)
        )
    return (
        "production_judge_hold",
        "生产裁判：可入座窗 %s 但开仓门票没成 · %s · 本链已投 %.2f"
        % (buckets, why, chain_stake),
    )


_OBSERVED_RUNNING_HIGH_CACHE: dict[tuple[str, str], list[tuple[str, float]]] = {}


def _observed_running_high_at(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    at_utc: str,
) -> float | None:
    """Charter 125: the day's observed high as of one book clock.

    Production reads this off the canonical observation lane.  This lane has
    no such connection, so it reads the same number where production already
    wrote it down: every committed P1 checkpoint carries the running high it
    was built on, and P1 mints within minutes of each report.  Taking the
    maximum over the checkpoints at or before the clock is as-of by
    construction and never sees a report the decision could not have.

    A day whose P1 lane never minted returns None -- no floor, exactly as
    before the ruling -- which is honest rather than convenient: this lane
    cannot invent an observation production did not record.
    """

    key = (str(station_id), str(event_date))
    series = _OBSERVED_RUNNING_HIGH_CACHE.get(key)
    if series is None:
        series = []
        for decision_at, envelope_json in conn.execute(
            "SELECT decision_at_utc, envelope_json"
            " FROM basket_probability_bridge_history"
            " WHERE station_id=? AND event_date=? AND state='COMMITTED'"
            "   AND stage='P1'"
            " ORDER BY decision_at_utc ASC",
            (station_id, event_date),
        ):
            try:
                lineage = (
                    json.loads(str(envelope_json)).get("lineage") or {}
                )
                value = lineage.get("metar_trajectory_running_high_c")
            except (TypeError, ValueError, AttributeError):
                continue
            if value in (None, ""):
                continue
            try:
                series.append((str(decision_at), float(value)))
            except (TypeError, ValueError):
                continue
        _OBSERVED_RUNNING_HIGH_CACHE[key] = series
    highs = [value for decision_at, value in series if decision_at <= at_utc]
    return max(highs) if highs else None


def _production_judge(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    stage: str,
    decision_at_utc: str,
    payload: Mapping[str, Any],
    quotes: dict[str, dict[str, Any]],
    probabilities: Mapping[str, float],
    center: str,
    core: Sequence[str],
    held: Mapping[str, float],
    held_cost: Mapping[str, float],
    equity: float,
    chain_stake: float,
    tilt: float,
    last_decision_signature: Mapping[str, Any] | None = None,
    last_acted_probability_identity: str | None = None,
    excluded_candidate_ids: Sequence[str] = (),
    chain_seat_buckets: Sequence[str] = (),
    chain_sent_shares: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """Production's whole-basket judge, run on this lane's book.

    Everything here is production's own: the scope bins are derived the way
    ``compile_production_cycle_from_ssot`` derives them, the station policy
    and the delta-v2 policy are resolved by the same resolvers against the
    same tables as of the same clock, the window shortlist is
    ``_support_windows`` and the seat and its size come out of
    ``_solve_over_support_windows`` -- the objective production seats and
    sizes with, charter 68 folded into the claim it optimises.  The ledger
    then books the target it returns exactly as it books its own seat: the
    same fills, the same chain law (charters 66, 69, 70), the same
    settlement.

    What this arm does not carry over, so a reader knows what the gap it
    measures is made of:

    * the regime concentration multiplier -- the fleet regime shift that
      would have fed it was removed on 2026-09-17, so it is 1.0 here too;
    * nothing of the seed, since charter 75: the snapshot is production's
      ``snapshot_from_probability_envelope`` over the envelope production
      compiled, the centre, direction and admitted set are production's
      ``_choose_support``, and the enumeration is the one function.

    The chain law is the solver's, not this lane's (2026-09-04).  Until
    then the solver was called without the served identity, the chain's
    last-acted identity, the growth frame and the last decision
    signature, so charters 66(A), 71 and 77 could not bind inside it; the
    ledger re-applied them on the seated window afterwards and refused the
    cycle when they failed.  Production applies them per window inside the
    solver and seats the first window that clears them -- a head window
    refused on chain growth hands the seat to the next one, which this
    lane never did.  Every one of those inputs is now handed through, the
    solver's own decision-moment audit is read back, and the ledger's
    post-seat copies are skipped for a production verdict.

    Charter 74's ticket is likewise the solver's candidate: offered only
    when the chain is empty at an opening stage, the Kelly law sized the
    seat to nothing, and the ticket fits the capital.  The ledger used to
    add its own ticket whenever the solver held -- including when the
    solver had refused the ticket as unaffordable under the tilt -- which
    opened chains production never opens.

    Capital is production's: the scope's share of the book less what the
    chain has spent, tilted.  The station's ``total_budget_usdc`` is not a
    sizing bound in production -- it is the execution gate's per-leg
    ``budget_ok`` -- so it is no longer folded into the capital here and
    is judged per leg at the same place instead.
    """

    fee_rate = FEE_RATE
    try:
        # The policy scope production compiles under: the strategy
        # activation's ``target_scope`` names the season, liquidity and
        # strategy version (the policy layer still speaks the fleet-wide
        # ``summer`` vocabulary; charter 62's station-local season is the
        # calibration layer's, not this one's), and the maturity is the
        # calendar distance from this decision's own clock.
        scope = _production_policy_scope(conn)
        season_bin = str(scope["season_bin"])
        liquidity_bin = str(scope["liquidity_bin"])
        strategy_version = str(scope["strategy_version"])
        maturity_bin = derive_policy_maturity_bin(
            # The label only chooses the vocabulary; the distance is computed
            # from the decision clock, which is what production does too.
            "h00_12",
            event_date=event_date,
            settlement_timezone=_settlement_timezone(
                conn, station_id, event_date
            ),
            evaluation_at_utc=decision_at_utc,
        )
        delta_policy = resolve_delta_v2_policy(
            conn,
            station_id=station_id,
            stage=stage,
            season_bin=season_bin,
            maturity_bin=maturity_bin,
            liquidity_bin=liquidity_bin,
            strategy_version=strategy_version,
            evaluation_at_utc=decision_at_utc,
            required_authority_scope="PRODUCTION_NO_LIVE",
        )
        bundle = load_production_artifacts(
            conn,
            station_id=station_id,
            evaluation_at_utc=decision_at_utc,
            require_calibration=False,
            require_policy=True,
        )
        resolved = resolve_station_policy(
            activations=bundle.policy_activations,
            global_priors=bundle.global_priors,
            artifacts=bundle.policy_artifacts,
            station_id=station_id,
            stage=stage,
            season_bin=season_bin,
            maturity_bin=maturity_bin,
            liquidity_bin=liquidity_bin,
            strategy_version=strategy_version,
            decision_at_utc=decision_at_utc,
            required_authority_scope="PRODUCTION_NO_LIVE_CANDIDATE",
        )
    except Exception as exc:  # noqa: BLE001 -- reported, never swallowed
        return {
            "refusal": (
                "production_judge_blocked",
                "生产裁判无法解析政策：%s:%s" % (type(exc).__name__, exc),
            ),
        }
    policy_config = dict(_PRODUCTION_DEFAULT_POLICY)
    policy_config.update(resolved.to_adaptive_policy_overrides())
    policy_config.update(_capital_layer_policy_bounds(float(equity)))
    try:
        basket_policy = TargetBasketPolicy.from_mapping(policy_config)
        basket_policy.validate()
    except Exception as exc:  # noqa: BLE001
        return {
            "refusal": (
                "production_judge_blocked",
                "生产裁判篮子政策无效：%s:%s" % (type(exc).__name__, exc),
            ),
        }
    fee_rate = float(basket_policy.fee_rate)
    reported_high = None
    running_high_c = payload.get("running_high_c")
    if running_high_c not in (None, ""):
        try:
            running = float(running_high_c)
        except (TypeError, ValueError):
            running = None
        if running is not None:
            for bucket, ordinal, lower, upper, _probability in payload["buckets"]:
                if (lower is None or running >= float(lower)) and (
                    upper is None or running < float(upper)
                ):
                    reported_high = (str(bucket), int(ordinal))
                    break
    # Charter 75: the snapshot production builds, from the envelope it
    # compiled, over this lane's book; then production's own support
    # resolver for the centre, the direction and the seed.  Nothing of the
    # seat's shape is this lane's any more.
    built = _paper_snapshot_mapping(
        station_id=station_id,
        event_date=event_date,
        observed_at=str(payload["observed_at"]),
        buckets=payload["buckets"],
        quotes=quotes,
        stage=stage,
        held=held,
        held_cost=held_cost,
        reported_high=reported_high,
        # Charter 125: at every stage, not only the one whose posterior
        # already carries it.  ``reported_high`` above stays P1's own risk
        # lineage; this is the arithmetic floor, and a P0 checkpoint
        # compiled after the local midnight is subject to it too.
        observed_running_high_c=_observed_running_high_at(
            conn,
            station_id=station_id,
            event_date=event_date,
            at_utc=str(payload["observed_at"]),
        ),
        chain_seat_buckets=chain_seat_buckets,
        chain_sent_shares=chain_sent_shares,
    )
    if built is None:
        return {
            "refusal": ("incomplete_book", "生产裁判：快照缺腿"),
        }
    mapping, _probabilities, _prices, _depth = built
    try:
        envelope = _production_envelope_for(conn, payload)
        mapping["settlement_timezone"] = envelope.settlement_timezone
        snapshot = snapshot_from_probability_envelope(envelope, mapping)
    except (ContractError, _KernelContractError) as exc:
        return {
            "refusal": (
                "production_judge_blocked",
                "生产裁判快照不成立：%s" % exc,
            ),
        }
    probabilities = {row.bucket: float(row.probability) for row in snapshot.buckets}
    by_bucket = {row.bucket: row for row in snapshot.buckets}
    try:
        resolved = _choose_support(snapshot, basket_policy)
    except (ContractError, _KernelContractError) as exc:
        return {
            "refusal": (
                "production_judge_blocked",
                "生产裁判种子失败：%s" % exc,
            ),
        }
    centre_row = resolved[0]
    desired = set(resolved[5])
    if not desired:
        return {
            "refusal": ("selection_failed", "生产裁判：支持集为空"),
        }
    try:
        shortfall, breakthrough = _derive_dual_risk_scope(snapshot, centre_row)
    except (ContractError, _KernelContractError) as exc:
        return {
            "refusal": (
                "production_judge_blocked",
                "生产裁判风险族解析失败：%s" % exc,
            ),
        }
    shortfall &= desired
    breakthrough &= desired
    # One clock for the enumeration's beta lead band (charter 128 v2) and
    # the solver's completion cutoff (charter 110(六)): the same settlement
    # clock production reads.  NOT ``_resting_deadline`` below: that one
    # folds in the peak cutoff and the resting horizon, which are reasons an
    # order stops rather than the moment the market settles.
    judge_hours_to_cutoff = hours_to_settlement_if_known(
        event_date,
        as_of_utc=decision_at_utc,
        settlement_timezone=_settlement_timezone(conn, station_id, event_date),
    )
    # Charter 136 (四): production's completion budget, off the same kernel
    # function, the same equity and what this chain has spent.
    judge_completion_budget = _completion_budget_usdc_v1(
        book_equity_usdc=float(equity), stage=stage,
        committed_usdc=float(chain_stake),
    )
    try:
        windows, audit = _support_windows(
            snapshot=snapshot,
            desired=desired,
            probabilities=probabilities,
            fee_rate=basket_policy.fee_rate,
            # Charter 50's bounds through the kernel-side helper, not the
            # policy fields directly.  Patching only production's call left
            # this one reading the policy, so the width arms came back
            # byte-identical to their baseline under two fingerprints --
            # the void-arm signature, and the reason this reads one
            # function on both sides now.
            **dict(zip(
                ("minimum_support_buckets", "maximum_support_buckets"),
                _support_width_bounds_v1(),
            )),
            center_bucket=centre_row.bucket,
            probability_stage=stage,
            taf_tx_bucket=_taf_tx_bucket(envelope, stage),
            # Arm B (2026-09-06): the same loader production's entries read,
            # as of this decision's own clock.
            convergence_evidence=load_convergence_evidence(
                conn, station_id=station_id, event_date=event_date,
                as_of_utc=decision_at_utc,
            ),
            # Charter 87: the gate reads lambda off the tables served as of
            # this decision, the same ones the solver below is handed.
            effective_edge_tables=_effective_edge_tables_as_of(decision_at_utc),
            off_centre_claim_bands=_off_centre_claim_bands(),
            hours_to_cutoff=judge_hours_to_cutoff,
            completion_budget_usdc=judge_completion_budget,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "enumerated_windows": windows,
            "refusal": (
                "selection_failed",
                "生产裁判枚举失败：%s" % type(exc).__name__,
            ),
        }
    # Charter 77's signature, recorded per stage by the walk.  Whether this
    # cycle is a decision moment is the solver's own verdict, read back
    # from its audit after the solve -- one implementation, not a copy.
    probability_identity = str(payload.get("probability_identity") or "")
    decision_signature = {
        "probability_identity": probability_identity,
        "edge_state": edge_state_v1(windows),
        "stage": str(stage),
        # Charter 110(七): the third arm.  Same kernel function production
        # calls, on this lane's own held shares, so the two sides cannot
        # drift into two definitions of "the basket changed".
        "basket_holding": basket_holding_signature_v1(held),
    }
    holds_anything = any(float(v or 0.0) > 0.0 for v in held.values())
    # Charter 143: a window that admitted a completion by held count is not
    # held back by 71(a) -- production's solver exempts that winner -- so the
    # identity question is left to the solve whenever one is on the table.
    completion_143_on_table = CHARTER_143_COMPLETION == "on" and any(
        bool(row.get("admissible", True))
        and ((row.get("completion") or {}).get("admitted_by")
             == _COMPLETION_143_RULE)
        for row in windows
    )
    # Charter 71(a) is window-independent, so it is answered here under its
    # own name.  Inside the solver every spending window fails it
    # identically and the seat falls to a window that stands pat, which
    # the walk could only report as a hold.
    if (
        holds_anything
        and not completion_143_on_table
        and last_acted_probability_identity
        and probability_identity
        and str(last_acted_probability_identity) == probability_identity
    ):
        return {
            "enumerated_windows": windows,
            "refusal": (
                "topup_requires_probability_change",
                "宪章 71(a)：概率身份 %s 自上次买入未变，不加仓"
                "（本链已投 %.2f）" % (probability_identity[:12], chain_stake),
            ),
            "decision_signature": decision_signature,
        }
    # Capital the way production hands it to the solver: the scope's share
    # of the book less what the chain has spent, then the consolidation
    # tilt on what is left -- floored at a few minimum clips so a tilted
    # opener can still form a basket.
    scope_cap = scope_stake_cap_usdc(
        book_equity_usdc=float(equity),
        stage=stage,
        committed_usdc=float(chain_stake),
    )
    # The station's ``total_budget_usdc`` is not a sizing bound in
    # production: ``_compile_target_plan`` hands the solver the scope's
    # share of the book less what the station has committed, and the
    # operator's dial binds only at the execution gate, per leg
    # (``budget_ok``).  The walk applies that gate at the same place.
    capital = scope_cap
    # Charter 70 as production runs it, bounded as charter 141 bounds it:
    # the lift to one exchange lot, a completion and the budget gate read the
    # station-day's ceiling, not the sizing allowance.  Handed back for the
    # walk's lot raise.
    lot_room = station_day_ceiling_usdc(
        book_equity_usdc=float(equity),
        committed_usdc=float(chain_stake),
    )
    # Charter 71(b)'s frame: the station-day's ceiling share of the whole
    # book, the number production passes as ``chain_growth_frame_usdc``.
    growth_frame = CHAIN_TOPUP_BANKROLL_USDC or (
        float(equity) * STATION_DAY_HARD_FRACTION
    )
    if 0.0 < tilt < 1.0 and capital > 0.0:
        # The kernel's floor (2026-09-06): the venue's minimum order on each
        # of three candidate legs, as production reads it.
        tilt_floor = consolidation_tilt_floor_usdc(
            minimum_clip_usdc=(
                min(delta_policy.cash_clip_grid)
                if delta_policy.cash_clip_grid else 0.0
            ),
            legs=CONSOLIDATION_TILT_FLOOR_LEGS,
            leg_minimum_costs_usdc=[
                venue_minimum_shares_v1(
                    min_lot_shares=row.min_order_size,
                    ask=row.ask_price,
                    minimum_notional_usdc=(
                        EXCHANGE_MINIMUM_ORDER_NOTIONAL_USDC
                        if CHARTER_83_MIN_NOTIONAL == "on" else 0.0
                    ),
                ) * float(row.ask_price)
                for row in snapshot.buckets
                if row.bucket in desired and row.ask_available
            ],
        )
        capital = min(capital, max(capital * float(tilt), tilt_floor))
    bankroll = float(basket_policy.total_budget_usdc)
    if JUDGE_BANKROLL == "scope":
        bankroll = max(float(capital), 1e-6)
    elif JUDGE_BANKROLL == "book":
        bankroll = max(float(equity), 1e-6)
    # Charter 72: production sizes by the whole-basket Kelly stake off the
    # book; the curve keeps its floor.  The judge arm passes what the
    # runtime passes -- including the switch, which is the half that was
    # missing.  This read the Kelly form unconditionally until 2026-09-09
    # while the ledger's fingerprint hashed ``charter_72_kelly_sizing``
    # away from ``on``, so an arm run with ``PM_CHARTER_72_KELLY=off``
    # rehashed its book and then judged it under the Kelly law anyway.  Two
    # arms, two digests, one strategy: the arm looked like a measurement
    # and was the baseline wearing a different name (measured 2026-09-09 --
    # 278 positions, 151 settled, 873.212 staked, +31.788 pnl, identical to
    # the last decimal across both digests).
    #
    # The default is unchanged, so the standing book does not move: the
    # switch is ``on`` unless an arm sets it, and ``on`` is the Kelly form
    # this line already returned.
    objective_form = (
        _KELLY_OBJECTIVE_FORM
        if CHARTER_72_KELLY_SIZING == "on" and bankroll > 0.0 and equity > 0.0
        else _CURVE_OBJECTIVE_FORM if bankroll > 0.0
        else _VALUE_OBJECTIVE_FORM
    )
    live_buckets = tuple(
        row.bucket for row in snapshot.buckets
        if row.probability > _KERNEL_EPS
    )
    try:
        seated, solution, selection = _solve_over_support_windows(
            # Charter 110(六)'s tau, from the same clock production reads.
            # NOT ``_resting_deadline`` below: that one folds in the peak
            # cutoff and the resting horizon, which are reasons an order
            # stops rather than the moment the market settles.
            hours_to_cutoff=judge_hours_to_cutoff,
            windows=windows,
            window_audit=audit,
            live_buckets=live_buckets,
            snapshot=snapshot,
            probabilities=probabilities,
            shortfall=set(shortfall),
            breakthrough=set(breakthrough),
            delta_policy=delta_policy,
            basket_policy=basket_policy,
            capital_available_usdc=capital,
            objective_form=objective_form,
            bankroll_usdc=bankroll,
            book_equity_usdc=float(equity),
            # Charters 66(A), 71 and 77 bind inside the solver, per
            # window, exactly as production's compile hands them in.  A
            # head window refused on chain growth hands the seat to the
            # next one there; without these the solver never refused
            # anything and this lane re-judged the seat afterwards.
            probability_identity=probability_identity or None,
            last_acted_probability_identity=(
                str(last_acted_probability_identity)
                if last_acted_probability_identity else None
            ),
            # Charter 121: rows this seat has already had refused by the
            # post-lift floor downstream, so the winner chain picks among
            # what is left rather than this lane holding the whole payload.
            excluded_candidate_ids=tuple(excluded_candidate_ids),
            # Charter 84 (八) as charter 141 bounds it: production sizes a
            # completion against the station-day's ceiling.
            completion_capital_usdc=lot_room,
            chain_growth_frame_usdc=growth_frame,
            last_decision_signature=last_decision_signature,
            # Charter 68(c)/87: production's solver reads the store's
            # activated tables as of the decision; this judge read the
            # kernel bootstrap until 2026-09-06, which is a belief
            # production stopped serving on 2026-09-05.
            effective_edge_tables=_effective_edge_tables_as_of(decision_at_utc),
            # Charter 122's band is NOT passed here.  It shapes which windows
            # exist, and ``_support_windows`` above -- the one call that
            # enumerates them -- already read it; the solver is handed the
            # windows themselves and re-declaring the band to it would be a
            # parameter nothing downstream reads.  (Charter 136's completion
            # budget IS read by the solver -- it sizes the completion -- so it
            # is passed, below.)  It was declared here when
            # charter 122 (five) landed (c032ea92), and since the runtime's
            # solver never grew the keyword, every production-judge replay
            # from that commit onward died on ``TypeError`` before its first
            # station -- the whole ledger arm, not one lane of it.
            completion_budget_usdc=judge_completion_budget,
        )
    except (ContractError, _KernelContractError) as exc:
        if holds_anything and str(exc) == (
            "delta_v2_no_solvable_support_window"
        ):
            # Every window that would spend was refused by the chain law
            # -- charter 66(A) par or 71(b) growth -- and none stood pat.
            return {
                "enumerated_windows": windows,
                "refusal": (
                    "no_chain_growth",
                    "生产求解器：每个窗口都被链法则拒绝"
                    "（66甲款 par 或 71乙款增益）· 本链已投 %.2f"
                    % chain_stake,
                ),
                "decision_signature": decision_signature,
            }
        detail = "生产裁判求解失败：%s" % exc
        if str(exc) in (
            "delta_v2_support_empty",
            "delta_v2_support_scope_unquoted",
            "delta_v2_support_desired_empty",
            "delta_v2_no_solvable_support_window",
        ):
            # Which gate emptied the list (2026-09-05).  The funnel the
            # enumeration published, in the page's own gate vocabulary,
            # so a reader can tell "the seed window was structurally
            # refused and nothing else cleared the price" from a solver
            # fault; and, when windows existed and none solved, what
            # each window's solve threw.
            detail += " · " + _window_refusal_detail(audit)
            by_reason: dict[str, int] = {}
            for row in getattr(exc, "window_failures", None) or ():
                key = str(row.get("reason") or "") if isinstance(
                    row, Mapping
                ) else str(row)
                by_reason[key] = by_reason.get(key, 0) + 1
            if by_reason:
                detail += " · 逐窗求解失败 " + "、".join(
                    "%s×%d" % item for item in sorted(by_reason.items())
                )
        return {
            "enumerated_windows": windows,
            "refusal": ("production_judge_blocked", detail),
        }
    moment = dict(selection.get("decision_moment") or {})
    completion_143_winner = (
        solution.get("winner_completion_rule") == _COMPLETION_143_RULE
    )
    if moment.get("is_decision_moment") is False and not completion_143_winner:
        # Charter 77, as the solver ruled it: same served identity, same
        # admissibility as the last recorded decision of this stage.
        return {
            "enumerated_windows": windows,
            "refusal": (
                "no_decision_moment",
                "宪章 77：概率身份、可入座状态（%s）与持仓（%s）都没变，"
                "只是盘口动了"
                % (
                    decision_signature["edge_state"],
                    decision_signature["basket_holding"] or "空",
                ),
            ),
            "decision_signature": decision_signature,
        }
    target = {
        str(bucket): float(quantity)
        for bucket, quantity in (
            solution.get("target_shares_by_bucket") or {}
        ).items()
    }
    delta_cost = 0.0
    for bucket in seated:
        delta = max(
            0.0, target.get(str(bucket), 0.0) - float(held.get(bucket, 0.0))
        )
        row = by_bucket.get(str(bucket))
        if delta <= 0.0 or row is None or not row.ask_available:
            continue
        delta_cost += delta * float(row.ask_price) * (1.0 + fee_rate)
    return {
        "enumerated_windows": windows,
        "decision_signature": decision_signature,
        "buckets": tuple(str(bucket) for bucket in seated),
        # Charter 121: which row took the seat, so the caller can exclude it
        # and ask again when its lifted shape fails charter 93's floor.
        "winner_candidate_id": str(solution.get("winner_candidate_id") or ""),
        "winner_candidate_kind": str(
            solution.get("winner_candidate_kind") or ""
        ),
        # Charter 127 (五): which rule took the seat, because the answer now
        # changes how the order is EXECUTED and not only how it is sized.
        # Production crosses a designated completion as a limit FOK and rests
        # everything else; this lane rests both, so the two are measuring
        # different execution from the day that clause shipped.  Carried here
        # first so the divergence can be counted before it is mirrored -- a
        # fill model written for a case this book never reaches would be a
        # second unmeasured difference, not a fix for the first.
        "winner_candidate_origin": str(
            solution.get("winner_candidate_origin") or ""
        ),
        # Charter 143: set only when the winner is a completion by held
        # count; the walk's 71(a), floor and edge mirrors read it.
        "winner_completion_rule": solution.get("winner_completion_rule"),
        "hours_to_cutoff": judge_hours_to_cutoff,
        # Charter 143 v2: the send history production's exemption reads, off
        # the same snapshot and the same helper.
        "completion_143_sent_shares": (
            None
            if ((selection.get("seated") or {}).get("completion") or {})
            .get("taf_tx_entry")
            else _completion_143_sent_shares(snapshot)
        ),
        # Charter 145: the seat is charter 90's TX-entry span, which the
        # exemption reads as multi-leg whatever it holds.
        "completion_143_taf_entry": bool(
            ((selection.get("seated") or {}).get("completion") or {})
            .get("taf_tx_entry")
        ),
        "target_shares": target,
        "delta_cost_usdc": delta_cost,
        "capital_usdc": capital,
        "curve_score": float(
            (selection.get("seated") or {}).get("curve_score") or 0.0
        ),
        # For the walk's reading of a hold on an empty chain (2026-09-05):
        # the funnel the seat came out of.
        "window_audit": dict(audit or {}),
        "delta_policy_id": str(delta_policy.artifact_id),
        "scope": (season_bin, maturity_bin, liquidity_bin, strategy_version),
        # The centre the seat was actually chosen around.  Production's
        # ``_choose_support`` reads the envelope this lane never opens --
        # model centre, dispersion, rung width -- so its mean bucket is
        # not always the one this lane's own enumeration names, and the
        # row has to describe the enumeration that seated it.  Recorded
        # against the ledger's instead, seven P1 rows in the 2026-08-15
        # book named a centre outside their own window.
        "ladder_center_bucket": str(
            (audit or {}).get("ladder_center_bucket") or centre_row.bucket
        ),
        # Production's execution-gate dial and its lot-lift allowance, for
        # the two checks the walk applies where production applies them.
        "total_budget_usdc": float(basket_policy.total_budget_usdc),
        "lot_room_usdc": float(lot_room),
        # Charter 73: the belief the seat was judged on and the policy's
        # authorization epsilon, for the walk's execution-layer check.
        "edge_probabilities": dict(
            (selection.get("seated") or {}).get("window_probabilities")
            or {}
        ),
        # Audit only (user ruling, 2026-09-09).  h/K for the seated window
        # and the marginal Δh/ΔK of what it adds over what the seat holds,
        # from production's own reading -- not recomputed here, because a
        # ledger column that restates the rule is a column that can drift
        # from it.
        "marginal_coverage": (
            (selection.get("seated") or {}).get("marginal_coverage")
        ),
        "edge_epsilon": float(
            delta_policy.execution_weights.epsilon_score
        ),
    }


def _completion_143_exempt_on_legs(
    *,
    verdict: Mapping[str, Any] | None,
    seat: Mapping[str, Any],
    held: Mapping[str, float],
    held_cost: Mapping[str, float],
    leg_shares: Sequence[float],
    leg_cash: Sequence[float],
) -> bool:
    """Charter 143 on this book's legs, as production reads its lifted bundle.

    ``True`` only when the production judge's winner was a completion by held
    count and the kernel's exemption holds on the increments this walk is
    about to post -- ``leg_shares``/``leg_cash`` are per seat bucket, added to
    what the chain holds.
    """

    if (verdict or {}).get("winner_completion_rule") != _COMPLETION_143_RULE:
        return False
    buckets = [str(b) for b in seat["buckets"]]
    return bool(_completion_143_exemption_v1(
        support=tuple(buckets),
        held_shares=held,
        held_cost_usdc=held_cost,
        target_shares={
            b: float(held.get(b, 0.0)) + float(q)
            for b, q in zip(buckets, leg_shares)
        },
        target_cost_usdc={
            b: float(held_cost.get(b, 0.0)) + float(c)
            for b, c in zip(buckets, leg_cash)
        },
        claim_by_bucket=verdict.get("edge_probabilities") or {},
        below_leg_floor_buckets=tuple(
            (seat.get("completion") or {}).get("market_dead_buckets") or ()
        ),
        hours_to_cutoff=verdict.get("hours_to_cutoff"),
        sent_shares_by_bucket=verdict.get("completion_143_sent_shares"),
        taf_entry=bool(verdict.get("completion_143_taf_entry")),
    )["exempt"])


def _production_sized_legs(
    *,
    legs: list[list[dict[str, float]]],
    buckets: Sequence[str],
    target: Mapping[str, float],
    held: Mapping[str, float],
    fee_rate: float,
) -> tuple[list[float], float, list[float]] | None:
    """Walk the book for the production judge's target on top of the held.

    Returns the wanted totals per leg (what the chain should hold after
    this buy), the cash the increments cost, and the per-leg cash -- the
    same three the ledger's own sizers return, so everything downstream
    reads them unchanged.  ``None`` when a leg's increment cannot be filled
    from the depth on the book.
    """

    shares: list[float] = []
    cash: list[float] = []
    for leg, bucket in zip(legs, buckets):
        want_total = float(target.get(str(bucket), 0.0))
        have = float(held.get(str(bucket), 0.0))
        delta = max(0.0, want_total - have)
        if delta <= 0.0:
            shares.append(max(want_total, have))
            cash.append(0.0)
            continue
        # A taker is a FOK at the top of the offer: production's candidate
        # carries ``limit_price = row.ask_price`` and sizes the leg against
        # ``_executable_depth_cash``, which counts only the offer at or
        # below that limit.  A thin top level cuts the leg to what is
        # there (``sizing_clamped_below_authorized_clip``, then the bundle
        # is re-scored at the cut sizes); an empty one refuses it
        # (``executable_depth_empty``).  This walker used to descend the
        # book for the full increment instead, paying deeper levels for
        # shares production would not have bought: 120 of 861 legs on the
        # 2026-09-04 book, 29.16 USDC above the top of book, one leg 137%
        # over it.
        got, paid = _walk_ask_shares_at_limit(leg, delta)
        if got <= 0.0:
            return None
        shares.append(have + got)
        cash.append(paid * (1.0 + fee_rate))
    return shares, sum(cash), cash


def _walk_ask_shares_at_limit(
    levels: Sequence[Mapping[str, float]], want: float
) -> tuple[float, float]:
    """Shares bought and cash spent lifting only the top of the offer.

    The limit is the best ask itself, as production's FOK carries it, so
    only levels at that price fill; the rest of the book is what the
    order would have walked into and does not.
    """

    if not levels:
        return 0.0, 0.0
    limit = float(levels[0]["price"])
    return _walk_ask_shares(
        [level for level in levels if float(level["price"]) <= limit + 1e-9],
        want,
    )


# The strategies the comparison sweep can put in production's place.  Each
# returns the same shape ``_support_windows`` does, so everything downstream
# -- the positive-EV gate, Kelly, the depth walk, settlement -- is unchanged
# and the only thing varying between runs is which buckets were chosen.
#
# Why these run through the ledger rather than a notebook: four separate
# simulations of the live strategy returned -17.2%, +24.3%, +73.5% and
# +146%, against the ledger's -0.2%.  Every one of them was wrong in the
# same direction, because a penny leg's payout dominates any P&L it enters
# and only the ledger walks real ask depth with the "average price under the
# win probability" bound the kernel enforces.  Strategy questions are
# answered here or not at all.
WINDOW_STRATEGIES = (
    "live",             # whatever _support_windows currently ranks first
    "mass4",            # widest window holding the centre, most mass (D)
    "mass3",
    "peak4",            # most-mass contiguous run, centre not required (C)
    "mu4",              # centred on the distribution mean (B)
    "centre1",          # the ladder centre alone (E)
    "mu1",              # the mean bucket alone (F)
    "leg_positive_ev",  # every bucket whose own p exceeds its own ask (G)
    "mass3_best_leg",   # take mass3's basket, then buy only its best p/c leg
    "mass4_best_leg",
)


def _resting_ladder(
    row: Any,
    steps: int = MAKER_RESTING_LADDER_STEPS,
    *,
    value_cap: float | None = None,
) -> list[float]:
    """The price band one leg rests across, cheapest first.

    Collapses to a single price when the spread has no room for a ladder --
    one tick of spread leaves nowhere to stand between the two sides.

    ``value_cap`` is the model's part of the price: no rung ever asks for
    more than the leg is worth to us (lambda times its probability), so a
    hedge leg the market prices above our valuation posts only below it
    and buys nothing unless the book actually comes down.  A cap under the
    bid is a legal deep resting bid, not a refusal; a cap at or under one
    tick leaves nothing postable and the leg sits out.
    """

    if not row.ask_available:
        return []
    ask = float(row.ask_price)
    bid = float(getattr(row, "bid_price", 0.0) or 0.0)
    tick = float(getattr(row, "price_tick_size", 0.001) or 0.001)
    if bid <= 0.0 or ask <= 0.0:
        return []
    # Production's own ladder, not a second copy of it.  The copy that used
    # to live here rounded each rung to six decimals where production floors
    # it to the tick, so this lane priced -- and "filled" -- orders at prices
    # no exchange would accept: 0.2864 on a tick-0.001 book.  Measured over
    # 64 (bid, ask, tick, cap) combinations on 2026-08-26, 37 produced a
    # different ladder on the two sides.
    #
    # MAKER_RESTING_BAND_TOP == "ask" is the research arm that lifts the top
    # onto the ask itself rather than a tick under it -- paying the spread to
    # guarantee the leg.  Handing the ceiling in as ``ask + tick`` expresses
    # that in production's own arithmetic (it subtracts a tick from whatever
    # ceiling it is given), so the arm gets tick-legal rungs too instead of
    # being a second place the two can diverge.
    # The cap is this lane's research-arm ceiling; production's ladder no
    # longer takes one, so it is folded into the ask before the call and the
    # arithmetic stays production's.
    if MAKER_RESTING_ANCHORED:
        # Charter 94: the anchor's shape, and no cap on the rungs.
        return bid_anchored_ladder(bid=bid, ask=ask, tick=tick)
    ceiling = ask if value_cap is None else min(ask, float(value_cap))
    if MAKER_RESTING_BAND_TOP == "ask":
        return _maker_ladder(
            bid=bid, ask=ceiling + tick, tick=tick,
            steps=steps, improve_ticks=MAKER_RESTING_IMPROVE_TICKS,
        )
    return _maker_ladder(
        bid=bid, ask=ceiling, tick=tick,
        steps=steps, improve_ticks=MAKER_RESTING_IMPROVE_TICKS,
    )


def _value_cap(probability: float | None) -> float | None:
    """The dearest price the model lets a rung ask for one leg."""

    if probability is None or MAKER_RESTING_VALUE_LAMBDA <= 0:
        return None
    return MAKER_RESTING_VALUE_LAMBDA * float(probability)


def _consolidation_signals(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    entry_decision_at_utc: str,
    probabilities: Mapping[str, float],
    ordinals: Mapping[str, int],
    quotes: Mapping[str, Any],
) -> dict[str, Any]:
    """How much pricing work the market had already done at entry.

    Three booleans and their count, recorded on the position row and never
    consulted by any decision.  ``stable``: the mass centre unchanged over
    the prior CONSOLIDATION_STABLE_LOOKBACK committed checkpoints of the
    station-day, any stage (production's chain).  ``sharp``: the
    entry distribution's top-two mass at or above the sharpness line.
    ``quoted``: the ideal centre+-1 window carries at least
    CONSOLIDATION_QUOTED_FRACTION of its mass on buckets asked at or above
    the leg floor.  The measured inversion that motivates recording them:
    the settled book's entire positive tail sits in the stratum where all
    three are off.
    """

    centres: list[str] = []
    if probabilities:
        centres.append(max(probabilities, key=lambda b: probabilities[b]))
    # Every stage, not P0 alone: production's ``prior_centre_buckets`` are
    # the argmax of each earlier committed envelope on the station-day,
    # whatever stage served it (``_compile_target_plan``'s ``_earlier``
    # walks the chain unfiltered).  A P0-M checkpoint between two P0 ones
    # can therefore break the run there; filtered to P0 it could not here,
    # and the two lights disagreed on exactly those days (aligned
    # 2026-09-04).
    for (hid,) in conn.execute(
        "SELECT history_id FROM basket_probability_bridge_history"
        " WHERE station_id=? AND event_date=? AND state='COMMITTED'"
        "   AND decision_at_utc<?"
        " ORDER BY decision_at_utc DESC LIMIT ?",
        (station_id, event_date, entry_decision_at_utc,
         CONSOLIDATION_STABLE_LOOKBACK),
    ):
        row = conn.execute(
            "SELECT bucket FROM basket_probability_bridge_bucket_probability"
            " WHERE history_id=? ORDER BY probability DESC LIMIT 1",
            (hid,),
        ).fetchone()
        if row:
            centres.append(str(row[0]))
    stable = (
        len(centres) >= CONSOLIDATION_STABLE_LOOKBACK + 1
        and len(set(centres)) == 1
    )

    top2 = sum(sorted(probabilities.values(), reverse=True)[:2])
    sharp = top2 >= CONSOLIDATION_SHARP_TOP2

    quoted = False
    if probabilities:
        order = sorted(probabilities, key=lambda b: ordinals.get(b, 0))
        ci = max(
            range(len(order)), key=lambda i: probabilities[order[i]]
        )
        ideal = order[max(0, ci - 1):ci + 2]
        ideal_mass = sum(probabilities[b] for b in ideal)
        ok_mass = 0.0
        for bucket in ideal:
            quote = quotes.get(str(bucket))
            if not isinstance(quote, Mapping):
                continue
            asks = quote.get("ask_levels") or []
            best = quote.get("best_ask")
            if best is None and asks:
                best = (asks[0] or {}).get("price")
            if best is not None and not _leg_below_floor(
                ask=best,
                bid=quote.get("bid_price", quote.get("best_bid")),
                probability=probabilities.get(bucket),
            ):
                ok_mass += probabilities[bucket]
        quoted = (
            ideal_mass > 0
            and ok_mass / ideal_mass >= CONSOLIDATION_QUOTED_FRACTION
        )

    return {
        "stable": bool(stable),
        "sharp": bool(sharp),
        "quoted": bool(quoted),
        "count": int(stable) + int(sharp) + int(quoted),
    }



def _market_top_bucket(
    window: Sequence[str] | None, by_bucket: Mapping[str, Any] | None
) -> str | None:
    """The rung the book asks most for, among those on the window.

    The market's price ORDER is the piece that carries direction: measured on
    the pairs where exactly one shoulder settled, the dearer side was the one
    21 times out of 22, while our own claim picked it 12 times out of 22.
    """

    if not window or not by_bucket:
        return None
    best, top = None, None
    for bucket in window:
        row = by_bucket.get(str(bucket))
        if row is None or not getattr(row, "ask_available", False):
            continue
        ask = float(getattr(row, "ask_price", 0.0) or 0.0)
        if top is None or ask > top:
            best, top = str(bucket), ask
    return best


def _tail_bucket(
    window: Sequence[str],
    centre_bucket: str | None,
    probabilities: Mapping[str, float] | None,
) -> str | None:
    """The insurance rung: the end of the window farther from the centre.

    The window arrives ordinal-ordered, so distance is list distance.  A
    centre sitting exactly mid-window leaves two equally far ends; the
    lighter one is the insurance (the heavier end is a second favourite,
    not a hedge).
    """

    ordered = [str(b) for b in window]
    if centre_bucket is None or str(centre_bucket) not in ordered:
        return None
    if len(ordered) < 2:
        return None
    at = ordered.index(str(centre_bucket))
    low_d, high_d = at, len(ordered) - 1 - at
    if low_d > high_d:
        return ordered[0]
    if high_d > low_d:
        return ordered[-1]
    probs = probabilities or {}
    return min(
        (ordered[0], ordered[-1]),
        key=lambda b: float(probs.get(b, 0.0)),
    )


def _coverage_tier(
    bucket: str,
    *,
    window: Sequence[str] | None,
    probabilities: Mapping[str, float] | None,
) -> str:
    """Which coverage tier a leg belongs to, under the belief in force now.

    The roles are live, not seated (user ruling, 2026-08-24): the core is
    wherever the current belief puts the most mass, its neighbours are the
    shoulders, everything further is a tail.  A distribution that walks
    intraday walks the roles with it -- the rung seated as centre can end the
    day as a tail, and it is the day's belief, not the entry's, that decides
    what a fill there is worth.  Tails are the hedge legs: they fill at their
    clamp like everyone else, they just never command a premium.
    """

    labels = [str(item) for item in (window or [])]
    if not labels:
        return "core"
    claims = probabilities or {}
    core_idx = max(
        range(len(labels)),
        key=lambda i: float(claims.get(labels[i], 0.0) or 0.0),
    )
    try:
        distance = abs(labels.index(str(bucket)) - core_idx)
    except ValueError:
        return "tail"
    if distance == 0:
        return "core"
    if distance == 1:
        return "shoulder"
    return "tail"


def _leg_value_cap(
    bucket: str,
    *,
    probabilities: Mapping[str, float] | None,
    centre_bucket: str | None,
    window: Sequence[str] | None = None,
    market_top_bucket: str | None = None,
    all_in_ask: float | None = None,
) -> float | None:
    """The value cap for one leg, with the centre bucket exempt.

    The centre is the basket's base holding (user ruling, 2026-08-20): a
    clamp that prices it away turns every basket into a shoulders-only
    bet, and the charter's whole shape argument starts from the centre
    being held.  Its rungs stand on the book skeleton alone.

    Under the tail experiment the window's insurance rung gets its own
    treatment; with the knob unset (production default) the window is
    ignored and behaviour is unchanged.
    """

    # Charter 63 retired (2026-09-03): this is maker_resting research-arm
    # arithmetic.  The taker family never reads it -- ``_execution_price``
    # returns the fee-adjusted ask before any cap -- so it is left as pure
    # arithmetic for the arm's own simulation rather than gated here.
    if centre_bucket is not None and str(bucket) == str(centre_bucket):
        if MAKER_RESTING_CENTRE_LAMBDA == 1.0:
            # Unchanged behaviour: the caller falls back to the claim itself,
            # which is exactly a lambda of one.
            return None
        probability = (probabilities or {}).get(str(bucket))
        return (
            None if probability is None
            else MAKER_RESTING_CENTRE_LAMBDA * float(probability)
        )
    if MAKER_RESTING_MAX_CLAIM_EDGE and all_in_ask is not None:
        probability = (probabilities or {}).get(str(bucket))
        if (
            probability is not None
            and float(probability) - float(all_in_ask)
            >= float(MAKER_RESTING_MAX_CLAIM_EDGE) - 1e-9
        ):
            # Toxic band: the claim stands so far above the price that the
            # market has ruled the leg out, not underpriced it.  A zero cap
            # leaves nothing postable, which the ladder and the withdrawal
            # filter both already honour.
            return 0.0
    if (
        MAKER_MARKET_TOP_EXEMPT
        and market_top_bucket is not None
        and str(bucket) == str(market_top_bucket)
    ):
        return None
    if MAKER_RESTING_SHOULDER_LAMBDA and _is_shoulder(
        bucket, centre_bucket, window
    ):
        if MAKER_RESTING_SHOULDER_LAMBDA == "exempt":
            return None
        try:
            shoulder_lambda = float(MAKER_RESTING_SHOULDER_LAMBDA)
        except ValueError:
            shoulder_lambda = None
        if shoulder_lambda is not None:
            probability = (probabilities or {}).get(str(bucket))
            return (
                None if probability is None
                else shoulder_lambda * float(probability)
            )
    if MAKER_RESTING_TAIL_LAMBDA and window is not None:
        tail = _tail_bucket(window, centre_bucket, probabilities)
        if tail is not None and str(bucket) == tail:
            if MAKER_RESTING_TAIL_LAMBDA == "exempt":
                return None
            try:
                lam = float(MAKER_RESTING_TAIL_LAMBDA)
            except ValueError:
                lam = None
            if lam is not None:
                p = (probabilities or {}).get(str(bucket))
                return None if p is None else lam * float(p)
    # Past the research arms: the plain lambda.  Until 2026-09-03 this
    # called production's ``_hedge_value_cap``; production retired it, so the
    # number is this lane's alone and says so in the binding test.
    return _value_cap((probabilities or {}).get(str(bucket)))


def _market_centre_ordinal(snapshot: Any) -> int | None:
    """The ordinal of the bucket the BOOK prices highest, not the model.

    Two-sided rungs only.  A one-sided quote has no mid, and the whole point
    of this reading is to use the market's own opinion rather than ours --
    the model's centre is the thing being checked against, so it cannot also
    be the reference.
    """

    best: tuple[float, int] | None = None
    for row in getattr(snapshot, "buckets", ()) or ():
        if not getattr(row, "ask_available", False):
            continue
        bid = float(getattr(row, "bid_price", 0.0) or 0.0)
        ask = float(getattr(row, "ask_price", 0.0) or 0.0)
        if bid <= 0.0 or ask <= bid:
            continue
        mid = (bid + ask) / 2.0
        ordinal = int(getattr(row, "ordinal", 0))
        if best is None or mid > best[0]:
            best = (mid, ordinal)
    return None if best is None else best[1]


def _beyond_market_distance(
    ordinal: int | None, centre_ordinal: int | None
) -> bool:
    """Is this rung further from the book's centre than the arm allows?

    Fail-open on a missing reading: a book with no two-sided quote at all
    has no centre to measure from, and refusing every leg there would make
    the arm a liquidity filter wearing a distance filter's name.
    """

    if MARKET_DISTANCE_MAX <= 0:
        return False
    if ordinal is None or centre_ordinal is None:
        return False
    return abs(int(ordinal) - int(centre_ordinal)) > MARKET_DISTANCE_MAX


def _leg_below_floor(
    *,
    ask: float | None,
    bid: float | None,
    probability: float | None,
    fee_rate: float = 0.0,
    ordinal: int | None = None,
    market_centre_ordinal: int | None = None,
) -> bool:
    """Charter 91 + 103, the ledger's read point for the leg floor.

    Production's is ``delta_v2_runtime._leg_below_floor`` and both call the
    same kernel on the same side of the book: the mid where the rung is
    two-sided, the ask where nobody bids.  Deliberately not the execution
    price -- this book prices a maker leg at the resting band and
    production prices it at the offer, and a floor that followed those
    would be screening two different things in the two books.
    """

    if _beyond_market_distance(ordinal, market_centre_ordinal):
        return True
    if ask is None:
        return False
    return leg_below_floor(
        float(ask) * (1.0 + float(fee_rate)),
        probability,
        MINIMUM_LEG_ALL_IN_ASK,
        bid=float(bid or 0.0),
    )


def _execution_price(row: Any, *, value_cap: float | None = None) -> float | None:
    """The per-share price this run assumes for one leg."""

    if not row.ask_available:
        return None
    ask = float(row.ask_price) * (1.0 + FEE_RATE)
    if EXECUTION_MODE == "taker":
        return ask
    bid = float(getattr(row, "bid_price", 0.0) or 0.0)
    if bid <= 0.0:
        # No resting bid to join or improve: a maker order here would be
        # alone on its side, which is the one-sided quote this ledger
        # already refuses to treat as a price.
        return None
    tick = float(getattr(row, "price_tick_size", 0.001) or 0.001)
    if EXECUTION_MODE == "maker_resting":
        # The band's own mean: what the leg pays if the whole ladder fills.
        # The realised price replaces this once the book has been walked --
        # this is the number the gate and the Kelly sizing see beforehand.
        rungs = _resting_ladder(row, value_cap=value_cap)
        if not rungs:
            return None
        price = sum(rungs) / len(rungs)
    elif EXECUTION_MODE == "maker_bid":
        price = bid
    elif EXECUTION_MODE == "maker_improve":
        price = min(bid + tick, ask)
    elif EXECUTION_MODE == "mid":
        price = (bid + ask) / 2.0
    else:
        raise ValueError(f"unknown execution mode: {EXECUTION_MODE}")
    return price * (1.0 + FEE_RATE)


def _maker_leg_prices(
    snapshot: Any,
    buckets: Sequence[str],
    probabilities: Mapping[str, float] | None = None,
    centre_bucket: str | None = None,
) -> list[float]:
    """The assumed resting price for each leg, in the seat's own order.

    With probabilities the band mean is value-capped, so the sizing prices
    the order the way it will actually be posted.
    """

    by_bucket = {row.bucket: row for row in snapshot.buckets}
    prices = []
    for bucket in buckets:
        row = by_bucket.get(str(bucket))
        price = None if row is None else _execution_price(
            row,
            value_cap=_leg_value_cap(
                str(bucket),
                probabilities=probabilities,
                centre_bucket=centre_bucket,
                window=buckets,
                market_top_bucket=_market_top_bucket(buckets, by_bucket),
                all_in_ask=(
                    float(row.ask_price) if row.ask_available else None
                ),
            ),
        )
        # A leg with no price here cannot have been seated: the strategy
        # builder refuses those.  Falling back to the ask keeps the walk
        # well-formed if a caller ever gets here another way.
        prices.append(
            float(price)
            if price is not None
            else float(row.ask_price) * (1.0 + FEE_RATE)
        )
    return prices


def _seat_pricing_basis(stage: str) -> str:
    """Which price this stage's gate judges on -- crossing, unless exempt."""

    if str(stage) in SEAT_PRICING_LANE_STAGES:
        return "lane"
    return SEAT_PRICING_BASIS


def _seat_execution_price(
    seat: Mapping[str, Any],
    *,
    snapshot: Any,
    probabilities: Mapping[str, float],
    centre_bucket: str,
    stage: str,
) -> float:
    """The price the stake rule and the payout test judge the seat on.

    Crossing by default since 2026-08-27 (user ruling; three same-entry
    rebuilds at +50.7/+55.5/+62.8 USDC) -- the ask is treated as
    information.  The lane paragraphs below are kept as the history of the
    one-day reversal they argued for, and ``lane`` remains the basis for
    the stages in ``SEAT_PRICING_LANE_STAGES`` (P0-M: posts once, takes
    whatever book it finds) and for any run that sets the env back.

    ``_support_windows`` belongs to production and production crosses the
    spread, so the ``all_in_price`` it hands back is always the sum of the
    seated legs' top-of-book offers.  Under a maker mode that is not a
    price this run would ever pay: the legs rest inside the spread, and the
    difference is the whole of the book's vig plus the spread itself.

    Sizing and refusing on the crossing total while filling at the resting
    band is how KHOU 2026-08-27 came to be refused as "dearer than its own
    payout" at 1.100 -- a basket this lane would have posted at 0.876, on a
    book whose eleven buckets asked 1.290 and bid 0.982.  Measured over the
    four event days to 2026-08-26, 94 seats were refused on the crossing
    total; 2 of them carry a positive edge at the price they would actually
    have been posted at (ZSQD 2026-08-26 P1: 1.210 crossing, 0.906 resting,
    against a 0.958 claim).  The other 92 are refused either way -- but
    every one of them was told the wrong reason, and 57 of them were told
    the market was dearer than a full payout when this lane's own price was
    not.

    Taker keeps the crossing total, because there it *is* the price.

    One seam stays open by design: the edge gate inside ``_support_windows``
    still judges on the crossing total, so a window it refused for being
    dear is not on this shortlist to be re-priced here.  Closing that means
    changing production's own enumeration, which is a different layer and a
    different decision.
    """

    if EXECUTION_MODE == "taker" or _seat_pricing_basis(stage) == "crossing":
        return float(seat["all_in_price"])
    return sum(
        _maker_leg_prices(
            snapshot,
            seat["buckets"],
            probabilities,
            centre_bucket=centre_bucket,
        )
    )


def _window_row(
    buckets: Sequence[str],
    *,
    snapshot: Any,
    probabilities: Mapping[str, float],
    centre_bucket: str | None = None,
) -> dict[str, Any] | None:
    """Price a chosen bucket set the way ``_support_windows`` prices one."""

    by_bucket = {row.bucket: row for row in snapshot.buckets}
    ordered = [b for b in buckets if b in by_bucket]
    if not ordered:
        return None
    market_centre = _market_centre_ordinal(snapshot)
    quotes = []
    for bucket in ordered:
        quote = _execution_price(
            by_bucket[bucket],
            value_cap=_leg_value_cap(
                str(bucket),
                probabilities=probabilities,
                centre_bucket=centre_bucket,
                window=ordered,
                market_top_bucket=_market_top_bucket(ordered, by_bucket),
                all_in_ask=(
                    float(by_bucket[bucket].ask_price)
                    if by_bucket[bucket].ask_available else None
                ),
            ),
        )
        # Charter 91: the floor a leg faces falls with our belief in it.
        # Charter 103: and it is read off that leg's mid, not its offer.
        if quote is None or _leg_below_floor(
            ask=(
                float(by_bucket[bucket].ask_price)
                if by_bucket[bucket].ask_available else None
            ),
            bid=getattr(by_bucket[bucket], "bid_price", 0.0),
            probability=probabilities.get(bucket),
            fee_rate=FEE_RATE,
            ordinal=getattr(by_bucket[bucket], "ordinal", None),
            market_centre_ordinal=market_centre,
        ):
            return None
        quotes.append(quote)
    win = sum(float(probabilities.get(b, 0.0)) for b in ordered)
    price = sum(quotes)
    return {
        "buckets": tuple(
            sorted(ordered, key=lambda b: by_bucket[b].ordinal)
        ),
        "win_probability": win,
        "all_in_price": price,
        "edge": win - price,
        "log_growth_rank": None,
        "is_incumbent": False,
    }


def _strategy_windows(
    strategy: str,
    *,
    snapshot: Any,
    probabilities: Mapping[str, float],
    center: str,
    ordinals: Mapping[str, int],
) -> list[dict[str, Any]]:
    """One candidate seat under the named strategy, priced and gated.

    Returned as a list so the caller's ``priced_windows[0]`` and its
    "nothing priced" refusal both keep working unchanged.
    """

    by_ordinal = sorted(probabilities, key=lambda b: ordinals[b])
    count = len(by_ordinal)
    if not count:
        return []
    index = {bucket: position for position, bucket in enumerate(by_ordinal)}
    mass = lambda seg: sum(  # noqa: E731
        float(probabilities.get(b, 0.0)) for b in seg
    )
    centre_at = index.get(center)
    if centre_at is None:
        centre_at = max(
            range(count), key=lambda j: probabilities.get(by_ordinal[j], 0.0)
        )
    mean_at = sum(
        j * float(probabilities.get(by_ordinal[j], 0.0)) for j in range(count)
    ) / max(sum(probabilities.values()), 1e-12)

    def contiguous(width: int, *, hold_centre: bool) -> list[str] | None:
        width = min(width, count)
        starts = range(0, count - width + 1)
        if hold_centre:
            starts = [
                a for a in starts if a <= centre_at < a + width
            ]
        best = None
        for start in starts:
            seg = by_ordinal[start:start + width]
            value = mass(seg)
            if best is None or value > best[0]:
                best = (value, seg)
        return None if best is None else best[1]

    if strategy == "mass4":
        chosen = contiguous(4, hold_centre=True)
    elif strategy == "mass3":
        chosen = contiguous(3, hold_centre=True)
    elif strategy == "peak4":
        chosen = contiguous(4, hold_centre=False)
    elif strategy == "mu4":
        low = max(0, min(int(round(mean_at - 1.5)), count - 4))
        chosen = by_ordinal[low:low + 4]
    elif strategy == "centre1":
        chosen = [by_ordinal[centre_at]]
    elif strategy == "mu1":
        chosen = [by_ordinal[max(0, min(int(round(mean_at)), count - 1))]]
    elif strategy in ("mass3_best_leg", "mass4_best_leg"):
        # The basket is chosen exactly as mass3/mass4 would, and has to clear
        # the whole-basket gate downstream -- but the money goes to one leg.
        # This separates "is this window the right region" from "is spreading
        # across it worth what it costs".
        width = 3 if strategy == "mass3_best_leg" else 4
        seg = contiguous(width, hold_centre=True)
        chosen = None
        if seg:
            by_bucket = {row.bucket: row for row in snapshot.buckets}
            ranked = []
            for bucket in seg:
                row = by_bucket.get(bucket)
                if row is None:
                    continue
                price = _execution_price(row)
                if price is None or _leg_below_floor(
                    ask=(
                        float(row.ask_price) if row.ask_available else None
                    ),
                    bid=getattr(row, "bid_price", 0.0),
                    probability=probabilities.get(bucket),
                    fee_rate=FEE_RATE,
                    ordinal=getattr(row, "ordinal", None),
                    market_centre_ordinal=_market_centre_ordinal(snapshot),
                ):
                    continue
                ranked.append(
                    (float(probabilities.get(bucket, 0.0)) / price, bucket)
                )
            if ranked:
                chosen = [max(ranked)[1]]
    elif strategy == "leg_positive_ev":
        # Every bucket the model prices above the market, contiguity not
        # required.  This is the one shape the live contract forbids, and
        # the reason to measure it here rather than argue about it.
        by_bucket = {row.bucket: row for row in snapshot.buckets}
        chosen = [
            bucket for bucket in by_ordinal
            if bucket in by_bucket
            and by_bucket[bucket].ask_available
            and not _leg_below_floor(
                ask=float(by_bucket[bucket].ask_price),
                bid=getattr(by_bucket[bucket], "bid_price", 0.0),
                probability=probabilities.get(bucket),
                fee_rate=FEE_RATE,
                ordinal=getattr(by_bucket[bucket], "ordinal", None),
                market_centre_ordinal=_market_centre_ordinal(snapshot),
            )
            and float(probabilities.get(bucket, 0.0))
            > (_execution_price(by_bucket[bucket]) or 9.9)
        ]
    else:
        raise ValueError(f"unknown window strategy: {strategy}")
    if not chosen:
        return []
    row = _window_row(
        chosen,
        snapshot=snapshot,
        probabilities=probabilities,
        centre_bucket=center,
    )
    return [] if row is None else [row]




def _ladder_from_quote(
    quote: Mapping[str, Any],
    steps: int = MAKER_RESTING_LADDER_STEPS,
    *,
    floor_fraction: float = 0.0,
    ceiling: float | None = None,
) -> list[float]:
    """The same band as ``_resting_ladder``, from a raw snapshot quote.

    Re-pricing happens against the book as it stood mid-day, where there is
    no compiled snapshot row to read -- only the quote the socket recorded.
    The arithmetic is deliberately the same one, so a re-posted ladder is
    the ladder the opening would have posted at that moment -- with two
    adjustments only a re-post has a reason to make.

    ``floor_fraction`` lifts the cheap end of the band toward the dear end:
    at 0 the band spans the whole spread as it does at the open, at 1 every
    remaining rung sits a tick under the ask.  The misses concentrate on
    the rungs that only fill if the market collapses, and an order that has
    already waited most of its window has run out of time to hope for that.

    ``ceiling`` is the dearest price any rung may ask for -- the model's
    value cap, lambda times the leg's freshest probability, so a book
    trending against the position is never chased past what the leg is
    worth to us.  A band pushed entirely under the current bid by that cap
    still posts there: a deep resting bid is a valid order, it just waits
    in a longer queue.
    """

    levels = [
        level for level in (quote.get("ask_levels") or [])
        if level.get("price") is not None
    ]
    ask = quote.get("best_ask")
    if ask is None and levels:
        ask = levels[0].get("price")
    bid = quote.get("bid_price", quote.get("best_bid"))
    if ask is None or bid is None:
        return []
    ask, bid = float(ask), float(bid)
    tick = float(quote.get("price_tick_size") or 0.001)
    if bid <= 0.0 or ask <= 0.0:
        return []
    if MAKER_RESTING_ANCHORED:
        # Charter 94: a re-post, were one ever made, stands on the anchor.
        #
        # Truncated to the rungs still owed, like every other branch of this
        # function and like production.  The early return skipped the
        # ``steps`` cap below, so a leg whose ladder was one rung deep --
        # charter 94(二)'s degeneration, which is most legs -- was re-posted
        # three rungs wide every five minutes and could buy three times the
        # position it was sized for.  Measured 2026-09-07 on the rebuilt
        # book: 218 of 273 positions bought more shares than they intended,
        # RPLL 2026-09-03 read "3/1" on two legs and staked 14.40 against a
        # 13.08 per-scope cap.  Production never had it -- its re-mint sizes
        # the ladder from the gap (``bid_anchored_rungs_for_gap``) and the
        # sequential gate holds the bucket until the open order resolves; on
        # the live book 0 of 262 authorised legs ever filled past their
        # requested size.  So this was the ledger measuring a strategy
        # production does not run, on every arm fitted since charter 94.
        return sorted(
            bid_anchored_ladder(bid=bid, ask=ask, tick=tick), reverse=True
        )[:max(1, int(steps))]
    capped = ask if ceiling is None else min(ask, float(ceiling))
    high = _floor_to_tick(
        capped if MAKER_RESTING_BAND_TOP == "ask" else capped - tick, tick
    )
    if high < tick / 2:
        return []
    low = min(
        _floor_to_tick(bid + MAKER_RESTING_IMPROVE_TICKS * tick, tick), high
    )
    if floor_fraction > 0.0:
        low = _floor_to_tick(
            low + (high - low) * min(1.0, float(floor_fraction)), tick
        )
    if steps <= 1:
        return [low]
    # Every owed rung is re-posted, at the same price when the band has no
    # room to spread them.  The opening ladder collapses duplicates because
    # each of its rungs is a *distinct* order the sizing splits across; a
    # re-post is the remainder of one leg, and shrinking its rung count to
    # the band's width quietly shrank the order -- a leg owed three rungs
    # into a one-tick spread re-posted one, filled it, and then waited a
    # whole re-pricing round to post the next.  Measured: escalation
    # *lowered* the share fill rate (P1 69.3% -> 51.4%) because the narrowed
    # band throttled every leg to one rung per round.  Same-price rungs are
    # one order at that price for the combined size, which is exactly how
    # the fill test treats them.
    # Floored to the tick like the opening ladder and like production: a
    # re-post is an order, and an order off the grid is not one.  Duplicates
    # survive on purpose (see above) -- that is this function's one
    # deliberate difference from ``_maker_ladder``, and flooring makes
    # duplicates *more* likely, which is the intended semantics.
    span = high - low
    return [
        _floor_to_tick(low + span * index / (steps - 1), tick)
        for index in range(steps)
    ]


def _belief_fresh_until(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    placed_at_utc: str,
) -> str:
    """When the belief this order was posted on stops being served.

    Charter 127 (二): production stamps a GTD's expiry from the served
    belief's own ``fresh_until_utc``, which is the model's run cadence --
    measured on this column, P0 averages 4.44 hours and is capped at 6
    (the run period), P0-M 4.31, and P1 is exactly 60 minutes (the
    observation window).  So "the belief is stale" and "re-decide" are the
    same event by construction, and an order cannot outlive the claim that
    justified it.

    The row is the newest committed one at or before placement -- the one
    serving when the order was signed.  Empty when the bridge cannot
    answer, which the caller reads as "no belief bound", not as zero.
    """

    try:
        row = conn.execute(
            "SELECT fresh_until_utc FROM basket_probability_bridge_history"
            " WHERE station_id=? AND event_date=? AND state='COMMITTED'"
            "   AND decision_at_utc<=? AND fresh_until_utc IS NOT NULL"
            " ORDER BY decision_at_utc DESC LIMIT 1",
            (str(station_id), str(event_date), str(placed_at_utc)),
        ).fetchone()
    except sqlite3.OperationalError:
        return ""
    return "" if row is None else str(row[0] or "")


def _resting_deadline(
    *,
    placed_at_utc: str,
    settlement_timezone: str,
    event_date: str,
    cutoff_local_minutes: int | None = None,
    belief_fresh_until_utc: str = "",
) -> str:
    """When the order gives up.

    Past the hours in which this station's daily maximum actually lands
    (``cutoff_local_minutes``, station-local wall clock on the event day),
    or the day itself when no cutoff is known, or a configured horizon.  A
    temperature market is informationally dead once the afternoon peak has
    passed: what trades after that is the outcome being priced in, and a
    resting ladder is on the wrong side of all of it -- the losing
    buckets' asks collapse down through the escalated rungs, and the
    winner is only available as a chase toward 1.00.
    """

    placed = datetime.fromisoformat(str(placed_at_utc).replace("Z", "+00:00"))
    try:
        day_end = settlement_day_window(
            event_date, settlement_timezone
        )["target_day_end_utc"]
    except Exception:
        day_end = ""
    cutoff = ""
    if cutoff_local_minutes is not None and cutoff_local_minutes > 0:
        try:
            local = datetime.fromisoformat(str(event_date)).replace(
                hour=int(cutoff_local_minutes) // 60,
                minute=int(cutoff_local_minutes) % 60,
                tzinfo=ZoneInfo(str(settlement_timezone or "UTC")),
            )
            cutoff = local.astimezone(timezone.utc).isoformat().replace(
                "+00:00", "Z"
            )
        except Exception:
            cutoff = ""
    horizon = (
        (placed + timedelta(minutes=MAKER_RESTING_HORIZON_MINUTES))
        .isoformat().replace("+00:00", "Z")
        if MAKER_RESTING_HORIZON_MINUTES > 0
        else ""
    )
    # Charter 127 (二), under ``production_v127``: the served belief's own
    # freshness.  First in the list because when it ties with another bound
    # it is the binding rule -- the order was signed with this expiry in
    # it, and the venue would have killed it there whatever else was true.
    # Absent under the ledger lifecycle, so the shipped book is unchanged.
    belief = (
        str(belief_fresh_until_utc or "")
        if RESTING_LIFECYCLE == "production_v127" else ""
    )
    if belief and belief <= str(placed_at_utc):
        # A belief already stale at placement cannot bound an order that
        # does not exist yet; production would not have signed this GTD.
        # The matching engine's own grace is the floor charter 127 (二)
        # names, so the order gets that and no more.
        belief = (
            placed + timedelta(
                seconds=VENUE_GTD_MINIMUM_HORIZON_SECONDS
                + GTD_SIGNING_MARGIN_SECONDS
            )
        ).isoformat().replace("+00:00", "Z")
    named = [
        (bound, name)
        for bound, name in (
            (belief, "信念新鲜期"),
            (cutoff, "达峰截止"),
            (day_end, "当日结束"),
            (horizon, "挂单时长"),
        )
        if bound
    ]
    if not named:
        return (placed + timedelta(days=1)).isoformat().replace("+00:00", "Z")
    # Ties resolve toward the earliest name in the list above, which is the
    # order the rules bind in: the peak cutoff is the real reason an order
    # stops, and reporting "day end" when both land together would send a
    # reader looking at the wrong rule.
    return min(named, key=lambda item: item[0])[0]


def _resting_deadline_reason(
    *,
    placed_at_utc: str,
    settlement_timezone: str,
    event_date: str,
    cutoff_local_minutes: int | None = None,
    belief_fresh_until_utc: str = "",
) -> str:
    """Which bound actually stopped the order, in the page's words.

    The refusal text used to read "挂到结算" whenever the configured horizon
    was unset, which is only one of three bounds -- and never the binding
    one while a peak cutoff exists.  Stations stop a little after their own
    peak (median 19:20 local, earliest 16:53), so an order the page called
    "rested until settlement" had in fact stopped mid-afternoon.
    """

    deadline = _resting_deadline(
        placed_at_utc=placed_at_utc,
        settlement_timezone=settlement_timezone,
        event_date=event_date,
        cutoff_local_minutes=cutoff_local_minutes,
        belief_fresh_until_utc=belief_fresh_until_utc,
    )
    # Asked before the cutoff, in the order the bounds bind: an order the
    # belief killed did not rest to the peak, whatever the clock says.
    if (
        RESTING_LIFECYCLE == "production_v127"
        and belief_fresh_until_utc
        and str(deadline) == str(belief_fresh_until_utc)
    ):
        return "挂到信念新鲜期结束 %s" % str(belief_fresh_until_utc)[11:16]
    if cutoff_local_minutes is not None:
        try:
            local = datetime.fromisoformat(
                deadline.replace("Z", "+00:00")
            ).astimezone(ZoneInfo(settlement_timezone))
            if (
                local.hour * 60 + local.minute
                == int(cutoff_local_minutes) % (24 * 60)
            ):
                return "挂到达峰截止 %02d:%02d 本地" % (
                    int(cutoff_local_minutes) // 60,
                    int(cutoff_local_minutes) % 60,
                )
        except Exception:
            pass
    if MAKER_RESTING_HORIZON_MINUTES > 0:
        return "%.0f 分钟" % MAKER_RESTING_HORIZON_MINUTES
    return "挂到当日结束"


# Where the peak-hour statistics read intraday temperatures from.  The
# trajectory lane's METAR store covers all 45 stations at minute grain;
# the ledger only reads it, once per pass.
OBSERVATION_DB = Path(
    os.environ.get("PAPER_LEDGER_OBSERVATION_DB", "")
    or (ROOT / "data" / "archive.db")
)


def _station_peak_cutoffs(
    timezones: Mapping[str, str]
) -> dict[str, int]:
    """Per station, the local minute-of-day the resting orders die at.

    The P-th percentile of the local time at which the running daily
    maximum was *last* touched, over the trajectory lane's observed days,
    plus the configured buffer.  Last touched, not first: a plateau that
    holds the maximum into the evening keeps the market honest that long.
    Days whose observations do not span the daytime are skipped -- a
    truncated day reports the peak of what happened to be recorded, not
    of the day.  Stations with too few days take the pooled percentile.
    """

    if MAKER_RESTING_CUTOFF_PERCENTILE <= 0:
        return {}
    try:
        observations = sqlite3.connect(
            f"file:{OBSERVATION_DB}?mode=ro", uri=True, timeout=30.0
        )
        rows = observations.execute(
            "SELECT station_id, valid_at, reported_temperature_c"
            " FROM reported_trajectory_observations"
            " WHERE reported_temperature_c IS NOT NULL"
        ).fetchall()
        observations.close()
    except sqlite3.Error:
        return {}
    zones: dict[str, ZoneInfo] = {}
    by_day: dict[tuple[str, str], list[tuple[int, float]]] = {}
    for station, valid_at, temperature in rows:
        station = str(station)
        zone = zones.get(station)
        if zone is None:
            try:
                zone = ZoneInfo(str(timezones.get(station) or "UTC"))
            except Exception:
                zone = ZoneInfo("UTC")
            zones[station] = zone
        try:
            local = datetime.fromisoformat(
                str(valid_at).replace("Z", "+00:00")
            ).astimezone(zone)
        except ValueError:
            continue
        by_day.setdefault(
            (station, local.date().isoformat()), []
        ).append((local.hour * 60 + local.minute, float(temperature)))
    peaks: dict[str, list[int]] = {}
    pooled: list[int] = []
    for (station, _day), samples in by_day.items():
        first = min(minute for minute, _t in samples)
        last = max(minute for minute, _t in samples)
        if first > 9 * 60 or last < 19 * 60:
            continue
        top = max(t for _m, t in samples)
        peak = max(minute for minute, t in samples if t >= top - 0.05)
        peaks.setdefault(station, []).append(peak)
        pooled.append(peak)

    def percentile(values: list[int]) -> int:
        ordered = sorted(values)
        index = int(
            (MAKER_RESTING_CUTOFF_PERCENTILE / 100.0) * len(ordered) + 0.999
        ) - 1
        return ordered[max(0, min(len(ordered) - 1, index))]

    fallback = percentile(pooled) if pooled else None
    out: dict[str, int] = {}
    for station in timezones:
        values = peaks.get(str(station), [])
        base = (
            percentile(values)
            if len(values) >= MAKER_RESTING_CUTOFF_MIN_DAYS
            else fallback
        )
        if base is None:
            continue
        out[str(station)] = int(min(
            base + MAKER_RESTING_CUTOFF_BUFFER_MINUTES, 24 * 60 - 1
        ))
    return out



# Production applies the cooling-side ceiling inside the P1 compile as of
# 2026-08-23, so a replay that reads the historical posteriors raw is scoring a
# strategy the live engine no longer runs.  The serving artifact cannot supply
# it here -- it refuses every date at or before its own training cutoff, and a
# replay is entirely made of those -- so the replay table is used instead: one
# curve per event date, fitted only on strictly earlier days.


_TZ_CACHE: dict[tuple[str, str], str] = {}


def _settlement_timezone(conn, station_id: str, event_date: str) -> str:
    """The zone the day settles in, as the book itself recorded it."""

    key = (str(station_id), str(event_date))
    if key not in _TZ_CACHE:
        row = conn.execute(
            "SELECT settlement_timezone FROM basket_official_ws_snapshot"
            " WHERE station_id=? AND event_date=? LIMIT 1", key,
        ).fetchone()
        _TZ_CACHE[key] = str(row[0]) if row and row[0] else ""
    return _TZ_CACHE[key]


# The centre/width layer as production would apply it, for replay only
# (A/B knob, 2026-08-23).  That layer has never once served -- its evidence
# window resets on every serving-arithmetic change and has never reached the
# eight days it needs -- so the question "what would it be worth" has no
# production answer to read.  The scalars come from a per-event-date table
# fitted only on earlier days, the same discipline the ceiling replay uses.


def _center_width_corrected(
    probabilities: dict[str, float],
    *,
    stage: str,
    envelope_json: Any,
    event_date: str,
) -> dict[str, float]:
    """The belief with this stage's shift and width scale applied."""

    global _CW_REPLAY
    center_width_on = (
        PAPER_LEDGER_CENTER_WIDTH
        and str(stage) in PAPER_LEDGER_CENTER_WIDTH_STAGES
    )
    if not probabilities or not center_width_on:
        return probabilities
    if _CW_REPLAY is None:
        # An arm points this at its own table rather than swapping the one
        # production's own refit writes.  Swapping that file leaves a restore
        # step for someone to forget, and the file is read by more than this.
        override = os.environ.get(
            "PAPER_LEDGER_CW_REPLAY_TABLE", ""
        ).strip()
        path = (
            Path(override) if override else
            Path(__file__).resolve().parents[1] / "data" / "runtime"
            / "bayes_basket_target_nwp" / "center_width_replay_table.json"
        )
        try:
            _CW_REPLAY = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _CW_REPLAY = {"stages": {}}
    entry = (
        (_CW_REPLAY.get("stages") or {}).get(str(stage)) or {}
    ).get(str(event_date))
    if not entry:
        return probabilities
    forced = os.environ.get("PAPER_LEDGER_CW_FORCE_LAMBDA", "").strip()
    if forced:
        # Test the mechanism rather than the fitted scalars: if sharpening
        # loses because it walks the window toward the consensus price, then
        # widening should walk it away and the sign should flip.  The shift
        # is dropped with it so only the width is under test.
        entry = {"delta_c": 0.0, "width_lambda": float(forced)}
    try:
        envelope = (
            envelope_json if isinstance(envelope_json, dict)
            else json.loads(envelope_json or "{}")
        )
        lineage = envelope.get("lineage") or {}
        intervals = json.loads(
            str(lineage.get("bucket_intervals_c_json") or "{}")
        )
        ordinals = envelope.get("bucket_ordinals") or {}
    except (TypeError, ValueError):
        return probabilities
    if not intervals or not ordinals:
        return probabilities
    keys = sorted(probabilities, key=lambda k: ordinals.get(k, 0))
    if set(keys) != set(intervals):
        return probabilities
    try:
        centers = bucket_centers_from_intervals(
            intervals, ordered_keys=keys, edge_width_c=EDGE_WIDTH_C
        )
    except (ValueError, KeyError):
        return probabilities
    excluded = str(lineage.get("hard_excluded_buckets") or "NONE")
    corrected, _ = apply_center_width_calibration(
        probabilities,
        delta_c=float(entry["delta_c"]),
        width_lambda=float(entry["width_lambda"]),
        # The floor the day's scalars were fitted under, carried on the same
        # replay entry.  Production serves the pair and the floor together,
        # so the ledger applies them together or it is pricing a rule
        # production does not run.
        sigma_floor_rungs=float(entry.get("sigma_floor_rungs") or 0.0),
        # Width tiers (2026-09-05), carried on the same entry when the
        # replay table records them; absent means one lambda, as served.
        width_tier_lambdas=(
            (entry.get("width_tiers") or {}).get("lambdas") or None
        ),
        width_tier_boundaries=(
            (entry.get("width_tiers") or {}).get("boundaries_rungs") or None
        ),
        ordered_keys=keys,
        key_centers_c=centers,
        excluded_buckets=() if excluded == "NONE" else tuple(excluded.split(",")),
    )
    return corrected


# Formal reports per station-day, for the ceiling's gap axis (v2, 2026-09-06):
# the same archive series production's P1 chain consumes, read once per
# station-day and kept for the pass.
_FORMAL_OBSERVATION_POINTS: dict[tuple[str, str], list[tuple[str, float]]] = {}


def _formal_observation_points(
    station_id: str, event_date: str, zone_name: str
) -> list[tuple[str, float]]:
    """(valid_at, temperature) of the day's formal METAR/SPECI reports."""

    key = (str(station_id), str(event_date))
    cached = _FORMAL_OBSERVATION_POINTS.get(key)
    if cached is not None:
        return cached
    points: list[tuple[str, float]] = []
    try:
        start = datetime.fromisoformat(str(event_date)).replace(
            tzinfo=ZoneInfo(str(zone_name))
        )
        end = start + timedelta(days=1)
        stamp = "%Y-%m-%dT%H:%M:%S.000Z"
        observations = sqlite3.connect(
            f"file:{OBSERVATION_DB}?mode=ro", uri=True, timeout=30.0
        )
        rows = observations.execute(
            "SELECT valid_at, reported_temperature_c"
            " FROM reported_trajectory_observations"
            " WHERE station_id=? AND reported_temperature_c IS NOT NULL"
            " AND report_type IN ('METAR','SPECI')"
            " AND valid_at>=? AND valid_at<? ORDER BY valid_at",
            (
                str(station_id),
                start.astimezone(timezone.utc).strftime(stamp),
                end.astimezone(timezone.utc).strftime(stamp),
            ),
        ).fetchall()
        observations.close()
        points = [(str(valid_at), float(temperature)) for valid_at, temperature in rows]
    except (sqlite3.Error, ValueError, KeyError, TypeError):
        points = []
    _FORMAL_OBSERVATION_POINTS[key] = points
    return points


def _ceiling_corrected(
    conn,
    probabilities: dict[str, float],
    *,
    stage: str,
    envelope_json: Any,
    station_id: str,
    event_date: str,
    as_of_utc: str,
) -> dict[str, float]:
    """The belief production would have held, ceiling included.

    Only P1 carries a running high, so only P1 can be corrected; P0 and P0-M
    are returned untouched, which is also what production does with them.
    """

    if not PAPER_LEDGER_CEILING or stage != "P1" or not probabilities:
        return probabilities
    try:
        envelope = (
            envelope_json if isinstance(envelope_json, dict)
            else json.loads(envelope_json or "{}")
        )
    except (TypeError, ValueError):
        return probabilities
    lineage = envelope.get("lineage") or {}
    running = lineage.get("metar_trajectory_running_high_c")
    ordinals = envelope.get("bucket_ordinals") or {}
    intervals_raw = lineage.get("bucket_intervals_c_json")
    if running in (None, "") or not ordinals or not intervals_raw:
        return probabilities
    try:
        intervals = json.loads(intervals_raw)
        running_c = float(running)
    except (TypeError, ValueError):
        return probabilities
    running_bucket = None
    for bucket, bounds in intervals.items():
        low, high = bounds[0], bounds[1]
        if (low is None or running_c >= float(low)) and (
            high is None or running_c < float(high)
        ):
            running_bucket = bucket
            break
    if running_bucket is None:
        return probabilities
    curve = metar_ceiling.replay_curve(event_date)
    if curve is None:
        return probabilities
    zone = _settlement_timezone(conn, station_id, event_date)
    if not zone:
        return probabilities
    # Charter 99: the regional curve answers both halves of the ceiling's
    # question from today's run, and stands in its place when it speaks.
    # The replay reads the as-of table, fitted only on earlier days.
    regional = regional_path_prior.build_serving_input(
        conn, station_id=station_id, event_date=event_date,
        as_of_utc=as_of_utc, settlement_timezone=zone,
        artifact=regional_path_prior.replay_curve(event_date),
        # Charter 95: the same as-of discipline as the residual table --
        # the offsets a morning could have known, not today's.
        debias_artifact=regional_point_debias.replay_curve(event_date),
    ) if regional_path_prior.ENABLED else None
    if regional is not None:
        upper_bounds = {
            bucket: (None if bounds[1] is None else float(bounds[1]))
            for bucket, bounds in intervals.items()
        }
        blended, note = regional_path_prior.apply_regional_path_prior(
            probabilities, ordinals=ordinals, upper_bounds_c=upper_bounds,
            running_high_bucket=running_bucket, running_high_c=running_c,
            remaining_peak_c=float(regional["remaining_peak_c"]),
            cell=regional["cell"], lineage=regional.get("lineage"),
        )
        if note.get("regional_path_prior_applied") == "true":
            return blended
    # v2: the newest formal report at this decision clock, against the
    # running high -- the same pairing production computes from its prefix.
    latest_formal = metar_ceiling.latest_temperature_c(
        _formal_observation_points(station_id, event_date, zone),
        as_of_utc=as_of_utc,
    )
    corrected, _ = metar_ceiling.apply_ceiling(
        probabilities,
        ordinals=ordinals,
        running_high_bucket=running_bucket,
        event_date=event_date,
        settlement_timezone=zone,
        as_of_utc=as_of_utc,
        artifact=curve,
        enabled=True,
        current_gap_c=metar_ceiling.current_gap_c(running_c, latest_formal),
    )
    return corrected


class _BeliefTimeline:
    """Our own freshest probabilities for one station-day, as of a moment.

    The withdrawal rules are anchored to what *we* believe, not to what
    the market does (user ruling, 2026-08-20): the bridge history holds
    every stage's compiled distribution with its decision time, so the
    walk can re-read the belief that was in force at each re-pricing round
    without a shred of hindsight.  "Freshest" is by observation stage
    first and clock second: once a P1 row exists, later P0/P0-M rows are
    forecasts that never saw the day's reported high and do not update
    the belief.  Buckets absent from the freshest distribution keep their
    last known probability -- a narrower P1 ladder is a re-focus, not a
    verdict of zero on the shoulders it dropped.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        station_id: str,
        event_date: str,
        placed_at_utc: str,
        entry_probabilities: Mapping[str, float],
    ) -> None:
        self._conn = conn
        self._station_id = str(station_id)
        self._event_date = str(event_date)
        try:
            self._rows = conn.execute(
                "SELECT decision_at_utc, history_id, stage, envelope_json,"
                " checkpoint_id, envelope_content_sha256"
                " FROM basket_probability_bridge_history"
                " WHERE station_id=? AND event_date=? AND state='COMMITTED'"
                "   AND decision_at_utc>?"
                " ORDER BY decision_at_utc ASC",
                (station_id, event_date, str(placed_at_utc)),
            ).fetchall()
        except sqlite3.OperationalError:
            # A database without the bridge (tests, foreign replays) keeps
            # the entry belief for the whole window.
            self._rows = []
        self._index = 0
        self._p1_seen = False
        self._current = {
            str(bucket): float(p)
            for bucket, p in entry_probabilities.items()
        }

    def at(self, moment: datetime) -> dict[str, float]:
        stamp = moment.isoformat().replace("+00:00", "Z")
        newest = None
        while self._index < len(self._rows) and (
            str(self._rows[self._index][0]) <= stamp
        ):
            # Chronologically-newest is not epistemically-freshest: P0
            # recomputes all day by charter 46 but its chain carries no
            # observation, so a P0 row written after the first P1 row is a
            # forecast overriding a measurement.  Measured on the settled
            # book, 112/135 station-days ended on such a row, holding 46.9%
            # of mass on buckets the day's reported high had already
            # excluded (the same days' last P1 rows held 3.5%).  Once a P1
            # row exists, later non-P1 rows no longer update the belief.
            row_stage = str(self._rows[self._index][2] or "")
            if row_stage == "P1":
                newest = self._rows[self._index]
                self._p1_seen = True
            elif not self._p1_seen:
                newest = self._rows[self._index]
            self._index += 1
        if newest is not None:
            from bayes_basket_target.probability_bridge import (  # noqa: PLC0415
                history_envelope_payload,
            )
            rows = self._conn.execute(
                "SELECT bucket, probability"
                " FROM basket_probability_bridge_bucket_probability"
                " WHERE history_id=?",
                (newest[1],),
            ).fetchall()
            envelope = history_envelope_payload(
                self._conn,
                checkpoint_id=str(newest[4]),
                envelope_content_sha256=str(newest[5]),
                stored=str(newest[3]) if newest[3] else None,
            )
            fresh = {str(bucket): float(p) for bucket, p in rows}
            fresh = _ceiling_corrected(
                self._conn,
                fresh,
                stage=str(newest[2] or ""),
                envelope_json=envelope,
                station_id=self._station_id,
                event_date=self._event_date,
                as_of_utc=str(newest[0]),
            )
            # Geometry before claim, exactly as production composes it.
            fresh = _center_width_corrected(
                fresh,
                stage=str(newest[2] or ""),
                envelope_json=envelope,
                event_date=self._event_date,
            )
            self._current.update(fresh)
        return self._current



def _queue_worked_off(
    state: dict[str, dict[str, float]],
    bucket: str,
    quote: Mapping[str, Any],
    *,
    require_queue: bool,
) -> bool:
    """Has the size that stood in front of us at this price been worked off?

    Under ``through`` the answer is always yes: the caller has already
    required the book to trade past our level and nothing was claimed about
    the queue.  Under ``queue`` the shares resting ahead when the rung was
    posted have to be consumed first, and consumption is inferred from the
    level's depth shrinking between snapshots -- a cancel is indistinguishable
    from a fill in a snapshot, so this still credits us too fast rather than
    too slow.
    """

    if not require_queue:
        return True
    cell = state.setdefault(str(bucket), {})
    if "remaining" not in cell:
        ahead = quote.get("maker_queue_ahead_shares")
        known = bool(quote.get("maker_queue_known"))
        # An unknown queue is treated as empty.  Inventing a number would be
        # a worse error than the optimism it is meant to correct.
        cell["remaining"] = float(ahead or 0.0) if known else 0.0
    depth = 0.0
    for level in (quote.get("ask_levels") or []):
        shares = level.get("shares")
        if shares is not None:
            depth += float(shares)
    previous = cell.get("depth")
    cell["depth"] = depth
    if previous is not None and depth < previous:
        cell["remaining"] = max(0.0, cell["remaining"] - (previous - depth))
    return cell["remaining"] <= 0.0


def adverse_fill_predicate(
    *,
    station_id: str,
    event_date: str,
    settled_bucket: str | None,
    p_win: float = ADVERSE_FILL_P_WIN,
    p_lose: float = ADVERSE_FILL_P_LOSE,
) -> Callable[[str, float], bool]:
    """Will the other side sell us this rung?  Built here, answered inside.

    The firewall is the shape of this function, not a rule about using it.
    Modelling an informed counterparty REQUIRES the simulator to know the
    outcome -- adverse selection is the statement that the fill probability
    depends on it -- and a strategy that knows the outcome is worthless.
    So the settled bucket is closed over HERE, and what the walk receives is
    a function of ``(bucket, rung)``.  There is no truth in the walk's scope
    to leak into a price, a claim, a cap or a seat, and no discipline needed
    to keep it out.

    Deterministic: the draw is a hash of the leg and its price, so a replay
    is byte-identical and a rung refused at one price stays refused at that
    price for the order's whole life.  A reprice is a new price and so a new
    question, which is the right reading -- a new order is a new ask of the
    market.

    ``settled_bucket`` of ``None`` -- an open day, or a value outside the
    ladder -- admits every crossing, which is the unconditioned model.  Such
    a day has no settlement and therefore enters no return figure.
    """

    def admits(bucket: str, rung: float) -> bool:
        if settled_bucket is None:
            return True
        threshold = p_win if str(bucket) == str(settled_bucket) else p_lose
        if threshold >= 1.0:
            return True
        if threshold <= 0.0:
            return False
        digest = hashlib.sha256(
            "|".join((
                "adverse_fill_v1", str(station_id), str(event_date),
                str(bucket), "%.6f" % float(rung),
            )).encode("utf-8")
        ).digest()
        # The first eight bytes as a uniform draw on [0, 1).
        draw = int.from_bytes(digest[:8], "big") / float(1 << 64)
        return draw < threshold

    return admits


def _resting_ladder_fill(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    buckets: Sequence[str],
    ladders: Sequence[Sequence[float]],
    fill_admits: Callable[[str, float], bool] | None = None,
    placed_at_utc: str,
    settlement_timezone: str = "UTC",
    probabilities: Mapping[str, float] | None = None,
    cutoff_local_minutes: int | None = None,
    centre_bucket: str | None = None,
    market_top_bucket: str | None = None,
) -> tuple[dict[str, list[float]], str | None, bool]:
    """Which rungs of each leg's ladder the book traded through.

    A leg counts as filled when any rung fills.  Returns the filled prices
    per bucket so the cost is the ladder's own average rather than an
    assumed one, and whether the belief gate cancelled the order -- an
    order we withdrew and one the market never came down to both end with
    nothing bought, and they mean opposite things.

    What the order does while it waits, in the order the rules bind:

    - It lives until the station's own peak-heat cutoff
      (``cutoff_local_minutes``), not until settlement -- past the peak
      the market is pricing an answer, not a question.
    - Every ``MAKER_RESTING_REPRICE_MINUTES`` it re-reads our freshest
      probabilities and re-posts whatever is still open.  A belief that
      collapsed pulls the value cap down and the rungs above it are
      withdrawn; if the whole order's claimed expectation under the fresh
      belief has gone negative, every open rung is withdrawn for good.
      Market moves trigger nothing (user ruling, 2026-08-20).
    - A re-post stands on the current book, floor lifted by the *larger*
      of elapsed time and value headroom: a leg whose value exceeds the
      band top has no reason to fish the bottom and posts at the top from
      its first round.
    - Rungs already bought are never re-posted, and a re-post replenishes
      the ladder back to its posted depth -- a band that collapsed to one
      rung mid-day and bought it has not finished the leg.
    """

    # Charter 127 (二): the order carries the served belief's own expiry.
    # Read once, here, and handed to both the bound and the name for it --
    # the walk and the page must not disagree about when the order died.
    belief_fresh_until = _belief_fresh_until(
        conn,
        station_id=station_id,
        event_date=event_date,
        placed_at_utc=placed_at_utc,
    )
    deadline = _resting_deadline(
        placed_at_utc=placed_at_utc,
        settlement_timezone=settlement_timezone,
        event_date=event_date,
        cutoff_local_minutes=cutoff_local_minutes,
        belief_fresh_until_utc=belief_fresh_until,
    )
    placed = datetime.fromisoformat(str(placed_at_utc).replace("Z", "+00:00"))
    try:
        deadline_at = datetime.fromisoformat(
            str(deadline).replace("Z", "+00:00")
        )
    except ValueError:
        deadline_at = placed + timedelta(days=1)
    escalate_minutes = 0.0
    if MAKER_RESTING_ESCALATE_SHARE > 0:
        escalate_minutes = max(
            0.0,
            (deadline_at - placed).total_seconds() / 60.0
            * MAKER_RESTING_ESCALATE_SHARE,
        )
    entry_p = {
        str(bucket): float(p)
        for bucket, p in (probabilities or {}).items()
    }
    belief = _BeliefTimeline(
        conn,
        station_id=station_id,
        event_date=event_date,
        placed_at_utc=placed_at_utc,
        entry_probabilities=entry_p,
    ) if entry_p else None
    current_p = dict(entry_p)
    pending = {
        str(bucket): sorted(prices, reverse=True)
        for bucket, prices in zip(buckets, ladders)
    }
    # How many rungs each leg posted, so a re-post keeps the ladder the same
    # depth rather than growing it every round.
    posted_depth = {
        bucket: len(prices) for bucket, prices in pending.items()
    }
    filled: dict[str, list[float]] = {b: [] for b in pending}
    queue_state: dict[str, dict[str, float]] = {}
    # The confirmation arm's baseline: the best ask each leg first re-priced
    # against.  Deliberately the first re-pricing round's book rather than
    # the placement snapshot -- one baseline per leg, recorded where it is
    # compared, and a rise measured from an hour in is a conservative read
    # of "the market moved toward us".
    first_ask: dict[str, float] = {}
    withdrawn = False
    next_reprice = (
        None if MAKER_RESTING_REPRICE_MINUTES <= 0
        else placed + timedelta(minutes=MAKER_RESTING_REPRICE_MINUTES)
    )
    for observed, payload in conn.execute(
        "SELECT observed_at, buckets_json FROM basket_official_ws_snapshot"
        " WHERE station_id=? AND event_date=? AND state='COMMITTED'"
        "   AND observed_at > ? AND observed_at <= ?"
        " ORDER BY observed_at",
        (station_id, event_date, placed_at_utc, deadline),
    ):
        if withdrawn or all(
            len(filled[bucket]) >= posted_depth.get(bucket, 0)
            for bucket in filled
        ):
            break
        quotes = _decode_quotes(payload)
        # Re-post first, then see what the same tick trades through: an
        # order moved to a new price is live at that price from the moment
        # it is moved, and checking the old band one last time would credit
        # a fill to a price that had already been cancelled.
        if next_reprice is not None:
            at = datetime.fromisoformat(str(observed).replace("Z", "+00:00"))
            while at >= next_reprice and not withdrawn:
                if belief is not None:
                    current_p = belief.at(next_reprice)
                # The whole-order belief gate: rungs bought plus rungs
                # still posted, at their own prices, against the fresh
                # claim.  Negative means our own evidence has turned
                # against this basket, and the one thing this lane must
                # never do is keep buying it.  Withdrawal is final for
                # this order -- production re-decides at the next stage.
                if MAKER_RESTING_BELIEF_GATE and belief is not None:
                    expectation = 0.0
                    for bucket, ladder in zip(buckets, ladders):
                        depth_n = len(ladder)
                        if not depth_n:
                            continue
                        bought = filled.get(str(bucket)) or []
                        open_rungs = pending.get(str(bucket)) or []
                        claim = current_p.get(str(bucket), 0.0)
                        if "w4" in MAKER_RESTING_PPOST_COMPONENTS:
                            # W4: the gate weighs each leg's claim by what
                            # the market has already done with it -- eager
                            # fills discount, standing rejections appreciate.
                            claim = claim * _ppost_multiplier(
                                claim, bool(bought)
                            )
                        expectation += (
                            claim * (len(bought) + len(open_rungs))
                            - sum(bought) - sum(open_rungs)
                        ) / depth_n
                    if expectation < 0.0:
                        for bucket in pending:
                            pending[bucket] = []
                        withdrawn = True
                        break
                # The tier contract's one number: the claimed probability
                # still sitting on open rungs.  This is the uncovered truth
                # mass the basket could still buy down -- the out-of-window
                # mass was a selection decision, not a fill decision.
                coverage_tiering = MAKER_RESTING_COVERAGE_BUDGET >= 0.0
                coverage_chase = False
                if coverage_tiering:
                    uncovered_mass = 0.0
                    for bucket in pending:
                        depth_posted = posted_depth.get(bucket, 0)
                        open_n = depth_posted - len(filled[bucket])
                        if depth_posted <= 0 or open_n <= 0:
                            continue
                        uncovered_mass += (
                            float(current_p.get(str(bucket), 0.0) or 0.0)
                            * open_n / depth_posted
                        )
                    coverage_chase = (
                        uncovered_mass > MAKER_RESTING_COVERAGE_BUDGET
                    )
                floor_time = 0.0
                if escalate_minutes > 0:
                    elapsed = (
                        next_reprice - placed
                    ).total_seconds() / 60.0
                    floor_time = min(1.0, elapsed / escalate_minutes)
                # Where the order stands in its own window, 0..1.  The
                # completion pressure is phased on this rather than on the
                # escalate schedule: the schedule answers "how fast do
                # rungs drift up anyway", the phase answers "when does the
                # unfilled fraction start to matter, and when may it cross".
                window_minutes = max(
                    (deadline_at - placed).total_seconds() / 60.0, 1e-9
                )
                progress = min(
                    1.0,
                    (next_reprice - placed).total_seconds()
                    / 60.0 / window_minutes,
                )
                for bucket in pending:
                    open_rungs = posted_depth.get(bucket, 0) - len(
                        filled[bucket]
                    )
                    if open_rungs <= 0:
                        # Fully bought.  Re-posting the rungs it already
                        # owns would buy them twice.
                        continue
                    gate_item = quotes.get(bucket)
                    gate_ask = None
                    if isinstance(gate_item, Mapping):
                        gate_ask = gate_item.get("best_ask")
                        if gate_ask is None:
                            gate_levels = gate_item.get("ask_levels") or []
                            if gate_levels:
                                gate_ask = gate_levels[0].get("price")
                    cap = _leg_value_cap(
                        bucket,
                        probabilities=current_p,
                        centre_bucket=centre_bucket,
                        window=buckets,
                        market_top_bucket=market_top_bucket,
                        all_in_ask=(
                            float(gate_ask) if gate_ask is not None else None
                        ),
                    )
                    # Gate 1, the red line: what this leg is worth.  The
                    # clamp for a hedge leg, the fresh claim itself for the
                    # exempt core -- pressure below chooses where to stand
                    # inside it and may take the ask at it, never above it.
                    ceiling = (
                        cap
                        if cap is not None
                        else current_p.get(str(bucket))
                    )
                    # Coverage premium (user ruling, 2026-08-24, corrected):
                    # every leg keeps the full lifecycle inside its own
                    # ceiling -- the tail is the hedge leg and fills at its
                    # clamp like everyone else.  What the risk budget buys
                    # is the premium alone: while the uncovered claim mass
                    # exceeds it, a core or shoulder leg's crossing ceiling
                    # relaxes toward COVERAGE_LAMBDA times the live claim
                    # (never above the claim); a tail's never does.
                    cross_ceiling = ceiling
                    if (
                        coverage_tiering
                        and coverage_chase
                        and MAKER_RESTING_COVERAGE_LAMBDA > 0.0
                        and cross_ceiling is not None
                    ):
                        leg_tier = _coverage_tier(
                            bucket,
                            window=buckets,
                            probabilities=current_p,
                        )
                        claim_now = current_p.get(str(bucket))
                        if leg_tier != "tail" and claim_now is not None:
                            cross_ceiling = max(
                                float(cross_ceiling),
                                MAKER_RESTING_COVERAGE_LAMBDA
                                * float(claim_now),
                            )
                    item = quotes.get(bucket)
                    if not isinstance(item, Mapping):
                        # No readable book this round.  The standing rungs
                        # still answer to the fresh belief: the ones above
                        # the value cap are withdrawn, the rest wait.
                        if cap is not None:
                            pending[bucket] = [
                                rung for rung in pending[bucket]
                                if rung <= cap + 1e-9
                            ]
                        continue
                    # Gate 3, late phase: completion beats the spread.  A
                    # leg still open past GAP_LATE takes the ask -- the
                    # whole remaining saving is the sacrifice the redesign
                    # budgets for -- but only when the ask sits inside the
                    # leg's value ceiling.  Above the ceiling the red line
                    # holds and the leg stays a resting order.
                    #
                    # The confirmation arm moves only the clock: from
                    # GAP_MID, an ask that has climbed the configured ticks
                    # above this leg's first re-priced book takes early,
                    # through the identical ceiling and floor checks below.
                    round_ask = item.get("best_ask")
                    if round_ask is None:
                        round_levels = item.get("ask_levels") or []
                        if round_levels:
                            round_ask = round_levels[0].get("price")
                    if round_ask is not None:
                        first_ask.setdefault(bucket, float(round_ask))
                    confirm_take = False
                    if (
                        MAKER_RESTING_CONFIRM_TAKE_TICKS > 0.0
                        and progress >= MAKER_RESTING_GAP_MID
                        and round_ask is not None
                        and bucket in first_ask
                    ):
                        confirm_take = (
                            float(round_ask)
                            >= first_ask[bucket]
                            + MAKER_RESTING_CONFIRM_TAKE_TICKS
                            * float(item.get("price_tick_size") or 0.001)
                        )
                    if (
                        (
                            progress >= MAKER_RESTING_GAP_LATE
                            or confirm_take
                        )
                        and cross_ceiling is not None
                    ):
                        if (
                            "w2" in MAKER_RESTING_PPOST_COMPONENTS
                            and not filled[bucket]
                        ):
                            # W2: a leg the market has refused to fill all
                            # this time carries the unfilled appreciation --
                            # its crossing ceiling rises with it, which is
                            # what lets the standing winning shoulder take
                            # an ask its raw claim called too dear.
                            cross_ceiling = (
                                float(cross_ceiling) * PPOST_UNFILLED_MULT
                            )
                        cross_ask = item.get("best_ask")
                        cross_levels = item.get("ask_levels") or []
                        if cross_ask is None and cross_levels:
                            cross_ask = cross_levels[0].get("price")
                        # The same floor selection applies to every quote:
                        # an ask under two cents is the one-sided tail
                        # offer this book repeatedly ruled is not a price.
                        # Crossing it "inside value" is how the first run
                        # of this redesign manufactured +260% ROI out of
                        # tenth-of-a-cent lottery tickets on collapsed
                        # buckets; a resting rung may still be traded
                        # through down there, but we do not reach for it.
                        if (
                            cross_ask is not None
                            and float(cross_ask) <= float(cross_ceiling) + 1e-9
                            and float(cross_ask) >= MINIMUM_LEG_ALL_IN_ASK
                            # W3: under the arm, a sub-nickel ask realises
                            # nothing regardless of claim -- never cross it.
                            and (
                                "w3" not in MAKER_RESTING_PPOST_COMPONENTS
                                or float(cross_ask) >= PPOST_LOW_ASK_VETO
                            )
                        ):
                            filled[bucket].extend(
                                [float(cross_ask)] * open_rungs
                            )
                            pending[bucket] = []
                            continue
                    # Value headroom as an escalation floor of its own: a
                    # leg worth more than the band's top has no reason to
                    # fish the bottom (the model calls the whole band
                    # cheap), while a leg worth less than the band's mean
                    # keeps the time schedule only up to its cap.
                    floor_edge = 0.0
                    if cap is not None:
                        ask = item.get("best_ask")
                        levels = item.get("ask_levels") or []
                        if ask is None and levels:
                            ask = levels[0].get("price")
                        bid = item.get("bid_price", item.get("best_bid"))
                        tick = float(item.get("price_tick_size") or 0.001)
                        if ask is not None and bid is not None:
                            high = min(float(ask), cap) - tick
                            low = min(
                                float(bid)
                                + MAKER_RESTING_IMPROVE_TICKS * tick,
                                high,
                            )
                            mean = (low + high) / 2.0
                            if high > mean + 1e-9:
                                floor_edge = max(
                                    0.0,
                                    min(1.0, (cap - mean) / (high - mean)),
                                )
                    # Gate 3, mid phase on: the escalation floor also rises
                    # with this leg's own unfilled fraction.  A filled leg
                    # feels no pressure; a wholly-open leg past GAP_MID is
                    # pushed toward the band top as its window runs out --
                    # the fill result does not change the value, it changes
                    # how the remaining order executes.
                    floor_gap = 0.0
                    if progress >= MAKER_RESTING_GAP_MID:
                        leg_gap = open_rungs / max(
                            posted_depth.get(bucket, open_rungs), 1
                        )
                        ramp = min(1.0, (progress - MAKER_RESTING_GAP_MID) / (
                            MAKER_RESTING_GAP_LATE - MAKER_RESTING_GAP_MID
                        ))
                        floor_gap = leg_gap * ramp
                    if "w1" in MAKER_RESTING_PPOST_COMPONENTS:
                        # W1: replenishment stops where the fill information
                        # says more buying realises little -- a high-claim
                        # leg the market has already sold into (x0.32), or
                        # any leg asking under a nickel (x0.06 measured,
                        # x0.00 on the verification half).  Standing rungs
                        # stay within the cap; no new ones are derived.
                        ppost_claim = float(
                            current_p.get(str(bucket)) or 0.0
                        )
                        ppost_ask = item.get("best_ask")
                        if ppost_ask is None:
                            ppost_ask_levels = item.get("ask_levels") or []
                            if ppost_ask_levels:
                                ppost_ask = ppost_ask_levels[0].get("price")
                        if (
                            (
                                filled[bucket]
                                and ppost_claim
                                >= PPOST_HIGH_CLAIM_THRESHOLD
                            )
                            or (
                                ppost_ask is not None
                                and float(ppost_ask) < PPOST_LOW_ASK_VETO
                            )
                        ):
                            if cap is not None:
                                pending[bucket] = [
                                    rung for rung in pending[bucket]
                                    if rung <= cap + 1e-9
                                ]
                            continue
                    if MAKER_RESTING_ANCHORED:
                        # Charter 110 (三): the re-post is asymmetric, and the
                        # decision is the kernel's so this lane and production
                        # cannot answer the same book two ways.
                        #
                        # What was here re-derived the ladder from the current
                        # quote and REPLACED the standing rungs with it, which
                        # follows the bid down as readily as up -- the ledger's
                        # own version of production's symmetric re-mint; and
                        # when the derivation came back empty (no bid) it kept
                        # the standing rungs, which is the stranding the
                        # collapsing ask sweeps.  Both are now decided by
                        # ``resting_rung_action_v1``.
                        #
                        # The rung is exactly one dwell old at each round --
                        # this loop steps on ``MAKER_RESTING_REPRICE_MINUTES``,
                        # which is pinned equal to the dwell -- so that is the
                        # age the kernel is asked about.
                        standing = max(pending.get(bucket) or [0.0])
                        r_bid = item.get("bid_price", item.get("best_bid"))
                        r_ask = item.get("best_ask")
                        if r_ask is None:
                            _lv = item.get("ask_levels") or []
                            r_ask = _lv[0].get("price") if _lv else None
                        rung_verdict = resting_rung_action_v1(
                            limit_price=float(standing or 0.0),
                            bid=float(r_bid or 0.0),
                            ask=float(r_ask or 0.0),
                            tick=float(item.get("price_tick_size") or 0.0),
                            age_seconds=RESTING_REPRICE_DWELL_SECONDS,
                            dwell_seconds=RESTING_REPRICE_DWELL_SECONDS,
                            far_ticks=RESTING_REPRICE_FAR_TICKS,
                        )
                        if rung_verdict["action"] == LEG_ACTION_CANCEL:
                            pending[bucket] = []
                        elif rung_verdict["action"] == LEG_ACTION_REPRICE_UP:
                            fresh = _ladder_from_quote(
                                item, open_rungs, ceiling=cap,
                            )
                            pending[bucket] = (
                                sorted(fresh, reverse=True) if fresh else []
                            )
                        # REST keeps the rung exactly where it is, which is
                        # the whole point: a bid that fell does not move it.
                    else:
                        fresh = _ladder_from_quote(
                            item,
                            open_rungs,
                            floor_fraction=max(floor_time, floor_edge, floor_gap),
                            ceiling=cap,
                        )
                        if fresh:
                            pending[bucket] = sorted(fresh, reverse=True)
                        elif cap is not None:
                            pending[bucket] = [
                                rung for rung in pending[bucket]
                                if rung <= cap + 1e-9
                            ]
                next_reprice = next_reprice + timedelta(
                    minutes=MAKER_RESTING_REPRICE_MINUTES
                )
        if withdrawn:
            break
        for bucket, rungs in pending.items():
            if not rungs:
                continue
            item = quotes.get(bucket)
            if not isinstance(item, Mapping):
                continue
            ask = item.get("best_ask")
            levels = item.get("ask_levels") or []
            if ask is None and levels:
                ask = levels[0].get("price")
            if ask is None:
                continue
            price = float(ask)
            # Control cell only (see MAKER_RESTING_CRASH_TICKS): an ask
            # that has gapped *through* the leg's cheapest rung is a
            # collapse, not a trade at our price.  Off by default -- the
            # standing ruling gives the market no say -- and kept solely
            # to measure what that ruling costs.
            if MAKER_RESTING_CRASH_TICKS > 0 and rungs:
                tick = float(item.get("price_tick_size") or 0.001)
                if price <= rungs[-1] - MAKER_RESTING_CRASH_TICKS * tick:
                    pending[bucket] = []
                    continue
            # Every rung the book reached -- what "reached" means is the
            # fill model's whole question.
            # A crossing is a necessary condition, never a sufficient one
            # under ``adverse``: the printed price reaching our rung says
            # the market traded there, and the question the venue answers
            # is whether anybody chose to trade there WITH US.  See
            # ``adverse_fill_predicate``.
            def _admitted(rung: float) -> bool:
                return fill_admits is None or fill_admits(bucket, rung)

            if MAKER_FILL_MODEL == "touch":
                while rungs and price <= rungs[0] + 1e-9:
                    if not _admitted(rungs[0]):
                        break
                    filled[bucket].append(rungs.pop(0))
            else:
                worked = _queue_worked_off(
                    queue_state, bucket, item,
                    require_queue=MAKER_FILL_MODEL == "queue",
                )
                while rungs and worked and price < rungs[0] - 1e-9:
                    if not _admitted(rungs[0]):
                        break
                    filled[bucket].append(rungs.pop(0))
    missing = next(
        (bucket for bucket, prices in filled.items() if not prices), None
    )
    return filled, missing, withdrawn


def _progressive_entry(
    *,
    buckets: Sequence[str],
    leg_shares: Sequence[float],
    filled_fraction: Mapping[str, float],
    prices: Mapping[str, float],
    probabilities: Mapping[str, float],
) -> tuple[
    list[str], list[float], float, float, list[float]
] | None:
    """Keep the part of each leg that filled, and only that part.

    A resting basket is built up, not switched on (user ruling,
    2026-08-17).  Each rung of each leg is its own order and is decided on
    its own, so the position that exists at the end of the horizon is
    whatever the book came down to -- a leg that filled four rungs of five
    is four fifths of a leg, and a leg that filled none is simply absent.

    The earlier rule sized the whole basket down to its least-filled leg to
    keep the shape intact.  That is a defensible *bet*, but it is not what
    resting orders do, and it threw away fills that had already happened.

    What this costs is honesty about the result: the basket that settles can
    be smaller than the one that was chosen, can be missing a rung in the
    middle, and its win probability is the sum over what was actually
    bought.  All three are recorded rather than assumed away.

    Returns ``None`` when nothing filled anywhere -- there is no position to
    record, which is the one case that is still a refusal.  The kept cash is
    returned leg by leg beside the shares: the rungs fill at different
    prices, so which leg the money went into does not follow from the share
    counts and cannot be recovered from the total.
    """

    kept_buckets: list[str] = []
    kept_shares: list[float] = []
    kept_stake: list[float] = []
    for bucket, target in zip(buckets, leg_shares):
        quantity = float(target) * float(filled_fraction.get(str(bucket), 0.0))
        price = float(prices.get(str(bucket), 0.0))
        if quantity <= 0.0 or price <= 0.0:
            continue
        kept_buckets.append(str(bucket))
        kept_shares.append(quantity)
        kept_stake.append(quantity * price)
    if not kept_buckets:
        return None
    stake = sum(kept_stake)
    win_probability = sum(
        float(probabilities.get(bucket, 0.0)) for bucket in kept_buckets
    )
    return kept_buckets, kept_shares, stake, win_probability, kept_stake


def _chain_cost_after_topup(
    *,
    held_after: Mapping[str, float],
    probabilities: Mapping[str, float],
    total_stake: float,
) -> float | None:
    """Charter 66 clause A's number: the chain's blended cost after a buy.

    The same expectation-equivalent share count the settlement rows carry
    (``cost_basis`` in the rule fingerprint), computed on the whole chain --
    every bucket it would hold after the buy, under the stage's served
    probabilities, against every USDC it would then have spent.  At or past
    1.00 no outcome returns the stake: the chain loses even when it wins,
    which is the class measured at 37 hits in 38 chains and -31.9 USDC.

    ``None`` when the claim carries no mass on any held bucket -- there is
    no expected payout to divide by, and a chain like that is refused for
    the same reason a division by zero is.
    """

    win = sum(
        float(probabilities.get(bucket, 0.0))
        for bucket, quantity in held_after.items()
        if quantity > 0.0
    )
    if win <= 1e-9:
        return None
    expected = sum(
        float(probabilities.get(bucket, 0.0)) * float(quantity)
        for bucket, quantity in held_after.items()
    )
    if expected / win <= 1e-12:
        return None
    return float(total_stake) / (expected / win)


def _chain_topup_growth(
    *,
    held: Mapping[str, float],
    bought: Mapping[str, float],
    probabilities: Mapping[str, float],
    spend: float,
    bankroll: float,
) -> float:
    """Charter 66 clause B's number, the kernel's since charter 71.

    Deliberately a *gate* on the sizing production's tilt rule proposes, not
    a sizer of its own: the full log-optimal increment was tried on the
    2026-08-29 rebuild and bought its way from -3.3% to -18.1% ROI.  The
    miss-floor replay arm stays this lane's knob and is handed through.
    """

    return chain_topup_growth_v1(
        held=held,
        bought=bought,
        probabilities=probabilities,
        spend=spend,
        bankroll=bankroll,
        miss_floor=CHAIN_TOPUP_MISS_FLOOR,
    )


def _effective_growth_partition(
    *,
    stage: str,
    event_date: str,
    probabilities: Mapping[str, float],
    buckets: Sequence[str],
    snapshot: Any,
    decision_at_utc: str | None = None,
) -> dict[str, float]:
    """Charter 71(b): the lambda*p partition the growth gate reads.

    The same dispatch the seat gate uses -- window scope deflates the seat by
    the window's agreement, bucket scope by each rung's own -- fed to the
    kernel's partition helper, which is the function production's solver
    hands its own growth gate.  Off-mode and unlisted stages read the served
    claim unchanged, exactly as the seat gate does.
    """

    if EFFECTIVE_EDGE_MODE != "on" or (
        stage not in EFFECTIVE_EDGE_STAGES
        and stage not in EFFECTIVE_EDGE_P0M_STAGES
        and stage not in EFFECTIVE_EDGE_P1_STAGES
    ):
        return {str(b): float(v) for b, v in probabilities.items()}
    p1_day_table = (
        P1_EFFECTIVE_EDGE_TABLE.get(str(event_date))
        if P1_EFFECTIVE_EDGE_TABLE else None
    )
    served = dict(_effective_edge_tables_as_of(decision_at_utc))
    if p1_day_table:
        served["P1"] = p1_day_table
    rows = {str(item.bucket): item for item in snapshot.buckets}
    prices: dict[str, float] = {}
    for bucket in buckets:
        row = rows.get(str(bucket))
        price = _execution_price(row) if row is not None else None
        if price is not None:
            prices[str(bucket)] = float(price)
    if EFFECTIVE_EDGE_SCOPE == "bucket":
        factors = {}
        for bucket in buckets:
            claim = float(probabilities.get(str(bucket), 0.0) or 0.0)
            price = prices.get(str(bucket))
            factors[str(bucket)] = (
                effective_edge_bucket_lambda_for_stage(
                    stage, price / claim, p1_table=served.get("P1")
                )
                if price is not None and claim > 0 else 1.0
            )
        return deflate_window_partition_v1(
            probabilities, list(buckets), bucket_factors=factors
        )
    claimed = sum(
        float(probabilities.get(str(bucket), 0.0) or 0.0) for bucket in buckets
    )
    if claimed <= 0 or len(prices) < len(list(buckets)):
        return {str(b): float(v) for b, v in probabilities.items()}
    # Charter 97: both axes, same resolver the production gate calls.
    lam, _lam_source = effective_edge_lambda_resolved(
        stage, claim=claimed, price=sum(prices.values()), tables=served
    )
    return deflate_window_partition_v1(
        probabilities, list(buckets), claim_factor=lam
    )


def _kelly_stake(
    *, win_probability: float, cost: float, equity: float
) -> tuple[float, float]:
    """Fractional Kelly for a basket paying 1 when the truth lands inside.

    Charter 72: the kernel's function, which production sizes with too.
    """

    return whole_basket_kelly_stake_v1(
        win_probability=win_probability,
        cost=cost,
        book_equity_usdc=equity,
        kelly_fraction=KELLY_FRACTION,
        scope_fraction=MAX_STAKE_FRACTION_PER_SCOPE,
    )


def _walk_ask(
    levels: list[dict[str, float]], shares: float
) -> float | None:
    """Cost of lifting ``shares`` off one bucket's offer ladder.

    ``None`` when the ladder does not hold that many.  The top level is a
    price for its own size and nothing more, so anything past it pays the
    next level up -- which is the whole difference between a book and a
    quote.
    """

    remaining = shares
    spent = 0.0
    for level in levels:
        take = min(remaining, level["shares"])
        spent += take * level["price"]
        remaining -= take
        if remaining <= 1e-9:
            return spent
    return None



def _leg_ask(quotes: Mapping[str, Any], bucket: str) -> float:
    """One rung's offer, top of book, from this book's quote row.

    Both share shapes price the same rung off the same number; writing the
    fallback twice is how two shapes end up disagreeing about what a leg
    costs on a book whose top level came in as a ladder rather than a scalar.
    """

    row = quotes.get(bucket) or {}
    if row.get("best_ask") is not None:
        return float(row["best_ask"] or 0.0)
    return float((row.get("ask_levels") or [{}])[0].get("price") or 0.0)


def _centre_tilt_weights(
    buckets: Sequence[str],
    *,
    centre_bucket: str,
    probabilities: Mapping[str, float],
    prices: Sequence[float],
    ordinals: Mapping[str, int],
) -> list[float] | None:
    """Production's rung weights, recomputed here from the same inputs.

    Imported rather than duplicated would be better, but the runtime builds
    them inside a candidate generator that needs a live snapshot; the shape
    is three lines and the constants come from the module that owns them, so
    a drift shows up as an import error rather than as a silent divergence.
    """

    if centre_bucket not in ordinals:
        return None
    centre = ordinals[centre_bucket]
    edges = [
        float(probabilities.get(b, 0.0)) - price
        for b, price in zip(buckets, prices)
    ]
    best_edge = max(edges) if edges else 0.0
    weights = []
    for bucket, edge in zip(buckets, edges):
        decay = CENTRE_ANCHOR_DISTANCE_DECAY ** abs(ordinals[bucket] - centre)
        lean = 1.0 + CENTRE_ANCHOR_EDGE_TILT * (
            max(edge, 0.0) / best_edge if best_edge > 1e-12 else 0.0
        )
        weights.append(max(decay * lean, CENTRE_ANCHOR_MIN_RUNG_SHARE))
    if centre_bucket in buckets:
        index = list(buckets).index(centre_bucket)
        weights[index] = max(weights[index], 1.0)
    total = sum(weights)
    return None if total <= 0 else [w / total for w in weights]


def _fillable_weighted(
    *,
    legs: list[list[dict[str, float]]],
    weights: Sequence[float],
    probabilities: Sequence[float],
    budget: float,
    fee_rate: float,
) -> tuple[list[float], float, list[float]] | None:
    """Largest weighted clip inside budget whose expected payout beats it.

    The equal-share path bounds itself with "average price under the win
    probability", which is the right test when every rung pays the same.
    Once rungs differ, the payout depends on which one lands, so the bound
    becomes the expectation itself: the basket must expect to return more
    than it costs, summed over the outcomes it holds.
    """

    # The weights size the *share count*, not the cash.  Weighting cash puts
    # the largest share count on the cheapest rung -- shares are cash over
    # price -- so a basket meant to hold its centre ends up holding its tail:
    # measured on ZGSZ 2026-08-17, 4.14 USDC on 30C bought 207 shares while
    # 6.51 on the centre bought 50, and the payout curve peaked two rungs out.
    # Sizing shares directly makes the centre the position and lets price
    # decide what that costs.
    def attempt(
        scale: float,
    ) -> tuple[list[float], float, float, list[float]] | None:
        shares: list[float] = []
        cash: list[float] = []
        for leg, weight in zip(legs, weights):
            want = scale * weight
            got, paid = _walk_ask_shares(leg, want)
            if got <= 0.0 or got + 1e-9 < want:
                return None
            shares.append(got)
            # The fee rides on the leg that incurred it, so the legs still
            # add up to the stake the position is recorded at.
            cash.append(paid * (1.0 + fee_rate))
        spend = sum(cash)
        if spend > budget + 1e-9:
            return None
        expected = sum(p * q for p, q in zip(probabilities, shares))
        return shares, spend, expected, cash

    best: tuple[list[float], float, list[float]] | None = None
    # ``scale`` is now a share multiplier, so its ceiling comes from the
    # thinnest leg rather than from the budget being a fraction of itself.
    ceiling = min(
        (sum(float(level["shares"]) for level in leg) / max(weight, 1e-9)
         for leg, weight in zip(legs, weights)),
        default=0.0,
    )
    if ceiling <= 0.0:
        return None
    low, high = 0.0, ceiling
    probe = attempt(min(ceiling, 1e-3))
    if probe is None or probe[2] <= probe[1]:
        return None
    for _ in range(60):
        mid = 0.5 * (low + high)
        got = attempt(mid)
        if got is not None and got[2] > got[1]:
            best = (got[0], got[1], got[3])
            low = mid
        else:
            high = mid
    return best


def _walk_ask_shares(
    levels: Sequence[Mapping[str, float]], want: float
) -> tuple[float, float]:
    """Shares bought and cash spent walking one leg for a share target."""

    shares = spent = 0.0
    for level in levels:
        if shares + 1e-12 >= want:
            break
        take = min(float(level["shares"]), want - shares)
        if take <= 0.0:
            break
        shares += take
        spent += take * float(level["price"])
    return shares, spent


def _walk_ask_cash(
    levels: Sequence[Mapping[str, float]], cash: float
) -> tuple[float, float]:
    """Shares bought and cash spent walking one leg with a cash limit."""

    shares = spent = 0.0
    for level in levels:
        price = float(level["price"])
        if price <= 0.0 or cash - spent <= 1e-12:
            break
        take = min(float(level["shares"]), (cash - spent) / price)
        if take <= 0.0:
            break
        shares += take
        spent += take * price
    return shares, spent


def _fillable_size(
    *,
    legs: list[list[dict[str, float]]],
    win_probability: float,
    budget: float,
    fee_rate: float,
    capacity: float,
) -> tuple[float, float, list[float]] | None:
    """Largest clip whose walked cost stays inside budget and edge.

    Both constraints are monotone in size -- spend rises with it, and so
    does the average price -- so the feasible sizes are an interval from
    zero and a bisection finds its end.
    """

    def spend(size: float) -> tuple[float, list[float]] | None:
        # Per leg, then summed -- equal share counts do not mean equal cash,
        # and the page has to be able to say which rung the money is in.
        # The fee rides on the leg that incurred it so the parts still add
        # up to the whole.
        per_leg: list[float] = []
        for leg in legs:
            leg_cost = _walk_ask(leg, size)
            if leg_cost is None:
                return None
            per_leg.append(leg_cost * (1.0 + fee_rate))
        return sum(per_leg), per_leg

    def feasible(size: float) -> float | None:
        walked = spend(size)
        if walked is None or walked[0] > budget:
            return None
        cost = walked[0]
        # An all-in average at or above the payout is a guaranteed loss no
        # matter how the day settles, which is exactly what the top-of-book
        # price hid when the clip outran the level that quoted it.
        if cost / size >= win_probability:
            return None
        return cost

    if capacity <= 0.0 or feasible(min(capacity, 1e-6)) is None:
        return None
    low, high = 0.0, capacity
    for _ in range(60):
        mid = 0.5 * (low + high)
        if mid <= 0.0:
            break
        if feasible(mid) is None:
            high = mid
        else:
            low = mid
    walked = spend(low)
    if low <= 0.0 or walked is None:
        return None
    return low, walked[0], walked[1]


def _settled_bucket(
    buckets: list[tuple], truth_c: float
) -> tuple[str, int] | None:
    for bucket, ordinal, lower, upper, _probability in buckets:
        low = -1e9 if lower is None else float(lower)
        high = 1e9 if upper is None else float(upper)
        if low <= truth_c < high:
            return str(bucket), int(ordinal)
    return None


class _Score(NamedTuple):
    """What a settled value does to a seat, once the value is known."""

    settled_bucket: str
    hit: int
    payout_usdc: float
    pnl_usdc: float
    center_error_c: float | None
    bucket_distance: int
    # ``None`` when the seat never recorded which legs it posted, which is
    # the only honest answer for a row written before the column existed.
    intended_hit: int | None = None


def _score_settlement(
    *,
    ladder: Sequence[tuple],
    window: Sequence[str],
    stake: float,
    shares: float,
    per_leg: Mapping[str, float],
    center_c: float | None,
    truth_c: float,
    intended_window: Sequence[str] | None = None,
) -> _Score | None:
    """Score one seat against one settled value.

    Two callers reach this: the entry walk, which knows the seat because it
    just chose it, and ``_rescore_settlements``, which reads the seat back off
    the recorded row.  They have to agree exactly -- a settlement re-derived
    under a corrected truth that also silently re-derived its *payout* rule
    would move numbers the correction never touched -- so the arithmetic lives
    in one place rather than twice.

    ``None`` means the settled value fell outside the ladder the seat was
    priced against: nothing to score, not a loss.
    """

    landed = _settled_bucket(list(ladder), truth_c)
    if landed is None:
        return None
    settled_bucket, settled_ordinal = landed
    seated = {str(bucket) for bucket in window}
    hit = 1 if settled_bucket in seated else 0
    # The rung that landed is the one that pays.  With one share count for
    # the whole basket these are the same number; with a weighted basket they
    # are not, and using the basket-level count credits a payout no outcome
    # produced.
    payout = (
        float(per_leg.get(settled_bucket, 0.0))
        if (hit and per_leg)
        else (shares if hit else 0.0)
    )
    ordinals = {str(b): int(o) for b, o, _l, _u, _p in ladder}
    distance = min(abs(ordinals[b] - settled_ordinal) for b in seated)
    # The same membership test against the basket that was chosen rather
    # than the one that filled.  Scored here beside ``hit`` for the reason
    # the docstring gives: two answers derived twice will disagree the first
    # time either side is touched, and these two have to be comparable.
    chosen = {str(bucket) for bucket in (intended_window or ())}
    return _Score(
        settled_bucket=settled_bucket,
        hit=hit,
        payout_usdc=payout,
        pnl_usdc=payout - stake,
        center_error_c=(
            None if center_c is None else float(center_c) - truth_c
        ),
        bucket_distance=distance,
        intended_hit=(1 if settled_bucket in chosen else 0) if chosen else None,
    )


def _settlement_ladder(
    conn: sqlite3.Connection,
    station_id: str,
    event_date: str,
    stage: str,
    decision_at_utc: str,
) -> list[tuple]:
    """The bucket ladder the recorded seat was priced against.

    One indexed lookup: ``decision_at_utc`` is unique within a stage scope, so
    this recovers exactly the ``buckets`` the entry walk held when it opened
    the seat -- edges included, which ``probabilities_json`` does not carry.
    That is why re-scoring needs no history walk and no quote replay.
    """

    return conn.execute(
        "SELECT b.bucket, b.ordinal, b.lower_c, b.upper_c, b.probability"
        " FROM basket_probability_bridge_history h"
        " JOIN basket_probability_bridge_bucket_probability b"
        "   ON b.history_id = h.history_id"
        " WHERE h.station_id=? AND h.event_date=? AND h.state='COMMITTED'"
        "   AND h.stage=?"
        "   AND h.decision_at_utc=?"
        " ORDER BY b.ordinal",
        (station_id, event_date, stage, decision_at_utc),
    ).fetchall()


def _score_candidate_windows(
    conn: sqlite3.Connection, truth: dict[tuple[str, str], _Truth]
) -> dict[str, int]:
    """Grade every recorded candidate window against the truth that stands.

    The same two production functions the settlement uses -- the ladder the
    decision was priced against and the bucket a value lands in -- so a
    window and the seat beside it can never disagree about which bucket won.
    Bound to ``truth_content_sha256`` for the same reason settlements are:
    truth is revisable by design, and a row that does not say which revision
    it read cannot know it has gone stale.

    A window with no truth yet is simply not scored.  It is not evidence of
    anything and the fit skips it -- which is the entire difference between
    this table and the seats: a station-day the gate refused still resolves,
    and its windows still say what a claim of that size and that agreement
    was worth.
    """

    rows = conn.execute(
        "SELECT candidate_id, station_id, event_date, stage,"
        " decision_at_utc, buckets_json, truth_content_sha256"
        " FROM paper_trade_candidate_window"
    ).fetchall()
    counts: dict[str, int] = defaultdict(int)
    ladders: dict[tuple, list] = {}
    write: list[tuple] = []
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    for (candidate_id, station_id, event_date, stage, decided,
         buckets_json, bound_sha) in rows:
        standing = truth.get((str(station_id), str(event_date)))
        if standing is None:
            counts["candidate_open"] += 1
            continue
        if str(bound_sha or "") == standing.content_sha256:
            counts["candidate_current"] += 1
            continue
        key = (station_id, event_date, stage, decided)
        if key not in ladders:
            ladders[key] = _settlement_ladder(
                conn, str(station_id), str(event_date), str(stage),
                str(decided),
            )
        ladder = ladders[key]
        if not ladder:
            counts["candidate_no_ladder"] += 1
            continue
        landed = _settled_bucket(list(ladder), standing.tmax_c)
        if landed is None:
            counts["candidate_truth_outside_book"] += 1
            continue
        settled_bucket, _ordinal = landed
        try:
            buckets = [str(b) for b in json.loads(buckets_json or "[]")]
        except (TypeError, ValueError):
            counts["candidate_unreadable"] += 1
            continue
        write.append((
            settled_bucket, 1 if settled_bucket in buckets else 0, now,
            standing.revision_no, standing.content_sha256, candidate_id,
        ))
        counts["candidate_scored"] += 1
    if write:
        conn.executemany(
            "UPDATE paper_trade_candidate_window"
            " SET settled_bucket=?, hit=?, settled_at_utc=?,"
            "     truth_revision_no=?, truth_content_sha256=?"
            " WHERE candidate_id=?",
            write,
        )
        conn.commit()
    return dict(counts)


def _settlement_identity_id(
    *,
    station_id: str,
    event_date: str,
    chain_opened_at: str,
    window: Sequence[str],
    stake_usdc: float,
    truth_c: float,
) -> str:
    """The settlement's id, from the one place that decides its shape.

    Two writers put rows in ``paper_trade_settlement`` -- the entry walk
    when the day resolves, and the re-score when a truth revision moves it
    -- and they hashed different dictionaries.  The walk keyed the CHAIN
    (``chain_opened_at`` and the accumulated window, no stage); the
    re-score keyed a STAGE (``stage`` and its own ``decision_at_utc``).
    So a corrected settlement got an id the walk would never produce, and
    re-scoring was not idempotent on identity even when it re-derived the
    same numbers.  Measured on the live book 2026-09-10: 237 of 249 ids
    carried the walk's shape and 7 the re-score's.

    A settlement is one station-day -- the chain settles once, whichever
    stage last topped it up -- so the chain's shape is the right one and
    the stage is deliberately not in it.
    """

    return "paper-set-" + content_sha256({
        "station_id": str(station_id),
        "event_date": str(event_date),
        "chain_opened_at": str(chain_opened_at),
        "window": list(window),
        "stake_usdc": round(float(stake_usdc), 6),
        "truth": truth_c,
    })[:24]


def _rescore_settlements(
    conn: sqlite3.Connection, truth: dict[tuple[str, str], _Truth]
) -> dict[str, int]:
    """Score every recorded seat against the truth that stands *now*.

    A seat is chosen before the day resolves, so settled truth is no part of
    it -- but the settlement is nothing else, and both were written by the
    same branch of the entry walk.  That walk skips a stage it has already
    seated, so once the position row existed its settlement was frozen in two
    ways.  A seat opened before its truth landed never got one at all: the
    next sweep saw the stage as recorded and walked past it, and on 2026-08-18
    that left 86 of 262 positions open with only a rebuild able to close any
    of them -- two already had their truth in hand.  And a settlement whose
    truth was later *revised* kept scoring the superseded value, because the
    write is ``INSERT OR IGNORE`` on ``UNIQUE(station_id,event_date,stage)``
    and the only thing that deletes a settlement keys on the selection rule,
    which a truth correction does not touch.

    Truth is revisable by design -- append-only under ``revision_no``, guarded
    by the ``calibration_truth_no_update``/``calibration_truth_no_delete``
    triggers, so a correction can only arrive as a new revision -- and on
    2026-08-18 the sub-degree Fahrenheit re-derivation appended 192 of them at
    once, 156 changing the value and 35 moving the winning bucket.  The
    quarter-hourly ``--backfill`` would have scored the old buckets
    indefinitely; a full ``--rebuild``, which discards the whole derived book,
    was the only way to move them.

    So each settlement records the truth it read, and is re-derived here when
    that binding is no longer the standing one.  Three answers per seat:
    nothing to do when the binding still stands; a two-column re-bind when the
    revision moved but the value did not, which leaves the page's numbers and
    ``settled_at_utc`` untouched; and a full re-score otherwise.

    Deliberately *not* bounded by ``recent_days`` the way
    ``_drop_superseded_positions`` is.  That bound exists because re-deriving
    a seat means walking a day's whole decision history and quote book, which
    is most of the pass; re-scoring means one indexed ladder lookup, and only
    for the rows whose truth actually moved.  A correction that reaches back a
    month has to reach that month's rows, and on the days where nothing moved
    this costs a single scan of a table with a few hundred rows in it.
    """

    counts: dict[str, int] = defaultdict(int)
    # One row per chain, not per stage: the stages of a station-day are one
    # position and settle once, so this re-scores the position as the last
    # stage left it.  Reading every stage row here would write a settlement
    # per stage and rebuild the three-book account this ledger no longer
    # keeps -- against a single truth, which is how the same event came to
    # be charged three times.
    rows = conn.execute(
        "SELECT p.position_id, p.station_id, p.event_date, p.stage,"
        " p.decision_at_utc, p.chain_shares_json, p.leg_shares_json,"
        " p.chain_stake_usdc, p.stake_usdc, p.shares, p.probabilities_json,"
        " p.win_probability, p.center_c, p.intended_legs_json,"
        " s.settlement_id, s.truth_content_sha256, s.settled_tmax_c,"
        # The chain's first decision, which is what its identity is keyed
        # on.  The walk carries it in hand; this pass has to read it, and
        # it is the same MIN the walk's own accumulation arrives at.
        " t.chain_opened_at"
        " FROM paper_trade_position p"
        # The terminal row is the last row the chain ACCUMULATED, which is
        # insert order, not wall-clock decision order.  A P1 entry compiled
        # from an earlier cycle carries an earlier decision_at_utc than the
        # P0-M row before it, and the wall-clock max then settles the chain
        # off a row missing the later stage's fills -- ZHHH 2026-08-21 lost
        # a 2/2-filled winning leg to exactly this (nine chains measured,
        # the fourth site of the parallel-stage wall-clock family).
        " JOIN (SELECT station_id, event_date, MAX(rowid) AS last_rowid,"
        "        MIN(decision_at_utc) AS chain_opened_at"
        "       FROM paper_trade_position GROUP BY station_id, event_date) t"
        "   ON t.station_id = p.station_id AND t.event_date = p.event_date"
        "  AND t.last_rowid = p.rowid"
        " LEFT JOIN paper_trade_settlement s"
        "   ON s.position_id = p.position_id"
    ).fetchall()
    rebind: list[tuple] = []
    drop: list[tuple] = []
    write: list[tuple] = []
    for row in rows:
        (
            position_id, station_id, event_date, stage, decision_at_utc,
            chain_shares_json, leg_shares_json, chain_stake, stage_stake,
            stage_shares, probabilities_json, stage_win_probability,
            center_c, intended_legs_json,
            settlement_id, bound_sha, settled_tmax_c, chain_opened_at,
        ) = row
        standing = truth.get((str(station_id), str(event_date)))
        if standing is None:
            # No settled value yet.  An open seat, not a stale one -- and
            # truth is never deleted, so a settled row cannot lose its truth.
            counts["open_unsettled"] += 1
            continue
        # Both short-circuits below used to fire here, before the window
        # was known.  They cannot any more: whether a recorded row may be
        # left alone now also depends on whether its ID is the one this
        # ledger writes, and the id is keyed on the chain's window -- so
        # the decision moves under the scoring, which is where the window
        # exists.  The extra cost is one indexed ladder lookup and a few
        # buckets of arithmetic per settled chain per sweep, on a table of
        # a few hundred rows.
        truth_is_current = (
            settlement_id is not None
            and str(bound_sha or "") == standing.content_sha256
        )
        value_is_unchanged = (
            settlement_id is not None
            and settled_tmax_c is not None
            and float(settled_tmax_c) == standing.tmax_c
        )
        ladder = _settlement_ladder(
            conn, str(station_id), str(event_date), str(stage),
            str(decision_at_utc),
        )
        if not ladder:
            counts["settlement_ladder_missing"] += 1
            continue
        # The accumulated position, falling back to this stage's own legs for
        # a row written before the chain columns existed -- a one-stage chain
        # is the same thing either way.
        per_leg = {
            str(bucket): float(value)
            for bucket, value in (
                json.loads(chain_shares_json or leg_shares_json or "{}") or {}
            ).items()
        }
        if not per_leg:
            counts["settlement_position_empty"] += 1
            continue
        stake = float(
            chain_stake if chain_stake is not None else stage_stake
        )
        ordinals = {str(b): int(o) for b, o, _l, _u, _p in ladder}
        window = sorted(per_leg, key=lambda b: ordinals.get(b, 0))
        probabilities = json.loads(probabilities_json or "{}") or {}
        win_probability = sum(
            float(probabilities.get(bucket, 0.0)) for bucket in per_leg
        ) or float(stage_win_probability)
        expected_payout = sum(
            float(probabilities.get(bucket, 0.0)) * quantity
            for bucket, quantity in per_leg.items()
        )
        shares = (
            expected_payout / max(win_probability, 1e-9)
            if expected_payout > 0
            else float(stage_shares)
        )
        try:
            intended_legs = json.loads(intended_legs_json or "{}") or {}
        except (TypeError, ValueError):
            intended_legs = {}
        scored = _score_settlement(
            ladder=ladder,
            window=window,
            stake=stake,
            shares=shares,
            per_leg=per_leg,
            center_c=center_c,
            truth_c=standing.tmax_c,
            # Same union as the entry walk: what the chain holds plus what
            # its last stage posted.  A row with no recorded intent scores
            # ``None`` here rather than borrowing ``window``, which would
            # make an unmeasurable seat look like a perfectly filled one.
            intended_window=(
                sorted(set(window) | set(map(str, intended_legs)))
                if intended_legs else None
            ),
        )
        if scored is None:
            # The corrected value left the ladder the seat was priced
            # against.  The entry walk records no settlement in that case, so
            # neither does a re-score -- and a row scored against a value
            # that is no longer the truth must not survive as if it were.
            counts["truth_outside_book"] += 1
            if settlement_id is not None:
                drop.append((station_id, event_date))
                counts["settlement_dropped"] += 1
            continue
        rebuilt_id = _settlement_identity_id(
            station_id=str(station_id),
            event_date=str(event_date),
            chain_opened_at=str(chain_opened_at or decision_at_utc),
            window=window,
            stake_usdc=float(stake),
            truth_c=standing.tmax_c,
        )
        if settlement_id is not None and str(settlement_id) == rebuilt_id:
            # The row this ledger would write is the row that is there.
            if truth_is_current:
                counts["settlement_current"] += 1
                continue
            if value_is_unchanged:
                # The revision moved but the value did not, so every
                # number derived from it is unchanged by construction.
                # Re-binding rather than re-writing is what keeps a
                # revision that only restates the day from touching the
                # page -- and it is how the rows written before this
                # column existed, which carry the empty sha, settle down
                # after one pass instead of looking stale forever.
                rebind.append(
                    (standing.revision_no, standing.content_sha256,
                     position_id)
                )
                counts["settlement_rebound"] += 1
                continue
        elif settlement_id is not None and truth_is_current:
            # Right numbers, wrong key: written by the shape the re-score
            # used before 2026-09-10, which no writer produces now.  Seven
            # rows of the live book were in this state, and while they
            # were, "a settlement's id is what the entry walk would write"
            # was not something the book could be held to.  Rewritten
            # once, then never again.
            counts["settlement_rekeyed"] += 1
        write.append((
            rebuilt_id,
            position_id, station_id, event_date, stage,
            standing.tmax_c, scored.settled_bucket, scored.hit,
            scored.intended_hit,
            float(stake), scored.payout_usdc, scored.pnl_usdc,
            float(win_probability), scored.center_error_c,
            scored.bucket_distance,
            datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            standing.revision_no, standing.content_sha256,
            stake / shares if shares > 0 else None,
        ))
        counts[
            "settlement_rescored" if settlement_id is not None
            else "settlement_settled_late"
        ] += 1
    if not (rebind or drop or write):
        return dict(counts)
    # Same short write burst as the entry walk, and for the same reason: the
    # scan above is all reads, and the resident gives up on a busy database
    # after five seconds.
    if rebind:
        conn.executemany(
            "UPDATE paper_trade_settlement"
            " SET truth_revision_no=?, truth_content_sha256=?"
            " WHERE position_id=?",
            rebind,
        )
    # Delete before insert rather than upsert: a re-scored row gets a new
    # ``settlement_id`` whenever the value moved (the id hashes the truth), so
    # the incoming row can collide on the primary key or on the station-day
    # uniqueness, and only one of those can be an upsert target.  Keyed on the
    # station-day alone -- one chain settles once, and the stage it carries is
    # whichever one last topped the position up, which can move between passes
    # without the day's account changing hands.
    if drop or write:
        conn.executemany(
            "DELETE FROM paper_trade_settlement"
            " WHERE station_id=? AND event_date=?",
            drop + [(row[2], row[3]) for row in write],
        )
    if write:
        conn.executemany(
            "INSERT INTO paper_trade_settlement ("
            " settlement_id, position_id, station_id, event_date, stage,"
            " settled_tmax_c, settled_bucket, hit, intended_hit,"
            " stake_usdc,"
            " payout_usdc, pnl_usdc, claimed_win_probability,"
            " center_error_c, bucket_distance, settled_at_utc,"
            " truth_revision_no, truth_content_sha256,"
            " basket_all_in_cost"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            write,
        )
    conn.commit()
    return dict(counts)


def _recorded_refusal_stages(
    conn: sqlite3.Connection, *, station_id: str, event_date: str
) -> set[str]:
    """The stages THIS rule set has already refused for one station-day.

    One definition, so the walk and its test cannot hold two.  Rows stamped
    by another rule set are not this one's answer: see ``_REFUSAL_DDL`` for
    the shared-database arm that read 783 of them as its own.  An unstamped
    row (written before the column existed) is not this rule set either.
    """

    return {
        str(row[0])
        for row in conn.execute(
            "SELECT stage FROM paper_trade_refusal"
            " WHERE station_id=? AND event_date=?"
            "   AND selection_rule_sha256=?",
            (station_id, event_date, SELECTION_RULE_SHA256),
        )
    }


def _scope_answer_is_final(
    present: set[str],
    seated: set[str],
    refused: set[str],
    *,
    settled: bool,
) -> bool:
    """Whether this station-day's answer is recorded and cannot change.

    A seat is final whatever the day is doing: the chain is walked whole, so
    a stage with a position row has been priced.  A *refusal* is only final
    once the day is settled -- an open day's ``no_positive_kelly`` can become
    a seat on tomorrow's book or a later P1, so those keep being re-derived.

    The walk used to ask about seats alone, and a refused scope writes no
    seat -- so ``seated`` stayed short of ``present`` forever and the chain
    was re-derived every pass to reach the refusal it already held.
    Measured 2026-09-01 over the four-day window: 201 scopes, 22 short-
    circuiting, 179 re-walked, and every one of the 179 already answered by
    a seat or a refusal.  Nothing was unanswered.  125 of them sat on
    settled days -- 08-29, 08-30 and 08-31, forty-odd stations apiece --
    answering ``chain_never_opened`` or ``negative_edge_window`` again every
    fifteen minutes.  That is what the pass spent its ~1100 seconds on,
    beside a resident whose WAL checkpoint waits on the same lock.

    ``refused`` must be built from THIS rule set's refusals only.  The
    table carried no fingerprint until 2026-09-11 and a refusal is what
    this function trusts on a settled day, so an arm reading another arm's
    table skipped every scope that arm had refused; the column, the
    superseded drop that now reaches it, and the measurement are in
    ``_REFUSAL_DDL``.  A change to the reason vocabulary still forces a
    full walk through ``refusal_rebuilt``.
    """

    if not present:
        return False
    if present <= seated:
        return True
    return settled and present <= (seated | refused)


def backfill(
    conn: sqlite3.Connection,
    *,
    verbose: bool = False,
    recent_days: int | None = None,
    through_date: str | None = None,
    budget_sec: float | None = None,
) -> dict:
    # The walk stops between scopes once its budget is spent and says so in
    # ``counts["stopped_for_time"]``: what it has answered is committed, the
    # scopes it did not reach are unanswered and are re-walked next pass.
    # Bounding only the repair *slices* left the scheduled pass itself
    # unbounded -- a rule change drops the window's rows and the first pass
    # re-walks them all: 1,216s on 2026-09-15T07:18Z, production waiting
    # behind it for every second of it.
    walk_deadline = (
        None if budget_sec is None else time.monotonic() + float(budget_sec)
    )
    refusal_rebuilt = ensure_schema(conn)
    # Rows below the floor are not re-derived, so nothing else would ever remove
    # them; without this an incremental pass would leave a retired strategy on
    # the page forever while claiming the window starts later.
    below_floor = _purge_below_floor(conn)
    if refusal_rebuilt:
        # The reason vocabulary just changed and the table was rebuilt
        # around it.  An incremental pass would re-answer only the recent
        # window and leave every older row speaking the previous
        # vocabulary under the new labels -- so this one pass walks
        # everything and the table comes back uniformly coded.  Self-
        # healing, deliberately: the operator step this replaces was
        # "remember to run --recent-days 0 after touching
        # REFUSAL_REASONS", and nobody remembered.
        recent_days = None
    # Charter 129's outstanding item, ruled on 2026-09-12: the same
    # self-healing for the rule set itself.  A book holding two of them
    # publishes one return for two strategies, and the incremental window
    # can never reach the older one.
    foreign_rule_set_rows = _positions_under_other_rule_sets(conn)
    # Bounded, oldest days first (see ``REDERIVE_SLICE_DAYS``).
    repair_through = (
        _repair_slice_through(conn, recent_days=recent_days)
        if through_date is None else None
    )
    if (
        foreign_rule_set_rows or repair_through
    ) and AUTO_REDERIVE_ON_RULE_CHANGE == "on":
        recent_days = None
        if repair_through:
            through_date = repair_through
    superseded, superseded_refusals = _drop_superseded_positions(
        conn, recent_days=recent_days, through_date=through_date
    )
    truth = _truth(conn)
    # Charter 68(c): bind the store for as-of lambda lookups this pass.
    global _EE_CONN
    _EE_CONN = conn
    _EE_AS_OF_CACHE.clear()
    # The market day a resting order lives inside.  Charter 44 binds the
    # settlement timezone per station in its own table rather than the
    # registry, whose raw column is wrong for 33 of 45 stations.
    timezones = {
        str(station): str(zone)
        for station, zone in conn.execute(
            "SELECT station_id, settlement_timezone"
            " FROM calibration_station_market_timezone_binding"
        )
    }
    # Each station's own peak-heat cutoff, derived once per pass from the
    # trajectory lane's intraday temperatures.  Resting orders die there,
    # not at settlement.
    peak_cutoffs = _station_peak_cutoffs(timezones)
    # Before the walk, not after.  The equity curve below reads each day's
    # realised P&L back out of the committed settlements, so a correction that
    # landed after this pass would size the following days off numbers it had
    # already superseded.  The walk itself only ever adds seats this pass
    # created, which this cannot have touched -- they do not exist yet.
    rescored = _rescore_settlements(conn, truth)
    # A day whose history and book are both closed cannot grow a new seat, and
    # re-deriving its refusals is what made the pass expensive: roughly two
    # hundred scopes that will answer "no positive kelly" or "no book" forever
    # were re-answered every run.  On a schedule that read is what the
    # resident's WAL checkpoint waits on.  ``recent_days`` bounds an
    # incremental pass to the window that can still change; a rebuild leaves
    # it None and walks everything, because that is the pass whose equity
    # actually compounds day over day.
    # ``through_date`` bounds the walk from above so a rebuild that cannot
    # finish in one sitting can be taken oldest-first, one slice at a
    # time.  The order matters: every seat is sized off the equity the
    # days before it left behind, so a slice taken newest-first would be
    # sized off rows the next slice then re-derives.  Rows above the bound
    # are left exactly as they are.
    scopes = conn.execute(
        "SELECT DISTINCT station_id, event_date"
        " FROM basket_probability_bridge_history"
        " WHERE event_date >= ?2"
        "   AND (?1 IS NULL OR event_date >= date("
        "   (SELECT max(event_date) FROM basket_probability_bridge_history),"
        "   '-' || ?1 || ' days'))"
        "   AND (?3 IS NULL OR event_date <= ?3)"
        " ORDER BY event_date, station_id",
        (recent_days, LEDGER_EVENT_DATE_FLOOR, through_date),
    ).fetchall()
    counts: dict[str, int] = defaultdict(int)
    if refusal_rebuilt:
        counts["refusal_schema_rebuilt_full_walk"] = 1
    if foreign_rule_set_rows:
        counts["positions_under_other_rule_sets"] = foreign_rule_set_rows
        counts["rule_change_full_rederive"] = (
            1 if AUTO_REDERIVE_ON_RULE_CHANGE == "on" else 0
        )
    if repair_through:
        counts["repair_slice_through"] = repair_through
    if superseded:
        counts["superseded_rule_dropped"] = superseded
    if superseded_refusals:
        counts["superseded_rule_refusals_dropped"] = superseded_refusals
    if below_floor:
        counts["below_event_date_floor_dropped"] = below_floor
    for key, value in rescored.items():
        if value:
            counts[key] += value
    # Equity advances a day at a time, and every position opened on a day is
    # sized off the equity the book held when that day opened.  Compounding
    # within the day would let a station-day that settles first bankroll the
    # ones beside it, which no book can do -- they are all opened before any
    # of them resolve.
    #
    # Only credible fills compound.  The report has always split the book at
    # ``DUST_COST_FLOOR`` because a basket bought under a cent is a one-sided
    # tail quote rather than a price anyone would fill, and a 0.001 leg that
    # lands returns a thousand times its stake.  Sizing was still reading the
    # unsplit number, so those phantom wins bankrolled the real ones: on
    # 2026-08-13 dust manufactured +3713 (P0) and +3733 (P1), lifting equity
    # from about a thousand to 4615/4792, and the 2% per-scope cap then sized
    # 2026-08-15's seats at 73 USDC apiece instead of 20.  Dust seats are
    # still recorded and still reported -- charter 35 buys the negative-EV leg
    # and refusing one-sided quotes outright leaves a single tradable basket
    # -- they just no longer set the size of anything.
    #
    # An incremental pass inherits the book the days before its window left
    # behind; a rebuild walks from the first day and starts at the initial
    # capital.  Both read the same settlements through the same dust filter,
    # so the two paths size a seat identically.
    equity = _opening_equity(
        conn, earliest=_window_floor(conn, recent_days=recent_days)
    )
    pending_positions: list[tuple] = []
    pending_settlements: list[tuple] = []
    pending_candidates: list[tuple] = []
    pending_refusals: list[tuple] = []
    pending_chain_resets: list[tuple[str, str]] = []
    by_day: dict[str, list[str]] = defaultdict(list)
    for station_id, event_date in scopes:
        by_day[str(event_date)].append(str(station_id))
    for event_date in sorted(by_day):
        day_pnl = 0.0
        if counts.get("stopped_for_time"):
            break
        for station_id in sorted(by_day[event_date]):
            if (
                walk_deadline is not None
                and time.monotonic() >= walk_deadline
            ):
                counts["stopped_for_time"] = 1
                break
            # Ask the cheap question first.  Resolving a stage's opening
            # distribution walks its whole history looking for the earliest
            # decision that has a book, and doing that for scopes already
            # seated was most of the pass: a steady-state sweep spent the
            # better part of a minute re-deriving 268 answers it was about
            # to throw away.  On a schedule beside the resident that long
            # read is what a WAL checkpoint has to wait on, and the resident
            # gives up after five seconds -- so the wasted work was not just
            # slow, it fail-closed P0 and P1.
            seated = {
                str(row[0])
                for row in conn.execute(
                    "SELECT stage FROM paper_trade_position"
                    " WHERE station_id=? AND event_date=?",
                    (station_id, event_date),
                )
            }
            present = {
                str(row[0])
                for row in conn.execute(
                    "SELECT DISTINCT stage FROM basket_probability_bridge_history"
                    " WHERE station_id=? AND event_date=? AND state='COMMITTED'",
                    (station_id, event_date),
                )
            }
            if present and present <= seated:
                counts["already_recorded"] += len(present)
                continue
            # A refusal is an answer too, once the day can no longer change.
            # Asked only on a settled day, and only after the seat check
            # above has missed; see ``_scope_answer_is_final``.
            if present and (station_id, event_date) in truth:
                refused = _recorded_refusal_stages(
                    conn, station_id=station_id, event_date=event_date
                )
                if _scope_answer_is_final(
                    present, seated, refused, settled=True
                ):
                    counts["already_answered_settled"] += len(present)
                    continue
            # A chain is walked whole or not at all.  A top-up is priced
            # against what the position already holds, so replaying P1 on
            # its own -- with P0's rows left in place and its shares unknown
            # to this pass -- would size the top-up as if the chain were
            # empty.  Rows for the day are dropped in the write burst and
            # rewritten from P0 forward.
            if seated:
                pending_chain_resets.append((station_id, event_date))
                counts["chain_replayed"] += 1
            # The position this station-day holds, built up stage by stage.
            # Shares per bucket, what they cost, and the ladder the last
            # stage to act was working from -- settlement reads all three.
            held: dict[str, float] = {}
            held_cost: dict[str, float] = {}
            chain_stake = 0.0
            # The whole chain spends under the tilt its opener earned.
            chain_tilt = 1.0
            chain_consolidation: dict | None = None
            chain_probabilities: dict[str, float] = {}
            # Every leg the chain POSTED, across its stages.  ``held`` keeps
            # what came back; this keeps what was asked for, so the two can
            # be compared at settlement instead of only the survivor being
            # recorded.
            chain_intended: set[str] = set()
            # Charter 143 v2: the size each leg was last posted at -- the
            # most a completion may buy of it.
            chain_intended_shares: dict[str, float] = {}
            chain_probability_identity = ""
            # Charter 77: the last recorded decision per stage.
            chain_decision_signatures: dict[str, dict] = {}
            chain_ladder: list = []
            chain_center_c: float | None = None
            chain_win_probability = 0.0
            chain_credible = False
            chain_opened_at = ""
            chain_position_id = ""
            chain_last_stage = ""
            for stage in (s for s in LEDGER_STAGES if s in present):
                # Only P0 opens.  A later stage on a day the position was
                # never opened is not a bet this strategy would have held --
                # under three books it was, and that is where a third of the
                # recorded P1 rows came from.
                if stage != "P0" and not held and STAGE_SELF_OPEN != "on":
                    counts["chain_never_opened"] += 1
                    pending_refusals.append((
                        station_id, event_date, stage, "chain_never_opened",
                        "P0 未建仓，本阶段无仓可加", 0, "",
                        datetime.now(timezone.utc)
                        .isoformat().replace("+00:00", "Z"),
                        SELECTION_RULE_SHA256,
                    ))
                    continue
                # Why this stage has no seat, kept as the walk goes.  The
                # deepest answer the walk reached is the one the page gets
                # told, not the last one -- see ``_deeper_refusal``.  It starts
                # at "no book" because a stage with no priced decision yields
                # nothing below and would otherwise be reported as nothing at
                # all.
                #
                # The cycle counter is separate now.  It used to be carried in
                # the refusal tuple and re-read as ``refusal[2] + 1``, which
                # only worked while every cycle overwrote the tuple; under
                # depth-keeping a shallower cycle leaves it alone and the count
                # would have frozen.
                refusal = ("no_book_at_decision", "", 0, "")
                cycles = 0
                opened = False
                # (decision_at_utc, windows, band_axis, band_table_sha256)
                stage_candidates: (
                    tuple[str, list, str | None, str | None] | None
                ) = None
                stage_candidates_seated: (
                    tuple[str, list, str | None, str | None] | None
                ) = None
                stage_seated_buckets: tuple[str, ...] | None = None
                lane_opened_at: datetime | None = None
                # Production's clamp compares this cycle's distribution with
                # the one before it, so the walk has to carry the previous
                # cycle forward.  Reset per stage: the first cycle a stage
                # sees has nothing behind it, which is exactly the case the
                # kernel treats as "unmeasured, leave alone".
                previous_probabilities: dict[str, float] | None = None
                # Charter 121 (2026-09-08): production re-solves a seat whose
                # lifted shape charter 93 refuses, so this walk has to be able
                # to ask the same payload a second question rather than drop
                # it.  A queue rather than a plain iterator, because the two
                # things the body carries across payloads -- the cycle counter
                # and the previous cycle's distribution -- would otherwise be
                # advanced twice for one payload and the clamp would compare a
                # cycle with itself.  Both are restored on a re-entry, so a
                # retry differs from the first pass in exactly one input.
                _payload_queue: list[
                    tuple[Mapping[str, Any], tuple[str, ...], Any]
                ] = [
                    (row, (), _PAYLOAD_FIRST_ENTRY)
                    for row in _stage_entry_candidates(
                        conn, station_id, event_date, stage
                    )
                ]
                while _payload_queue:
                    payload, _excluded_candidate_ids, _entry_prev = (
                        _payload_queue.pop(0)
                    )
                    _payload_retry = _entry_prev is not _PAYLOAD_FIRST_ENTRY
                    if _payload_retry:
                        previous_probabilities = _entry_prev
                    else:
                        cycles += 1
                    _payload_entry_prev = previous_probabilities
                    # Research arm: P0 waits out the young book.  Measured
                    # from the lane's own first cycle -- the listing proxy
                    # the ledger can see -- and skipping the cycle, never
                    # the day.
                    if stage == "P0" and P0_ENTRY_DELAY_HOURS > 0:
                        decided_at = datetime.fromisoformat(
                            str(payload["decision_at_utc"])
                            .replace("Z", "+00:00")
                        )
                        if lane_opened_at is None:
                            lane_opened_at = decided_at
                        age_hours = (
                            decided_at - lane_opened_at
                        ).total_seconds() / 3600.0
                        if age_hours < P0_ENTRY_DELAY_HOURS:
                            refusal = _deeper_refusal(refusal, (
                                "entry_window_not_open",
                                "P0 入座窗未开：挂牌后 %.1f 时"
                                " · 窗从 %g 时起（研究臂）" % (
                                    age_hours, P0_ENTRY_DELAY_HOURS,
                                ),
                                cycles, payload["decision_at_utc"],
                            ))
                            continue
                    # Charter 67: P1's one top-up waits for the hours the
                    # daily maximum forms in.  A cycle before the window is
                    # not judged at all -- the walk keeps going, so a stage
                    # with an afternoon cycle still seats there.
                    if stage == "P1" and P1_ENTRY_LOCAL_HOUR > 0:
                        # On the book's clock, which is the compile's clock
                        # (production reads ``snapshot.observed_at``).
                        local_hour = (
                            datetime.fromisoformat(
                                str(payload["observed_at"])
                                .replace("Z", "+00:00")
                            )
                            .astimezone(ZoneInfo(
                                timezones.get(station_id, "UTC")
                            ))
                        )
                        if (
                            local_hour.hour + local_hour.minute / 60.0
                            < P1_ENTRY_LOCAL_HOUR
                        ):
                            refusal = _deeper_refusal(refusal, (
                                "entry_window_not_open",
                                "P1 入座窗未开：该轮本地 %02d:%02d"
                                " · 窗从 %g 时起（宪章 67）" % (
                                    local_hour.hour, local_hour.minute,
                                    P1_ENTRY_LOCAL_HOUR,
                                ),
                                cycles, payload["decision_at_utc"],
                            ))
                            continue
                    # Charter 119: the same evidence rule production runs
                    # before its compile, off the same function, so the
                    # strategy measured is the strategy run.  Read from the
                    # envelope body the payload already carries.
                    if stage == "P1":
                        _p1_lineage = (
                            (payload.get("envelope_payload") or {}).get(
                                "lineage"
                            ) or {}
                        )
                        try:
                            assert_p1_execution_evidence(_p1_lineage)
                        except ContractError as _exc:
                            refusal = _deeper_refusal(refusal, (
                                "p1_execution_evidence",
                                "P1 无自带证据：%s（宪章 119）" % (_exc,),
                                cycles, payload["decision_at_utc"],
                            ))
                            continue
                    quotes = payload["quotes"]
                    observed_at = payload["observed_at"]
                    built = _build_snapshot(
                        station_id=station_id,
                        event_date=event_date,
                        observed_at=observed_at,
                        buckets=payload["buckets"],
                        quotes=quotes,
                    )
                    if built is None:
                        missing = [
                            str(row[0]) for row in payload["buckets"]
                            if str(row[0]) not in quotes
                        ]
                        refusal = _deeper_refusal(refusal, (
                            "incomplete_book", ",".join(missing[:4]),
                            cycles, payload["decision_at_utc"],
                        ))
                        continue
                    snapshot, probabilities, prices, depth = built
                    # Research arm: the calibration layer's centre/width
                    # scalars, applied with production's own transform
                    # before anything -- centre included -- reads the
                    # distribution.  See CENTER_WIDTH_OVERRIDE for why the
                    # placement is an approximation.
                    if CENTER_WIDTH_OVERRIDE.get(stage):
                        delta_c, width_lambda = CENTER_WIDTH_OVERRIDE[stage]
                        ordered = [str(r[0]) for r in payload["buckets"]]
                        centers = bucket_centers_from_intervals(
                            {
                                str(r[0]): (r[2], r[3])
                                for r in payload["buckets"]
                            },
                            ordered_keys=ordered,
                        )
                        probabilities, _cw_receipt = (
                            apply_center_width_calibration(
                                probabilities,
                                delta_c=delta_c,
                                width_lambda=width_lambda,
                                ordered_keys=ordered,
                                key_centers_c=centers,
                            )
                        )
                    # Take the previous cycle's reading, then advance the
                    # carry immediately.  Every cycle this walk examines is
                    # a cycle production also computed a distribution for,
                    # so the carry must move whether or not this one goes
                    # on to seat -- and there are a dozen ``continue``s
                    # below.  Advancing here rather than at each of them is
                    # what makes "the previous cycle" mean the same thing
                    # on both sides.
                    previous_cycle_probabilities = previous_probabilities
                    previous_probabilities = {
                        str(bucket): float(value)
                        for bucket, value in probabilities.items()
                    }
                    if JUDGE != "ledger":
                        # Production's "previous" is the previous committed
                        # checkpoint on the station-day, any stage; the
                        # per-stage carry above is this lane's own walk
                        # order and only serves the ledger judge, whose
                        # corrected vectors the raw checkpoint would not
                        # match.
                        previous_cycle_probabilities = (
                            _previous_checkpoint_probabilities(
                                conn,
                                station_id=station_id,
                                event_date=event_date,
                                decision_at_utc=str(
                                    payload["decision_at_utc"]
                                ),
                            )
                        )
                    ordinals = {
                        row.bucket: int(row.ordinal)
                        for row in snapshot.buckets
                    }
                    # Charter 75: the centre is the mean bucket of the
                    # served distribution; the envelope's model centre
                    # and the argmax are fallbacks for an empty vector.
                    center = (
                        mean_center_bucket_v1(probabilities, ordinals)
                        or payload["center_bucket"]
                        or max(probabilities, key=lambda b: probabilities[b])
                    )
                    if center not in probabilities:
                        center = max(
                            probabilities, key=lambda b: probabilities[b]
                        )
                    core = _entry_support_core(
                        probabilities=probabilities,
                        ordinals=ordinals,
                        center=center,
                    )
                    try:
                        windows, audit = _support_windows(
                            snapshot=snapshot,
                            # There is no incumbent here -- the ledger opens
                            # each station-day fresh -- so the evidence support
                            # is the mass-selected run the continuous-core
                            # resolver would produce, which is what
                            # probability_core computes.  A bare centre made a
                            # one-bucket incumbent, and the incumbent is exempt
                            # from every floor, so the ledger seated windows
                            # the live rule refuses.
                            desired=set(core) or {center},
                            probabilities=probabilities,
                            fee_rate=FEE_RATE,
                            **dict(zip(
                                (
                                    "minimum_support_buckets",
                                    "maximum_support_buckets",
                                ),
                                _support_width_bounds_v1(),
                            )),
                            center_bucket=center,
                            # P1 converges rather than opens, so charter 50a
                            # gives it a floor of two while the opening stages
                            # keep three.  Left unset the enumeration held P1
                            # to the opening floor, which is a rule production
                            # does not run -- the ledger would report a stage
                            # refusing seats the live system takes.
                            probability_stage=stage,
                            # Charter 87: the gate deflates by lambda(a) as
                            # served at this decision's clock.
                            effective_edge_tables=_effective_edge_tables_as_of(
                                payload["decision_at_utc"]
                            ),
                            off_centre_claim_bands=_off_centre_claim_bands(),
                            # Charter 128 v2: the beta lane's lead band, off
                            # the same settlement clock production reads.
                            hours_to_cutoff=hours_to_settlement_if_known(
                                event_date,
                                as_of_utc=payload["decision_at_utc"],
                                settlement_timezone=_settlement_timezone(
                                    conn, station_id, event_date
                                ),
                            ),
                        )
                    except Exception as exc:  # noqa: BLE001 -- scope skipped
                        refusal = _deeper_refusal(refusal, (
                            "selection_failed", f"{type(exc).__name__}",
                            cycles, payload["decision_at_utc"],
                        ))
                        continue
                    # Every window this cycle enumerated, kept until the
                    # stage stops walking and then recorded.  Overwritten
                    # each cycle on purpose: what is wanted is one book per
                    # station-day-stage, and the cycle the walk stops on is
                    # the one that decided -- the seating cycle when it
                    # seated, the last one it looked at when it did not.
                    stage_candidates = (
                        payload["decision_at_utc"], windows,
                        payload.get("band_axis"),
                        payload.get("band_table_sha256"),
                    )
                    # The centre of whichever enumeration ends up seating
                    # this cycle.  This lane's own to begin with; the
                    # production judge reassigns it to production's when
                    # it seats, because the two do not always agree and
                    # every number recorded about a seat has to come from
                    # the enumeration that chose it.
                    seat_center = str(
                        audit.get("ladder_center_bucket") or center
                    )
                    priced_windows = [
                        row for row in windows
                        if row.get("all_in_price") is not None
                        and row.get("edge") is not None
                        # Charter 92: this lane's own judge seats only a
                        # window the gate admitted on the deflated belief.
                        # The enumeration keeps a refused incumbent on the
                        # list flagged inadmissible (charter 78) so the
                        # solver has a window to hold on; seating it here
                        # and ticketing it was a buy no gate had passed.
                        and row.get("admissible", True)
                    ]
                    if WINDOW_STRATEGY != "live":
                        priced_windows = _strategy_windows(
                            WINDOW_STRATEGY,
                            snapshot=snapshot,
                            probabilities=probabilities,
                            center=center,
                            ordinals=ordinals,
                        )
                    if not priced_windows and JUDGE == "ledger":
                        # Name every gate that turned windows away, or the
                        # page reports a refusal whose stated causes do not
                        # add up to it.  The leg floor landed on 2026-08-17
                        # and was not listed here, so 112 P0 refusals were
                        # attributed to a centre-bucket count that could not
                        # explain them.  The edge/friction gate was still
                        # unlisted on 2026-08-20 -- the same disease, half
                        # treated -- and 308 station-days carried counters
                        # that summed to less than the enumeration they were
                        # offered as the explanation of.  The line below is
                        # therefore written as an equation the reader can
                        # check: enumerated = every gate + shortlisted.
                        #
                        # Which gate is a question about this cycle's book;
                        # which cycle is the walk's, and the two are decided
                        # separately.  ``_refusal_reason_for_gate`` reads the
                        # funnel the enumeration itself published, and
                        # ``_deeper_refusal`` then keeps whichever cycle got
                        # furthest down it.  Collapsing the two -- picking the
                        # gate by vote count, or the cycle by recency -- is
                        # what each of the last two misattributions did.
                        #
                        # One caveat the audit cannot cover: under a
                        # non-live WINDOW_STRATEGY the empty shortlist is
                        # the *strategy's* answer, and this audit still
                        # describes the live enumeration -- attributing the
                        # strategy's refusal to the live funnel would be a
                        # third flavour of the same lie.  Research runs get
                        # told so, plainly.
                        if WINDOW_STRATEGY != "live":
                            refusal = _deeper_refusal(refusal, (
                                "no_priced_window",
                                "策略 %s 无可入座窗口（live 闸归因不适用）"
                                % WINDOW_STRATEGY,
                                cycles, payload["decision_at_utc"],
                            ))
                            continue
                        refusal = _deeper_refusal(refusal, (
                            _refusal_reason_for_gate(audit),
                            _window_refusal_detail(audit),
                            cycles, payload["decision_at_utc"],
                        ))
                        continue
                    # Under the production judge the seat below is only
                    # provisional -- production's own resolver replaces
                    # it -- so an empty shortlist here is not a refusal.
                    seat = priced_windows[0] if priced_windows else None
                    # Two prices for the seated basket, and they are not the
                    # same question.  ``seat_crossing_cost`` is what lifting
                    # it off the offers would take -- production's number,
                    # and the upper bound on anything a maker pays.
                    # ``seat_cost`` is what THIS lane pays, which is the one
                    # the stake rule and the refusal copy have to be built
                    # on.  Under taker they are the same number.
                    seat_crossing_cost = (
                        float(seat["all_in_price"]) if seat is not None else 0.0
                    )
                    seat_cost = (
                        _seat_execution_price(
                            seat,
                            snapshot=snapshot,
                            probabilities=probabilities,
                            centre_bucket=seat_center,
                            stage=stage,
                        )
                        if seat is not None else 0.0
                    )
                    # Sized off the book as it stood when the day opened, and
                    # never beyond what the per-scope cap leaves this chain.
                    # The cap is the station-day's, so a top-up spends what
                    # the opening left rather than a fresh allowance of its
                    # own -- three stages each taking 2% is a 6% position in
                    # one outcome, which is the three-book reading wearing
                    # the cap's clothes.
                    equity_at_open = equity
                    # The claim the gate judges.  Honest unconditionally,
                    # it realises 0.60 of itself conditional on trading, so
                    # the gate and the sizing may judge the shaded number
                    # while the row records the raw one.
                    # The signs this cycle enters under.  Computed for every
                    # stage, because two different rules read them and their
                    # scopes are not the same.
                    stage_consolidation = _consolidation_signals(
                        conn,
                        station_id=station_id,
                        event_date=event_date,
                        entry_decision_at_utc=payload["decision_at_utc"],
                        probabilities=probabilities,
                        ordinals=ordinals,
                        quotes=quotes,
                    )
                    # Wait for a less-consolidated moment rather than act
                    # into one the market has finished pricing.  Measured
                    # read-only on the settled book: with a sign lit at
                    # entry the stratum returns -50% to -55%, and unlit
                    # +40% to +44% -- this asks what acting on that is
                    # worth, which the read-only table cannot say.
                    #
                    # Every stage, not just the opener (2026-08-24).  This
                    # gate lived under ``if stage == "P0"`` and so measured
                    # a narrower rule than the one production runs: the
                    # live gate sits in ``compile_delta_v2_runtime_plan``
                    # with no stage condition at all, and the stage it
                    # judges is whatever ``select_current_stage`` returns --
                    # P0 while the day is still opening, P0-M or P1 once
                    # that evidence lands.  So in production a lit sign
                    # refuses an opening at P0 and refuses a *top-up* at
                    # P0-M/P1, leaving the held position where it stands;
                    # the ledger only ever modelled the first of those.
                    # A ledger that scores a rule production does not run
                    # cannot answer whether the rule is worth keeping,
                    # which is the whole reason this book exists.
                    #
                    # One difference deliberately survives: the history
                    # this reads is P0-only (``_consolidation_signals``
                    # filters ``stage='P0'``) while production's
                    # ``_earlier`` walks the envelope chain unfiltered, so
                    # a P1 decision there can call the centre stable off a
                    # run that includes P0-M/P1 centres.  Closing that too
                    # would change the signal and the scope in one pass and
                    # neither could be attributed; it is named here and
                    # measured separately.
                    # The opposite question, asked in the same breath: not
                    # "has the market finished pricing this window" but
                    # "did it ever price it at all".  Off by default; see
                    # REQUIRE_QUOTED_BOOK for the measurement and why nine
                    # chains do not yet carry a rule.
                    if (
                        REQUIRE_QUOTED_BOOK
                        and stage_consolidation.get("quoted") is not True
                    ):
                        refusal = _deeper_refusal(refusal, (
                            "unquoted_ideal_window",
                            "理想窗口未被报价：中心±1 的质量不足 %.0f%% 有 ≥%g 卖价"
                            % (100 * CONSOLIDATION_QUOTED_FRACTION,
                               MINIMUM_LEG_ALL_IN_ASK),
                            cycles, payload["decision_at_utc"],
                        ))
                        continue
                    if CERTAINTY_GATE and any(
                        stage_consolidation.get(name) is True
                        for name in CERTAINTY_GATE
                    ):
                        refusal = _deeper_refusal(refusal, (
                            "certainty_gate_lit",
    "shape_gap_too_wide",
                            "确定性闸：%s 已亮" % "+".join(
                                name for name in CERTAINTY_GATE
                                if stage_consolidation.get(name) is True
                            ),
                            cycles, payload["decision_at_utc"],
                        ))
                        continue
                    shape_tilt = 1.0
                    # Opening stages only.  The first (stage-blind) run of
                    # this arm cost P1 nineteen seats and 144u of profit --
                    # P1's edge is the observation, not the shape, and the
                    # agreement table shows the same split signal *inverts*
                    # there: P1 earns precisely where the market disagrees.
                    if SHAPE_GAP_LIMIT_BUCKETS > 0 and stage != "P1":
                        _pr = {
                            b: float(v) for b, v in probabilities.items()
                            if float(v) > 0
                        }
                        if len(_pr) >= 3:
                            _ords = {
                                r.bucket: int(r.ordinal)
                                for r in snapshot.buckets
                            }
                            _tot = sum(_pr.values())
                            _mean = sum(
                                _ords[b] * v / _tot for b, v in _pr.items()
                                if b in _ords
                            )
                            _mode = max(_pr, key=lambda b: _pr[b])
                            _gap = abs(_mean - _ords.get(_mode, _mean))
                            if _gap > SHAPE_GAP_LIMIT_BUCKETS:
                                if SHAPE_GAP_TILT > 0:
                                    shape_tilt = SHAPE_GAP_TILT
                                else:
                                    refusal = _deeper_refusal(refusal, (
                                        "shape_gap_too_wide",
                                        "分布撕裂：均值离众数 %.2f 桶（门 %g）"
                                        % (_gap, SHAPE_GAP_LIMIT_BUCKETS),
                                        cycles,
                                        payload["decision_at_utc"],
                                    ))
                                    continue
                    # The opener's own entry signals decide the chain's
                    # stake scale; later stages inherit rather than
                    # re-earn -- the strata were measured on the P0 count.
                    # Unlike the gate above, this stays P0-scoped: it is a
                    # measured stake tilt, not production's rule.
                    seat_consolidation = None
                    if stage == "P0" and not held:
                        # The opener earns its own tilt.  Production records
                        # that factor once (``record_chain_opener_tilt``,
                        # INSERT OR IGNORE at the first authorized P0
                        # decision) and every later compile on the day --
                        # a P0 reseat as much as P0-M or P1 -- inherits it
                        # outright (``load_chain_tilt``).  This lane re-read
                        # each P0 cycle's own lights and then let the reseat
                        # overwrite the chain's factor, so a chain that
                        # opened untilted could be tilted by its own third
                        # P0 seat and hand that to P1 (aligned 2026-09-04).
                        seat_consolidation = stage_consolidation
                        lit = sum(
                            1 for name in CONSOLIDATION_TILT_SIGNALS
                            if seat_consolidation.get(name) is True
                        )
                        tilt = (
                            CONSOLIDATION_TILT_FACTOR
                            if lit >= CONSOLIDATION_TILT_MIN_SIGNALS
                            else 1.0
                        )
                    elif CONSOLIDATION_TILT_SCOPE == "stage":
                        lit = sum(
                            1 for name in CONSOLIDATION_TILT_SIGNALS
                            if stage_consolidation.get(name) is True
                        )
                        tilt = (
                            CONSOLIDATION_TILT_FACTOR
                            if lit >= CONSOLIDATION_TILT_MIN_SIGNALS
                            else 1.0
                        )
                    else:
                        tilt = chain_tilt
                    tilt = tilt * shape_tilt
                    # The production judge arm.  It runs here, after the
                    # tilt is known, because production tilts the capital
                    # it hands the solver; the seat chosen above was this
                    # lane's and is replaced by production's, priced the
                    # same way so every later number reads unchanged.
                    production_verdict = None
                    # Reset per seat beside the verdict it is read from: a
                    # flag left standing from the previous cycle would make
                    # one seat's execution family another seat's.
                    seat_completion_take = False
                    if JUDGE in ("production", "production_seat"):
                        verdict = _production_judge(
                            conn,
                            station_id=station_id,
                            event_date=event_date,
                            stage=stage,
                            decision_at_utc=payload["decision_at_utc"],
                            payload=payload,
                            quotes=quotes,
                            probabilities=probabilities,
                            center=center,
                            core=core,
                            held=held,
                            held_cost=held_cost,
                            equity=equity_at_open,
                            chain_stake=chain_stake,
                            tilt=tilt,
                            last_decision_signature=(
                                chain_decision_signatures.get(stage)
                            ),
                            excluded_candidate_ids=(
                                _excluded_candidate_ids
                            ),
                            # Charter 71(a): the identity the chain last
                            # bought under, as production reads it off
                            # its last acted decision.
                            last_acted_probability_identity=(
                                chain_probability_identity or None
                            ),
                            # Charter 136 (五): the legs this chain posted.
                            chain_seat_buckets=tuple(sorted(chain_intended)),
                            # Charter 143 v2: and the size each was posted at.
                            chain_sent_shares=dict(chain_intended_shares),
                        )
                        counts["production_judge_cycles"] += 1
                        # Charter 104: the enumeration that DECIDED.  The
                        # ledger lane ran its own a few hundred lines up and
                        # that one is recorded until this point; when this
                        # judge is in force it decides nothing, and a fit
                        # reading it would be reading a shortlist the gate
                        # never saw.  Taken before the refusal branches
                        # below, because a refused stage's enumeration is
                        # exactly the evidence this population exists for.
                        _judged = verdict.get("enumerated_windows")
                        if _judged:
                            stage_candidates = (
                                payload["decision_at_utc"], _judged,
                                payload.get("band_axis"),
                                payload.get("band_table_sha256"),
                            )
                        _signature = verdict.get("decision_signature")
                        if isinstance(_signature, dict) and (
                            (verdict.get("refusal") or ("", ""))[0]
                            != "no_decision_moment"
                        ):
                            chain_decision_signatures[stage] = _signature
                        if verdict.get("refusal"):
                            _reason, _detail = verdict["refusal"]
                            counts["production_judge_cycles_" + _reason] += 1
                            if _reason == "no_decision_moment":
                                # Charter 77: this cycle decided nothing
                                # and stands on the stage's last recorded
                                # decision, whose refusal is already in
                                # the slot.  Counted, never reported.
                                continue
                            refusal = _deeper_refusal(refusal, (
                                _reason, _detail,
                                cycles, payload["decision_at_utc"],
                            ))
                            continue
                        # The centre of the enumeration that chose this
                        # seat, which under the production judge is
                        # production's own and not always this lane's:
                        # ``_choose_support`` reads the envelope (model
                        # centre, dispersion, rung width) that this lane
                        # never opens.  Every consumer below -- the
                        # off-centre pricing, the clamp's guard, the
                        # recorded row -- reads it rather than the
                        # ledger's, which named a centre outside its own
                        # window on seven P1 rows of the 2026-08-15 book.
                        seat_center = str(
                            verdict.get("ladder_center_bucket") or center
                        )
                        _row = _window_row(
                            verdict["buckets"],
                            snapshot=snapshot,
                            probabilities=probabilities,
                            centre_bucket=seat_center,
                        )
                        if _row is None:
                            refusal = _deeper_refusal(refusal, (
                                "no_priced_window",
                                "生产裁判入座 %s，本簿无法定价"
                                % "+".join(verdict["buckets"]),
                                cycles, payload["decision_at_utc"],
                            ))
                            continue
                        if (
                            JUDGE == "production"
                            and float(verdict["delta_cost_usdc"]) <= 0.0
                        ):
                            refusal = _deeper_refusal(refusal, (
                                *_production_hold_refusal(
                                    verdict, held=held,
                                    chain_stake=chain_stake,
                                ),
                                cycles, payload["decision_at_utc"],
                            ))
                            continue
                        counts["production_judge_cycles_seated"] += 1
                        _origin = str(
                            (_row or {}).get("winner_candidate_origin")
                            or verdict.get("winner_candidate_origin")
                            or "UNSPECIFIED"
                        )
                        counts["seated_origin_" + _origin] = counts.get(
                            "seated_origin_" + _origin, 0
                        ) + 1
                        seat = _row
                        # Charter 127 (五): a designated completion crosses.
                        # Production sends the short rung as a limit
                        # fill-or-kill; this book rested it like any other
                        # leg, so from the day that clause shipped the two
                        # sides were simulating different execution on 392 of
                        # 924 seats (measured 2026-09-11 over 08-28..09-08).
                        # The flag is per SEAT, not per run: one basket may
                        # carry both families now.
                        seat_completion_take = (
                            _CHARTER_127_COMPLETION_TAKE == "on"
                            and _origin == _LADDER_BALANCE_ORIGIN
                        )
                        seat_crossing_cost = float(seat["all_in_price"])
                        seat_cost = _seat_execution_price(
                            seat,
                            snapshot=snapshot,
                            probabilities=probabilities,
                            centre_bucket=seat_center,
                            stage=stage,
                        )
                        if JUDGE == "production":
                            production_verdict = verdict
                    gate_win = float(seat["win_probability"]) * (
                        SELECTION_DISCOUNT if SELECTION_DISCOUNT > 0 else 1.0
                    )
                    leg_veto_hit = None
                    if (
                        production_verdict is None
                        and EFFECTIVE_EDGE_MODE == "on"
                        and (
                            stage in EFFECTIVE_EDGE_STAGES
                            or stage in EFFECTIVE_EDGE_P0M_STAGES
                            or stage in EFFECTIVE_EDGE_P1_STAGES
                        )
                        and gate_win > 0
                    ):
                        p1_day_table = (
                            P1_EFFECTIVE_EDGE_TABLE.get(str(event_date))
                            if P1_EFFECTIVE_EDGE_TABLE
                            else None
                        )
                        discount = (
                            SELECTION_DISCOUNT if SELECTION_DISCOUNT > 0
                            else 1.0
                        )
                        # Each rung's own price and claim, for the bucket
                        # scope and for the veto arm alike.
                        leg_rows = {
                            str(item.bucket): item for item in snapshot.buckets
                        }
                        leg_prices = {}
                        for leg in seat["buckets"]:
                            leg_row = leg_rows.get(str(leg))
                            leg_price = (
                                _execution_price(leg_row)
                                if leg_row is not None else None
                            )
                            if leg_price is not None:
                                leg_prices[str(leg)] = float(leg_price)
                        if EFFECTIVE_EDGE_SCOPE == "bucket":
                            deflated = {}
                            for leg in seat["buckets"]:
                                claim_leg = float(
                                    probabilities.get(str(leg), 0.0) or 0.0
                                )
                                price_leg = leg_prices.get(str(leg))
                                lam_leg = (
                                    effective_edge_bucket_lambda_for_stage(
                                        stage,
                                        price_leg / claim_leg,
                                        p1_table=p1_day_table,
                                    )
                                    if price_leg is not None
                                    and claim_leg > 0
                                    else 1.0
                                )
                                deflated[str(leg)] = claim_leg * lam_leg
                            gate_win = discount * sum(deflated.values())
                        else:
                            # The same dispatch production calls.  A replay
                            # arm may hand P1 a per-day table by path;
                            # production and the default run read the
                            # kernel's P1 constant.
                            served_tables = dict(
                                _effective_edge_tables_as_of(
                                    payload["decision_at_utc"]
                                )
                            )
                            if p1_day_table:
                                served_tables["P1"] = p1_day_table
                            lam_window, _lam_src = (
                                effective_edge_lambda_resolved(
                                    stage,
                                    claim=gate_win,
                                    price=float(seat_cost),
                                    tables=served_tables,
                                )
                            )
                            gate_win = gate_win * lam_window
                            deflated = {
                                str(leg): float(
                                    probabilities.get(str(leg), 0.0) or 0.0
                                ) * lam_window
                                for leg in seat["buckets"]
                            }
                        if LEG_VETO == "on":
                            for leg in seat["buckets"]:
                                price_leg = leg_prices.get(str(leg))
                                if (
                                    price_leg is not None
                                    and price_leg > deflated.get(str(leg), 0.0)
                                    + 1e-9
                                ):
                                    leg_veto_hit = (
                                        str(leg), price_leg,
                                        deflated.get(str(leg), 0.0),
                                    )
                                    break
                    if leg_veto_hit is not None:
                        refusal = _deeper_refusal(refusal, (
                            "negative_edge_window",
                            "逐腿否决：%s 卖价 %.3f > 折后主张 %.3f"
                            % leg_veto_hit,
                            cycles,
                            payload["decision_at_utc"],
                        ))
                        continue
                    if production_verdict is not None:
                        # Production sized it: the increment's cost at
                        # the offer, inside the capital the solver was
                        # handed.  No Kelly and no second clamp -- both
                        # are inside the objective that chose it.
                        fraction = 0.0
                        wanted_stake = float(
                            production_verdict["delta_cost_usdc"]
                        )
                        stage_budget = (
                            equity_at_open * MAX_STAKE_FRACTION_PER_SCOPE
                        )
                        stage_room = float(production_verdict["capital_usdc"])
                        stake = wanted_stake
                    else:
                        fraction, wanted_stake = _kelly_stake(
                            win_probability=gate_win,
                            cost=seat_cost,
                            equity=equity_at_open,
                        )
                        stage_budget = (
                            equity_at_open * MAX_STAKE_FRACTION_PER_SCOPE
                        )
                        stage_room = stage_budget * tilt - chain_stake
                        # Charter 137: the level reads off the kernel's
                        # basis.  ``net`` returns the room and reproduces
                        # the pre-clause number exactly; ``gross`` returns
                        # the stage's whole allowance, and the room comes
                        # back as a bound on the increment below.
                        stake = min(
                            wanted_stake,
                            kelly_target_budget_usdc_v1(
                                book_equity_usdc=equity_at_open,
                                stage=stage,
                                capital_available_usdc=max(0.0, stage_room),
                            ),
                        )
                        # A tilted stake floors at one minimum clip per leg:
                        # below that the venue refuses the order and the tilt
                        # becomes a gate.  The floor never lifts the stake
                        # above the untilted budget or the Kelly ask -- it
                        # only stops the cut from cutting past executability.
                        if 0.0 < tilt < 1.0 and stake > 0.0:
                            stake = min(
                                wanted_stake,
                                max(0.0, stage_budget - chain_stake),
                                max(
                                    stake,
                                    _tilt_minimum_clip(conn)
                                    * len(seat["buckets"]),
                                ),
                            )
                    if stake <= 0.0:
                        # Whichever price the stage's basis judged on, the
                        # OTHER one is the context the reader needs.  Under
                        # the crossing basis (the rule since 2026-08-27) the
                        # parenthetical is the resting band this lane could
                        # have posted -- and the copy says why that number
                        # did not save the seat: the open ask is the market's
                        # judgement, and a basket that is only +EV at the
                        # hoped-for fill price has already been priced out.
                        # Under a lane-priced stage (P0-M) the parenthetical
                        # stays the crossing total, the honest upper bound.
                        cost = seat_cost
                        if EXECUTION_MODE == "taker":
                            crossing_note = ""
                        elif abs(cost - seat_crossing_cost) < 5e-4:
                            resting_cost = sum(_maker_leg_prices(
                                snapshot,
                                seat["buckets"],
                                probabilities,
                                centre_bucket=seat_center,
                            ))
                            crossing_note = (
                                ""
                                if abs(resting_cost - cost) < 5e-4
                                else (
                                    "（本可挂 %.3f——要价本身说明不该买）"
                                    % resting_cost
                                )
                            )
                        else:
                            crossing_note = (
                                "（吃单 %.3f）" % seat_crossing_cost
                            )
                        # Only say the budget stopped it when the budget
                        # stopped it.  This used to fire on any chain that
                        # already held something, so a stage refused on a
                        # negative Kelly -- or on a basket dearer than its
                        # own payout -- was told its allowance had run out.
                        # Measured 2026-08-20: all 78 refusals carrying the
                        # line were one of those two, none had run out of
                        # room, and the smallest remaining allowance among
                        # them was 5.45 against a chain that had spent 1.08.
                        exhausted = (
                            " · %s 段额度已用尽（本链已投 %.2f）"
                            % (stage, chain_stake)
                            if wanted_stake > 0.0 and stage_room <= 0.0
                            else ""
                        )
                        # A bucket nobody is offering arrives here priced at
                        # the unfillable ceiling (``_build_snapshot`` must
                        # hand the kernel a representable book, and the
                        # snapshot schema requires ask_available to agree with
                        # a non-empty level list).  That ceiling is a
                        # placeholder, not a price, so a basket carrying one
                        # is unbuyable rather than expensive -- reporting it
                        # as "dearer than its payout" produced baskets
                        # "costing" 2.997 and 3.996, which no book asked for.
                        # Measured 2026-08-20: 13 of the 102, and every
                        # extreme value among them.
                        unoffered = [
                            str(bucket) for bucket in seat["buckets"]
                            if not (quotes.get(str(bucket)) or {}).get(
                                "ask_levels"
                            )
                        ]
                        if unoffered:
                            refusal = _deeper_refusal(refusal, (
                                "no_priced_window",
                                "%d 条腿无人挂卖（%s）%s" % (
                                    len(unoffered),
                                    "、".join(unoffered[:3]),
                                    exhausted,
                                ),
                                cycles, payload["decision_at_utc"],
                            ))
                            continue
                        # A basket that asks more than the 1.00 it can pay is
                        # refused by arithmetic, not by the objective -- the
                        # model's probability never enters it.  Reporting that
                        # as "the objective declined" credits us with a
                        # judgement the price did not leave room for.
                        if cost >= 1.0:
                            refusal = _deeper_refusal(refusal, (
                                "cost_exceeds_payout",
                                "整篮成本 %.3f · 满额赔付 1.000"
                                "（超出 %.1f%%）%s%s" % (
                                    cost, (cost - 1.0) * 100.0,
                                    crossing_note, exhausted,
                                ),
                                cycles, payload["decision_at_utc"],
                            ))
                            continue
                        refusal = _deeper_refusal(refusal, (
                            "no_positive_kelly",
                            # The seat's own buckets, not a near-miss: this
                            # branch had a basket, priced it, and Kelly sized
                            # it at zero.  Naming it turns "赔率不划算" from a
                            # verdict into a verdict about something.
                            "目标篮 %s · 概率 %.3f · 成本 %.3f%s%s" % (
                                "+".join(
                                    str(bucket) for bucket in seat["buckets"]
                                ),
                                gate_win,
                                cost, crossing_note, exhausted,
                            ),
                            cycles, payload["decision_at_utc"],
                        ))
                        continue
                    # A complete basket buys the same share count on every leg
                    # (atomicity), so the size is set by the thinnest ladder --
                    # and the price by how deep into each the clip has to
                    # reach.  Charging the whole clip the top-of-book price is
                    # what let a 30-share offer at one tick "fill" 24,213
                    # shares at 0.004 when walking the ladders for that size
                    # costs 3.06 a set, three times anything the basket can pay
                    # out.
                    legs = [
                        depth.get(bucket) or [] for bucket in seat["buckets"]
                    ]
                    if EXECUTION_MODE != "taker" and not seat_completion_take:
                        # A resting order fills at its own price, not by
                        # walking the offer ladder, so the ladder is replaced
                        # by a single level at the assumed maker price.  Its
                        # size is still bounded by the visible offer depth --
                        # not because a maker consumes it, but because it is
                        # the only measure of how much trades here at all,
                        # and letting the clip run past it would reproduce
                        # the 24,213-shares-at-0.004 fiction in a new place.
                        legs = [
                            [{
                                "price": float(price),
                                "shares": sum(
                                    level["shares"]
                                    for level in (depth.get(bucket) or [])
                                ),
                            }]
                            for bucket, price in zip(
                                seat["buckets"],
                                _maker_leg_prices(
                                    snapshot,
                                    seat["buckets"],
                                    probabilities,
                                    centre_bucket=seat_center,
                                ),
                            )
                        ]
                    capacity = min(
                        (
                            sum(level["shares"] for level in leg)
                            for leg in legs
                        ),
                        default=0.0,
                    )
                    per_leg: dict[str, float] = {}
                    weights = None
                    if ALLOCATION_SHAPE == "edge_weighted":
                        # Production's shape, run from production's kernel.
                        # ``_leg_ask`` is the same offer the seat is priced
                        # against, so the agreement this reads is the one the
                        # live lane reads.
                        _prices = {
                            str(b): _leg_ask(quotes, b)
                            for b in seat["buckets"]
                        }
                        _realisable = {}
                        for b in seat["buckets"]:
                            _claim = float(probabilities.get(b, 0.0) or 0.0)
                            _a = (
                                _prices[str(b)] / _claim if _claim > 1e-12
                                else float("inf")
                            )
                            _realisable[str(b)] = (
                                _claim
                                * effective_edge_bucket_lambda_for_stage(
                                    str(stage or ""), _a
                                )
                            )
                        _w = floor_first_edge_weights_v1(
                            ordered=[str(b) for b in seat["buckets"]],
                            prices=_prices,
                            effective_claims=_realisable,
                        )
                        weights = [str(b) for b in seat["buckets"]]
                        weights = [_w[b] for b in weights]
                    elif ALLOCATION_SHAPE == "centre_tilt":
                        weights = _centre_tilt_weights(
                            seat["buckets"],
                            centre_bucket=seat_center,
                            probabilities=probabilities,
                            prices=[
                                _leg_ask(quotes, b) for b in seat["buckets"]
                            ],
                            ordinals={
                                str(row[0]): int(row[1])
                                for row in payload["buckets"]
                            },
                        )
                    resting_fills: dict[str, str] = {}
                    if EXECUTION_MODE == "maker_resting" and not seat_completion_take:
                        by_row = {r.bucket: r for r in snapshot.buckets}
                        seat_centre = seat_center
                        ladders = []
                        for b in seat["buckets"]:
                            if str(b) not in by_row:
                                ladders.append([])
                                continue
                            row_b = by_row[str(b)]
                            cap_b = _leg_value_cap(
                                str(b),
                                probabilities=probabilities,
                                centre_bucket=seat_centre,
                                window=seat["buckets"],
                                market_top_bucket=_market_top_bucket(
                                    seat["buckets"], by_row),
                                all_in_ask=(
                                    float(row_b.ask_price)
                                    if row_b.ask_available
                                    else None
                                ),
                            )
                            rungs = _resting_ladder(row_b, value_cap=cap_b)
                            if MAKER_RESTING_ANCHORED and rungs:
                                # Charter 94: as many rungs as the leg's
                                # planned shares hold whole lots for, the
                                # way production's candidate loop counts
                                # them off the gap.
                                _planned = (
                                    float(stake) / float(seat_cost)
                                    if float(seat_cost) > 0.0 else 0.0
                                )
                                rungs = bid_anchored_rungs_for_gap(
                                    rungs=rungs,
                                    gap_shares=_planned,
                                    min_lot_shares=float(
                                        getattr(row_b, "min_order_size", 0.0)
                                        or EXCHANGE_MINIMUM_LOT_SHARES_FALLBACK
                                    ),
                                    minimum_notional_usdc=(
                                        EXCHANGE_MINIMUM_ORDER_NOTIONAL_USDC
                                        if CHARTER_83_MIN_NOTIONAL == "on"
                                        else 0.0
                                    ),
                                )
                            # The hybrid arm: the centre's whole ladder
                            # posts at min(entry ask, its value cap) --
                            # taking inside value, resting at the red
                            # line above it.
                            if (
                                MAKER_CENTRE_TAKER
                                and str(b) == seat_centre
                                and rungs
                                and row_b.ask_available
                            ):
                                take = float(row_b.ask_price)
                                if cap_b is not None:
                                    take = min(take, float(cap_b))
                                if take >= MINIMUM_LEG_ALL_IN_ASK:
                                    rungs = [take] * len(rungs)
                            ladders.append(rungs)
                        # Built here, where the truth already is, so the
                        # walk below receives a function and never the
                        # outcome itself.  ``None`` under every other fill
                        # model, which leaves that walk byte-identical.
                        _fill_admits = None
                        if MAKER_FILL_MODEL == "adverse":
                            _standing = truth.get(
                                (station_id, event_date)
                            )
                            _landed = None
                            if _standing is not None:
                                _hit = _settled_bucket(
                                    list(payload["buckets"]),
                                    _standing.tmax_c,
                                )
                                _landed = None if _hit is None else _hit[0]
                            _fill_admits = adverse_fill_predicate(
                                station_id=station_id,
                                event_date=event_date,
                                settled_bucket=_landed,
                            )
                        got, missing, withdrawn = _resting_ladder_fill(
                            conn,
                            fill_admits=_fill_admits,
                            settlement_timezone=timezones.get(
                                station_id, "UTC"
                            ),
                            station_id=station_id,
                            event_date=event_date,
                            buckets=seat["buckets"],
                            ladders=ladders,
                            market_top_bucket=_market_top_bucket(
                                seat["buckets"], by_row),
                            placed_at_utc=payload["decision_at_utc"],
                            # The entry claim seeds the belief timeline;
                            # the walk re-reads the freshest one at every
                            # re-pricing round.
                            probabilities=probabilities,
                            cutoff_local_minutes=peak_cutoffs.get(
                                station_id
                            ),
                            centre_bucket=seat_centre,
                        )
                        if not any(got.values()):
                            # Withdrawing and never being reached both end
                            # with nothing bought and used to read the same,
                            # but they call for opposite actions: one is our
                            # own evidence turning against the basket, the
                            # other is the market never coming down to us.
                            if withdrawn:
                                refusal = _deeper_refusal(refusal, (
                                    "belief_withdrew_order",
                                    "挂 %d 腿 · 信念转负后全撤，一档未成交"
                                    % len(seat["buckets"]),
                                    cycles, payload["decision_at_utc"],
                                ))
                                continue
                            refusal = _deeper_refusal(refusal, (
                                "resting_unfilled",
                                "挂 %d 腿 · 全程一档未成交（%s）" % (
                                    len(seat["buckets"]),
                                    _resting_deadline_reason(
                                        placed_at_utc=payload[
                                            "decision_at_utc"
                                        ],
                                        settlement_timezone=timezones.get(
                                            station_id, "UTC"
                                        ),
                                        event_date=event_date,
                                        cutoff_local_minutes=(
                                            peak_cutoffs.get(station_id)
                                        ),
                                        belief_fresh_until_utc=(
                                            _belief_fresh_until(
                                                conn,
                                                station_id=station_id,
                                                event_date=event_date,
                                                placed_at_utc=payload[
                                                    "decision_at_utc"
                                                ],
                                            )
                                        ),
                                    ),
                                ),
                                cycles, payload["decision_at_utc"],
                            ))
                            continue
                        # Each leg pays the average of the rungs it got.
                        # That average is the point of resting across a band
                        # rather than at a price: the cheap rungs pull it
                        # below the single price a point order would have
                        # had to pick, and no quote in the book equals it.
                        resting_prices = {
                            str(b): sum(got[str(b)]) / len(got[str(b)])
                            * (1.0 + FEE_RATE)
                            for b in seat["buckets"]
                            if got.get(str(b))
                        }
                        resting_fraction = {
                            str(b): (
                                len(got[str(b)]) / len(ladder)
                                if ladder else 0.0
                            )
                            for b, ladder in zip(seat["buckets"], ladders)
                        }
                        # Every intended leg is recorded, including the ones
                        # that bought nothing -- "which rung never filled" is
                        # the number the page needs, and it is invisible if
                        # the empty legs are dropped here.
                        resting_fills = {
                            str(b): "%d/%d@%.4f" % (
                                len(got.get(str(b)) or []), len(ladder),
                                (
                                    sum(got[str(b)]) / len(got[str(b)])
                                    if got.get(str(b)) else 0.0
                                ),
                            )
                            for b, ladder in zip(seat["buckets"], ladders)
                            if ladder
                        }
                        # Sizing is done on the order that was *posted*, so
                        # a leg that filled nothing is still priced at its
                        # ladder's mean here -- what it would have paid.  The
                        # realised price only replaces it for the legs that
                        # bought something, and the rest drop out below.
                        posted = _maker_leg_prices(
                            snapshot,
                            seat["buckets"],
                            probabilities,
                            centre_bucket=seat_centre,
                        )
                        legs = [
                            [{
                                "price": resting_prices.get(
                                    str(bucket), float(price)
                                ),
                                "shares": sum(
                                    level["shares"]
                                    for level in (depth.get(bucket) or [])
                                ),
                            }]
                            for bucket, price in zip(seat["buckets"], posted)
                        ]
                        capacity = min(
                            (
                                sum(level["shares"] for level in leg)
                                for leg in legs
                            ),
                            default=0.0,
                        )
                    if production_verdict is not None:
                        sized = _production_sized_legs(
                            legs=legs,
                            buckets=seat["buckets"],
                            target=production_verdict["target_shares"],
                            held=held,
                            fee_rate=FEE_RATE,
                        )
                        if sized is None:
                            refusal = _deeper_refusal(refusal, (
                                "no_depth",
                                "生产裁判目标 %s 在本簿深度内买不齐"
                                % "+".join(seat["buckets"]),
                                cycles, payload["decision_at_utc"],
                            ))
                            continue
                        leg_shares, stake, leg_cash = sized
                        if (
                            seat_completion_take
                            and float(stake)
                            > float(production_verdict["delta_cost_usdc"]) + 1e-9
                        ):
                            # Charter 127 (五)'s cap, on this side too: a take
                            # may not pay more than the cash the gates
                            # authorized.  Production refuses the candidate by
                            # name (``completion_take_above_authorized_cost``);
                            # the book has to refuse it too, or it scores a
                            # completion at a price production would never
                            # have sent -- which is the divergence this
                            # mirror exists to close, pointing the other way.
                            refusal = _deeper_refusal(refusal, (
                                "completion_take_above_authorized_cost",
                                "补齐越价走深度 %.4f 高于授权 %.4f"
                                % (
                                    float(stake),
                                    float(production_verdict["delta_cost_usdc"]),
                                ),
                                cycles, payload["decision_at_utc"],
                            ))
                            continue
                    elif weights is None:
                        filled = _fillable_size(
                            legs=legs,
                            win_probability=float(
                                seat["win_probability"]
                            ),
                            budget=stake,
                            fee_rate=FEE_RATE,
                            capacity=capacity,
                        )
                        if filled is None:
                            refusal = _deeper_refusal(refusal, (
                                "no_depth", "最薄一档 %.0f 份" % capacity,
                                cycles, payload["decision_at_utc"],
                            ))
                            continue
                        shares, stake, leg_cash = filled
                        leg_shares = [shares] * len(seat["buckets"])
                    else:
                        weighted = _fillable_weighted(
                            legs=legs,
                            weights=weights,
                            probabilities=[
                                float(probabilities.get(b, 0.0))
                                for b in seat["buckets"]
                            ],
                            budget=stake,
                            fee_rate=FEE_RATE,
                        )
                        if weighted is None:
                            refusal = _deeper_refusal(refusal, (
                                "no_depth",
                                "加权分配无可行规模 · 最薄一档 %.0f 份"
                                % capacity,
                                cycles, payload["decision_at_utc"],
                            ))
                            continue
                        leg_shares, stake, leg_cash = weighted
                    # What was chosen and posted, before the book had a say.
                    # The charter's minimum support is a rule about
                    # selection, so it has to stay auditable even though the
                    # recorded window is now whatever actually filled.
                    posted_buckets = list(seat["buckets"])
                    # Top-ups only add.  The stage chose a target position;
                    # what it may buy is the part of that target the chain
                    # does not already hold, and a rung it wants less of
                    # than it has is simply left alone (user ruling,
                    # 2026-08-18).  P0 opens with an empty book, so this is
                    # the identity there and binds only on the later stages.
                    held_before = {
                        bucket: held[bucket]
                        for bucket in seat["buckets"] if bucket in held
                    }
                    # Production's declining-probability clamp, run on the
                    # same gap it runs on there: what this stage wants less
                    # what the chain already holds.  Applied before the
                    # top-up walk so both entry paths see it -- an opening
                    # holds nothing, so a clamped leg goes to zero shares
                    # and its cash leaves the stake; a top-up already holds
                    # some, so the leg is pinned at what it has and the
                    # delta below is zero.  Both are "held at the size it
                    # already has", which is what the clamp means.
                    if DECLINE_CLAMP != "off" and previous_cycle_probabilities:
                        _gaps = {
                            bucket: max(0.0, float(want) - held.get(bucket, 0.0))
                            for bucket, want in zip(seat["buckets"], leg_shares)
                        }
                        _, _declined = clamp_declining_gaps_v2(
                            gap_by_bucket=_gaps,
                            support=tuple(seat["buckets"]),
                            probabilities=probabilities,
                            previous_probabilities=(
                                previous_cycle_probabilities
                            ),
                            minimum_drop=DECLINE_MIN_DROP,
                        )
                        if DECLINE_CLAMP == "guarded" and _declined:
                            _declined = contiguous_edge_clamp_v2(
                                declining=_declined,
                                support=tuple(seat["buckets"]),
                                centre_bucket=seat_center,
                            )
                        if DECLINE_CLAMP == "after_open" and _declined:
                            # Production's ``after_open``: the opening
                            # seat (chain holds nothing) is never clamped;
                            # every later seat is, held rung or not.
                            if not any(
                                float(v or 0.0) > 0.0 for v in held.values()
                            ):
                                _declined = ()
                        if DECLINE_CLAMP == "held_only" and _declined:
                            # Production's ``held_only``: only a rung the
                            # chain already holds can be held at what it
                            # has.  An opening holds nothing, so it is
                            # never clamped; a top-up clamps every falling
                            # rung it holds, centre included.
                            _declined = tuple(
                                bucket for bucket in _declined
                                if held.get(bucket, 0.0) > 0.0
                            )
                        if _declined and DECLINE_CLAMP == "hold":
                            # The whole basket waits.  A target the evidence
                            # is moving away from in any leg is not a basket
                            # this cycle should open a piece of; the next
                            # cycle re-solves and may seat the whole thing.
                            refusal = _deeper_refusal(refusal, (
                                "decline_clamp_hold_basket",
                                "目标篮 %s · 其中 %s 概率较上一 cycle 下降，"
                                "整篮不入座" % (
                                    "+".join(seat["buckets"]),
                                    "+".join(sorted(_declined)),
                                ),
                                cycles, payload["decision_at_utc"],
                            ))
                            continue
                        if _declined:
                            _drop = set(_declined)
                            _shares: list[float] = []
                            _cash: list[float] = []
                            for bucket, want, cash in zip(
                                seat["buckets"], leg_shares, leg_cash
                            ):
                                if bucket in _drop:
                                    _shares.append(held.get(bucket, 0.0))
                                    _cash.append(0.0)
                                else:
                                    _shares.append(float(want))
                                    _cash.append(float(cash))
                            leg_shares = _shares
                            leg_cash = _cash
                            stake = sum(_cash)
                            if stake <= 0.0:
                                refusal = _deeper_refusal(refusal, (
                                    "decline_clamp_emptied_basket",
                                    "本轮全部目标桶概率较上一 cycle 下降：%s"
                                    % "+".join(sorted(_drop)),
                                    cycles, payload["decision_at_utc"],
                                ))
                                continue
                    if held:
                        # Charter 71(a): the probability must have moved
                        # since the chain last bought.  A refreshed book or
                        # refreshed room is not a reason to add.  Charter
                        # 143: not asked of a completion by held count.
                        _identity = str(
                            payload.get("probability_identity") or ""
                        )
                        if (
                            (production_verdict or {}).get(
                                "winner_completion_rule"
                            ) != _COMPLETION_143_RULE
                            and chain_probability_identity
                            and _identity
                            and _identity == chain_probability_identity
                        ):
                            refusal = _deeper_refusal(refusal, (
                                "topup_requires_probability_change",
                                "宪章 71(a)：概率身份 %s 自上次买入未变，不加仓"
                                "（本链已投 %.2f）" % (
                                    _identity[:12], chain_stake,
                                ),
                                cycles, payload["decision_at_utc"],
                            ))
                            continue
                        deltas = [
                            max(0.0, float(want) - held.get(bucket, 0.0))
                            for bucket, want in zip(
                                seat["buckets"], leg_shares
                            )
                        ]
                        # Charter 137: the cash bound, once the gaps are
                        # known.  Unreachable under ``net`` -- the legs sum
                        # to ``stake`` and ``stake`` is at most the room --
                        # so this lane walks the same ladder with the same
                        # numbers.  Under ``gross`` the whole basket scales
                        # together; dropping a leg is the incomplete basket
                        # the atomic arm exists to refuse.
                        if KELLY_TOPUP_BASIS == "gross":
                            _planned = 0.0
                            for leg, delta in zip(legs, deltas):
                                if delta <= 0.0:
                                    continue
                                _planned += _walk_ask_shares(leg, delta)[1]
                            _planned *= 1.0 + FEE_RATE
                            _room = max(0.0, stage_room)
                            if _planned > _room + 1e-9 and _planned > 0.0:
                                deltas = [
                                    d * (_room / _planned) for d in deltas
                                ]
                        topped: list[float] = []
                        topped_cash: list[float] = []
                        spend = 0.0
                        for leg, delta in zip(legs, deltas):
                            if delta <= 0.0:
                                topped.append(0.0)
                                topped_cash.append(0.0)
                                continue
                            got, paid = _walk_ask_shares(leg, delta)
                            topped.append(got)
                            topped_cash.append(paid * (1.0 + FEE_RATE))
                            spend += paid
                        if (
                            TOPUP_ATOMIC == "on"
                            # Charter 84: a designated completion buys the
                            # SHORT rungs and nothing else -- that is what it
                            # is for.  The atomic arm is this lane's own rule
                            # about ordinary top-ups ("整篮加仓"), and applied
                            # to a completion it refuses the one action
                            # production seats.  Measured 2026-09-11 on the
                            # 08-28..09-08 book: the judge seats 392
                            # completions and this gate turned every one that
                            # reached it into 整篮不动, which is why mirroring
                            # the CROSSING alone moved not a single settled
                            # chain -- the seats it freed were refused one
                            # gate later.  The divergence was never the fill
                            # model; it was that this book could not buy a
                            # completion at all.
                            and _origin != _LADDER_BALANCE_ORIGIN
                            and sum(topped) > 0.0
                            and any(q <= 0.0 for q in topped)
                        ):
                            refusal = _deeper_refusal(refusal, (
                                "topup_not_complete_basket",
                                "篮子完整性：目标 %s 只有 %d/%d 条腿需要加，整篮不动"
                                % (
                                    "+".join(seat["buckets"]),
                                    sum(1 for q in topped if q > 0.0),
                                    len(topped),
                                ),
                                cycles, payload["decision_at_utc"],
                            ))
                            continue
                        if sum(topped) <= 0.0:
                            refusal = _deeper_refusal(refusal, (
                                "no_top_up_needed",
                                "本阶段目标 %s 已在仓内" % "+".join(
                                    seat["buckets"]
                                ),
                                cycles, payload["decision_at_utc"],
                            ))
                            continue
                        spend *= 1.0 + FEE_RATE
                        # Charter 66 (user ruling, 2026-08-29).  The tilt
                        # delta above stays the size -- the log-optimal
                        # increment was tried as the sizer on the 08-29
                        # rebuild and bought the book from -3.3% to -18.1%
                        # -- but it buys only if it clears two chain-level
                        # clauses.  B first: does this buy improve the
                        # chain's expected log growth from here?  Then A:
                        # is the chain it leaves behind still priced under
                        # par?  A does not read the claim at all, which is
                        # why B cannot replace it.
                        bought_by_bucket = {
                            bucket: quantity
                            for bucket, quantity in zip(
                                seat["buckets"], topped
                            )
                            if quantity > 0.0
                        }
                        # Under the production judge the chain law was
                        # applied inside the solver, per window, on the
                        # partition it scored (charters 66(A), 71(b)):
                        # the seated window already cleared it, so this
                        # lane's copies below are computed for the record
                        # and refuse nothing.  Re-judging here refused
                        # seats production takes whenever the two
                        # partitions disagreed.
                        chain_law_here = production_verdict is None
                        # Charter 71(b): the lambda*p partition, read once
                        # for the growth gate and the increment-edge arm.
                        _partition = _effective_growth_partition(
                            stage=stage,
                            event_date=event_date,
                            probabilities=probabilities,
                            buckets=seat["buckets"],
                            snapshot=snapshot,
                            decision_at_utc=payload["decision_at_utc"],
                        )
                        if TOPUP_INCREMENT_EDGE == "on" and chain_law_here:
                            _increment_payout = sum(
                                float(_partition.get(str(b), 0.0)) * float(q)
                                for b, q in bought_by_bucket.items()
                            )
                            if not _increment_payout > spend:
                                refusal = _deeper_refusal(refusal, (
                                    "topup_increment_edge_negative",
                                    "宪章 71 乙款(增量)：增量折后期望赔付 %.3f"
                                    " ≤ 本笔成本 %.3f · 本链已投 %.2f" % (
                                        _increment_payout, spend, chain_stake,
                                    ),
                                    cycles, payload["decision_at_utc"],
                                ))
                                continue
                        growth_frame = CHAIN_TOPUP_BANKROLL_USDC or (
                            equity_at_open * STATION_DAY_HARD_FRACTION
                        )
                        growth = _chain_topup_growth(
                            held=held,
                            bought=bought_by_bucket,
                            probabilities=_partition,
                            spend=spend,
                            bankroll=growth_frame,
                        )
                        if chain_law_here and not growth > 0.0:
                            refusal = _deeper_refusal(refusal, (
                                "no_chain_growth",
                                "目标篮 %s · 本笔 %.2f 不改善整链对数增长"
                                "（ΔG=%.4f ≤ 0）· 本链已投 %.2f" % (
                                    "+".join(seat["buckets"]), spend,
                                    growth if math.isfinite(growth)
                                    else float("nan"),
                                    chain_stake,
                                ),
                                cycles, payload["decision_at_utc"],
                            ))
                            continue
                        held_after = dict(held)
                        for bucket, quantity in bought_by_bucket.items():
                            held_after[bucket] = (
                                held_after.get(bucket, 0.0) + quantity
                            )
                        chain_cost_after = _chain_cost_after_topup(
                            held_after=held_after,
                            probabilities=probabilities,
                            total_stake=chain_stake + spend,
                        )
                        if chain_law_here and (
                            chain_cost_after is None
                            or chain_cost_after >= CHAIN_COST_PAR_BOUND
                        ):
                            refusal = _deeper_refusal(refusal, (
                                "chain_cost_at_par",
                                "买入后整链混合成本 %s ≥ %.2f"
                                " · 本链已投 %.2f · 本笔 %.2f"
                                "（链层必亏，本阶段自身成本 %.3f 无关）" % (
                                    "%.3f" % chain_cost_after
                                    if chain_cost_after is not None
                                    else "∞",
                                    CHAIN_COST_PAR_BOUND,
                                    chain_stake, spend, seat_cost,
                                ),
                                cycles, payload["decision_at_utc"],
                            ))
                            continue
                        leg_shares = topped
                        leg_cash = topped_cash
                        stake = spend
                    # The order as posted, leg by leg, kept beside the one
                    # that came back.  Progressive entry overwrites
                    # ``leg_shares`` and ``stake`` with the filled part a
                    # few lines below, and the shortfall on a leg that
                    # bought nothing cannot be recovered afterwards: its
                    # kept share is zero, and the centre-tilt weights that
                    # sized it are not invertible from the legs that
                    # survived.  Recorded here so "posted 4 legs, got 1" is
                    # a readable number rather than an inference.
                    #
                    # The stake is priced at the ladder means the sizing
                    # itself used, so it answers "what the basket would have
                    # cost had every rung filled" -- the denominator the
                    # completion rate needs.
                    # Charter 70: a rung this seat would buy in fewer shares
                    # than the exchange fills is lifted to one lot when the
                    # basket can afford it -- same rule, same kernel function
                    # as production.  The kernel decides which rungs and
                    # whether the whole raise fits the scope's remaining room;
                    # this book then walks the real ladder for the lot, since
                    # a paper fill pays what the depth charges, not the top
                    # ask.  ``leg_shares`` here are the shares this seat buys
                    # -- on a top-up seat the deltas -- which is the order the
                    # exchange would judge.
                    # An order's size is a whole share (user ruling,
                    # 2026-09-06), on the kernel's grid -- the same one
                    # production's ``cash_phase`` floors onto.  Floored
                    # here, where the comment above says these are the
                    # shares the exchange would judge: after the seat is
                    # sized, before the venue's lot and dollar.  That is
                    # production's order too -- the solver picks a clip and
                    # sizing floors its answer -- and it is why the floor
                    # is not pushed inside ``_fillable_weighted``: that
                    # search probes a thousandth of a share for
                    # feasibility, so a floor inside it refuses every seat.
                    #
                    # Two rungs are left alone.  One under a whole share,
                    # because charter 70 lifts it to the venue minimum and
                    # the kernel's raise skips a zero increment ("a rung
                    # with no increment is not an order") -- flooring it to
                    # zero would stand down the basket production buys, the
                    # 0.862-share ZGSZ rung being the case.  And one that
                    # bought nothing this cycle: no cash, no order, nothing
                    # to floor -- the production-judge sizer records the
                    # chain's total on such a leg rather than an increment,
                    # and re-walking it would charge for shares nobody is
                    # buying.
                    _grid = [
                        floor_to_order_share_grid_v1(q)
                        if float(q) >= ORDER_SHARE_STEP_SHARES
                        and float(c) > 0.0
                        else float(q)
                        for q, c in zip(leg_shares, leg_cash)
                    ]
                    if any(
                        abs(float(a) - float(b)) > 1e-9
                        for a, b in zip(_grid, leg_shares)
                    ):
                        _gs: list[float] = []
                        _gc: list[float] = []
                        for _leg, _want, _was_cash in zip(
                            legs, _grid, leg_cash
                        ):
                            if float(_was_cash) <= 0.0:
                                _gs.append(float(_want))
                                _gc.append(float(_was_cash))
                                continue
                            _got, _paid = (
                                _walk_ask_shares_at_limit(_leg, _want)
                                if production_verdict is not None
                                else _walk_ask_shares(_leg, _want)
                            )
                            _gs.append(_got)
                            _gc.append(_paid * (1.0 + FEE_RATE))
                        leg_shares = _gs
                        leg_cash = _gc
                        stake = sum(_gc)
                    lot_top_up_record = None
                    if EXCHANGE_LOT_TOP_UP != "off" and legs:
                        # The lot the market states, or the exchange's
                        # standing minimum when the snapshot predates the
                        # field (before 2026-08-31 no snapshot carried
                        # it; the registry has stated 5.0 on every row
                        # since).  Production fails closed on an unstated
                        # lot; a replay of days the exchange enforced the
                        # same five shares on must not treat them as
                        # lot-free.
                        _seat_lots = {
                            str(b): float(
                                (quotes.get(str(b)) or {}).get("min_order_size")
                                or EXCHANGE_MINIMUM_LOT_SHARES_FALLBACK
                            )
                            for b in seat["buckets"]
                        }
                        _seat_asks = {
                            str(b): float(
                                ((quotes.get(str(b)) or {}).get("ask_levels")
                                 or [{}])[0].get("price") or 0.0
                            )
                            for b in seat["buckets"]
                        }
                        if (
                            production_verdict is not None
                            and LOT_LIFT_BUDGET == "untilted"
                        ):
                            # Production's lift budget is the untilted
                            # remaining scope allowance (``cash_phase``
                            # sizes against ``remaining_budget``, which
                            # the tilt never touches), not the tilted
                            # capital the solver was handed.
                            _room = max(
                                0.0,
                                float(production_verdict["lot_room_usdc"])
                                - float(stake),
                            )
                        else:
                            _room = max(0.0, float(stage_room) - float(stake))
                        _decision = raise_sub_lot_targets_v2(
                            target_shares_by_bucket={
                                str(b): float(q)
                                for b, q in zip(seat["buckets"], leg_shares)
                            },
                            support=tuple(str(b) for b in seat["buckets"]),
                            min_lot_by_bucket=_seat_lots,
                            ask_by_bucket=_seat_asks,
                            capital_available_usdc=float(stake) + _room,
                            # Charter 83: the venue's dollar floor, read
                            # from production's switch so one arm flips
                            # both sides.
                            minimum_notional_usdc=(
                                EXCHANGE_MINIMUM_ORDER_NOTIONAL_USDC
                                if CHARTER_83_MIN_NOTIONAL == "on" else 0.0
                            ),
                        )
                        if _decision["raised_buckets"]:
                            _new_shares = list(leg_shares)
                            _new_cash = list(leg_cash)
                            _walked = True
                            for _i, (_b, _leg) in enumerate(
                                zip(seat["buckets"], legs)
                            ):
                                if str(_b) not in _decision["raised_buckets"]:
                                    continue
                                _got, _paid = (
                                    _walk_ask_shares_at_limit(
                                        _leg, _seat_lots[str(_b)]
                                    )
                                    if production_verdict is not None
                                    else _walk_ask_shares(
                                        _leg, _seat_lots[str(_b)]
                                    )
                                )
                                if _got + 1e-9 < _seat_lots[str(_b)]:
                                    _walked = False
                                    break
                                _new_shares[_i] = _got
                                _new_cash[_i] = _paid * (1.0 + FEE_RATE)
                            _new_stake = sum(_new_cash)
                            if _walked and _new_stake <= float(stake) + _room + 1e-9:
                                lot_top_up_record = {
                                    "raised_buckets": list(
                                        _decision["raised_buckets"]
                                    ),
                                    "lot_raise_usdc": round(
                                        _new_stake - float(stake), 6
                                    ),
                                }
                                leg_shares = _new_shares
                                leg_cash = _new_cash
                                stake = _new_stake
                            else:
                                lot_top_up_record = {
                                    "refused": (
                                        "depth" if not _walked else "room"
                                    ),
                                    "raised_buckets": list(
                                        _decision["raised_buckets"]
                                    ),
                                }
                        elif _decision["refused_reason"]:
                            lot_top_up_record = {
                                "refused": _decision["refused_reason"],
                                "requested_lot_raise_usdc": round(
                                    float(_decision["requested_lot_raise_usdc"]), 6
                                ),
                            }
                        # Charter 93: the floor is re-read on the lifted
                        # legs -- held plus this increment -- the way
                        # production re-reads it on its lifted targets.  A
                        # basket that would lose inside its own window is
                        # refused by name, whatever the edge said.  Charter
                        # 143: not a completion by held count, which
                        # production exempts on its lifted bundle.
                        if CHARTER_93_SUPPORT_FLOOR == "on" and not (
                            _completion_143_exempt_on_legs(
                                verdict=production_verdict, seat=seat,
                                held=held, held_cost=held_cost,
                                leg_shares=leg_shares, leg_cash=leg_cash,
                            )
                        ):
                            _floor_shares = {
                                str(b): float(held.get(str(b), 0.0)) + float(q)
                                for b, q in zip(seat["buckets"], leg_shares)
                            }
                            _floor_cost = {
                                str(b): float(c) for b, c in held_cost.items()
                            }
                            for _b, _c in zip(seat["buckets"], leg_cash):
                                _floor_cost[str(_b)] = (
                                    _floor_cost.get(str(_b), 0.0) + float(_c)
                                )
                            # Charter 84 (八): a market-dead rung is
                            # outside the protected set, as the solver
                            # reads it.
                            _floor_dead = set(
                                (seat.get("completion") or {})
                                .get("market_dead_buckets") or ()
                            )
                            _floor_verdict = support_scenario_floor_v1(
                                shares_by_bucket=_floor_shares,
                                cost_by_bucket=_floor_cost,
                                support=tuple(
                                    str(b) for b in seat["buckets"]
                                    if str(b) not in _floor_dead
                                ),
                            )
                            if not _floor_verdict["legal"]:
                                # Charter 121: the floor refuses this
                                # candidate, not the payload.  Re-ask with the
                                # refused row excluded; the refusal is recorded
                                # first either way, and ``_deeper_refusal``
                                # keeps it -- depth 8 outranks anything a
                                # shallower retry can report, and ties keep the
                                # incumbent -- so a retry that finds nothing
                                # leaves exactly the record this lane wrote
                                # before the clause existed.
                                _refused_id = str(
                                    (production_verdict or {}).get(
                                        "winner_candidate_id"
                                    ) or ""
                                )
                                _refused_complete = str(
                                    (production_verdict or {}).get(
                                        "winner_candidate_kind"
                                    ) or ""
                                ) == "COMPLETE_BASKET_TARGET"
                                refusal = _deeper_refusal(refusal, (
                                    "support_floor_after_lift",
                                    "宪章 93：抬手后支持集内最坏结算 %.2f"
                                    " < 地板 %.2f（成本 %.2f，本笔 %.2f）" % (
                                        _floor_verdict["worst_in_support_pnl"],
                                        _floor_verdict["floor"],
                                        _floor_verdict["cost"],
                                        float(stake),
                                    ),
                                    cycles, payload["decision_at_utc"],
                                ))
                                if (
                                    CHARTER_121_POST_LIFT_RERANK == "on"
                                    and _refused_complete
                                    and _refused_id
                                    and _refused_id
                                    not in _excluded_candidate_ids
                                    and len(_excluded_candidate_ids)
                                    < CHARTER_121_POST_LIFT_ATTEMPTS - 1
                                ):
                                    _payload_queue.insert(0, (
                                        payload,
                                        _excluded_candidate_ids
                                        + (_refused_id,),
                                        _payload_entry_prev,
                                    ))
                                continue
                        # The exchange refuses any order under its lot, so
                        # a leg still under it after the raise cannot fill
                        # at any price.  Production's ``cash_phase`` names
                        # this ``exchange_minimum_lot_unaffordable`` and
                        # the whole basket stands down; this book used to
                        # record the fraction as filled -- 134 of the 209
                        # standing rows on 2026-09-04 were orders no
                        # exchange takes.
                        _under_lot = [
                            (str(b), float(q), _seat_lots[str(b)])
                            for b, q in zip(seat["buckets"], leg_shares)
                            if _seat_lots[str(b)] > 0.0
                            and 0.0 < float(q) + 1e-9 < _seat_lots[str(b)]
                        ]
                        if _under_lot:
                            refusal = _deeper_refusal(refusal, (
                                "exchange_minimum_lot_unaffordable",
                                "不足一手：%s · 抬到一手%s · 本笔 %.2f · 额度 %.2f"
                                % (
                                    "、".join(
                                        "%s %.2f股<%g" % item
                                        for item in _under_lot
                                    ),
                                    (
                                        "需再花 %.2f" % float(
                                            lot_top_up_record.get(
                                                "requested_lot_raise_usdc", 0.0
                                            )
                                        )
                                        if isinstance(lot_top_up_record, dict)
                                        and "requested_lot_raise_usdc"
                                        in lot_top_up_record
                                        else "盘口深度不够"
                                    ),
                                    float(stake), float(stake) + _room,
                                ),
                                cycles, payload["decision_at_utc"],
                            ))
                            continue
                    if production_verdict is not None and leg_cash:
                        # Where the operator's station dial actually binds
                        # in production: the kernel's ``profit_floor_ok``
                        # (``evaluate_action_v2`` per leg, then
                        # ``rescore_maker_bundle_v2`` on the bundle).  The
                        # worst settled outcome inside the seated support
                        # -- shares held on that rung minus everything the
                        # chain has paid -- may not fall below
                        # ``minimum_expected_pnl_after_usdc``, which the
                        # dial sets to minus itself.  Corrected 2026-09-04:
                        # the mirror here used to copy
                        # ``execution_gates.evaluate_execution_gate``'s
                        # per-leg ``budget_ok``, and that function has no
                        # caller on the live path; the live ``budget_ok``
                        # reads the tilted 2% capital the solver already
                        # sized inside.
                        _floor = -float(production_verdict["total_budget_usdc"])
                        _held_after = dict(held)
                        for _b, _q in zip(seat["buckets"], leg_shares):
                            _held_after[str(_b)] = (
                                _held_after.get(str(_b), 0.0) + float(_q)
                            )
                        _cost_after = float(chain_stake) + float(stake)
                        _worst = min(
                            _held_after.get(str(_b), 0.0)
                            for _b in seat["buckets"]
                        ) - _cost_after
                        if _worst + 1e-9 < _floor:
                            refusal = _deeper_refusal(refusal, (
                                "station_loss_floor_exceeded",
                                "生产 profit_floor_ok：支持集内最坏结算 %.2f"
                                " < 站亏损下限 %.2f（链已投 %.2f + 本笔 %.2f）"
                                % (_worst, _floor, float(chain_stake),
                                   float(stake)),
                                cycles, payload["decision_at_utc"],
                            ))
                            continue
                        # Charter 73: production's bundle re-score
                        # authorizes on the increment's edge -- its
                        # expected payout on the belief the seat was
                        # judged on, less its cash -- above the policy's
                        # epsilon.  The kernel's own number.  Charter 92:
                        # every increment is authorized on the deflated
                        # belief, with no exempt origin -- the one that was
                        # exempt, charter 74's ticket, is retired (user
                        # ruling 2026-09-08).
                        if True:
                            _held_cost_after = dict(held_cost)
                            for _b, _c in zip(seat["buckets"], leg_cash):
                                _held_cost_after[str(_b)] = (
                                    _held_cost_after.get(str(_b), 0.0)
                                    + float(_c)
                                )
                            _edge = whole_basket_edge_delta_v1(
                                edge_probabilities=production_verdict[
                                    "edge_probabilities"
                                ],
                                current_shares=held,
                                current_cost=held_cost,
                                resulting_shares=_held_after,
                                resulting_cost=_held_cost_after,
                            )
                            _epsilon = float(
                                production_verdict["edge_epsilon"]
                            )
                            # Charter 136: production admits this completion
                            # on the exposure and exempts it after the lift;
                            # its kernel authorizes it as a charter-116 repair.
                            # Both conditions, read here from the same seat
                            # and the kernel's own repair function.
                            _exposure_repair = (
                                str(((seat.get("completion") or {})
                                     .get("admitted_by")) or "")
                                == _COMPLETION_RISK_BUDGET_RULE
                                and bool(_completion_repair_exemption_v1(
                                    support=tuple(
                                        str(_b) for _b in seat["buckets"]
                                    ),
                                    held_shares=held,
                                    held_cost_usdc=held_cost,
                                    target_shares=_held_after,
                                    ask_by_bucket={
                                        str(_b): (
                                            float(_c) / float(_q)
                                            if float(_q) > 1e-9 else 0.0
                                        )
                                        for _b, _q, _c in zip(
                                            seat["buckets"], leg_shares,
                                            leg_cash,
                                        )
                                    },
                                    claim_by_bucket=production_verdict[
                                        "edge_probabilities"
                                    ],
                                ).get("exempt"))
                            )
                            if (
                                not _exposure_repair
                                and not _completion_143_exempt_on_legs(
                                    verdict=production_verdict, seat=seat,
                                    held=held, held_cost=held_cost,
                                    leg_shares=leg_shares, leg_cash=leg_cash,
                                )
                                and not _edge > _epsilon + 1e-9
                            ):
                                refusal = _deeper_refusal(refusal, (
                                    "execution_edge_not_authorized",
                                    "宪章 73：增量在入座信念上的边际 %.4f"
                                    " ≤ ε %.4f（本笔 %.2f）" % (
                                        _edge, _epsilon, float(stake),
                                    ),
                                    cycles, payload["decision_at_utc"],
                                ))
                                continue
                    intended_legs = {
                        str(bucket): float(quantity)
                        for bucket, quantity in zip(
                            seat["buckets"], leg_shares
                        )
                    }
                    # Charter 136 (五) / 143 v2: the seat is what the chain
                    # POSTED, recorded here at posting.  Production's "sent"
                    # is an exchange answer on the intent, a cancel included;
                    # until 2026-09-17 this book recorded the seat only on a
                    # fill, so a leg the venue cancelled was never one it
                    # could complete while production could.  The size is
                    # the posted size of a leg not yet held -- the one bound
                    # charter 143 v2 reads.
                    # Charter 145: a completion's post -- a charter-143
                    # winner, the TX entry included -- is not a send that
                    # defines the basket, on either side (production skips
                    # the FOK in ``load_chain_sent_shares``).
                    if (production_verdict or {}).get("winner_completion_rule") is None:
                        chain_intended.update(str(b) for b in intended_legs)
                        chain_intended_shares.update({
                            str(b): float(q) for b, q in intended_legs.items()
                            if float(q) > 0.0
                            and float(held.get(str(b), 0.0)) <= 1e-9
                        })
                    # What each posted leg was priced to cost.  Sizing
                    # weights the share counts, not the cash, so the cash
                    # per leg is the share count times whatever the book
                    # charged for it -- a number the share column cannot be
                    # read as and the basket total cannot be split into.
                    intended_leg_stake = {
                        str(bucket): float(cash)
                        for bucket, cash in zip(seat["buckets"], leg_cash)
                    }
                    intended_stake = float(stake)
                    # Read here, above the narrowing, because this is the
                    # last line where ``seat`` is still the basket the gate
                    # judged.  Charter 94(五) keeps the gate and Kelly on the
                    # crossing ask, so ``all_in_price`` -- not the maker
                    # posting price -- is the denominator lambda(a) was
                    # looked up at, and the one it has to be fitted at.
                    # The two numbers the gate judged -- from the lane that
                    # judged.  ``seat`` is this lane's row, and when the
                    # production judge is in force this lane decided
                    # nothing: it prices the same window off its own
                    # snapshot and, since charter 81, without the lift the
                    # gate divides by.  EFHK 2026-08-16 is the whole
                    # argument: this lane read claim 0.6982 against price
                    # 0.7000 and the D-rule audit called the seat an
                    # impossible one, while production's own row for the
                    # same three buckets read a lifted claim of 0.7540
                    # against 0.7500 and was admitted correctly.  Neither
                    # number was wrong; the pairing was.
                    intended_win_probability = float(seat["win_probability"])
                    intended_all_in_cost = float(seat["all_in_price"])
                    if production_verdict is not None:
                        _judged_rows = (
                            production_verdict.get("enumerated_windows") or ()
                        )
                        _wanted = tuple(str(b) for b in seat["buckets"])
                        for _row in _judged_rows:
                            if tuple(
                                str(b) for b in (_row.get("buckets") or ())
                            ) != _wanted:
                                continue
                            if _row.get("all_in_price") is None:
                                continue
                            intended_win_probability = float(
                                _row["win_probability"]
                            )
                            intended_all_in_cost = float(_row["all_in_price"])
                            break
                    # Which enumerated window this stage actually took, kept
                    # here rather than read back off ``seat`` at the flush:
                    # the resting lane narrows ``seat["buckets"]`` to what
                    # filled, and a narrowed window matches no row in the
                    # enumeration it came from.
                    stage_seated_buckets = tuple(str(b) for b in seat["buckets"])
                    # And the enumeration this stage ACTED on.  Recording
                    # only "the cycle the walk stopped on" lost it whenever
                    # the stage re-seated: the walk keeps going, and a later
                    # cycle whose book has gone unpriced overwrites the one
                    # that bought.  EFHK 2026-08-16 seated twice and left no
                    # candidate row at all that way.
                    stage_candidates_seated = stage_candidates
                    if EXECUTION_MODE == "maker_resting" and not seat_completion_take:
                        # The sizing above is the order that was posted.
                        # This is the position that came back.
                        entered = _progressive_entry(
                            buckets=seat["buckets"],
                            leg_shares=leg_shares,
                            filled_fraction=resting_fraction,
                            prices=resting_prices,
                            probabilities=probabilities,
                        )
                        if entered is None:
                            refusal = _deeper_refusal(refusal, (
                                "resting_unfilled",
                                "挂 %d 腿 · 成交量为零" % len(seat["buckets"]),
                                cycles, payload["decision_at_utc"],
                            ))
                            continue
                        (
                            kept, leg_shares, stake, win_probability,
                            leg_cash,
                        ) = entered
                        if len(kept) < len(seat["buckets"]):
                            counts["resting_partial_basket"] += 1
                            ordinals = {
                                str(row[0]): int(row[1])
                                for row in payload["buckets"]
                            }
                            span = (
                                ordinals.get(kept[-1], 0)
                                - ordinals.get(kept[0], 0)
                            )
                            if span != len(kept) - 1:
                                counts["resting_gapped_basket"] += 1
                        seat = dict(seat)
                        seat["buckets"] = kept
                        seat["win_probability"] = win_probability
                    # The basket's own payout per unit spent.  Under equal
                    # shares this was cost over share count; weighted, the
                    # honest analogue is what the position expects to
                    # return, so ``shares`` becomes the expectation-
                    # equivalent count the settlement math already assumes.
                    # Two different questions, two different numbers.
                    #
                    # Settlement pays the rung that landed, so it reads the
                    # per-leg counts -- a single basket-level count would
                    # credit a payout no outcome produces.
                    #
                    # ``shares`` is the divisor of ``all_in_cost``, which the
                    # positive-EV gate, the dust floor and the whole page are
                    # built on, and it has to mean "cost per unit of payout".
                    # Taking the thinnest leg made that a number no outcome
                    # produces either, in the other direction: FACT
                    # 2026-07-31 recorded 0.3400 against a 0.2800
                    # top-of-book total -- a resting basket costing *more*
                    # than crossing, which cannot happen.  The
                    # payout-weighted count is the honest analogue and
                    # degenerates to the equal-share case exactly: with q_i
                    # all equal, sum(p_i q_i)/p_win = q.
                    per_leg = {
                        str(b): float(q)
                        for b, q in zip(seat["buckets"], leg_shares)
                    }
                    # The same split for the cash.  Built after progressive
                    # entry has replaced the window with the legs that
                    # filled, so this is what the position cost, leg by leg,
                    # and it sums to ``stake``.
                    leg_stake = {
                        str(b): float(c)
                        for b, c in zip(seat["buckets"], leg_cash)
                    }
                    expected_payout = sum(
                        float(probabilities.get(b, 0.0)) * q
                        for b, q in zip(seat["buckets"], leg_shares)
                    )
                    shares = expected_payout / max(
                        float(seat["win_probability"]), 1e-9
                    )
                    opened = True
                    # What the basket actually cost per set, which is the
                    # number every downstream P&L and edge reading has to be
                    # built on.
                    all_in_cost = stake / shares
                    # Still measured against the CROSSING total, not against
                    # what this lane posted at.  The counter asks "did the
                    # clip have to reach past top of book", and top of book
                    # is the offer -- a resting basket realising above its
                    # own band mean is a different fact (the dear rungs are
                    # the ones that filled) and would be a lie under this
                    # name.  Charter aside, the invariant this guards is the
                    # one written at 2026-08-19: a resting basket cannot cost
                    # more than crossing, and if it reads that way the share
                    # count is wrong.
                    if all_in_cost > seat_crossing_cost + 1e-9:
                        counts["walked_book"] += 1
                    # One book, one rule: a seat that does not compound does
                    # not consume the day's budget either.  Charging dust
                    # against a bankroll its own wins are barred from feeding
                    # would let the phantom side crowd out the real one.
                    credible = all_in_cost >= DUST_COST_FLOOR
                    if not credible:
                        counts["dust_fill_excluded_from_equity"] += 1
                    # The position this station-day now holds.  A stage adds
                    # to it; nothing here ever takes away.  Everything the
                    # chain settles on is read off these, not off the stage
                    # that happened to act last.
                    for bucket, quantity in per_leg.items():
                        held[bucket] = held.get(bucket, 0.0) + float(quantity)
                    for bucket, cash in leg_stake.items():
                        held_cost[bucket] = (
                            held_cost.get(bucket, 0.0) + float(cash)
                        )
                    chain_stake += stake
                    chain_probabilities = probabilities
                    chain_probability_identity = str(
                        payload.get("probability_identity") or ""
                    )
                    chain_ladder = payload["buckets"]
                    chain_center_c = payload.get("center_c")
                    chain_last_stage = stage
                    if not chain_opened_at:
                        chain_opened_at = payload["decision_at_utc"]
                    # The economic identity of one decision (user ruling,
                    # 2026-09-02).  Four legs, and a decision is the same
                    # decision only if all four match:
                    #
                    #   1 probability   -- envelope content hash, posterior
                    #                      hash, exclusion mask
                    #   2 sizing output -- the objective's own answer:
                    #                      selected buckets and final
                    #                      support, score, limit price,
                    #                      maximum shares, cash clip, the
                    #                      stake, and the status.  Edge
                    #                      enters here.
                    #   3 position book -- filled shares and cost, pending
                    #                      shares and notional, budget left
                    #   4 policy        -- strategy version, per-station
                    #                      policy, delta_v2 / regime /
                    #                      maker_grid artefact hashes
                    #
                    # ``decision_at_utc`` is deliberately NOT in it.  The
                    # old identity was {station, day, stage, decision time,
                    # window, stake}: the clock made every recompile a new
                    # row while carrying none of what the decision was
                    # actually made of, so two economically different
                    # decisions a second apart could collide on the window
                    # and stake alone, and two identical ones a minute
                    # apart could not.  That is the same churn the P0
                    # posterior identity was corrected for.
                    #
                    # This is what lets a stage seat more than once
                    # honestly: a later cycle that believes something
                    # different, or is priced differently, or stands on a
                    # different book, is a different decision and gets its
                    # own row; one that is economically identical resolves
                    # to the same id and is idempotent.
                    identity = {
                        "schema": "paper_trade_economic_decision_v2",
                        "scope": {
                            "station_id": station_id,
                            "event_date": event_date,
                            "stage": stage,
                        },
                        "probability": {
                            "envelope_content_sha256": payload.get(
                                "envelope_content_sha256"
                            ),
                            "posterior_sha256": payload.get(
                                "posterior_sha256"
                            ),
                            "exclusion_mask": payload.get("exclusion_mask"),
                        },
                        "sizing": {
                            "selected_buckets": list(seat["buckets"]),
                            "final_support": list(posted_buckets),
                            "selected_score": seat.get("log_growth_rank"),
                            "limit_price": round(float(all_in_cost), 8),
                            "maximum_shares": round(float(shares), 8),
                            "cash_clip": round(float(stake), 8),
                            "sizing": [round(float(q), 8) for q in leg_shares],
                            "status": "SEATED",
                        },
                        "position_book": {
                            "filled_shares": {
                                b: round(float(q), 8)
                                for b, q in sorted(held_before.items())
                            },
                            "filled_cost_usdc": round(float(chain_stake), 8),
                            "pending_shares": {},
                            "pending_notional_usdc": 0.0,
                            "budget_remaining_usdc": round(
                                float(stage_room), 8
                            ),
                        },
                        "policy": {
                            "strategy_version": SELECTION_RULE_LABEL,
                            "selection_rule_sha256": SELECTION_RULE_SHA256,
                            "artifacts": payload.get("policy_artifacts"),
                        },
                    }
                    position_id = "paper-pos-" + content_sha256(identity)[:24]
                    chain_position_id = position_id
                    pending_positions.append(
                        (
                            position_id, station_id, event_date, stage,
                            payload["decision_at_utc"], observed_at,
                            seat_center,
                            json.dumps(
                                list(seat["buckets"]), ensure_ascii=False
                            ),
                            len(seat["buckets"]),
                            float(seat["win_probability"]),
                            all_in_cost,
                            float(seat["win_probability"]) - all_in_cost,
                            seat.get("log_growth_rank"),
                            fraction, stake, shares,
                            json.dumps(probabilities, ensure_ascii=False),
                            json.dumps(prices, ensure_ascii=False),
                            payload.get("center_c"),
                            int(audit.get("center_refused_window_count") or 0),
                            content_sha256(identity),
                            SELECTION_RULE_SHA256,
                            SELECTION_RULE_LABEL,
                            json.dumps(per_leg, sort_keys=True),
                            json.dumps(resting_fills, sort_keys=True),
                            json.dumps(posted_buckets, ensure_ascii=False),
                            json.dumps(intended_legs, sort_keys=True),
                            # Audit only: production's own reading, stored
                            # verbatim rather than recomputed here.
                            json.dumps(
                                production_verdict.get("marginal_coverage")
                                or {},
                                sort_keys=True, ensure_ascii=False,
                            ) if production_verdict else "",
                            intended_stake,
                            intended_win_probability,
                            intended_all_in_cost,
                            json.dumps(intended_leg_stake, sort_keys=True),
                            json.dumps(leg_stake, sort_keys=True),
                            json.dumps(held_before, sort_keys=True),
                            json.dumps(held, sort_keys=True),
                            chain_stake,
                            # The signs this stage entered under, already
                            # computed for the gate.  A later stage used to
                            # recompute them here from the same arguments;
                            # one call now serves both.  Charter 70's lot
                            # raise, when one happened, rides on the same
                            # record.
                            json.dumps(
                                {
                                    **(
                                        dict(stage_consolidation)
                                        if isinstance(stage_consolidation, Mapping)
                                        else {"value": stage_consolidation}
                                    ),
                                    **(
                                        {"exchange_lot_top_up": lot_top_up_record}
                                        if lot_top_up_record is not None
                                        else {}
                                    ),
                                },
                                sort_keys=True,
                            ),
                        )
                    )
                    # The chain spends the rest of its day under the tilt
                    # its opener earned -- recorded at the opening seat
                    # only, as production's INSERT OR IGNORE records it.
                    if seat_consolidation is not None:
                        chain_tilt = tilt
                        chain_consolidation = seat_consolidation
                    counts["positions"] += 1
                    counts["entry_cycle_%s" % (
                        "first" if cycles == 1 else "later"
                    )] += 1
                    # Stop at the first seat, or keep walking and let the
                    # stage add to itself on every later cycle.
                    #
                    # Production does the latter: it re-decides on every
                    # model update (charter 46), and 363 of 387 live
                    # station-days from 2026-08-25 carry ten or more
                    # decisions.  This ledger has always done the former,
                    # so every arm measured on it describes a world where
                    # each stage is entered once -- which is not the world
                    # production trades.  The clamp in particular can fire
                    # ten times a day there and at most once here.
                    #
                    # Nothing else has to change to allow it: ``held`` and
                    # ``chain_stake`` already accumulate across cycles
                    # because the three stages share one book, and
                    # ``stage_room`` is ``stage_budget * tilt -
                    # chain_stake``, so a second seat inside one stage is
                    # financed out of what the first one left and the
                    # allowance runs out on its own.
                    if not RESEAT_WITHIN_STAGE:
                        break
                if not opened:
                    counts[refusal[0]] += 1
                    # ``cycles_examined`` stays the whole walk, not the cycle
                    # the kept refusal came from -- it used to be read off the
                    # tuple, which was the same number only while every cycle
                    # overwrote it.  The stamp beside it is the decision the
                    # reported reason was measured at, so the detail string and
                    # the timestamp describe the same book.
                    pending_refusals.append(
                        (
                            station_id, event_date, stage, refusal[0],
                            refusal[1], cycles, refusal[3],
                            datetime.now(timezone.utc)
                            .isoformat().replace("+00:00", "Z"),
                            SELECTION_RULE_SHA256,
                        )
                    )
                # Charter 87, second reading (user ruling 2026-09-07,
                # "lambda 改拟在全部候选窗口上"): the enumeration is the
                # evidence, not the seat.  Recorded for the refused stages
                # too -- most of the book is refused, and a fit that only
                # ever sees what the gate let through is the gate reading
                # its own decisions back as though they were outcomes.
                recorded = stage_candidates_seated or stage_candidates
                if recorded is not None:
                    decided_at, enumerated, band_axis, band_sha = recorded
                    seated_key = stage_seated_buckets
                    for row in enumerated:
                        if row.get("all_in_price") is None:
                            continue
                        buckets = [str(b) for b in row.get("buckets") or ()]
                        if not buckets:
                            continue
                        # The row IS the enumeration at that decision
                        # moment, so the rule fingerprint is recorded beside
                        # it and is deliberately NOT part of its identity.
                        # With the sha in here the primary key moved every
                        # time a rule changed while the UNIQUE below did
                        # not, and a rebuild under a new fingerprint aborted
                        # on the second row it wrote -- taking the whole
                        # book with it, because the walk buffers its writes
                        # to one burst at the end.
                        identity = {
                            "station_id": str(station_id),
                            "event_date": str(event_date),
                            "stage": str(stage),
                            "decision_at_utc": str(decided_at),
                            "buckets": buckets,
                        }
                        pending_candidates.append((
                            "paper-cand-" + content_sha256(identity)[:24],
                            station_id, event_date, stage, decided_at,
                            json.dumps(buckets, ensure_ascii=False),
                            len(buckets),
                            float(row.get("win_probability") or 0.0),
                            (
                                None
                                if row.get("raw_win_probability") is None
                                else float(row["raw_win_probability"])
                            ),
                            float(row.get("all_in_price") or 0.0),
                            (
                                None
                                if row.get("effective_edge_agreement") is None
                                else float(row["effective_edge_agreement"])
                            ),
                            1 if row.get("admissible", True) else 0,
                            1 if tuple(buckets) == seated_key else 0,
                            SELECTION_RULE_SHA256,
                            band_axis,
                            band_sha,
                            (
                                CLAIM_TRANSFORM_SHA256_CENTRE
                                if float(
                                    row.get("center_gap_buckets") or 0.0
                                ) <= 0.0
                                else CLAIM_TRANSFORM_SHA256_OFF_CENTRE
                            ),
                            # Per-rung claim and cost, for the bucket-scope
                            # fit.  Absent on a runtime that predates the
                            # field, which the fit reads as "no legs here"
                            # rather than as zero legs priced at zero.
                            (
                                json.dumps(
                                    row["leg_claim_cost"],
                                    sort_keys=True, ensure_ascii=False,
                                )
                                if row.get("leg_claim_cost") else None
                            ),
                            # Charter 131: the shape rule this window was
                            # enumerated under -- the lambda fit's third
                            # partition key.
                            SEAT_SHAPE_RULE_VERSION,
                            # Charter 128 v2: the market's belief and the
                            # clock, both off the enumeration's own row.
                            # Absent (None) when the book gave no belief
                            # or the runtime predates the field.
                            (
                                None
                                if row.get("beta_market_mid") is None
                                else float(row["beta_market_mid"])
                            ),
                            (
                                None
                                if row.get("beta_hours_to_cutoff") is None
                                else float(row["beta_hours_to_cutoff"])
                            ),
                        ))
            # The chain is done adding.  One position, one settlement: the
            # truth lands once, and scoring each stage against it separately
            # is what charged 40 of 152 settled station-days two and three
            # times over.  What settles is the accumulated position, not the
            # window the last stage happened to choose.
            if not held:
                continue
            chain_win_probability = sum(
                float(chain_probabilities.get(bucket, 0.0)) for bucket in held
            )
            chain_expected_payout = sum(
                float(chain_probabilities.get(bucket, 0.0)) * quantity
                for bucket, quantity in held.items()
            )
            chain_shares = chain_expected_payout / max(
                chain_win_probability, 1e-9
            )
            chain_cost = (
                chain_stake / chain_shares if chain_shares > 0 else 0.0
            )
            chain_credible = chain_cost >= DUST_COST_FLOOR
            standing = truth.get((station_id, event_date))
            if standing is None:
                counts["open_unsettled"] += 1
                continue
            chain_ordinals = {
                str(row[0]): int(row[1]) for row in chain_ladder
            }
            chain_window = sorted(
                held, key=lambda bucket: chain_ordinals.get(bucket, 0)
            )
            scored = _score_settlement(
                ladder=chain_ladder,
                window=chain_window,
                stake=chain_stake,
                shares=chain_shares,
                per_leg=held,
                center_c=chain_center_c,
                truth_c=standing.tmax_c,
                intended_window=sorted(set(chain_window) | chain_intended),
            )
            if scored is None:
                counts["truth_outside_book"] += 1
                continue
            pending_settlements.append(
                (
                    _settlement_identity_id(
                        station_id=station_id,
                        event_date=event_date,
                        chain_opened_at=chain_opened_at,
                        window=chain_window,
                        stake_usdc=chain_stake,
                        truth_c=standing.tmax_c,
                    ),
                    chain_position_id, station_id, event_date,
                    # The stage that last added to the position.  A chain is
                    # one bet, so this says how far it had been adjusted when
                    # the day resolved, not which of three books it belonged
                    # to.
                    chain_last_stage or "P0",
                    standing.tmax_c, scored.settled_bucket,
                    scored.hit, scored.intended_hit,
                    chain_stake, scored.payout_usdc,
                    scored.pnl_usdc,
                    chain_win_probability,
                    scored.center_error_c, scored.bucket_distance,
                    datetime.now(timezone.utc)
                    .isoformat().replace("+00:00", "Z"),
                    # The revision this score was struck against.  Without it
                    # the next sweep cannot tell a seat it scored correctly
                    # from one whose truth has since been corrected under it.
                    standing.revision_no, standing.content_sha256,
                    chain_cost,
                )
            )
            counts["settlements"] += 1
            if chain_credible:
                day_pnl += scored.pnl_usdc
        # The day is over: realise it, then let the next day size off the
        # book it actually leaves behind.
        #
        # Read the day's realised P&L back out of the settlements rather than
        # accumulating what this pass happened to write.  A scope already
        # recorded is skipped above and contributes nothing to ``day_pnl``,
        # so on any incremental sweep the equity curve saw only the seats
        # this run created -- which on a steady-state pass is none at all,
        # leaving equity pinned at the opening capital.  That is how a P1
        # seat came to be staked at exactly 20.00 (2% of 1000) on a day the
        # book had drawn down to 726.  Same filter as the report: only fills
        # at or above the dust floor compound.
        #
        # Chains this pass replayed are the one case where the two sets are
        # not disjoint: their old settlement is still in the table and their
        # new one is in ``day_pnl``.  Counting the recorded side of a chain
        # that has just been rewritten would compound a P&L this pass has
        # already superseded, so those stations are read out of the query
        # and taken from ``day_pnl`` alone.
        replayed_today = {
            station for station, day in pending_chain_resets
            if day == event_date
        }
        realised = 0.0
        for station, pnl in conn.execute(
            "SELECT s.station_id, SUM(s.pnl_usdc)"
            " FROM paper_trade_settlement s"
            " JOIN paper_trade_position p USING(position_id)"
            " WHERE s.event_date=? AND p.all_in_cost>=?"
            " GROUP BY s.station_id",
            (event_date, DUST_COST_FLOOR),
        ):
            if str(station) in replayed_today:
                continue
            realised += float(pnl or 0.0)
        # Settlements this pass created are still buffered for the final
        # write burst, so they are absent from the query above and are added
        # on top rather than replacing it.
        equity = max(0.0, equity + realised + day_pnl)
    # Every write in one short burst at the end.  The scan above reads most of
    # a ten-gigabyte database and takes about half a minute; holding the write
    # lock across it is what cost the resident whole cycles to
    # ``database is locked``, and the resident is right to give up fast --
    # "a concurrent maintenance writer must not consume a whole production
    # cadence" is its own stated rule.  Reads do not block it, so the only
    # thing that has to be brief is this, and a few dozen inserts are.
    # Replayed chains first: their rows were written stage by stage under a
    # reading where each stage was its own bet, and the rewrite below is one
    # position built up across them.  ``INSERT OR IGNORE`` would keep the old
    # rows and silently drop the new ones.
    if pending_chain_resets:
        conn.executemany(
            "DELETE FROM paper_trade_settlement WHERE position_id IN ("
            "  SELECT position_id FROM paper_trade_position"
            "  WHERE station_id=? AND event_date=?)",
            pending_chain_resets,
        )
        conn.executemany(
            "DELETE FROM paper_trade_position"
            " WHERE station_id=? AND event_date=?",
            pending_chain_resets,
        )
    if pending_positions:
        conn.executemany(
            "INSERT OR IGNORE INTO paper_trade_position ("
            " position_id, station_id, event_date, stage,"
            " decision_at_utc, quote_observed_at_utc,"
            " ladder_center_bucket, window_buckets_json, window_size,"
            " win_probability, all_in_cost, edge, curve_rank,"
            " kelly_fraction, stake_usdc, shares,"
            " probabilities_json, prices_json, center_c,"
            " refused_window_count, content_sha256, selection_rule_sha256,"
            " selection_rule_label, leg_shares_json, resting_fill_json,"
            " posted_buckets_json, intended_legs_json, marginal_coverage_json,"
            " intended_stake_usdc,"
            " intended_win_probability, intended_all_in_cost,"
            " intended_leg_stake_json, leg_stake_json,"
            " held_before_json, chain_shares_json, chain_stake_usdc,"
            " consolidation_json"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
            "?,?,?,?,?,?,?,?,?,?,?)",
            pending_positions,
        )
    if pending_candidates:
        # Replace rather than ignore: an incremental pass re-walks the same
        # station-day and may stop on a different cycle than the last one
        # did, and the row is the answer for that scope, not an event log.
        # The truth columns are left out of the write so a re-walk does not
        # blank a scoring the sweep already did; when the walk moves to a
        # different decision moment the old rows are stale by key and the
        # sweep re-scores whatever it finds unscored.
        conn.executemany(
            "INSERT INTO paper_trade_candidate_window ("
            " candidate_id, station_id, event_date, stage, decision_at_utc,"
            " buckets_json, window_size, win_probability,"
            " raw_win_probability, all_in_price, gate_agreement,"
            " admissible, seated, selection_rule_sha256,"
            " band_axis, band_table_sha256, claim_transform_sha256,"
            " leg_claim_cost_json, seat_shape_rule, market_mid, hours_to_cutoff"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(station_id, event_date, stage, decision_at_utc,"
            "             buckets_json) DO UPDATE SET"
            "  win_probability=excluded.win_probability,"
            "  raw_win_probability=excluded.raw_win_probability,"
            "  all_in_price=excluded.all_in_price,"
            "  gate_agreement=excluded.gate_agreement,"
            "  admissible=excluded.admissible,"
            "  seated=excluded.seated,"
            "  selection_rule_sha256=excluded.selection_rule_sha256,"
            "  band_axis=excluded.band_axis,"
            "  band_table_sha256=excluded.band_table_sha256,"
            "  claim_transform_sha256=excluded.claim_transform_sha256,"
            "  leg_claim_cost_json=excluded.leg_claim_cost_json,"
            "  seat_shape_rule=excluded.seat_shape_rule,"
            "  market_mid=excluded.market_mid,"
            "  hours_to_cutoff=excluded.hours_to_cutoff",
            pending_candidates,
        )
    if pending_settlements:
        conn.executemany(
            "INSERT OR IGNORE INTO paper_trade_settlement ("
            " settlement_id, position_id, station_id, event_date, stage,"
            " settled_tmax_c, settled_bucket, hit, intended_hit,"
            " stake_usdc,"
            " payout_usdc, pnl_usdc, claimed_win_probability,"
            " center_error_c, bucket_distance, settled_at_utc,"
            " truth_revision_no, truth_content_sha256,"
            " basket_all_in_cost"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            pending_settlements,
        )
    # A refusal is the current answer for a scope, not an event log: the next
    # sweep re-walks it and may seat where this one did not, so the row is
    # replaced rather than appended.
    if pending_refusals:
        conn.executemany(
            "INSERT INTO paper_trade_refusal ("
            " station_id, event_date, stage, reason, detail,"
            " cycles_examined, last_decision_at_utc, recorded_at_utc,"
            " selection_rule_sha256"
            ") VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(station_id,event_date,stage) DO UPDATE SET"
            "  reason=excluded.reason, detail=excluded.detail,"
            "  cycles_examined=excluded.cycles_examined,"
            "  last_decision_at_utc=excluded.last_decision_at_utc,"
            "  recorded_at_utc=excluded.recorded_at_utc,"
            "  selection_rule_sha256=excluded.selection_rule_sha256",
            pending_refusals,
        )
    # A scope that seated has no refusal to explain any more.
    if pending_positions:
        conn.executemany(
            "DELETE FROM paper_trade_refusal"
            " WHERE station_id=? AND event_date=? AND stage=?",
            [(row[1], row[2], row[3]) for row in pending_positions],
        )
    conn.commit()
    # Scored after the write, not before it.  The settlement sweep runs at
    # the top of this pass because settlements are re-derived from rows that
    # already exist; candidate windows are written by the walk that just
    # finished, so grading them up there scores the previous pass's rows and
    # leaves this pass's unscored until the next one -- which on a
    # once-a-day cadence is a day of blind bands for no reason.
    for key, value in _score_candidate_windows(conn, truth).items():
        counts[key] += value
    return dict(counts)


def _fill_slice(rows: list) -> dict[str, Any]:
    """Headline numbers for one credibility slice of the settled book."""

    if not rows:
        return {"settled_count": 0}
    hits = sum(int(r[0]) for r in rows)
    staked = sum(float(r[1]) for r in rows)
    pnl = sum(float(r[3]) for r in rows)
    claimed = sum(float(r[4]) for r in rows) / len(rows)
    return {
        "settled_count": len(rows),
        "hit_count": hits,
        "hit_rate": hits / len(rows),
        "claimed_win_probability_mean": claimed,
        "calibration_ratio": (
            (hits / len(rows)) / claimed if claimed > 0 else None
        ),
        "staked_usdc": staked,
        "pnl_usdc": pnl,
        "return_on_stake": pnl / staked if staked > 0 else None,
        "equity_if_only_these_usdc": INITIAL_CAPITAL_USDC + pnl,
    }


def _rule_set_composition(conn: sqlite3.Connection) -> dict[str, Any]:
    """The digests the settled book is made of, and whether it is one book.

    Read off the settlements rather than the positions: the headline return
    is computed from settlements, so the question "is this number one
    strategy" has to be asked of the same rows.
    """

    try:
        rows = conn.execute(
            "SELECT p.selection_rule_sha256, COUNT(*),"
            "       SUM(s.stake_usdc), SUM(s.pnl_usdc)"
            "  FROM paper_trade_settlement s"
            "  JOIN paper_trade_position p ON p.position_id=s.position_id"
            " GROUP BY 1 ORDER BY 2 DESC"
        ).fetchall()
    except sqlite3.DatabaseError:
        return {"single_rule_set": None, "served": SELECTION_RULE_SHA256[:12]}
    members = [
        {
            "sha256": str(sha or "")[:12],
            "served": str(sha or "") == SELECTION_RULE_SHA256,
            "settled_chains": int(count or 0),
            "staked_usdc": round(float(stake or 0.0), 2),
            "pnl_usdc": round(float(pnl or 0.0), 2),
        }
        for sha, count, stake, pnl in rows
    ]
    return {
        "served": SELECTION_RULE_SHA256[:12],
        "single_rule_set": len(members) <= 1,
        "members": members,
        # What the headline would read if only the served rule set counted.
        "served_only": next(
            (
                {
                    "settled_chains": m["settled_chains"],
                    "staked_usdc": m["staked_usdc"],
                    "pnl_usdc": m["pnl_usdc"],
                    "return_on_stake": (
                        round(m["pnl_usdc"] / m["staked_usdc"], 4)
                        if m["staked_usdc"] > 0 else None
                    ),
                }
                for m in members if m["served"]
            ),
            None,
        ),
    }


def report(conn: sqlite3.Connection) -> dict[str, Any]:
    # A report is a read.  This called ``ensure_schema`` so a database
    # without the tables would not raise, and ``ensure_schema`` writes --
    # DDL plus the label backfill -- so ``--report`` on its own opened the
    # book read-only and then died on "attempt to write a readonly
    # database".  The whole point of the read-only path is to read the book
    # while a sweep holds the write lock, so it cannot be the path that
    # needs the lock.  Missing tables are handled where they surface, below.
    try:
        conn.execute("SELECT 1 FROM paper_trade_settlement LIMIT 1")
    except sqlite3.DatabaseError:
        return {
            "schema": "paper_trading_report_v1",
            "settled_count": 0, "open_positions": 0, "stages": {},
        }
    stages: dict[str, Any] = {}
    # Grouped by the stage that last topped the position up, which is not the
    # same question the three-book ledger answered.  These slices partition
    # one book: a chain appears in exactly one of them, and they sum to the
    # book below.  "How far had this position been adjusted when the day
    # resolved" is what a stage means now.
    for stage in STAGES:
        rows = conn.execute(
            "SELECT s.hit, s.stake_usdc, s.payout_usdc, s.pnl_usdc,"
            " s.claimed_win_probability, s.center_error_c,"
            " s.bucket_distance,"
            " COALESCE(s.basket_all_in_cost, p.all_in_cost)"
            " FROM paper_trade_settlement s"
            " JOIN paper_trade_position p ON p.position_id=s.position_id"
            " WHERE s.stage=? ORDER BY s.event_date",
            (stage,),
        ).fetchall()
        # A chain is open when its station-day has no settlement at all, and
        # it belongs to the slice of the stage that last topped it up -- the
        # same partition the settled counts use, so the three slices sum to
        # the book's open count instead of counting one position three times.
        open_positions = conn.execute(
            "SELECT COUNT(*) FROM paper_trade_position p"
            # Same terminal-row key as settlement: accumulation order, not
            # wall-clock decision order.
            " JOIN (SELECT station_id, event_date,"
            "         MAX(rowid) AS last_rowid"
            "       FROM paper_trade_position GROUP BY 1, 2) t"
            "   ON t.station_id=p.station_id AND t.event_date=p.event_date"
            "  AND t.last_rowid=p.rowid"
            " WHERE p.stage=? AND NOT EXISTS ("
            "   SELECT 1 FROM paper_trade_settlement s"
            "   WHERE s.station_id=p.station_id"
            "     AND s.event_date=p.event_date)",
            (stage,),
        ).fetchone()[0]
        if not rows:
            stages[stage] = {
                "settled_count": 0, "open_positions": open_positions
            }
            continue
        hits = sum(int(r[0]) for r in rows)
        staked = sum(float(r[1]) for r in rows)
        pnl = sum(float(r[3]) for r in rows)
        claimed = sum(float(r[4]) for r in rows) / len(rows)
        errors = [float(r[5]) for r in rows if r[5] is not None]
        distances = [int(r[6]) for r in rows if r[6] is not None]
        stages[stage] = {
            "settled_count": len(rows),
            "open_positions": open_positions,
            "hit_count": hits,
            "hit_rate": hits / len(rows),
            "claimed_win_probability_mean": claimed,
            "calibration_ratio": (
                (hits / len(rows)) / claimed if claimed > 0 else None
            ),
            "staked_usdc": staked,
            "pnl_usdc": pnl,
            "return_on_stake": pnl / staked if staked > 0 else None,
            # No equity of its own.  A slice is part of one book, and giving
            # each an initial capital is exactly the three-book reading this
            # ledger was corrected out of.
            "center_bias_c": (
                sum(errors) / len(errors) if errors else None
            ),
            "center_abs_error_c": (
                sum(abs(e) for e in errors) / len(errors)
                if errors else None
            ),
            "miss_distance_buckets_mean": (
                sum(distances) / len(distances) if distances else None
            ),
            # A basket bought for under a cent is almost always a bucket
            # offered at the minimum tick with nothing bid against it -- the
            # known one-sided tail quote, not a tradable price.  Such fills
            # dominate any P&L they appear in (a 0.001 leg returns 1000x), so
            # the book is reported with and without them rather than having
            # them silently set the headline.
            "credible_fill": _fill_slice(
                [r for r in rows if float(r[7]) >= DUST_COST_FLOOR]
            ),
            "dust_offer_fill": _fill_slice(
                [r for r in rows if float(r[7]) < DUST_COST_FLOOR]
            ),
        }
    # The book itself: one bankroll, one row per station-day.
    book_rows = conn.execute(
        "SELECT s.hit, s.stake_usdc, s.pnl_usdc, s.claimed_win_probability,"
        " COALESCE(s.basket_all_in_cost, p.all_in_cost)"
        " FROM paper_trade_settlement s"
        " JOIN paper_trade_position p ON p.position_id=s.position_id"
        " ORDER BY s.event_date"
    ).fetchall()
    credible_rows = [
        row for row in book_rows
        if row[4] is not None and float(row[4]) >= DUST_COST_FLOOR
    ]
    book_pnl = sum(float(r[2]) for r in credible_rows)
    book_staked = sum(float(r[1]) for r in credible_rows)
    book_hits = sum(int(r[0]) for r in credible_rows)
    open_chains = conn.execute(
        "SELECT COUNT(*) FROM ("
        "  SELECT station_id, event_date FROM paper_trade_position p"
        "  WHERE NOT EXISTS (SELECT 1 FROM paper_trade_settlement s"
        "    WHERE s.station_id=p.station_id AND s.event_date=p.event_date)"
        "  GROUP BY station_id, event_date)"
    ).fetchone()[0]
    return {
        "schema": "paper_trading_report_v1",
        "initial_capital_usdc": INITIAL_CAPITAL_USDC,
        "kelly_fraction": KELLY_FRACTION,
        "max_stake_fraction_per_scope": MAX_STAKE_FRACTION_PER_SCOPE,
        "book_model": BOOK_MODEL,
        "generated_at_utc": datetime.now(timezone.utc)
        .isoformat().replace("+00:00", "Z"),
        # Which rule sets the book below is made of.
        #
        # The superseded sweep is bounded by the window a pass can re-derive,
        # which is right -- never delete what you will not rebuild -- and the
        # consequence is that after a rule change the standing book holds two
        # rule sets at once: the scheduled pass re-derives four days and the
        # rest keep the digest they were seated under.  Three knobs moved on
        # 2026-09-12 and the book came to be 276 chains of one strategy and
        # 184 of another, reported as a single return.  A reader who sees one
        # number reads one number, so the composition is on the payload and
        # ``single_rule_set`` says outright whether the headline means
        # anything.  A mixed book is repaired by ``--backfill --recent-days
        # 0``, oldest-first; nothing here does that on its own, because a
        # full re-derive on every knob is an operator's call.
        "rule_sets": _rule_set_composition(conn),
        "book": {
            "settled_chains": len(credible_rows),
            "open_chains": open_chains,
            "hit_count": book_hits,
            "hit_rate": (
                book_hits / len(credible_rows) if credible_rows else None
            ),
            "staked_usdc": book_staked,
            "pnl_usdc": book_pnl,
            "return_on_stake": (
                book_pnl / book_staked if book_staked > 0 else None
            ),
            "equity_usdc": INITIAL_CAPITAL_USDC + book_pnl,
            "return_on_capital": book_pnl / INITIAL_CAPITAL_USDC,
            "dust_chains_excluded": len(book_rows) - len(credible_rows),
        },
        "stages": stages,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--backfill", action="store_true")
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help=(
            "discard recorded positions and settlements before backfilling."
            "  These rows are derived, so a structural rule change (the"
            " ladder floor, the dispersion cap) makes the old ones describe"
            " a strategy that no longer runs."
        ),
    )
    parser.add_argument(
        "--reset-paper-tables",
        action="store_true",
        help=(
            "drop this script's four tables before anything else, so an arm"
            " inherits nothing from the arm that ran before it.  Refused on"
            " the default database -- the standing book lives there.  Use it"
            " on a replay database between arms; --rebuild is NOT a"
            " substitute, because it keeps the candidate windows and, until"
            " 2026-09-11, kept the previous arm's refusals as well."
        ),
    )
    parser.add_argument(
        "--recent-days",
        type=int,
        default=4,
        help=(
            "how far back an incremental --backfill looks, counted from the"
            " newest event day the bridge knows.  Older days have a closed"
            " history and a closed book, so re-reading them only costs the"
            " lock.  0 means no bound.  --rebuild always walks everything."
        ),
    )
    parser.add_argument(
        "--through-date",
        default=None,
        help=(
            "walk only event days at or before this date (YYYY-MM-DD)."
            "  For a rebuild that will not finish inside one sitting:"
            " take it oldest-first, raising this bound each pass, so every"
            " seat is sized off a book whose earlier days are already"
            " re-derived.  Rows above the bound are left untouched."
        ),
    )
    parser.add_argument("--report", action="store_true")
    args = parser.parse_args()
    if args.recent_days is not None and args.recent_days <= 0:
        args.recent_days = None
    database = args.db.expanduser().resolve(strict=True)
    if args.reset_paper_tables:
        # Before the refusal on the default database, so the message names
        # the reason rather than an argument combination.
        if database == DEFAULT_DB.resolve():
            raise SystemExit(
                "--reset-paper-tables refuses the production SSOT:"
                " the standing book is in these tables"
            )
        if not (args.backfill or args.rebuild):
            raise SystemExit(
                "--reset-paper-tables is a step before a walk;"
                " pass --backfill or --rebuild too"
            )
    if args.backfill or args.rebuild:
        conn = _connect(database, writable=True)
        if args.reset_paper_tables:
            print(
                "reset %d paper_trade_* tables in %s"
                % (reset_paper_tables(conn), database.name)
            )
        if args.rebuild:
            ensure_schema(conn)
            # Bounded by the same date the walk is bounded by.  ``--through
            # -date`` promises, in its own help text and in ``backfill``'s,
            # that rows above the bound are left exactly as they are -- and
            # this discard deleted the whole table regardless, so a rebuild
            # taken oldest-first in slices (the workflow that option exists
            # for) wiped every day it had no intention of re-deriving.  The
            # rule is ``_drop_superseded_positions``' rule, written down
            # there since the beginning: never delete outside the window
            # this pass can re-derive, or the page ends up emptier than the
            # ledger.
            bound = (args.through_date or "").strip() or None
            conn.execute(
                "DELETE FROM paper_trade_settlement WHERE position_id IN ("
                "  SELECT position_id FROM paper_trade_position"
                "  WHERE ?1 IS NULL OR event_date <= ?1)",
                (bound,),
            )
            for table in (
                "paper_trade_position", "paper_trade_refusal",
            ):
                conn.execute(
                    f"DELETE FROM {table}"
                    " WHERE ?1 IS NULL OR event_date <= ?1",
                    (bound,),
                )
            conn.commit()
        started = time.monotonic()
        counts = backfill(
            conn,
            recent_days=None if args.rebuild else args.recent_days,
            through_date=(args.through_date or "").strip() or None,
            # A rebuild by hand walks to the end; the scheduled pass holds
            # the resident's writer and stops on its budget.
            budget_sec=None if args.rebuild else REDERIVE_PASS_BUDGET_SEC,
        )
        print(json.dumps(
            {"schema": "paper_trading_backfill_v1", "counts": counts},
            ensure_ascii=False, sort_keys=True,
        ))
        # A repair slice is followed by the next one while this pass can
        # still finish it inside its budget (the hosted timeout is 1800s),
        # judging the next slice by the last one with half again for slack.
        # A slice that did not move the book's cursor stops the loop rather
        # than repeating itself.
        slice_sec = time.monotonic() - started
        walk_cut = bool(counts.get("stopped_for_time"))
        repaired = bool(counts.get("repair_slice_through"))
        repair_stalled = False
        while (
            not walk_cut
            and counts.get("repair_slice_through")
            and time.monotonic() - started + 1.5 * slice_sec
            < REDERIVE_PASS_BUDGET_SEC
        ):
            previous = counts["repair_slice_through"]
            slice_started = time.monotonic()
            counts = backfill(
                conn,
                recent_days=args.recent_days,
                budget_sec=max(
                    0.0, REDERIVE_PASS_BUDGET_SEC - (time.monotonic() - started)
                ),
            )
            walk_cut = bool(counts.get("stopped_for_time"))
            print(json.dumps(
                {"schema": "paper_trading_backfill_v1", "counts": counts},
                ensure_ascii=False, sort_keys=True,
            ))
            slice_sec = time.monotonic() - slice_started
            if counts.get("repair_slice_through") == previous:
                repair_stalled = True
                break
        # A pass that moved the repair and stopped on its budget asks the
        # resident for the next pass now rather than after the sweep's
        # three-hour interval.  Without it a rule change left the book
        # partial for two intervals -- the whole-history walk needs about
        # two budgets -- and a second rule change inside that window started
        # it over: 2026-09-14 had three by 12:51Z.  A repair that did not
        # move stays on the interval, so a stuck one cannot hold the SSOT
        # queue every five minutes.
        if walk_cut or (
            repaired
            and not repair_stalled
            and _repair_slice_through(conn, recent_days=args.recent_days)
        ):
            print(HOSTED_SWEEP_CONTINUE_MARKER)
        if args.report:
            print(json.dumps(report(conn), ensure_ascii=False, sort_keys=True))
        return 0
    conn = _connect(database, writable=False)
    print(json.dumps(report(conn), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
