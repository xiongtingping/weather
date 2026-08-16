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
    """Resolve the lean from the current stage probability vector only.

    The market is intentionally absent here: direction is which side of the
    centre the calibrated mass sits on, and nothing else votes.  A stale
    stage yields no direction, since it has nothing current to lean with.
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
    # Direction is the mass, with nothing between (charter 52c, extended
    # 2026-08-16).  The three thresholds that used to sit here -- a skew
    # deadband, an entropy ceiling and a floor on the leaning side's mass --
    # each answered "is this lean big enough to believe", which is a question
    # the objective already answers better and with the prices in hand: a
    # thin lean produces a thin bias and the curve score sizes it
    # accordingly, whereas a threshold discards it and calls the day neutral.
    # They were also a second opinion on p(t) inside the strategy layer, the
    # same defect as the regime gate.  What survives is not a threshold: a
    # stale stage cannot speak to direction at all, and an exactly balanced
    # distribution has no side to lean to.
    if not evidence_fresh or skew == 0.0:
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


def _breakthrough_is_upper(direction: Direction) -> bool | None:
    """Return which end carries the breakthrough hedge, or None if undirected."""

    if direction is Direction.UP:
        return True
    if direction is Direction.DOWN:
        return False
    return None


def _converge_contiguous_support(
    admitted: set[str],
    *,
    center_bucket: str,
    ordinal_by_bucket: Mapping[str, int],
    probability_by_bucket: Mapping[str, float],
    maximum_buckets: int,
    hedge_value_by_bucket: Mapping[str, float] | None = None,
    breakthrough_is_upper: bool | None = None,
) -> set[str]:
    """Return a contiguous, bounded support anchored on the stable center.

    Peripheral risk buckets are admitted by evidence/Maker/probability/price
    gates that say nothing about adjacency, so ``base | bias | edge`` could
    contain holes -- a real basket once selected ordinals {0, 2, 3, 5}, quietly
    skipping two buckets inside its own span.  A basket with a hole is not a
    temperature range: settlement in a skipped bucket pays nothing while the
    basket was priced as if that outcome were covered.

    Convergence closes a hole by buying the bucket inside it.  Keeping only
    the longest admitted run instead would delete whatever sits past the hole,
    and what sits past a hole is usually the hedge: with 35 at 0.45, 36 at
    0.10 and 37 at 0.45, the run containing the center is 35 alone, so the
    basket would cover 0.45 of the distribution and drop an equal share of it.
    Reaching an admitted bucket costs every bucket between it and the center,
    so the walk starts at the center and extends one admitted endpoint at a
    time, taking the more valuable side first and skipping any side whose span
    no longer fits ``maximum_buckets``.  A hedge that cannot be reached inside
    the cap is left out whole rather than approached with orphan bridge
    buckets, which would spend the cap on buckets that cover nothing by
    themselves.

    The result narrows on its own as probability concentrates: a sharp P1
    leaves fewer admitted buckets within reach, so the span -- and the basket
    -- shrinks without any separate schedule.  Open-ended edge buckets ("31°C
    or below", "41°C or higher") are ordinary members here and act as the tail
    hedge whenever the distribution has a real tail.

    A hedge bucket need not touch the core.  With a core at 32/33 and a hedge
    at 35, 34 is pulled in to keep the range whole, giving 32/33/34/35 -- a
    contiguous four-bucket basket whose far end carries the breakthrough
    cover.  Reach therefore ranks sides by hedge value rather than raw
    probability, so the far end is not the first thing given up: it is held
    precisely because it is unlikely, paying when the distribution is wrong
    and the core pays nothing.  ``breakthrough_is_upper`` breaks an exact tie
    toward the side the direction points at.
    """

    if center_bucket not in admitted:
        raise ContractError("support_center_not_admitted")
    if maximum_buckets < 1:
        raise ContractError("support_maximum_buckets_invalid")
    missing = [bucket for bucket in admitted if bucket not in ordinal_by_bucket]
    if missing:
        raise ContractError("support_bucket_ordinal_missing")

    center_ordinal = ordinal_by_bucket[center_bucket]
    bucket_by_ordinal = {
        ordinal: bucket for bucket, ordinal in ordinal_by_bucket.items()
    }
    if len(bucket_by_ordinal) != len(ordinal_by_bucket):
        raise ContractError("support_duplicate_ordinal")
    admitted_ordinals = sorted(ordinal_by_bucket[bucket] for bucket in admitted)

    # ``hedge_value_by_bucket`` prices tail cover as coverage value times
    # distance, which is what the far bucket is bought for; raw probability
    # would rank every hedge below its near-core neighbours and give the tail
    # up first.  Callers without those scores fall back to probability alone.
    def _reach_key(ordinal: int) -> tuple[float, float]:
        bucket = bucket_by_ordinal[ordinal]
        hedge = float((hedge_value_by_bucket or {}).get(bucket, 0.0))
        return (hedge, float(probability_by_bucket.get(bucket, 0.0)))

    def _spannable(low: int, high: int) -> bool:
        # Every ordinal between the two ends is bought, so the contract has to
        # name a bucket for each one and the cap has to cover the whole span.
        return high - low + 1 <= maximum_buckets and all(
            ordinal in bucket_by_ordinal for ordinal in range(low, high + 1)
        )

    lower = upper = center_ordinal
    while True:
        target_low = max(
            (ordinal for ordinal in admitted_ordinals if ordinal < lower),
            default=None,
        )
        target_high = min(
            (ordinal for ordinal in admitted_ordinals if ordinal > upper),
            default=None,
        )
        low_ok = target_low is not None and _spannable(target_low, upper)
        high_ok = target_high is not None and _spannable(lower, target_high)
        if low_ok and high_ok:
            low_key = _reach_key(target_low)
            high_key = _reach_key(target_high)
            if high_key != low_key:
                take_high = high_key > low_key
            else:
                # An exact tie resolves toward the breakthrough side, and with
                # no directional evidence toward the upper one, so the result
                # stays reproducible.
                take_high = breakthrough_is_upper is not False
        elif low_ok:
            take_high = False
        elif high_ok:
            take_high = True
        else:
            break
        if take_high:
            upper = target_high
        else:
            lower = target_low

    return {bucket_by_ordinal[ordinal] for ordinal in range(lower, upper + 1)}


def _center(
    buckets: tuple[BucketObservation, ...],
    *,
    model_center_ordinal: int | None = None,
) -> BucketObservation:
    """Anchor the basket where the stage's data points.

    Highest probability is the wrong anchor when an open-ended edge bucket is
    in play.  "35°C or higher" spans an unbounded interval, so it collects the
    whole upper tail of every ensemble member and is not commensurable with the
    1°C buckets it is ranked against.  ZUUU showed this exactly: system centers
    of 30.87°C and 34.39°C, a model envelope of 31°C..34°C, and yet the edge
    bucket won the argmax at 0.3344 and anchored the basket outside the
    envelope.

    ``model_center_ordinal`` is the bucket containing this stage's own point
    estimate -- for P0 the weight-averaged calibrated system centers.  An edge
    bucket remains a legitimate anchor whenever that estimate genuinely falls
    inside it (centers of 36/37 do).  The probability argmax stays as the
    fallback for stages that publish no anchor.
    """

    if model_center_ordinal is not None:
        anchored = [
            item for item in buckets if item.ordinal == model_center_ordinal
        ]
        if anchored:
            return anchored[0]
        raise ContractError("support_model_center_bucket_absent")
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
    center = _center(
        buckets,
        model_center_ordinal=snapshot.evidence.model_center_ordinal,
    )
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
        desired = _converge_contiguous_support(
            base | bias | edge,
            center_bucket=center.bucket,
            ordinal_by_bucket={
                item.bucket: item.ordinal for item in buckets
            },
            probability_by_bucket={
                item.bucket: item.probability for item in buckets
            },
            maximum_buckets=policy.maximum_support_buckets,
            # This branch has no coverage-value scores, so the direction alone
            # protects the breakthrough end from being trimmed first.
            breakthrough_is_upper=_breakthrough_is_upper(direction),
        )
        # Risk families only ever classify what survived convergence.  A
        # peripheral bucket that convergence dropped must not keep a bias/edge
        # role, or the score vector would reference a bucket outside support.
        base &= desired
        bias &= desired
        edge &= desired
        selected_risk &= desired
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
                max(
                    0.0,
                    item.probability
                    - (
                        item.ask_price * (1.0 + policy.fee_rate)
                        + policy.taker_slippage_buffer_per_share
                    ),
                )
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
    core_lower = min(
        item.ordinal for item in buckets if item.bucket in base
    )
    core_upper = max(
        item.ordinal for item in buckets if item.bucket in base
    )
    persisted_probability_core = [
        item.bucket
        for item in sorted(
            buckets,
            key=lambda row: (row.ordinal, row.bucket),
        )
        if core_lower <= item.ordinal <= core_upper
    ]
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
        # No per-bucket EV gate (charter 52d, 2026-08-16).  Requiring each
        # peripheral bucket to clear a cent of its own coverage value asks
        # the wrong question of a tail rung: its job is the odds and the
        # shape of the wealth curve, not standing alone on expectation, and
        # a rung whose own EV is slightly negative can still be right for the
        # basket.  Whole-basket EV is what the objective solves for, and the
        # curve score already prices every leg against its ask -- so this
        # gate was a second, cruder opinion applied before the objective
        # could form one.  The evidence, ask-ceiling, reliability and Maker
        # gates above stay: they answer "is this bucket real and buyable",
        # which is not an EV question.
        return True

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
        # Price may qualify a peripheral bucket through positive probability
        # coverage value, but a reciprocal-price multiplier would turn a
        # cheap quote into synthetic probability and overweight long odds.
        first_share_all_in = (
            item.ask_price * (1.0 + policy.fee_rate)
            + policy.taker_slippage_buffer_per_share
        )
        edge_scores[item.bucket] = (
            max(0.0, item.probability - first_share_all_in)
            * distance
        )
    edge = set(edge_scores)

    # Final support is a contiguous temperature range, never a sparse set.
    # Historical holdings remain inventory facts, but neither they nor two
    # selected endpoints may leave an internal gap in the new target support:
    # settlement inside a skipped bucket pays nothing while the basket was
    # priced as if that outcome were covered.  Convergence only removes
    # buckets, so every survivor keeps the admission evidence it already had.
    desired = _converge_contiguous_support(
        set(base) | set(bias) | set(edge),
        center_bucket=center.bucket,
        ordinal_by_bucket=ordinal_by_bucket,
        probability_by_bucket={
            item.bucket: item.probability for item in buckets
        },
        maximum_buckets=policy.maximum_support_buckets,
        # Coverage value times distance is exactly what the far breakthrough
        # bucket is bought for, so rank ends by it instead of raw probability.
        hedge_value_by_bucket=edge_scores,
        breakthrough_is_upper=_breakthrough_is_upper(direction),
    )
    base = set(base) & desired
    bias = set(bias) & desired
    edge = set(edge) & desired
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
                *persisted_probability_core
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
