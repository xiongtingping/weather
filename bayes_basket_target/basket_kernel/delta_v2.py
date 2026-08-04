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
import math
from typing import Any, Mapping, Sequence

from .contracts import EPS, ContractError, content_sha256


VALUE_SCHEMA = "delta_basket_value_v2"
TARGET_SOLUTION_SCHEMA = "delta_basket_target_inventory_v2"
ACTION_SCHEMA = "delta_basket_action_v2"
ACTION_EVALUATION_SCHEMA = "delta_basket_action_evaluation_v2"
ACTION_SELECTION_SCHEMA = "delta_basket_action_selection_v2"
BUNDLE_EVALUATION_SCHEMA = "delta_basket_bundle_evaluation_v2"
BUNDLE_SELECTION_SCHEMA = "delta_basket_bundle_selection_v2"
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


def solve_target_inventory_v2(
    *,
    probabilities: Mapping[str, Any],
    feasible_inventory_candidates: Sequence[Mapping[str, Any]],
    protected_buckets: Sequence[str],
    shortfall_buckets: Sequence[str],
    breakthrough_buckets: Sequence[str],
    weights: ValueWeightsV2,
    maximum_candidates: int = 4096,
) -> dict[str, Any]:
    """Return the exact argmax over a caller-provided bounded feasible set."""

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
            "audit": audit,
        })
    winner = max(
        evaluations,
        key=lambda row: (
            float(row["audit"]["value"]),
            -sum(row["audit"]["cost_by_bucket"].values()),
            row["candidate_id"],
        ),
    )
    identity = {
        "schema": TARGET_SOLUTION_SCHEMA,
        "kernel_version": KERNEL_VERSION,
        "candidate_count": len(evaluations),
        "winner_candidate_id": winner["candidate_id"],
        "winner_candidate_index": winner["candidate_index"],
        "winner_candidate_kind": winner["candidate_kind"],
        "target_shares_by_bucket": winner["audit"]["shares_by_bucket"],
        "target_cost_by_bucket": winner["audit"]["cost_by_bucket"],
        "target_value": winner["audit"]["value"],
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
        if mode != "MAKER":
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
            reserved = sum(
                float(row.get("capital_reserved_usdc") or 0.0)
                for row in combo
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
            joint_rows: list[dict[str, Any]] = []
            expected_delta = 0.0
            for outcomes in product(*distributions):
                outcome_count += 1
                if outcome_count > resource_bound["outcome_row_limit"]:
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
                    filled_shares = float(action["maximum_shares"]) * fraction
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
                joint_rows.append({
                    "probability": probability,
                    "fills": fills,
                    "delta_value": delta,
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
            score = expected_delta - execution_cost
            expected_shares = dict(current_shares)
            for evaluation in combo:
                action = dict(evaluation["action"])
                bucket = str(action["bucket"])
                expected_fraction = sum(
                    float(outcome["probability"])
                    * float(outcome["fill_fraction"])
                    for outcome in evaluation["fill_distribution"]
                )
                expected_shares[bucket] += (
                    float(action["maximum_shares"]) * expected_fraction
                )
            fit = sum(
                max(0.0, target[bucket] - expected_shares[bucket]) ** 2
                for bucket in required
            )
            authorized = (
                not structural_blockers
                and score > execution_weights.epsilon_score + EPS
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
            evaluated.append({
                **identity,
                "content_sha256": content_sha256(identity),
            })

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
            *[f"missing_maker_candidate:{bucket}" for bucket in missing],
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
    "evaluate_action_v2",
    "evaluate_basket_value_v2",
    "hold_action_v2",
    "select_action_v2",
    "select_complete_maker_bundle_v2",
    "solve_reservation_price_v2",
    "solve_target_inventory_v2",
]
