"""Pure B13 execution hard gate and B15 trigger/urgency evaluation."""

from __future__ import annotations

from typing import Mapping

from .contracts import (
    EPS,
    TargetBasketPolicy,
    TargetBasketSnapshot,
    BucketObservation,
    ExecutionCandidate,
    ExecutionGateDecision,
    TargetPriceResult,
    UrgencyEvaluation,
    content_sha256,
)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def evaluate_urgency(
    snapshot: TargetBasketSnapshot,
    policy: TargetBasketPolicy,
    observation: BucketObservation,
    *,
    target_shares: float,
    delta_shares: float,
) -> UrgencyEvaluation:
    """Evaluate B15 without mutating probability, direction, inventory, or orders."""

    target_shares = float(target_shares)
    delta_shares = float(delta_shares)

    time_component = _clamp(
        (policy.taker_time_threshold_minutes - snapshot.time_left_minutes)
        / policy.taker_time_threshold_minutes
    )
    gap_component = _clamp(delta_shares / max(target_shares, EPS))
    fill_component = _clamp(1.0 - snapshot.recent_fill_rate)
    spread = max(0.0, observation.ask_price - observation.bid_price)
    spread_component = _clamp(1.0 - spread / policy.taker_spread_reference)
    depth_component = (
        _clamp(observation.executable_depth_shares / delta_shares)
        if delta_shares > EPS
        else 0.0
    )
    components = {
        "time": time_component,
        "gap": gap_component,
        "fill": fill_component,
        "spread": spread_component,
        "depth": depth_component,
    }
    weights = {
        "time": policy.urgency_weight_time,
        "gap": policy.urgency_weight_gap,
        "fill": policy.urgency_weight_fill,
        "spread": policy.urgency_weight_spread,
        "depth": policy.urgency_weight_depth,
    }
    score = _clamp(sum(components[key] * weights[key] for key in components))
    trigger_flags = {
        "checkpoint": snapshot.checkpoint_changed,
        "quote": snapshot.quote_changed,
        "fill": snapshot.fill_changed,
        "time_gate": snapshot.time_gate_changed,
    }
    trigger_changed = any(trigger_flags.values())
    urgent = score + EPS >= policy.taker_urgency_threshold
    reasons = [key + "_trigger_changed" for key, changed in trigger_flags.items() if changed]
    if not reasons:
        reasons.append("trigger_vector_unchanged")
    reasons.append("taker_urgency_threshold_passed" if urgent else "maker_preferred_below_urgency_threshold")
    core = {
        "schema": "target_urgency_evaluation_v1",
        "snapshot_sha256": snapshot.content_sha256,
        "policy_sha256": content_sha256(policy.to_dict()),
        "bucket": observation.bucket,
        "target_shares": target_shares,
        "delta_shares": delta_shares,
        "spread": spread,
        "total_ask_depth_shares": observation.executable_depth_shares,
        "components": components,
        "weights": weights,
        "urgency_score": score,
        "urgency_threshold": policy.taker_urgency_threshold,
        "urgent": urgent,
        "trigger_flags": trigger_flags,
        "trigger_changed": trigger_changed,
        "reason_codes": reasons,
    }
    value = UrgencyEvaluation(
        urgency_id="urgency_" + content_sha256(core)[:32],
        snapshot_sha256=snapshot.content_sha256,
        policy_sha256=content_sha256(policy.to_dict()),
        bucket=observation.bucket,
        target_shares=target_shares,
        delta_shares=delta_shares,
        spread=spread,
        total_ask_depth_shares=observation.executable_depth_shares,
        components=components,
        weights=weights,
        urgency_score=score,
        urgency_threshold=policy.taker_urgency_threshold,
        urgent=urgent,
        trigger_flags=trigger_flags,
        trigger_changed=trigger_changed,
        reason_codes=tuple(reasons),
    )
    return UrgencyEvaluation.from_mapping(value.to_dict())


def expected_version_vector(
    snapshot: TargetBasketSnapshot,
    pricing: TargetPriceResult,
) -> dict[str, str]:
    return {
        "posterior_version": snapshot.evidence.posterior_version,
        "posterior_sha256": snapshot.evidence.posterior_sha256,
        "snapshot_sha256": snapshot.content_sha256,
        "quote_version": snapshot.quote_version,
        "inventory_version": snapshot.inventory_version,
        "order_state_version": snapshot.order_state_version,
        "target_price_id": pricing.pricing_id,
    }


