"""Canonical ``DeltaBasketScore v2`` value and action-selection kernel.

This module is deliberately pure and side-effect free.  It owns:

* the single basket value ``V(q)`` and bounded ``q*`` selection;
* Maker execution utility over a real zero/partial/full fill distribution;
* the explicit zero-valued ``HOLD`` alternative;
* mathematical reservation-price inversion; and
* bounded shared-capital selection across events.

It does not read a clock, database, network, gateway, probability model, or
market registry.  Callers must bind those authorities before constructing the
immutable inputs accepted here.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from itertools import combinations, product
import json
import math
import os
from typing import Any, Mapping, Sequence

from .contracts import EPS, ContractError, content_sha256


VALUE_SCHEMA = "delta_basket_value_v2"
TARGET_SOLUTION_SCHEMA = "delta_basket_target_inventory_v2"
ACTION_SCHEMA = "delta_basket_action_v2"
ACTION_EVALUATION_SCHEMA = "delta_basket_action_evaluation_v2"
ACTION_SELECTION_SCHEMA = "delta_basket_action_selection_v2"
BUNDLE_EVALUATION_SCHEMA = "delta_basket_bundle_evaluation_v2"
BUNDLE_SELECTION_SCHEMA = "delta_basket_bundle_selection_v2"
BUNDLE_RESCORE_SCHEMA = "delta_basket_bundle_rescore_v2"
RESERVATION_SCHEMA = "delta_basket_reservation_price_v2"
PORTFOLIO_SELECTION_SCHEMA = "delta_basket_portfolio_selection_v2"
KERNEL_VERSION = "delta_basket_score_kernel_v2"

# A bundle evaluation retains every joint fill outcome in its auditable result.
# Keep the retained set small enough for the bounded JSON handoff and well
# below the process-memory guard.  This is a kernel constant: a policy artifact
# must never be able to increase the runtime resource envelope.
MAX_BUNDLE_JOINT_OUTCOME_ROWS = 4_096

REQUIRED_HARD_GATES = (
    "unique_ssot",
    "lineage_complete",
    "registry_timezone_matches",
    "bucket_contract_matches",
    "official_ws_fresh",
    "inventory_known",
    "order_state_known",
    "positive_inventory_gap",
    "budget_ok",
    "profit_floor_ok",
    "resource_probe_ok",
    "reservation_price_ok",
)


def _finite(name: str, value: Any) -> float:
    if isinstance(value, bool):
        raise ContractError(f"{name}_invalid")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name}_invalid") from exc
    if not math.isfinite(result):
        raise ContractError(f"{name}_invalid")
    return result


def _nonnegative(name: str, value: Any) -> float:
    result = _finite(name, value)
    if result < 0.0:
        raise ContractError(f"{name}_negative")
    return result


def _positive(name: str, value: Any) -> float:
    result = _finite(name, value)
    if result <= EPS:
        raise ContractError(f"{name}_nonpositive")
    return result


def _probability(name: str, value: Any) -> float:
    result = _nonnegative(name, value)
    if result > 1.0 + EPS:
        raise ContractError(f"{name}_outside_unit_interval")
    return min(1.0, result)


def _text(name: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"{name}_missing")
    return result


def _sha(name: str, value: Any) -> str:
    result = _text(name, value).lower()
    if len(result) != 64 or any(
        character not in "0123456789abcdef" for character in result
    ):
        raise ContractError(f"{name}_invalid_sha256")
    return result


def _canonical_buckets(probabilities: Mapping[str, Any]) -> tuple[str, ...]:
    if not isinstance(probabilities, Mapping) or not probabilities:
        raise ContractError("delta_v2_probability_scope_empty")
    buckets = tuple(sorted(_text("delta_v2_bucket", key) for key in probabilities))
    if len(set(buckets)) != len(buckets):
        raise ContractError("delta_v2_probability_bucket_duplicate")
    return buckets


def _canonical_probability(
    probabilities: Mapping[str, Any],
    buckets: Sequence[str],
) -> dict[str, float]:
    result = {
        bucket: _probability(
            f"delta_v2_probability_{bucket}",
            probabilities[bucket],
        )
        for bucket in buckets
    }
    if abs(sum(result.values()) - 1.0) > 1e-8:
        raise ContractError("delta_v2_probability_mass_invalid")
    return result


def _canonical_map(
    name: str,
    raw: Mapping[str, Any] | None,
    buckets: Sequence[str],
) -> dict[str, float]:
    value = raw if isinstance(raw, Mapping) else {}
    unknown = set(value) - set(buckets)
    if unknown:
        raise ContractError(f"{name}_bucket_unknown")
    return {
        bucket: _nonnegative(f"{name}_{bucket}", value.get(bucket, 0.0))
        for bucket in buckets
    }


def _risk_scope(
    name: str,
    raw: Sequence[str],
    buckets: Sequence[str],
) -> tuple[str, ...]:
    if isinstance(raw, (str, bytes, bytearray)):
        raise ContractError(f"{name}_invalid")
    result = tuple(sorted(set(_text(name, value) for value in raw)))
    if set(result) - set(buckets):
        raise ContractError(f"{name}_bucket_unknown")
    if len(result) > 2:
        raise ContractError(f"{name}_exceeds_two_layer_cap")
    return result


@dataclass(frozen=True)
class ValueWeightsV2:
    lambda_profit_floor: float
    lambda_cvar: float
    lambda_shortfall: float
    lambda_breakthrough: float
    lambda_overfill: float
    lambda_excess_hhi: float
    cvar_alpha: float
    hhi_max: float
    expected_pnl_scale: float
    profit_floor_scale: float
    cvar_scale: float
    coverage_scale: float
    overfill_scale: float
    excess_hhi_scale: float

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ValueWeightsV2":
        if not isinstance(raw, Mapping):
            raise ContractError("delta_v2_value_weights_mapping_required")
        value = cls(
            lambda_profit_floor=_nonnegative(
                "delta_v2_lambda_profit_floor",
                raw.get("lambda_profit_floor"),
            ),
            lambda_cvar=_nonnegative(
                "delta_v2_lambda_cvar", raw.get("lambda_cvar")
            ),
            lambda_shortfall=_nonnegative(
                "delta_v2_lambda_shortfall", raw.get("lambda_shortfall")
            ),
            lambda_breakthrough=_nonnegative(
                "delta_v2_lambda_breakthrough",
                raw.get("lambda_breakthrough"),
            ),
            lambda_overfill=_nonnegative(
                "delta_v2_lambda_overfill", raw.get("lambda_overfill")
            ),
            lambda_excess_hhi=_nonnegative(
                "delta_v2_lambda_excess_hhi",
                raw.get("lambda_excess_hhi"),
            ),
            cvar_alpha=_positive(
                "delta_v2_cvar_alpha", raw.get("cvar_alpha")
            ),
            hhi_max=_positive("delta_v2_hhi_max", raw.get("hhi_max")),
            expected_pnl_scale=_positive(
                "delta_v2_expected_pnl_scale",
                raw.get("expected_pnl_scale"),
            ),
            profit_floor_scale=_positive(
                "delta_v2_profit_floor_scale",
                raw.get("profit_floor_scale"),
            ),
            cvar_scale=_positive(
                "delta_v2_cvar_scale", raw.get("cvar_scale")
            ),
            coverage_scale=_positive(
                "delta_v2_coverage_scale", raw.get("coverage_scale")
            ),
            overfill_scale=_positive(
                "delta_v2_overfill_scale", raw.get("overfill_scale")
            ),
            excess_hhi_scale=_positive(
                "delta_v2_excess_hhi_scale",
                raw.get("excess_hhi_scale"),
            ),
        )
        if value.cvar_alpha > 1.0 + EPS:
            raise ContractError("delta_v2_cvar_alpha_outside_unit_interval")
        if value.hhi_max > 1.0 + EPS:
            raise ContractError("delta_v2_hhi_max_outside_unit_interval")
        return value

    @property
    def content_sha256(self) -> str:
        return content_sha256({
            "schema": "delta_basket_value_weights_v2",
            **asdict(self),
        })


@dataclass(frozen=True)
class ExecutionWeightsV2:
    epsilon_score: float
    lambda_wait: float
    lambda_queue: float
    lambda_adverse_selection: float
    lambda_reprice: float
    lambda_capital_lock: float
    wait_scale: float
    queue_scale: float
    adverse_selection_scale: float
    reprice_scale: float
    capital_lock_scale: float
    maximum_candidates: int = 16

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ExecutionWeightsV2":
        if not isinstance(raw, Mapping):
            raise ContractError("delta_v2_execution_weights_mapping_required")
        try:
            maximum_candidates = int(raw.get("maximum_candidates", 16))
        except (TypeError, ValueError) as exc:
            raise ContractError("delta_v2_maximum_candidates_invalid") from exc
        if maximum_candidates <= 0 or maximum_candidates > 4096:
            raise ContractError("delta_v2_maximum_candidates_out_of_bounds")
        return cls(
            epsilon_score=_nonnegative(
                "delta_v2_epsilon_score", raw.get("epsilon_score")
            ),
            lambda_wait=_nonnegative(
                "delta_v2_lambda_wait", raw.get("lambda_wait")
            ),
            lambda_queue=_nonnegative(
                "delta_v2_lambda_queue", raw.get("lambda_queue")
            ),
            lambda_adverse_selection=_nonnegative(
                "delta_v2_lambda_adverse_selection",
                raw.get("lambda_adverse_selection"),
            ),
            lambda_reprice=_nonnegative(
                "delta_v2_lambda_reprice", raw.get("lambda_reprice")
            ),
            lambda_capital_lock=_nonnegative(
                "delta_v2_lambda_capital_lock",
                raw.get("lambda_capital_lock"),
            ),
            wait_scale=_positive(
                "delta_v2_wait_scale", raw.get("wait_scale")
            ),
            queue_scale=_positive(
                "delta_v2_queue_scale", raw.get("queue_scale")
            ),
            adverse_selection_scale=_positive(
                "delta_v2_adverse_selection_scale",
                raw.get("adverse_selection_scale"),
            ),
            reprice_scale=_positive(
                "delta_v2_reprice_scale", raw.get("reprice_scale")
            ),
            capital_lock_scale=_positive(
                "delta_v2_capital_lock_scale",
                raw.get("capital_lock_scale"),
            ),
            maximum_candidates=maximum_candidates,
        )

    @property
    def content_sha256(self) -> str:
        return content_sha256({
            "schema": "delta_basket_execution_weights_v2",
            **asdict(self),
        })


def _lower_tail_cvar(
    scenario_pnl: Mapping[str, float],
    probabilities: Mapping[str, float],
    alpha: float,
) -> float:
    remaining = alpha
    weighted = 0.0
    for bucket in sorted(scenario_pnl, key=lambda key: (scenario_pnl[key], key)):
        take = min(remaining, probabilities[bucket])
        weighted += take * scenario_pnl[bucket]
        remaining -= take
        if remaining <= EPS:
            break
    if remaining > 1e-8:
        raise ContractError("delta_v2_cvar_probability_support_incomplete")
    return weighted / alpha


def evaluate_basket_value_v2(
    *,
    probabilities: Mapping[str, Any],
    shares_by_bucket: Mapping[str, Any],
    cost_by_bucket: Mapping[str, Any],
    protected_buckets: Sequence[str],
    shortfall_buckets: Sequence[str],
    breakthrough_buckets: Sequence[str],
    weights: ValueWeightsV2,
    target_shares_by_bucket: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate the single normalized economic value ``V(q)``."""

    buckets = _canonical_buckets(probabilities)
    probability = _canonical_probability(probabilities, buckets)
    shares = _canonical_map("delta_v2_shares", shares_by_bucket, buckets)
    costs = _canonical_map("delta_v2_costs", cost_by_bucket, buckets)
    protected = tuple(sorted(set(_text("delta_v2_protected", x) for x in protected_buckets)))
    if not protected or set(protected) - set(buckets):
        raise ContractError("delta_v2_protected_scope_invalid")
    shortfall = _risk_scope(
        "delta_v2_shortfall_scope", shortfall_buckets, buckets
    )
    breakthrough = _risk_scope(
        "delta_v2_breakthrough_scope", breakthrough_buckets, buckets
    )
    target = (
        _canonical_map(
            "delta_v2_target_shares",
            target_shares_by_bucket,
            buckets,
        )
        if target_shares_by_bucket is not None
        else None
    )
    total_cost = sum(costs.values())
    scenario_pnl = {
        bucket: shares[bucket] - total_cost for bucket in buckets
    }
    expected_pnl = sum(
        probability[bucket] * scenario_pnl[bucket] for bucket in buckets
    )
    # The settled-outcome PnL curve's spread.  ``scenario_pnl`` already spans
    # every bucket -- an unheld bucket settles at minus the basket's cost --
    # so the outcome space is a complete partition and this is the honest
    # variance of the curve, not of the held legs alone.
    variance = sum(
        probability[bucket]
        * (scenario_pnl[bucket] - expected_pnl) ** 2
        for bucket in buckets
    )
    profit_floor = min(scenario_pnl[bucket] for bucket in protected)
    cvar = _lower_tail_cvar(
        scenario_pnl,
        probability,
        weights.cvar_alpha,
    )
    shortfall_coverage = sum(
        probability[bucket] * shares[bucket] for bucket in shortfall
    )
    breakthrough_coverage = sum(
        probability[bucket] * shares[bucket] for bucket in breakthrough
    )
    overfill = (
        sum(max(0.0, shares[bucket] - target[bucket]) ** 2 for bucket in buckets)
        if target is not None
        else 0.0
    )
    total_shares = sum(shares.values())
    hhi = (
        sum((shares[bucket] / total_shares) ** 2 for bucket in buckets)
        if total_shares > EPS
        else 0.0
    )
    excess_hhi = max(0.0, hhi - weights.hhi_max) ** 2
    normalized = {
        "expected_pnl": expected_pnl / weights.expected_pnl_scale,
        "profit_floor": profit_floor / weights.profit_floor_scale,
        "cvar": cvar / weights.cvar_scale,
        "shortfall_coverage": (
            shortfall_coverage / weights.coverage_scale
        ),
        "breakthrough_coverage": (
            breakthrough_coverage / weights.coverage_scale
        ),
        "overfill": overfill / weights.overfill_scale,
        "excess_hhi": excess_hhi / weights.excess_hhi_scale,
    }
    value = (
        normalized["expected_pnl"]
        + weights.lambda_profit_floor * normalized["profit_floor"]
        + weights.lambda_cvar * normalized["cvar"]
        + weights.lambda_shortfall * normalized["shortfall_coverage"]
        + weights.lambda_breakthrough
        * normalized["breakthrough_coverage"]
        - weights.lambda_overfill * normalized["overfill"]
        - weights.lambda_excess_hhi * normalized["excess_hhi"]
    )
    identity = {
        "schema": VALUE_SCHEMA,
        "kernel_version": KERNEL_VERSION,
        "weights_sha256": weights.content_sha256,
        "value": value,
        "expected_pnl": expected_pnl,
        "variance": variance,
        "profit_floor": profit_floor,
        "cvar": cvar,
        "shortfall_coverage": shortfall_coverage,
        "breakthrough_coverage": breakthrough_coverage,
        "overfill": overfill,
        "hhi": hhi,
        "excess_hhi": excess_hhi,
        "normalized_terms": normalized,
        "scenario_pnl": scenario_pnl,
        "probabilities": probability,
        "shares_by_bucket": shares,
        "cost_by_bucket": costs,
        "target_shares_by_bucket": target,
        "protected_buckets": list(protected),
        "shortfall_buckets": list(shortfall),
        "breakthrough_buckets": list(breakthrough),
    }
    return {**identity, "content_sha256": content_sha256(identity)}


