"""The four layers of the temperature path, applied to one root.

    P1(k)  proportional to   Split_2( Mask_1(k) * Root'_3(k) * LR_4(k) )

Each layer answers one question from one kind of evidence, and the order is
forced by what each does to the distribution rather than chosen for taste:

1. **fact** -- the day's maximum is not below what has already been observed.
   A mask, no parameters, nothing to learn.  It is the one layer entitled to
   write an exact zero.
2. **phase** -- will the day rise at all from here.  Applied last because it
   sets the total mass above the running high's bucket, and any layer applied
   after it would put back the mass it just took.
3. **reanchor** -- is the model running warm or cold today.  Applied first
   because it corrects the prior, not the evidence: it translates the root
   along the temperature axis.  This is the only place the model's level is
   read, and it is read once.
4. **remaining warming** -- given the day will rise, how far.  Reshapes the
   buckets above the running high without touching how much mass is up there;
   that total belongs to the phase layer.

Every layer is written so that switching it off recovers the layer below
exactly, because that is how each one gets priced.  Setting the reanchor slope
to zero returns the unshifted root; a likelihood ratio of one returns the
masked prior; a phase target equal to the mass already above the high returns
its own input.  The counterfactual arms the acceptance run scores are
therefore the production code path with one constant changed, not a
reimplementation of it.

The artifacts arrive from the caller rather than being loaded here, for the
same reason the band table and the ceiling artifact do: a replay has to be
able to rebuild the exact distribution that was served, and a layer that
reads its own fitted file rebuilds a different one the moment it is refitted.
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime
from typing import Any, Mapping, Sequence

from .contracts import ContractError

SCHEMA = "bayes_basket_target_metar_four_layer_v1"
# Serving since 2026-09-07 (operator ruling "别影子了，直接上线").  The chain
# stands in the ceiling's place at P1's outermost composition point, and it
# runs with the translation OFF: charter 95's step-two measurement scored the
# chain against a bare clock rate, while production's own stack already
# carries a level correction in the centre/width scalars and the band table,
# so promoting the reanchor as well would be shipping a correction nobody
# measured on top of one that is already there.  ``-reanchor`` is also the
# best-measured arm: 0.644 hit, -0.900 nat, 0.430 Brier, 0.577C centre MAE
# over 18096 walk-forward samples, against production's 0.632 / -0.962 /
# 0.448 / 0.616.  ``PM_P1_FOUR_LAYER_SERVE=0`` restores the ceiling.
SERVE_ENABLED = (
    os.environ.get("PM_P1_FOUR_LAYER_SERVE", "1").strip()
    not in {"0", "false", "FALSE", "no", ""}
)
PHASE_RECEIPT_SCHEMA = "metar_phase_receipt_v1"
REANCHOR_RECEIPT_SCHEMA = "metar_reanchor_receipt_v1"
REMAINING_RECEIPT_SCHEMA = "metar_remaining_warming_receipt_v1"

# Never drive a side of the split to exactly zero.  The phase layer states a
# rate, not a proof about this station-day, and a zeroed bucket cannot be
# revived by a later report that does warm.  Only the observation hard bound
# has the authority to write a zero.
_EDGE = 1e-6
_EPS = 1e-12


def _finite(name: str, value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"metar_four_layer_{name}_invalid") from exc
    if not math.isfinite(number):
        raise ContractError(f"metar_four_layer_{name}_invalid")
    return number


def _hours_bin(hours: float, edges: Sequence[float]) -> int:
    for index in range(len(edges) - 1):
        if edges[index] <= hours < edges[index + 1]:
            return index
    return len(edges) - 2


def curve_points(
    curve_json: str | Mapping[str, Any] | None,
) -> list[tuple[datetime, float]]:
    """The root's hourly curve as sorted (instant, temperature) pairs.

    One parser for training and serving both.  The retired layer kept a view
    of the curve on each side and they drifted; here a caller that wants the
    shape or the bias gets it from this function or not at all.
    """

    if not curve_json:
        return []
    try:
        body = (
            curve_json
            if isinstance(curve_json, Mapping)
            else json.loads(str(curve_json))
        )
        return sorted(
            (
                datetime.fromisoformat(
                    str(point["valid_time_utc"]).replace("Z", "+00:00")
                ),
                float(point["temperature_c"]),
            )
            for point in body.get("points") or []
        )
    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
        return []


def interpolate_curve(
    points: Sequence[tuple[datetime, float]], instant: datetime
) -> float | None:
    """The curve's temperature at one instant, linear between its own hours.

    No extrapolation.  Past the curve's last point the model has stopped
    speaking, and carrying the last value flat would read as a claim it never
    made -- which is exactly the shape of the evening exhaustion that took a
    third of station-days off the retired layer.
    """

    if len(points) < 2 or instant < points[0][0] or instant > points[-1][0]:
        return None
    for (left_t, left_v), (right_t, right_v) in zip(points, points[1:]):
        if left_t <= instant <= right_t:
            span = (right_t - left_t).total_seconds()
            if span <= 0.0:
                return left_v
            ratio = (instant - left_t).total_seconds() / span
            return left_v + ratio * (right_v - left_v)
    return None


def curve_shape_realized(
    points: Sequence[tuple[datetime, float]], instant: datetime
) -> float | None:
    """How much of the curve's own daily rise is already behind us.

    Zero at its coldest point, one at its warmest.  This is the only thing
    the remaining-warming layer reads off the model: the shape of the day,
    never its level.  Two curves five degrees apart that peak at the same
    hour give the identical number, which is what keeps that layer
    independent of the root it multiplies.  A curve with under half a degree
    of range states no shape at all and returns None.
    """

    if len(points) < 2:
        return None
    value = interpolate_curve(points, instant)
    if value is None:
        return None
    low = min(temperature for _, temperature in points)
    high = max(temperature for _, temperature in points)
    if high - low < 0.5:
        return None
    return max(0.0, min(1.0, (value - low) / (high - low)))


def recent_curve_bias(
    points: Sequence[tuple[datetime, float]],
    observations: Sequence[tuple[datetime, float]],
    *,
    as_of: datetime,
    window_seconds: float = 10800.0,
) -> tuple[float | None, int]:
    """Mean (observed minus curve) over the recent window, and its report count.

    Three hours because the layer that consumes it models a bias that persists
    through the day, and one report's difference is mostly that report's own
    rounding.  Returns ``(None, 0)`` when the curve covers none of the window,
    which is the honest statement that no bias was measured.
    """

    values = [
        temperature - modelled
        for instant, temperature in observations
        if 0.0 <= (as_of - instant).total_seconds() <= window_seconds
        for modelled in (interpolate_curve(points, instant),)
        if modelled is not None
    ]
    if not values:
        return None, 0
    return sum(values) / len(values), len(values)


def feature_row(
    *,
    remaining_hours: float,
    drawdown_from_high_c: float,
    momentum_c_per_hour: float,
    cloud_cover_fraction: float,
    precipitation_present: bool | int,
    dewpoint_depression_c: float,
    wind_speed_mps: float,
    warming_so_far_c: float,
    curve_shape_realized: float | None,
    regional_headroom_c: float | None,
    regional_live_gap_c: float | None,
) -> dict[str, float | None]:
    """The feature vector, named exactly as the sample table names its columns.

    Every argument is keyword-required, including the nullable ones: a caller
    that has not thought about a feature must say ``None`` rather than inherit
    a default, because a default is how a feature goes quietly missing on one
    side of the fit.

    One function for both sides.  The retired layer's training packet and its
    serving path each built their own view of the same state, and the two
    drifted -- a hardcoded season literal on one side of a scope key was how
    the soft likelihood went dark for three days without a single blocker
    being recorded.  Here a feature that is not in this dict does not exist.
    """

    return {
        "remaining_hours": _finite("remaining_hours", remaining_hours),
        "drawdown_from_high_c": _finite(
            "drawdown_from_high_c", drawdown_from_high_c
        ),
        "momentum_c_per_hour": _finite(
            "momentum_c_per_hour", momentum_c_per_hour
        ),
        "cloud_cover_fraction": _finite(
            "cloud_cover_fraction", cloud_cover_fraction
        ),
        "precipitation_present": 1.0 if precipitation_present else 0.0,
        "dewpoint_depression_c": _finite(
            "dewpoint_depression_c", dewpoint_depression_c
        ),
        "wind_speed_mps": _finite("wind_speed_mps", wind_speed_mps),
        "warming_so_far_c": _finite("warming_so_far_c", warming_so_far_c),
        "curve_shape_realized": (
            None
            if curve_shape_realized is None
            else _finite("curve_shape_realized", curve_shape_realized)
        ),
        # Charter 95's step two: the regional curve joins the regression here
        # rather than being blended into the finished distribution outside it.
        # Both are keyword-required and nullable for the same reason
        # ``curve_shape_realized`` is -- most of the 45-station fleet has no
        # regional lane at all, and absent has to mean "nothing known here"
        # (the fleet mean) rather than "the model promises nothing more"
        # (a zero, which is a strong and wrong claim about the day).
        "regional_headroom_c": (
            None
            if regional_headroom_c is None
            else _finite("regional_headroom_c", regional_headroom_c)
        ),
        "regional_live_gap_c": (
            None
            if regional_live_gap_c is None
            else _finite("regional_live_gap_c", regional_live_gap_c)
        ),
    }


def _linear(
    features: Mapping[str, float | None],
    coefficients: Mapping[str, float],
    fallbacks: Mapping[str, float],
) -> float:
    total = 0.0
    for name, weight in coefficients.items():
        value = features.get(name)
        if value is None:
            # Absent only ever means the root carried no curve, or its curve
            # had run out.  The fleet mean says "nothing known about the
            # shape here"; a zero would say "the day has not started".
            value = float(fallbacks.get(name, 0.0))
        total += float(weight) * float(value)
    return total


def phase_rise_probability(
    features: Mapping[str, float | None], artifact: Mapping[str, Any]
) -> float:
    """P(the day's maximum is still above the running high), from the clock
    and the reports alone."""

    phase = artifact["phase"]
    edges = phase["hours_edges"]
    intercepts = phase["bin_intercepts"]
    index = _hours_bin(
        _finite("remaining_hours", features["remaining_hours"]), edges
    )
    eta = float(intercepts[index]) + _linear(
        features, phase["coefficients"], artifact["feature_fallbacks"]
    )
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, eta))))


def _continuation_distribution(
    features: Mapping[str, float | None],
    continuation: Mapping[str, Any],
    fallbacks: Mapping[str, float],
) -> dict[int, float]:
    """P(delta = k | delta >= 1) for every rung, from the hazard chain.

    The rungs above the highest fitted one inherit its conditional rate; the
    chain is truncated at the last rung, whose mass is whatever survives it.
    """

    rungs = [int(value) for value in continuation["rungs"]]
    intercepts = [float(value) for value in continuation["rung_intercepts"]]
    shift = _linear(features, continuation["coefficients"], fallbacks)
    survival = 1.0
    distribution: dict[int, float] = {}
    previous = 1
    for rung, intercept in zip(rungs, intercepts):
        eta = intercept + shift
        conditional = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, eta))))
        distribution[previous] = survival * (1.0 - conditional)
        survival *= conditional
        previous = rung
    distribution[previous] = survival
    total = sum(distribution.values())
    if total <= _EPS:
        raise ContractError("metar_four_layer_continuation_zero_mass")
    return {rung: value / total for rung, value in distribution.items()}


def remaining_warming_likelihood_ratio(
    features: Mapping[str, float | None],
    artifact: Mapping[str, Any],
    *,
    running_high_c: float,
    bucket_centers_c: Mapping[str, float],
    excluded: Sequence[str] = (),
) -> dict[str, float]:
    """The ratio of the full chain to its own phase-only twin, per bucket.

    The denominator is the same rungs fitted on the phase covariates only, so
    the ratio carries just what the physical state and the model's shape add
    beyond the clock.  Against a flat denominator the clock would be priced
    twice: once here and once in the split applied over it.

    Buckets at or below the running high get a ratio of one.  They are the
    hard bound's business, and the chain says nothing about them by
    construction -- it is conditional on the day rising at all.
    """

    fallbacks = artifact["feature_fallbacks"]
    full = _continuation_distribution(
        features, artifact["continuation_full"], fallbacks
    )
    phase_only = _continuation_distribution(
        features, artifact["continuation_phase_only"], fallbacks
    )
    rungs = sorted(full)
    floor = float(artifact.get("likelihood_ratio_floor", 0.25))
    ceiling = float(artifact.get("likelihood_ratio_ceiling", 4.0))
    dead = set(excluded)
    ratios: dict[str, float] = {}
    for bucket, center in bucket_centers_c.items():
        increment = float(center) - _finite("running_high_c", running_high_c)
        if bucket in dead or increment <= 0.5:
            ratios[str(bucket)] = 1.0
            continue
        rung = min(rungs, key=lambda value: abs(value - increment))
        numerator = full.get(rung, 0.0)
        denominator = phase_only.get(rung, 0.0)
        if denominator <= _EPS:
            ratios[str(bucket)] = ceiling
            continue
        ratios[str(bucket)] = max(
            floor, min(ceiling, numerator / denominator)
        )
    return ratios


def reanchor_shift_c(
    *,
    bias_recent_c: float | None,
    bias_report_count: int,
    remaining_hours: float,
    artifact: Mapping[str, Any],
) -> tuple[float, str]:
    """How far to translate the root, and why it is not further.

    Returns ``(0.0, reason)`` whenever the layer declines: no measured bias
    (the root carried no curve), too few reports behind it, or a bin the fit
    backed off.  Declining is a first-class outcome -- the acceptance run
    reads the reason, and the execution gate distinguishes a layer that
    declined from one that was absent.
    """

    if bias_recent_c is None:
        return 0.0, "NO_CURVE_BIAS"
    if int(bias_report_count) < int(artifact.get("minimum_bias_reports", 2)):
        return 0.0, "BIAS_REPORTS_INSUFFICIENT"
    index = _hours_bin(
        _finite("remaining_hours", remaining_hours), artifact["hours_edges"]
    )
    entry = artifact["bins"].get(str(index))
    if not entry:
        return 0.0, "BIN_MISSING"
    slope = float(entry["slope"])
    cap = float(artifact.get("max_shift_c", 3.0))
    shift = max(-cap, min(cap, slope * _finite("bias", bias_recent_c)))
    return shift, "APPLIED" if abs(shift) > _EPS else "SLOPE_ZERO"


def translate_distribution(
    probabilities: Mapping[str, float],
    *,
    bucket_intervals_c: Mapping[str, Sequence[float | None]],
    shift_c: float,
) -> dict[str, float]:
    """Slide the prior along the temperature axis by ``shift_c``.

    Each bucket is read as uniform density over its own interval, shifted, and
    re-integrated onto the original edges.  An open-ended edge bucket is given
    the ladder's narrowest closed width, which is the same convention the
    bucket geometry helper uses for its centre -- an unbounded edge would send
    all the mass off the end of the ladder.

    A zero shift returns the input unchanged, bit for bit, which is what makes
    the ablation arm exact.
    """

    if abs(shift_c) <= _EPS:
        return {str(bucket): float(value) for bucket, value in probabilities.items()}
    widths = [
        float(bounds[1]) - float(bounds[0])
        for bounds in bucket_intervals_c.values()
        if bounds[0] is not None and bounds[1] is not None
    ]
    step = min(widths) if widths else 1.0
    spans: dict[str, tuple[float, float]] = {}
    for bucket, bounds in bucket_intervals_c.items():
        low = None if bounds[0] is None else float(bounds[0])
        high = None if bounds[1] is None else float(bounds[1])
        if low is None and high is None:
            raise ContractError("metar_four_layer_bucket_interval_invalid")
        if low is None:
            low = high - step
        if high is None:
            high = low + step
        spans[str(bucket)] = (low, high)
    if set(spans) != {str(bucket) for bucket in probabilities}:
        raise ContractError("metar_four_layer_bucket_support_mismatch")
    shifted: dict[str, float] = {bucket: 0.0 for bucket in spans}
    for source, mass in probabilities.items():
        weight = float(mass)
        if weight <= 0.0:
            continue
        low, high = spans[str(source)]
        low += shift_c
        high += shift_c
        width = high - low
        if width <= 0.0:
            raise ContractError("metar_four_layer_bucket_interval_invalid")
        placed = 0.0
        for target, (target_low, target_high) in spans.items():
            overlap = min(high, target_high) - max(low, target_low)
            if overlap > 0.0:
                shifted[target] += weight * overlap / width
                placed += overlap / width
        # Mass shifted off the end of the ladder belongs to the edge bucket it
        # left through: the open-ended bucket is the ladder's statement about
        # everything beyond it, so dropping the mass would quietly renormalise
        # the whole distribution toward the middle.
        if placed < 1.0 - 1e-9:
            ordered = sorted(spans, key=lambda name: spans[name][0])
            edge = ordered[-1] if shift_c > 0 else ordered[0]
            shifted[edge] += weight * (1.0 - placed)
    total = sum(shifted.values())
    if total <= _EPS:
        raise ContractError("metar_four_layer_translate_zero_mass")
    return {bucket: value / total for bucket, value in shifted.items()}


def apply_phase_split(
    probabilities: Mapping[str, float],
    *,
    bucket_ordinals: Mapping[str, int],
    running_high_bucket: str,
    rise_probability: float,
) -> tuple[dict[str, float], str]:
    """Rescale the above/below split to the phase layer's rate.

    Corrects exactly one number -- how much mass sits above the bucket already
    holding the day's high -- and leaves the shape inside each side alone.
    That split is the measured error and it is zero sum: what is wrongly left
    above was taken from the bucket that settles.

    Declines, returning its input, when the split is already degenerate: at
    zero or one there is no ratio to rescale that would not invent support.
    """

    base = {str(bucket): float(value) for bucket, value in probabilities.items()}
    if running_high_bucket not in bucket_ordinals:
        return base, "RUNNING_HIGH_BUCKET_UNKNOWN"
    floor_ordinal = int(bucket_ordinals[running_high_bucket])
    above = sum(
        value for bucket, value in base.items()
        if int(bucket_ordinals.get(bucket, floor_ordinal)) > floor_ordinal
    )
    if above <= 0.0 or above >= 1.0:
        return base, "SPLIT_DEGENERATE"
    target = min(max(float(rise_probability), _EDGE), 1.0 - _EDGE)
    up = target / above
    down = (1.0 - target) / (1.0 - above)
    scaled = {
        bucket: value * (
            up
            if int(bucket_ordinals.get(bucket, floor_ordinal)) > floor_ordinal
            else down
        )
        for bucket, value in base.items()
    }
    total = sum(scaled.values())
    if total <= _EPS:
        return base, "ZERO_MASS"
    return {bucket: value / total for bucket, value in scaled.items()}, "APPLIED"


def compile_four_layer_posterior(
    *,
    root_probabilities: Mapping[str, float],
    bucket_ordinals: Mapping[str, int],
    bucket_centers_c: Mapping[str, float],
    bucket_intervals_c: Mapping[str, Sequence[float | None]],
    hard_excluded_buckets: Sequence[str],
    running_high_c: float,
    running_high_bucket: str,
    features: Mapping[str, float | None],
    hazard_artifact: Mapping[str, Any] | None,
    reanchor_artifact: Mapping[str, Any] | None,
    bias_recent_c: float | None,
    bias_report_count: int,
    reanchor_enabled: bool = True,
    remaining_enabled: bool = True,
    phase_enabled: bool = True,
) -> tuple[dict[str, float], dict[str, Any]]:
    """Run the four layers in order and return the posterior with its receipts.

    The three ``*_enabled`` switches exist so the counterfactual arms are this
    function with a flag flipped rather than a second implementation of it.
    With all three off the result is the root under the hard bound alone,
    which is what production serves today.
    """

    excluded = {str(bucket) for bucket in hard_excluded_buckets}
    receipts: dict[str, Any] = {}

    # 3. Reanchor -- corrects the prior, so it runs before the evidence.
    shift = 0.0
    reason = "DISABLED"
    if reanchor_enabled and reanchor_artifact:
        shift, reason = reanchor_shift_c(
            bias_recent_c=bias_recent_c,
            bias_report_count=bias_report_count,
            remaining_hours=float(features["remaining_hours"]),
            artifact=reanchor_artifact,
        )
    working = translate_distribution(
        root_probabilities,
        bucket_intervals_c=bucket_intervals_c,
        shift_c=shift,
    ) if shift else {
        str(bucket): float(value) for bucket, value in root_probabilities.items()
    }
    receipts["reanchor"] = {
        "schema": REANCHOR_RECEIPT_SCHEMA,
        "state": reason,
        "shift_c": shift,
        "bias_recent_c": bias_recent_c,
        "bias_report_count": int(bias_report_count),
        "artifact_sha256": str(
            (reanchor_artifact or {}).get("content_sha256") or ""
        ),
    }

    # 4. Remaining warming -- reshapes above the high without moving the total.
    ratios = {bucket: 1.0 for bucket in working}
    remaining_state = "DISABLED"
    if remaining_enabled and hazard_artifact:
        ratios = remaining_warming_likelihood_ratio(
            features,
            hazard_artifact,
            running_high_c=running_high_c,
            bucket_centers_c=bucket_centers_c,
            excluded=sorted(excluded),
        )
        remaining_state = (
            "APPLIED"
            if any(abs(value - 1.0) > 1e-9 for value in ratios.values())
            else "IDENTITY"
        )
    receipts["remaining_warming"] = {
        "schema": REMAINING_RECEIPT_SCHEMA,
        "state": remaining_state,
        "likelihood_ratio_by_bucket": {
            bucket: float(value) for bucket, value in sorted(ratios.items())
        },
        "artifact_sha256": str(
            (hazard_artifact or {}).get("content_sha256") or ""
        ),
    }

    # 1. Fact -- the mask, and the only layer that may write a zero.
    weighted = {
        bucket: (
            0.0 if bucket in excluded
            else working[bucket] * float(ratios.get(bucket, 1.0))
        )
        for bucket in working
    }
    total = sum(weighted.values())
    if total <= _EPS:
        raise ContractError("metar_four_layer_posterior_zero_mass")
    posterior = {bucket: value / total for bucket, value in weighted.items()}

    # 2. Phase -- outermost, because it owns the total above the high.
    rise = None
    phase_state = "DISABLED"
    if phase_enabled and hazard_artifact:
        rise = phase_rise_probability(features, hazard_artifact)
        posterior, phase_state = apply_phase_split(
            posterior,
            bucket_ordinals=bucket_ordinals,
            running_high_bucket=running_high_bucket,
            rise_probability=rise,
        )
        # A rate may not revive what an observation killed.
        posterior = {
            bucket: (0.0 if bucket in excluded else value)
            for bucket, value in posterior.items()
        }
        residual = sum(posterior.values())
        if residual <= _EPS:
            raise ContractError("metar_four_layer_posterior_zero_mass")
        posterior = {
            bucket: value / residual for bucket, value in posterior.items()
        }
    receipts["phase"] = {
        "schema": PHASE_RECEIPT_SCHEMA,
        "state": phase_state,
        "rise_probability": rise,
        "running_high_bucket": str(running_high_bucket),
        "artifact_sha256": str(
            (hazard_artifact or {}).get("content_sha256") or ""
        ),
    }
    if any(posterior[bucket] != 0.0 for bucket in excluded):
        raise ContractError("metar_four_layer_hard_exclusion_revived")
    return posterior, receipts


__all__ = [
    "PHASE_RECEIPT_SCHEMA",
    "curve_points",
    "curve_shape_realized",
    "interpolate_curve",
    "recent_curve_bias",
    "REANCHOR_RECEIPT_SCHEMA",
    "REMAINING_RECEIPT_SCHEMA",
    "SCHEMA",
    "apply_phase_split",
    "compile_four_layer_posterior",
    "SERVE_ENABLED",
    "feature_row",
    "phase_rise_probability",
    "reanchor_shift_c",
    "remaining_warming_likelihood_ratio",
    "translate_distribution",
]
