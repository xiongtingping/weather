"""Canonical incremental ``DeltaBasketScore`` objective kernel.

This module owns the only economic score used by both target optimization and
strict execution authorization.  It deliberately has no direction term and no
pending-order payout semantics:

* filled shares and filled cost form the realised ``J0`` baseline;
* expected newly filled shares and their mode-specific expected cost form
  ``J1``;
* confirmed pending shares/notional are execution state only.  They may reserve
  budget and contribute execution risk outside this function, but never enter
  scenario PnL, profit floor, coverage, or concentration.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Any, Mapping, Sequence

from .contracts import EPS, ContractError, content_sha256


OBJECTIVE_SCHEMA = "canonical_delta_basket_score_v1"
OBJECTIVE_KERNEL_VERSION = "delta_basket_score_kernel_v1"


def _finite(name: str, value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name}_invalid") from exc
    if not math.isfinite(number):
        raise ContractError(f"{name}_invalid")
    return number


def _nonnegative(name: str, value: Any) -> float:
    number = _finite(name, value)
    if number < 0.0:
        raise ContractError(f"{name}_negative")
    return number


def _canonical_nonnegative_map(
    name: str,
    raw: Mapping[str, Any],
    buckets: Sequence[str],
) -> dict[str, float]:
    unknown = set(raw) - set(buckets)
    if unknown:
        raise ContractError(f"{name}_bucket_unknown")
    return {
        bucket: _nonnegative(f"{name}_{bucket}", raw.get(bucket, 0.0))
        for bucket in buckets
    }


@dataclass(frozen=True)
class ObjectiveWeights:
    lambda_profit_floor: float
    lambda_downside: float
    lambda_shortfall: float
    lambda_breakthrough: float
    lambda_concentration: float
    lambda_execution: float

    def validate(self) -> None:
        for field, value in asdict(self).items():
            _nonnegative(f"objective_{field}", value)

    @property
    def content_sha256(self) -> str:
        self.validate()
        return content_sha256({
            "schema": "delta_basket_score_weights_v1",
            **asdict(self),
        })


def evaluate_objective_state(
    *,
    probabilities: Mapping[str, Any],
    filled_shares_by_bucket: Mapping[str, Any],
    filled_cost_by_bucket: Mapping[str, Any],
    action_shares_by_bucket: Mapping[str, Any],
    action_cost_by_bucket: Mapping[str, Any],
    protected_buckets: Sequence[str],
    shortfall_buckets: Sequence[str],
    breakthrough_buckets: Sequence[str],
    weights: ObjectiveWeights,
    execution_risk: float,
    concentration_scale_usdc: float,
) -> dict[str, Any]:
    """Evaluate one realised-plus-action state under the canonical ``J``.

    ``action_shares_by_bucket`` means actual FOK shares or conservative
    expected Maker fills.  Requested-but-unfilled Maker shares are not accepted
    here; their budget reservation and execution risk belong to hard-constraint
    state.
    """

    weights.validate()
    buckets = tuple(sorted(str(bucket) for bucket in probabilities))
    if not buckets:
        raise ContractError("objective_probability_scope_empty")
    if len(set(buckets)) != len(buckets):
        raise ContractError("objective_probability_bucket_duplicate")
    probability = {
        bucket: _nonnegative(
            f"objective_probability_{bucket}",
            probabilities[bucket],
        )
        for bucket in buckets
    }
    if abs(sum(probability.values()) - 1.0) > 1e-8:
        raise ContractError("objective_probability_mass_invalid")
    filled_shares = _canonical_nonnegative_map(
        "objective_filled_shares",
        filled_shares_by_bucket,
        buckets,
    )
    filled_cost = _canonical_nonnegative_map(
        "objective_filled_cost",
        filled_cost_by_bucket,
        buckets,
    )
    action_shares = _canonical_nonnegative_map(
        "objective_action_shares",
        action_shares_by_bucket,
        buckets,
    )
    action_cost = _canonical_nonnegative_map(
        "objective_action_cost",
        action_cost_by_bucket,
        buckets,
    )
    protected = tuple(sorted(set(str(item) for item in protected_buckets)))
    shortfall = tuple(sorted(set(str(item) for item in shortfall_buckets)))
    breakthrough = tuple(
        sorted(set(str(item) for item in breakthrough_buckets))
    )
    unknown = (
        set(protected) | set(shortfall) | set(breakthrough)
    ) - set(buckets)
    if unknown:
        raise ContractError("objective_risk_scope_unknown")
    if not protected:
        raise ContractError("objective_protected_scope_empty")
    protected_tail_scope = tuple(
        sorted(set(shortfall) | set(breakthrough))
    ) or protected
    scale = _nonnegative(
        "objective_concentration_scale_usdc",
        concentration_scale_usdc,
    )
    if scale <= EPS:
        raise ContractError("objective_concentration_scale_zero")
    execution = _nonnegative("objective_execution_risk", execution_risk)

    total_cost = sum(filled_cost.values()) + sum(action_cost.values())
    scenario_pnl = {
        bucket: (
            filled_shares[bucket]
            + action_shares[bucket]
            - total_cost
        )
        for bucket in buckets
    }
    expected_pnl = sum(
        probability[bucket] * scenario_pnl[bucket]
        for bucket in buckets
    )
    downside = sum(
        probability[bucket] * max(0.0, -scenario_pnl[bucket]) ** 2
        for bucket in buckets
    )
    profit_floor = min(scenario_pnl[bucket] for bucket in protected)
    protected_tail_loss = sum(
        probability[bucket] * max(0.0, -scenario_pnl[bucket])
        for bucket in protected_tail_scope
    )
    realised_shares = {
        bucket: filled_shares[bucket] + action_shares[bucket]
        for bucket in buckets
    }
    shortfall_coverage = sum(
        probability[bucket] * realised_shares[bucket]
        for bucket in shortfall
    )
    breakthrough_coverage = sum(
        probability[bucket] * realised_shares[bucket]
        for bucket in breakthrough
    )
    realised_cost = {
        bucket: filled_cost[bucket] + action_cost[bucket]
        for bucket in buckets
    }
    concentration = sum(
        (realised_cost[bucket] / scale) ** 2
        for bucket in buckets
    )
    score = (
        expected_pnl
        + weights.lambda_profit_floor * profit_floor
        - weights.lambda_downside * downside
        + weights.lambda_shortfall * shortfall_coverage
        + weights.lambda_breakthrough * breakthrough_coverage
        - weights.lambda_concentration * concentration
        - weights.lambda_execution * execution
    )
    identity = {
        "schema": OBJECTIVE_SCHEMA,
        "kernel_version": OBJECTIVE_KERNEL_VERSION,
        "weights_sha256": weights.content_sha256,
        "score": score,
        "expected_pnl": expected_pnl,
        "profit_floor": profit_floor,
        "downside": downside,
        "shortfall_coverage": shortfall_coverage,
        "breakthrough_coverage": breakthrough_coverage,
        "concentration": concentration,
        "execution_risk": execution,
        "protected_tail_loss": protected_tail_loss,
        "scenario_pnl": scenario_pnl,
        "total_realised_cost": total_cost,
        "protected_buckets": list(protected),
        "protected_tail_buckets": list(protected_tail_scope),
        "shortfall_buckets": list(shortfall),
        "breakthrough_buckets": list(breakthrough),
        "concentration_scale_usdc": scale,
        "probabilities": probability,
        "filled_shares_by_bucket": filled_shares,
        "filled_cost_by_bucket": filled_cost,
        "action_shares_by_bucket": action_shares,
        "action_cost_by_bucket": action_cost,
    }
    return {
        **identity,
        "content_sha256": content_sha256(identity),
    }


def evaluate_delta_objective(
    *,
    probabilities: Mapping[str, Any],
    filled_shares_by_bucket: Mapping[str, Any],
    filled_cost_by_bucket: Mapping[str, Any],
    action_shares_by_bucket: Mapping[str, Any],
    action_cost_by_bucket: Mapping[str, Any],
    protected_buckets: Sequence[str],
    shortfall_buckets: Sequence[str],
    breakthrough_buckets: Sequence[str],
    weights: ObjectiveWeights,
    baseline_execution_risk: float,
    action_execution_risk: float,
    concentration_scale_usdc: float,
) -> dict[str, Any]:
    """Return ``J0``, ``J1`` and the exact canonical delta decomposition."""

    baseline_execution = _nonnegative(
        "objective_baseline_execution_risk",
        baseline_execution_risk,
    )
    incremental_execution = _nonnegative(
        "objective_action_execution_risk",
        action_execution_risk,
    )
    baseline = evaluate_objective_state(
        probabilities=probabilities,
        filled_shares_by_bucket=filled_shares_by_bucket,
        filled_cost_by_bucket=filled_cost_by_bucket,
        action_shares_by_bucket={},
        action_cost_by_bucket={},
        protected_buckets=protected_buckets,
        shortfall_buckets=shortfall_buckets,
        breakthrough_buckets=breakthrough_buckets,
        weights=weights,
        execution_risk=baseline_execution,
        concentration_scale_usdc=concentration_scale_usdc,
    )
    proposed = evaluate_objective_state(
        probabilities=probabilities,
        filled_shares_by_bucket=filled_shares_by_bucket,
        filled_cost_by_bucket=filled_cost_by_bucket,
        action_shares_by_bucket=action_shares_by_bucket,
        action_cost_by_bucket=action_cost_by_bucket,
        protected_buckets=protected_buckets,
        shortfall_buckets=shortfall_buckets,
        breakthrough_buckets=breakthrough_buckets,
        weights=weights,
        execution_risk=baseline_execution + incremental_execution,
        concentration_scale_usdc=concentration_scale_usdc,
    )
    deltas = {
        "delta_expected_pnl": (
            proposed["expected_pnl"] - baseline["expected_pnl"]
        ),
        "delta_profit_floor": (
            proposed["profit_floor"] - baseline["profit_floor"]
        ),
        "delta_downside": proposed["downside"] - baseline["downside"],
        "delta_shortfall_coverage": (
            proposed["shortfall_coverage"]
            - baseline["shortfall_coverage"]
        ),
        "delta_breakthrough_coverage": (
            proposed["breakthrough_coverage"]
            - baseline["breakthrough_coverage"]
        ),
        "delta_concentration": (
            proposed["concentration"] - baseline["concentration"]
        ),
        "delta_execution_risk": (
            proposed["execution_risk"] - baseline["execution_risk"]
        ),
        "delta_protected_tail_loss": (
            proposed["protected_tail_loss"]
            - baseline["protected_tail_loss"]
        ),
    }
    delta_score = proposed["score"] - baseline["score"]
    reconstructed = (
        deltas["delta_expected_pnl"]
        + weights.lambda_profit_floor * deltas["delta_profit_floor"]
        - weights.lambda_downside * deltas["delta_downside"]
        + weights.lambda_shortfall
        * deltas["delta_shortfall_coverage"]
        + weights.lambda_breakthrough
        * deltas["delta_breakthrough_coverage"]
        - weights.lambda_concentration * deltas["delta_concentration"]
        - weights.lambda_execution * deltas["delta_execution_risk"]
    )
    if abs(delta_score - reconstructed) > 1e-8:
        raise ContractError("objective_delta_decomposition_mismatch")

    probability = {
        str(bucket): _nonnegative(
            f"objective_probability_{bucket}",
            value,
        )
        for bucket, value in probabilities.items()
    }
    action_shares = _canonical_nonnegative_map(
        "objective_action_shares",
        action_shares_by_bucket,
        tuple(sorted(probability)),
    )
    action_cost = _canonical_nonnegative_map(
        "objective_action_cost",
        action_cost_by_bucket,
        tuple(sorted(probability)),
    )
    delta_ev_by_bucket = {
        bucket: (
            probability[bucket] * action_shares[bucket]
            - action_cost[bucket]
        )
        for bucket in sorted(probability)
        if action_shares[bucket] > EPS or action_cost[bucket] > EPS
    }
    delta_ev_sum = sum(delta_ev_by_bucket.values())
    delta_ev_reconciliation_slack = (
        deltas["delta_expected_pnl"] - delta_ev_sum
    )
    if abs(delta_ev_reconciliation_slack) > 1e-8:
        raise ContractError("objective_delta_ev_reconciliation_mismatch")

    # Keep the incremental settlement curve explicit in the canonical audit.
    # For an action with total all-in cost C, outcome b receives only the
    # action shares in b and pays C for every outcome:
    #
    #   hit(b):  x_b - C       (one-bucket case: x * (1-a))
    #   miss:   -C
    #
    # This is deliberately derived from action shares/costs, never from a
    # reciprocal price, reservation value, direction, or model envelope.
    total_action_cost = sum(action_cost.values())
    action_scenario_pnl = {
        bucket: action_shares[bucket] - total_action_cost
        for bucket in sorted(probability)
    }
    delta_scenario_pnl = {
        bucket: (
            proposed["scenario_pnl"][bucket]
            - baseline["scenario_pnl"][bucket]
        )
        for bucket in sorted(probability)
    }
    if any(
        abs(
            delta_scenario_pnl[bucket]
            - action_scenario_pnl[bucket]
        ) > 1e-8
        for bucket in sorted(probability)
    ):
        raise ContractError("objective_delta_scenario_pnl_mismatch")
    miss_cost = -total_action_cost
    if any(
        abs(
            (action_scenario_pnl[bucket] - action_shares[bucket])
            - miss_cost
        ) > 1e-8
        for bucket in sorted(probability)
    ):
        raise ContractError("objective_action_settlement_curve_mismatch")

    identity = {
        "schema": OBJECTIVE_SCHEMA,
        "kernel_version": OBJECTIVE_KERNEL_VERSION,
        "weights_sha256": weights.content_sha256,
        "baseline_score": baseline["score"],
        "proposed_score": proposed["score"],
        "delta_basket_score": delta_score,
        **deltas,
        "delta_ev_by_bucket": delta_ev_by_bucket,
        "delta_ev_sum": delta_ev_sum,
        "delta_ev_reconciliation_slack": delta_ev_reconciliation_slack,
        "total_action_cost": total_action_cost,
        "action_scenario_pnl": action_scenario_pnl,
        "action_miss_cost": miss_cost,
        "delta_scenario_pnl": delta_scenario_pnl,
        "baseline_content_sha256": baseline["content_sha256"],
        "proposed_content_sha256": proposed["content_sha256"],
    }
    return {
        **identity,
        "baseline": baseline,
        "proposed": proposed,
        "content_sha256": content_sha256(identity),
    }


__all__ = [
    "OBJECTIVE_KERNEL_VERSION",
    "OBJECTIVE_SCHEMA",
    "ObjectiveWeights",
    "evaluate_delta_objective",
    "evaluate_objective_state",
]