VALUE_OBJECTIVE_FORM = "value_v2"
CURVE_OBJECTIVE_FORM = "fractional_log_wealth_curve_v1"
# Charter 72 (user ruling, 2026-09-03): the size is the whole-basket Kelly
# stake the paper ledger has measured every charter under -- the seat as a
# binary bet against the whole book, a quarter of full Kelly, capped at the
# scope's share of the book.  The curve objective keeps only its legality
# floor: under this form the solver's winner is the Kelly-sized candidate
# when it clears the floor and the baseline (hold) when it does not; the
# curve score is still computed, for the audit, and decides nothing.
KELLY_OBJECTIVE_FORM = "whole_basket_kelly_v1"
KELLY_CANDIDATE_ORIGIN = "WHOLE_BASKET_KELLY"
PRODUCTION_SIZING_LAW = KELLY_OBJECTIVE_FORM
# Charter 73 (user ruling, 2026-09-03): what authorizes a bundle at the
# execution layer.  ``joint_value_v2`` is the penalized value_v2 delta over
# the joint fill lattice (floor, CVaR, overfill, concentration) -- the
# per-outcome judge that, applied to the ledger's seats, refuses 170 of
# 394 with a positive expected pnl and +174.6 of its +358.9.
# ``whole_basket_edge_v1`` reads what the ledger reads: the increment's
# expected payout on the seated window's charter-68 partition, less what
# it costs.  The value delta is still computed, for the audit.
EXECUTION_AUTHORIZATION_JOINT_VALUE = "joint_value_v2"
EXECUTION_AUTHORIZATION_WHOLE_BASKET_EDGE = "whole_basket_edge_v1"
# Charter 74 (user ruling, 2026-09-03): only P0 opens a station-day.  A P0
# the Kelly law sizes to nothing still opens the chain with a ticket --
# one minimum clip per support leg, above the dust floor -- so P0-M and
# P1 have a position to add to; a later stage never opens an empty chain.
# Measured with production's own judge on the replay book: ticket and
# add-only +48.7% on 938 staked, P1 opening alone +15.7% on 3029.
CHAIN_TICKET_CANDIDATE_ORIGIN = "CHAIN_TICKET"
CHAIN_TICKET_DUST_FLOOR_USDC = 0.01
CHAIN_OPENING_STAGE = "P0"
EXECUTION_AUTHORIZATION_CHAIN_TICKET = "chain_ticket_v1"
EXECUTION_AUTHORIZATION_RULES = frozenset({
    EXECUTION_AUTHORIZATION_JOINT_VALUE,
    EXECUTION_AUTHORIZATION_WHOLE_BASKET_EDGE,
    EXECUTION_AUTHORIZATION_CHAIN_TICKET,
})


def chain_ticket_cash_v1(*, minimum_clip_usdc: float, legs: int) -> float:
    """Charter 74's ticket: one executable clip per leg, above dust."""

    return max(
        float(minimum_clip_usdc) * int(legs),
        CHAIN_TICKET_DUST_FLOOR_USDC * 2.0,
    )
PRODUCTION_EXECUTION_AUTHORIZATION = EXECUTION_AUTHORIZATION_WHOLE_BASKET_EDGE
OBJECTIVE_FORMS = frozenset({
    VALUE_OBJECTIVE_FORM, CURVE_OBJECTIVE_FORM, KELLY_OBJECTIVE_FORM,
})
DEFAULT_KELLY_FRACTION = 0.25
# How much of the whole book one station-day may put at risk, and how much of
# that allowance each stage of its chain may reach.
#
# This lives here, in the kernel, because it is one rule and there must not be
# a second copy of it.  Production capped a station at a flat 30 USDC while
# the paper ledger capped it at 2% of a book that moves with its own P&L, so
# the same station-day was sized two different ways depending on which side
# you asked -- and the ledger is the side every measured ROI figure comes
# from.  User ruling, 2026-08-26: the floating fraction is the rule, and both
# sides read it from here.
#
# The scope is the station-day, not the station-day-stage.  One book means one
# position per station-day, so the cap binds the whole chain: whatever P0
# spent is spent, and a top-up at P0-M or P1 draws on what is left of the same
# 2%.  Forty-five stations trading daily means an unbounded Kelly outlier
# would otherwise dominate the curve.
MAX_STAKE_FRACTION_PER_SCOPE = 0.02
# Each stage's reach into that one allowance.  P0 opens with part of it held
# back, because the two later stages exist to add on evidence P0 did not have;
# P1 may reach the whole of it.
STAGE_BUDGET_SHARE = {"P0": 0.6, "P0-M": 0.8, "P1": 1.0}


def scope_stake_cap_usdc(
    *,
    book_equity_usdc: float,
    stage: str = "",
    committed_usdc: float = 0.0,
    scope_fraction: float = MAX_STAKE_FRACTION_PER_SCOPE,
) -> float:
    """What this station-day may still spend, in USDC.

    ``book_equity_usdc`` is the whole book, not the station's allotment: the
    cap is a share of what we have, so it grows with a winning book and
    shrinks with a losing one.  A flat per-station number cannot do that, and
    a flat number is what production carried until 2026-08-26.

    ``committed_usdc`` is what this station-day has already spent -- filled
    and confirmed-pending alike, because both have used part of the
    allowance whether or not they have settled.
    """

    equity = float(book_equity_usdc)
    if not equity > 0.0:
        return 0.0
    share = float(STAGE_BUDGET_SHARE.get(str(stage), 1.0))
    room = equity * float(scope_fraction) * share - float(committed_usdc)
    return max(0.0, room)
# The basket promise, made enforceable (user ruling, 2026-08-13): a settlement
# inside the support may cost at most this fraction of the basket's premium.
# Measured over the 21 most recent plans, the unconstrained optimizer was a
# faithful mispricing harvester and an unfaithful basket machine -- median win
# probability 29%, median best-case odds 7.3x, median spike ratio 0.92, with
# support buckets that held a positive *target* and still settled at -97% of
# cost.  The objective could not fix this because at these stakes log wealth
# is numerically expected value, and the cheap-tail edge the market offers is
# convex; the shape had to become a legality condition, not a preference.
# -0.10 rather than 0.0 keeps a small ability to lean toward a genuinely
# mispriced bucket; the zero-cost baseline always satisfies it, so a book
# that only offers lottery shapes resolves to HOLD instead of a lottery.
# Charter 70 (user ruling, 2026-09-02): a leg whose solved target is under the
# exchange's minimum lot is raised to one lot when the basket can afford it.
#
# The ruling it replaces -- drop a sub-lot leg rather than round it up -- was
# written for a book sized off a flat 30 USDC, where a sub-lot leg meant the
# objective had chosen a small position.  Sized off the pool's 2% (6.17 USDC
# for a P0 open on the current book) a three- or four-rung ladder routinely
# solves one rung to two or three shares, below the five the exchange will
# fill, and a basket has to be bought whole: the missing leg refused the
# whole ladder.  Measured 2026-09-02, 11 of the 17 legs refused on positive-
# edge forward baskets were under a lot; every one of those baskets was
# affordable with the raise.
#
# This lives in the kernel, like the stake cap, because the paper ledger and
# production must size the same station-day the same way and both read it
# from here.  ``smallest_leg`` raises every rung under a lot -- a basket with
# one rung still under is still not buyable -- and raises none when the sum
# of the raises would exceed what the scope may still spend; ``off`` is the
# pre-charter-70 book, kept so a replay can re-derive it.
EXCHANGE_LOT_TOP_UP_MODES = ("off", "smallest_leg")
EXCHANGE_LOT_TOP_UP = "smallest_leg"


def raise_sub_lot_targets_v2(
    *,
    target_shares_by_bucket: Mapping[str, Any],
    support: Sequence[str],
    min_lot_by_bucket: Mapping[str, Any],
    ask_by_bucket: Mapping[str, Any],
    capital_available_usdc: Any,
    mode: str = EXCHANGE_LOT_TOP_UP,
) -> dict[str, Any]:
    """Raise every seated rung under one exchange lot to exactly one lot.

    Pure and deterministic.  ``min_lot_by_bucket`` is the exchange's stated
    minimum per market (0 when unstated -- such a rung is left alone, it is
    not known to be under anything); ``ask_by_bucket`` prices the raise.  The
    raise is all-or-none over the support: a basket that can afford to lift
    only some of its short rungs to a lot still cannot be bought, so nothing
    is changed and the refusal is named.  Affordability is the whole basket's
    -- the solved targets at their asks plus the raises -- against
    ``capital_available_usdc``, which is what the scope may still spend.
    """

    if mode not in EXCHANGE_LOT_TOP_UP_MODES:
        raise ContractError("delta_v2_exchange_lot_top_up_mode_invalid")
    targets = {
        str(bucket): _nonnegative("delta_v2_target_shares", value)
        for bucket, value in target_shares_by_bucket.items()
    }
    unchanged = {
        "schema": "delta_v2_exchange_lot_top_up_v1",
        "mode": mode,
        "target_shares_by_bucket": dict(targets),
        "raised_buckets": (),
        "lot_raise_usdc": 0.0,
        "requested_lot_raise_usdc": 0.0,
        "refused_reason": None,
    }
    if mode == "off":
        return unchanged
    capital = _nonnegative("delta_v2_capital_available", capital_available_usdc)
    raised: list[str] = []
    lots: dict[str, float] = {}
    extra = 0.0
    committed = 0.0
    for raw_bucket in support:
        bucket = str(raw_bucket)
        target = targets.get(bucket, 0.0)
        lot = _nonnegative(
            "delta_v2_min_lot", min_lot_by_bucket.get(bucket, 0.0) or 0.0
        )
        ask = _nonnegative(
            "delta_v2_ask", ask_by_bucket.get(bucket, 0.0) or 0.0
        )
        committed += target * ask
        if lot <= EPS or ask <= EPS or target <= EPS or target + EPS >= lot:
            continue
        raised.append(bucket)
        lots[bucket] = lot
        extra += (lot - target) * ask
    if not raised:
        return unchanged
    if committed + extra > capital + EPS:
        return {
            **unchanged,
            "requested_lot_raise_usdc": extra,
            "refused_reason": "exchange_lot_top_up_unaffordable",
        }
    for bucket in raised:
        targets[bucket] = lots[bucket]
    return {
        **unchanged,
        "target_shares_by_bucket": targets,
        "raised_buckets": tuple(raised),
        "lot_raise_usdc": extra,
        "requested_lot_raise_usdc": extra,
    }


SUPPORT_SCENARIO_FLOOR_FRACTION = -0.10
# Effective edge (user ruling, charter 68, 2026-09-01).  Nominal edge --
# claim minus price -- realizes as an inverted U in the market's agreement
# with us, a = price / claim.  Measured on the 189 settled opening seats
# before 2026-08-27, the realized/claimed ratio by agreement band runs
# 0.14 / 0.60 / 0.87 / 0.82: where the book disagrees most and the nominal
# edge is largest, five sixths of the claim never arrives, and the only
# positive strip of e_eff = p*(lambda(a) - a) sits at a in [0.70, 0.85].
# The objective was climbing the left cliff because it read p; reading
# lambda(a)*p instead leaves the objective's form untouched -- fractional
# log wealth, one ranking, one solver -- and lets the inverted U fall out
# of it.  The 08-27..30 collapse is what the left cliff costs: -67% on the
# opening stage against -8.8% before it.
#
# Fitted on the early window ALONE, deliberately: a table fitted on the
# whole sample would let the late collapse grade its own homework.  The
# forward half of that split is the evidence -- +41.0 points, p=0.007.
#
# P1 is exempt and the exemption is the point: its ratio runs 1.09-1.23
# exactly where the market disagrees, because its edge is the observation
# rather than the shape.  Shrinking it would gut the one stage that earns.
EFFECTIVE_EDGE_LAMBDA = (
    (0.50, 0.14),
    (0.70, 0.60),
    (0.85, 0.87),
    (float("inf"), 0.82),
)
# Research arm (2026-09-04): a P0-specific table handed in by env as JSON
# [[bound, lambda], ...].  Production never sets it; the paper ledger hashes
# it into its rule fingerprint when set, so an arm cannot pass for the book.
EFFECTIVE_EDGE_LAMBDA_P0_OVERRIDE = os.environ.get(
    "PM_EFFECTIVE_EDGE_LAMBDA_P0_JSON", ""
).strip()
if EFFECTIVE_EDGE_LAMBDA_P0_OVERRIDE:
    EFFECTIVE_EDGE_LAMBDA = tuple(
        (float(bound), float(lam))
        for bound, lam in json.loads(EFFECTIVE_EDGE_LAMBDA_P0_OVERRIDE)
    )
# Each stage reads its own table (user ruling, 2026-09-05).  P0-M shared P0's
# for three days and contributed no seat to the pooled fit in any 28-day
# window measured -- the shared table was P0's table wearing two names.  Its
# own constant starts equal to P0's so nothing served moves today, and the
# store fits its scope on its own seats from here; a band without twelve of
# them keeps this incumbent, which is exactly what it was served before.
EFFECTIVE_EDGE_STAGES = frozenset({"P0"})
EFFECTIVE_EDGE_P0M_LAMBDA = (
    (0.50, 0.14),
    (0.70, 0.60),
    (0.85, 0.87),
    (float("inf"), 0.82),
)
EFFECTIVE_EDGE_P0M_LAMBDA_OVERRIDE = os.environ.get(
    "PM_EFFECTIVE_EDGE_LAMBDA_P0M_JSON", ""
).strip()
if EFFECTIVE_EDGE_P0M_LAMBDA_OVERRIDE:
    EFFECTIVE_EDGE_P0M_LAMBDA = tuple(
        (float(bound), float(lam))
        for bound, lam in json.loads(EFFECTIVE_EDGE_P0M_LAMBDA_OVERRIDE)
    )
EFFECTIVE_EDGE_P0M_STAGES = frozenset({"P0-M"})
# P1's own table (user ruling, 2026-09-02).  The exemption above was a
# statement about a stage; this makes it a table about that stage, so the
# same machinery -- one function, one rolling fit, one journal -- covers all
# three and P1's ratio is measured rather than assumed.
#
# Fitted on the production book's own 113 settled P1 seats, 2026-08-15..09-01,
# per agreement band a = price / claim, realized / claimed:
#
#     a < 0.50   1.102   (47 seats, +113.7%)    early/late split 0.84 / 1.21
#     0.50-0.70  1.035   (31 seats,  +16.7%)                     1.10 / 1.01
#     0.70-0.85  0.831   (20 seats,   +7.7%)                     0.54 / 0.92
#     a >= 0.85  1.113   (15 seats,  +28.3%)                     1.39 / 1.08
#
# Not an inverted U.  Where the market disagrees most, P1 over-delivers and
# earns most -- the mirror of P0 -- because its edge is the observation.  The
# served value is capped at one: the exemption was "trust it", never "trust it
# more than it trusts itself".  That leaves one candidate discount, the
# 0.70-0.85 band at 0.83, and it is NOT promoted: its early half is five seats
# (inadmissible) reading 0.54 and its late half reads 0.92, so no stable
# forward evidence yet.  The store fits this scope separately each day; a
# person promotes a band here when the journal shows it holding.  Until then
# the table is identity and P1 sizes exactly as it did.
#
# The replay arms of 2026-09-02 measured +6 points from a P1 table -- on a
# replay book rebuilt from a fresh bankroll, whose bands read 0.80 where this
# book reads 1.10.  The replay's own four arms stay valid against each other;
# its bands do not transfer to production's constant, and this one comes from
# production's book.
EFFECTIVE_EDGE_P1_LAMBDA = (
    (0.50, 1.00),
    (0.70, 1.00),
    (0.85, 1.00),
    (float("inf"), 1.00),
)
EFFECTIVE_EDGE_P1_STAGES = frozenset({"P1"})
# Charter 68, bucket scope (research arm, 2026-09-03).  The window table
# above deflates every rung of a window by one factor read off the whole
# window's agreement.  Measured per leg on the same settled book the shape
# is not uniform: the cheap tails the market disagrees with realize a
# fifth of their claim, the dear centre rungs the market agrees with
# realize more than claimed.  Fitted on the early half alone (settled
# P0/P0-M legs, event days 2026-08-15..24: 220 / 100 / 57 / 248 legs per
# band, realized/claimed 0.216 / 0.425 / 0.803 / 1.377); the late half
# (08-25..09-02) reads 0.208 / 0.502 / 0.750 / 1.326 and is the scoring
# window, so the table is not grading its own homework.  Served only under
# EFFECTIVE_EDGE_SCOPE == "bucket".  Measured 2026-09-03 on that scoring
# window (416 seats, 197 settled, same seats either way): bucket scope
# staked 917 against window's 800 and returned +263 against +355, ROI
# +28.7% against +44.4%, and no single station's excision flips the
# order.  Window stays the rule; this table is the research arm.
EFFECTIVE_EDGE_BUCKET_LAMBDA = (
    (0.50, 0.22),
    (0.70, 0.43),
    (0.85, 0.80),
    (float("inf"), 1.38),
)
EFFECTIVE_EDGE_SCOPES = ("window", "bucket")


