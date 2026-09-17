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
# Charter 84 (user ruling, 2026-09-05): the completion of a basket the venue
# broke.  A seat whose legs were partly refused -- the gateway's dropped
# collateral reading, the dollar minimum -- holds some rungs and not others;
# the Kelly law then sizes the seat below what is already held and offers
# nothing, and the grid's slivers fail the scenario floor, so the ladder
# stays broken.  This candidate brings every short rung of the seated window
# up to the level the held rungs sit at, and nothing else.
LADDER_BALANCE_CANDIDATE_ORIGIN = "LADDER_BALANCE"
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
# Charter 74 (user ruling, 2026-09-03): only P0 opens a station-day, so
# P0-M and P1 have a position to add to and a later stage never opens an
# empty chain.  The clause's other half -- a P0 the Kelly law sizes to
# nothing still opens with a flat ticket -- is retired (user ruling
# 2026-09-08, "已经没有门票这个语义了"); a P0 with no edge now opens nothing,
# and the stages that follow find an empty chain and hold.
CHAIN_OPENING_STAGE = "P0"
# The fill below which a buy is dust and is kept out of the equity curve.
# It was named for the ticket and outlived it: the ticket is retired (user
# ruling, 2026-09-08, "已经没有门票这个语义了"), a fill too small to compound
# is still a fill too small to compound.  The paper ledger's DUST_COST_FLOOR
# is this same number.
DUST_FILL_FLOOR_USDC = 0.01
EXECUTION_AUTHORIZATION_RULES = frozenset({
    EXECUTION_AUTHORIZATION_JOINT_VALUE,
    EXECUTION_AUTHORIZATION_WHOLE_BASKET_EDGE,
})
PRODUCTION_EXECUTION_AUTHORIZATION = EXECUTION_AUTHORIZATION_WHOLE_BASKET_EDGE
# The blocker a bundle carries when the whole-basket edge rule is asked to
# authorize without a seated partition: refused by name, never authorized
# on a belief no gate has deflated.
EXECUTION_EDGE_BELIEF_MISSING = "edge_belief_missing"


def execution_authorization_rule_v1(
    *, winner_candidate_origin: Any, objective_form: Any
) -> str:
    """The execution layer's rule for a plan, one answer for both lanes.

    Charter 92: every increment is authorized on the whole-basket deflated
    edge.  ``winner_candidate_origin`` no longer changes the answer -- the
    one origin that used to, the chain ticket, is retired (user ruling
    2026-09-08) -- and is kept in the signature because the plan records
    which candidate the rule was struck for.  The curve form keeps
    ``joint_value_v2`` for its Kelly-less seats.
    """

    _ = str(winner_candidate_origin or "")
    form = str(objective_form or "")
    if form == KELLY_OBJECTIVE_FORM:
        return EXECUTION_AUTHORIZATION_WHOLE_BASKET_EDGE
    return EXECUTION_AUTHORIZATION_JOINT_VALUE
OBJECTIVE_FORMS = frozenset({
    VALUE_OBJECTIVE_FORM, CURVE_OBJECTIVE_FORM, KELLY_OBJECTIVE_FORM,
})
# Charter 141 (user, 2026-09-17: "前端增加凯利的调整按钮", "单站不能调整么"):
# the capital layer's three numbers are operator dials, written by the board
# to ``config/trading/capital_layer_v1.json`` and read fresh before every
# production compile (``refresh_capital_layer``).  An environment override
# still pins a value for a research arm; the built-in defaults stand when the
# file is absent or invalid.  The paper ledger runs as a fresh process and
# reads the file once at import, so its fingerprint names the values it used.
CAPITAL_LAYER_CONFIG_PATH = os.environ.get("PM_CAPITAL_LAYER_CONFIG", "") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "config", "trading", "capital_layer_v1.json",
)
CAPITAL_LAYER_SCHEMA = "basket_capital_layer_v1"
CAPITAL_LAYER_DEFAULTS = {
    "kelly_fraction": 0.25,
    "station_day_sizing_fraction": 0.065,
    "station_day_ceiling_fraction": 0.10,
}
CAPITAL_LAYER_BOUNDS = {
    "kelly_fraction": (0.05, 1.0),
    "station_day_sizing_fraction": (0.005, 0.25),
    "station_day_ceiling_fraction": (0.01, 0.50),
}
_CAPITAL_LAYER_ENV = {
    "kelly_fraction": "PM_KELLY_FRACTION",
    "station_day_sizing_fraction": "PM_STATION_DAY_SOFT_FRACTION",
    "station_day_ceiling_fraction": "PM_STATION_DAY_HARD_FRACTION",
}


def validate_capital_layer(raw: Mapping[str, Any]) -> dict[str, float]:
    """The three dials, bounded and ordered, or ``ContractError``."""

    out: dict[str, float] = {}
    for key, (lo, hi) in CAPITAL_LAYER_BOUNDS.items():
        try:
            value = float(raw[key])
        except (KeyError, TypeError, ValueError) as exc:
            raise ContractError(f"capital_layer_{key}_missing") from exc
        if not math.isfinite(value) or not lo - 1e-12 <= value <= hi + 1e-12:
            raise ContractError(f"capital_layer_{key}_out_of_bounds")
        out[key] = value
    if out["station_day_sizing_fraction"] > out["station_day_ceiling_fraction"] + 1e-12:
        raise ContractError("capital_layer_sizing_above_ceiling")
    return out


def _resolve_capital_layer() -> tuple[dict[str, float], str, str | None]:
    values = dict(CAPITAL_LAYER_DEFAULTS)
    source = "defaults"
    error: str | None = None
    try:
        with open(CAPITAL_LAYER_CONFIG_PATH, encoding="utf-8") as handle:
            raw = json.load(handle)
        if raw.get("schema") != CAPITAL_LAYER_SCHEMA:
            raise ContractError("capital_layer_schema_invalid")
        values = validate_capital_layer(raw)
        source = "config"
    except FileNotFoundError:
        pass
    except (OSError, ValueError, ContractError) as exc:
        error = f"{type(exc).__name__}:{exc}"[:160]
    for key, env in _CAPITAL_LAYER_ENV.items():
        pinned = os.environ.get(env, "").strip()
        if pinned:
            values[key] = float(pinned)
            source = "env" if source == "defaults" else source + "+env"
    values = validate_capital_layer(values)
    return values, source, error


_CAPITAL_LAYER_INITIAL, _CAPITAL_LAYER_SOURCE, _CAPITAL_LAYER_ERROR = _resolve_capital_layer()
DEFAULT_KELLY_FRACTION = _CAPITAL_LAYER_INITIAL["kelly_fraction"]
# Which way the Kelly fraction enters the curve's bankroll (2026-09-09).
#
# Fractional Kelly is ``f * q*(W)``, and the log-optimal stake is linear in
# the bankroll, so ``f * q*(W) == q*(f * W)``: the risk capital a fractional
# bet is shaped against is ``W * f``.  This module shipped ``W / f``, which
# is the same algebra read backwards -- ``q*(W/f) == q*(W)/f`` -- so at
# f=0.25 the curve asked for FOUR TIMES full Kelly where a quarter was
# intended, sixteen times the stake, and turning the dial "down" made the
# ranking flatter and therefore bolder.  Verified against this package's own
# solver (three rungs, claims 0.30/0.20/0.10 at 0.22/0.15/0.08, W=30, depth
# and budget unbound): full Kelly 8.182 USDC, the intended quarter 2.045,
# what shipped 32.727.
#
# Consequences of the correction, both deliberate:
#
# * the ruin guard below now bites at ``W * f`` rather than ``W / f``.  A
#   station-day that has committed more than its own risk capital scores
#   minus infinity on every candidate INCLUDING the baseline, which is the
#   wall the flat per-station cap (``runtime._station_capital_usdc``, off
#   the live path since the 2026-08-26 per-scope stake cap and deleted
#   2026-09-17) was built to keep unreachable -- and it sized that cap
#   against ``W / f``.  Making the wall unreachable again is a decision
#   about the station cap, not about this function, and is deliberately NOT
#   taken here.
# * ``curve_score`` is audit-only on every served path (charter 72 makes the
#   Kelly law the winner chain, and the seat is the probability-mass
#   shortlist head, not the curve's argmax), so nothing production decides
#   moves.  What moves is the recorded score and therefore the solution's
#   content hash, once.
#
# ``w_over_f`` reproduces the shipped arithmetic byte for byte, so the books
# taken under it stay reproducible.
CURVE_BANKROLL_CONVENTIONS = ("w_times_f", "w_over_f")
CURVE_BANKROLL_CONVENTION = (
    os.environ.get("PM_CURVE_BANKROLL_CONVENTION", "").strip().lower()
    or "w_times_f"
)
if CURVE_BANKROLL_CONVENTION not in CURVE_BANKROLL_CONVENTIONS:
    raise RuntimeError(
        f"unknown curve bankroll convention: {CURVE_BANKROLL_CONVENTION}"
    )


def curve_effective_bankroll_v1(
    bankroll_usdc: float,
    kelly_fraction: float | None = None,
    *,
    convention: str | None = None,
) -> float:
    """The risk capital the log-wealth curve is shaped against.

    One function so the ranking and the generator that feeds it cannot drift:
    ``curve_score`` divides the settled PnL by this, and
    ``_continuous_optimum_candidates`` hands the same number to the solver
    with fraction one, which is what makes its first candidate the ranking's
    own argmax rather than an approximation of it.
    """

    bankroll = float(bankroll_usdc)
    fraction = float(
        DEFAULT_KELLY_FRACTION if kelly_fraction is None else kelly_fraction
    )
    if not (bankroll > 0.0) or not math.isfinite(bankroll):
        raise ContractError("delta_v2_curve_bankroll_invalid")
    if not (0.0 < fraction <= 1.0):
        raise ContractError("delta_v2_curve_kelly_fraction_invalid")
    mode = convention or CURVE_BANKROLL_CONVENTION
    if mode not in CURVE_BANKROLL_CONVENTIONS:
        raise ContractError("delta_v2_curve_bankroll_convention_invalid")
    return bankroll * fraction if mode == "w_times_f" else bankroll / fraction


# How much of the whole book one station-day may put at risk.
#
# This lives here, in the kernel, because it is one rule and there must not be
# a second copy of it.  Production capped a station at a flat 30 USDC while
# the paper ledger capped it at 2% of a book that moves with its own P&L, so
# the same station-day was sized two different ways depending on which side
# you asked.  User ruling, 2026-08-26: the floating fraction is the rule, and
# both sides read it from here.
#
# The scope is the station-day, not the station-day-stage.  One book means one
# position per station-day, so the cap binds the whole chain: whatever P0
# spent is spent, and a top-up at P0-M or P1 draws on what is left.
#
# Charter 141 (user rulings 2026-09-17: "统一优化资金层面", "资金池用余额",
# then "改").  The book is now the real account -- exchange cash plus the
# marked value of open positions (``account_equity``), not the pool counter
# that only ever subtracted fills from a paper starting balance -- and the
# station-day has TWO numbers instead of 2% times a stage share:
#
# * ``MAX_STAKE_FRACTION_PER_SCOPE`` (6.5%) bounds what SIZING may target:
#   the Kelly stake, the grid and centre candidates.
# * ``STATION_DAY_HARD_FRACTION`` (10%) bounds what may actually go OUT: the
#   lift of a rung to the venue's lot and dollar minimum (charters 70/83),
#   a completion back to the held level (charter 84), the execution
#   authorization's budget gate, and the order sizing that follows.
#
# Together that is "max(6.5%, the basket's minimum ticket) <= 10%": a seat is
# never refused because the ticket costs more than the sizing number, only
# when it costs more than the ceiling.  Measured before the ruling on the
# real equity (99.80): of 24 recently traded station-days, 2% x stage share
# replayed to 0 trades (a P0 allowance of 1.20 buys no lot); this rule
# replayed to 20, and released 9 of 37 capital-suspect holds (EPWA 09-18's
# 22C completion among them).  Historical order spend: p50 3.95, p90 5.46,
# max 6.36.
#
# The stage share (P0 0.6 / P0-M 0.8 / P1 1.0) is retired with it.  It held
# 40% of the allowance back for later stages that charter 137 measured could
# never reach it, and it was the reason a P0 completion could not afford the
# leg its own opening had planned for.
MAX_STAKE_FRACTION_PER_SCOPE = _CAPITAL_LAYER_INITIAL["station_day_sizing_fraction"]
STATION_DAY_HARD_FRACTION = _CAPITAL_LAYER_INITIAL["station_day_ceiling_fraction"]
_CAPITAL_LAYER_MTIME: int | None = None


def current_capital_layer() -> dict[str, Any]:
    """The three dials this process is sizing with, and where they came from."""

    return {
        "schema": CAPITAL_LAYER_SCHEMA,
        "kelly_fraction": DEFAULT_KELLY_FRACTION,
        "station_day_sizing_fraction": MAX_STAKE_FRACTION_PER_SCOPE,
        "station_day_ceiling_fraction": STATION_DAY_HARD_FRACTION,
        "source": _CAPITAL_LAYER_SOURCE,
        "error": _CAPITAL_LAYER_ERROR,
    }


def refresh_capital_layer() -> dict[str, Any]:
    """Re-read the dials when the file changed; an invalid file changes nothing.

    Every kernel function that reads a dial resolves it at call time, so the
    module attributes set here are the values the next compile sizes with.
    """

    global DEFAULT_KELLY_FRACTION, MAX_STAKE_FRACTION_PER_SCOPE
    global STATION_DAY_HARD_FRACTION, _CAPITAL_LAYER_MTIME
    global _CAPITAL_LAYER_SOURCE, _CAPITAL_LAYER_ERROR
    try:
        mtime: int | None = os.stat(CAPITAL_LAYER_CONFIG_PATH).st_mtime_ns
    except OSError:
        mtime = None
    if mtime == _CAPITAL_LAYER_MTIME:
        return current_capital_layer()
    try:
        values, source, error = _resolve_capital_layer()
    except ContractError as exc:
        _CAPITAL_LAYER_ERROR = f"ContractError:{exc}"[:160]
        return current_capital_layer()
    if error is not None:
        # A file the board half-wrote or a value out of bounds: the book keeps
        # sizing with the last good dials rather than falling to the defaults.
        _CAPITAL_LAYER_MTIME = mtime
        _CAPITAL_LAYER_ERROR = error
        return current_capital_layer()
    _CAPITAL_LAYER_MTIME = mtime
    DEFAULT_KELLY_FRACTION = values["kelly_fraction"]
    MAX_STAKE_FRACTION_PER_SCOPE = values["station_day_sizing_fraction"]
    STATION_DAY_HARD_FRACTION = values["station_day_ceiling_fraction"]
    _CAPITAL_LAYER_SOURCE, _CAPITAL_LAYER_ERROR = source, error
    return current_capital_layer()
CAPITAL_LAYER_RULE_VERSION = "real_equity_soft_sizing_hard_ceiling_v1"
# The stages a chain moves through.  Used only to tell a named stage from an
# unnamed caller; no stage owns a share of the allowance any more.
CHAIN_STAGES = frozenset({"P0", "P0-M", "P1"})


def scope_stake_cap_usdc(
    *,
    book_equity_usdc: float,
    stage: str = "",
    committed_usdc: float = 0.0,
    scope_fraction: float | None = None,
) -> float:
    """What SIZING may still target for this station-day, in USDC.

    ``book_equity_usdc`` is the whole book -- the real account since charter
    141 -- not the station's allotment: the cap is a share of what we have,
    so it grows with a winning book and shrinks with a losing one.

    ``committed_usdc`` is what this station-day has already spent -- filled
    and confirmed-pending alike, because both have used part of the
    allowance whether or not they have settled.

    ``stage`` is kept on the signature so every reader keeps its call and is
    deliberately unread: charter 141 retired the per-stage share.
    """

    del stage
    if scope_fraction is None:
        scope_fraction = MAX_STAKE_FRACTION_PER_SCOPE
    equity = float(book_equity_usdc)
    if not equity > 0.0:
        return 0.0
    room = equity * float(scope_fraction) - float(committed_usdc)
    return max(0.0, room)


def station_day_ceiling_usdc(
    *,
    book_equity_usdc: float,
    committed_usdc: float = 0.0,
    hard_fraction: float | None = None,
) -> float:
    """Charter 141: what may still go OUT for this station-day, in USDC.

    The ceiling the venue-minimum lift, a completion and the execution
    authorization are bounded by.  Never below the sizing cap, so a caller
    that reads both can only widen what sizing chose, never narrow it.
    """

    if hard_fraction is None:
        hard_fraction = STATION_DAY_HARD_FRACTION
    equity = float(book_equity_usdc)
    if not equity > 0.0:
        return 0.0
    ceiling = equity * max(float(hard_fraction), MAX_STAKE_FRACTION_PER_SCOPE)
    return max(0.0, ceiling - float(committed_usdc))


# Charter 137 (user ruling, 2026-09-16).  Which budget the per-leg TARGET
# LEVEL is read off, as against which budget bounds the cash that goes out.
#
# ``net`` is charter 72 as written -- ``b = min(W_book*f, 站日及阶段剩余额度)``
# -- and is what production and the ledger have both shipped.  It reads a
# STOCK (the level a leg should hold) off a FLOW (the cash still unspent),
# then compares that level with the shares the chain already holds.  Once P0
# has spent its 0.6 share, the later stages' remaining room converts to fewer
# shares than are already held on every leg, ``increment <= EPS`` fires, and
# ``_whole_basket_kelly_candidate`` returns None -- so the 0.2 and 0.2 that
# ``STAGE_BUDGET_SHARE`` reserves for P0-M and P1 can never be reached by a
# top-up.  Measured on the 45-station book, event_date >= 2026-09-09, by how
# much of its gross stage allowance the chain had already spent: <25% spent
# -> a Kelly candidate was offered on 15% of P0-M held decisions; 25-60% ->
# 2%; 60-90% -> 0 of 107; >=90% -> 0 of 43.  Across all three stages, 259
# held decisions past 60% produced zero Kelly candidates.  Every top-up that
# did land in that window came from ``LADDER_BALANCE`` (charter 84), which
# already sizes off the gross allowance (``completion_capital_usdc``,
# charter 84(八); ``completion_budget_usdc_v1``, charter 136(四)) -- the lane
# that reads the gross number is the lane that still moves.
#
# ``gross`` reads the level off the stage's whole allowance with nothing
# committed, exactly as the completion lanes do.  The cash bound does not
# move: the caller still clamps the increment to what the scope may spend,
# so the chain's total outlay stays inside 2% x stage share.  The tilt
# (consolidation cut) is deliberately not applied to the level, on the same
# argument charter 84(八) made for completions -- it has already cut once at
# open, and cutting the level again leaves a slice too small to protect the
# sunk legs -- while it keeps binding on the increment through the room.
#
# Shipped default is ``net``: identical to the pre-clause arithmetic, byte
# for byte.  ``gross`` is the arm charter 137 exists to measure.
KELLY_TOPUP_BASIS_MODES = ("net", "gross")
KELLY_TOPUP_BASIS = (
    os.environ.get("PM_KELLY_TOPUP_BASIS", "").strip().lower() or "net"
)
if KELLY_TOPUP_BASIS not in KELLY_TOPUP_BASIS_MODES:
    raise RuntimeError(f"unknown Kelly top-up basis: {KELLY_TOPUP_BASIS}")
KELLY_TOPUP_BASIS_RULE_VERSION = "kelly_target_level_off_gross_allowance_v1"


def kelly_target_budget_usdc_v1(
    *,
    book_equity_usdc: float,
    stage: str,
    capital_available_usdc: float,
    basis: str = KELLY_TOPUP_BASIS,
) -> float:
    """Charter 137: the budget the per-leg target LEVEL is sized against.

    Never below the room the caller was handed, so the arm can only widen a
    target, never narrow one; the cash that actually goes out is bounded by
    ``capital_available_usdc`` at the call site, not here.
    """

    room = max(0.0, float(capital_available_usdc))
    if str(basis) != "gross":
        return room
    # An unnamed stage is a caller that is not a chain -- it keeps the room it
    # was given.  (Until charter 141 the stage share also said by how much a
    # named stage widened; the share is retired and every stage reads the one
    # sizing allowance.)
    if str(stage) not in CHAIN_STAGES:
        return room
    gross = scope_stake_cap_usdc(
        book_equity_usdc=book_equity_usdc,
        stage=str(stage),
        committed_usdc=0.0,
    )
    return max(room, float(gross))
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
# Charter 83 (user ruling, 2026-09-05): the exchange's second lot.  A
# marketable BUY must be worth at least one dollar -- "invalid amount for a
# marketable BUY order ($0.95), min size: $1" -- so five shares of a rung
# priced under 0.20 are refused however many lots they are.  On the first
# live morning 9 of 18 legs sat under a dollar (42% of every planned BUY leg
# since 08-28), and every basket lost its cheap wing to it.  A rung under the
# dollar is raised to the fewest whole shares that reach it, all-or-none with
# the lot raise above, through the same function and the same affordability
# check; 0 turns the rule off (the pre-charter-83 arm).
EXCHANGE_MINIMUM_ORDER_NOTIONAL_USDC = float(
    os.environ.get("PM_EXCHANGE_MINIMUM_ORDER_NOTIONAL_USDC", "").strip()
    or "1.0"
)
if not math.isfinite(EXCHANGE_MINIMUM_ORDER_NOTIONAL_USDC) or (
    EXCHANGE_MINIMUM_ORDER_NOTIONAL_USDC < 0.0
):
    raise RuntimeError(
        "unknown PM_EXCHANGE_MINIMUM_ORDER_NOTIONAL_USDC: "
        f"{EXCHANGE_MINIMUM_ORDER_NOTIONAL_USDC}"
    )


