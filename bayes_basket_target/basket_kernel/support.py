"""Probability and support diagnostics shared by the production basket runtime."""

from __future__ import annotations

import math
from typing import Iterable, Mapping

from .contracts import (
    EPS,
    BucketObservation,
    ContractError,
    Direction,
    TargetBasketPolicy,
    TargetBasketSnapshot,
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


# Charter 75 (user ruling, 2026-09-03): the centre bucket is where the served
# distribution's probability-weighted mean falls, and the direction bias is
# read by comparing that mean with the mode, gated at half a bucket:
#   left-skewed   mean < median < mode   -> DOWN (the tail is on the cold side)
#   symmetric     mean ~ median ~ mode   -> NEUTRAL
#   right-skewed  mode < median < mean   -> UP
# One definition for the seed (``_choose_support``), the ladder centre
# (charter 54) and the skew extension sign (charter 55), and the paper ledger
# imports the same two functions.
#
# The gate is in units of the served sigma, not buckets (user ruling,
# 2026-09-11).  Half a bucket was an absolute gate: as the served width fell
# from 1.7 to 0.65 bucket steps between 08-25 and 09-10 the mean could no
# longer leave the mode's bucket and 89% of station-days read SYMMETRIC --
# the reading had become a width reading.  Measured on 719 station-days
# (08-25..09-09, P0, last served vector, settled truth): whenever a lean is
# read the truth lands on the lean side 83-85% of the time against a 46/54
# base rate, under every definition tried; the absolute gate simply read a
# lean on 11 of 162 narrow days where 0.35 sigma reads 80 at 80% accuracy.
# 0.35 was a charter default, not a fitted value.  User ruling 2026-10-16
# 「换成0.2σ」: re-selected on the spread-adjusted pool (charter 273) by the
# mutual information between the three-state reading and the side the truth
# lands off the mode (docs/research/p0_skew_threshold_20261016) -- every lead
# preferred less than 0.35 (D0 0.25, D1 0.05, D2 0.10, pooled 0.20): pooled,
# 0.20 reads a lean on 60% of off-mode days at 71.5% right against 33% at
# 75.6% (0.081 bit against 0.065).  The walk-forward gain crossed zero on
# 12-14 evaluated days; the ruling took the pooled optimum.
MEAN_MODE_SKEW_THRESHOLD_SIGMA = 0.20


def mean_center_bucket_v1(
    probabilities: Mapping[str, Any],
    ordinals: Mapping[str, Any],
    *,
    admitted: Sequence[str] | None = None,
) -> str | None:
    """The bucket nearest the probability-weighted mean ordinal.

    Read off the whole served distribution -- a bucket the book declined to
    price still carries mass -- and snapped onto ``admitted`` (every bucket
    when None), ties to the heavier bucket.  None on empty mass, so a
    caller can fall back to its own anchor.
    """

    weighted = [
        (float(probabilities[bucket]), int(ordinals[bucket]), str(bucket))
        for bucket in probabilities
        if bucket in ordinals and float(probabilities[bucket]) > 0.0
    ]
    total = sum(mass for mass, _, _ in weighted)
    if not weighted or total <= 0.0:
        return None
    target = sum(mass * ordinal for mass, ordinal, _ in weighted) / total
    pool = [
        str(bucket)
        for bucket in (admitted if admitted is not None else ordinals)
        if bucket in ordinals
    ]
    if not pool:
        return None
    return min(
        pool,
        key=lambda bucket: (
            abs(int(ordinals[bucket]) - target),
            -float(probabilities.get(bucket, 0.0) or 0.0),
            int(ordinals[bucket]),
            bucket,
        ),
    )


# What the shape reading is called when it is written down.  The sign is
# what the gates consume; these are what a reader is shown, and they are
# fixed here so the board, the plan and any audit spell one vocabulary.
SKEW_STATE_SYMMETRIC = "SYMMETRIC"
SKEW_STATE_RIGHT = "RIGHT_SKEWED"
SKEW_STATE_LEFT = "LEFT_SKEWED"
SKEW_STATE_BY_SIGN = {
    0: SKEW_STATE_SYMMETRIC,
    1: SKEW_STATE_RIGHT,
    -1: SKEW_STATE_LEFT,
}


def mean_mode_skew_reading_v1(
    probabilities: Mapping[str, Any],
    ordinals: Mapping[str, Any],
    *,
    signal: str = "mean_mode",
    threshold_sigma: float = MEAN_MODE_SKEW_THRESHOLD_SIGMA,
) -> dict[str, Any]:
    """The whole shape reading, not just the sign it reduces to.

    The gap is the weighted mean ordinal minus the heaviest bucket's ordinal
    (``mean_median``: minus the median's), gated at ``threshold_sigma``
    times the served distribution's own standard deviation -- so the same
    offset reads as a lean on a narrow day and as nothing on a wide one, and
    a Fahrenheit station is not read as twice as skewed as a Celsius one
    (gap and sigma are both in bucket steps).  Lifted from charter 55's
    ``_skew_extension_sign`` so the seed's direction reads the same number.

    ``mean_mode_skew_sign_v1`` is this function's sign and nothing else, so
    a reading shown to a reader and a gate acting on it can never be two
    different numbers.  The gap and the threshold travel with it because a
    state alone cannot be checked: "symmetric" at 0.49 buckets and
    "symmetric" at 0.01 are the same word about very different days, and
    the first is one hair from seating differently under charter 79.
    """

    weighted = [
        (float(probabilities[bucket]), int(ordinals[bucket]))
        for bucket in probabilities
        if bucket in ordinals and float(probabilities[bucket]) > 0.0
    ]
    total = sum(mass for mass, _ in weighted)
    empty = {
        "schema": "mean_mode_skew_reading_v1",
        "signal": str(signal),
        "sign": 0,
        "state": SKEW_STATE_SYMMETRIC,
        "gap_buckets": None,
        "mean_ordinal": None,
        "mode_ordinal": None,
        "bucket_step": None,
        "sigma_buckets": None,
        "threshold_sigma": float(threshold_sigma),
        "threshold_buckets": None,
        "threshold_basis": "sigma",
        "readable": False,
    }
    if not weighted or total <= 0.0:
        return empty
    if signal == "mean_median":
        running = 0.0
        anchor_ordinal = weighted[-1][1]
        for mass, ordinal in sorted(weighted, key=lambda row: row[1]):
            running += mass
            if running >= total / 2.0:
                anchor_ordinal = ordinal
                break
        mode_ordinal = anchor_ordinal
    else:
        mode_ordinal = max(weighted, key=lambda row: row[0])[1]
    mean_ordinal = sum(mass * o for mass, o in weighted) / total
    steps = sorted({o for _, o in weighted})
    diffs = [b - a for a, b in zip(steps, steps[1:]) if b > a]
    step = min(diffs) if diffs else 1
    gap = mean_ordinal - mode_ordinal
    # The served width, in the same bucket steps as the gap.  Measured
    # 2026-09-08 on every P0 envelope since 08-25: under the old absolute
    # gate the share of days this reading called SYMMETRIC was a monotone
    # function of this number alone (19% at sigma >= 2.25 steps, 84% under
    # 1.0), so a shape reading that did not carry its width could not be
    # told apart from a width reading.  Charter 122 gates the symmetric seat
    # on it; since 2026-09-11 the gate itself is in units of it.
    variance = sum(mass * (o - mean_ordinal) ** 2 for mass, o in weighted) / total
    sigma_buckets = math.sqrt(max(0.0, variance)) / step
    threshold_buckets = float(threshold_sigma) * sigma_buckets
    # A one-bucket distribution has no width and no gap: symmetric, rather
    # than "not below a zero gate" and therefore leaning.
    sign = 0 if (
        sigma_buckets <= 0.0 or abs(gap) / step < threshold_buckets
    ) else (1 if gap > 0 else -1)
    return {
        "schema": "mean_mode_skew_reading_v1",
        "signal": str(signal),
        "sign": sign,
        "state": SKEW_STATE_BY_SIGN[sign],
        # In bucket steps, so a 2°F station and a 1°C station are read on
        # one scale -- the same normalisation the threshold uses.
        "gap_buckets": round(gap / step, 6),
        "mean_ordinal": round(mean_ordinal, 6),
        "mode_ordinal": int(mode_ordinal),
        "bucket_step": int(step),
        "sigma_buckets": round(sigma_buckets, 6),
        "threshold_sigma": float(threshold_sigma),
        # The gate this day was read at, in bucket steps: threshold_sigma
        # times this day's sigma.  Kept under the old name so every reader
        # of the reading still finds the number the sign was judged by.
        "threshold_buckets": round(threshold_buckets, 6),
        "threshold_basis": "sigma",
        "readable": True,
    }


def served_sigma_buckets_v1(
    probabilities: Mapping[str, Any],
    ordinals: Mapping[str, Any],
) -> float | None:
    """The served distribution's standard deviation in bucket steps.

    One implementation, in ``mean_mode_skew_reading_v1``: the width a gate
    acts on and the width a reader is shown are the same number.  None on
    empty mass.
    """

    reading = mean_mode_skew_reading_v1(probabilities, ordinals)
    return None if not reading["readable"] else float(reading["sigma_buckets"])


def mean_mode_skew_sign_v1(
    probabilities: Mapping[str, Any],
    ordinals: Mapping[str, Any],
    *,
    signal: str = "mean_mode",
    threshold_sigma: float = MEAN_MODE_SKEW_THRESHOLD_SIGMA,
) -> int:
    """The distribution's skew: +1 mean above the mode, -1 below, 0 within.

    One implementation, in ``mean_mode_skew_reading_v1``.
    """

    return int(mean_mode_skew_reading_v1(
        probabilities,
        ordinals,
        signal=signal,
        threshold_sigma=threshold_sigma,
    )["sign"])


def expected_direction_from_probability_vector(
    probabilities: Mapping[str, float],
    ordinals: Mapping[str, int],
    policy: TargetBasketPolicy,
    *,
    evidence_fresh: bool = True,
) -> Direction:
    """Charter 75: the direction bias is the distribution's skew.

    Mean below the mode by at least 0.35 of the served sigma is left-skewed
    and leans DOWN; mean above the mode leans UP; within that the shape is
    symmetric and there is no lean.  The market is intentionally absent
    here, as before, and a stale stage yields no direction.  ``policy`` is
    kept on the signature for the callers that pass it; the deadband and
    entropy dials it carried are superseded by the sigma gate.
    """

    del policy
    parsed = _validated_probability_vector(
        probabilities,
        ordinals,
        contract="direction",
    )
    if not evidence_fresh:
        return Direction.NEUTRAL
    sign = mean_mode_skew_sign_v1(parsed, ordinals)
    if sign > 0:
        return Direction.UP
    if sign < 0:
        return Direction.DOWN
    return Direction.NEUTRAL


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
    """Anchor the basket where the served distribution's mean falls.

    Charter 75: the centre bucket is the one nearest the probability-weighted
    mean ordinal (charter 54's ladder centre, now the seed's too).  Reading
    the mean rather than the argmax keeps an open-ended edge bucket from
    anchoring the basket on its unbounded tail, which is what the model
    centre existed to prevent; the model centre remains the fallback for a
    vector with no mass, and the argmax the fallback after that.
    """

    mean_bucket = mean_center_bucket_v1(
        {item.bucket: item.probability for item in buckets},
        {item.bucket: item.ordinal for item in buckets},
    )
    if mean_bucket is not None:
        for item in buckets:
            if item.bucket == mean_bucket:
                return item
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
    # Charter 76 (user ruling, 2026-09-04): the seed is the probability core
    # alone.  The envelope anchor (candidate boundary stretched toward the
    # model envelope by the disagreement-derived layer budget) and the
    # peripheral gates (probability floor, ask ceiling, per-bucket model
    # reliability) that admitted bias/edge buckets around it are retired
    # from this layer.  Measured over 130,764 judged cycles, 2026-08-15 to
    # 09-03: the reliability gate read 0.0 against its 0.15 floor on every
    # envelope until 09-02 (no producer published per-bucket reliability),
    # bias/edge came back non-empty on 78 cycles after that, and not one
    # seated leg ever came from them.  What the seed decides is therefore
    # exactly what it decided in practice: the heaviest buckets to the
    # coverage target, made contiguous and capped around the centre.
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
    ordinal_by_bucket = {item.bucket: item.ordinal for item in buckets}
    probability_by_bucket = {item.bucket: item.probability for item in buckets}
    core_lower = min(ordinal_by_bucket[bucket] for bucket in base)
    core_upper = max(ordinal_by_bucket[bucket] for bucket in base)
    persisted_probability_core = [
        item.bucket
        for item in buckets
        if core_lower <= item.ordinal <= core_upper
    ]
    # The anchor is the stage's centre when the core holds it; otherwise the
    # incumbent is grown from the heaviest core bucket and the fact is
    # carried on the transition record (2026-09-02 degrade ruling).
    center_admitted = center.bucket in base
    if center_admitted:
        incumbent_anchor = center.bucket
    else:
        incumbent_anchor = max(
            base,
            key=lambda bucket: (
                probability_by_bucket[bucket],
                -ordinal_by_bucket[bucket],
                bucket,
            ),
        )
    desired = _converge_contiguous_support(
        set(base),
        center_bucket=incumbent_anchor,
        ordinal_by_bucket=ordinal_by_bucket,
        probability_by_bucket=probability_by_bucket,
        maximum_buckets=policy.maximum_support_buckets,
        breakthrough_is_upper=_breakthrough_is_upper(direction),
    )
    base = set(base) & desired
    bias: set[str] = set()
    edge: set[str] = set()
    # The candidate boundary is the seed's own span now that nothing
    # stretches it; the plan still records it, and the market-centre
    # diagnostic (charter 45) still reads it.
    candidate_lower = min(ordinal_by_bucket[bucket] for bucket in desired)
    candidate_upper = max(ordinal_by_bucket[bucket] for bucket in desired)
    expansion_layers = 0
    scores: dict[str, tuple[float, float, float]] = {}
    for item in buckets:
        base_score = (
            item.probability
            if item.bucket in desired and item.bucket in base
            else 0.0
        )
        scores[item.bucket] = (base_score, 0.0, 0.0)
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
            # Only present when it happened, so every plan that compiled
            # before this branch existed still hashes byte for byte.
            **(
                {}
                if center_admitted
                else {
                    "center_admitted": False,
                    "center_bucket": center.bucket,
                    "incumbent_anchor_bucket": incumbent_anchor,
                    "center_not_admitted_reason": (
                        "anchor_outside_admitted_support"
                    ),
                }
            ),
        },
    )


__all__ = [
    "_choose_support",
    "_derive_dual_risk_scope",
    "_normalized_entropy",
    "expected_direction_from_probability_vector",
    "probability_center_diagnostics",
]