def effective_edge_lambda(
    agreement: float,
    table: Sequence[tuple[float, float]] | None = None,
) -> float:
    """Realized fraction of a claim, given how far the book agrees with it.

    ``agreement`` is price over claim.  Below the first bound the book is
    calling us wrong and is measured to be right five times in six; above
    the last the sliver left is eaten by friction.

    ``table`` is the rolling fit from ``effective_edge_store`` when one has
    been activated.  The constant above is the bootstrap and stays the
    fallback: a lane that cannot reach the database still prices the
    inverted U, it just prices it with the numbers charter 68 shipped.
    """

    bands = tuple(table) if table else EFFECTIVE_EDGE_LAMBDA
    value = float(agreement)
    if not math.isfinite(value) or value <= 0.0:
        return float(bands[0][1])
    for bound, lam in bands:
        if value < bound:
            return float(lam)
    return float(bands[-1][1])


def effective_edge_lambda_for_stage(
    stage: str,
    agreement: float,
    *,
    table: Sequence[tuple[float, float]] | None = None,
    p1_table: Sequence[tuple[float, float]] | None = None,
    p0m_table: Sequence[tuple[float, float]] | None = None,
) -> float:
    """The realized fraction of a claim, for the stage that made it.

    One dispatch both lanes call, so the question "does this stage get a
    lambda, and which" has one answer.  P0 and P0-M read the charter 68 table
    (or the rolling fit handed in as ``table``); P1 reads its own (or
    ``p1_table``, which is how a replay arm hands in a per-day fit); any other
    stage reads one, because no table has been fitted for it and an unfitted
    shrink is a guess wearing a number.
    """

    name = str(stage or "").strip().upper()
    if name in EFFECTIVE_EDGE_STAGES:
        return effective_edge_lambda(agreement, table)
    if name in EFFECTIVE_EDGE_P0M_STAGES:
        return effective_edge_lambda(
            agreement,
            tuple(p0m_table) if p0m_table else EFFECTIVE_EDGE_P0M_LAMBDA,
        )
    if name in EFFECTIVE_EDGE_P1_STAGES:
        return effective_edge_lambda(
            agreement, tuple(p1_table) if p1_table else EFFECTIVE_EDGE_P1_LAMBDA
        )
    return 1.0


def effective_edge_lambda_from_tables(
    stage: str,
    agreement: float,
    tables: "Mapping[str, Sequence[tuple[float, float]] | None] | None",
) -> float:
    """The stage's lambda read off the served tables, one unpacking for both
    lanes.  ``tables`` is the store's scope->bands mapping; a missing or None
    scope reads the kernel constant, which is the bootstrap before a scope's
    first activation."""

    tables = tables or {}
    return effective_edge_lambda_for_stage(
        stage,
        agreement,
        table=tables.get("P0") or None,
        p0m_table=tables.get("P0-M") or None,
        p1_table=tables.get("P1") or None,
    )


def effective_edge_bucket_lambda_for_stage(
    stage: str,
    agreement: float,
    *,
    p1_table: Sequence[tuple[float, float]] | None = None,
) -> float:
    """The per-leg realized fraction, for the bucket-scope arm of charter 68.

    ``agreement`` is one leg's price over that leg's claim.  P0 and P0-M
    read the bucket table; P1 keeps its own (identity) table because its
    edge is the observation, not the shape, and a per-leg deflation of it
    was never fitted.
    """

    name = str(stage or "").strip().upper()
    # The bucket arm's table was fitted on P0 and P0-M pooled and is a
    # research arm; both keep reading it until a per-stage bucket fit exists.
    if name in EFFECTIVE_EDGE_STAGES or name in EFFECTIVE_EDGE_P0M_STAGES:
        return effective_edge_lambda(agreement, EFFECTIVE_EDGE_BUCKET_LAMBDA)
    if name in EFFECTIVE_EDGE_P1_STAGES:
        return effective_edge_lambda(
            agreement, tuple(p1_table) if p1_table else EFFECTIVE_EDGE_P1_LAMBDA
        )
    return 1.0
# Charter 71 (user ruling, 2026-09-03): the two hard gates on a top-up.
# (a) A stage may add to a held chain only after the probability it is
#     served has changed since the chain last bought -- the trigger is the
#     model, never the book.  Measured on the replay book with production's
#     own judge: 77% of its 545 P0 top-ups fired on cycles where the
#     probability had not moved, only the offer or the room had.
# (b) The chain-growth gate (charter 66 clause B) reads the charter-68
#     partition, lambda*p, not the served claim: 61% of those top-ups had a
#     deflated increment edge at or below zero and still passed a gate that
#     read the undeflated claim.
# The partition and the growth number are one implementation here, imported
# by production's solver and by the paper ledger; the ledger hashes the
# version into its rule fingerprint.
CHARTER_71_TOPUP_GATE_VERSION = (
    "charter_71_probability_change_and_effective_growth_v1"
)
# Charter 77 (user ruling, 2026-09-04): a decision moment is a change in
# the model -- a new served probability identity -- or a change in
# whether the day has any window clearing the whole-basket positive-EV
# gate.  A quote refresh that changes neither is not a moment: the book
# moving is not a reason to decide, the same way charter 71(a) made it
# not a reason to top up.  The ledger walks the same moments.
DECISION_MOMENT_RULE_VERSION = "checkpoint_or_admissibility_v1"
EDGE_STATE_ADMISSIBLE = "admissible"
EDGE_STATE_NONE = "none"


def edge_state_v1(windows: Sequence[Mapping[str, Any]]) -> str:
    """Whether any enumerated window clears the positive-EV gate.

    Read off the enumeration the seat is chosen from: a priced window
    whose claim exceeds its all-in price.  Which window it is, and by
    how much, is the quote's business and not a moment of its own.
    """

    for row in windows:
        if (
            row.get("admissible", True)
            and row.get("all_in_price") is not None
            and row.get("edge") is not None
            and float(row["edge"]) > 0.0
        ):
            return EDGE_STATE_ADMISSIBLE
    return EDGE_STATE_NONE


def is_decision_moment_v1(
    *,
    probability_identity: str | None,
    edge_state: str,
    last_probability_identity: str | None,
    last_edge_state: str | None,
) -> bool:
    """True when the model or the day's admissibility changed since the
    last recorded decision; always true with no recorded decision."""

    if not last_probability_identity:
        return True
    if str(probability_identity or "") != str(last_probability_identity):
        return True
    return str(edge_state) != str(last_edge_state or "")


def whole_basket_kelly_stake_v1(
    *,
    win_probability: float,
    cost: float,
    book_equity_usdc: float,
    kelly_fraction: float = DEFAULT_KELLY_FRACTION,
    scope_fraction: float = MAX_STAKE_FRACTION_PER_SCOPE,
) -> tuple[float, float]:
    """Charter 72's size: fractional Kelly on the basket as a binary bet.

    A basket costing ``cost`` all-in per share and paying 1 when the truth
    lands inside it has Kelly fraction (p - c) / (1 - c); a quarter of that
    is served, capped at the scope's share of the book, and the stake is
    that fraction of the whole book -- never of a station's budget, which
    is a cap and not a bankroll.  ``win_probability`` is the charter-68
    deflated claim.  Returns (fraction, stake_usdc); both zero when the
    claim does not clear the cost.  Lifted from the paper ledger, which
    measured every charter since 2026-08-15 under it.
    """

    cost = float(cost)
    if cost <= 0.0 or cost >= 1.0:
        return 0.0, 0.0
    full = (float(win_probability) - cost) / (1.0 - cost)
    if full <= 0.0:
        return 0.0, 0.0
    fraction = min(float(kelly_fraction) * full, float(scope_fraction))
    return fraction, max(0.0, fraction * float(book_equity_usdc))


def whole_basket_edge_delta_v1(
    *,
    edge_probabilities: Mapping[str, Any],
    current_shares: Mapping[str, Any],
    current_cost: Mapping[str, Any],
    resulting_shares: Mapping[str, Any],
    resulting_cost: Mapping[str, Any],
) -> float:
    """Charter 73's number: what the increment pays, less what it costs.

    Expected payout of the shares this action adds, each share paying 1
    when its bucket settles, read on the belief the seat was judged on
    (the charter-68 partition), minus the cash it spends.  No penalty
    terms: those stay in value_v2 and in the audit.
    """

    buckets = set(resulting_shares) | set(current_shares)
    payout = sum(
        float(edge_probabilities.get(bucket, 0.0) or 0.0)
        * (
            float(resulting_shares.get(bucket, 0.0))
            - float(current_shares.get(bucket, 0.0))
        )
        for bucket in buckets
    )
    spent = sum(
        float(resulting_cost.get(bucket, 0.0))
        - float(current_cost.get(bucket, 0.0))
        for bucket in set(resulting_cost) | set(current_cost)
    )
    return payout - spent


def probability_vector_identity_v1(probabilities: Mapping[str, float]) -> str:
    """The identity charter 71(a) compares: the served vector itself.

    Not the envelope's content sha and not the posterior sha -- both
    churn on the clock (ZGGG 2026-09-01 P0: 64 checkpoints, 64 shas,
    7 distinct vectors), so a gate keyed on them never binds.  Each
    bucket's probability rounded to six places, sorted by bucket, then
    content-hashed; the same rounding on both sides.
    """

    return content_sha256([
        [str(bucket), round(float(value), 6)]
        for bucket, value in sorted(
            probabilities.items(), key=lambda item: str(item[0])
        )
    ])


def deflate_window_partition_v1(
    probabilities: Mapping[str, float],
    window: Sequence[str],
    *,
    bucket_factors: Mapping[str, float] | None = None,
    claim_factor: float = 1.0,
) -> dict[str, float]:
    """Deflate a window's claim and keep the distribution a partition.

    Bucket scope (``bucket_factors``): each rung keeps its own corrected
    mass and the mass the window gives up, or takes, is balanced against
    the rest so the scenario space stays a partition.  Window scope
    (``claim_factor``): the window's buckets are deflated by one factor and
    the mass they give up returns to the buckets outside it.  Either way
    the result is renormalised, so the ranking, the sizing and the
    charter-71 growth gate read one belief.  Lifted from production's
    solver on 2026-09-03 so the paper ledger reads the same partition.
    """

    inside_set = {str(bucket) for bucket in window}
    factors = {
        str(bucket): float(factor)
        for bucket, factor in (bucket_factors or {}).items()
    }
    if factors and any(abs(factor - 1.0) > EPS for factor in factors.values()):
        inside = sum(
            float(probabilities.get(bucket, 0.0)) for bucket in inside_set
        )
        outside = max(0.0, 1.0 - inside)
        corrected_inside = sum(
            float(probabilities.get(bucket, 0.0)) * factors.get(bucket, 1.0)
            for bucket in inside_set
        )
        if corrected_inside > 1.0 - EPS:
            shrink = (1.0 - EPS) / corrected_inside
            factors = {
                bucket: factor * shrink for bucket, factor in factors.items()
            }
            corrected_inside = 1.0 - EPS
        outside_scale = (
            (1.0 - corrected_inside) / outside if outside > EPS else 0.0
        )
        corrected = {
            str(bucket): (
                float(value) * factors.get(str(bucket), 1.0)
                if str(bucket) in inside_set
                else float(value) * outside_scale
            )
            for bucket, value in probabilities.items()
        }
    elif abs(float(claim_factor) - 1.0) > EPS:
        inside = sum(
            float(probabilities.get(bucket, 0.0)) for bucket in inside_set
        )
        outside = max(0.0, 1.0 - inside)
        corrected_inside = min(1.0 - EPS, inside * float(claim_factor))
        outside_scale = (
            (1.0 - corrected_inside) / outside if outside > EPS else 0.0
        )
        corrected = {
            str(bucket): (
                float(value) * float(claim_factor)
                if str(bucket) in inside_set
                else float(value) * outside_scale
            )
            for bucket, value in probabilities.items()
        }
    else:
        return {
            str(bucket): float(value) for bucket, value in probabilities.items()
        }
    total = sum(corrected.values())
    if total <= EPS:
        return {
            str(bucket): float(value) for bucket, value in probabilities.items()
        }
    return {bucket: value / total for bucket, value in corrected.items()}


def chain_topup_growth_v1(
    *,
    held: Mapping[str, float],
    bought: Mapping[str, float],
    probabilities: Mapping[str, float],
    spend: float,
    bankroll: float,
    miss_floor: float = 0.0,
) -> float:
    """Charter 66 clause B's number: what a buy does to expected log wealth.

    From here, not from flat: the chain's held payout curve is the initial
    condition and its past spend is sunk.  The frame is the scope's own
    risk capital; a buy that consumes the whole frame is minus infinity by
    construction, which is the right answer for a log bettor.  Under
    charter 71(b) ``probabilities`` is the charter-68 partition (lambda*p,
    renormalised), never the served claim.  ``miss_floor`` is the ledger's
    replay arm (off by default): reserve at least that much mass for "none
    of ours settles", deflating the in-mass proportionally.
    """

    frame = float(bankroll)
    cost = float(spend)
    if frame <= 0.0 or frame - cost <= 1e-9:
        return float("-inf")
    buckets = set(held) | set(bought)
    mass_in = sum(float(probabilities.get(bucket, 0.0)) for bucket in buckets)
    deflate = 1.0
    if miss_floor > 0.0 and mass_in > 1.0 - miss_floor:
        deflate = (1.0 - miss_floor) / mass_in
        mass_in *= deflate
    p_out = max(0.0, 1.0 - mass_in)
    growth = p_out * (math.log(frame - cost) - math.log(frame))
    for bucket in buckets:
        mass = float(probabilities.get(bucket, 0.0)) * deflate
        if mass <= 0.0:
            continue
        before = frame + float(held.get(bucket, 0.0))
        after = (
            frame - cost
            + float(held.get(bucket, 0.0))
            + float(bought.get(bucket, 0.0))
        )
        growth += mass * (math.log(after) - math.log(before))
    return growth


