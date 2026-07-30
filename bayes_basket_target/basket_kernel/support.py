"""Probability and support diagnostics shared by the production basket runtime."""

from __future__ import annotations

import math
import json
from typing import Iterable, Mapping

from .contracts import (
    EPS,
    BucketObservation,
    ContractError,
    Direction,
    TargetBasketPolicy,
    TargetBasketSnapshot,
)
from ..continuous_core import (
    ContinuousCoreError,
    ContinuousCorePolicy,
    CoreResolutionState,
    ModelCenter,
    PeripheralRiskCandidate,
    resolve_continuous_core,
)


def _normalized_entropy(probabilities: Iterable[float]) -> float:
    values = [float(value) for value in probabilities if float(value) > EPS]
    if len(values) <= 1:
        return 0.0
    entropy = -sum(value * math.log(value) for value in values)
    return min(1.0, max(0.0, entropy / math.log(len(values))))


def _validated_probability_vector(
    probabilities: Mapping[str, float],
    ordinals: Mapping[str, int],
    *,
    contract: str,
) -> dict[str, float]:
    if not probabilities or set(probabilities) != set(ordinals):
        raise ContractError(f"{contract}_probability_vector_incomplete")
    if (
        len(set(ordinals.values())) != len(ordinals)
        or any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in ordinals.values()
        )
    ):
        raise ContractError(f"{contract}_bucket_ordinals_invalid")
    parsed: dict[str, float] = {}
    for bucket, raw in probabilities.items():
        if isinstance(raw, bool):
            raise ContractError(f"{contract}_probability_vector_invalid")
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise ContractError(
                f"{contract}_probability_vector_invalid"
            ) from exc
        if not math.isfinite(value) or value < 0.0:
            raise ContractError(f"{contract}_probability_vector_invalid")
        parsed[bucket] = value
    total = sum(parsed.values())
    if total <= EPS or abs(total - 1.0) > 1e-8:
        raise ContractError(f"{contract}_probability_vector_not_normalized")
    return parsed


def expected_direction_from_probability_vector(
    probabilities: Mapping[str, float],
    ordinals: Mapping[str, int],
    policy: TargetBasketPolicy,
    *,
    evidence_fresh: bool = True,
) -> Direction:
    """Resolve B05/B06 from the current stage probability vector only.

    The market is intentionally absent from this function. B06 disables the
    bias direction when the stage is stale, too entropic, or lacks enough
    probability mass on the direction side; the caller may still use the
    neutral/base basket, but cannot manufacture a directional bias.
    """

    parsed = _validated_probability_vector(
        probabilities,
        ordinals,
        contract="direction",
    )
    center_bucket, _runner_up, _lead, center_stable = probability_center_diagnostics(
        parsed,
        ordinals,
        policy,
    )
    if not center_stable:
        return Direction.NEUTRAL
    center = int(ordinals[center_bucket])
    lower = sum(
        float(value)
        for bucket, value in parsed.items()
        if int(ordinals[bucket]) < center
    )
    upper = sum(
        float(value)
        for bucket, value in parsed.items()
        if int(ordinals[bucket]) > center
    )
    skew = upper - lower
    if not evidence_fresh or abs(skew) < policy.direction_skew_deadband:
        return Direction.NEUTRAL
    if _normalized_entropy(parsed.values()) > policy.direction_entropy_max + EPS:
        return Direction.NEUTRAL
    side_mass = upper if skew > 0.0 else lower
    if side_mass + EPS < policy.direction_min_side_mass:
        return Direction.NEUTRAL
    return Direction.UP if skew > 0.0 else Direction.DOWN


def probability_center_diagnostics(
    probabilities: Mapping[str, float],
    ordinals: Mapping[str, int],
    policy: TargetBasketPolicy,
) -> tuple[str, str | None, float, bool]:
    """Resolve nominal center and the probability-only stability gate.

    The diagnostic model envelope is intentionally absent. It owns candidate
    boundaries and disagreement breadth, never center stability or direction.
    """

    parsed = _validated_probability_vector(
        probabilities,
        ordinals,
        contract="center",
    )
    ranked = sorted(
        parsed,
        key=lambda bucket: (
            -parsed[bucket],
            int(ordinals[bucket]),
            str(bucket),
        ),
    )
    center_bucket = ranked[0]
    runner_up = ranked[1] if len(ranked) > 1 else None
    lead = (
        parsed[center_bucket] - parsed[runner_up]
        if runner_up is not None
        else 1.0
    )
    lead = max(0.0, lead)
    stable = lead + EPS >= policy.center_lead_min_probability
    return center_bucket, runner_up, lead, stable


