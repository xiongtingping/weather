"""Pure domain contract for continuous basket cores and directional migration.

The four concepts in this module are deliberately separate:

* ``ModelAnchorRange`` is the non-directional hull of calibrated centers from
  formal, positive-weight ensemble members.
* ``CoreInterval`` is a contiguous, positive-target interval selected by the
  combined probability vector and containing the stable probability center.
* ``CandidateBoundary`` is only the outer limit within which risk buckets may
  be considered.
* ``SupportSet`` is the core plus the individually qualified (possibly sparse)
  peripheral risk buckets.

No market order or persistence side effect belongs in this module.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Mapping, Sequence

from .basket_kernel.contracts import Direction


EPS = 1e-12


class ContinuousCoreError(ValueError):
    """Raised when the caller supplies a malformed domain contract."""


class CoreResolutionState(str, Enum):
    APPLIED = "APPLIED"
    NEUTRAL = "NEUTRAL"
    UNREPRESENTABLE = "UNREPRESENTABLE"
    BLOCKED = "BLOCKED"
    HOLD = "HOLD"


class CoreTransition(str, Enum):
    INITIALIZE = "INITIALIZE"
    MIGRATE = "MIGRATE"
    FILL_GAP = "FILL_GAP"
    KEEP = "KEEP"
    STOP_ADDING = "STOP_ADDING"
    HOLD = "HOLD"


class DirectionTransition(str, Enum):
    INITIALIZE = "INITIALIZE"
    MIGRATE = "MIGRATE"
    NEUTRALIZE = "NEUTRALIZE"
    KEEP = "KEEP"
    HOLD = "HOLD"


@dataclass(frozen=True)
class ModelCenter:
    model_id: str
    calibrated_center_bucket: str
    weight: float
    formal_member: bool = True

    def __post_init__(self) -> None:
        if not self.model_id.strip():
            raise ContinuousCoreError("model_id_required")
        if not math.isfinite(self.weight) or self.weight < 0.0:
            raise ContinuousCoreError("model_weight_invalid")


@dataclass(frozen=True)
class ModelAnchorRange:
    lower_bucket: str
    upper_bucket: str
    buckets: tuple[str, ...]
    member_ids: tuple[str, ...]
    member_weight_by_bucket: tuple[tuple[str, float], ...]


@dataclass(frozen=True)
class CoreInterval:
    lower_bucket: str
    upper_bucket: str
    buckets: tuple[str, ...]
    target_inventory: tuple[tuple[str, float], ...]


@dataclass(frozen=True)
class CandidateBoundary:
    lower_bucket: str
    upper_bucket: str
    buckets: tuple[str, ...]
    max_peripheral_layers: int


@dataclass(frozen=True)
class SupportSet:
    core_buckets: tuple[str, ...]
    optional_risk_buckets: tuple[str, ...]
    buckets: tuple[str, ...]
    target_inventory: tuple[tuple[str, float], ...]


@dataclass(frozen=True)
class PeripheralRiskCandidate:
    bucket: str
    probability_mass: float
    model_reliability: float
    price_coverage_value: float
    target_inventory: float
    evidence_eligible: bool = True
    maker_available: bool = True

    def __post_init__(self) -> None:
        values = (
            self.probability_mass,
            self.model_reliability,
            self.price_coverage_value,
            self.target_inventory,
        )
        if any(not math.isfinite(value) for value in values):
            raise ContinuousCoreError("risk_candidate_non_finite")
        if self.probability_mass < 0.0 or self.model_reliability < 0.0:
            raise ContinuousCoreError("risk_candidate_negative")
        if self.target_inventory < 0.0:
            raise ContinuousCoreError("risk_target_inventory_negative")


@dataclass(frozen=True)
class ContinuousCorePolicy:
    max_peripheral_layers: int = 2
    min_peripheral_probability_mass: float = 0.01
    min_peripheral_model_reliability: float = 0.50
    min_peripheral_price_coverage_value: float = 0.0
    default_core_target_inventory: float = 1.0
    formal_member_weight_floor: float = 0.0

    def __post_init__(self) -> None:
        integer_fields = (self.max_peripheral_layers,)
        if any(
            isinstance(value, bool) or value < 0 or value > 2
            for value in integer_fields
        ):
            raise ContinuousCoreError("continuous_core_layer_policy_invalid")
        bounded = (
            self.min_peripheral_probability_mass,
            self.min_peripheral_model_reliability,
            self.formal_member_weight_floor,
        )
        if any(not math.isfinite(value) or not 0.0 <= value <= 1.0 for value in bounded):
            raise ContinuousCoreError("continuous_core_probability_policy_invalid")
        if (
            not math.isfinite(self.min_peripheral_price_coverage_value)
            or not math.isfinite(self.default_core_target_inventory)
            or self.default_core_target_inventory <= 0.0
        ):
            raise ContinuousCoreError("continuous_core_value_policy_invalid")


@dataclass(frozen=True)
class ContinuousCoreResult:
    resolution_state: CoreResolutionState
    transition: CoreTransition
    direction_transition: DirectionTransition
    requested_direction: Direction
    effective_direction: Direction
    reason: str
    anchor_range: ModelAnchorRange | None
    core_interval: CoreInterval | None
    candidate_boundary: CandidateBoundary | None
    support_set: SupportSet | None
    trimmed_buckets: tuple[str, ...]
    rejected_risk_buckets: tuple[tuple[str, str], ...]
    positive_inventory_gap: tuple[tuple[str, float], ...]


@dataclass(frozen=True)
class _BucketAxis:
    buckets: tuple[str, ...]
    index: Mapping[str, int]

    @classmethod
    def from_ordinals(cls, bucket_ordinals: Mapping[str, int]) -> "_BucketAxis":
        if not bucket_ordinals:
            raise ContinuousCoreError("bucket_contract_empty")
        if any(
            not isinstance(bucket, str)
            or not bucket
            or isinstance(ordinal, bool)
            or not isinstance(ordinal, int)
            for bucket, ordinal in bucket_ordinals.items()
        ):
            raise ContinuousCoreError("bucket_contract_invalid")
        if len(set(bucket_ordinals.values())) != len(bucket_ordinals):
            raise ContinuousCoreError("bucket_ordinals_not_unique")
        buckets = tuple(
            bucket
            for bucket, _ in sorted(
                bucket_ordinals.items(),
                key=lambda item: (item[1], item[0]),
            )
        )
        return cls(
            buckets=buckets,
            index={bucket: index for index, bucket in enumerate(buckets)},
        )

    def interval(self, lower_index: int, upper_index: int) -> tuple[str, ...]:
        if lower_index < 0 or upper_index >= len(self.buckets) or lower_index > upper_index:
            raise ContinuousCoreError("bucket_interval_unrepresentable")
        return self.buckets[lower_index : upper_index + 1]


def _direction(value: Direction | str) -> Direction:
    if isinstance(value, Direction):
        return value
    try:
        return Direction(str(value).strip().upper())
    except ValueError as exc:
        raise ContinuousCoreError("direction_invalid") from exc


def _validate_probability_vector(
    probabilities: Mapping[str, float],
    axis: _BucketAxis,
) -> dict[str, float]:
    if set(probabilities) != set(axis.buckets):
        raise ContinuousCoreError("probability_vector_not_exact_bucket_contract")
    parsed: dict[str, float] = {}
    for bucket in axis.buckets:
        value = float(probabilities[bucket])
        if not math.isfinite(value) or value < 0.0:
            raise ContinuousCoreError("probability_vector_invalid")
        parsed[bucket] = value
    total = sum(parsed.values())
    if total <= EPS or abs(total - 1.0) > 1e-8:
        raise ContinuousCoreError("probability_vector_not_normalized")
    return parsed


def build_model_anchor_range(
    bucket_ordinals: Mapping[str, int],
    members: Iterable[ModelCenter],
    *,
    formal_member_weight_floor: float = 0.0,
) -> ModelAnchorRange:
    """Build the non-directional anchor from formal positive-weight members.

    Diagnostic/ablation members are ignored before their center bucket is
    inspected. Therefore an unrepresentable diagnostic outlier cannot pull or
    invalidate the formal anchor.
    """

    if (
        not math.isfinite(formal_member_weight_floor)
        or not 0.0 <= formal_member_weight_floor <= 1.0
    ):
        raise ContinuousCoreError("formal_member_weight_floor_invalid")
    axis = _BucketAxis.from_ordinals(bucket_ordinals)
    selected = tuple(
        member
        for member in members
        if member.formal_member
        and member.weight > EPS
        and member.weight + EPS >= formal_member_weight_floor
    )
    if len(selected) < 2:
        raise ContinuousCoreError("formal_anchor_requires_two_positive_members")
    if len({member.model_id for member in selected}) != len(selected):
        raise ContinuousCoreError("formal_member_id_duplicate")
    missing = sorted(
        member.calibrated_center_bucket
        for member in selected
        if member.calibrated_center_bucket not in axis.index
    )
    if missing:
        raise ContinuousCoreError(
            "formal_member_center_unrepresentable:" + ",".join(missing)
        )
    lower = min(axis.index[member.calibrated_center_bucket] for member in selected)
    upper = max(axis.index[member.calibrated_center_bucket] for member in selected)
    weight_by_bucket: dict[str, float] = {}
    for member in selected:
        weight_by_bucket[member.calibrated_center_bucket] = (
            weight_by_bucket.get(member.calibrated_center_bucket, 0.0)
            + member.weight
        )
    return ModelAnchorRange(
        lower_bucket=axis.buckets[lower],
        upper_bucket=axis.buckets[upper],
        buckets=axis.interval(lower, upper),
        member_ids=tuple(sorted(member.model_id for member in selected)),
        member_weight_by_bucket=tuple(
            (bucket, weight_by_bucket[bucket])
            for bucket in axis.buckets
            if bucket in weight_by_bucket
        ),
    )


def _result_without_structure(
    *,
    state: CoreResolutionState,
    direction: Direction,
    reason: str,
    anchor: ModelAnchorRange | None = None,
) -> ContinuousCoreResult:
    return ContinuousCoreResult(
        resolution_state=state,
        transition=(
            CoreTransition.HOLD
            if state in {
                CoreResolutionState.UNREPRESENTABLE,
                CoreResolutionState.BLOCKED,
                CoreResolutionState.HOLD,
            }
            else CoreTransition.KEEP
        ),
        direction_transition=DirectionTransition.HOLD,
        requested_direction=direction,
        effective_direction=Direction.NEUTRAL,
        reason=reason,
        anchor_range=anchor,
        core_interval=None,
        candidate_boundary=None,
        support_set=None,
        trimmed_buckets=(),
        rejected_risk_buckets=(),
        positive_inventory_gap=(),
    )


def _proposed_interval(
    proposed_core_buckets: Sequence[str],
    axis: _BucketAxis,
) -> tuple[int, int] | None:
    if not proposed_core_buckets:
        return None
    if len(set(proposed_core_buckets)) != len(proposed_core_buckets):
        raise ContinuousCoreError("proposed_core_duplicate_bucket")
    if any(bucket not in axis.index for bucket in proposed_core_buckets):
        raise ContinuousCoreError("proposed_core_unrepresentable")
    indexes = tuple(axis.index[bucket] for bucket in proposed_core_buckets)
    lower, upper = min(indexes), max(indexes)
    if tuple(proposed_core_buckets) != axis.interval(lower, upper):
        raise ContinuousCoreError("proposed_core_not_contiguous")
    return lower, upper


def _probability_core_bounds(
    *,
    center_index: int,
    axis: _BucketAxis,
    probabilities: Mapping[str, float],
    core_probability_mass_target: float,
    proposed_core_buckets: Sequence[str],
) -> tuple[int, int, str | None]:
    if (
        not math.isfinite(core_probability_mass_target)
        or not 0.0 < core_probability_mass_target <= 1.0
    ):
        raise ContinuousCoreError(
            "core_probability_mass_target_invalid"
        )
    lower = upper = center_index
    selected_mass = probabilities[axis.buckets[center_index]]
    lower_side_mass = 0.0
    upper_side_mass = 0.0
    while selected_mass + EPS < core_probability_mass_target:
        lower_probability = (
            probabilities[axis.buckets[lower - 1]]
            if lower > 0
            else -1.0
        )
        upper_probability = (
            probabilities[axis.buckets[upper + 1]]
            if upper + 1 < len(axis.buckets)
            else -1.0
        )
        if lower_probability < 0.0 and upper_probability < 0.0:
            raise ContinuousCoreError(
                "core_probability_mass_target_unreachable"
            )
        choose_lower = lower_probability > upper_probability + EPS
        if abs(lower_probability - upper_probability) <= EPS:
            # Equal adjacent probability is assigned to the side with less
            # mass already included. This keeps the core centered on the
            # combined distribution and makes warm/cold mirrors exact.
            choose_lower = lower_side_mass <= upper_side_mass
        if choose_lower:
            lower -= 1
            added = probabilities[axis.buckets[lower]]
            lower_side_mass += added
        else:
            upper += 1
            added = probabilities[axis.buckets[upper]]
            upper_side_mass += added
        selected_mass += added
    canonical = axis.interval(lower, upper)
    proposed = _proposed_interval(proposed_core_buckets, axis)
    if proposed is not None and tuple(proposed_core_buckets) != canonical:
        return lower, upper, "proposed_core_not_probability_selected"
    if not lower <= center_index <= upper:
        return lower, upper, "stable_center_outside_probability_core"
    return lower, upper, None


def resolve_continuous_core(
    *,
    bucket_ordinals: Mapping[str, int],
    probabilities: Mapping[str, float],
    members: Iterable[ModelCenter],
    stable_center_bucket: str,
    center_stable: bool,
    direction: Direction | str,
    policy: ContinuousCorePolicy = ContinuousCorePolicy(),
    core_probability_mass_target: float,
    proposed_core_buckets: Sequence[str] = (),
    risk_candidates: Iterable[PeripheralRiskCandidate] = (),
    maker_available_by_bucket: Mapping[str, bool] | None = None,
    target_inventory_by_bucket: Mapping[str, float] | None = None,
    filled_inventory_by_bucket: Mapping[str, float] | None = None,
    pending_inventory_by_bucket: Mapping[str, float] | None = None,
    current_core_buckets: Sequence[str] = (),
    current_direction: Direction | str | None = None,
    order_state_known: bool = True,
    stop_adding: bool = False,
) -> ContinuousCoreResult:
    """Resolve one deterministic continuous-core state-machine step."""

    requested_direction = _direction(direction)
    prior_direction = (
        None
        if current_direction is None
        else _direction(current_direction)
    )
    axis = _BucketAxis.from_ordinals(bucket_ordinals)
    probability = _validate_probability_vector(probabilities, axis)
    try:
        anchor = build_model_anchor_range(
            bucket_ordinals,
            members,
            formal_member_weight_floor=policy.formal_member_weight_floor,
        )
    except ContinuousCoreError as exc:
        return _result_without_structure(
            state=CoreResolutionState.UNREPRESENTABLE,
            direction=requested_direction,
            reason=str(exc),
        )
    if stable_center_bucket not in axis.index:
        return _result_without_structure(
            state=CoreResolutionState.UNREPRESENTABLE,
            direction=requested_direction,
            reason="stable_center_unrepresentable",
            anchor=anchor,
        )

    effective_direction = requested_direction if center_stable else Direction.NEUTRAL
    resolution_state = (
        CoreResolutionState.APPLIED
        if effective_direction is not Direction.NEUTRAL
        else CoreResolutionState.NEUTRAL
    )
    reason = (
        "direction_applied"
        if resolution_state is CoreResolutionState.APPLIED
        else "center_unstable_neutralized"
        if not center_stable
        else "neutral_anchor_retained"
    )
    center_index = axis.index[stable_center_bucket]
    lower, upper, boundary_error = _probability_core_bounds(
        center_index=center_index,
        axis=axis,
        probabilities=probability,
        core_probability_mass_target=core_probability_mass_target,
        proposed_core_buckets=proposed_core_buckets,
    )
    if boundary_error is not None:
        state = (
            CoreResolutionState.UNREPRESENTABLE
            if "unrepresentable" in boundary_error
            else CoreResolutionState.BLOCKED
        )
        return _result_without_structure(
            state=state,
            direction=requested_direction,
            reason=boundary_error,
            anchor=anchor,
        )

    core_buckets = axis.interval(lower, upper)
    maker = maker_available_by_bucket or {}
    unavailable_core = tuple(
        bucket for bucket in core_buckets if maker.get(bucket, True) is not True
    )
    if unavailable_core:
        return _result_without_structure(
            state=CoreResolutionState.HOLD,
            direction=requested_direction,
            reason="complete_core_maker_bundle_missing:" + ",".join(unavailable_core),
            anchor=anchor,
        )
    if not order_state_known:
        return _result_without_structure(
            state=CoreResolutionState.HOLD,
            direction=requested_direction,
            reason="order_state_unknown_reconcile_required",
            anchor=anchor,
        )

    # Formal model centers diagnose disagreement and cap how far the
    # probability-selected core may search.  They never fill the interval or
    # become support merely because they are extrema.  The layer cap prevents
    # a lone remote member from opening an unbounded candidate range.
    anchor_lower = axis.index[anchor.lower_bucket]
    anchor_upper = axis.index[anchor.upper_bucket]
    candidate_lower = (
        lower
        if anchor_lower >= lower
        else max(anchor_lower, lower - policy.max_peripheral_layers)
    )
    candidate_upper = (
        upper
        if anchor_upper <= upper
        else min(anchor_upper, upper + policy.max_peripheral_layers)
    )
    boundary_buckets = axis.interval(candidate_lower, candidate_upper)
    candidate_boundary = CandidateBoundary(
        lower_bucket=boundary_buckets[0],
        upper_bucket=boundary_buckets[-1],
        buckets=boundary_buckets,
        max_peripheral_layers=policy.max_peripheral_layers,
    )

    candidate_by_bucket: dict[str, PeripheralRiskCandidate] = {}
    for candidate in risk_candidates:
        if candidate.bucket in candidate_by_bucket:
            raise ContinuousCoreError("risk_candidate_duplicate_bucket")
        candidate_by_bucket[candidate.bucket] = candidate
    selected_risk: list[str] = []
    rejected_risk: list[tuple[str, str]] = []
    for bucket in sorted(
        candidate_by_bucket,
        key=lambda item: (
            axis.index.get(item, len(axis.buckets)),
            item,
        ),
    ):
        candidate = candidate_by_bucket[bucket]
        reject_reason: str | None = None
        if bucket not in axis.index:
            reject_reason = "bucket_unrepresentable"
        elif bucket in core_buckets:
            reject_reason = "already_in_core"
        elif not candidate_lower <= axis.index[bucket] <= candidate_upper:
            reject_reason = "outside_candidate_boundary"
        elif not candidate.evidence_eligible:
            reject_reason = "evidence_gate_failed"
        elif not candidate.maker_available:
            reject_reason = "maker_gate_failed"
        elif candidate.probability_mass + EPS < policy.min_peripheral_probability_mass:
            reject_reason = "probability_mass_gate_failed"
        elif (
            candidate.model_reliability + EPS
            < policy.min_peripheral_model_reliability
        ):
            reject_reason = "model_reliability_gate_failed"
        # The per-bucket coverage-value gate is gone (charter 52d,
        # 2026-08-16): a tail rung earns its place through the odds it adds
        # to the whole basket's curve, not by clearing expectation on its
        # own.  ``price_coverage_value`` is still carried on the candidate
        # so a reader can see what each rung was worth at its ask; it just
        # no longer refuses one.
        elif candidate.target_inventory <= EPS:
            reject_reason = "positive_target_inventory_required"
        if reject_reason is None:
            selected_risk.append(bucket)
        else:
            rejected_risk.append((bucket, reject_reason))

    explicit_targets = target_inventory_by_bucket
    target: dict[str, float] = {}
    for bucket in core_buckets:
        value = (
            policy.default_core_target_inventory
            if explicit_targets is None
            else float(explicit_targets.get(bucket, 0.0))
        )
        if not math.isfinite(value) or value <= EPS:
            return _result_without_structure(
                state=CoreResolutionState.BLOCKED,
                direction=requested_direction,
                reason="core_positive_target_inventory_required:" + bucket,
                anchor=anchor,
            )
        target[bucket] = value
    for bucket in selected_risk:
        candidate = candidate_by_bucket[bucket]
        value = (
            candidate.target_inventory
            if explicit_targets is None
            else float(explicit_targets.get(bucket, 0.0))
        )
        if not math.isfinite(value) or value <= EPS:
            return _result_without_structure(
                state=CoreResolutionState.BLOCKED,
                direction=requested_direction,
                reason="risk_positive_target_inventory_required:" + bucket,
                anchor=anchor,
            )
        target[bucket] = value

    ordered_support = tuple(
        bucket for bucket in axis.buckets if bucket in target
    )
    core_interval = CoreInterval(
        lower_bucket=core_buckets[0],
        upper_bucket=core_buckets[-1],
        buckets=core_buckets,
        target_inventory=tuple((bucket, target[bucket]) for bucket in core_buckets),
    )
    support_set = SupportSet(
        core_buckets=core_buckets,
        optional_risk_buckets=tuple(selected_risk),
        buckets=ordered_support,
        target_inventory=tuple((bucket, target[bucket]) for bucket in ordered_support),
    )

    filled = filled_inventory_by_bucket or {}
    pending = pending_inventory_by_bucket or {}
    gaps: list[tuple[str, float]] = []
    for bucket in ordered_support:
        values = (
            float(filled.get(bucket, 0.0)),
            float(pending.get(bucket, 0.0)),
        )
        if any(not math.isfinite(value) or value < 0.0 for value in values):
            raise ContinuousCoreError("inventory_state_invalid")
        gap = max(target[bucket] - values[0] - values[1], 0.0)
        if gap > EPS:
            gaps.append((bucket, gap))

    if stop_adding:
        transition = CoreTransition.STOP_ADDING
        gaps = []
    elif not current_core_buckets:
        transition = CoreTransition.INITIALIZE if gaps else CoreTransition.KEEP
    elif tuple(current_core_buckets) != core_buckets:
        transition = CoreTransition.MIGRATE if gaps else CoreTransition.KEEP
    elif gaps:
        transition = CoreTransition.FILL_GAP
    else:
        transition = CoreTransition.KEEP
    if prior_direction is None:
        direction_transition = DirectionTransition.INITIALIZE
    elif prior_direction is effective_direction:
        direction_transition = DirectionTransition.KEEP
    elif effective_direction is Direction.NEUTRAL:
        direction_transition = DirectionTransition.NEUTRALIZE
    else:
        direction_transition = DirectionTransition.MIGRATE

    return ContinuousCoreResult(
        resolution_state=resolution_state,
        transition=transition,
        direction_transition=direction_transition,
        requested_direction=requested_direction,
        effective_direction=effective_direction,
        reason=reason,
        anchor_range=anchor,
        core_interval=core_interval,
        candidate_boundary=candidate_boundary,
        support_set=support_set,
        trimmed_buckets=(),
        rejected_risk_buckets=tuple(rejected_risk),
        positive_inventory_gap=tuple(gaps),
    )


__all__ = [
    "CandidateBoundary",
    "ContinuousCoreError",
    "ContinuousCorePolicy",
    "ContinuousCoreResult",
    "CoreInterval",
    "CoreResolutionState",
    "CoreTransition",
    "DirectionTransition",
    "ModelAnchorRange",
    "ModelCenter",
    "PeripheralRiskCandidate",
    "SupportSet",
    "build_model_anchor_range",
    "resolve_continuous_core",
]