# Charter 67 (user ruling, 2026-08-30): the local hour before which P1 may
# not take a seat.  The P1 lane starts compiling at local midnight, and both
# lanes' entry rules were spending the stage's budget on a midnight book with
# one observation behind it.  Measured on the settled multi-stage book: P1
# seats before local 11:00 contributed -49.5 USDC net of the P0-only
# counterfactual; seats in the 11:00-16:00 window contributed +99.8 (+38.7
# ex-best, permutation p=0.006).  The daily maximum forms in the early
# afternoon; the stage whose whole edge is intraday observation has nothing
# to say before it.  One definition, imported by the runtime and the paper
# ledger both, so the strategy measured is the strategy run.
DEFAULT_P1_ENTRY_LOCAL_HOUR = 11.0
# Retired hard rails (2026-08-14 ruling).  The 0.45 win-probability gate was
# a portfolio-level target (55% book win rate) misapplied per window: it
# forbade the base+tail mixing the doctrine explicitly wants, contradicted
# clause 37's "no shape-class bans", and its variance-control job was already
# held by fractional Kelly sizing, the scenario floor and the portfolio
# walls.  The 10%-of-cost edge gate was worse than redundant -- proportional
# to cost, it bound hardest on the expensive high-win windows the strategy
# exists to buy (0.90 cost demanded 9 points of edge) while waving through
# penny lotteries (0.05 cost demanded half a cent).  Both now rank the
# shortlist only; the objective's curve score against the no-new-action
# baseline is the sole seat authority, and the win-rate target lives where a
# portfolio average belongs: in the clause-40 dials.
#
# The only hard admission floor left is friction.  All-in prices already
# carry the fee, so what remains is tick-rounding noise: a basket whose whole
# edge is smaller than one tick per leg is quantisation, not signal.
# A window that excludes the model's own centre bucket is over-claimed, and
# by a measurable amount.  Realized containment against claimed mass over
# 5,230 station-days, four-bucket contiguous windows:
#
#   claimed        containing the centre     excluding it
#   0.05 - 0.10          --                  0.78
#   0.10 - 0.20          --                  0.83
#   0.20 - 0.35        2.38 (n=319)          0.85
#   0.35 - 0.55        1.27                  0.82
#   0.55 - 1.00        1.01 (n=16,981)       0.48
#
# Windows holding the centre are calibrated where it matters (1.01 on the
# bulk); windows without it run fifteen to eighteen percent rich across four
# adjacent bands and worse than double at the top.  The mechanism is the
# spread inflation: scaling every member's deviation about the centre by two
# to five is what fixes the tails, and it necessarily moves mass outward
# faster than the truth does.
#
# The correction is applied to the probabilities the objective sees, not as a
# gate.  When it was written, forbidding off-centre windows was judged a
# rail; the Ladder ruling (2026-08-15, charter 49) has since made centre
# containment a structural admission floor for every non-incumbent window,
# so the only off-centre window left on the menu is the evidence-exempt
# incumbent -- and this deflation is what keeps its ranking honest rather
# than letting an inherited seat score on mass the measurement says it does
# not have.
#
# Factors are shrunk 30% toward one rather than served at the point estimate.
OFF_CENTER_CLAIM_BANDS = (
    # (claimed-mass upper bound, realized-over-claimed factor)
    (0.05, 1.15),
    (0.10, 0.85),
    (0.20, 0.88),
    (0.35, 0.90),
    (0.55, 0.87),
    (1.01, 0.64),
)


# How many buckets of distance from the centre buys the full deflation.
#
# The table above was fitted as a two-value question -- does the window
# contain the centre bucket, yes or no -- so the correction it implies is a
# cliff: one bucket of movement in the centre flips the factor between 1.00
# and 0.64.  The quantity underneath ("how far off centre is this window")
# is continuous, and the discretisation is what makes the cliff bite when
# two charters disagree about which bucket the centre is: measured
# 2026-08-27, 86% of the disagreements between the seating centre (charter
# 54's mass-weighted mean) and the claim centre (charter 42's point
# estimate) are exactly one bucket, and 13 of 384 seats were deflated for
# missing a centre they were seated on.
#
# 1 is the fitted rule exactly -- any gap at all takes the whole deflation --
# and is the default so nothing moves without a measurement.  Above 1 the
# deflation ramps: at 2 a one-bucket miss takes half of it, at 3 a third.
# The ramp is an assumption the table cannot settle, because the table has
# no distance axis; that is what the A/B is for.
OFF_CENTER_CLAIM_RAMP_BUCKETS = float(
    os.environ.get("PM_OFF_CENTER_CLAIM_RAMP_BUCKETS", "1") or 1.0
)


def off_center_claim_factor(
    claimed_mass: float, *, gap_buckets: float = 1.0
) -> float:
    """Deflation for a window that does not contain the model centre.

    ``gap_buckets`` is how far the centre sits outside the window, in
    bucket ordinals: 0 when the window contains it.  The default of 1
    reproduces the fitted two-value rule for every caller that does not
    measure a distance.
    """

    mass = float(claimed_mass)
    full = OFF_CENTER_CLAIM_BANDS[-1][1]
    for bound, factor in OFF_CENTER_CLAIM_BANDS:
        if mass < bound:
            full = factor
            break
    gap = max(0.0, float(gap_buckets))
    if gap <= 0.0:
        return 1.0
    ramp = OFF_CENTER_CLAIM_RAMP_BUCKETS
    if not ramp > 1.0:
        return full
    reach = min(1.0, gap / ramp)
    return 1.0 - (1.0 - full) * reach


# Charter 81 (user ruling, 2026-09-05): the mirror of the deflation above --
# a claim multiplier for windows that HOLD the centre.  Re-measured on the
# served (post-band) claims that day (``scripts/measure_off_center_claim.py``,
# P0, 1,287 station-days, mean centre): centre-holding windows realise 1.30
# [1.23,1.37] of their claim in [0.35,0.55) and 1.12 [1.09,1.15] at 0.55 and
# above, so the whole-basket positive-EV gate was systematically too strict
# there.  The shipped table is those ratios shrunk 30% toward one; the ledger
# priced it at +3.11% against the charter-80 baseline's -2.08% (same book,
# production judge, drop-the-winners lead steady at +8 points).  JSON list of
# [claimed-mass upper bound, factor], read like the off-centre table (the
# first bound the claim falls below); ``PM_CENTER_WINDOW_CLAIM_BANDS=off`` (or
# ``[]``) is the research arm that turns it off.  Lives in the kernel because
# three readers must agree on it: the enumeration (gate, ranking, partition,
# charter 68's agreement), the paper ledger's fingerprint, and the charter-68
# lambda store, which fits agreement on the claim the solver actually reads.
CENTER_WINDOW_CLAIM_DEFAULT = "[[0.35, 1.0], [0.55, 1.21], [1.01, 1.08]]"


def _parse_claim_bands(text: str) -> tuple[tuple[float, float], ...]:
    cleaned = (text or "").strip()
    if cleaned.lower() == "off":
        return ()
    raw = json.loads(cleaned or CENTER_WINDOW_CLAIM_DEFAULT)
    bands = tuple((float(bound), float(factor)) for bound, factor in raw)
    if any(bound <= 0.0 or factor <= 0.0 for bound, factor in bands):
        raise RuntimeError(f"invalid centre-window claim bands: {text}")
    if list(bound for bound, _ in bands) != sorted(bound for bound, _ in bands):
        raise RuntimeError(f"centre-window claim bands must ascend: {text}")
    return bands


CENTER_WINDOW_CLAIM_BANDS: tuple[tuple[float, float], ...] = _parse_claim_bands(
    os.environ.get("PM_CENTER_WINDOW_CLAIM_BANDS", "")
)


def center_window_claim_factor(claimed_mass: float) -> float:
    """Charter 81's multiplier for a window holding the centre; 1.0 when off."""

    if not CENTER_WINDOW_CLAIM_BANDS:
        return 1.0
    mass = float(claimed_mass)
    for bound, factor in CENTER_WINDOW_CLAIM_BANDS:
        if mass < bound:
            return float(factor)
    return 1.0


# The ladder's length is a property of the day, not a constant.  A model
# that has made up its mind puts its mass on two or three buckets and a
# wider basket only buys tails it does not believe in; a model that has not
# needs the width or the truth lands outside the seat.  So the seat spans
# the distribution's own one-sigma width, measured in buckets, and the
# policy floor and cap bound it rather than setting it.
#
# Sigma is computed on bucket ordinals rather than degrees on purpose: the
# Fahrenheit books quote 2F buckets and the Celsius books 1C, so a width in
# degrees means a different number of rungs in each, while "one sigma wide"
# means the same thing in both.
def dispersion_support_size(
    probabilities: "Mapping[str, float]",
    *,
    ordinals: "Mapping[str, int]",
    minimum: int,
    maximum: int,
) -> tuple[int, dict[str, Any]]:
    """How many rungs this day's dispersion asks for, within policy bounds."""

    live = {
        bucket: max(0.0, float(value))
        for bucket, value in probabilities.items()
        if bucket in ordinals
    }
    total = sum(live.values())
    floor_size = max(1, int(minimum))
    cap_size = max(floor_size, int(maximum))
    if total <= 0.0:
        return floor_size, {
            "schema": "dispersion_support_size_v1",
            "state": "NO_MASS",
            "served_size": floor_size,
        }
    mean = sum(
        float(ordinals[bucket]) * value for bucket, value in live.items()
    ) / total
    variance = sum(
        value * (float(ordinals[bucket]) - mean) ** 2
        for bucket, value in live.items()
    ) / total
    sigma = math.sqrt(max(0.0, variance))
    # Centre rung plus one sigma of rungs either side, rounded up so a
    # half-bucket of spread still earns its rung.
    wanted = 1 + 2 * math.ceil(sigma - 1e-9)
    served = min(cap_size, max(floor_size, int(wanted)))
    return served, {
        "schema": "dispersion_support_size_v1",
        "state": "MEASURED",
        "sigma_buckets": sigma,
        "requested_size": int(wanted),
        "policy_minimum": floor_size,
        "policy_maximum": cap_size,
        "served_size": served,
    }


PROBABILITY_CORE_MASS = 0.50


def probability_core(
    probabilities: Mapping[str, float],
    *,
    ordinals: Mapping[str, int],
    mass: float = PROBABILITY_CORE_MASS,
    center_bucket: str | None = None,
) -> frozenset[str]:
    """The contiguous run p(t) actually stands behind.

    Grown outward from the stage's own point estimate -- for P0 the bucket
    holding the weight-averaged calibrated system centers -- always taking
    the heavier neighbour, until the run holds ``mass``.

    The seed is the point estimate rather than the probability argmax, and
    the distinction is load-bearing: an open-ended edge bucket ("35C or
    higher") spans an unbounded interval, collects the whole upper tail of
    every member, and is not commensurable with the 1C buckets it is ranked
    against.  Seeding on the argmax inherits exactly the anchor ``_center``
    exists to refuse -- measured 2026-08-15 over 69 live station-days, the
    argmax sat on an edge bucket in 9% of them and the core disagreed with
    the point estimate in 4%, including ZGGG 2026-08-15 (centre 30C, core
    {33C, 34C}): a gate meant to keep the seat on trustworthy mass was
    instead forbidding every window that held the model's own best guess.
    An edge bucket is still a legitimate seed when the point estimate
    genuinely falls inside it.  The argmax remains the fallback for a caller
    with no anchor to offer.

    This exists because p(t) is not uniformly trustworthy across its own
    support, and the divergence is sharp.  Measured 2026-08-15 over 2,398
    bucket-days (218 station-days, 16 days of settled truth), realized over
    claimed by claim size: buckets claiming 0.12-0.18 realize 1.04, 0.18-0.30
    realize 1.09, above 0.30 realize 1.02 -- calibrated.  Below that the
    shoulder over-claims: 0.08-0.12 realizes 0.77, 0.05-0.08 realizes 0.83,
    0.02-0.05 realizes 0.80.  A window assembled entirely out of shoulder
    buckets is assembled entirely out of mass we do not have, and its edge is
    an artifact of the over-claim rather than a disagreement with the market.

    Deflating the shoulder instead would be the direct fix, and it is not
    available: the 0.08-0.12 band's day-bootstrap interval is [0.54, 1.04],
    which touches 1.0 and so fails the two-hurdle admission rule that governs
    every other correction here.  The structural rule is what the evidence
    can carry.

    Overlap with this run, rather than a floor on the per-bucket claim, is
    the test because bucket width is not constant across markets: the
    Fahrenheit books quote 2F buckets, so 0.12 of mass means a different
    physical interval there than in a Celsius book, while "touches the run
    p(t) stands behind" means the same thing in both.
    """

    ordered = sorted(
        (bucket for bucket in probabilities if bucket in ordinals),
        key=lambda bucket: (ordinals[bucket], bucket),
    )
    if not ordered:
        return frozenset()
    values = [max(0.0, float(probabilities[bucket])) for bucket in ordered]
    seed = (
        ordered.index(center_bucket)
        if center_bucket is not None and center_bucket in ordered
        else max(range(len(ordered)), key=lambda index: values[index])
    )
    low = high = seed
    held = values[seed]
    target = min(max(float(mass), 0.0), 1.0)
    while held < target and (low > 0 or high < len(ordered) - 1):
        left = values[low - 1] if low > 0 else -1.0
        right = values[high + 1] if high < len(ordered) - 1 else -1.0
        if right >= left:
            high += 1
            held += right
        else:
            low -= 1
            held += left
    return frozenset(ordered[low:high + 1])


# Zero since 2026-08-25 (charter 58): the window price gate is whole-basket
# positive EV and nothing else -- probability sum strictly above the all-in
# price sum.  The one-tick-per-leg buffer this used to carry was a
# quantisation-noise floor; the ruling removes it so the gate states the
# criterion it always approximated, and an unpriceable window is simply the
# trivial failure of the same test rather than a rule of its own.  The env
# override remains for the research arm that wants the buffer back.
SUPPORT_EDGE_FRICTION_TICKS_PER_LEG = float(
    os.environ.get("PM_SUPPORT_EDGE_FRICTION_TICKS_PER_LEG", "") or 0.0
)
# ``candidate_kind`` is a load-bearing activation signal, not a label: the
# state machine reads it to decide TRADE versus HOLD, and this kernel reads it
# to know which candidate is the "spend nothing" option the floor exempts.
# Both live here because the legality rule above depends on the vocabulary.
CANDIDATE_KIND_COMPLETE = "COMPLETE_BASKET_TARGET"
CANDIDATE_KIND_BASELINE = "NO_NEW_ACTION_BASELINE"
CANDIDATE_KINDS = frozenset({
    CANDIDATE_KIND_COMPLETE, CANDIDATE_KIND_BASELINE,
})


def curve_score(
    audit: Mapping[str, Any],
    *,
    bankroll_usdc: float,
    kelly_fraction: float = DEFAULT_KELLY_FRACTION,
) -> float:
    """Score the settled-outcome PnL curve by fractional log wealth.

        sum_o  p_o * log(1 + f * s_o / W)

    Concave in the payoff, which is what charges a basket for spread: two
    baskets of equal mean rank by how evenly their outcomes pay, so the
    ranking prefers a flat curve over a spike and the lottery shape loses.

    The plain quadratic charge ``E - Var/(2W)`` expresses the same
    preference and was tried first, but it is only the local expansion of
    this and breaks once a payoff is large next to the bankroll: on a
    50/50 paying +24.5 or -0.5 against a 5 USDC book it scores -1.48 and
    refuses, because a squared term grows without bound and ends up pricing
    *winning too much* as risk.  The log saturates instead, so a bet that
    cannot hurt the book is never refused for being too good.

    ``f`` is the variance dial and the model-error hedge.  Betting a
    fraction of the log-optimal stake is equivalent to betting the full
    stake against a larger book, so it enters here as an effective bankroll
    ``W/f`` -- one number, and the same one whether it sizes a basket or
    ranks two.
    """

    bankroll = float(bankroll_usdc)
    fraction = float(kelly_fraction)
    if not (bankroll > 0.0) or not math.isfinite(bankroll):
        raise ContractError("delta_v2_curve_bankroll_invalid")
    if not (0.0 < fraction <= 1.0):
        raise ContractError("delta_v2_curve_kelly_fraction_invalid")
    effective = bankroll / fraction
    total = 0.0
    for bucket, pnl in audit["scenario_pnl"].items():
        probability = float(audit["probabilities"][bucket])
        if probability <= 0.0:
            continue
        wealth = 1.0 + float(pnl) / effective
        if wealth <= EPS:
            # Ruin under a settled outcome the model gives real mass: no
            # finite score, and nothing to trade off against.
            return -math.inf
        total += probability * math.log(wealth)
    return total


def _win_probability(audit: Mapping[str, Any]) -> float:
    return sum(
        float(audit["probabilities"][bucket])
        for bucket, pnl in audit["scenario_pnl"].items()
        if float(pnl) > EPS
    )


def _odds_ratio(audit: Mapping[str, Any]) -> float | None:
    """Probability-weighted mean win over mean loss; None when one-sided."""

    win_mass = loss_mass = win_sum = loss_sum = 0.0
    for bucket, pnl in audit["scenario_pnl"].items():
        probability = float(audit["probabilities"][bucket])
        value = float(pnl)
        if value > EPS:
            win_mass += probability
            win_sum += probability * value
        elif value < -EPS:
            loss_mass += probability
            loss_sum += probability * (-value)
    if win_mass <= EPS or loss_mass <= EPS:
        return None
    return (win_sum / win_mass) / (loss_sum / loss_mass)