# The grid an order's size is written on (user ruling, 2026-09-06: round
# the size to whole lots before it is persisted).  ``venue_minimum_shares_v1``
# below has always returned whole shares -- "a fractional share cannot be
# bought" -- but that statement only reached the legs sitting ON the
# minimum.  A leg the objective sized above it kept whatever the cash
# divided out to, and the sizing grid it was floored to was 0.01 shares:
# an unexamined default that no market snapshot has ever overridden.
#
# Measured before shipping, over all 1,581 order intents in the book: 1,206
# carry a fractional size, but none since 2026-09-04 -- charter 70's lot and
# charter 83's dollar already land every current leg on a whole share, so
# this is a guard rather than a change.  It stops being one the day a leg
# sizes above the venue minimum, which is any day the book or the bankroll
# grows.
#
# In the kernel because production's ``cash_phase`` and the paper ledger
# must size one station-day the same way and both read it from here.
# ``venue`` is the pre-ruling arm, kept so a replay can re-derive the book
# as it was sized.
ORDER_SHARE_GRID_MODES = ("whole", "venue")
ORDER_SHARE_GRID = (
    os.environ.get("PM_ORDER_SHARE_GRID", "").strip().lower() or "whole"
)
if ORDER_SHARE_GRID not in ORDER_SHARE_GRID_MODES:
    raise RuntimeError(f"unknown PM_ORDER_SHARE_GRID: {ORDER_SHARE_GRID}")
ORDER_SHARE_STEP_SHARES = 1.0


def order_share_step_v1(venue_quantum: Any = 0.0) -> float:
    """The step an order's share count is floored to.

    The venue's own quantum is a floor on how fine the grid can be, never a
    licence to go finer than a whole share: under ``whole`` the step is the
    coarser of the two, so a venue that one day states a five-share tick
    still governs.
    """

    quantum = float(venue_quantum or 0.0)
    if ORDER_SHARE_GRID != "whole":
        return quantum if quantum > 0.0 else ORDER_SHARE_STEP_SHARES
    return max(ORDER_SHARE_STEP_SHARES, quantum)


def floor_to_order_share_grid_v1(shares: Any, venue_quantum: Any = 0.0) -> float:
    """Floor a share count onto that grid, never up: the caps above it --
    depth, budget, the authorised clip -- are all upper bounds, so rounding
    up would breach whichever one was binding."""

    step = order_share_step_v1(venue_quantum)
    value = float(shares or 0.0)
    if value <= EPS or step <= 0.0:
        return 0.0
    return round(max(0.0, math.floor((value + EPS) / step) * step), 10)


def venue_minimum_shares_v1(
    *,
    min_lot_shares: Any,
    ask: Any,
    minimum_notional_usdc: Any = EXCHANGE_MINIMUM_ORDER_NOTIONAL_USDC,
) -> float:
    """The fewest shares the exchange will take at this ask: lot and dollar.

    The lot is the market's stated ``orderMinSize`` (charter 70); the dollar
    is the venue's marketable-order minimum (charter 83).  Whole shares,
    because a fractional share cannot be bought; zero when neither bound is
    stated or the rung has no ask, which the caller reads as "not known to
    be under anything".
    """

    lot = _nonnegative("delta_v2_min_lot", min_lot_shares or 0.0)
    price = _nonnegative("delta_v2_ask", ask or 0.0)
    notional = _nonnegative(
        "delta_v2_minimum_notional", minimum_notional_usdc or 0.0
    )
    if price <= EPS or notional <= EPS:
        return lot
    by_notional = float(math.ceil(notional / price - 1e-9))
    return max(lot, by_notional)


# The consolidation tilt's floor (operator ruling 2026-09-06: "改").  The
# tilt cuts a lit chain's capital to a twentieth, and the floor is what keeps
# the cut from becoming a refusal.  It was three minimum clips (0.75 USDC),
# sized before charter 70 made a leg at least one exchange lot and charter
# 83 made it at least a dollar: on 2026-09-06 a tilted ZGSZ chain reached its
# P1 window with 0.75 USDC against legs that cost 1.05 each, so every later
# stage on a tilted chain -- 42 of 90 chains that week -- held by
# arithmetic.  The floor is now the venue's minimum on each of the legs the
# seat may take: the largest ``legs`` per-leg minimums among the candidate
# support, each at least one minimum clip.  One implementation, read by
# production and by the paper ledger.
CONSOLIDATION_TILT_FLOOR_VERSION = "venue_minimum_per_leg_v1"

# Charter 131 (user ruling, 2026-09-12, Q8: "0.03 地板：读中价"; value raised
# to 0.05 by rulings 2026-09-17, ``MINIMUM_LEG_ALL_IN_ASK``): the floor
# is a flat price on the rung's own reference price -- the mid where
# the book is two-sided (charter 103), the ask where nobody bids -- and it
# does not fall with belief.  Charter 91's ``anchor / probability`` scaling
# (2026-09-06, the ZGSZ 27°C case) is retired with the ruling; a rung the
# market prices under three cents is not bought however much we believe in
# it.  One implementation, read by production's enumerator and the paper
# ledger alike; the rule string is in both fingerprints.
LEG_FLOOR_RULE_VERSION = "flat_mid_v2"


LEG_FLOOR_PRICE_BASIS = (
    os.environ.get("PM_LEG_FLOOR_PRICE_BASIS", "mid").strip().lower() or "mid"
)
if LEG_FLOOR_PRICE_BASIS not in {"mid", "ask"}:
    raise ValueError(f"unknown leg floor price basis: {LEG_FLOOR_PRICE_BASIS}")


def leg_floor_reference_price(
    all_in_ask: float | None,
    bid: float | None,
    *,
    basis: str = LEG_FLOOR_PRICE_BASIS,
) -> float | None:
    """The price the leg floor is measured on (charter 103, user ruling
    2026-09-07: "地板改读中间价").

    The floor asks what the market thinks a bucket is worth, and the ask is
    that question's upper bound -- so on a 0.01/0.03 book the only number
    the floor ever saw was 0.03, and a bucket the market values at two
    cents cleared a three-cent floor.  EGLC 2026-09-07 22C is the case:
    ask 0.03 admitted it against a floor of exactly 0.03, and the mid it
    was actually worth was 0.02.  The mid is the two-sided answer.

    The ask keeps whatever fee the caller already put on it and the bid
    does not carry one, because the fee is what we would pay and nobody
    pays it on the side we are not lifting; the reference is therefore a
    hair above the raw mid, which is the strict direction.

    A one-sided book keeps the ask.  There is no mid without a bid, and
    reading the missing bid as zero would bar every bucket nobody is
    bidding on -- 174 of the 850 quoted legs on 2026-09-07 -- which is a
    wider rule than the one that was given.
    """

    if all_in_ask is None:
        return None
    try:
        ask = float(all_in_ask)
    except (TypeError, ValueError):
        return None
    if basis == "ask":
        return ask
    try:
        best_bid = float(bid) if bid is not None else 0.0
    except (TypeError, ValueError):
        best_bid = 0.0
    if not math.isfinite(best_bid) or best_bid <= 0.0 or best_bid >= ask:
        return ask
    return (ask + best_bid) / 2.0


def leg_floor_all_in_ask(
    minimum_all_in_ask: float,
    probability: float | None,
) -> float:
    """The all-in price below which a rung is a dead lottery: the flat floor.

    Charter 131 (Q8): three cents, whatever the belief.  ``probability``
    stays on the signature so every reader keeps its call and is
    deliberately unread -- the rule relaxes on nothing.
    """

    del probability
    return float(minimum_all_in_ask)


def leg_below_floor(
    all_in_ask: float | None,
    probability: float | None,
    minimum_all_in_ask: float,
    *,
    bid: float | None = None,
    basis: str = LEG_FLOOR_PRICE_BASIS,
) -> bool:
    """Whether a quoted rung sits under the flat floor (charter 131, Q8).

    Charter 103: measured on the mid when the book is two-sided.  ``bid``
    left None keeps the ask, which is what a caller with no book to hand
    can honestly say.
    """

    reference = leg_floor_reference_price(all_in_ask, bid, basis=basis)
    if reference is None:
        return False
    return float(reference) < leg_floor_all_in_ask(
        minimum_all_in_ask, probability
    )


def consolidation_tilt_floor_usdc(
    *,
    minimum_clip_usdc: Any,
    legs: int,
    leg_minimum_costs_usdc: Sequence[Any],
) -> float:
    """The least a tilted decision may keep: ``legs`` venue minimums.

    ``leg_minimum_costs_usdc`` is, per candidate bucket, what the smallest
    order the venue will take there costs (``venue_minimum_shares_v1`` times
    the ask).  Each is raised to at least ``minimum_clip_usdc``; the largest
    ``legs`` of them are summed, and when fewer buckets are priced the
    largest is repeated, so the floor covers the widest seat the enumerator
    may choose rather than the cheapest.  No priced bucket at all falls back
    to ``legs`` minimum clips, which is the floor it replaced.
    """

    clip = _nonnegative("consolidation_tilt_minimum_clip", minimum_clip_usdc)
    count = int(legs)
    if count <= 0:
        return 0.0
    costs = sorted(
        (
            max(clip, _nonnegative("consolidation_tilt_leg_cost", value))
            for value in leg_minimum_costs_usdc
        ),
        reverse=True,
    )
    if not costs:
        return clip * count
    top = costs[:count]
    while len(top) < count:
        top.append(costs[0])
    return float(sum(top))