def evaluate_execution_gate(
    snapshot: TargetBasketSnapshot,
    policy: TargetBasketPolicy,
    pricing: TargetPriceResult,
    urgency: UrgencyEvaluation,
    *,
    proposed_execution: ExecutionCandidate,
    proposed_notional_usdc: float,
    effective_price: float,
    candidate_versions: Mapping[str, str] | None = None,
) -> ExecutionGateDecision:
    """Materialize B13 ALLOW as a conjunction; any false factor fails closed."""

    proposed_notional_usdc = float(proposed_notional_usdc)
    effective_price = float(effective_price)

    expected = expected_version_vector(snapshot, pricing)
    candidate = (
        {str(key): str(value) for key, value in candidate_versions.items()}
        if candidate_versions is not None
        else dict(expected)
    )
    committed = sum(
        row.filled_cost_usdc + row.pending_notional_usdc for row in snapshot.buckets
    )
    evidence_fresh = snapshot.evidence_fresh
    contract_matches = snapshot.contract_matches
    budget_ok = committed + proposed_notional_usdc <= policy.total_budget_usdc + EPS
    order_state_known = snapshot.order_state_known
    risk_inputs_complete = snapshot.risk_inputs_complete
    risk_floor_ok = (
        pricing.feasible
        and pricing.minimum_protected_pnl_at_limit_usdc + EPS >= policy.protected_floor_usdc
        and pricing.expected_pnl_after_at_limit_usdc + EPS >= policy.minimum_expected_pnl_after_usdc
        and pricing.capital_after_at_limit_usdc <= policy.total_budget_usdc + EPS
    )
    delta_positive = urgency.delta_shares > EPS
    trigger_changed = urgency.trigger_changed
    clip_minimum_met = proposed_notional_usdc + EPS >= policy.clip_min_notional_usdc
    effective_price_ok = (
        effective_price > EPS
        and effective_price <= pricing.all_in_limit_cost_per_share + EPS
    )
    versions_match = candidate == expected
    candidate_present = (
        proposed_execution is not ExecutionCandidate.NONE
        and proposed_notional_usdc > EPS
        and effective_price > EPS
    )
    factors = {
        "evidence_fresh": evidence_fresh,
        "contract_matches": contract_matches,
        "budget_ok": budget_ok,
        "order_state_known": order_state_known,
        "risk_inputs_complete": risk_inputs_complete,
        "risk_floor_ok": risk_floor_ok,
        "delta_positive": delta_positive,
        "trigger_changed": trigger_changed,
        "clip_minimum_met": clip_minimum_met,
        "effective_price_ok": effective_price_ok,
        "versions_match": versions_match,
        "candidate_present": candidate_present,
    }
    allow = all(factors.values())
    reasons = [name + "_failed" for name, passed in factors.items() if not passed]
    if allow:
        reasons.append("b13_all_execution_factors_passed")
    core = {
        "schema": "target_execution_gate_decision_v2",
        "urgency_id": urgency.urgency_id,
        "snapshot_sha256": snapshot.content_sha256,
        "policy_sha256": content_sha256(policy.to_dict()),
        "bucket": urgency.bucket,
        "proposed_execution": proposed_execution.value,
        "proposed_notional_usdc": proposed_notional_usdc,
        "effective_price": effective_price,
        "all_in_limit_cost_per_share": pricing.all_in_limit_cost_per_share,
        "committed_notional_usdc": committed,
        "budget_usdc": policy.total_budget_usdc,
        "expected_versions": expected,
        "candidate_versions": candidate,
        **factors,
        "allow": allow,
        "reason_codes": reasons,
    }
    value = ExecutionGateDecision(
        gate_id="execution_gate_" + content_sha256(core)[:32],
        urgency_id=urgency.urgency_id,
        snapshot_sha256=snapshot.content_sha256,
        policy_sha256=content_sha256(policy.to_dict()),
        bucket=urgency.bucket,
        proposed_execution=proposed_execution,
        proposed_notional_usdc=proposed_notional_usdc,
        effective_price=effective_price,
        all_in_limit_cost_per_share=pricing.all_in_limit_cost_per_share,
        committed_notional_usdc=committed,
        budget_usdc=policy.total_budget_usdc,
        expected_versions=expected,
        candidate_versions=candidate,
        evidence_fresh=evidence_fresh,
        contract_matches=contract_matches,
        budget_ok=budget_ok,
        order_state_known=order_state_known,
        risk_inputs_complete=risk_inputs_complete,
        risk_floor_ok=risk_floor_ok,
        delta_positive=delta_positive,
        trigger_changed=trigger_changed,
        clip_minimum_met=clip_minimum_met,
        effective_price_ok=effective_price_ok,
        versions_match=versions_match,
        candidate_present=candidate_present,
        allow=allow,
        reason_codes=tuple(reasons),
    )
    return ExecutionGateDecision.from_mapping(value.to_dict())


__all__ = [
    "evaluate_execution_gate",
    "evaluate_urgency",
    "expected_version_vector",
]