def clamp_declining_gaps_v2(
    *,
    gap_by_bucket: Mapping[str, Any],
    support: Sequence[str],
    probabilities: Mapping[str, Any],
    previous_probabilities: Mapping[str, Any] | None,
    minimum_drop: float = 0.0,
) -> tuple[dict[str, float], tuple[str, ...]]:
    """Hold, do not add to, a bucket whose probability just fell.

    Returns the clamped gaps and the buckets that were clamped.

    Adding to a bucket the evidence has just moved away from is buying into
    a fading claim: the objective re-solves from scratch each cycle and
    would happily top up a leg whose probability fell, because the solve
    sees today's number and not its direction.

    One-sided on purpose.  ``min(0, gap)`` only ever lowers, so a faded leg
    is held at the size it already has; selling is a separate matter and
    stays forbidden.  A bucket with no previous reading is not falling --
    it is unmeasured -- and is left alone.

    ``minimum_drop`` is how far a probability must fall before this counts
    as falling.  It defaults to 0, which is the rule as shipped: the
    comparison is then ``now + EPS < before``, so a drop of 1e-9 clamps a
    leg exactly as a drop of 0.5 does.  That default has never been
    measured against any other value.

    Two facts make the question live.  P0 probabilities follow the model
    run, not the clock: measured over 138,116 adjacent P0 checkpoints from
    2026-08-15, only 6.5% differ at all -- so this rule fires only in the
    moment a run lands, and whether a station-day is clamped depends on
    where its first seatable cycle falls relative to that moment.  And of
    the drops that do occur, 18% are smaller than 0.001, which is noise
    against a median drop of 0.0167.

    Lives in the kernel because it is a selection rule, and selection rules
    are one rule on both sides (standing ruling 2026-08-22).  It was inline
    in ``delta_v2_runtime`` until 2026-09-01 and the paper ledger therefore
    never ran it at all: the ledger's whole recorded history is the arm
    where this clamp is off, which is not something either side declared.
    The binding test could not catch that -- it classifies the *ledger's*
    constants, and a rule that exists only in production has no constant
    there to classify.
    """

    clamped = {
        str(bucket): float(gap_by_bucket[bucket]) for bucket in support
    }
    if not previous_probabilities:
        return clamped, ()
    # EPS is the floor, not the setting: a threshold below it would make
    # float noise a signal.
    threshold = max(EPS, float(minimum_drop))
    declining: list[str] = []
    for bucket in support:
        before = previous_probabilities.get(bucket)
        if before is None:
            continue
        now = float(probabilities.get(bucket, 0.0))
        if float(before) - now > threshold:
            declining.append(str(bucket))
            clamped[str(bucket)] = min(0.0, clamped[str(bucket)])
    return clamped, tuple(declining)


def contiguous_edge_clamp_v2(
    *,
    declining: Sequence[str],
    support: Sequence[str],
    centre_bucket: str | None,
) -> tuple[str, ...]:
    """Narrow a clamp to what charter 49 and contiguity would allow.

    Returns the subset of ``declining`` that may actually be dropped: only
    buckets at the ends of the window, and never the centre rung.

    The plain clamp is free of both rules because it runs *before* the
    complete-basket check, which then judges what survives -- so a clamp
    that removes the centre rung, or leaves a hole in the middle, produces
    a basket that passes as complete.  Measured 2026-09-01 over 581 live
    replay openings: 48 dropped the centre and 10 left the support
    non-contiguous.  Neither is a violation of the rules as written; both
    are outside the path that enforces them.

    Ends-only is what contiguity means here.  Walking in from each side and
    stopping at the first bucket that is not declining leaves the remainder
    a single run by construction; dropping from the middle could not.
    """

    order = [str(bucket) for bucket in support]
    falling = {str(bucket) for bucket in declining}
    centre = str(centre_bucket) if centre_bucket else ""
    allowed: list[str] = []
    left = 0
    while left < len(order) and order[left] in falling:
        if order[left] == centre:
            break
        allowed.append(order[left])
        left += 1
    right = len(order) - 1
    while right >= left and order[right] in falling:
        if order[right] == centre:
            break
        allowed.append(order[right])
        right -= 1
    return tuple(bucket for bucket in order if bucket in set(allowed))