def raise_sub_lot_targets_v2(
    *,
    target_shares_by_bucket: Mapping[str, Any],
    support: Sequence[str],
    min_lot_by_bucket: Mapping[str, Any],
    ask_by_bucket: Mapping[str, Any],
    capital_available_usdc: Any,
    mode: str = EXCHANGE_LOT_TOP_UP,
    minimum_notional_usdc: Any = EXCHANGE_MINIMUM_ORDER_NOTIONAL_USDC,
    held_shares_by_bucket: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Raise every seated rung under the venue minimum to exactly that minimum.

    ``held_shares_by_bucket`` (charter 84, 2026-09-05) is what the chain
    already holds per rung; the venue's minimum is a rule about *orders*, so
    it binds on the increment a rung would buy, not on the target that
    includes what is held.  A rung with no increment is not an order and is
    not raised; a rung whose increment is under the minimum is raised to
    held plus the minimum; and only the increments are charged against the
    capital.  Absent (the paper ledger hands in increments already), every
    target is read as an increment, which is the pre-charter-84 reading.

    Pure and deterministic.  ``min_lot_by_bucket`` is the exchange's stated
    minimum per market (0 when unstated); ``minimum_notional_usdc`` is the
    venue's marketable-order floor in dollars (charter 83; 0 turns it off);
    ``ask_by_bucket`` prices the raise.  A rung under neither bound, or with
    no ask, is left alone -- it is not known to be under anything.  The raise
    is all-or-none over the support: a basket that can afford to lift only
    some of its short rungs still cannot be bought, so nothing is changed and
    the refusal is named.  Affordability is the whole basket's -- the solved
    targets at their asks plus the raises -- against
    ``capital_available_usdc``, which is what the scope may still spend.
    """

    if mode not in EXCHANGE_LOT_TOP_UP_MODES:
        raise ContractError("delta_v2_exchange_lot_top_up_mode_invalid")
    targets = {
        str(bucket): _nonnegative("delta_v2_target_shares", value)
        for bucket, value in target_shares_by_bucket.items()
    }
    notional_floor = _nonnegative(
        "delta_v2_minimum_notional", minimum_notional_usdc or 0.0
    )
    unchanged = {
        "schema": "delta_v2_exchange_lot_top_up_v1",
        "mode": mode,
        "minimum_notional_usdc": notional_floor,
        "target_shares_by_bucket": dict(targets),
        "raised_buckets": (),
        "notional_raised_buckets": (),
        "lot_raise_usdc": 0.0,
        "requested_lot_raise_usdc": 0.0,
        "refused_reason": None,
    }
    if mode == "off":
        return unchanged
    capital = _nonnegative("delta_v2_capital_available", capital_available_usdc)
    held = {
        str(bucket): _nonnegative("delta_v2_held_shares", value or 0.0)
        for bucket, value in (held_shares_by_bucket or {}).items()
    }
    raised: list[str] = []
    by_notional: list[str] = []
    lots: dict[str, float] = {}
    extra = 0.0
    committed = 0.0
    for raw_bucket in support:
        bucket = str(raw_bucket)
        target = targets.get(bucket, 0.0)
        increment = max(0.0, target - held.get(bucket, 0.0))
        lot = _nonnegative(
            "delta_v2_min_lot", min_lot_by_bucket.get(bucket, 0.0) or 0.0
        )
        ask = _nonnegative(
            "delta_v2_ask", ask_by_bucket.get(bucket, 0.0) or 0.0
        )
        committed += increment * ask
        floor_shares = venue_minimum_shares_v1(
            min_lot_shares=lot,
            ask=ask,
            minimum_notional_usdc=notional_floor,
        )
        if (
            floor_shares <= EPS
            or ask <= EPS
            or increment <= EPS
            or increment + EPS >= floor_shares
        ):
            continue
        raised.append(bucket)
        if floor_shares > lot + EPS:
            by_notional.append(bucket)
        lots[bucket] = held.get(bucket, 0.0) + floor_shares
        extra += (floor_shares - increment) * ask
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
        "notional_raised_buckets": tuple(by_notional),
        "lot_raise_usdc": extra,
        "requested_lot_raise_usdc": extra,
    }


# Charter 93 (user ruling, 2026-09-06: "把另外一个缺口也堵死，即 10% 地板亏损，
# 下线"): the in-support loss tolerance is retired.  Charter 37's floor let a
# basket lose a tenth of its premium when the truth landed inside its own
# window; from here the worst in-support settlement is floored at zero of
# the premium.  And the floor is re-read on the lifted targets: the solver
# judged the equal-share candidate, then charters 70/83 raised rungs to the
# venue's lot and dollar floor one at a time, and the shape that went out
# was never judged -- KDAL 2026-09-07 left at 5 / 5 / 6 shares for 5.12 and
# would have lost 0.12 on two of its three rungs while winning.  ``off`` is
# the pre-ruling arm (minus a tenth, no re-read); the fraction itself can
# be overridden for a research arm.
CHARTER_93_SUPPORT_FLOOR = (
    os.environ.get("PM_CHARTER_93_SUPPORT_FLOOR", "").strip().lower() or "on"
)
if CHARTER_93_SUPPORT_FLOOR not in {"on", "off"}:
    raise RuntimeError(
        f"unknown charter 93 support floor mode: {CHARTER_93_SUPPORT_FLOOR}"
    )
_SUPPORT_FLOOR_OVERRIDE = os.environ.get(
    "PM_SUPPORT_SCENARIO_FLOOR_FRACTION", ""
).strip()
SUPPORT_SCENARIO_FLOOR_FRACTION = (
    float(_SUPPORT_FLOOR_OVERRIDE) if _SUPPORT_FLOOR_OVERRIDE
    else (0.05 if CHARTER_93_SUPPORT_FLOOR == "on" else -0.10)
)
# The upper bound is 0.5, not 0.0, so a POSITIVE floor can be measured: the
# standing rule is "a settlement inside the support may not lose" and the
# stronger contract worth pricing is "it must make at least delta" (v2.1 of
# the strategy plan, section 5).  Nothing about the default moves -- charter
# 93 still serves 0.0 -- but an arm can now ask for 0.05, which is
# ``min q_i >= 1.05 C``, and be refused for a typo rather than for the sign.
if not (-1.0 <= SUPPORT_SCENARIO_FLOOR_FRACTION <= 0.5):
    raise RuntimeError(
        f"support scenario floor fraction out of range: {SUPPORT_SCENARIO_FLOOR_FRACTION}"
    )
# Charter 131 (user ruling, 2026-09-12): the seat's shape rule, named so a
# candidate window and a lambda table can both say which shape they were
# enumerated under.  The runtime reads it for the seat; the ledger writes
# it on every candidate row; the effective-edge store partitions on it.
SEAT_SHAPE_RULE_VERSION = "seat_shape_mean_anchored_v1"

# Charter 131 (12b) (user ruling, 2026-09-12): the hit-protection gate is
# ``min q_i >= 1.05 C`` -- every settlement inside the support returns at
# least five percent on the whole premium -- at every point the floor is
# read (opening, after the lift, top-up, completion; Q13).  Charter 93's
# zero floor is the ``off`` history; the arm above can still price others.
SUPPORT_SCENARIO_FLOOR_RULE_VERSION = "support_floor_after_lift_v2_hit_roi_5pct"

# --------------------------------------------------------------------------
# Charter 110: one objective, three arrivals of information, a one-way action
# set.  The execution layer answers to the objective every round instead of
# being handed a target and left alone.
# --------------------------------------------------------------------------

COMPLETION_VERDICT_SCHEMA = "delta_basket_completion_verdict_v1"
COMPLETION_CLAIM_SHAPE_RULE_VERSION = "completion_claim_shape_v1"
RESTING_RUNG_ACTION_RULE_VERSION = "bid_anchored_up_only_v3"

# The fill-shape correction.  Cells are keyed on ``ask / claim`` for the leg
# that did NOT fill, and hold the measured realized-over-claimed ratio.
#
# The market's refusal to sell us a leg is information about that leg, and it
# is information our claim does not carry -- the same shape charter 68's
# lambda(a) reads at the whole-basket level, read here per missing leg.
# Measured 2026-09-07 on 2,575 missing legs of three-leg windows the system
# claimed edge on (maker_shadow_order, bid+1t rested 900s, positive lead):
#
#     ask <= claim   1,971 legs   claim 0.183  ask 0.089  hit  5.1%  -> 0.28
#     ask >  claim     604 legs   claim 0.168  ask 0.242  hit 27.0%  -> 1.60
#
# So a leg the market discounts relative to us realizes a quarter of what we
# claim for it, and a leg the market bids above us realizes more than we
# claim.  Seventy-one percent of the second group is the market's own modal
# bucket.  The old completion ceiling -- buy only where ``ask <= claim`` --
# bought exactly the first group and refused exactly the second.
#
# Confidence, stated because the two directions do not have the same
# strength (station-day clustered bootstrap, 90%): the first group's
# completion edge is -0.038 [-0.053, -0.021], which does not cross zero --
# completing a leg the market discounts loses money.  The second group's is
# +0.028 [-0.029, +0.087], which does cross zero -- completing a leg the
# market bids up is indistinguishable from break-even, and what breaks the
# tie is the support floor, not the edge.
COMPLETION_CLAIM_SHAPE_V1 = (
    (1.0, 0.28),
    (None, 1.60),
)

# Past this point the completion is measurably negative in every zone
# (measured 2026-09-07: -0.026 at 1-3h, -0.075 inside the last hour) and the
# residual can no longer be undone -- see ``ONE_WAY_ACTION_SET`` below.  So
# this is the last hour at which completing is worth doing, not a moment at
# which a broken basket can be handed back.
COMPLETION_CUTOFF_HOURS = float(
    os.environ.get("PM_COMPLETION_CUTOFF_HOURS", "") or 3.0
)

# The action set is {BUY, CANCEL}.  Cancelling is always available and this
# module leans on it heavily; what is missing is SELL (production's
# ``order_action`` is the literal "BUY", and the only SELL lane is
# ``certain_win_recycle``, which sells buckets whose day has already settled
# in our favour).  A cancel removes an order that has not filled, not a leg
# that has -- so it can take away the half of the exposure that has not
# happened yet and not the half that has.  A basket short a leg therefore
# has exactly two futures: completed, or carried broken to settlement.
# There is no third branch, and no rule here may be written as though there
# were.
ONE_WAY_ACTION_SET = True

LEG_ACTION_NONE = "none"
LEG_ACTION_POST = "post"
LEG_ACTION_REST = "rest"
LEG_ACTION_REPRICE_UP = "reprice_up"
LEG_ACTION_CANCEL = "cancel"
LEG_ACTION_CROSS = "cross"
LEG_ACTIONS = frozenset({
    LEG_ACTION_NONE, LEG_ACTION_POST, LEG_ACTION_REST,
    LEG_ACTION_REPRICE_UP, LEG_ACTION_CANCEL, LEG_ACTION_CROSS,
})

BASKET_STATE_COMPLETE = "complete"
BASKET_STATE_PARTIAL = "partial"
BASKET_STATE_EMPTY = "empty"


def _floor_to_tick_v1(price: float, tick: float) -> float:
    return round(math.floor((float(price) + EPS) / float(tick)) * float(tick), 12)


def post_only_anchor_v1(*, bid: float, ask: float, tick: float) -> float | None:
    """Where a post-only rung stands on this book: bid+1t, clamped under the ask.

    Charter 110(一) puts the whole gap one tick over the bid and CLAMPS that
    rung to the highest post-only price (ask-1t) when the spread is one tick
    -- a rung on the ask is a take.  ``bid_anchored_ladder`` has clamped
    since 2026-09-07; this is the same clamp for the rules that judge a
    standing rung, so a rung is never "off its anchor" for sitting exactly
    where the ladder put it.

    Measured 2026-09-14 12:02:33Z: ZBAA 2026-09-16 31C rested at 0.26 on a
    0.26/0.27 book and 32C at 0.31 on 0.31/0.32 -- the clamped rung both
    times, the book unchanged since the post -- and ``resting_rung_action_v1``
    read the unclamped bid+1t as an anchor the bid had "risen" to, so past
    the dwell both were cancelled to be re-posted one tick higher: at the
    ask, where post-only cannot post.  Six of the twenty-one lifecycle
    cancels since 2026-09-13 were this shape, and the compile that was to
    re-mint them would have minted the same clamped rung for the same
    cancel five minutes later.

    ``None`` with no bid or no tick (no anchor).  With no ask the anchor is
    bid+1t unclamped, as before.
    """

    bid_value = float(bid)
    tick_value = float(tick)
    if bid_value <= EPS or tick_value <= EPS:
        return None
    anchor = _floor_to_tick_v1(bid_value + tick_value, tick_value)
    ask_value = float(ask or 0.0)
    if ask_value > EPS:
        anchor = min(anchor, _floor_to_tick_v1(ask_value - tick_value, tick_value))
    return anchor if anchor > EPS else None


def resting_rung_action_v1(
    *,
    limit_price: float,
    bid: float,
    ask: float,
    tick: float,
    age_seconds: float,
    dwell_seconds: float = 300.0,
    far_ticks: float = 3.0,
) -> dict[str, Any]:
    """Keep, follow up, or cancel one resting rung.  Charter 110(四).

    v2 (2026-09-14): the anchor is ``post_only_anchor_v1`` -- bid+1t clamped
    to the post-only ceiling, the same clamp the ladder posts with -- so a
    rung the ladder clamped to the bid on a one-tick book rests on its
    anchor instead of being read as one the bid rose past.

    v3 (2026-09-14): "the bid rose past the rung" means the BID is above the
    limit, not that bid+1t is.  The official book carries our own resting
    order, and a rung posted at bid+1t inside the spread IS the new best bid
    the moment it rests; v2 read that book as bid+1t > limit and, past the
    dwell, cancelled the rung to re-post one tick over itself.  Measured on
    every lifecycle cancel since 2026-09-12 (24): six were our own order
    alone at the top (VILK 2026-09-16 29C, 20 shares at 0.05, cancelled
    against a best bid of exactly 20 shares at 0.05 -- after v2 shipped), six
    the one-tick clamp v2 fixed, two others joining us at our price, one a
    GTD expiry, and eight a bid genuinely above the limit.  A rung AT the bid
    is first in the queue at the best price post-only can hold; it rests.

    The asymmetry is the whole rule.  A bid that RISES is the market coming
    to a leg, and the rung follows it up; a bid that FALLS is the market
    leaving, and the rung does not follow it down -- walking a limit down
    behind a falling bid re-mints it into the swept population by hand,
    which is the -0.0234-a-share cell charter 110 is built around.  And a
    book with no bid has no anchor at all: the shipped rule kept the rung
    standing there, which is precisely the state in which a collapsing ask
    runs through a stale limit.

    The no-bid cancel is not new law.  Charter 94(六)② already said a rung
    that has fallen off the recomputed ladder is cancelled after its dwell,
    and with no bid the ladder is EMPTY, so every rung has fallen off it.
    What kept those rungs standing was the implementation: the shipped
    ``resting_rung_verdict`` opened with ``if not ladder: keep``, ahead of
    the off-ladder test, and the distance rule could not reach them either
    because distance-from-bid is undefined with no bid.  Live case,
    2026-09-07 11:47Z: LFPB 21C had rested 18.8 hours at 0.061 with the bid
    gone and the ask down to 0.020.  Cancelled immediately here rather than
    after the dwell -- there is no anchor to wait for.

    Pure, and the single implementation: ``execution_completion_verdict_v1``
    calls this for its resting legs and the production lifecycle calls it for
    each rung, so the two cannot drift.
    """

    price = float(limit_price)
    bid_value = float(bid)
    tick_value = float(tick)
    age = max(0.0, float(age_seconds))
    out: dict[str, Any] = {
        "rule": RESTING_RUNG_ACTION_RULE_VERSION,
        "limit_price": price,
        "bid": bid_value,
        "ask": float(ask),
        "tick": tick_value,
        "age_seconds": age,
        "dwell_seconds": float(dwell_seconds),
        "far_ticks": float(far_ticks),
    }
    anchor = post_only_anchor_v1(bid=bid_value, ask=ask, tick=tick_value)
    if anchor is None:
        return {**out, "action": LEG_ACTION_CANCEL, "reason": "no_anchor"}
    out["anchor"] = anchor
    distance = abs(price - bid_value) / tick_value
    out["distance_ticks"] = distance
    # The bid above the limit, not the anchor above it: a rung at the bid --
    # our own order being the bid, others joining us there, or the one-tick
    # clamp -- is at the top of the book and has nothing to follow.
    rose = bid_value > price + tick_value / 2.0
    far = distance + EPS >= float(far_ticks)
    # Distance says the rung has been left behind by the book, and it is
    # measured unsigned because both directions are stale -- a rung far
    # BELOW the bid will never be reached and is only holding its bucket
    # shut, a rung far ABOVE it is the limit a collapsing ask runs through.
    # What the direction decides is what comes next, not whether to keep it:
    # a bid that ran up is a leg the market is coming to and the rung
    # follows without waiting out its dwell, a bid that ran down is a leg
    # the market is leaving and the rung does not follow it at all.
    if far:
        if rose:
            return {**out, "action": LEG_ACTION_REPRICE_UP,
                    "reason": "bid_rose_far", "post_at": anchor}
        return {**out, "action": LEG_ACTION_CANCEL, "reason": "bid_ran_far"}
    if rose:
        past = age + EPS >= float(dwell_seconds)
        return {
            **out,
            "action": LEG_ACTION_REPRICE_UP if past else LEG_ACTION_REST,
            "reason": "bid_rose_past_dwell" if past else "bid_rose_within_dwell",
            "post_at": anchor,
        }
    return {**out, "action": LEG_ACTION_REST, "reason": "on_anchor"}


def completion_claim_shape_factor_v1(
    *, claim: float, ask: float, factors: Sequence[Any] | None = None
) -> float:
    """The realized-over-claimed factor for a leg the market would not sell.

    Keyed on ``ask / claim``: how far the market's price for this leg stands
    from our own claim for it.  A claim at zero has no ratio and takes the
    dearest cell, which is the conservative end for a buy decision (the
    conditioned claim is zero either way).
    """

    table = tuple(factors or COMPLETION_CLAIM_SHAPE_V1)
    if not table:
        return 1.0
    claim_value = float(claim)
    ask_value = float(ask)
    if claim_value <= EPS:
        return float(table[-1][1])
    ratio = ask_value / claim_value
    for upper, factor in table:
        if upper is None or ratio <= float(upper) + EPS:
            return float(factor)
    return float(table[-1][1])


def completion_conditioned_claim_v1(
    *, claim: float, ask: float, factors: Sequence[Any] | None = None
) -> dict[str, Any]:
    """Our claim for a missing leg, conditioned on the market's refusal.

    The claim is never rewritten (user ruling, 2026-08-26: the market does
    not define value, it provides information).  This is a separate quantity
    the execution layer reads, and only for legs it does not hold.
    """

    factor = completion_claim_shape_factor_v1(
        claim=claim, ask=ask, factors=factors
    )
    raw = max(0.0, float(claim))
    conditioned = min(1.0, raw * factor)
    return {
        "rule": COMPLETION_CLAIM_SHAPE_RULE_VERSION,
        "claim": raw,
        "ask": float(ask),
        "ratio": (float(ask) / raw) if raw > EPS else None,
        "factor": factor,
        "conditioned_claim": conditioned,
        "buys": bool(conditioned > float(ask) + EPS),
    }


def execution_completion_verdict_v1(
    *,
    window: Sequence[str],
    target_shares: Mapping[str, Any],
    held_shares: Mapping[str, Any],
    held_cost_usdc: Mapping[str, Any],
    claim_by_bucket: Mapping[str, Any],
    book_by_bucket: Mapping[str, Mapping[str, Any]],
    rung_by_bucket: Mapping[str, Any] | None = None,
    hours_to_cutoff: float | None = None,
    dwell_seconds: float = 900.0,
    far_ticks: float = 3.0,
    cutoff_hours: float | None = None,
    shape_factors: Sequence[Any] | None = None,
) -> dict[str, Any]:
    """What the objective says to do with this basket, this round.

    Pure.  ``claim_by_bucket`` is the seated charter-68 partition (lambda*p),
    never a raw served vector.  ``book_by_bucket`` carries ``bid``/``ask``/
    ``tick`` per bucket; ``rung_by_bucket`` carries the resting rung's
    ``limit``/``age_seconds`` where one stands.

    The basket numbers are the four the objective needs to judge a position
    rather than a target: the support floor read on what is actually HELD,
    the claim mass sitting on legs we do not hold, the premium that mass puts
    at risk, and what completing at the current asks would cost and be worth.

    Note the identity worth not re-deriving at each call site: for a basket
    held level at ``n`` shares a leg, ``support_scenario_floor_v1`` is legal
    exactly when total premium <= n, which is the same inequality as charter
    74's "whole-basket per-share cost < 1".  Completion and the floor are one
    condition, not two.
    """

    rungs = dict(rung_by_bucket or {})
    cutoff = (
        COMPLETION_CUTOFF_HOURS if cutoff_hours is None else float(cutoff_hours)
    )
    ordered = tuple(str(bucket) for bucket in window)
    if len(set(ordered)) != len(ordered):
        raise ContractError("delta_v2_completion_window_duplicate")
    if not ordered:
        raise ContractError("delta_v2_completion_window_empty")

    held = {b: max(0.0, float(held_shares.get(b, 0.0) or 0.0)) for b in ordered}
    target = {
        b: max(0.0, float(target_shares.get(b, 0.0) or 0.0)) for b in ordered
    }
    gap = {b: max(0.0, target[b] - held[b]) for b in ordered}
    paid = sum(
        max(0.0, float(held_cost_usdc.get(b, 0.0) or 0.0)) for b in ordered
    )
    level = max(target.values(), default=0.0)
    missing = tuple(b for b in ordered if gap[b] > EPS)
    holds = tuple(b for b in ordered if held[b] > EPS)

    state = (
        BASKET_STATE_EMPTY if not holds
        else BASKET_STATE_COMPLETE if not missing
        else BASKET_STATE_PARTIAL
    )

    floor = support_scenario_floor_v1(
        shares_by_bucket=held,
        cost_by_bucket={
            b: max(0.0, float(held_cost_usdc.get(b, 0.0) or 0.0))
            for b in ordered
        },
        support=ordered,
    )

    def _book(bucket: str) -> dict[str, float]:
        raw = dict(book_by_bucket.get(bucket) or {})
        try:
            return {
                "bid": float(raw.get("bid") or 0.0),
                "ask": float(raw.get("ask") or 0.0),
                "tick": float(raw.get("tick") or 0.0),
            }
        except (TypeError, ValueError):
            return {"bid": 0.0, "ask": 0.0, "tick": 0.0}

    # The claim mass sitting on legs we do not hold in full, and the premium
    # already paid that it puts at risk.
    uncovered_mass = 0.0
    for bucket in ordered:
        if target[bucket] <= EPS:
            continue
        share_missing = min(1.0, gap[bucket] / target[bucket])
        uncovered_mass += (
            max(0.0, float(claim_by_bucket.get(bucket, 0.0) or 0.0))
            * share_missing
        )
    exposure = paid * uncovered_mass

    # What completing at the current asks would cost, and what our
    # conditioned belief says it is worth.
    completion_cost = 0.0
    completion_priced = True
    conditioned: dict[str, dict[str, Any]] = {}
    for bucket in missing:
        book = _book(bucket)
        if book["ask"] <= EPS:
            completion_priced = False
            continue
        completion_cost += gap[bucket] * book["ask"]
        conditioned[bucket] = completion_conditioned_claim_v1(
            claim=float(claim_by_bucket.get(bucket, 0.0) or 0.0),
            ask=book["ask"],
            factors=shape_factors,
        )
    cost_after = paid + completion_cost
    per_share_cost_after = (cost_after / level) if level > EPS else None
    cost_ceiling_ok = bool(
        completion_priced
        and per_share_cost_after is not None
        and per_share_cost_after < 1.0 - EPS
    )
    # The whole-basket edge of the completed position, on the conditioned
    # belief for the legs we would be buying and the seated claim for the
    # legs we already hold.
    edge_after = -cost_after
    for bucket in ordered:
        claim = max(0.0, float(claim_by_bucket.get(bucket, 0.0) or 0.0))
        if bucket in conditioned:
            claim = float(conditioned[bucket]["conditioned_claim"])
        edge_after += claim * target[bucket]

    in_completion_window = (
        hours_to_cutoff is None or float(hours_to_cutoff) > cutoff
    )

    legs: dict[str, dict[str, Any]] = {}
    for bucket in ordered:
        book = _book(bucket)
        rung = dict(rungs.get(bucket) or {})
        limit = float(rung.get("limit") or 0.0)
        age = float(rung.get("age_seconds") or 0.0)
        standing = limit > EPS
        entry: dict[str, Any] = {
            "gap_shares": gap[bucket],
            "bid": book["bid"],
            "ask": book["ask"],
            "tick": book["tick"],
            "limit_price": limit if standing else None,
        }
        if gap[bucket] <= EPS:
            legs[bucket] = {**entry, "action": LEG_ACTION_NONE,
                            "reason": "leg_at_target"}
            continue
        if bucket in conditioned:
            entry["conditioned"] = conditioned[bucket]
        # No anchor: the bid has gone.  A rung left standing here is the one
        # a collapsing ask sweeps -- the loss concentrator, measured at
        # -0.0234 a share against -0.0067 for a rung the ask merely reaches.
        anchor = post_only_anchor_v1(
            bid=book["bid"], ask=book["ask"], tick=book["tick"],
        )
        if anchor is None:
            legs[bucket] = {
                **entry,
                "action": LEG_ACTION_CANCEL if standing else LEG_ACTION_NONE,
                "reason": "no_anchor",
            }
            continue
        # Completion: cross to buy the leg the market would not sell us, but
        # only where the conditioned claim clears the ask, the completed
        # basket still costs under a share, and there is time for it to be
        # worth doing.
        # Two refusals stop this leg buying at any price, so they are read
        # before the resting rules rather than inside the crossing branch:
        # a basket that would cost a whole share can never pay, and past the
        # cutoff the market is pricing in an outcome we are on the wrong
        # side of.  Everything else only ever decides whether to CROSS --
        # declining to pay the ask is not a reason to stop offering to buy
        # the leg cheaply, and conflating the two cancels rungs that are
        # still the basket's best chance of completing.
        if standing and state != BASKET_STATE_EMPTY and gap[bucket] > EPS:
            if not in_completion_window:
                legs[bucket] = {**entry, "action": LEG_ACTION_CANCEL,
                                "reason": "past_completion_cutoff"}
                continue
            if bucket in conditioned and not cost_ceiling_ok:
                legs[bucket] = {**entry, "action": LEG_ACTION_CANCEL,
                                "reason": "completed_basket_costs_a_share"}
                continue
        verdict = conditioned.get(bucket)
        if (
            standing
            and age + EPS >= dwell_seconds
            and state != BASKET_STATE_EMPTY
            and verdict is not None
            and book["ask"] > EPS
            and verdict["buys"]
        ):
            legs[bucket] = {**entry, "action": LEG_ACTION_CROSS,
                            "reason": "completion_clears_the_ask"}
            continue
        if verdict is not None and not verdict["buys"]:
            # Recorded, not acted on: the market prices this leg below our
            # claim and legs in that zone realize 0.28 of what we claim, so
            # we will not pay its ask.  The rung stays where it is -- if the
            # market comes down to the anchor the leg is still wanted.
            entry["completion_declined"] = "market_discounts_this_leg"
        if not standing:
            legs[bucket] = {**entry, "action": LEG_ACTION_POST,
                            "reason": "no_rung_standing", "post_at": anchor}
            continue
        rung_action = resting_rung_action_v1(
            limit_price=limit, bid=book["bid"], ask=book["ask"],
            tick=book["tick"], age_seconds=age,
            dwell_seconds=dwell_seconds, far_ticks=far_ticks,
        )
        legs[bucket] = {
            **entry,
            "action": rung_action["action"],
            "reason": rung_action["reason"],
            **(
                {"post_at": rung_action["post_at"]}
                if "post_at" in rung_action else {}
            ),
            **(
                {"distance_ticks": rung_action["distance_ticks"]}
                if "distance_ticks" in rung_action else {}
            ),
        }

    return {
        "schema": COMPLETION_VERDICT_SCHEMA,
        "rule": COMPLETION_CLAIM_SHAPE_RULE_VERSION,
        "one_way_action_set": ONE_WAY_ACTION_SET,
        "window": list(ordered),
        "basket_state": state,
        "level_shares": level,
        "held_shares": held,
        "gap_shares": gap,
        "premium_paid_usdc": paid,
        "support_floor": floor,
        "uncovered_mass": uncovered_mass,
        "exposure_usdc": exposure,
        "completion": {
            "priced": completion_priced,
            "cost_usdc": completion_cost,
            "cost_after_usdc": cost_after,
            "per_share_cost_after": per_share_cost_after,
            "cost_ceiling_ok": cost_ceiling_ok,
            "edge_after": edge_after,
            "in_completion_window": in_completion_window,
            "cutoff_hours": cutoff,
            "hours_to_cutoff": (
                None if hours_to_cutoff is None else float(hours_to_cutoff)
            ),
        },
        "legs": legs,
    }



def support_scenario_floor_v1(
    *,
    shares_by_bucket: Mapping[str, Any],
    cost_by_bucket: Mapping[str, Any],
    support: Sequence[str],
    fraction: float | None = None,
) -> dict[str, Any]:
    """Charter 37's floor, readable on any basket shape (charter 93).

    The worst settlement inside ``support`` -- that rung's shares paying one
    each, less the whole premium ``cost_by_bucket`` sums to -- against
    ``fraction`` of that premium.  The solver reads the same inequality on
    its candidates; this is the form the lifted targets and the paper
    ledger's lifted legs are judged by, so the two cannot drift.
    """

    frac = (
        SUPPORT_SCENARIO_FLOOR_FRACTION if fraction is None else float(fraction)
    )
    cost = sum(float(value or 0.0) for value in cost_by_bucket.values())
    rungs = [str(bucket) for bucket in support]
    worst = (
        min(
            float(shares_by_bucket.get(bucket, 0.0) or 0.0) - cost
            for bucket in rungs
        )
        if rungs else 0.0
    )
    floor = frac * cost
    return {
        "worst_in_support_pnl": worst,
        "cost": cost,
        "floor": floor,
        "fraction": frac,
        "legal": bool(worst >= floor - EPS),
    }
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
# Charter 96 (user ruling, 2026-09-06): the partition is cut at 0.80 and 0.95.
# The gate's admission test reduces to ``lambda(a) > a``, so a band admits
# only ``[its lower bound, its own lambda)`` -- which made the served value
# of the top band its own cut point.  Measured on the 28-day book, that cut
# fell inside the band and on the wrong side: the 32 seats it admitted
# realised 0.864 against the 33 it refused at 0.909.  Split at 0.95 the two
# halves separate on their own evidence (22 seats 0.826, 18 seats 1.011) and
# the boundary is a constant rather than the number the fit is producing.
#
# The same pathology one band lower, measured by a second session and
# re-derived here through ``fit_lambda_table`` itself rather than its
# scripts: (0.70, 0.85] averages a half that clears its own floor with one
# that does not -- 20 seats at 0.795 against 10 at 0.471 -- into 0.686,
# which is below 0.70, so the whole band goes blind.  Cut at 0.80 the lower
# half admits [0.700, 0.795) on 20 seats and 12.583 of claim mass, over
# both of charter 68's hurdles; the upper half keeps its incumbent on ten
# seats and stays shut, which is what the evidence there says.
EFFECTIVE_EDGE_BAND_BOUNDS = (
    0.50, 0.70, 0.80, 0.85, 0.95, 1.00, 1.10, float("inf"),
)
# The bounds above 0.95 (user ruling, 2026-09-07: "lambda 顶档拆细").  The
# open top band was measured on the enumerated windows and found internally
# heterogeneous in exactly the way that makes a band average unusable as a
# threshold: lambda tracks ``a`` almost one for one inside it -- sub-bands
# (0.95,1.00] / (1.00,1.10] / (1.10,1.25] / (1.25,1.50] / (1.50,2.00] read
# 0.985 / 1.031 / 1.154 / 1.321 / 1.486 against their own mean agreements of
# roughly 0.98 / 1.05 / 1.17 / 1.37 / 1.70.  ``lambda - a`` is therefore
# positive only below about 1.00, and the pooled 1.217 the whole band fitted
# to would have admitted the 1.00-1.22 slice, whose windows each returned
# -2.3% and -2.7% when bought.  Splitting is what makes the band average
# mean something again; the split itself moves nothing, because
# ``_project_onto_partition`` gives every new band the value of the old one
# that contained it.
EFFECTIVE_EDGE_LAMBDA = (
    (0.50, 0.14),
    (0.70, 0.60),
    (0.80, 0.87),
    (0.85, 0.87),
    (0.95, 0.82),
    (1.00, 0.82),
    (1.10, 0.82),
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
    (0.80, 0.87),
    (0.85, 0.87),
    (0.95, 0.82),
    (1.00, 0.82),
    (1.10, 0.82),
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
    (0.80, 1.00),
    (0.85, 1.00),
    (0.95, 1.00),
    (1.00, 1.00),
    (1.10, 1.00),
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
# The open top band, split (2026-09-09).  Charter 96 split the WINDOW table
# here at 0.95/1.00/1.10 because an open band above 0.85 is internally
# heterogeneous in exactly the way that makes its average unusable as a
# threshold: lambda tracks ``a`` almost one for one inside it, so a pooled
# value admits the slice where ``lambda - a`` has already gone negative.  The
# bucket table was left un-split and its pooled value is 1.38 -- the only
# band in it above one, and therefore the only one that INFLATES a claim.
#
# Measured 2026-09-09 on the 2026-08-28..09-08 replay, running the bucket arm
# against the shipped window arm: it opened 33 station-days the window scope
# refused, and those 33 returned -19.77% while the 57 it refused in exchange
# returned +5.69%.  68.1% of the rungs in the newly-opened losers sit in this
# top band, against 50.4% in the ones it turned down.  The whole -59.44 USDC
# of the arm is that swap; on the 112 seats both books take it is a wash and
# slightly favours the arm.
#
# Splitting moves nothing on its own -- ``_project_onto_partition`` gives
# every new band the stored value of the old band that contained it, so all
# four of these read 1.38 until each fits itself from its own legs.  What it
# buys is that they CAN: an open band cannot be told apart from its own tail.
EFFECTIVE_EDGE_BUCKET_LAMBDA = (
    (0.50, 0.22),
    (0.70, 0.43),
    (0.85, 0.80),
    (0.95, 1.38),
    (1.00, 1.38),
    (1.10, 1.38),
    (float("inf"), 1.38),
)
EFFECTIVE_EDGE_SCOPES = ("window", "bucket")
# Where the bucket lane's fitted table travels in the served-tables
# mapping.  Not a stage, so a reader iterating stages cannot pick it up
# by accident; here in the kernel because both the store that writes it
# and the runtime that reads it must name the same string, and the
# runtime must not import the store.
BUCKET_TABLE_KEY = "bucket"
# Charter 128 (2026-09-12): the beta lane rides in the same served-tables
# mapping under a key no stage can collide with.  Its value per stage is a
# dict (``beta``, ``beta_lcb``, ``sha256``, ``accumulating``), not a band
# table; readers that iterate the mapping as band tables must skip it.
BETA_TABLE_KEY = "beta"
# ``off``: the lane is not read.  ``shadow`` (default): every priced window
# records the beta belief, edge and verdict and nothing gates on them.
# ``on``: the gate and the solver read ``q + beta_lcb * (p - q)`` instead of
# the lifted-claim-times-lambda belief; a stage with no fit reads
# ``beta_lcb = 0``, which is the belief collapsing onto the price and the
# gate refusing -- the honest reading of "no evidence yet".
BETA_EDGE_MODE = (
    os.environ.get("PM_BETA_EDGE", "").strip().lower() or "shadow"
)
if BETA_EDGE_MODE not in {"off", "shadow", "on"}:
    raise RuntimeError(f"unknown beta edge mode: {BETA_EDGE_MODE}")
# v2 (user ruling, 2026-09-14: "改"): the disagreement is measured against
# the market's BELIEF -- the normalised mid of the whole ladder, the same
# vector charter 132 reads -- and the cost is charged at the all-in ask by
# the gate, separately.  On the v1 basis ``q`` was the all-in ask, so the
# regressor ``p - q`` carried half a spread plus the fee and a spread that
# varied across windows read as realised disagreement: with zero information
# the slope is ``Var(d) / (Var(x) + Var(d))``, not 0.  Measured 2026-09-14
# on identical rows (leg level, 28 days): the ask basis read beta 0.016
# (P0) and 0.033 (P1) higher than the mid basis, with the intercept
# absorbing the mean spread.  The ledger hashes this string, so the book
# re-imports under the new basis and every candidate window carries the mid.
BETA_EDGE_RULE_VERSION = "beta_lcb_times_disagreement_mid_v2"
BETA_PRICE_BASIS = "market_mid_reference_v1"
# The second axis of the fit (same ruling): hours from the decision to the
# settlement cutoff.  The disagreement realises differently through the day
# -- measured 2026-09-14, P1's k = Cov/Var runs 0.99 -> 0.95 -> 0.97 from the
# first third of a station-day's decisions to the last, and P0 sits at 1.01
# throughout -- so a stage pools bands it should not.  A band serves only
# when it clears the lane's own hurdles; otherwise the stage's whole row does.
BETA_LEAD_BANDS: tuple[tuple[float, float, str], ...] = (
    (0.0, 6.0, "<6h"),
    (6.0, 12.0, "6-12h"),
    (12.0, 24.0, "12-24h"),
    (24.0, 36.0, "24-36h"),
    (36.0, float("inf"), ">=36h"),
)


def beta_lead_band(hours_to_cutoff: Any) -> str | None:
    """The lead band a decision falls in, or None without a clock."""

    try:
        hours = float(hours_to_cutoff)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(hours):
        return None
    hours = max(0.0, hours)
    for low, high, label in BETA_LEAD_BANDS:
        if low <= hours < high:
            return label
    return BETA_LEAD_BANDS[-1][2]


def beta_belief_v1(raw_win: float, market_mid: float, beta_lcb: float) -> float:
    """Charter 128 (v2 basis): ``m + beta_lcb * (p - m)``, clipped to a
    probability.

    ``p`` is the raw served claim, ``m`` the market's belief for the window
    (the normalised mid mass, ``BETA_PRICE_BASIS``), ``beta_lcb`` the lower
    bound of the share of the disagreement that arrives.  At ``beta_lcb = 0``
    this is the market's belief and the disagreement is worth nothing; at 1
    it is the raw claim.  The cost -- the all-in ask -- is not in here: the
    gate charges it when it compares this belief with what the window costs.
    """

    belief = float(market_mid) + float(beta_lcb) * (float(raw_win) - float(market_mid))
    return min(1.0 - EPS, max(EPS, belief))


def beta_conditional_edge_v2(
    raw_win: float,
    market_mid: float,
    joint_draws: Sequence[Sequence[float]],
    *,
    quantile: float = 0.05,
) -> dict[str, float] | None:
    """Charter 132: the conditional net edge ``alpha + beta * (p - m)``,
    point and lower bound, read on the fit's joint draws.  ``m`` is the
    market's belief (v2 basis), so the prediction is of ``hit - m``; a caller
    comparing it with a cost adds ``m`` back first.

    Three things charter 128's ``q + beta_lcb * (p - q)`` gets wrong, each a
    counterexample in the 2026-09-13 audit and each a test in
    ``test_charter_132_evidence_contract``:

    * **The intercept is dropped.**  On this lane alpha is about -0.04 -- the
      spread above the price the book would clear at.  p=.8, q=.6, beta=.3,
      alpha=-.08: the served edge is +.06 and clears 5%; the fitted edge is
      -.02.
    * **The bound is taken on beta's marginal.**  alpha and beta come from one
      sample and are correlated; the bound belongs to the prediction.
    * **A negative disagreement flips it.**  d=-.2 with beta in [-.8, .4]:
      ``beta_lcb * d`` is +.16, while the worst prediction on the interval is
      -.08.  A quantile over ``alpha_k + beta_k * d`` has no sign to get wrong.

    The number is a net edge per unit of cost basis, not a probability, and is
    named so on every row that records it.  None when there are no draws --
    an accumulating fit has none, and nothing may be served from it.
    """

    pairs = [
        (float(pair[0]), float(pair[1]))
        for pair in joint_draws
        if len(pair) == 2
        and math.isfinite(float(pair[0]))
        and math.isfinite(float(pair[1]))
    ]
    if not pairs:
        return None
    d = float(raw_win) - float(market_mid)
    values = sorted(alpha + beta * d for alpha, beta in pairs)
    q = min(max(float(quantile), 0.0), 1.0)
    lower = values[min(len(values) - 1, int(q * len(values)))]
    upper = values[max(0, int((1.0 - q) * len(values)) - 1)]
    point = sum(values) / len(values)
    return {
        "disagreement": d,
        "edge_point": point,
        "edge_lcb": lower,
        "edge_ucb": upper,
        "draws": float(len(values)),
    }


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


# Charter 97 (user ruling, 2026-09-06, relayed via the 9/8 book session):
# the claim-edge thermometer stops being a monitor and serves.  Measured on
# the same 89 settled P0 seats the lambda table is fitted on, banded by the
# claim edge ``e = lifted claim - all-in price`` rather than by agreement:
#
#     (0, 0.05]   20 seats   realised 1.036   ROI +6.52%
#     (0.05,0.10] 17 seats            0.731       -17.96%
#     (0.10,0.20] 35 seats            0.674       -15.48%
#     (0.20, inf) 17 seats            0.304       -40.27%
#
# The ruling was written as a second admission path -- admit when
# ``lambda(a) > a`` OR when ``0 < e <= 0.05``.  Implemented literally that is
# inert and would have shipped as a silent no-op: under charter 96 P0 sizes
# on the deflated claim, so a window admitted with a negative deflated edge
# yields a zero Kelly stake, and the ticket that used to catch that case was
# retired in the same charter.  Both seats the ruling's own backtest rests on
# -- SBGR 2026-08-27 and ZGGG 2026-09-04 -- size to exactly zero that way.
# That is the shape charter 87 exists to remove: "闸放行、Kelly 按 λ 归零、HOLD".
#
# So the thermometer is served as what it actually is: a second axis of the
# same lambda, not a second gate.  Inside the thin band the served value is
# the thermometer's own measurement, and admission stays ``lambda > a``.  The
# admission SET is identical to the ruling as written -- in the thin band
# ``lambda = 1 > a`` reduces to ``price < claim``, which is ``e > 0``, the
# ruling's own condition -- and the difference is only that a window admitted
# this way can now be sized, which is what the backtest assumed all along.
#
# Capped at one rather than served at the measured 1.036, for the reason
# charter 68 caps P1: trust the observation, never more than it trusts
# itself.  A band that over-delivers is not a licence to buy above the claim.
CLAIM_EDGE_THERMOMETER_MODE = (
    os.environ.get("PM_CLAIM_EDGE_THERMOMETER", "").strip().lower() or "on"
)
if CLAIM_EDGE_THERMOMETER_MODE not in {"on", "off"}:
    raise RuntimeError(
        f"unknown claim edge thermometer mode: {CLAIM_EDGE_THERMOMETER_MODE}"
    )
# The thin band's upper edge, and what it serves inside it.  Boundary in the
# kernel like the agreement partition (charter 96); unlike that partition the
# VALUE here is a constant too, because no fit lane measures this axis yet --
# see charter 97's "未建" note.
CLAIM_EDGE_THIN_BAND_MAX = 0.05
CLAIM_EDGE_THIN_BAND_LAMBDA = 1.00
LAMBDA_SOURCE_AGREEMENT = "agreement_band"
LAMBDA_SOURCE_CLAIM_EDGE = "claim_edge_thin_band"


def effective_edge_lambda_resolved(
    stage: str,
    *,
    claim: float,
    price: float,
    tables: "Mapping[str, Sequence[tuple[float, float]] | None] | None",
) -> tuple[float, str]:
    """The served lambda and which axis produced it.

    Charter 97: the claim-edge axis wins inside its thin band; everywhere
    else the agreement axis is the whole rule, exactly as charter 68 left
    it.  P1 is unaffected in practice -- its table is identity, so both
    axes serve one.

    Returns ``(lambda, source)`` so the gate can record which axis bought
    the window; without that, the seats this path opens are unattributable
    and the retirement condition charter 97 sets could never be evaluated.
    """

    claim = float(claim)
    price = float(price)
    agreement_lambda = effective_edge_lambda_from_tables(
        stage, (price / claim) if claim > 0.0 else 0.0, tables
    )
    if CLAIM_EDGE_THERMOMETER_MODE != "on" or claim <= 0.0:
        return agreement_lambda, LAMBDA_SOURCE_AGREEMENT
    edge = claim - price
    if 0.0 < edge <= CLAIM_EDGE_THIN_BAND_MAX:
        # Never below what the agreement axis already grants: the thermometer
        # is evidence that this claim arrives, so it may lift a band the
        # agreement axis deflated -- it may not deflate one the agreement
        # axis trusted more.
        if CLAIM_EDGE_THIN_BAND_LAMBDA > agreement_lambda:
            return float(CLAIM_EDGE_THIN_BAND_LAMBDA), LAMBDA_SOURCE_CLAIM_EDGE
    return agreement_lambda, LAMBDA_SOURCE_AGREEMENT


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
    bucket_table: Sequence[tuple[float, float]] | None = None,
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
        # The store's fitted table when one has been activated, the kernel
        # constant before that -- the same relationship the window path has
        # had since charter 68(c).  Until 2026-09-09 there was no bucket lane
        # at all, so this read a constant hand-set on 2026-09-03 while the
        # window path read a nightly refit, and the two arms were never
        # comparable in the way "same rule, different scope" implies.
        return effective_edge_lambda(
            agreement,
            tuple(bucket_table) if bucket_table
            else EFFECTIVE_EDGE_BUCKET_LAMBDA,
        )
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
DECISION_MOMENT_RULE_VERSION = "checkpoint_or_admissibility_or_holding_v1"
# Charter 135 (一) (user ruling, 2026-09-14: "撤单后不会重挂：策略重新决策"): an
# order that ended without filling -- a cancel acknowledged, a GTD expiry, a
# killed FOK -- is a moment of its own, and what runs at it is the whole
# strategy decision, not a re-price of the old seat.  Until then nothing
# re-posted a cancelled rung: the resting lane cancels and places nothing, and
# the next compile asked only about the model, the day's admissibility and
# fills.  Measured 2026-09-14 over 195 cancels ahead of their event day since
# go-live: the same bucket was re-quoted within an hour 37 times, and on 100 of
# those scopes no order of any kind was ever minted again.
#
# Named apart from DECISION_MOMENT_RULE_VERSION, which the paper ledger hashes:
# the ledger has no counterpart event to feed this arm -- its resting walk
# re-posts inside the seat every re-pricing round and withdraws on its own
# belief read -- so its behaviour does not change, and a hash that moved
# anyway would rebuild a book to the same numbers.
ORDER_END_MOMENT_RULE_VERSION = "unfilled_order_end_is_a_moment_v1"
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


def basket_holding_signature_v1(shares_by_bucket: Mapping[str, Any]) -> str:
    """What the chain HOLDS, as a value that changes only when a fill lands.

    Charter 110(七).  Composition and level, because both decide whether the
    basket is short: ``{"34": 5.0, "35": 2.0}`` reads ``34:5|35:2``.  The
    level is rounded to whole shares -- the venue's own unit -- so dust does
    not make every cycle a moment, and buckets are sorted so the string does
    not depend on dict order.

    HELD only, never pending.  Pending shares are our own unfilled orders,
    and letting them into this would make placing a rung a reason to
    re-solve, which is a loop rather than an event.
    """

    parts = []
    for bucket in sorted(str(key) for key in shares_by_bucket):
        try:
            shares = float(shares_by_bucket.get(bucket, 0.0) or 0.0)
        except (TypeError, ValueError):
            shares = 0.0
        if shares > EPS:
            parts.append(f"{bucket}:{int(round(shares))}")
    return "|".join(parts)


def is_decision_moment_v1(
    *,
    probability_identity: str | None,
    edge_state: str,
    last_probability_identity: str | None,
    last_edge_state: str | None,
    basket_holding: str | None = None,
    last_basket_holding: str | None = None,
    order_ends: str | None = None,
    last_order_ends: str | None = None,
) -> bool:
    """True when the model, the day's admissibility, or what the chain holds
    changed since the last recorded decision; always true with no recorded
    decision.

    The third arm is charter 110(七).  Until it existed, a fill was not an
    event: the solver was asked when the model moved or the day's
    admissibility flipped, and a leg landing did neither, so a basket that
    got one rung of three sat at one rung of three -- charter 84's balance
    candidate, which exists to raise the short rungs to the held level, was
    only ever built inside a compile that this rule had already refused to
    run.  Measured 2026-09-07: of 24 live baskets with any fill, 12 were
    whole; of the 8 three-leg baskets, 8 held one leg.

    ``basket_holding`` left None keeps the two-arm rule exactly, which is
    what a caller with no holdings view can honestly say.

    The fourth arm is charter 135 (一): ``order_ends`` is the store's count of
    the scope's orders that ended without filling (``order_end_marker_v1``),
    and a count that moved since the last recorded decision is a moment.
    Only ends count -- placing an order moves nothing here, so the arm cannot
    loop the way letting pending into the holding would.  Left None it is off,
    which is what a caller with no order store honestly says.
    """

    if not last_probability_identity:
        return True
    if str(probability_identity or "") != str(last_probability_identity):
        return True
    if str(edge_state) != str(last_edge_state or ""):
        return True
    if order_ends is not None and str(order_ends) != str(last_order_ends or ""):
        return True
    if basket_holding is None:
        return False
    return str(basket_holding) != str(last_basket_holding or "")


# The order events that end an order without it filling.  A refusal before
# the exchange (``NEVER_REACHED_EXCHANGE``, ``SUBMIT_REFUSED``) is deliberately
# not one: a barred station or a lowered live flag refuses every cycle, and
# counting those would make every cycle a moment that mints the same refused
# order again.
ORDER_END_EVENT_TYPES = ("CANCEL_ACK", "EXPIRED")


def order_end_marker_v1(count: int) -> str:
    """The fourth arm's signature value: how many orders ended unfilled.

    A count rather than a timestamp, because an order can be learned to have
    ended after a decision that was recorded later than the end itself (an
    expiry the venue stamps before the sync that reads it), and a clock
    comparison would lose exactly that one.  The event table is append-only,
    so the count only moves when a new end is written.
    """

    return f"ended:{int(count)}"


def whole_basket_kelly_stake_v1(
    *,
    win_probability: float,
    cost: float,
    book_equity_usdc: float,
    kelly_fraction: float | None = None,
    scope_fraction: float | None = None,
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

    if kelly_fraction is None:
        kelly_fraction = DEFAULT_KELLY_FRACTION
    if scope_fraction is None:
        scope_fraction = MAX_STAKE_FRACTION_PER_SCOPE
    cost = float(cost)
    if cost <= 0.0 or cost >= 1.0:
        return 0.0, 0.0
    full = (float(win_probability) - cost) / (1.0 - cost)
    if full <= 0.0:
        return 0.0, 0.0
    fraction = min(float(kelly_fraction) * full, float(scope_fraction))
    return fraction, max(0.0, fraction * float(book_equity_usdc))


CHARTER_116_COMPLETION_REPAIR = (
    os.environ.get("PM_CHARTER_116_COMPLETION_REPAIR", "").strip().lower() or "on"
)
if CHARTER_116_COMPLETION_REPAIR not in {"on", "off"}:
    raise RuntimeError(
        f"unknown charter 116 completion repair mode: {CHARTER_116_COMPLETION_REPAIR}"
    )
COMPLETION_REPAIR_RULE_VERSION = "completion_repairs_an_illegal_floor_v1"
COMPLETION_RISK_BUDGET_RULE_VERSION = "completion_exposure_against_edge_v2"
def completion_budget_usdc_v1(
    *,
    book_equity_usdc: float,
    stage: str,
    committed_usdc: float,
) -> float:
    """Charter 136 (三), as charter 141 rewrote it: what a completion may spend.

    The station-day's ceiling less what the chain has spent -- the same number
    every other thing that goes out is bounded by.  It was 1.5x the stage
    allowance, a second number for the same question, and a completion the
    5% hurdle admitted was sized against the smaller remainder while one the
    risk budget admitted got the larger: the stronger case bought less (EPWA
    2026-09-18, 22C refused at 1.44 against a 2.15 lot).  ``stage`` is unread.
    """

    del stage
    return station_day_ceiling_usdc(
        book_equity_usdc=float(book_equity_usdc),
        committed_usdc=float(committed_usdc),
    )


def completion_risk_budget_v1(
    *,
    window: Sequence[str],
    held_shares_by_bucket: Mapping[str, Any],
    held_cost_by_bucket: Mapping[str, Any],
    chain_cost_usdc: float,
    ask_by_bucket: Mapping[str, Any],
    depth_by_bucket: Mapping[str, Sequence[Any]],
    claim_by_bucket: Mapping[str, Any],
    budget_usdc: float,
    hours_to_cutoff: float | None,
    chain_seat_buckets: Sequence[str] = (),
    chain_held_buckets: Sequence[str] = (),
    dead_buckets: Sequence[str] = (),
    cutoff_hours: float | None = None,
    hurdle: float | None = None,
) -> dict[str, Any]:
    """Charter 136: complete a broken seat when its exposure outweighs the edge.

    User rulings 2026-09-15: "补单的负边际权重判断要小于篮子不完整的暴露风险",
    then "篮子不完整的风险暴露的成本、已建仓篮子的成本、整篮不命中的成本，要综合
    考虑，既要降低风险暴露，又不能放大风险暴露", then "落地".

    The three costs, on one book.  What the chain has paid is sunk: it is not
    in the completion's expectation, but it sets both risks.  A missing leg
    loses the whole chain when the truth lands on it -- the exposure; buying
    it adds its cash to what a truth outside the basket loses -- the miss.
    The miss is already inside the expectation (``delta_ev`` charges the cash
    on every outcome); the exposure is the change in the worst settlement
    inside the window (``delta_exposure``).  So:

    (一) belief: each missing leg at ``completion_conditioned_claim_v1`` --
         claim x 1.60 when the market prices it above the claim, x 0.28 when
         at or under it (2026-09-07, 2,575 missing legs);
    (二) ``delta_ev >= -hurdle * delta_exposure``: every unit of worst-case
         loss removed may cost at most ``hurdle`` of expectation, and a
         completion that removes none gets nothing from this rule;
    (三) after completion the worst settlement inside the window returns at
         least ``hurdle`` on the whole chain (charter 93's floor);
    (四) the spend stays inside ``budget_usdc`` (``completion_budget_usdc_v1``);
    (五) every leg bought is one the seat already wanted
         (``chain_seat_buckets``), so no relabel widens a broken seat, and the
         window holds every leg the chain holds (``chain_held_buckets``), so
         no narrower window reads the exposure off a subset of the chain;
    (六) more than ``cutoff_hours`` to settlement (charter 110 六).

    The level is charter 84's own (``ladder_balance_targets_v1``) at the
    budget, and each leg is walked down its book, so the numbers judged are
    the ones the completion would send.  Pure; ``admits`` is the verdict.
    """

    h = ECONOMIC_HURDLE_V1 if hurdle is None else float(hurdle)
    cutoff = COMPLETION_CUTOFF_HOURS if cutoff_hours is None else float(cutoff_hours)
    ordered = tuple(str(bucket) for bucket in window)
    dead = {str(bucket) for bucket in dead_buckets}
    held = {
        bucket: float(held_shares_by_bucket.get(bucket, 0.0) or 0.0)
        for bucket in ordered
    }
    held_cost = {
        bucket: float(held_cost_by_bucket.get(bucket, 0.0) or 0.0)
        for bucket in ordered
    }
    missing = [bucket for bucket in ordered if held[bucket] <= EPS]
    buyable = [bucket for bucket in missing if bucket not in dead]
    seat = {str(bucket) for bucket in chain_seat_buckets}
    reading: dict[str, Any] = {
        "rule": COMPLETION_RISK_BUDGET_RULE_VERSION,
        "hurdle": h,
        "cutoff_hours": cutoff,
        "hours_to_cutoff": hours_to_cutoff,
        "budget_usdc": float(budget_usdc),
        "chain_cost_usdc": float(chain_cost_usdc),
        "buyable_buckets": list(buyable),
        "chain_seat_buckets": sorted(seat),
    }

    def refuse(reason: str, **extra: Any) -> dict[str, Any]:
        return {**reading, **extra, "admits": False, "reason": reason}

    if not any(held[bucket] > EPS for bucket in ordered) or not buyable:
        return refuse("not_a_broken_seat")
    dropped = sorted(
        str(bucket) for bucket in chain_held_buckets if str(bucket) not in ordered
    )
    if dropped:
        return refuse("window_drops_a_held_leg", dropped_buckets=dropped)
    if seat and any(bucket not in seat for bucket in buyable):
        return refuse(
            "leg_outside_the_seat",
            outside_buckets=[b for b in buyable if b not in seat],
        )
    if hours_to_cutoff is not None and float(hours_to_cutoff) <= cutoff:
        return refuse("inside_completion_cutoff")
    asks = {}
    for bucket in ordered:
        value = ask_by_bucket.get(bucket)
        asks[bucket] = 0.0 if value is None else float(value)
    if any(asks[bucket] <= EPS for bucket in buyable):
        return refuse("short_leg_unpriced")
    level_inputs = dict(
        held_by_bucket=held,
        support=ordered,
        held_cost_by_bucket=held_cost,
        ask_by_bucket=asks,
        depth_by_bucket={
            bucket: tuple(depth_by_bucket.get(bucket) or ()) for bucket in ordered
        },
        below_leg_floor_buckets=tuple(sorted(dead)),
        fee_rate=0.0,
    )
    targets = ladder_balance_targets_v1(
        **level_inputs, capital_available_usdc=max(0.0, float(budget_usdc)),
    )
    if not targets:
        # Charter 84's level search already refuses a level whose completed
        # basket fails charter 93's floor, so an empty answer is one of two
        # things, and they have different repairs: the budget, or the book.
        unbounded = ladder_balance_targets_v1(
            **level_inputs, capital_available_usdc=None,
        )
        return refuse(
            "no_level_inside_the_budget" if unbounded
            else "no_floor_legal_level"
        )
    spend = 0.0
    payout = 0.0
    legs: dict[str, Any] = {}
    for bucket in buyable:
        increment = max(0.0, float(targets.get(bucket, 0.0)) - held[bucket])
        if increment <= EPS:
            continue
        walk = ask_depth_walk_v1(
            levels=tuple(depth_by_bucket.get(bucket) or ()), shares=increment,
        )
        if walk is None:
            return refuse("depth_short_of_the_level", short_bucket=bucket)
        belief = completion_conditioned_claim_v1(
            claim=float(claim_by_bucket.get(bucket, 0.0) or 0.0),
            ask=asks[bucket],
        )
        conditioned = min(1.0, float(belief["conditioned_claim"]))
        spend += float(walk["cash"])
        payout += increment * conditioned
        legs[bucket] = {
            "shares": increment,
            "cash": float(walk["cash"]),
            "claim": float(belief["claim"]),
            "ask": asks[bucket],
            "factor": float(belief["factor"]),
            "conditioned_claim": conditioned,
        }
    if not legs:
        return refuse("nothing_to_buy_at_the_level")
    after_shares = {
        bucket: max(held[bucket], float(targets.get(bucket, held[bucket])))
        for bucket in ordered
    }
    chain_cost = float(chain_cost_usdc)
    worst_before = min(held.values()) - chain_cost
    worst_after = min(after_shares.values()) - (chain_cost + spend)
    delta_ev = payout - spend
    delta_exposure = worst_after - worst_before
    allowed_give_up = h * max(0.0, delta_exposure)
    reading.update({
        "target_shares_by_bucket": after_shares,
        "legs": legs,
        "spend_usdc": spend,
        "expected_payout_usdc": payout,
        "delta_ev_usdc": delta_ev,
        "worst_in_window_before_usdc": worst_before,
        "worst_in_window_after_usdc": worst_after,
        "delta_exposure_usdc": delta_exposure,
        "allowed_give_up_usdc": allowed_give_up,
        "miss_loss_after_usdc": chain_cost + spend,
    })
    if spend > float(budget_usdc) + EPS:
        return refuse("spend_over_budget")
    if delta_exposure <= EPS:
        return refuse("removes_no_exposure")
    if worst_after + EPS < h * (chain_cost + spend):
        return refuse("floor_not_restored")
    if delta_ev + EPS < -allowed_give_up:
        return refuse("edge_given_up_exceeds_exposure_removed")
    return {**reading, "admits": True, "reason": "exposure_outweighs_edge"}


def completion_repair_exemption_v1(
    *,
    support: Sequence[str],
    held_shares: Mapping[str, Any],
    held_cost_usdc: Mapping[str, Any],
    target_shares: Mapping[str, Any],
    ask_by_bucket: Mapping[str, Any],
    claim_by_bucket: Mapping[str, Any] | None = None,
    fee_rate: float = 0.0,
) -> dict[str, Any]:
    """Charter 116: a completion that makes an illegal floor legal is a repair.

    Charter 93 forbids a basket that loses when the truth lands inside its own
    support, and exempts what the seat already holds -- holdings cannot be
    un-bought, so judging inherited shape by that floor would condemn the one
    answer always available.  The 2026-09-08 ruling closes the other half of
    that: the floor now also obliges the seat to *repair* an illegal holding,
    not merely to refrain from creating one.

    The reason it needs an exemption at all is that the two clauses read the
    action differently.  Charter 73/92 asks "is this buy worth its cash in
    expectation" and the answer for a missing leg is usually no -- the leg is
    missing because the book would not sell it at our price, which is the same
    region where ``claim < ask``.  Charter 93 asks "is the shape you are left
    holding legal", and buying that leg is the only action that changes the
    answer.  Both are right about their own question; the buy is not an
    opening, it is a repair, and an expectation test is the wrong question to
    put to a repair.

    Deliberately narrow, so this cannot become a way to buy anything:

    * the floor must be **illegal before** -- a legal seat has nothing to
      repair and is judged on its edge like every other buy;
    * it must be **legal after** -- a completion that leaves the seat illegal
      buys nothing this clause values;
    * every short rung must be **priced** -- charter 84 (八) is all-or-none;
    * and the level is charter 112's cheapest floor-legal held size, so the
      exemption can only ever pay for the smallest repair that works.

    Returns the reading either way; ``exempt`` is the verdict and
    ``floor_delta`` carries what it bought, so a refused repair is as
    auditable as an authorized one.
    """

    reading = completion_floor_delta_v1(
        support=support,
        held_shares=held_shares,
        held_cost_usdc=held_cost_usdc,
        target_shares=target_shares,
        ask_by_bucket=ask_by_bucket,
        # The charter 73/92 belief, so the repair reports what it costs in
        # EDGE beside what it buys in floor.  Optional here since 2026-09-08
        # and never supplied, so every exemption this clause granted
        # recorded ``increment_edge: null`` -- the exemption's whole premise
        # is that the increment prices negative while the floor gain is
        # real, and the number that would have shown it was the one not
        # being computed.  Measured 2026-09-09: seven exemptions granted,
        # all seven with a floor gain and none with an edge to weigh it
        # against.
        claim_by_bucket=claim_by_bucket,
        fee_rate=fee_rate,
    )
    exempt = bool(
        CHARTER_116_COMPLETION_REPAIR == "on"
        and reading["completion_priced"]
        and not reading["floor_legal_before"]
        and reading["floor_legal_after"]
    )
    return {
        "rule": COMPLETION_REPAIR_RULE_VERSION,
        "mode": CHARTER_116_COMPLETION_REPAIR,
        "exempt": exempt,
        "floor_delta": reading,
    }


COMPLETION_FLOOR_DELTA_SCHEMA = "completion_floor_delta_v1"


def completion_floor_delta_v1(
    *,
    support: Sequence[str],
    held_shares: Mapping[str, Any],
    held_cost_usdc: Mapping[str, Any],
    target_shares: Mapping[str, Any],
    ask_by_bucket: Mapping[str, Any],
    claim_by_bucket: Mapping[str, Any] | None = None,
    fee_rate: float = 0.0,
) -> dict[str, Any]:
    """What completing a broken seat buys, in both currencies at once.

    Charter 73/92 authorizes a buy on one number -- the increment's own
    whole-basket edge, ``sum(claim * added shares) - cash`` -- and charter 93
    judges the shape on another -- the worst settlement inside the support.
    A completion is the one action where the two habitually disagree: the leg
    that is missing is missing because the book would not sell it at our
    price, which is the same region where ``claim < ask``, so the increment
    prices negative; and buying it is what lifts the seat's worst in-support
    settlement off ``-premium``, which the increment does not price at all.

    Recorded, not gated (user ruling, 2026-09-08: "先只记账不进闸").  Nothing
    here refuses anything; it exists so the joint distribution of the two
    numbers can be measured on the book's own history before anyone argues
    for a floor exemption in charter 73/92.  ``floor_delta`` is the charter
    93 reading before and after, ``increment_edge`` the charter 73/92 one,
    and ``floor_per_edge_usdc`` how much floor a unit of forgone edge buys --
    ``None`` where the increment is not negative, because the trade only has
    a rate when there is something being traded away.
    """

    window = tuple(_text("delta_v2_floor_delta_bucket", b) for b in support)
    if len(set(window)) != len(window):
        raise ContractError("delta_v2_floor_delta_support_duplicate")
    if not window:
        raise ContractError("delta_v2_floor_delta_support_empty")
    fee = _nonnegative("delta_v2_floor_delta_fee", fee_rate)
    held = {
        b: _nonnegative("delta_v2_floor_delta_held", held_shares.get(b, 0.0) or 0.0)
        for b in window
    }
    paid = {
        b: _nonnegative("delta_v2_floor_delta_cost", held_cost_usdc.get(b, 0.0) or 0.0)
        for b in window
    }
    want = {
        b: _nonnegative("delta_v2_floor_delta_target", target_shares.get(b, 0.0) or 0.0)
        for b in window
    }
    ask = {
        b: _nonnegative("delta_v2_floor_delta_ask", ask_by_bucket.get(b, 0.0) or 0.0)
        for b in window
    }
    added = {b: max(0.0, want[b] - held[b]) for b in window}
    priced = all(ask[b] > EPS for b in window if added[b] > EPS)
    cash = sum(added[b] * ask[b] * (1.0 + fee) for b in window)
    after_shares = {b: max(held[b], want[b]) for b in window}
    after_cost = {b: paid[b] + added[b] * ask[b] * (1.0 + fee) for b in window}
    before = support_scenario_floor_v1(
        shares_by_bucket=held, cost_by_bucket=paid, support=window
    )
    after = support_scenario_floor_v1(
        shares_by_bucket=after_shares, cost_by_bucket=after_cost, support=window
    )
    floor_delta = (
        after["worst_in_support_pnl"] - before["worst_in_support_pnl"]
    )
    increment_edge: float | None = None
    if claim_by_bucket is not None:
        claim = {
            b: _finite(f"delta_v2_floor_delta_claim_{b}", claim_by_bucket.get(b, 0.0))
            for b in window
        }
        increment_edge = sum(claim[b] * added[b] for b in window) - cash
    rate: float | None = None
    if increment_edge is not None and increment_edge < -EPS:
        rate = floor_delta / -increment_edge
    identity = {
        "schema": COMPLETION_FLOOR_DELTA_SCHEMA,
        "support": list(window),
        "completion_priced": priced,
        "added_shares_by_bucket": added,
        "completion_cash_usdc": cash,
        "floor_before": before["worst_in_support_pnl"],
        "floor_after": after["worst_in_support_pnl"],
        "floor_delta": floor_delta,
        "floor_legal_before": before["legal"],
        "floor_legal_after": after["legal"],
        "increment_edge": increment_edge,
        "floor_per_edge_usdc": rate,
    }
    return {**identity, "content_sha256": content_sha256(identity)}



BASKET_RETURN_STRUCTURE_SCHEMA = "basket_return_structure_v1"
# The net return a basket is bought FOR (user ruling, 2026-09-09: "目标设置
# 为5%", then "把闸也上了").  Gated at the execution layer since 2026-09-09
# (``runtime.py``, blocker ``basket_net_roi_below_target``): read on the
# FINAL share vector at each leg's resting limit, charter-116 repairs exempt.
# This constant is the target; the kernel function below only reports it.
#
# Charter 130 (user ruling 2026-09-12, "5%"): there is ONE economic hurdle
# and four places it is evaluated -- the window at admission (one share of
# each leg, at the offer), the lifted bundle at execution (the shares that
# would go out, at the offer or the walked limit), a top-up's increment and
# a completion's increment.  Before this the admission gate cleared at zero
# (tick friction and margin, both zero), the execution target at 5%, and a
# completion at zero: the same expectation asked three times with three
# pass marks.  Measured 2026-09-09..12: 147 execution refusals across 48
# station-days of seats that had cleared admission, been sized and lifted --
# SBGR 2026-09-12 refused the same 22/23/24 window three cycles running at
# 1.7%, 0.1%, 1.7%, and the next window down was never asked.  The pass
# mark is an admission condition; it was being checked after admission.
#
# ``PM_ECONOMIC_HURDLE`` exists for one reason: a book seated under the old
# pass mark can be rebuilt (``PM_ECONOMIC_HURDLE=0`` is charter 58's gate to
# the epsilon) rather than argued about -- the same rule charter 129 set for
# the fill model.  The ledger hashes the value, so an arm cannot be mistaken
# for the served book.  It is not a tuning knob; the served value is 0.05.
_HURDLE_OVERRIDE = os.environ.get("PM_ECONOMIC_HURDLE", "").strip()
ECONOMIC_HURDLE_V1 = float(_HURDLE_OVERRIDE) if _HURDLE_OVERRIDE else 0.05
if not (0.0 <= ECONOMIC_HURDLE_V1 <= 0.5):
    raise RuntimeError(
        f"PM_ECONOMIC_HURDLE={_HURDLE_OVERRIDE!r} is outside [0, 0.5]"
    )
ECONOMIC_HURDLE_RULE_VERSION = "one_hurdle_four_evaluation_points_v1"


def clears_economic_hurdle_v1(
    *,
    expected_payout: float,
    cost: float,
    hurdle: float | None = None,
) -> bool:
    """``sum q_b n_b >= (1 + h) * C`` -- the one inequality, at every point.

    ``expected_payout`` is the belief-weighted payout of the action
    (one share of each leg at admission, the actual shares after the lift,
    the increment for a top-up or completion) and ``cost`` its all-in cost
    on the basis that point prices at.  Written in the form the net-ROI
    target already used, ``roi >= h - EPS``, so the execution reading is
    unchanged to the epsilon.  A costless action clears nothing: there is
    no return structure to judge.
    """

    if hurdle is None:
        # Read at call time, not bound at definition: the served constant
        # and a test's or an arm's override are the same name.
        hurdle = ECONOMIC_HURDLE_V1
    cost = float(cost)
    if not math.isfinite(cost) or cost <= EPS:
        return False
    return float(expected_payout) >= (1.0 + float(hurdle) - EPS) * cost


def economic_hurdle_usdc(cost: float) -> float:
    """The dollars an action's payout must clear above its cost: h * C."""

    return ECONOMIC_HURDLE_V1 * float(cost)


# The execution target's own name, kept for its readers; it IS the hurdle.
BASKET_TARGET_NET_ROI = ECONOMIC_HURDLE_V1


def basket_return_structure_v1(
    *,
    shares_by_bucket: Mapping[str, Any],
    price_by_bucket: Mapping[str, Any],
    claim_by_bucket: Mapping[str, Any],
    support: Sequence[str],
    fee_rate: float = 0.0,
    target_net_roi: float = BASKET_TARGET_NET_ROI,
) -> dict[str, Any]:
    """The odds structure of the basket that is actually bought.

    One share of bucket k pays 1 if the day settles there, so a basket
    holding ``n_k`` shares at an all-in premium of ``C`` returns ``n_k - C``
    in scenario k and its gross multiple is ``n_k / C`` -- a property of the
    WHOLE basket in that scenario, never the leg's own ``1 / a_k``.  A leg
    bought at four cents does not pay twenty-five times; the basket holding
    twenty-five of it pays ``25 / C``.

    Three quantities that an equal basket lets people conflate and an
    unequal one does not:

    * ``coverage`` -- the claim inside the window, ``sum p_k``.  What the
      market prices.
    * ``net_expected_value`` -- ``sum p_k n_k - C``.  What the basket is
      worth.  Only when every ``n_k`` is equal does this reduce to the
      familiar ``P - Q`` on one share of each.
    * ``net_profit_probability`` -- ``sum p_k [n_k > C]``.  How often the
      basket ends AHEAD.  A leg with ``n_k <= C`` loses money on the day it
      wins, and coverage counts that day as a hit.

    The gap between the last two is the whole reason to compute this.  Since
    charter 70/83 raise a sub-lot leg to ``ceil(1/price)`` shares, 96% of
    seated baskets are unequal (measured 2026-09-08), and the venue's dollar
    floor puts the MOST shares on the CHEAPEST leg -- which is the one the
    model believes least.  So an unequal basket can cover 95% of the claim
    and still hold a rung it loses on.

    ``equal_basket`` is the one-share-of-each reading kept beside it, where
    ``Q = sum price``, the gross multiple is ``1 / Q``, break-even is
    ``P > Q`` and a target ``r`` needs ``Q <= P / (1 + r)``.  It is reported
    so the two can be compared rather than substituted: it is the arithmetic
    everyone reasons in, and it describes the bought basket only when the
    lot left it alone.
    """

    window = tuple(_text("basket_return_bucket", b) for b in support)
    if len(set(window)) != len(window):
        raise ContractError("basket_return_support_duplicate")
    if not window:
        raise ContractError("basket_return_support_empty")
    fee = _nonnegative("basket_return_fee", fee_rate)
    roi_target = _finite("basket_return_target_roi", target_net_roi)
    shares = {
        b: _nonnegative("basket_return_shares", shares_by_bucket.get(b, 0.0) or 0.0)
        for b in window
    }
    price = {
        b: _nonnegative("basket_return_price", price_by_bucket.get(b, 0.0) or 0.0)
        for b in window
    }
    claim = {
        b: _finite(f"basket_return_claim_{b}", claim_by_bucket.get(b, 0.0) or 0.0)
        for b in window
    }
    cost = sum(shares[b] * price[b] * (1.0 + fee) for b in window)
    coverage = sum(claim[b] for b in window)
    expected_payout = sum(claim[b] * shares[b] for b in window)
    net_ev = expected_payout - cost
    # A basket that cost nothing has no return structure; say so rather than
    # divide.  Everything downstream reads None as "not applicable", which
    # is what an empty basket is.
    roi = (expected_payout / cost - 1.0) if cost > EPS else None
    scenario_multiple = {
        b: (shares[b] / cost if cost > EPS else None) for b in window
    }
    profitable = {b: bool(shares[b] > cost + EPS) for b in window}
    net_profit_probability = sum(
        claim[b] for b in window if profitable[b]
    )
    # One share of each leg: the reading the P - Q rule is true of.
    equal_q = sum(price[b] * (1.0 + fee) for b in window)
    equal = {
        "all_in_cost": equal_q,
        "coverage": coverage,
        "gross_multiple": (1.0 / equal_q if equal_q > EPS else None),
        "breaks_even": bool(coverage > equal_q + EPS),
        # Q <= P / (1 + r)
        "max_cost_for_target": (
            coverage / (1.0 + roi_target) if (1.0 + roi_target) > EPS else None
        ),
        "meets_target": clears_economic_hurdle_v1(
            expected_payout=coverage, cost=equal_q, hurdle=roi_target,
        ),
    }
    identity = {
        "schema": BASKET_RETURN_STRUCTURE_SCHEMA,
        "support": list(window),
        "shares_by_bucket": shares,
        "all_in_cost_usdc": cost,
        "coverage": coverage,
        "expected_payout_usdc": expected_payout,
        "net_expected_value_usdc": net_ev,
        "net_roi": roi,
        "target_net_roi": roi_target,
        "meets_target": clears_economic_hurdle_v1(
            expected_payout=expected_payout, cost=cost, hurdle=roi_target,
        ),
        "net_profit_probability": net_profit_probability,
        "loss_on_win_buckets": sorted(
            b for b in window if not profitable[b] and shares[b] > EPS
        ),
        "scenario_gross_multiple": scenario_multiple,
        "equal_basket": equal,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


LADDER_BALANCE_LEVEL_RULE_VERSION = "depth_priced_cheapest_floor_legal_held_level_v2"


def ask_depth_cost_v1(
    *,
    levels: Sequence[Any],
    shares: float,
) -> float | None:
    """Cash to buy ``shares`` walking an ask ladder, or ``None`` if it is short.

    The inverse of the cash-limited walk the candidates are built with, and it
    exists because the two directions were being mixed: charter 112's level
    search priced a completion at the top of book and the candidate then bought
    it with the cash that price implied, walking into whatever the book charged
    above the first level.  A rung offering one share at 0.15 and the rest at
    0.90 turned a certified five-share completion into 1.7 shares, and the
    floor then refused the under-completed basket the search had called legal
    (user ruling 2026-09-08: "B+A").

    ``levels`` are the ask ladder in book order; each carries ``price`` and
    ``shares`` (attributes or mapping keys).  ``None`` means the ladder cannot
    supply the quantity at all -- which is charter 84 (八)'s all-or-none read
    on depth, not a reason to buy what it can.
    """

    walk = ask_depth_walk_v1(levels=levels, shares=shares)
    return None if walk is None else float(walk["cash"])


ASK_DEPTH_WALK_SCHEMA = "ask_depth_walk_v1"


def ask_depth_walk_v1(
    *,
    levels: Sequence[Any],
    shares: float,
) -> dict[str, float] | None:
    """The same walk, reporting the price the last share had to reach.

    ``ask_depth_cost_v1`` answers "what does this cost"; a fill-or-kill order
    also has to state a limit, and the limit that fills is the price of the
    DEEPEST level the quantity consumes, not the top of book.  A take priced
    at the best offer for a quantity the best offer cannot supply is killed
    by construction -- the venue would have to trade through the limit to
    fill it -- so the two numbers come out of one walk rather than one being
    guessed from the other.

    ``None`` where the ladder cannot supply the quantity at all, which is
    charter 84 (八)'s all-or-none read on depth.
    """

    want = _nonnegative("delta_v2_depth_shares", shares)
    if want <= EPS:
        return {"cash": 0.0, "marginal_price": 0.0, "levels_consumed": 0.0}
    cash = 0.0
    marginal = 0.0
    consumed = 0
    for level in levels:
        price = float(
            level["price"] if isinstance(level, Mapping) else level.price
        )
        available = float(
            level["shares"] if isinstance(level, Mapping) else level.shares
        )
        if price <= EPS or available <= EPS:
            continue
        take = min(want, available)
        cash += take * price
        marginal = price
        consumed += 1
        want -= take
        if want <= EPS:
            return {
                "cash": cash,
                "marginal_price": marginal,
                "levels_consumed": float(consumed),
            }
    return None



def ladder_balance_targets_v1(
    *,
    held_by_bucket: Mapping[str, Any],
    support: Sequence[str],
    held_cost_by_bucket: Mapping[str, Any] | None = None,
    ask_by_bucket: Mapping[str, Any] | None = None,
    depth_by_bucket: Mapping[str, Sequence[Any]] | None = None,
    capital_available_usdc: float | None = None,
    below_leg_floor_buckets: Sequence[str] = (),
    fee_rate: float = 0.0,
) -> dict[str, float]:
    """Charter 84's shape: every short rung of the seat raised to a held level.

    ``held_by_bucket`` is what the chain holds (filled plus pending) per
    bucket; ``support`` is the seated window.  A rung below the level is a
    short rung and its target is the level, a rung at or above it keeps what
    it has.  Empty when the window holds nothing (there is no level to
    balance to) or nothing is short (the ladder is whole).  Sized to the held
    rungs, not to a new Kelly stake: the seat was sized when it opened, and
    completing it is restoring that seat, not resizing it.

    **Which held level** (user ruling, 2026-09-08).  Charter 84 shipped with
    "the most shares any rung holds", and that rung is usually the one the
    venue lifted: charter 70/83 raise a sub-lot leg to a whole lot or a
    dollar, so the tallest rung is an artefact of the exchange minimum, not
    of what the objective wanted.  Completing to it buys the dear rungs at
    the cheap rung's share count.  Measured on the 2026-09-08 book, four
    seats read (whole-basket edge after completing to the tallest rung ->
    to the shortest): KMIA +0.162 -> +0.162, SAEZ **-1.130 -> +1.205**,
    KAUS -5.772 -> -2.848, KSFO -3.876 -> -2.221, at 35% to 50% of the cash.
    Never worse, once decisive, and SAEZ crosses zero -- a completion the
    tallest-rung level said not to make.

    So the level is the **cheapest held rung size the completed basket is
    still legal at**: the distinct held sizes are tried smallest first and
    the first one whose completed shape clears charter 93's in-support floor
    wins.  KAUS is why the search does not simply stop at the smallest --
    completing it to 5 costs 5.256 against 5 shares and settles under water
    (-0.256), so the level walks up to 11 and reproduces the shipped answer
    exactly.  ``{}`` when no held size is legal: buying nothing is always
    available and a basket that cannot be completed legally is not one.

    **What the search prices with** (user ruling 2026-09-08: "B+A").  Give it
    ``depth_by_bucket`` -- the ask ladder per bucket -- and each gap is priced
    by walking that ladder, which is what the candidate will actually pay.
    Priced at ``ask_by_bucket`` alone the search certifies a level the book
    cannot deliver: measured 2026-09-06..08, 45 of 78 offered completions were
    refused by the very floor this search had just cleared them against, and
    the reproduction is a rung offering one share at 0.15 and the rest at
    0.90.  A ladder that cannot supply the gap ends the search -- the gap only
    grows with the level, so no larger level can be supplied either.

    ``capital_available_usdc`` bounds it the same way and for the same reason:
    a level the cash cannot buy whole would be bought in part, and a part is
    the under-completed basket again.  Charter 84 (八) is all-or-none.

    ``below_leg_floor_buckets`` are charter 84 (八)'s dead rungs -- rungs the
    MARKET prices under the leg floor, "not bought, and not a reason to leave
    its neighbours short".  Nobody can buy them at a price we may pay, so the
    search must not charge for them: once the cash bound existed, pricing a
    rung nobody can buy could refuse a level the seat could afford.  Their
    target still rises with the level, because the shape the floor judges is
    the ladder, not the purchase.

    NAMED AFTER ITS CRITERION, and renamed to it on 2026-09-09.  It was
    ``unbuyable_buckets``, one word away from "the ones we are not buying",
    and that is exactly how it was read: charter 110(四)'s refusal -- legs the
    market offers cheap and we DECLINE -- was routed here, which excused
    those legs from the cash bound and from the floor's cost at once.  Three
    quarters of short legs were refused and the book's total stake barely
    moved (865 to 857) because the level rose on the money the refusal had
    notionally freed; twelve station-days were staked higher and carried
    -21.70 of a -39.65 swing.  A leg we decline is buyable and must keep
    paying for itself in both bounds, so it does not belong in this set --
    and under a name that states the market's verdict rather than ours, it
    cannot be put here by mistake.

    Pricing the search needs ``held_cost_by_bucket`` and one of
    ``depth_by_bucket`` / ``ask_by_bucket``; without them the level is the
    shipped ``max`` and the floor is left to the caller's after-lift recheck,
    which is what every pre-ruling caller gets.
    """

    window = tuple(_text("delta_v2_balance_bucket", bucket) for bucket in support)
    if len(set(window)) != len(window):
        raise ContractError("delta_v2_balance_support_duplicate")
    held = {
        bucket: _nonnegative("delta_v2_balance_held", held_by_bucket.get(bucket, 0.0) or 0.0)
        for bucket in window
    }
    if max(held.values(), default=0.0) <= EPS:
        return {}

    def _targets(level: float) -> dict[str, float] | None:
        short = {b for b, shares in held.items() if shares + EPS < level}
        if not short:
            return None
        return {b: (level if b in short else held[b]) for b in window}

    if held_cost_by_bucket is None or (
        ask_by_bucket is None and depth_by_bucket is None
    ):
        return _targets(max(held.values())) or {}

    fee = _nonnegative("delta_v2_balance_fee_rate", fee_rate)
    paid = {
        b: _nonnegative("delta_v2_balance_cost", held_cost_by_bucket.get(b, 0.0) or 0.0)
        for b in window
    }
    ask = {
        b: _nonnegative(
            "delta_v2_balance_ask", (ask_by_bucket or {}).get(b, 0.0) or 0.0
        )
        for b in window
    }
    dead = {
        _text("delta_v2_balance_unbuyable", bucket)
        for bucket in below_leg_floor_buckets
    }
    budget = (
        None if capital_available_usdc is None
        else _nonnegative(
            "delta_v2_balance_capital", capital_available_usdc
        )
    )
    for level in sorted({shares for shares in held.values() if shares > EPS}):
        targets = _targets(level)
        if targets is None:
            continue
        cost = dict(paid)
        spend = 0.0
        priced = True
        for bucket, want in targets.items():
            gap = want - held[bucket]
            if gap <= EPS or bucket in dead:
                continue
            if depth_by_bucket is not None:
                gap_cash = ask_depth_cost_v1(
                    levels=depth_by_bucket.get(bucket) or (), shares=gap
                )
            elif ask[bucket] > EPS:
                gap_cash = gap * ask[bucket]
            else:
                gap_cash = None
            if gap_cash is None:
                # A short rung the book cannot supply at this level cannot be
                # supplied at any larger one either -- the gap only grows --
                # and charter 84 (八) is all-or-none, so the search is done.
                priced = False
                break
            gap_cash *= 1.0 + fee
            cost[bucket] += gap_cash
            spend += gap_cash
        if not priced:
            return {}
        if budget is not None and spend > budget + EPS:
            # Too dear at this level, and dearer at every larger one.  A
            # completion that does not fit whole is not scaled: a part of a
            # completion is the short ladder it was meant to repair.
            return {}
        verdict = support_scenario_floor_v1(
            shares_by_bucket=targets, cost_by_bucket=cost, support=window
        )
        if verdict["legal"]:
            return targets
    return {}


# Charter 143 (user ruling 2026-09-17: "改").  A broken basket is completed by
# how many legs it already holds, not by whether the missing legs pay for
# themselves or whether the completed shape clears charter 93's 5%.
#
# Measured on the live book's broken station-days, 2026-09-06..16 (52 of
# them; production decision facts x settled truth, each action priced on
# that decision's own ask ladder, bootstrap over station-days): holding
# realised -0.66 a station-day; completing multi-leg baskets in full and
# single-leg baskets on their cheap legs only realised +0.45 (delta +1.12,
# 90% [+0.20, +2.14]); P(loss) 71% -> 52%, worst in-window settlement
# -2.47 -> -0.89, the loss when truth lands outside the window -2.66 ->
# -4.32 (the price of it), and the 5% floor was reachable on 4% -> 35%.
# Single-leg cheap-only carried +1.75; multi-leg full +0.57.  Buying the
# whole window for a single leg was worse than either.
#
# So: two or more held legs -> every buyable missing leg to the cheapest held
# size, provided the worst in-window settlement improves (charter 93's 5% is
# recorded, and binds nothing here); one held leg -> only the missing legs
# that clear the one hurdle (charter 130) on their own walked cost, at the
# held size.  Neither adds to a held leg: charter 112's walk up to a taller
# floor-legal level tops up the shorter held rungs, which is a top-up, not a
# completion, and not what was measured (RKSI 2026-09-18 held 27C x9 and
# 28C x5; the legal level was 9, raising 28C).  Neither buys past the
# completion cutoff (charter 110 (六)'s three hours) or past the station-day
# ceiling it is handed.  ``PM_CHARTER_143_COMPLETION=off`` is the rollback.
CHARTER_143_COMPLETION = (
    os.environ.get("PM_CHARTER_143_COMPLETION", "").strip().lower() or "on"
)
if CHARTER_143_COMPLETION not in {"on", "off"}:
    raise RuntimeError(
        f"unknown charter 143 completion mode: {CHARTER_143_COMPLETION}"
    )
COMPLETION_143_RULE_VERSION = "broken_basket_completion_by_held_count_v1"
COMPLETION_143_MULTI_HELD_LEGS = 2


def completion_143_basket_type_v1(held_legs: int) -> str | None:
    """``multi`` from two held legs, ``single`` at one, ``None`` otherwise."""

    if int(held_legs) >= COMPLETION_143_MULTI_HELD_LEGS:
        return "multi"
    if int(held_legs) == 1:
        return "single"
    return None


def completion_143_targets_v1(
    *,
    held_by_bucket: Mapping[str, Any],
    support: Sequence[str],
    held_cost_by_bucket: Mapping[str, Any],
    depth_by_bucket: Mapping[str, Sequence[Any]],
    capital_available_usdc: float,
    claim_by_bucket: Mapping[str, Any] | None = None,
    below_leg_floor_buckets: Sequence[str] = (),
    hours_to_cutoff: float | None = None,
    fee_rate: float = 0.0,
) -> dict[str, Any]:
    """Charter 143's completion of a broken window, or the reason there is none.

    ``targets`` is the whole window's share map after the completion (a rung
    not bought keeps what it holds; charter 84 (八)'s dead rungs are never
    bought and sit outside the in-window reading), empty when refused, and
    ``refused`` names why.  The claim is the seated charter-68 partition --
    the belief the execution layer authorizes on -- and only a single-leg
    basket reads it.  Every gap is priced by walking the ask ladder it will
    be bought down, so the cost the rule judges is the cost the candidate
    pays.
    """

    window = tuple(_text("delta_v2_c143_bucket", bucket) for bucket in support)
    if len(set(window)) != len(window):
        raise ContractError("delta_v2_c143_support_duplicate")
    fee = _nonnegative("delta_v2_c143_fee_rate", fee_rate)
    dead = {
        _text("delta_v2_c143_dead", bucket) for bucket in below_leg_floor_buckets
    }
    held = {
        b: _nonnegative("delta_v2_c143_held", held_by_bucket.get(b, 0.0) or 0.0)
        for b in window
    }
    paid = {
        b: _nonnegative(
            "delta_v2_c143_cost", held_cost_by_bucket.get(b, 0.0) or 0.0
        )
        for b in window
    }
    live = tuple(b for b in window if b not in dead)
    held_legs = [b for b in live if held[b] > EPS]
    missing = [b for b in live if held[b] <= EPS]
    basket_type = completion_143_basket_type_v1(len(held_legs))
    base: dict[str, Any] = {
        "rule": COMPLETION_143_RULE_VERSION,
        "mode": CHARTER_143_COMPLETION,
        "basket_type": basket_type,
        "held_buckets": held_legs,
        "missing_buckets": missing,
        "targets": {},
        "buy_buckets": [],
        "level_shares": None,
        "level_rule": None,
        "spend_usdc": 0.0,
        "floor_before": None,
        "floor_after": None,
        "legs": {},
        "refused": None,
    }
    if CHARTER_143_COMPLETION != "on":
        return {**base, "refused": "off"}
    if basket_type is None or not missing:
        return {**base, "refused": "not_broken"}
    if (
        hours_to_cutoff is not None
        and float(hours_to_cutoff) <= COMPLETION_CUTOFF_HOURS
    ):
        return {**base, "refused": "past_completion_cutoff"}
    budget = _nonnegative("delta_v2_c143_capital", capital_available_usdc)
    before = support_scenario_floor_v1(
        shares_by_bucket=held, cost_by_bucket=paid, support=live
    )
    base["floor_before"] = before["worst_in_support_pnl"]

    def _walk(bucket: str, shares: float) -> float | None:
        cash = ask_depth_cost_v1(
            levels=depth_by_bucket.get(bucket) or (), shares=shares
        )
        return None if cash is None or cash <= EPS else cash * (1.0 + fee)

    def _accept(targets: dict[str, float], buy: Sequence[str], level: float,
                level_rule: str, spend: float, cost: Mapping[str, float],
                legs: Mapping[str, Any]) -> dict[str, Any]:
        after = support_scenario_floor_v1(
            shares_by_bucket=targets, cost_by_bucket=cost, support=live
        )
        return {
            **base,
            "targets": targets,
            "buy_buckets": list(buy),
            "level_shares": float(level),
            "level_rule": level_rule,
            "spend_usdc": float(spend),
            "floor_after": after["worst_in_support_pnl"],
            "floor_legal_after": bool(after["legal"]),
            "legs": dict(legs),
        }

    if basket_type == "multi":
        # The cheapest held size: no held rung is below it, so only the
        # missing rungs are short.
        level = min(held[b] for b in held_legs)
        short = [b for b in live if held[b] + EPS < level]
        level_rule = "cheapest_held_level_worst_improves"
        targets = {
            b: (level if b in short else held[b]) for b in window
        }
        cost = dict(paid)
        spend = 0.0
        legs: dict[str, Any] = {}
        for bucket in short:
            cash = _walk(bucket, level - held[bucket])
            if cash is None:
                return {**base, "refused": "unpriced"}
            cost[bucket] += cash
            spend += cash
            legs[bucket] = {"shares": level - held[bucket], "cash": cash}
        if spend > budget + EPS:
            return {**base, "refused": "capital"}
        accepted = _accept(targets, short, level, level_rule, spend, cost, legs)
        if not accepted["floor_after"] > before["worst_in_support_pnl"] + EPS:
            return {
                **base,
                "floor_after": accepted["floor_after"],
                "refused": "worst_not_improved",
            }
        return accepted

    # Single held leg: only what pays for itself at the held size.
    if claim_by_bucket is None:
        return {**base, "refused": "no_claim"}
    level = held[held_legs[0]]
    readings: dict[str, Any] = {}
    clearing: list[tuple[float, str, float]] = []
    for bucket in missing:
        cash = _walk(bucket, level)
        claim = _finite(
            f"delta_v2_c143_claim_{bucket}",
            claim_by_bucket.get(bucket, 0.0) or 0.0,
        )
        clears = cash is not None and clears_economic_hurdle_v1(
            expected_payout=claim * level, cost=cash,
        )
        readings[bucket] = {
            "shares": level,
            "cash": cash,
            "claim": claim,
            "clears_hurdle": bool(clears),
        }
        if clears:
            clearing.append((claim * level / float(cash), bucket, float(cash)))
    if not clearing:
        return {**base, "legs": readings, "refused": "no_leg_clears_hurdle"}
    # Most expected payout per dollar first, while the ceiling lasts.
    clearing.sort(key=lambda item: (-item[0], item[1]))
    chosen: set[str] = set()
    spend = 0.0
    cost = dict(paid)
    for _, bucket, cash in clearing:
        if spend + cash > budget + EPS:
            continue
        chosen.add(bucket)
        spend += cash
        cost[bucket] += cash
    if not chosen:
        return {**base, "legs": readings, "refused": "capital"}
    buy = [b for b in live if b in chosen]
    targets = {b: (level if b in chosen else held[b]) for b in window}
    return _accept(
        targets, buy, level, "cheap_legs_at_held_level", spend, cost,
        {b: readings[b] for b in buy},
    )


def completion_143_exemption_v1(
    *,
    support: Sequence[str],
    held_shares: Mapping[str, Any],
    held_cost_usdc: Mapping[str, Any],
    target_shares: Mapping[str, Any],
    target_cost_usdc: Mapping[str, Any],
    claim_by_bucket: Mapping[str, Any] | None = None,
    below_leg_floor_buckets: Sequence[str] = (),
    hours_to_cutoff: float | None = None,
) -> dict[str, Any]:
    """Whether a bundle IS a charter-143 completion, read on what it buys.

    The exemption the completion carries past charter 93's floor, charter
    123's post-lift edge, charter 73's bundle score and the 5% execution
    target -- each of which it fails by construction -- is granted on the
    shape actually going out, after the venue's lot and dollar lifts, not on
    the candidate the solver saw.  ``target_cost_usdc`` is the whole cost per
    bucket after the buy, all in; ``held_cost_usdc`` before it.

    Exempt only when the bundle adds to no held leg (it is a completion, not
    a top-up), buys at least one missing live leg and no dead one, stands
    outside the completion cutoff, and then: a multi-leg basket leaves the
    worst in-window settlement higher than it found it; a single-leg basket
    buys only legs that clear the one hurdle on their own cost.
    """

    window = tuple(_text("delta_v2_c143x_bucket", bucket) for bucket in support)
    if len(set(window)) != len(window):
        raise ContractError("delta_v2_c143x_support_duplicate")
    dead = {
        _text("delta_v2_c143x_dead", bucket) for bucket in below_leg_floor_buckets
    }
    held = {
        b: _nonnegative("delta_v2_c143x_held", held_shares.get(b, 0.0) or 0.0)
        for b in window
    }
    paid = {
        b: _nonnegative("delta_v2_c143x_cost", held_cost_usdc.get(b, 0.0) or 0.0)
        for b in window
    }
    want = {
        b: max(
            held[b],
            _nonnegative(
                "delta_v2_c143x_target", target_shares.get(b, 0.0) or 0.0
            ),
        )
        for b in window
    }
    cost_after = {
        b: max(
            paid[b],
            _nonnegative(
                "delta_v2_c143x_target_cost",
                target_cost_usdc.get(b, paid[b]) or 0.0,
            ),
        )
        for b in window
    }
    added = {b: want[b] - held[b] for b in window}
    live = tuple(b for b in window if b not in dead)
    held_legs = [b for b in live if held[b] > EPS]
    basket_type = completion_143_basket_type_v1(len(held_legs))
    reason: str | None = None
    before = support_scenario_floor_v1(
        shares_by_bucket=held, cost_by_bucket=paid, support=live
    ) if live else None
    after = support_scenario_floor_v1(
        shares_by_bucket=want, cost_by_bucket=cost_after, support=live
    ) if live else None
    legs: dict[str, Any] = {}
    if CHARTER_143_COMPLETION != "on":
        reason = "off"
    elif basket_type is None or len(held_legs) == len(live):
        reason = "not_broken"
    elif (
        hours_to_cutoff is not None
        and float(hours_to_cutoff) <= COMPLETION_CUTOFF_HOURS
    ):
        reason = "past_completion_cutoff"
    elif any(added[b] > EPS for b in held_legs):
        reason = "adds_to_held_leg"
    elif any(added[b] > EPS for b in window if b in dead):
        reason = "buys_dead_leg"
    elif not any(added[b] > EPS for b in live):
        reason = "buys_nothing"
    elif basket_type == "multi":
        if not after["worst_in_support_pnl"] > before["worst_in_support_pnl"] + EPS:
            reason = "worst_not_improved"
    else:
        for bucket in live:
            if added[bucket] <= EPS:
                continue
            cash = cost_after[bucket] - paid[bucket]
            claim = (
                None if claim_by_bucket is None
                else _finite(
                    f"delta_v2_c143x_claim_{bucket}",
                    claim_by_bucket.get(bucket, 0.0) or 0.0,
                )
            )
            clears = bool(
                claim is not None
                and clears_economic_hurdle_v1(
                    expected_payout=claim * added[bucket], cost=cash,
                )
            )
            legs[bucket] = {
                "shares": added[bucket], "cash": cash, "claim": claim,
                "clears_hurdle": clears,
            }
            if not clears and reason is None:
                reason = "leg_below_hurdle"
    return {
        "rule": COMPLETION_143_RULE_VERSION,
        "mode": CHARTER_143_COMPLETION,
        "exempt": reason is None,
        "reason": reason,
        "basket_type": basket_type,
        "floor_before": None if before is None else before["worst_in_support_pnl"],
        "floor_after": None if after is None else after["worst_in_support_pnl"],
        "floor_legal_after": None if after is None else bool(after["legal"]),
        "legs": legs,
    }


# How much of a basket's floor cushion the tilt may spend (user ruling,
# 2026-09-08: "按照你的方案来调整，即用 lambda").  An equal-share basket at
# one share a leg costs ``K = sum(c)`` and its worst in-support settlement
# pays ``1 - K``; that gap is the only room a tilt has, because charter 93
# forbids spending past it.  ``0`` reproduces ``equal`` exactly and is the
# rollback; ``1`` spends the whole cushion.
#
# One by default (user ruling, 2026-09-08: "最优恒为 t=1：那先应用").  It
# shipped at 0.5 on a guess, and the guess was made against a bankroll read
# wrong: the stake is sized off the pool's ``balance_usdc`` (399.43 on
# 2026-09-08), not off the 15 USDC station cap.  Scanned on that book at the
# true equity, log wealth rises monotonically in ``t`` and the optimum is the
# corner every time -- SAEZ 0.000319 -> 0.000335 across t=0..1 -- because a
# 7.99 USDC stake against a 1597 USDC effective bankroll (W/f) leaves the
# curve straight, which is charter 72's own "注 <= 6 时曲线是直线" read at
# the right scale.  The half cushion was insurance against a curvature that
# is not there.
#
# The cushion it spent was the other half of the guess: t=1 lands the
# pre-lift floor on zero, and the fear was that charters 70/83 would lift a
# leg and push it under.  Measured on the same book it does not -- SAEZ's
# post-lift floor is +0.2064 at t=1, legal -- and charter 93(二)'s
# after-lift recheck is the guard for that case anyway.  A second, weaker
# guard that guesses at the lift is what this constant was.
KELLY_EDGE_TILT_FRACTION = float(
    os.environ.get("PM_KELLY_EDGE_TILT_FRACTION", "").strip() or 1.0
)
if not (0.0 <= KELLY_EDGE_TILT_FRACTION <= 1.0):
    raise RuntimeError(
        f"Kelly edge tilt fraction out of range: {KELLY_EDGE_TILT_FRACTION}"
    )


def floor_first_edge_weights_v1(
    *,
    ordered: Sequence[str],
    prices: Mapping[str, Any],
    effective_claims: Mapping[str, Any],
    tilt_fraction: float | None = None,
    partition_by_bucket: Mapping[str, Any] | None = None,
) -> dict[str, float]:
    """Relative shares per rung: an equal bed first, the cushion tilted second.

    ``prices`` is what one share of each rung costs on the lane the seat
    buys from; ``effective_claims`` is that rung's *realisable* claim --
    ``lambda(a) * p`` under the charter-68 bucket arm -- so a rung the book
    prices far under our claim is read at what such rungs were measured to
    return, not at what they assert.

    The shape is the two-段 form and its arithmetic is forced, not chosen.
    Write ``K = sum(prices)``, the per-share cost of the equal basket, and
    ``f`` for charter 93's floor fraction.  A basket held level at one share
    a leg settles at ``1 - K`` inside its support, and charter 93 admits a
    shape exactly while that number stays at or above ``f*K``.  So
    ``1 - (1 + f)*K`` is the entire budget any tilt has:

    * every rung is floored at ``w_min = 1 - t*(1 - (1 + f)*K)``, which leaves
      ``f*K + (1 - t)*(1 - (1 + f)*K)`` of in-support profit standing;
    * the cash that floor frees, ``(1 - w_min) * K``, is handed to the rungs
      whose realisable edge ``lambda*p - c`` is positive, in proportion to
      that edge -- so the *share* counts move as ``edge / price``, which is
      the density the objective ranks by.  A rung whose realisable edge is
      zero or negative keeps the bed and no more, however large its nominal
      ``p - c`` is: that gap is exactly what the bucket arm was fitted to
      say does not arrive.

    Two invariants make this composable with the clauses around it.
    ``sum(w_b * c_b) == K`` exactly, so charter 72's stake reads the same
    ``cost_per_share`` it always did and needs no amendment.  And the
    resulting floor is at least ``f*K`` by construction, so charter 93
    cannot refuse the shape -- which is the whole difference from the
    ``edge_weighted`` arm this replaces, whose floor was a fraction of the
    *best rung's weight* and therefore said nothing about the basket's cost.
    Measured on the 2026-09-08 book, every ``edge_weighted`` candidate was
    refused by the floor (SAEZ read -0.246 against a 0.30 premium), which is
    why the arm had never been able to trade.

    Returns ``equal`` unchanged when no rung has a positive realisable edge,
    when the basket already costs a share or more (no cushion to spend), or
    when the tilt fraction is zero.
    """

    window = tuple(_text("delta_v2_shape_bucket", bucket) for bucket in ordered)
    if len(set(window)) != len(window):
        raise ContractError("delta_v2_shape_bucket_duplicate")
    if not window:
        raise ContractError("delta_v2_shape_window_empty")
    equal = {bucket: 1.0 for bucket in window}
    tilt = (
        KELLY_EDGE_TILT_FRACTION if tilt_fraction is None else float(tilt_fraction)
    )
    if not (0.0 <= tilt <= 1.0):
        raise ContractError("delta_v2_shape_tilt_fraction_invalid")
    if tilt <= EPS:
        return equal
    cost = {
        bucket: _nonnegative(f"delta_v2_shape_price_{bucket}", prices.get(bucket, 0.0))
        for bucket in window
    }
    unit = sum(cost.values())
    # The cushion is what charter 93 leaves above its own floor, not above
    # zero.  It was ``1 - K`` while the floor sat at zero; since v2.1
    # (2026-09-12) the floor is ``SUPPORT_SCENARIO_FLOOR_FRACTION`` of the
    # premium, and a tilt that still spent ``1 - K`` landed every tilted
    # rung on zero -- under the 5% floor that judges it, so the arm was
    # refused again exactly as the shape it replaced had been.
    cushion = 1.0 - (1.0 + SUPPORT_SCENARIO_FLOOR_FRACTION) * unit
    if unit <= EPS or cushion <= EPS:
        # A basket with no room above the floor has no cushion to spend, and
        # charter 72 does not size one anyway.
        return equal
    edges = {
        bucket: (
            _finite(f"delta_v2_shape_claim_{bucket}", effective_claims.get(bucket, 0.0))
            - cost[bucket]
        )
        for bucket in window
    }
    positive = {b: e for b, e in edges.items() if e > EPS}
    if not positive:
        return equal
    total_edge = sum(positive.values())
    floor_weight = 1.0 - tilt * cushion
    freed = (1.0 - floor_weight) * unit
    weights = {bucket: floor_weight for bucket in window}
    for bucket, edge in positive.items():
        if cost[bucket] <= EPS:
            continue
        weights[bucket] += freed * (edge / total_edge) / cost[bucket]
    if partition_by_bucket is None:
        return weights
    # The floor is this shape's by construction; the whole-basket edge is
    # not, and the two answer to different clauses.  ``sum(w*c) == K`` holds
    # for the bed and the tilt alike, so at one stake they cost the same and
    # their expected values differ only through ``sum(partition * w)`` --
    # which makes "not worse than the bed" a one-line comparison rather than
    # a re-evaluation.  It has to be compared at all because the tilt orders
    # rungs by ``(lambda*p - c)/c`` while the expectation prices them at
    # ``p``: two different orders, so a tilt can pay less than the bed it
    # came from.
    #
    # Against the bed rather than against zero: the window this shape seats
    # in has already cleared charter 78's whole-basket positive-EV gate on
    # the equal shape, so a tilt at or above equal is positive by that
    # gate's own arithmetic, and one comparison serves both.  What it makes
    # impossible is the failure this is really here for -- the gate reading
    # one basket while the book buys another.
    share = {
        bucket: _nonnegative(
            f"delta_v2_shape_partition_{bucket}",
            partition_by_bucket.get(bucket, 0.0),
        )
        for bucket in window
    }
    tilted = sum(share[b] * weights[b] for b in window)
    bedded = sum(share[b] for b in window)
    return weights if tilted + EPS >= bedded else equal

MARGINAL_COVERAGE_RULE_VERSION = "marginal_coverage_reading_v1"


def marginal_coverage_reading_v1(
    *,
    claim_by_bucket: Mapping[str, Any],
    cost_by_bucket: Mapping[str, Any],
    window: Sequence[str],
    held_buckets: Sequence[str] = (),
) -> dict[str, Any]:
    """What a window claims per unit of cost, and what widening it adds.

    Audit only.  Nothing reads this back; it exists so the question "did the
    coverage this decision added pay for the cost it added" is answerable
    from a row instead of from a join written by hand.

    An equal-share basket settling one share a rung pays 1 inside its window
    against an all-in cost ``K``, so its return per share is ``h/K - 1`` and
    adding rungs raises it exactly while ``dh/dK > h/K``.  Both ratios are
    reported; the comparison is deliberately NOT turned into a verdict the
    caller can act on, because acting on it was measured to lose money
    (2026-09-09, 118 opening seats: building the window outward under this
    rule takes -10.34% to -19.12% on the served claim, -12.65% on the
    per-rung lambda-deflated one).

    ``kind`` is the part worth having.  The criterion speaks only about
    adding BUCKETS: scaling an equal-share basket by n leaves ``h/K``
    identically unchanged, so a top-up that only adds shares to rungs the
    seat already covers has no marginal ratio at all and the rule is silent
    about it.  Measured on the same book, 29 of 33 settled top-ups were
    exactly that -- which is invisible unless the two are recorded apart.
    """

    seat = tuple(_text("delta_v2_coverage_bucket", b) for b in window)
    if len(set(seat)) != len(seat):
        raise ContractError("delta_v2_coverage_bucket_duplicate")
    held = {_text("delta_v2_coverage_held_bucket", b) for b in held_buckets}

    def ratio(scope: Sequence[str]) -> tuple[float, float, float | None]:
        claim = sum(
            float(claim_by_bucket.get(bucket, 0.0) or 0.0) for bucket in scope
        )
        cost = sum(
            float(cost_by_bucket.get(bucket, 0.0) or 0.0) for bucket in scope
        )
        return claim, cost, (claim / cost if cost > EPS else None)

    claim, cost, window_ratio = ratio(seat)
    added = tuple(bucket for bucket in seat if bucket not in held)
    held_in_window = tuple(bucket for bucket in seat if bucket in held)
    _, _, held_ratio = ratio(held_in_window)
    added_claim, added_cost, added_ratio = ratio(added)

    if not held:
        kind = "open"
    elif added:
        kind = "widen"
    else:
        kind = "shares_only"

    pays: bool | None = None
    if kind == "widen" and added_ratio is not None and held_ratio is not None:
        pays = added_ratio > held_ratio

    return {
        "rule": MARGINAL_COVERAGE_RULE_VERSION,
        "kind": kind,
        "window_claim": claim,
        "window_cost": cost,
        "claim_cost_ratio": window_ratio,
        "held_claim_cost_ratio": held_ratio,
        "added_buckets": list(added),
        "added_claim": added_claim,
        "added_cost": added_cost,
        "marginal_claim_cost_ratio": added_ratio,
        # ``None`` on an opening (nothing to widen from) and on a shares-only
        # top-up (the criterion has nothing to say), which a reader must be
        # able to tell apart from "it was judged and refused".
        "marginal_pays_its_way": pays,
    }


ROBUST_CLAIM_RULE_VERSION = "kl_ball_worst_case_window_claim_v1"


def robust_window_claim_v1(
    claim: float,
    *,
    divergence_budget: float,
) -> tuple[float, float]:
    """The worst window claim inside a relative-entropy ball, and its factor.

    Every number this system sizes and gates on is monotone increasing in the
    mass the served partition puts inside the seated window: the charter-87
    admission test, charter 72's Kelly stake, charter 73/92's increment edge
    and the log-wealth curve all get better as that mass rises.  So the
    robust reading ``min over P`` of any of them is the same object -- the
    smallest inside-mass the ambiguity set allows -- and one scalar answers
    for all of them.

    ``P`` is the set of partitions within ``divergence_budget`` nats of the
    served one in KL.  Holding the shape inside the window fixed (the
    conditional ``r_i`` the proposal keeps), the problem collapses to one
    dimension -- the in/out split -- and the answer is the smaller root of

        q ln(q/p) + (1-q) ln((1-q)/(1-p)) = eta

    which is monotone on ``(0, p]`` and solved by bisection.  Returning the
    factor as well as the level is deliberate: the factor is what
    ``deflate_window_partition_v1`` takes, so the robust belief reaches the
    solver as a renormalised partition rather than as a set of separately
    shrunken buckets that no longer sum to anything.

    ``eta = 0`` is the identity, which is what makes the off arm exact.
    """

    p = float(claim)
    eta = float(divergence_budget)
    if not math.isfinite(p) or not math.isfinite(eta):
        raise ContractError("delta_v2_robust_claim_invalid")
    if eta < 0.0:
        raise ContractError("delta_v2_robust_divergence_budget_invalid")
    if eta <= 0.0 or p <= EPS:
        return max(0.0, p), 1.0
    p = min(p, 1.0 - EPS)

    def divergence(q: float) -> float:
        if q <= 0.0:
            return math.log(1.0 / (1.0 - p))
        return q * math.log(q / p) + (1.0 - q) * math.log((1.0 - q) / (1.0 - p))

    # The whole ball may reach zero, in which case the robust claim is zero
    # and the window is unbuyable under this budget -- a real answer, not a
    # failure to converge.
    if divergence(0.0) <= eta:
        return 0.0, 0.0
    low, high = 0.0, p
    for _ in range(80):
        mid = 0.5 * (low + high)
        if divergence(mid) > eta:
            low = mid
        else:
            high = mid
    worst = 0.5 * (low + high)
    return worst, worst / p


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
#
# Re-measured 2026-09-08 (user asked where the 0.48 came from).  The
# 2026-08-14 fit above left no code behind -- that commit shipped the kernel,
# the runtime, the daemon and a doctrine test, and nothing that could re-run
# the measurement; ``scripts/measure_off_center_claim.py`` was not written
# until 2026-09-05.  So the numbers it reports cannot be reproduced, the
# off-centre cells never recorded an n (only the centre-holding cell did,
# n=16,981), and it was taken the day BEFORE charter 49 made centre
# containment a structural admission floor -- so it measured a population of
# off-centre windows that the ladder ruling then removed from the menu.
#
# When the script was finally written, its first run (2026-09-05) found the
# other half of that same sentence had already failed: centre-holding windows
# realise 1.10-1.30, not the 1.01 recorded here.  That produced charter 81's
# lift.  The off-centre half of the same output was not acted on and stood
# for three more days.
#
# Two reproducible runs now disagree with it, in the same direction, on the
# same statistic (P0, four-bucket windows, argmax centre):
#
#   claimed      2026-08-14   2026-09-08 (1,419 station-days, 39 days)
#   0.05 - 0.10     0.78       0.80 [0.54,1.08] n=627
#   0.10 - 0.20     0.83       0.84 [0.74,0.96] n=1,304
#   0.20 - 0.35     0.85       0.99 [0.92,1.06] n=1,916
#   0.35 - 0.55     0.82       1.14 [1.08,1.19] n=1,533
#   0.55 - 1.00     0.48       1.06 [0.90,1.19] n=120
#
# The bottom three cells are served at 1.0 rather than at their point
# estimates: every one of their intervals contains 1, so "no correction" is
# what the evidence supports and lifting an off-centre claim is not something
# this table was ever asked to do.  The top two reproduce and are kept.
#
# The n on the last cell is the reason this is a correction and not a
# rewrite: 120 windows against 3,973 centre-holding ones in the same band.
# Charter 49 left the off-centre menu to the evidence-exempt incumbent alone,
# so this whole table now governs a rare case -- which is also why it went
# three weeks without anyone noticing it was wrong by a factor of two.
OFF_CENTER_CLAIM_BANDS = (
    # (claimed-mass upper bound, realized-over-claimed factor)
    (0.05, 1.15),
    (0.10, 0.85),
    (0.20, 0.88),
    (0.35, 1.00),
    (0.55, 1.00),
    (1.01, 1.00),
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


# Retired 2026-09-12 (user ruling: "离心折扣恒等 + 车道退役 + 标注", the
# second cut of the claim-chain clean-up after charter 81).  The deflation
# only ever applied to a window that does NOT hold the mean centre, and the
# seat is the probability-mass head, which always does -- so on every seated
# window in production the factor was 1.0 by construction, and the only rows
# it still touched were the unseated candidate windows in lambda's fitting
# population.  Its table was hand-measured once (2026-08-14, no script, no n)
# and refitted by a lane from 2026-09-09 that never had a seated row to price
# itself on.  ``off`` (the default) makes the factor identity on every path
# and the nightly refit a no-op; ``on`` is the research arm that restores the
# bootstrap constant above and the store's fitted tables.
OFF_CENTRE_CLAIM_MODE = (
    os.environ.get("PM_OFF_CENTRE_CLAIM", "").strip().lower() or "off"
)
if OFF_CENTRE_CLAIM_MODE not in {"off", "on"}:
    raise RuntimeError(f"unknown off-centre claim mode: {OFF_CENTRE_CLAIM_MODE}")


def off_center_claim_factor(
    claimed_mass: float,
    *,
    gap_buckets: float = 1.0,
    bands: "Sequence[tuple[float, float]] | None" = None,
) -> float:
    """Deflation for a window that does not contain the model centre.

    ``gap_buckets`` is how far the centre sits outside the window, in
    bucket ordinals: 0 when the window contains it.  The default of 1
    reproduces the fitted two-value rule for every caller that does not
    measure a distance.
    """

    if OFF_CENTRE_CLAIM_MODE == "off":
        # Retired 2026-09-12: identity on every path, whatever the store
        # or the caller hands in.
        return 1.0
    mass = float(claimed_mass)
    # ``bands`` is the DB-resident table for the stage's current band axis
    # (``off_centre_claim_store``); the constant is the bootstrap a stage
    # serves before one has been fitted on the axis it serves today.  The
    # kernel stays pure: the caller reads the store, this only applies it.
    table = tuple(bands) if bands else OFF_CENTER_CLAIM_BANDS
    full = table[-1][1]
    for bound, factor in table:
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
# Retired 2026-09-12 (user ruling: "先撤这种硬编码(打补丁)的宪章81").  The
# clause's own retirement condition was met: it was to stand only while
# centre-holding windows realised MORE than they claimed (1.30/1.12 measured
# 2026-09-05 on the pre-width-layer served vector).  Measured 2026-09-12 on
# the same enumeration (chain.json, first seated decisions and every live
# ordering decision, 27 event dates): the raw seated claim now OVERSTATES
# the truth (P0 ordering moments 0.887 claimed vs 0.737 realised), the lift
# applied to 100% of seated windows (every mass-head window holds the
# centre), clamped 53% of them at 1.0, and lowered charter 68's agreement
# a by 3 points so lambda deflated harder -- the two stages cancelled on
# the median (eff/raw 1.001) while adding a +-10% per-window noise factor
# (corr(log lift, log lambda) = +0.15; sd of the product 0.130 against
# 0.116 for lambda alone).  The default is therefore empty: the factor is
# identity, the gate and the solver read the raw claim, and the ledger's
# fingerprint moves with it.  The old table stays reachable as a research
# arm through PM_CENTER_WINDOW_CLAIM_BANDS='[[0.35, 1.0], [0.55, 1.21], [1.01, 1.08]]'.
CENTER_WINDOW_CLAIM_DEFAULT = "[]"
CENTER_WINDOW_CLAIM_RETIRED_TABLE = "[[0.35, 1.0], [0.55, 1.21], [1.01, 1.08]]"


def _parse_claim_bands(
    text: str, *, default: str = CENTER_WINDOW_CLAIM_DEFAULT
) -> tuple[tuple[float, float], ...]:
    cleaned = (text or "").strip()
    if cleaned.lower() == "off":
        return ()
    raw = json.loads(cleaned or default)
    bands = tuple((float(bound), float(factor)) for bound, factor in raw)
    if any(bound <= 0.0 or factor <= 0.0 for bound, factor in bands):
        raise RuntimeError(f"invalid centre-window claim bands: {text}")
    if list(bound for bound, _ in bands) != sorted(bound for bound, _ in bands):
        raise RuntimeError(f"centre-window claim bands must ascend: {text}")
    return bands


CENTER_WINDOW_CLAIM_BANDS: tuple[tuple[float, float], ...] = _parse_claim_bands(
    os.environ.get("PM_CENTER_WINDOW_CLAIM_BANDS", "")
)


# The admission/sizing split (user ruling, 2026-09-06: "拆").  Charter 81's
# lift decides who is seated: measured on the ledger, its coarse three-band
# table beats the reliability curve by five points, because a factor at the
# gate is an admission knob and matching it to the curve admits marginal
# windows the market has priced richer for a reason.  The claim IS
# calibratable all the same -- on the windows the gate enumerates, realised
# over claimed runs 1.44/1.31/1.18/1.05 by claim band on P0, the same shape
# on P0-M and P1 and on both widths.  This table is where that curve may be
# spent without touching admission: the whole-basket Kelly stake, and
# nothing else.  Empty (the default) means sizing reads exactly what
# admission reads, which is charter 68's "one belief" and the shipped rule.
SIZING_CLAIM_BANDS: tuple[tuple[float, float], ...] = _parse_claim_bands(
    os.environ.get("PM_SIZING_CLAIM_BANDS", ""), default="[]",
)


def sizing_claim_factor(claimed_mass: float) -> float:
    """The stake's own lift for a centre-holding window; 1.0 when off."""

    if not SIZING_CLAIM_BANDS:
        return 1.0
    mass = float(claimed_mass)
    for bound, factor in SIZING_CLAIM_BANDS:
        if mass < bound:
            return float(factor)
    return 1.0


def center_window_claim_factor(claimed_mass: float) -> float:
    """Charter 81's multiplier for a window holding the centre; 1.0 when off.

    Off since 2026-09-12 (charter 81 retired); the arm only multiplies when
    ``PM_CENTER_WINDOW_CLAIM_BANDS`` names a table.
    """

    if not CENTER_WINDOW_CLAIM_BANDS:
        return 1.0
    mass = float(claimed_mass)
    for bound, factor in CENTER_WINDOW_CLAIM_BANDS:
        if mass < bound:
            return float(factor)
    return 1.0


# Arm B (user ruling 2026-09-06, "做 B 臂。先影子"): the convergence claim
# lift.  Measured that day on the probability layer (P0-M and P1 stages,
# 852 station-days with truth): a window whose served claim ROSE by 0.02 or
# more over the prior 12 hours while the market's best-ask sum FELL by 0.05
# or more, and which the market still prices at 1.1x the claim or above,
# realises 1.31-1.44 of its claim (n=50/47, 30/25 stations, hit 82-89%,
# drop-3 winners +10% at 12h) -- while the same market move against an
# UNCHANGED claim realises 1.00-1.13 and loses (-15%/-27%, every week
# negative).  So the factor is conditioned on both sides moving toward each
# other, never on the market's move alone.  It multiplies the claim before
# the positive-EV gate, like charter 81, because the gate is what refuses
# these windows today: 46 of 50 sit at claim 0.63 against price 0.80.
#
# ``shadow`` (default) computes the state and the factor on every window,
# journals candidates and what the lift would have admitted, and changes
# nothing; ``on`` is the ledger arm and, only after the ledger prices it,
# production.  P0 is excluded: an opening seat has no prior belief or book
# (charter 48 opens at listing), and the measurement was P0-M/P1.  This
# collides with charter 68's P1 ceiling ("trust it, not more than it
# trusts itself") by construction -- the factor is exactly "more than the
# claim" -- which is why it ships in shadow and must be ruled on by name.
CONVERGENCE_CLAIM_MODE = (
    os.environ.get("PM_CONVERGENCE_CLAIM", "").strip().lower() or "shadow"
)
if CONVERGENCE_CLAIM_MODE not in {"off", "shadow", "on"}:
    raise RuntimeError(f"unknown convergence claim mode: {CONVERGENCE_CLAIM_MODE}")
CONVERGENCE_CLAIM_HORIZON_HOURS = float(
    os.environ.get("PM_CONVERGENCE_CLAIM_HORIZON_HOURS", "") or 12.0
)
CONVERGENCE_CLAIM_FACTOR = float(
    os.environ.get("PM_CONVERGENCE_CLAIM_FACTOR", "") or 1.30
)
CONVERGENCE_CLAIM_STAGES: frozenset[str] = frozenset({"P0-M", "P1"})
CONVERGENCE_CLAIM_MIN_AGREEMENT = 1.10
CONVERGENCE_CLAIM_MIN_CLAIM_RISE = 0.02
CONVERGENCE_CLAIM_MIN_PRICE_FALL = 0.05
CONVERGENCE_CLAIM_RULE_VERSION = "convergence_claim_v1"


# Charter 110 (extended 2026-09-08): the partition key for lambda is not the
# band axis alone.  ``a = price / claim`` is keyed on the claim, and the band
# table is only the first of several layers that move it -- charter 81's
# centre lift, the off-centre deflation and the convergence lift all
# multiply the same number afterwards.  A fit that pools rows written under
# two different versions of THOSE tables is fitting one curve to two x axes,
# exactly the failure the band-axis key was added to stop; the band axis
# simply happened to be the layer that moved first.
#
# So this is the fingerprint of every table that turns a raw window mass
# into the served claim.  It is a partition key, not an audit column: unlike
# a band table (content-addressed, refitted daily, sha churning in the third
# decimal) these are kernel constants that move only when a person changes
# them, so partitioning on them costs nothing until one actually moves --
# and when one does, it must cost the whole history, because the claims
# behind those rows are no longer the claims the code now computes.
CLAIM_TRANSFORM_RULE_VERSION = "claim_transform_v2"


def window_claim_factor(
    raw_win: float,
    *,
    center_gap: float,
    off_centre_bands: "Sequence[tuple[float, float]] | None" = None,
) -> float:
    """The claim multiplier a window earns from its position alone.

    The off-centre deflation with charter 81's centre lift on top, in that
    order.  ``center_gap`` is the distance from the model centre in bucket
    ordinals and is 0 for a window containing it, which is exactly what
    ``off_center_claim_factor`` reads as "no correction" -- so one call
    covers both sides and the lift is the only branch.

    Extracted 2026-09-08 so the enumeration and the band-axis replay compute
    it from one implementation rather than two.  The convergence lift is NOT
    here: it needs a prior belief and a prior book, which is state this
    function has no business holding.
    """

    factor = off_center_claim_factor(
        float(raw_win), gap_buckets=center_gap, bands=off_centre_bands
    )
    if float(center_gap) <= 0.0:
        factor *= center_window_claim_factor(float(raw_win))
    return factor



def claim_transform_fingerprint(
    *, holds_centre: bool, off_centre_table_sha: str | None = None
) -> str:
    """Content hash of the claim tables THIS window's claim actually went
    through.

    Keyed on the path, not on the union, and the distinction is the whole
    value of the key.  A window holding the centre never touches the
    off-centre table and a window without it never touches charter 81's
    lift, so hashing both into one fingerprint would retire every row in
    the sample whenever either table moved -- measured 2026-09-08: all
    1,493 candidate windows in the book carry a factor at or above 1.0,
    which is to say the ladder floor (charter 49) has left the fit with no
    off-centre rows at all.  A union key would have taken the entire lambda
    sample to zero to record a change to a table none of those rows read.

    Split this way the key still fails closed where it must: charter 81's
    lift is on 1,321 of those rows, and moving it does retire them, because
    their claims really would no longer be the claims this code computes.
    """

    common = {
        "rule": CLAIM_TRANSFORM_RULE_VERSION,
        "convergence_mode": str(CONVERGENCE_CLAIM_MODE),
        "convergence_factor": float(CONVERGENCE_CLAIM_FACTOR),
        "convergence_stages": sorted(CONVERGENCE_CLAIM_STAGES),
        "convergence_min_agreement": float(CONVERGENCE_CLAIM_MIN_AGREEMENT),
        "convergence_min_claim_rise": float(CONVERGENCE_CLAIM_MIN_CLAIM_RISE),
        "convergence_min_price_fall": float(CONVERGENCE_CLAIM_MIN_PRICE_FALL),
    }
    if holds_centre:
        return content_sha256({
            **common,
            "path": "holds_centre",
            "center_window_bands": [
                [float(bound), float(factor)]
                for bound, factor in CENTER_WINDOW_CLAIM_BANDS
            ],
        })
    # The off-centre table moved into the database on 2026-09-09 and refits
    # nightly, so the constant below is only the bootstrap.  A caller that
    # served a fitted table names it here; None means "the bootstrap", which
    # is what every row written before the store existed carries.  Without
    # this the served table could move under lambda's own sample and the
    # partition key -- the thing that exists to notice exactly that -- would
    # not see it.  Measured the day it landed: 84 replay rows were on this
    # path already.
    if OFF_CENTRE_CLAIM_MODE == "off":
        # Retired 2026-09-12: the claim on this path is the raw claim, so
        # neither the constant nor a served table is part of what computed
        # it, and moving them must not retire a row that never read them.
        return content_sha256({
            **common,
            "path": "off_centre",
            "off_centre_claim_mode": "off",
        })
    return content_sha256({
        **common,
        "path": "off_centre",
        "off_centre_claim_mode": OFF_CENTRE_CLAIM_MODE,
        "off_center_bands": [
            [float(bound), float(factor)]
            for bound, factor in OFF_CENTER_CLAIM_BANDS
        ],
        "off_center_ramp_buckets": float(OFF_CENTER_CLAIM_RAMP_BUCKETS),
        "off_centre_table_sha256": (
            str(off_centre_table_sha) if off_centre_table_sha else None
        ),
    })


def convergence_state_v1(
    *,
    stage: str,
    claim_now: float,
    claim_prev: float | None,
    price_now: float | None,
    price_prev: float | None,
    agreement_now: float | None,
) -> dict[str, Any]:
    """Whether a window is converging from both sides and still dear.

    ``claim`` is the served window mass before any lift, ``price`` the
    window's best-ask sum on the same basis at both ends, ``agreement`` the
    all-in price over the served claim -- the a charter 68 reads.  A
    missing prior (None) is "not converging" with the reason recorded;
    the state never invents a move.
    """

    state: dict[str, Any] = {
        "rule": CONVERGENCE_CLAIM_RULE_VERSION,
        "stage_eligible": str(stage) in CONVERGENCE_CLAIM_STAGES,
        "claim_rise": None,
        "price_fall": None,
        "agreement": None if agreement_now is None else float(agreement_now),
        "converging": False,
        "reason": None,
    }
    if not state["stage_eligible"]:
        state["reason"] = "stage"
        return state
    if claim_prev is None or price_now is None or price_prev is None:
        state["reason"] = "no_prior"
        return state
    rise = float(claim_now) - float(claim_prev)
    fall = float(price_prev) - float(price_now)
    state["claim_rise"] = round(rise, 4)
    state["price_fall"] = round(fall, 4)
    if agreement_now is None or float(agreement_now) < CONVERGENCE_CLAIM_MIN_AGREEMENT:
        state["reason"] = "not_dear"
        return state
    if rise < CONVERGENCE_CLAIM_MIN_CLAIM_RISE:
        state["reason"] = "claim_not_rising"
        return state
    if fall < CONVERGENCE_CLAIM_MIN_PRICE_FALL:
        state["reason"] = "price_not_falling"
        return state
    state["converging"] = True
    return state


def convergence_claim_factor(state: Mapping[str, Any]) -> float:
    """The lift a converging window earns; 1.0 for every other window."""

    return float(CONVERGENCE_CLAIM_FACTOR) if state.get("converging") else 1.0


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
# Charter 130 retired the per-leg tick friction and the margin: the hurdle
# is one constant, ``ECONOMIC_HURDLE_V1``.  A process still handing in the
# old knob is running an old plist, and must not silently get the new rule.
for _retired in ("PM_SUPPORT_EDGE_FRICTION_TICKS_PER_LEG", "PM_SUPPORT_EDGE_MARGIN"):
    if os.environ.get(_retired, "").strip():
        raise RuntimeError(
            f"{_retired} was retired by charter 130; the hurdle is ECONOMIC_HURDLE_V1"
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
    kelly_fraction: float | None = None,
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
    fraction of the log-optimal stake is the same problem as betting the
    full stake against a SMALLER book -- ``f*q*(W) == q*(f*W)`` -- so it
    enters as the effective bankroll ``W*f``: one number, and the same one
    whether it sizes a basket or ranks two.  See
    ``curve_effective_bankroll_v1`` for the direction this had backwards
    until 2026-09-09 and for what turning it round costs.
    """

    effective = curve_effective_bankroll_v1(bankroll_usdc, kelly_fraction)
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
    kelly_fraction: float | None = None,
    below_leg_floor_buckets: Sequence[str] = (),
    excluded_candidate_ids: Sequence[str] = (),
) -> dict[str, Any]:
    """Return the exact argmax over a caller-provided bounded feasible set.

    ``excluded_candidate_ids`` (charter 121, 2026-09-08) removes rows the
    caller has already tried and had refused downstream -- today, by charter
    93 (二)'s floor re-read on the lifted targets, which this function cannot
    see because the venue's lot and dollar lifts are applied after it returns.
    Excluded rows are dropped from the legal set, so the winner chain picks
    among what is left rather than the caller collapsing the whole round to a
    hold.  A baseline is never excluded: buying nothing is the fallback every
    round must keep, and dropping it would make the solve unable to answer.

    ``below_leg_floor_buckets`` (charter 84 八, 2026-09-05): rungs of the
    support the MARKET prices under the leg floor.  The ruling that forbids
    buying them forbids the scenario floor from demanding they be covered, so
    they are left out of the protected set here; everything else about them
    -- their probability, their place in the ladder -- is untouched.  A leg
    we merely decline to buy is not one of these and must stay protected;
    see ``ladder_balance_targets_v1`` for the judgement that renamed this.

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
    unbuyable = {_text("delta_v2_unbuyable_bucket", b) for b in below_leg_floor_buckets}
    protected_buckets = tuple(
        bucket for bucket in protected_buckets if str(bucket) not in unbuyable
    )
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
            # Charter 143: the candidate's own reading of whether it is a
            # completion by held count; ``_floor_legal`` below reads it.
            "completion_143_exempt": bool(
                CHARTER_143_COMPLETION == "on"
                and str(raw.get("candidate_origin") or "")
                == LADDER_BALANCE_CANDIDATE_ORIGIN
                and (raw.get("completion_143") or {}).get("exempt") is True
            ),
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
        # Charter 143: a completion of a broken basket answers to the
        # in-window worst settlement it improves (multi-leg) or to the
        # hurdle each leg clears (single-leg), not to this 5% -- which it
        # fails by construction wherever it is not reachable.
        if row.get("completion_143_exempt"):
            return True
        cost = sum(row["audit"]["cost_by_bucket"].values())
        return float(row["audit"]["profit_floor"]) >= (
            SUPPORT_SCENARIO_FLOOR_FRACTION * cost - EPS
        )

    # Charter 121: a row the caller has already had refused downstream is out
    # of the running, not out of the audit -- ``evaluations`` still carries it,
    # so the origin counts and the floor-refusal counts below read the round as
    # it really was.  The baseline is exempt by construction; see the argument
    # in this function's docstring.
    excluded_ids = {
        str(candidate_id) for candidate_id in excluded_candidate_ids
        if str(candidate_id or "")
    }
    legal = [
        row for row in evaluations
        if _floor_legal(row)
        and (
            str(row.get("candidate_kind") or "") == CANDIDATE_KIND_BASELINE
            or str(row.get("candidate_id") or "") not in excluded_ids
        )
    ]
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
        balance_rows = [
            row for row in legal
            if row["candidate_origin"] == LADDER_BALANCE_CANDIDATE_ORIGIN
        ]
        if kelly_rows:
            winner = max(
                kelly_rows,
                key=lambda row: (
                    -sum(row["audit"]["cost_by_bucket"].values()),
                    row["candidate_id"],
                ),
            )
        elif balance_rows:
            # Charter 84: the Kelly law offered nothing because the seat
            # already holds more than it would size today; a seat the venue
            # left short is completed to its held level when that completion
            # is floor-legal.  Never beside a Kelly row -- a Kelly row adds to
            # every leg and completes the ladder on its own terms.
            winner = min(
                balance_rows,
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
            (DEFAULT_KELLY_FRACTION if kelly_fraction is None else kelly_fraction)
            if form in (CURVE_OBJECTIVE_FORM, KELLY_OBJECTIVE_FORM)
            else None
        ),
        "winner_candidate_origin": winner["candidate_origin"],
        # Charter 143: present only when the winner is a completion by held
        # count, so every other solution's identity is byte-for-byte what it
        # was.
        **(
            {"winner_completion_rule": COMPLETION_143_RULE_VERSION}
            if winner.get("completion_143_exempt") else {}
        ),
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
    # Audit only, and deliberately OUTSIDE ``identity`` so no plan's content
    # sha churns: which origins were on the table, how many of each, and
    # which of them the floor refused.  A solution says only who won and how
    # many ran, so a completion candidate that was offered and then shadowed
    # by a Kelly row is invisible from a plan -- which is how the charter-84
    # loss (this function's winner chain is kelly > balance, with no
    # stage in it, and charter 93 may then refuse that Kelly row after the
    # lift, leaving the ladder short and the day held) has gone unseen on
    # P0-M and P1 since charter 84 landed.  Two sessions hunting it on
    # 2026-09-06 could not screen for it from a plan at all.
    origin_counts: dict[str, int] = {}
    floor_refused: dict[str, int] = {}
    legal_ids = {id(row) for row in legal}
    for row in evaluations:
        key = str(row.get("candidate_origin") or "")
        origin_counts[key] = origin_counts.get(key, 0) + 1
        if id(row) not in legal_ids:
            floor_refused[key] = floor_refused.get(key, 0) + 1
    completion_offered = int(
        origin_counts.get(LADDER_BALANCE_CANDIDATE_ORIGIN) or 0
    )
    completion_floor_refused = int(
        floor_refused.get(LADDER_BALANCE_CANDIDATE_ORIGIN) or 0
    )
    return {
        **identity,
        "content_sha256": content_sha256(identity),
        "candidate_origin_counts": dict(sorted(origin_counts.items())),
        "floor_refused_origin_counts": dict(sorted(floor_refused.items())),
        # The one question the charter-84 hunt asks: was a completion
        # offered, floor-legal, and then passed over for another row?
        #
        # The first version of this flag asked only "offered and did not
        # win", and a completion the floor refused answers yes to that --
        # it was offered, it is not the winner, and the winner chain never
        # saw it because ``legal`` had already dropped it.  Measured over
        # 2026-09-06..08, 78 compiles offered a completion: 45 of them were
        # floor refusals reading as shadowing, and 3 were the real thing.
        # A screen built on the old flag is wrong by 15x in the direction
        # that matters, and it is the screen the charter-84 hunt uses; the
        # refusal is a different defect with a different repair (charter
        # 93's floor, and charter 116's exemption for a repair), so the two
        # have to be separable from a plan.
        "shadowed_completion": bool(
            completion_offered > completion_floor_refused
            and winner["candidate_origin"] != LADDER_BALANCE_CANDIDATE_ORIGIN
        ),
        # Offered and refused before the winner chain ran.  Not shadowing:
        # nothing passed it over, the floor removed it.
        "completion_floor_refused": bool(
            completion_offered and completion_offered == completion_floor_refused
        ),
    }


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
    if mode == "FOK":
        # Fill-or-kill has exactly two outcomes and this contract used to
        # admit only one of them: any fraction under one was refused, so the
        # only distribution a FOK could carry was "fills, with certainty".
        # That is not a model of a fill-or-kill order, it is the assumption
        # that the book cannot move between the quote we priced and the
        # order's arrival -- the same assumption charter 120 removed from the
        # completion search when it made "the ladder cannot supply this" an
        # answer rather than a shortfall nobody looked at.  A killed FOK buys
        # nothing and costs nothing, and the objective can only weigh that
        # against what the fill buys if the outcome is expressible.
        #
        # Partial stays forbidden, because that is what the order type means:
        # a fraction strictly between zero and one is a fill this venue will
        # not produce for this family, and admitting it would let a FOK be
        # scored as the maker it is not.
        if any(EPS < value < 1.0 - EPS for value in fractions):
            raise ContractError("delta_v2_fok_distribution_partial_forbidden")
        if not any(value >= 1.0 - EPS for value in fractions):
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
    repair_exempt: bool = False,
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
    # Charter 92: under the whole-basket edge rule the belief is the seated
    # partition the solver judged on, never the snapshot vector.  A plan
    # that reaches this layer without one is refused by name.
    if (
        rule == EXECUTION_AUTHORIZATION_WHOLE_BASKET_EDGE
        and not edge_probabilities
    ):
        structural_blockers.add(EXECUTION_EDGE_BELIEF_MISSING)
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
    # Every bundle answers to its score.  Charter 74's structural
    # authorization was the one exception and it went with the ticket (user
    # ruling 2026-09-08); ``rule`` now selects which score, never whether a
    # score is needed.
    # Charter 116: a completion that turns an illegal floor legal is a
    # repair, and an expectation test is the wrong question to put to a
    # repair -- the missing leg is missing because ``claim < ask``, which is
    # exactly the region the edge rule refuses.  Structural blockers still
    # bind; only the score is waived, and only for a bundle the caller has
    # already established repairs an illegal seat at charter 112's cheapest
    # legal level.
    authorized = (
        not structural_blockers
        and (
            bool(repair_exempt)
            or score > execution_weights.epsilon_score + EPS
        )
    )
    identity = {
        "schema": BUNDLE_EVALUATION_SCHEMA,
        "completion_repair_exempt": bool(repair_exempt),
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
    completion_repair: Mapping[str, Any] | None = None,
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
    # Charter 116: the re-score authorizes on the rule and the state the
    # compile authorized on, so a repair that was exempt when it was chosen
    # is still a repair at the size it will be sent.
    repair_exempt = bool((completion_repair or {}).get("exempt"))

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
        repair_exempt=repair_exempt,
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

# What each execution intent asks the venue for, in the venue's own words
# (user ruling, 2026-09-11).
#
# The order type, the resting flag and the expiry were three fields nobody
# owned: the payload builder defaulted the type to GTC for both families, the
# flag was written on the intent and never sent, and no expiry existed at
# all.  So the venue was told the same thing for a rung meant to rest an hour
# and for a completion meant to fill now or not at all, and the difference
# between them lived only in this package's own vocabulary.
#
# One table, both sides read it:
#
# * ``MAKER`` -- an entry rung, or a rung continuing to rest.  GTD with an
#   expiry and post-only true.  Post-only is what makes the price the plan
#   judged the price the book pays; the expiry is what makes an order we
#   lose track of die on its own.  A resting order with neither is a
#   standing instruction with no end and no guarantee, which is what every
#   order this lane ever placed was.
# * ``TAKER_FOK`` -- an explicitly authorized immediate take, which today is
#   the designated completion of a broken ladder.  FOK with post-only false
#   and a price cap that must be stated: crossing without a cap is the one
#   shape this lane must never send, because the walked price is the whole
#   basis on which the completion was authorized.
ORDER_TYPE_GTD = "GTD"
ORDER_TYPE_GTC = "GTC"
ORDER_TYPE_FOK = "FOK"
EXECUTION_ORDER_BINDING_RULE = "execution_order_binding_v1"
_EXECUTION_ORDER_BINDING = {
    "MAKER": {
        "order_type": ORDER_TYPE_GTD,
        "post_only": True,
        "requires_expiration": True,
        "requires_price_cap": False,
    },
    "TAKER_FOK": {
        "order_type": ORDER_TYPE_FOK,
        "post_only": False,
        "requires_expiration": False,
        "requires_price_cap": True,
    },
}


def execution_order_binding_v1(execution_candidate: Any) -> dict[str, Any]:
    """The venue fields one execution intent is bound to.

    Read, never inferred, and there is exactly one of these: a second copy
    would let the intent that was minted and the order that was sent disagree
    about which of the two things this lane does it is doing.
    """

    family = str(execution_candidate or "").strip().upper()
    binding = _EXECUTION_ORDER_BINDING.get(family)
    if binding is None:
        raise ContractError("delta_v2_execution_order_binding_unknown_family")
    return {"rule": EXECUTION_ORDER_BINDING_RULE, "family": family, **binding}


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
    completion_repair: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Select one Maker leg per required bucket as one joint basket action.

    No proper subset is an admissible action.  Fill outcomes are evaluated
    jointly against the same baseline inventory so floor, CVaR, concentration,
    coverage and overfill remain basket-level quantities.
    """

    if len(actions) > execution_weights.maximum_candidates:
        raise ContractError("delta_v2_action_candidate_bound_exceeded")
    # Charter 116, decided by the caller and read here: the caller owns the
    # held shape, the target and the offers, so it is the only place that can
    # say whether this bundle repairs an illegal floor.  Absent or ``off``
    # reads as "not a repair", which is what every pre-ruling caller gets.
    repair_exempt = bool((completion_repair or {}).get("exempt"))
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
                repair_exempt=repair_exempt,
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
    "EXCHANGE_MINIMUM_ORDER_NOTIONAL_USDC",
    "EXCHANGE_LOT_TOP_UP_MODES",
    "raise_sub_lot_targets_v2",
    "venue_minimum_shares_v1",
    "hold_action_v2",
    "select_action_v2",
    "select_complete_maker_bundle_v2",
    "solve_reservation_price_v2",
    "solve_target_inventory_v2",
    "LADDER_BALANCE_CANDIDATE_ORIGIN",
    "ask_depth_cost_v1",
    "ladder_balance_targets_v1",
    "completion_143_targets_v1",
    "completion_143_exemption_v1",
]