def _center(buckets: tuple[BucketObservation, ...]) -> BucketObservation:
    return min(
        buckets,
        key=lambda item: (-item.probability, item.ordinal, item.bucket),
    )


def _direction(
    buckets: tuple[BucketObservation, ...],
    center: BucketObservation,
    policy: TargetBasketPolicy,
    *,
    evidence_fresh: bool,
) -> Direction:
    del center
    return expected_direction_from_probability_vector(
        {item.bucket: item.probability for item in buckets},
        {item.bucket: item.ordinal for item in buckets},
        policy,
        evidence_fresh=evidence_fresh,
    )


def _derive_dual_risk_scope(
    snapshot: TargetBasketSnapshot,
    center: BucketObservation,
) -> tuple[set[str], set[str]]:
    """Derive at most two adjacent buckets for each P1 risk family."""

    if snapshot.evidence.probability_stage != "P1":
        return set(), set()
    reported_bucket = snapshot.evidence.reported_high_bucket
    reported_ordinal = snapshot.evidence.reported_high_ordinal
    if reported_bucket is None or reported_ordinal is None:
        raise ContractError("p1_reported_high_risk_lineage_required")
    ordered = tuple(
        sorted(
            snapshot.buckets,
            key=lambda item: (item.ordinal, item.bucket),
        )
    )
    indexes = {item.bucket: index for index, item in enumerate(ordered)}
    if (
        reported_bucket not in indexes
        or ordered[indexes[reported_bucket]].ordinal != reported_ordinal
    ):
        raise ContractError("reported_high_risk_scope_mismatch")
    center_index = indexes[center.bucket]
    reported_index = indexes[reported_bucket]
    underreach: set[str] = set()
    if reported_ordinal < center.ordinal:
        underreach = {
            ordered[index].bucket
            for index in range(max(0, center_index - 2), center_index)
            if ordered[index].probability > EPS
        }
    # Successors of the reported high are genuine upside-breakthrough risk
    # only after the official trajectory has reached (or exceeded) the
    # current probability center. While R < M, successor(R) buckets are
    # merely intermediate temperatures on the path to M; adding them would
    # create a second, reported-high-centered support and can pull very remote
    # buckets into the basket. The underreach family already covers the
    # bounded lower tail as predecessor(M), predecessor²(M).
    breakthrough: set[str] = set()
    if reported_ordinal >= center.ordinal:
        breakthrough = {
            ordered[index].bucket
            for index in range(
                reported_index + 1,
                min(len(ordered), reported_index + 3),
            )
            if ordered[index].probability > EPS
        }
    return underreach, breakthrough