def solve_target_inventory_v2(
    *,
    probabilities: Mapping[str, Any],
    feasible_inventory_candidates: Sequence[Mapping[str, Any]],
    protected_buckets: Sequence[str],
    shortfall_buckets: Sequence[str],
    breakthrough_buckets: Sequence[str],
    weights: ValueWeightsV2,
    maximum_candidates: int = 4096,
    objective_form: str = VALUE_OBJECTIVE_FORM,
    bankroll_usdc: float | None = None,
    kelly_fraction: float = DEFAULT_KELLY_FRACTION,
) -> dict[str, Any]:
    """Return the exact argmax over a caller-provided bounded feasible set.

    ``objective_form`` selects what the argmax reads.  ``value_v2`` is the
    original weighted value function.  ``mean_variance_curve_v1`` ranks by
    the curve score above; every value-function term is still computed and
    audited, it simply no longer decides.  The feasible set is untouched by
    either -- protected buckets, contiguity, budget, depth and clip bounds
    all bind during candidate generation, so changing the ranking cannot
    admit anything the constraints refuse.
    """

    form = str(objective_form or "").strip()
    if form not in OBJECTIVE_FORMS:
        raise ContractError("delta_v2_objective_form_invalid")
    if form in (CURVE_OBJECTIVE_FORM, KELLY_OBJECTIVE_FORM) and not (
        bankroll_usdc is not None and float(bankroll_usdc) > 0.0
    ):
        raise ContractError("delta_v2_curve_bankroll_required")
    if (
        not isinstance(feasible_inventory_candidates, Sequence)
        or isinstance(feasible_inventory_candidates, (str, bytes, bytearray))
        or not feasible_inventory_candidates
    ):
        raise ContractError("delta_v2_target_candidate_set_empty")
    if maximum_candidates <= 0 or maximum_candidates > 65_536:
        raise ContractError("delta_v2_target_candidate_bound_invalid")
    if len(feasible_inventory_candidates) > maximum_candidates:
        raise ContractError("delta_v2_target_candidate_bound_exceeded")
    evaluations: list[dict[str, Any]] = []
    for index, raw in enumerate(feasible_inventory_candidates):
        if not isinstance(raw, Mapping):
            raise ContractError("delta_v2_target_candidate_mapping_required")
        audit = evaluate_basket_value_v2(
            probabilities=probabilities,
            shares_by_bucket=raw.get("shares_by_bucket") or {},
            cost_by_bucket=raw.get("cost_by_bucket") or {},
            protected_buckets=protected_buckets,
            shortfall_buckets=shortfall_buckets,
            breakthrough_buckets=breakthrough_buckets,
            weights=weights,
        )
        evaluations.append({
            "candidate_index": index,
            "candidate_id": str(raw.get("candidate_id") or f"candidate-{index}"),
            "candidate_kind": str(
                raw.get("candidate_kind") or "UNSPECIFIED"
            ),
            "candidate_origin": str(raw.get("candidate_origin") or ""),
            "audit": audit,
            "curve_score": (
                curve_score(
                    audit,
                    bankroll_usdc=float(bankroll_usdc),
                    kelly_fraction=kelly_fraction,
                )
                if form in (CURVE_OBJECTIVE_FORM, KELLY_OBJECTIVE_FORM)
                else None
            ),
        })
    # Legality before preference: a candidate whose worst in-support
    # settlement loses more than the floor fraction of its premium is not a
    # basket, whatever its score.  ``profit_floor`` is the minimum scenario
    # pnl over ``protected_buckets`` -- the support -- and the zero-cost
    # baseline has floor 0 against cost 0, so at least one candidate is
    # always legal and a book offering only lottery shapes resolves to HOLD.
    def _floor_legal(row: Mapping[str, Any]) -> bool:
        # Buying nothing is always available and therefore always legal.  The
        # baseline carries whatever the station already holds, and holdings
        # cannot be un-bought: judging inherited shape by this floor condemns
        # the one answer that is always correct -- hold, add nothing -- and
        # turns it into a hard compile error.  Every position opened before
        # this floor existed was built by the unconstrained objective, so
        # without this exemption the rule would have blocked exactly the
        # stations it was written to protect.  The floor governs what a
        # decision may *add*; a candidate that spends must meet it, and if
        # none can the winner falls back to the baseline as a HOLD.
        if str(row.get("candidate_kind") or "") == CANDIDATE_KIND_BASELINE:
            return True
        cost = sum(row["audit"]["cost_by_bucket"].values())
        return float(row["audit"]["profit_floor"]) >= (
            SUPPORT_SCENARIO_FLOOR_FRACTION * cost - EPS
        )

    legal = [row for row in evaluations if _floor_legal(row)]
    if not legal:
        raise ContractError("delta_v2_no_floor_legal_candidate")
    if form == KELLY_OBJECTIVE_FORM:
        # Charter 72: the Kelly-sized candidate is the answer when it is
        # legal; when the floor refuses it, or none was offered (no
        # edge, no book, a leg already at its target), the answer is the
        # baseline -- hold.  The curve ranks nothing here.
        kelly_rows = [
            row for row in legal
            if row["candidate_origin"] == KELLY_CANDIDATE_ORIGIN
        ]
        ticket_rows = [
            row for row in legal
            if row["candidate_origin"] == CHAIN_TICKET_CANDIDATE_ORIGIN
        ]
        if kelly_rows:
            winner = max(
                kelly_rows,
                key=lambda row: (
                    -sum(row["audit"]["cost_by_bucket"].values()),
                    row["candidate_id"],
                ),
            )
        elif ticket_rows:
            # Charter 74: no edge at P0 still opens the chain, minimally.
            winner = min(
                ticket_rows,
                key=lambda row: (
                    sum(row["audit"]["cost_by_bucket"].values()),
                    row["candidate_id"],
                ),
            )
        else:
            baseline_rows = [
                row for row in legal
                if row["candidate_kind"] == CANDIDATE_KIND_BASELINE
            ]
            if not baseline_rows:
                raise ContractError("delta_v2_no_floor_legal_candidate")
            winner = baseline_rows[0]
    else:
        winner = max(
            legal,
            key=lambda row: (
                float(
                    row["curve_score"]
                    if form == CURVE_OBJECTIVE_FORM
                    else row["audit"]["value"]
                ),
                -sum(row["audit"]["cost_by_bucket"].values()),
                row["candidate_id"],
            ),
        )
    identity = {
        "schema": TARGET_SOLUTION_SCHEMA,
        "kernel_version": KERNEL_VERSION,
        "objective_form": form,
        "kelly_fraction": (
            kelly_fraction
            if form in (CURVE_OBJECTIVE_FORM, KELLY_OBJECTIVE_FORM)
            else None
        ),
        "winner_candidate_origin": winner["candidate_origin"],
        "candidate_count": len(evaluations),
        "winner_candidate_id": winner["candidate_id"],
        "winner_candidate_index": winner["candidate_index"],
        "winner_candidate_kind": winner["candidate_kind"],
        "target_shares_by_bucket": winner["audit"]["shares_by_bucket"],
        "target_cost_by_bucket": winner["audit"]["cost_by_bucket"],
        "target_value": winner["audit"]["value"],
        "target_expected_pnl": winner["audit"]["expected_pnl"],
        "target_variance": winner["audit"]["variance"],
        "target_curve_score": winner["curve_score"],
        "target_scenario_pnl": winner["audit"]["scenario_pnl"],
        "support_scenario_floor_fraction": SUPPORT_SCENARIO_FLOOR_FRACTION,
        "floor_rejected_candidate_count": len(evaluations) - len(legal),
        "target_support_floor_pnl": winner["audit"]["profit_floor"],
        # The two dials the strategy is steered by -- P(pnl>0) under our own
        # probabilities, and mean win over mean loss.  At a 55% win rate the
        # break-even odds are 0.45/0.55 = 0.82; the design floor is 1.0 so a
        # five-point calibration slip still cannot turn the book negative.
        "target_win_probability": _win_probability(winner["audit"]),
        "target_odds_ratio": _odds_ratio(winner["audit"]),
        "target_value_sha256": winner["audit"]["content_sha256"],
        "candidate_value_sha256s": [
            row["audit"]["content_sha256"] for row in evaluations
        ],
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _fill_distribution(
    raw: Any,
    *,
    mode: str,
) -> tuple[dict[str, float], ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        raise ContractError("delta_v2_fill_distribution_invalid")
    outcomes: list[dict[str, float]] = []
    for row in raw:
        if not isinstance(row, Mapping):
            raise ContractError("delta_v2_fill_outcome_mapping_required")
        outcomes.append({
            "fill_fraction": _probability(
                "delta_v2_fill_fraction", row.get("fill_fraction")
            ),
            "probability": _probability(
                "delta_v2_fill_probability", row.get("probability")
            ),
        })
    if not outcomes or abs(sum(row["probability"] for row in outcomes) - 1.0) > 1e-8:
        raise ContractError("delta_v2_fill_distribution_mass_invalid")
    fractions = [row["fill_fraction"] for row in outcomes if row["probability"] > EPS]
    if mode == "MAKER":
        if not any(EPS < value < 1.0 - EPS for value in fractions):
            raise ContractError("delta_v2_maker_partial_fill_support_required")
        if not any(value <= EPS for value in fractions):
            raise ContractError("delta_v2_maker_zero_fill_support_required")
    if mode == "FOK" and any(
        value < 1.0 - EPS for value in fractions
    ):
        raise ContractError("delta_v2_fok_distribution_must_be_full_fill")
    return tuple(sorted(outcomes, key=lambda row: row["fill_fraction"]))


def _hard_gate_state(raw: Any) -> tuple[dict[str, bool], list[str]]:
    if not isinstance(raw, Mapping):
        raise ContractError("delta_v2_hard_gates_mapping_required")
    unknown = set(raw) - set(REQUIRED_HARD_GATES)
    missing = set(REQUIRED_HARD_GATES) - set(raw)
    if unknown or missing:
        raise ContractError("delta_v2_hard_gate_set_mismatch")
    gates = {name: raw[name] is True for name in REQUIRED_HARD_GATES}
    return gates, [name for name in REQUIRED_HARD_GATES if not gates[name]]


def evaluate_action_v2(
    *,
    event_id: str,
    bucket: str,
    cash_clip: Any,
    limit_price: Any,
    timeout_seconds: Any,
    order_mode: str,
    fill_distribution: Sequence[Mapping[str, Any]],
    probabilities: Mapping[str, Any],
    current_shares_by_bucket: Mapping[str, Any],
    current_cost_by_bucket: Mapping[str, Any],
    target_shares_by_bucket: Mapping[str, Any],
    protected_buckets: Sequence[str],
    shortfall_buckets: Sequence[str],
    breakthrough_buckets: Sequence[str],
    value_weights: ValueWeightsV2,
    execution_weights: ExecutionWeightsV2,
    hard_gates: Mapping[str, Any],
    capital_available_usdc: Any,
    minimum_profit_floor: Any,
    reservation_price: Any,
    wait_cost: Any,
    queue_risk: Any,
    adverse_selection: Any,
    reprice_risk: Any,
    capital_lock_cost: Any,
    confirmed_pending_shares_by_bucket: Mapping[str, Any] | None = None,
    confirmed_pending_cost_by_bucket: Mapping[str, Any] | None = None,
    fill_artifact_content_sha256: str | None = None,
    fee_rate: Any = 0.0,
) -> dict[str, Any]:
    """Evaluate one complete event×bucket×clip×price×timeout×mode action."""

    event = _text("delta_v2_event_id", event_id)
    selected_bucket = _text("delta_v2_action_bucket", bucket)
    clip = _positive("delta_v2_cash_clip", cash_clip)
    price = _positive("delta_v2_limit_price", limit_price)
    if price >= 1.0:
        raise ContractError("delta_v2_limit_price_not_below_one")
    timeout = _nonnegative("delta_v2_timeout_seconds", timeout_seconds)
    mode = _text("delta_v2_order_mode", order_mode).upper()
    if mode not in {"MAKER", "FOK"}:
        raise ContractError("delta_v2_order_mode_invalid")
    if mode == "MAKER" and timeout <= EPS:
        raise ContractError("delta_v2_maker_timeout_nonpositive")
    if mode == "FOK" and timeout > EPS:
        raise ContractError("delta_v2_fok_timeout_must_be_zero")
    fee = _nonnegative("delta_v2_fee_rate", fee_rate)
    if fee >= 1.0:
        raise ContractError("delta_v2_fee_rate_invalid")
    distribution = _fill_distribution(fill_distribution, mode=mode)
    gates, blockers = _hard_gate_state(hard_gates)
    capital_available = _nonnegative(
        "delta_v2_capital_available", capital_available_usdc
    )
    required_profit_floor = _finite(
        "delta_v2_minimum_profit_floor", minimum_profit_floor
    )
    reserve = _positive("delta_v2_reservation_price", reservation_price)
    if reserve >= 1.0:
        raise ContractError("delta_v2_reservation_price_not_below_one")
    if clip > capital_available + EPS:
        gates["budget_ok"] = False
        blockers.append("budget_ok")
    if price > reserve + EPS:
        gates["reservation_price_ok"] = False
        blockers.append("reservation_price_ok")
    buckets = _canonical_buckets(probabilities)
    if selected_bucket not in buckets:
        raise ContractError("delta_v2_action_bucket_unknown")
    current_shares = _canonical_map(
        "delta_v2_current_shares", current_shares_by_bucket, buckets
    )
    current_cost = _canonical_map(
        "delta_v2_current_cost", current_cost_by_bucket, buckets
    )
    pending_shares = _canonical_map(
        "delta_v2_confirmed_pending_shares",
        confirmed_pending_shares_by_bucket or {},
        buckets,
    )
    pending_cost = _canonical_map(
        "delta_v2_confirmed_pending_cost",
        confirmed_pending_cost_by_bucket or {},
        buckets,
    )
    target = _canonical_map(
        "delta_v2_target_shares", target_shares_by_bucket, buckets
    )
    # Confirmed pending inventory reduces the positive gap and reserves
    # capital, but is deliberately excluded from realised V(q), profit floor
    # and scenario PnL until an authoritative fill arrives.
    inventory_gap = (
        target[selected_bucket]
        - current_shares[selected_bucket]
        - pending_shares[selected_bucket]
    )
    if inventory_gap <= EPS and "positive_inventory_gap" not in blockers:
        blockers.append("positive_inventory_gap")
        gates["positive_inventory_gap"] = False
    maximum_shares = clip / price
    if maximum_shares > inventory_gap + EPS:
        blockers.append("clip_exceeds_inventory_gap")
    baseline = evaluate_basket_value_v2(
        probabilities=probabilities,
        shares_by_bucket=current_shares,
        cost_by_bucket=current_cost,
        protected_buckets=protected_buckets,
        shortfall_buckets=shortfall_buckets,
        breakthrough_buckets=breakthrough_buckets,
        weights=value_weights,
        target_shares_by_bucket=target,
    )
    outcome_rows: list[dict[str, Any]] = []
    expected_delta_value = 0.0
    for outcome in distribution:
        filled_shares = maximum_shares * outcome["fill_fraction"]
        resulting_shares = dict(current_shares)
        resulting_cost = dict(current_cost)
        resulting_shares[selected_bucket] += filled_shares
        resulting_cost[selected_bucket] += filled_shares * price * (1.0 + fee)
        value = evaluate_basket_value_v2(
            probabilities=probabilities,
            shares_by_bucket=resulting_shares,
            cost_by_bucket=resulting_cost,
            protected_buckets=protected_buckets,
            shortfall_buckets=shortfall_buckets,
            breakthrough_buckets=breakthrough_buckets,
            weights=value_weights,
            target_shares_by_bucket=target,
        )
        delta = float(value["value"]) - float(baseline["value"])
        expected_delta_value += outcome["probability"] * delta
        outcome_rows.append({
            **outcome,
            "filled_shares": filled_shares,
            "filled_notional_usdc": filled_shares * price,
            "delta_value": delta,
            "resulting_value_sha256": value["content_sha256"],
            "resulting_profit_floor": value["profit_floor"],
        })
    if any(
        row["resulting_profit_floor"] + EPS < required_profit_floor
        for row in outcome_rows
    ):
        gates["profit_floor_ok"] = False
        blockers.append("profit_floor_ok")
    execution_costs = {
        "wait": _nonnegative("delta_v2_wait_cost", wait_cost),
        "queue": _nonnegative("delta_v2_queue_risk", queue_risk),
        "adverse_selection": _nonnegative(
            "delta_v2_adverse_selection", adverse_selection
        ),
        "reprice": _nonnegative("delta_v2_reprice_risk", reprice_risk),
        "capital_lock": _nonnegative(
            "delta_v2_capital_lock_cost", capital_lock_cost
        ),
    }
    normalized_execution_cost = (
        execution_weights.lambda_wait
        * execution_costs["wait"]
        / execution_weights.wait_scale
        + execution_weights.lambda_queue
        * execution_costs["queue"]
        / execution_weights.queue_scale
        + execution_weights.lambda_adverse_selection
        * execution_costs["adverse_selection"]
        / execution_weights.adverse_selection_scale
        + execution_weights.lambda_reprice
        * execution_costs["reprice"]
        / execution_weights.reprice_scale
        + execution_weights.lambda_capital_lock
        * execution_costs["capital_lock"]
        / execution_weights.capital_lock_scale
    )
    score = expected_delta_value - normalized_execution_cost
    post_expected_shares = current_shares[selected_bucket] + sum(
        row["probability"] * row["filled_shares"] for row in outcome_rows
    ) + pending_shares[selected_bucket]
    inventory_fit_distance = (
        max(0.0, target[selected_bucket] - post_expected_shares) ** 2
    )
    authorized = (
        not blockers and score > execution_weights.epsilon_score + EPS
    )
    state = (
        "RECONCILE"
        if "order_state_known" in blockers
        else ("TRADE" if authorized else "HOLD")
    )
    identity = {
        "schema": ACTION_EVALUATION_SCHEMA,
        "kernel_version": KERNEL_VERSION,
        "action": {
            "schema": ACTION_SCHEMA,
            "event_id": event,
            "bucket": selected_bucket,
            "cash_clip": clip,
            "limit_price": price,
            "timeout_seconds": timeout,
            "order_mode": mode,
            "maximum_shares": maximum_shares,
        },
        "baseline_value_sha256": baseline["content_sha256"],
        "target_shares_by_bucket": target,
        "confirmed_pending_shares_by_bucket": pending_shares,
        "confirmed_pending_cost_by_bucket": pending_cost,
        "confirmed_pending_in_realised_value": False,
        "value_weights_sha256": value_weights.content_sha256,
        "execution_weights_sha256": execution_weights.content_sha256,
        "fill_distribution": list(distribution),
        "fill_artifact_content_sha256": (
            _sha(
                "delta_v2_fill_artifact_content",
                fill_artifact_content_sha256,
            )
            if fill_artifact_content_sha256 is not None
            else None
        ),
        "fill_outcomes": outcome_rows,
        "expected_delta_value": expected_delta_value,
        "execution_costs": execution_costs,
        "normalized_execution_cost": normalized_execution_cost,
        "delta_basket_score_v2": score,
        "inventory_fit_distance": inventory_fit_distance,
        "hard_gates": gates,
        "blockers": sorted(set(blockers)),
        "state": state,
        "authorized": authorized,
        "capital_reserved_usdc": clip,
        "capital_available_usdc": capital_available,
        "minimum_profit_floor": required_profit_floor,
        "reservation_price": reserve,
        "calls_order_gateway": False,
        "places_orders": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def hold_action_v2(*, reason: str = "HOLD_BASELINE") -> dict[str, Any]:
    identity = {
        "schema": ACTION_EVALUATION_SCHEMA,
        "kernel_version": KERNEL_VERSION,
        "action": {"schema": ACTION_SCHEMA, "order_mode": "HOLD"},
        "delta_basket_score_v2": 0.0,
        "inventory_fit_distance": 1e300,
        "state": "HOLD",
        "authorized": False,
        "blockers": [str(reason)],
        "capital_reserved_usdc": 0.0,
        "calls_order_gateway": False,
        "places_orders": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def select_action_v2(
    actions: Sequence[Mapping[str, Any]],
    *,
    execution_weights: ExecutionWeightsV2,
    hold_reason: str = "HOLD_BASELINE",
) -> dict[str, Any]:
    """Select the best authorized action against explicit ``HOLD=0``."""

    if len(actions) > execution_weights.maximum_candidates:
        raise ContractError("delta_v2_action_candidate_bound_exceeded")
    hold = hold_action_v2(reason=hold_reason)
    candidates = [hold]
    for raw in actions:
        if not isinstance(raw, Mapping):
            raise ContractError("delta_v2_action_evaluation_mapping_required")
        if raw.get("schema") != ACTION_EVALUATION_SCHEMA:
            raise ContractError("delta_v2_action_evaluation_schema_invalid")
        candidates.append(dict(raw))
    winner = max(
        candidates,
        key=lambda row: (
            float(row["delta_basket_score_v2"])
            if row.get("authorized") is True
            else 0.0,
            -float(row.get("inventory_fit_distance", math.inf)),
            str(row.get("content_sha256") or ""),
        ),
    )
    if (
        winner.get("authorized") is not True
        or float(winner["delta_basket_score_v2"])
        <= execution_weights.epsilon_score + EPS
    ):
        winner = hold
    identity = {
        "schema": ACTION_SELECTION_SCHEMA,
        "kernel_version": KERNEL_VERSION,
        "candidate_count": len(actions),
        "hold_score": 0.0,
        "selected_state": winner["state"],
        "selected_action_sha256": winner["content_sha256"],
        "selected_score": winner["delta_basket_score_v2"],
        "authorized": winner.get("authorized") is True,
        "calls_order_gateway": False,
        "places_orders": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }
    return {
        **identity,
        "selected_action": winner,
        "content_sha256": content_sha256(identity),
    }


def _bounded_maker_bundle_candidates(
    by_bucket: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    required: Sequence[str],
    probabilities: Mapping[str, Any],
    execution_weights: ExecutionWeightsV2,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    """Retain a complete Maker candidate set within a fixed outcome budget.

    The old selector counted every joint-fill row globally but compared it to
    a three-dimensional bound.  A valid basket may require more than three
    buckets, so ordinary five-bucket candidate grids could fail part-way
    through evaluation.  Calculate the exact Cartesian row count first,
    retain one valid candidate per required bucket, then add alternatives only
    while the fixed resource limit remains satisfied.  If one candidate per
    bucket cannot fit, no complete basket can be evaluated safely and the
    caller remains fail-closed.
    """

    outcome_row_limit = min(
        MAX_BUNDLE_JOINT_OUTCOME_ROWS,
        execution_weights.maximum_candidates ** 3,
    )
    if outcome_row_limit <= 0:
        raise ContractError("delta_v2_bundle_outcome_bound_exceeded")

    def outcome_count(row: Mapping[str, Any]) -> int:
        action = row.get("action")
        if not isinstance(action, Mapping):
            raise ContractError("delta_v2_action_mapping_required")
        return len(_fill_distribution(
            row.get("fill_distribution"),
            mode=str(action.get("order_mode") or "").upper(),
        ))

    def candidate_key(row: Mapping[str, Any]) -> tuple[float, float, str]:
        action = row.get("action")
        if not isinstance(action, Mapping):
            raise ContractError("delta_v2_action_mapping_required")
        # When the resource budget forces a choice, retain the smaller-capital
        # alternative first.  This changes no hard gate and cannot authorize a
        # trade; it only keeps the lower-reservation complete basket available.
        return (
            _nonnegative(
                "delta_v2_bundle_candidate_reserved",
                row.get("capital_reserved_usdc"),
            ),
            _nonnegative(
                "delta_v2_bundle_candidate_maximum_shares",
                action.get("maximum_shares"),
            ),
            str(row.get("content_sha256") or ""),
        )

    normalized: dict[str, list[dict[str, Any]]] = {}
    selected: dict[str, list[dict[str, Any]]] = {}
    requested_outcomes_by_bucket: dict[str, int] = {}
    selected_outcomes_by_bucket: dict[str, int] = {}
    for bucket in required:
        rows = [dict(row) for row in by_bucket[bucket]]
        rows.sort(key=candidate_key)
        # Validate even alternatives that will not be retained.  Malformed
        # inputs remain invalid rather than silently disappearing in pruning.
        sizes = [outcome_count(row) for row in rows]
        normalized[bucket] = rows
        requested_outcomes_by_bucket[bucket] = sum(sizes)
        selected[bucket] = [rows[0]]
        selected_outcomes_by_bucket[bucket] = sizes[0]

    requested_joint_outcome_count = math.prod(
        requested_outcomes_by_bucket.values()
    )
    selected_joint_outcome_count = math.prod(
        selected_outcomes_by_bucket.values()
    )
    if selected_joint_outcome_count > outcome_row_limit:
        raise ContractError("delta_v2_bundle_outcome_bound_exceeded")

    next_indexes = {bucket: 1 for bucket in required}
    while True:
        additions: list[tuple[float, float, str, str, int]] = []
        for bucket in required:
            index = next_indexes[bucket]
            rows = normalized[bucket]
            if index >= len(rows):
                continue
            candidate = rows[index]
            candidate_outcomes = outcome_count(candidate)
            current_outcomes = selected_outcomes_by_bucket[bucket]
            projected = (
                selected_joint_outcome_count
                // current_outcomes
                * (current_outcomes + candidate_outcomes)
            )
            if projected > outcome_row_limit:
                continue
            action = candidate["action"]
            expected_fill = sum(
                float(row["probability"]) * float(row["fill_fraction"])
                for row in _fill_distribution(
                    candidate.get("fill_distribution"),
                    mode=str(action.get("order_mode") or "").upper(),
                )
            ) * float(action["maximum_shares"])
            # The allocation is deterministic and only constructs a bounded
            # feasible set.  The original joint score and hard gates still
            # decide whether any retained basket can be authorized.
            additions.append((
                -_probability(
                    "delta_v2_bundle_candidate_probability",
                    probabilities[bucket],
                ),
                -expected_fill,
                bucket,
                str(candidate.get("content_sha256") or ""),
                candidate_outcomes,
            ))
        if not additions:
            break
        _probability_rank, _fill_rank, bucket, _sha, candidate_outcomes = min(
            additions
        )
        candidate = normalized[bucket][next_indexes[bucket]]
        selected[bucket].append(candidate)
        selected_outcomes_by_bucket[bucket] += candidate_outcomes
        selected_joint_outcome_count = math.prod(
            selected_outcomes_by_bucket.values()
        )
        next_indexes[bucket] += 1

    retained_hashes = {
        str(row.get("content_sha256") or "")
        for rows in selected.values()
        for row in rows
    }
    return selected, {
        "outcome_row_limit": outcome_row_limit,
        "requested_joint_outcome_count": requested_joint_outcome_count,
        "evaluated_joint_outcome_count": selected_joint_outcome_count,
        "requested_candidate_action_count": sum(
            len(rows) for rows in normalized.values()
        ),
        "evaluated_candidate_action_count": sum(
            len(rows) for rows in selected.values()
        ),
        "pruned_action_sha256s": sorted(
            str(row.get("content_sha256") or "")
            for rows in normalized.values()
            for row in rows
            if str(row.get("content_sha256") or "") not in retained_hashes
        ),
    }


def _evaluate_maker_bundle_combo(
    combo: Sequence[Mapping[str, Any]],
    *,
    required: Sequence[str],
    probabilities: Mapping[str, Any],
    current_shares: Mapping[str, float],
    current_cost: Mapping[str, float],
    target: Mapping[str, float],
    baseline: Mapping[str, Any],
    protected_buckets: Sequence[str],
    shortfall_buckets: Sequence[str],
    breakthrough_buckets: Sequence[str],
    value_weights: ValueWeightsV2,
    execution_weights: ExecutionWeightsV2,
    capital: float,
    required_floor: float,
    fee: float,
    outcome_row_budget: int,
    shares_by_bucket: Mapping[str, float] | None = None,
    reserved_by_bucket: Mapping[str, float] | None = None,
    authorization_rule: str = EXECUTION_AUTHORIZATION_JOINT_VALUE,
    edge_probabilities: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], int]:
    """Score one complete Maker bundle over its joint fill lattice.

    Extracted so that exactly one implementation answers "is this basket
    authorized".  The selector runs it over every candidate combination; the
    re-score runs it once over the winner with the sizes that will actually
    be sent.  Two implementations of this arithmetic would be two different
    answers to the same question, and the one that decides execution has to
    be the one that granted the authorization.

    ``shares_by_bucket`` and ``reserved_by_bucket`` override each leg's
    quantity and reserved cash.  Absent, the actions' own numbers are used
    and the evaluation is the ordinary candidate scoring.

    Returns the evaluation and how many joint outcome rows it consumed, so
    the caller keeps ownership of the resource bound across candidates.
    """

    def _leg_shares(action: Mapping[str, Any]) -> float:
        bucket = str(action["bucket"])
        if shares_by_bucket is not None:
            return _nonnegative(
                "delta_v2_bundle_sized_shares", shares_by_bucket.get(bucket)
            )
        return float(action["maximum_shares"])

    reserved = 0.0
    for row in combo:
        bucket = str(dict(row["action"])["bucket"])
        reserved += (
            _nonnegative(
                "delta_v2_bundle_sized_reserved", reserved_by_bucket.get(bucket)
            )
            if reserved_by_bucket is not None
            else float(row.get("capital_reserved_usdc") or 0.0)
        )
    structural_blockers: set[str] = set()
    for row in combo:
        for gate, passed in dict(row.get("hard_gates") or {}).items():
            if passed is not True and gate not in {
                "budget_ok",
                "profit_floor_ok",
            }:
                structural_blockers.add(str(gate))
        for blocker in row.get("blockers", ()):
            if blocker not in {
                "budget_ok",
                "profit_floor_ok",
            }:
                structural_blockers.add(str(blocker))
    if reserved > capital + EPS:
        structural_blockers.add("budget_ok")

    distributions = [
        tuple(dict(outcome) for outcome in row.get("fill_distribution", ()))
        for row in combo
    ]
    if any(not distribution for distribution in distributions):
        raise ContractError("delta_v2_fill_distribution_missing")
    rule = str(authorization_rule or "").strip()
    if rule not in EXECUTION_AUTHORIZATION_RULES:
        raise ContractError("delta_v2_execution_authorization_rule_invalid")
    edge_belief = edge_probabilities if edge_probabilities else probabilities
    joint_rows: list[dict[str, Any]] = []
    expected_delta = 0.0
    expected_edge = 0.0
    outcome_rows = 0
    for outcomes in product(*distributions):
        outcome_rows += 1
        if outcome_rows > outcome_row_budget:
            raise ContractError("delta_v2_bundle_outcome_bound_exceeded")
        probability = math.prod(
            _probability(
                "delta_v2_joint_fill_probability",
                outcome.get("probability"),
            )
            for outcome in outcomes
        )
        resulting_shares = dict(current_shares)
        resulting_cost = dict(current_cost)
        fills: list[dict[str, Any]] = []
        for evaluation, outcome in zip(combo, outcomes):
            action = dict(evaluation["action"])
            bucket = str(action["bucket"])
            fraction = _probability(
                "delta_v2_joint_fill_fraction",
                outcome.get("fill_fraction"),
            )
            filled_shares = _leg_shares(action) * fraction
            price = float(action["limit_price"])
            resulting_shares[bucket] += filled_shares
            resulting_cost[bucket] += filled_shares * price * (1.0 + fee)
            fills.append({
                "bucket": bucket,
                "fill_fraction": fraction,
                "filled_shares": filled_shares,
                "filled_notional_usdc": filled_shares * price,
            })
        value = evaluate_basket_value_v2(
            probabilities=probabilities,
            shares_by_bucket=resulting_shares,
            cost_by_bucket=resulting_cost,
            protected_buckets=protected_buckets,
            shortfall_buckets=shortfall_buckets,
            breakthrough_buckets=breakthrough_buckets,
            weights=value_weights,
            target_shares_by_bucket=target,
        )
        delta = float(value["value"]) - float(baseline["value"])
        expected_delta += probability * delta
        edge_delta = whole_basket_edge_delta_v1(
            edge_probabilities=edge_belief,
            current_shares=current_shares,
            current_cost=current_cost,
            resulting_shares=resulting_shares,
            resulting_cost=resulting_cost,
        )
        expected_edge += probability * edge_delta
        joint_rows.append({
            "probability": probability,
            "fills": fills,
            "delta_value": delta,
            "delta_edge": edge_delta,
            "resulting_value_sha256": value["content_sha256"],
            "resulting_profit_floor": value["profit_floor"],
        })
    probability_mass = sum(row["probability"] for row in joint_rows)
    if abs(probability_mass - 1.0) > 1e-8:
        raise ContractError("delta_v2_joint_fill_probability_mass_invalid")
    if any(
        row["resulting_profit_floor"] + EPS < required_floor
        for row in joint_rows
    ):
        structural_blockers.add("profit_floor_ok")
    execution_cost = sum(
        float(row.get("normalized_execution_cost") or 0.0)
        for row in combo
    )
    # Charter 73: under the whole-basket edge rule the score the
    # authorization reads is the increment's edge; the penalized value
    # delta is carried beside it for the audit.
    score = (
        expected_edge - execution_cost
        if rule == EXECUTION_AUTHORIZATION_WHOLE_BASKET_EDGE
        else expected_delta - execution_cost
    )
    expected_shares = dict(current_shares)
    for evaluation in combo:
        action = dict(evaluation["action"])
        bucket = str(action["bucket"])
        expected_fraction = sum(
            float(outcome["probability"])
            * float(outcome["fill_fraction"])
            for outcome in evaluation["fill_distribution"]
        )
        expected_shares[bucket] += _leg_shares(action) * expected_fraction
    fit = sum(
        max(0.0, target[bucket] - expected_shares[bucket]) ** 2
        for bucket in required
    )
    # Charter 74: a ticket is authorized on structure alone -- it exists
    # to open the chain, and its edge is known to be nothing.
    authorized = (
        not structural_blockers
        and (
            rule == EXECUTION_AUTHORIZATION_CHAIN_TICKET
            or score > execution_weights.epsilon_score + EPS
        )
    )
    identity = {
        "schema": BUNDLE_EVALUATION_SCHEMA,
        "kernel_version": KERNEL_VERSION,
        "required_buckets": list(required),
        "selected_action_sha256s": sorted(
            str(row["content_sha256"]) for row in combo
        ),
        "selected_actions": sorted(
            (dict(row) for row in combo),
            key=lambda row: (
                str(dict(row["action"]).get("bucket") or ""),
                str(row.get("content_sha256") or ""),
            ),
        ),
        "baseline_value_sha256": baseline["content_sha256"],
        "joint_fill_outcomes": joint_rows,
        "expected_delta_value": expected_delta,
        "expected_delta_edge": expected_edge,
        "authorization_rule": rule,
        "normalized_execution_cost": execution_cost,
        "delta_basket_score_v2": score,
        "inventory_fit_distance": fit,
        "capital_reserved_usdc": reserved,
        "capital_available_usdc": capital,
        "minimum_profit_floor": required_floor,
        "blockers": sorted(structural_blockers),
        "authorized": authorized,
        "state": "TRADE" if authorized else "HOLD",
        "basket_complete": True,
        "calls_order_gateway": False,
        "places_orders": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }
    if shares_by_bucket is not None:
        identity["sized_shares_by_bucket"] = {
            str(bucket): float(shares_by_bucket[bucket])
            for bucket in sorted(shares_by_bucket)
        }
    if reserved_by_bucket is not None:
        identity["sized_reserved_by_bucket"] = {
            str(bucket): float(reserved_by_bucket[bucket])
            for bucket in sorted(reserved_by_bucket)
        }
    return (
        {**identity, "content_sha256": content_sha256(identity)},
        outcome_rows,
    )


def rescore_maker_bundle_v2(
    selected_actions: Sequence[Mapping[str, Any]],
    *,
    required_buckets: Sequence[str],
    probabilities: Mapping[str, Any],
    current_shares_by_bucket: Mapping[str, Any],
    current_cost_by_bucket: Mapping[str, Any],
    target_shares_by_bucket: Mapping[str, Any],
    protected_buckets: Sequence[str],
    shortfall_buckets: Sequence[str],
    breakthrough_buckets: Sequence[str],
    value_weights: ValueWeightsV2,
    execution_weights: ExecutionWeightsV2,
    capital_available_usdc: Any,
    minimum_profit_floor: Any,
    sized_shares_by_bucket: Mapping[str, Any],
    sized_notional_by_bucket: Mapping[str, Any],
    fee_rate: Any = 0.0,
    authorization_rule: str = EXECUTION_AUTHORIZATION_JOINT_VALUE,
    edge_probabilities: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Re-authorize the bundle at the sizes that will actually be sent.

    Depth, budget or a share quantum can cut a leg below the clip the
    selector authorized.  The result is not a smaller version of the
    authorized basket -- floor, CVaR, concentration and coverage are joint
    over the legs, so shrinking one changes all of them and can turn a legal
    basket illegal.  Rather than refuse every cut basket or send an unscored
    one, this puts the actual sizes back through the identical evaluation the
    selector used and lets the same gates rule on it: no structural blocker,
    every joint fill outcome above the profit floor, reserved within capital,
    and a score still clearing epsilon.

    The verdict is a receipt, not an authority: the caller decides what to do
    with a refusal, and this writes nothing.
    """

    required = tuple(sorted(
        set(_text("delta_v2_required_bucket", x) for x in required_buckets)
    ))
    if len(required) != len(tuple(required_buckets)):
        raise ContractError("delta_v2_required_bucket_duplicate")
    if not required:
        raise ContractError("delta_v2_rescore_required_buckets_empty")
    capital = _nonnegative("delta_v2_capital_available", capital_available_usdc)
    required_floor = _finite(
        "delta_v2_minimum_profit_floor", minimum_profit_floor
    )
    fee = _nonnegative("delta_v2_fee_rate", fee_rate)
    if fee >= 1.0:
        raise ContractError("delta_v2_fee_rate_invalid")
    buckets = _canonical_buckets(probabilities)
    if set(required) - set(buckets):
        raise ContractError("delta_v2_required_bucket_unknown")
    current_shares = _canonical_map(
        "delta_v2_current_shares", current_shares_by_bucket, buckets
    )
    current_cost = _canonical_map(
        "delta_v2_current_cost", current_cost_by_bucket, buckets
    )
    target = _canonical_map(
        "delta_v2_target_shares", target_shares_by_bucket, buckets
    )
    combo: list[dict[str, Any]] = []
    seen_buckets: set[str] = set()
    for raw in selected_actions:
        if not isinstance(raw, Mapping):
            raise ContractError("delta_v2_action_evaluation_mapping_required")
        if raw.get("schema") != ACTION_EVALUATION_SCHEMA:
            raise ContractError("delta_v2_action_evaluation_schema_invalid")
        action = raw.get("action")
        if not isinstance(action, Mapping):
            raise ContractError("delta_v2_action_mapping_required")
        # The third family filter found after charter 65 landed: the two in
        # enumeration and bundle selection came out on 2026-08-31, and this
        # one sat in the re-score path, where it would have refused the first
        # clamped-clip re-authorization of any taker bundle.  Same rule as
        # the selector: the enumerated families pass, an unknown mode does not.
        if str(action.get("order_mode") or "").upper() not in EXECUTION_FAMILIES:
            raise ContractError("delta_v2_rescore_order_mode_invalid")
        bucket = str(action.get("bucket") or "")
        if bucket in seen_buckets:
            raise ContractError("delta_v2_rescore_leg_duplicate")
        seen_buckets.add(bucket)
        combo.append(dict(raw))
    if seen_buckets != set(required):
        # A re-score is only ever of the complete authorized bundle; a proper
        # subset is the incomplete basket the atomicity contract forbids.
        raise ContractError("delta_v2_rescore_bundle_incomplete")
    shares = {
        bucket: _nonnegative(
            "delta_v2_bundle_sized_shares", sized_shares_by_bucket.get(bucket)
        )
        for bucket in required
    }
    notional = {
        bucket: _nonnegative(
            "delta_v2_bundle_sized_reserved",
            sized_notional_by_bucket.get(bucket),
        )
        for bucket in required
    }
    combo.sort(key=lambda row: str(dict(row["action"]).get("bucket") or ""))
    baseline = evaluate_basket_value_v2(
        probabilities=probabilities,
        shares_by_bucket=current_shares,
        cost_by_bucket=current_cost,
        protected_buckets=protected_buckets,
        shortfall_buckets=shortfall_buckets,
        breakthrough_buckets=breakthrough_buckets,
        weights=value_weights,
        target_shares_by_bucket=target,
    )
    evaluation, _rows = _evaluate_maker_bundle_combo(
        combo,
        required=required,
        probabilities=probabilities,
        current_shares=current_shares,
        current_cost=current_cost,
        target=target,
        baseline=baseline,
        protected_buckets=protected_buckets,
        shortfall_buckets=shortfall_buckets,
        breakthrough_buckets=breakthrough_buckets,
        value_weights=value_weights,
        execution_weights=execution_weights,
        capital=capital,
        required_floor=required_floor,
        fee=fee,
        outcome_row_budget=min(
            MAX_BUNDLE_JOINT_OUTCOME_ROWS,
            execution_weights.maximum_candidates ** 3,
        ),
        shares_by_bucket=shares,
        reserved_by_bucket=notional,
        authorization_rule=authorization_rule,
        edge_probabilities=edge_probabilities,
    )
    identity = {
        "schema": BUNDLE_RESCORE_SCHEMA,
        "kernel_version": KERNEL_VERSION,
        "required_buckets": list(required),
        "authorized_bundle_action_sha256s": evaluation[
            "selected_action_sha256s"
        ],
        "sized_shares_by_bucket": {
            bucket: shares[bucket] for bucket in required
        },
        "sized_notional_by_bucket": {
            bucket: notional[bucket] for bucket in required
        },
        "delta_basket_score_v2": evaluation["delta_basket_score_v2"],
        "expected_delta_value": evaluation["expected_delta_value"],
        "normalized_execution_cost": evaluation["normalized_execution_cost"],
        "capital_reserved_usdc": evaluation["capital_reserved_usdc"],
        "capital_available_usdc": capital,
        "minimum_profit_floor": required_floor,
        "epsilon_score": execution_weights.epsilon_score,
        "blockers": evaluation["blockers"],
        "authorized": evaluation["authorized"],
        "state": evaluation["state"],
        "rescored_evaluation_sha256": evaluation["content_sha256"],
        "calls_order_gateway": False,
        "places_orders": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


# Which order families may compose a complete basket.
#
# This selector skipped everything that was not MAKER until 2026-08-31, which
# made it the second of two filters standing between charter 65 and
# production: the runtime handed it maker actions only, and it dropped
# anything else that reached it anyway.  Nothing under this line was ever
# maker-specific -- the two bundle helpers pass ``order_mode`` straight
# through to the fill-distribution contract, which already knows that FOK is
# all-or-nothing and a resting maker needs partial and zero support.  So the
# family was a filter, not an assumption, and it comes out.
#
# The caller decides which family to enumerate (``delta_v2_runtime`` has one
# mode at a time); this only stops an unknown mode from entering a bundle.
EXECUTION_FAMILIES = frozenset({"MAKER", "FOK"})


def select_complete_maker_bundle_v2(
    actions: Sequence[Mapping[str, Any]],
    *,
    required_buckets: Sequence[str],
    probabilities: Mapping[str, Any],
    current_shares_by_bucket: Mapping[str, Any],
    current_cost_by_bucket: Mapping[str, Any],
    target_shares_by_bucket: Mapping[str, Any],
    protected_buckets: Sequence[str],
    shortfall_buckets: Sequence[str],
    breakthrough_buckets: Sequence[str],
    value_weights: ValueWeightsV2,
    execution_weights: ExecutionWeightsV2,
    capital_available_usdc: Any,
    minimum_profit_floor: Any,
    fee_rate: Any = 0.0,
    authorization_rule: str = EXECUTION_AUTHORIZATION_JOINT_VALUE,
    edge_probabilities: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Select one Maker leg per required bucket as one joint basket action.

    No proper subset is an admissible action.  Fill outcomes are evaluated
    jointly against the same baseline inventory so floor, CVaR, concentration,
    coverage and overfill remain basket-level quantities.
    """

    if len(actions) > execution_weights.maximum_candidates:
        raise ContractError("delta_v2_action_candidate_bound_exceeded")
    required = tuple(sorted(set(_text("delta_v2_required_bucket", x) for x in required_buckets)))
    if len(required) != len(tuple(required_buckets)):
        raise ContractError("delta_v2_required_bucket_duplicate")
    capital = _nonnegative("delta_v2_capital_available", capital_available_usdc)
    required_floor = _finite(
        "delta_v2_minimum_profit_floor", minimum_profit_floor
    )
    fee = _nonnegative("delta_v2_fee_rate", fee_rate)
    if fee >= 1.0:
        raise ContractError("delta_v2_fee_rate_invalid")
    buckets = _canonical_buckets(probabilities)
    if set(required) - set(buckets):
        raise ContractError("delta_v2_required_bucket_unknown")
    current_shares = _canonical_map(
        "delta_v2_current_shares", current_shares_by_bucket, buckets
    )
    current_cost = _canonical_map(
        "delta_v2_current_cost", current_cost_by_bucket, buckets
    )
    target = _canonical_map(
        "delta_v2_target_shares", target_shares_by_bucket, buckets
    )
    baseline = evaluate_basket_value_v2(
        probabilities=probabilities,
        shares_by_bucket=current_shares,
        cost_by_bucket=current_cost,
        protected_buckets=protected_buckets,
        shortfall_buckets=shortfall_buckets,
        breakthrough_buckets=breakthrough_buckets,
        weights=value_weights,
        target_shares_by_bucket=target,
    )
    by_bucket: dict[str, list[dict[str, Any]]] = {
        bucket: [] for bucket in required
    }
    for raw in actions:
        if not isinstance(raw, Mapping):
            raise ContractError("delta_v2_action_evaluation_mapping_required")
        if raw.get("schema") != ACTION_EVALUATION_SCHEMA:
            raise ContractError("delta_v2_action_evaluation_schema_invalid")
        action = raw.get("action")
        if not isinstance(action, Mapping):
            raise ContractError("delta_v2_action_mapping_required")
        mode = str(action.get("order_mode") or "").upper()
        if mode not in EXECUTION_FAMILIES:
            continue
        bucket = str(action.get("bucket") or "")
        if bucket in by_bucket:
            by_bucket[bucket].append(dict(raw))
    for rows in by_bucket.values():
        rows.sort(key=lambda row: str(row.get("content_sha256") or ""))

    duplicate_action_sha256s: list[str] = []
    for bucket, rows in by_bucket.items():
        unique_rows: list[dict[str, Any]] = []
        seen_hashes: set[str] = set()
        for row in rows:
            action_sha256 = str(row.get("content_sha256") or "")
            if action_sha256 in seen_hashes:
                # Identical immutable action evaluations add no feasible
                # basket state.  Descriptor clipping can legitimately produce
                # this shape (for example, two cash clips both limited by the
                # same remaining share gap); never multiply it into the joint
                # outcome lattice.
                duplicate_action_sha256s.append(action_sha256)
                continue
            seen_hashes.add(action_sha256)
            unique_rows.append(row)
        by_bucket[bucket] = unique_rows

    deduplicated_candidate_count = sum(
        len(rows) for rows in by_bucket.values()
    )
    missing = sorted(bucket for bucket, rows in by_bucket.items() if not rows)
    evaluated: list[dict[str, Any]] = []
    combination_count = 0
    outcome_count = 0
    resource_bound = {
        "outcome_row_limit": min(
            MAX_BUNDLE_JOINT_OUTCOME_ROWS,
            execution_weights.maximum_candidates ** 3,
        ),
        "requested_joint_outcome_count": 0,
        "evaluated_joint_outcome_count": 0,
        "requested_candidate_action_count": len(actions),
        "deduplicated_candidate_action_count": deduplicated_candidate_count,
        "evaluated_candidate_action_count": 0,
        "duplicate_action_sha256s": sorted(duplicate_action_sha256s),
        "pruned_action_sha256s": [],
    }
    if not missing and required:
        by_bucket, resource_bound = _bounded_maker_bundle_candidates(
            by_bucket,
            required=required,
            probabilities=probabilities,
            execution_weights=execution_weights,
        )
        resource_bound.update({
            "raw_candidate_action_count": len(actions),
            "deduplicated_candidate_action_count": deduplicated_candidate_count,
            "duplicate_action_sha256s": sorted(duplicate_action_sha256s),
        })
        combination_bound = execution_weights.maximum_candidates ** min(
            len(required), 2
        )
        for combo in product(*(by_bucket[bucket] for bucket in required)):
            combination_count += 1
            if combination_count > combination_bound:
                raise ContractError("delta_v2_bundle_combination_bound_exceeded")
            evaluation, consumed = _evaluate_maker_bundle_combo(
                combo,
                required=required,
                probabilities=probabilities,
                current_shares=current_shares,
                current_cost=current_cost,
                target=target,
                baseline=baseline,
                protected_buckets=protected_buckets,
                shortfall_buckets=shortfall_buckets,
                breakthrough_buckets=breakthrough_buckets,
                value_weights=value_weights,
                execution_weights=execution_weights,
                capital=capital,
                required_floor=required_floor,
                fee=fee,
                outcome_row_budget=(
                    resource_bound["outcome_row_limit"] - outcome_count
                ),
                authorization_rule=authorization_rule,
                edge_probabilities=edge_probabilities,
            )
            outcome_count += consumed
            evaluated.append(evaluation)

    eligible = [row for row in evaluated if row["authorized"] is True]
    winner = max(
        eligible,
        key=lambda row: (
            float(row["delta_basket_score_v2"]),
            -float(row["inventory_fit_distance"]),
            str(row["content_sha256"]),
        ),
        default=None,
    )
    if winner is not None:
        blockers: list[str] = []
    elif not required:
        # The target-inventory argmax selected the no-new-action baseline.
        # This is an economic HOLD, not a missing execution leg.
        blockers = ["complete_basket_target_not_activated"]
    elif missing:
        blockers = [
            "complete_basket_candidate_missing",
            *[f"missing_leg_candidate:{bucket}" for bucket in missing],
        ]
    else:
        blockers = ["complete_basket_delta_not_positive"]
    identity = {
        "schema": BUNDLE_SELECTION_SCHEMA,
        "kernel_version": KERNEL_VERSION,
        "required_buckets": list(required),
        "candidate_action_count": len(actions),
        "joint_outcome_resource_bound": resource_bound,
        "bundle_candidate_count": len(evaluated),
        "selected_bundle_sha256": (
            winner["content_sha256"] if winner is not None else None
        ),
        "selected_action_sha256s": (
            winner["selected_action_sha256s"] if winner is not None else []
        ),
        "selected_score": (
            winner["delta_basket_score_v2"] if winner is not None else 0.0
        ),
        "capital_reserved_usdc": (
            winner["capital_reserved_usdc"] if winner is not None else 0.0
        ),
        "basket_complete": winner is not None,
        "authorized": winner is not None,
        "blockers": blockers,
        "hold_selected": winner is None,
        "calls_order_gateway": False,
        "places_orders": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }
    return {
        **identity,
        "selected_actions": (
            winner["selected_actions"] if winner is not None else []
        ),
        "selected_bundle": winner,
        "content_sha256": content_sha256(identity),
    }


def solve_reservation_price_v2(
    *,
    cash_clip: Any,
    price_grid: Sequence[Any],
    action_kwargs: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the highest grid price with positive raw basket-value delta."""

    clip = _positive("delta_v2_reservation_cash_clip", cash_clip)
    if not price_grid:
        raise ContractError("delta_v2_reservation_price_grid_empty")
    prices = sorted(set(_positive("delta_v2_reservation_price", x) for x in price_grid))
    if prices[-1] >= 1.0:
        raise ContractError("delta_v2_reservation_price_not_below_one")
    evaluations = []
    for price in prices:
        row = evaluate_action_v2(
            **dict(action_kwargs),
            cash_clip=clip,
            limit_price=price,
        )
        evaluations.append(row)
    admissible = [
        row for row in evaluations if row["expected_delta_value"] > EPS
    ]
    reserve = (
        max(float(row["action"]["limit_price"]) for row in admissible)
        if admissible
        else None
    )
    identity = {
        "schema": RESERVATION_SCHEMA,
        "kernel_version": KERNEL_VERSION,
        "cash_clip": clip,
        "price_grid": prices,
        "reservation_price": reserve,
        "admissible_price_count": len(admissible),
        "evaluation_sha256s": [
            row["content_sha256"] for row in evaluations
        ],
        "calls_order_gateway": False,
        "places_orders": False,
        "orders_submitted": 0,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def allocate_cross_event_bundle_capital_v2(
    bundles: Sequence[Mapping[str, Any]],
    *,
    capital_available_usdc: Any,
    lambda_portfolio_concentration: Any,
    portfolio_concentration_scale: Any,
    maximum_candidates: int = 16,
) -> dict[str, Any]:
    """Allocate shared capital without ever splitting a Maker basket bundle.

    Each candidate must be one already-authorized complete-basket evaluation.
    Portfolio selection may choose at most one whole bundle per event; an
    individual leg is never a portfolio candidate.
    """

    capital = _nonnegative("delta_v2_capital_available", capital_available_usdc)
    concentration_lambda = _nonnegative(
        "delta_v2_portfolio_concentration_lambda",
        lambda_portfolio_concentration,
    )
    concentration_scale = _positive(
        "delta_v2_portfolio_concentration_scale",
        portfolio_concentration_scale,
    )
    if maximum_candidates <= 0 or maximum_candidates > 20:
        raise ContractError("delta_v2_portfolio_candidate_bound_invalid")
    if len(bundles) > maximum_candidates:
        raise ContractError("delta_v2_portfolio_candidate_bound_exceeded")
    candidates: list[tuple[str, dict[str, Any]]] = []
    for raw in bundles:
        if (
            not isinstance(raw, Mapping)
            or raw.get("schema") != BUNDLE_EVALUATION_SCHEMA
            or raw.get("authorized") is not True
            or raw.get("basket_complete") is not True
        ):
            continue
        selected_actions = raw.get("selected_actions")
        required_buckets = {
            str(bucket) for bucket in raw.get("required_buckets", ())
        }
        if not isinstance(selected_actions, Sequence) or not selected_actions:
            raise ContractError("delta_v2_portfolio_bundle_actions_missing")
        event_ids: set[str] = set()
        selected_buckets: set[str] = set()
        for evaluation in selected_actions:
            if (
                not isinstance(evaluation, Mapping)
                or evaluation.get("schema") != ACTION_EVALUATION_SCHEMA
                or not isinstance(evaluation.get("action"), Mapping)
            ):
                raise ContractError(
                    "delta_v2_portfolio_bundle_action_invalid"
                )
            action = evaluation["action"]
            event_ids.add(str(action.get("event_id") or ""))
            selected_buckets.add(str(action.get("bucket") or ""))
        if "" in event_ids or len(event_ids) != 1:
            raise ContractError("delta_v2_portfolio_bundle_event_invalid")
        if not required_buckets or selected_buckets != required_buckets:
            raise ContractError(
                "delta_v2_portfolio_bundle_support_incomplete"
            )
        candidates.append((next(iter(event_ids)), dict(raw)))

    def utility(
        rows: Sequence[tuple[str, Mapping[str, Any]]],
    ) -> tuple[float, float, float]:
        reserved = sum(
            float(bundle["capital_reserved_usdc"])
            for _, bundle in rows
        )
        by_event: dict[str, float] = {}
        for event, bundle in rows:
            by_event[event] = by_event.get(event, 0.0) + float(
                bundle["capital_reserved_usdc"]
            )
        concentration = sum(
            (value / concentration_scale) ** 2 for value in by_event.values()
        )
        score = sum(
            float(bundle["delta_basket_score_v2"])
            for _, bundle in rows
        )
        return score - concentration_lambda * concentration, reserved, concentration

    best_rows: tuple[tuple[str, Mapping[str, Any]], ...] = ()
    best_key = (0.0, 0.0, "")
    for size in range(1, len(candidates) + 1):
        for rows in combinations(candidates, size):
            events = [event for event, _ in rows]
            if len(events) != len(set(events)):
                continue
            score, reserved, _ = utility(rows)
            if reserved > capital + EPS:
                continue
            digest = content_sha256(
                sorted(bundle["content_sha256"] for _, bundle in rows)
            )
            key = (score, -reserved, digest)
            if key > best_key:
                best_key = key
                best_rows = rows
    selected_score, reserved, concentration = utility(best_rows)
    if selected_score <= EPS:
        best_rows = ()
        selected_score, reserved, concentration = 0.0, 0.0, 0.0
    identity = {
        "schema": PORTFOLIO_SELECTION_SCHEMA,
        "kernel_version": KERNEL_VERSION,
        "candidate_count": len(candidates),
        "selected_bundle_sha256s": sorted(
            bundle["content_sha256"] for _, bundle in best_rows
        ),
        "selected_score": selected_score,
        "capital_available_usdc": capital,
        "capital_reserved_usdc": reserved,
        "portfolio_concentration": concentration,
        "hold_selected": not best_rows,
        "calls_order_gateway": False,
        "places_orders": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }
    selected_bundles = [
        dict(bundle)
        for _, bundle in sorted(best_rows, key=lambda row: row[0])
    ]
    selected_actions = sorted(
        (
            dict(action)
            for bundle in selected_bundles
            for action in bundle["selected_actions"]
        ),
        key=lambda row: (
            str(dict(row["action"]).get("event_id") or ""),
            str(dict(row["action"]).get("bucket") or ""),
            str(row.get("content_sha256") or ""),
        ),
    )
    return {
        **identity,
        "selected_bundles": selected_bundles,
        "selected_actions": selected_actions,
        "content_sha256": content_sha256(identity),
    }


__all__ = [
    "ACTION_EVALUATION_SCHEMA",
    "ACTION_SCHEMA",
    "ACTION_SELECTION_SCHEMA",
    "BUNDLE_EVALUATION_SCHEMA",
    "BUNDLE_SELECTION_SCHEMA",
    "ExecutionWeightsV2",
    "KERNEL_VERSION",
    "PORTFOLIO_SELECTION_SCHEMA",
    "REQUIRED_HARD_GATES",
    "RESERVATION_SCHEMA",
    "TARGET_SOLUTION_SCHEMA",
    "VALUE_SCHEMA",
    "ValueWeightsV2",
    "allocate_cross_event_bundle_capital_v2",
    "clamp_declining_gaps_v2",
    "contiguous_edge_clamp_v2",
    "evaluate_action_v2",
    "evaluate_basket_value_v2",
    "EXCHANGE_LOT_TOP_UP",
    "EXCHANGE_LOT_TOP_UP_MODES",
    "raise_sub_lot_targets_v2",
    "hold_action_v2",
    "select_action_v2",
    "select_complete_maker_bundle_v2",
    "solve_reservation_price_v2",
    "solve_target_inventory_v2",
]