def _choose_support(
    snapshot: TargetBasketSnapshot,
    policy: TargetBasketPolicy,
) -> tuple[
    BucketObservation,
    Direction,
    set[str],
    set[str],
    set[str],
    set[str],
    dict[str, tuple[float, float, float]],
    int,
    int,
    int,
]:
    buckets = tuple(
        sorted(snapshot.buckets, key=lambda item: (item.ordinal, item.bucket))
    )
    center = _center(buckets)
    direction = _direction(
        buckets,
        center,
        policy,
        evidence_fresh=snapshot.evidence_fresh,
    )
    entropy = _normalized_entropy(item.probability for item in buckets)
    coverage_target = min(
        policy.maximum_coverage,
        policy.base_coverage + policy.entropy_coverage_gain * entropy,
    )
    formal_members_json = snapshot.evidence.formal_model_members_json
    if formal_members_json is not None:
        try:
            formal_rows = json.loads(formal_members_json)
            members = tuple(
                ModelCenter(
                    model_id=str(row["model_id"]),
                    calibrated_center_bucket=str(
                        row["calibrated_center_bucket"]
                    ),
                    weight=float(row["weight"]),
                    formal_member=True,
                )
                for row in formal_rows
            )
        except (
            TypeError,
            ValueError,
            KeyError,
            json.JSONDecodeError,
        ) as exc:
            raise ContractError("formal_model_members_invalid") from exc
        expansion_layers = min(
            policy.max_disagreement_expansion_layers,
            int(
                math.ceil(
                    snapshot.evidence.forecast_dispersion_c
                    / policy.dispersion_c_per_expansion_layer
                )
            ),
        )
        _center_bucket, _runner, _lead, center_stable = (
            probability_center_diagnostics(
                {item.bucket: item.probability for item in buckets},
                {item.bucket: item.ordinal for item in buckets},
                policy,
            )
        )
        max_distance = max(
            1,
            max(abs(item.ordinal - center.ordinal) for item in buckets),
        )
        risk_candidates = []
        for item in buckets:
            first_share_all_in = (
                item.ask_price * (1.0 + policy.fee_rate)
                + policy.taker_slippage_buffer_per_share
            )
            risk_candidates.append(
                PeripheralRiskCandidate(
                    bucket=item.bucket,
                    probability_mass=item.probability,
                    model_reliability=item.peripheral_model_reliability,
                    price_coverage_value=max(
                        0.0, item.probability - first_share_all_in
                    ),
                    target_inventory=1.0,
                    evidence_eligible=(
                        item.probability + EPS
                        >= policy.edge_min_probability
                        and item.ask_price <= policy.edge_max_ask + EPS
                    ),
                    maker_available=item.ask_available,
                )
            )
        probability_core_selected: set[str] = set()
        probability_core_mass = 0.0
        for item in sorted(
            buckets,
            key=lambda row: (
                -row.probability,
                row.ordinal,
                row.bucket,
            ),
        ):
            probability_core_selected.add(item.bucket)
            probability_core_mass += item.probability
            if probability_core_mass + EPS >= coverage_target:
                break
        probability_core_lower = min(
            item.ordinal
            for item in buckets
            if item.bucket in probability_core_selected
        )
        probability_core_upper = max(
            item.ordinal
            for item in buckets
            if item.bucket in probability_core_selected
        )
        proposed_probability_core = tuple(
            item.bucket
            for item in buckets
            if probability_core_lower <= item.ordinal <= probability_core_upper
        )
        try:
            resolved = resolve_continuous_core(
                bucket_ordinals={
                    item.bucket: item.ordinal for item in buckets
                },
                probabilities={
                    item.bucket: item.probability for item in buckets
                },
                members=members,
                stable_center_bucket=center.bucket,
                center_stable=center_stable,
                direction=direction,
                policy=ContinuousCorePolicy(
                    max_peripheral_layers=expansion_layers,
                    min_peripheral_probability_mass=(
                        policy.edge_min_probability
                    ),
                    min_peripheral_model_reliability=(
                        policy.peripheral_model_reliability_min
                    ),
                    min_peripheral_price_coverage_value=(
                        policy.peripheral_min_coverage_value
                    ),
                ),
                core_probability_mass_target=coverage_target,
                proposed_core_buckets=proposed_probability_core,
                risk_candidates=risk_candidates,
                maker_available_by_bucket={
                    item.bucket: item.ask_available for item in buckets
                },
                filled_inventory_by_bucket={
                    item.bucket: item.filled_shares for item in buckets
                },
                pending_inventory_by_bucket={
                    item.bucket: item.pending_shares for item in buckets
                },
                current_core_buckets=snapshot.current_core_buckets,
                current_direction=snapshot.current_direction,
                order_state_known=snapshot.order_state_known,
                stop_adding=snapshot.terminal,
            )
        except ContinuousCoreError as exc:
            raise ContractError(f"continuous_core_invalid:{exc}") from exc
        if (
            resolved.resolution_state
            in {
                CoreResolutionState.BLOCKED,
                CoreResolutionState.HOLD,
                CoreResolutionState.UNREPRESENTABLE,
            }
            or resolved.support_set is None
            or resolved.core_interval is None
            or resolved.candidate_boundary is None
        ):
            raise ContractError(
                "continuous_core_hold:" + resolved.reason
            )
        base = set(resolved.support_set.core_buckets)
        selected_risk = set(resolved.support_set.optional_risk_buckets)
        bias = {
            bucket
            for bucket in selected_risk
            if (
                direction is Direction.UP
                and next(
                    item.ordinal
                    for item in buckets
                    if item.bucket == bucket
                )
                > center.ordinal
            )
            or (
                direction is Direction.DOWN
                and next(
                    item.ordinal
                    for item in buckets
                    if item.bucket == bucket
                )
                < center.ordinal
            )
        }
        edge = selected_risk - bias
        desired = base | bias | edge
        scores: dict[str, tuple[float, float, float]] = {}
        for item in buckets:
            base_score = item.probability if item.bucket in base else 0.0
            bias_score = (
                item.probability
                * abs(item.ordinal - center.ordinal)
                / max_distance
                if item.bucket in bias
                else 0.0
            )
            edge_score = (
                item.probability
                * (1.0 - item.ask_price)
                / item.ask_price
                * abs(item.ordinal - center.ordinal)
                / max_distance
                if item.bucket in edge
                else 0.0
            )
            scores[item.bucket] = (
                base_score,
                bias_score,
                edge_score,
            )
        ordinal_by_bucket = {
            item.bucket: item.ordinal for item in buckets
        }
        return (
            center,
            resolved.effective_direction,
            base,
            bias,
            edge,
            desired,
            scores,
            ordinal_by_bucket[
                resolved.candidate_boundary.lower_bucket
            ],
            ordinal_by_bucket[
                resolved.candidate_boundary.upper_bucket
            ],
            expansion_layers,
            {
                "resolution_state": resolved.resolution_state.value,
                "transition": resolved.transition.value,
                "direction_transition": (
                    resolved.direction_transition.value
                ),
                "current_direction": (
                    snapshot.current_direction.value
                    if snapshot.current_direction is not None
                    else None
                ),
                "next_direction": resolved.effective_direction.value,
                "reason": resolved.reason,
                "current_core_buckets": list(
                    snapshot.current_core_buckets
                ),
                "next_core_buckets": list(
                    resolved.support_set.core_buckets
                ),
            },
        )

    base: set[str] = set()
    mass = 0.0
    for item in sorted(
        buckets,
        key=lambda row: (-row.probability, row.ordinal, row.bucket),
    ):
        base.add(item.bucket)
        mass += item.probability
        if mass + EPS >= coverage_target:
            break
    # Diagnostic extrema bound the outer search range, but never define the
    # probability core and never add a bucket to final support.  A remote
    # outlier is still capped by the disagreement-derived layer budget.
    envelope_lower = snapshot.evidence.model_envelope_lower_ordinal
    envelope_upper = snapshot.evidence.model_envelope_upper_ordinal
    if (envelope_lower is None) != (envelope_upper is None):
        raise ContractError("model_envelope_boundary_incomplete")
    if envelope_lower is not None and envelope_upper is not None:
        ordinals = {item.ordinal for item in buckets}
        if envelope_lower not in ordinals or envelope_upper not in ordinals:
            raise ContractError(
                "model_envelope_boundary_outside_probability_support"
            )
        if envelope_lower > envelope_upper:
            raise ContractError("model_envelope_boundary_invalid")
    ordinal_by_bucket = {item.bucket: item.ordinal for item in buckets}
    core_lower = min(ordinal_by_bucket[bucket] for bucket in base)
    core_upper = max(ordinal_by_bucket[bucket] for bucket in base)
    expansion_layers = min(
        policy.max_disagreement_expansion_layers,
        int(
            math.ceil(
                snapshot.evidence.forecast_dispersion_c
                / policy.dispersion_c_per_expansion_layer
            )
        ),
    )
    support_lower = min(item.ordinal for item in buckets)
    support_upper = max(item.ordinal for item in buckets)
    anchor_lower = (
        core_lower if envelope_lower is None else envelope_lower
    )
    anchor_upper = (
        core_upper if envelope_upper is None else envelope_upper
    )
    candidate_lower = (
        core_lower
        if anchor_lower >= core_lower
        else max(
            support_lower,
            anchor_lower,
            core_lower - expansion_layers,
        )
    )
    candidate_upper = (
        core_upper
        if anchor_upper <= core_upper
        else min(
            support_upper,
            anchor_upper,
            core_upper + expansion_layers,
        )
    )

    max_distance = max(
        1,
        max(abs(item.ordinal - center.ordinal) for item in buckets),
    )

    def peripheral_eligible(item: BucketObservation) -> bool:
        if not candidate_lower <= item.ordinal <= candidate_upper:
            return False
        if item.probability + EPS < policy.edge_min_probability:
            return False
        if not item.ask_available:
            return False
        if item.ask_price > policy.edge_max_ask + EPS:
            return False
        if (
            item.peripheral_model_reliability + EPS
            < policy.peripheral_model_reliability_min
        ):
            return False
        first_share_all_in = (
            item.ask_price * (1.0 + policy.fee_rate)
            + policy.taker_slippage_buffer_per_share
        )
        coverage_value = max(0.0, item.probability - first_share_all_in)
        return coverage_value + EPS >= policy.peripheral_min_coverage_value

    bias_scores: dict[str, float] = {}
    if direction is not Direction.NEUTRAL:
        for item in buckets:
            if item.bucket in base:
                continue
            if not peripheral_eligible(item):
                continue
            signed_distance = item.ordinal - center.ordinal
            aligned = (
                signed_distance > 0
                if direction is Direction.UP
                else signed_distance < 0
            )
            if aligned:
                bias_scores[item.bucket] = (
                    item.probability * abs(signed_distance) / max_distance
                )
    bias = set(bias_scores)

    edge_scores: dict[str, float] = {}
    for item in buckets:
        if item.bucket in base or item.bucket in bias:
            continue
        if not peripheral_eligible(item):
            continue
        distance = abs(item.ordinal - center.ordinal) / max_distance
        edge_scores[item.bucket] = (
            item.probability
            * (1.0 - item.ask_price)
            / item.ask_price
            * distance
        )
    edge = set(edge_scores)

    # Final support may be sparse. Historical holdings remain inventory facts,
    # but neither they nor two selected endpoints force an internal gap into
    # the new target support.
    desired = set(base) | set(bias) | set(edge)
    if any(
        not candidate_lower <= ordinal_by_bucket[bucket] <= candidate_upper
        for bucket in desired
    ):
        raise ContractError("final_support_outside_candidate_boundary")

    scores: dict[str, tuple[float, float, float]] = {}
    for item in buckets:
        if item.bucket not in desired:
            scores[item.bucket] = (0.0, 0.0, 0.0)
            continue
        base_score = item.probability if item.bucket in base else 0.0
        bias_score = (
            bias_scores.get(item.bucket, 0.0) if item.bucket in bias else 0.0
        )
        edge_score = (
            edge_scores.get(item.bucket, 0.0) if item.bucket in edge else 0.0
        )
        scores[item.bucket] = (base_score, bias_score, edge_score)
    return (
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
        {
            "resolution_state": "APPLIED",
            "transition": (
                "INITIALIZE"
                if not snapshot.current_core_buckets
                else "KEEP"
            ),
            "direction_transition": (
                "INITIALIZE"
                if snapshot.current_direction is None
                else "KEEP"
                if snapshot.current_direction is direction
                else "NEUTRALIZE"
                if direction is Direction.NEUTRAL
                else "MIGRATE"
            ),
            "current_direction": (
                snapshot.current_direction.value
                if snapshot.current_direction is not None
                else None
            ),
            "next_direction": direction.value,
            "reason": "legacy_probability_core_without_formal_anchor",
            "current_core_buckets": list(
                snapshot.current_core_buckets
            ),
            "next_core_buckets": [
                item.bucket
                for item in sorted(
                    buckets,
                    key=lambda row: (row.ordinal, row.bucket),
                )
                if item.bucket in base
            ],
        },
    )


__all__ = [
    "_choose_support",
    "_derive_dual_risk_scope",
    "_normalized_entropy",
    "expected_direction_from_probability_vector",
    "probability_center_diagnostics",
]
