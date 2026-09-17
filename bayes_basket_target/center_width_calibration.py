"""Post-hoc centre and width correction on a served bucket distribution.

Two scalars per stage, measured against settled truth: a shift along the
temperature axis and a scale about the distribution's own mean.  Nothing
upstream changes -- this is the last thing that happens to a distribution
before the band table rescales the claim it produces.

Why both, and why per stage.  Measured over 2026-08-01..15 (1789
station-day-stage-hour cells), P0 runs cold by +0.14C and P1 runs *warm* by
-0.15C: P0 is forecast-only, while P1 has already been pushed upward by the
hard exclusion of buckets the reported high has passed.  A single global
shift is worth nothing out of sample -- fitted on three different cut dates
it comes out +0.062, -0.010 and -0.011, changing sign, and the forward NLL
delta it buys is +0.004, -0.002, -0.002.  Split by stage it is stable and the
pair earns 0.077-0.082 nats forward on all three cuts.

The width is the larger half and the steadier one: lambda lands on 0.75-0.82
in every slice tried (stage, local hour, and both crossed).  The model's
spread is honest about its own centre error -- z has standard deviation 0.85
-- but the centre error itself is larger than it needs to be, so the spread
that matches it is wider than the truth requires.

What this cannot do: create an edge.  Both operations are affine in the
distribution's centre, so an encompassing regression of truth on model and
market centres returns bit-identical coefficients before and after (t_model
0.61 either way).  This corrects what the model *claims*, so that everything
downstream sizing off that claim -- Kelly, the objective's edge, the risk
read -- is computed on a number that means what it says.  It does not move
the model any closer to knowing something the market does not.
"""

from __future__ import annotations

import math
import os
import json
from collections.abc import Mapping, Sequence
from typing import Any

CENTER_WIDTH_METHOD_TAG = "center_width_shift_scale_v1"
# A correction this layer is not entitled to make.  Beyond these the fit is
# describing something structural -- a broken bucket axis, a stage whose
# centre is not what it claims -- and silently bending the distribution onto
# it would hide the fault it should be reporting.
MAX_ABS_SHIFT_C = 1.5
MIN_WIDTH_LAMBDA = 0.5
MAX_WIDTH_LAMBDA = 1.5
# The per-station width scale composes with the stage lambda -- two scalings
# about the same mean multiply -- and carries its own entitlement bounds.
# Measured 2026-08-19 over 522 settled station-days on served envelopes, the
# per-station z standard deviation runs 0.26 (LTAC) to 1.21 (LIMC) around a
# 0.744 pool: the stage lambda can centre that dispersion but cannot flatten
# it, which is the same shape that moved the spread factor from a global
# constant to a per-scope fit.  Each factor is validated separately so each
# layer stays inside its own bounds.
MIN_STATION_WIDTH_SCALE = 0.5
MAX_STATION_WIDTH_SCALE = 1.5
# The composed lambda -- pooled lambda times station scale times floor lift,
# the one number a point's deviation from the mean is actually multiplied by
# -- has a floor of its own (user ruling 2026-09-17, charter 142).  Until then
# the product was allowed to reach 0.25 on the argument that each factor had
# passed its own bounds, and that is exactly what the station width table v1
# did: activated 2026-09-06 09:32Z on a forward gain of +0.028 nat, it served
# 1,611 P0 rows at 0.75 x 0.5 = 0.375 until the incumbent veto retired it on
# 2026-09-10 15:27Z.  Rebuilt from lineage (tmp/r3_zero_bucket_20260917/),
# those rows put the settled truth on an exact-zero bucket 4.59% of the time
# against 0.13% under the pooled 0.75 alone, and cost +0.57 nat against
# identity -- the whole of what the review filed as R3.  The fit and the
# serving path share this function, so a lane cannot buy a width below the
# floor either: a candidate scale that would compose below it is scored, and
# served, at the floor.  Receipts carry the unfloored product beside the
# served one so an audit can see when the floor bound.
MIN_COMPOSED_WIDTH_LAMBDA = 0.5
# The width floor.  One lambda cannot serve a fleet whose own
# claimed spread means different things at different sizes.  Measured
# 2026-09-05 on this stage's own serving cells, the standardised error's
# standard deviation runs 0.95 where the served distribution calls itself
# narrower than 1.44 rungs and 0.67 where it calls itself wider: the pooled
# 0.75 shrinks the first group that is already too narrow and fails to shrink
# the second enough.  The correction is not a second lambda -- fitting one per
# tier was measured and is worse (forward NLL +0.0101 against +0.0132 for the
# floor) -- because the heterogeneity is inside the narrow tier, not between
# the tiers: within it, the narrower the claim the worse it is.  So the claim
# is floored rather than tiered, and the single pooled lambda is refitted
# under the floor.
#
# The lift is bounded for the same reason the shrink is.  Halving a width is
# the most this layer may claim downward; doubling is the mirror of it, and
# beyond that the fit is describing a distribution so degenerate that bending
# it here would hide the fault it should be reporting.
MAX_SIGMA_FLOOR_LIFT = 2.0
MAX_SIGMA_FLOOR_RUNGS = 4.0
# Width tiers (user ruling, 2026-09-05: "上新设计").  One lambda cannot serve
# days whose served spread means different things: measured on P0's own
# serving cells (3,789 cells, 2026-08-27..09-04, under the served 0.75), the
# mean+-1 window realises 0.81 of its claim where the served sigma is under
# one rung, 1.06 at 1.0-1.44, 1.25 at 1.44-2.0, 1.34 at 2.0-2.5 and 1.76
# above three -- the wider the distribution calls itself, the more its core
# under-claims.  The floor above handles the narrow end by lifting; this
# gives each tier of served sigma (in rungs, charter 53(b)) its own lambda,
# fitted on an objective that scores the window the seat rule reads as well
# as the bucket.  The tier is read off the distribution before the transform,
# by one function the fit and serving share, so no distribution is tiered
# one way and served another.
def _parse_tier_boundaries(text: str) -> tuple[float, ...]:
    """Research switch: ascending positive boundaries as a JSON list."""

    cleaned = (text or "").strip()
    if not cleaned:
        return (1.44, 2.0)
    raw = json.loads(cleaned)
    bounds = tuple(float(value) for value in raw)
    if not bounds or any(b <= 0.0 or not math.isfinite(b) for b in bounds):
        raise RuntimeError(f"invalid centre-width tier boundaries: {text}")
    if list(bounds) != sorted(bounds) or len(set(bounds)) != len(bounds):
        raise RuntimeError(f"centre-width tier boundaries must ascend: {text}")
    return bounds


WIDTH_TIER_BOUNDARIES_RUNGS = _parse_tier_boundaries(
    os.environ.get("PM_CENTER_WIDTH_TIER_BOUNDARIES", "")
)
WIDTH_TIER_NAMES = (
    ("narrow", "mid", "wide")
    if len(WIDTH_TIER_BOUNDARIES_RUNGS) == 2
    else tuple(f"tier{i}" for i in range(len(WIDTH_TIER_BOUNDARIES_RUNGS) + 1))
)


def bucket_rung_width_c(centers: Sequence[float]) -> float:
    """The vocabulary's own rung, read off the axis it is serving.

    The smallest adjacent gap.  Every interior gap is exactly the rung, and
    each of the two edge gaps is strictly larger: an open-ended bucket is
    placed half of ``edge_width_c`` beyond its finite side, putting it half a
    rung plus half that width from its neighbour, which exceeds a rung for
    every rung narrower than ``edge_width_c`` -- 1.0 and 10/9 are the only two
    in production.  The minimum therefore reads the rung exactly, on ladders
    of any length.

    The median was tried first and is wrong on short ladders: a five-bucket
    market has four gaps, two of them edges, and the median of an even count
    lands on an edge gap and reads the rung half again too wide.  The minimum
    has no such length dependence, and its one failure direction -- a
    pathologically narrow gap -- reads the rung too small, which makes every
    rule counted in rungs more conservative rather than less.

    Charter 53 forbids deriving this from the unit: a Fahrenheit market's rung
    is 10/9 C, and reading it off the axis gets that right without anyone
    writing the constant down.
    """

    gaps = [
        upper - lower
        for lower, upper in zip(centers, centers[1:])
        if upper > lower
    ]
    if not gaps:
        raise ValueError("center_width_bucket_axis_invalid")
    return min(gaps)


def sigma_floor_lift_v1(
    probability: Mapping[str, float],
    *,
    ordered_keys: Sequence[str],
    key_centers_c: Sequence[float],
    floor_rungs: float,
) -> tuple[float, float, float]:
    """How far a distribution's own spread must be lifted to reach the floor.

    Returns ``(lift, sigma_c, rung_c)``.  The lift is what the width scale is
    multiplied by, so it is 1.0 for every distribution already at or above the
    floor -- the wide tier is not a separate case, it is the case where this
    returns one.

    One implementation, called by the fit and by serving alike.  A floor
    computed two ways would train against one shape and serve another, which
    is the split the spread cap already taught this codebase once.
    """

    keys, centers = _axis(ordered_keys, key_centers_c)
    if set(keys) != set(probability):
        raise ValueError("center_width_bucket_mismatch")
    floor = float(floor_rungs)
    if not math.isfinite(floor) or not 0.0 <= floor <= MAX_SIGMA_FLOOR_RUNGS:
        raise ValueError("center_width_sigma_floor_out_of_range")
    index = {key: position for position, key in enumerate(keys)}
    total = sum(float(value) for value in probability.values())
    if total <= 0.0 or not math.isfinite(total):
        raise ValueError("center_width_probability_empty")
    mean_c = sum(
        centers[index[str(key)]] * float(value) / total
        for key, value in probability.items()
    )
    variance = sum(
        float(value) / total * (centers[index[str(key)]] - mean_c) ** 2
        for key, value in probability.items()
    )
    sigma_c = math.sqrt(max(variance, 0.0))
    rung_c = bucket_rung_width_c(centers)
    if floor <= 0.0:
        return 1.0, sigma_c, rung_c
    if sigma_c <= 0.0:
        # A point mass claims certainty this layer has never been able to
        # justify; it takes the whole entitlement rather than dividing by zero.
        return MAX_SIGMA_FLOOR_LIFT, sigma_c, rung_c
    lift = (floor * rung_c) / sigma_c
    return min(MAX_SIGMA_FLOOR_LIFT, max(1.0, lift)), sigma_c, rung_c


def width_tier_index_v1(
    sigma_rungs: float,
    boundaries: Sequence[float] = WIDTH_TIER_BOUNDARIES_RUNGS,
) -> int:
    """Which width tier a served sigma (in rungs) falls in: the first
    boundary it is below, else the last tier."""

    value = float(sigma_rungs)
    if not math.isfinite(value):
        return 0
    for index, bound in enumerate(boundaries):
        if value < float(bound):
            return index
    return len(boundaries)


def distribution_sigma_rungs_v1(
    probability: Mapping[str, float],
    *,
    ordered_keys: Sequence[str],
    key_centers_c: Sequence[float],
) -> float:
    """The served distribution's own spread in rungs of its vocabulary."""

    _lift, sigma_c, rung_c = sigma_floor_lift_v1(
        probability,
        ordered_keys=ordered_keys,
        key_centers_c=key_centers_c,
        floor_rungs=0.0,
    )
    return sigma_c / rung_c if rung_c > 0.0 else 0.0


def _axis(
    ordered_keys: Sequence[str], key_centers_c: Sequence[float]
) -> tuple[list[str], list[float]]:
    keys = [str(key) for key in ordered_keys]
    centers = [float(center) for center in key_centers_c]
    if len(keys) != len(centers) or len(keys) < 2:
        raise ValueError("center_width_bucket_axis_invalid")
    if any(not math.isfinite(center) for center in centers):
        raise ValueError("center_width_bucket_axis_invalid")
    if any(b <= a for a, b in zip(centers, centers[1:])):
        raise ValueError("center_width_bucket_axis_unordered")
    return keys, centers


def apply_center_width_calibration(
    probability: Mapping[str, float],
    *,
    delta_c: float,
    width_lambda: float,
    ordered_keys: Sequence[str],
    key_centers_c: Sequence[float],
    excluded_buckets: Sequence[str] = (),
    station_width_scale: float = 1.0,
    sigma_floor_rungs: float = 0.0,
    width_tier_lambdas: Sequence[float] | None = None,
    width_tier_boundaries: Sequence[float] | None = None,
) -> tuple[dict[str, float], dict[str, Any]]:
    """Shift by ``delta_c`` and scale about the mean by ``width_lambda``.

    ``width_tier_lambdas`` (width tiers, 2026-09-05) replaces the scalar
    ``width_lambda`` with the lambda of the tier the distribution's own
    served sigma falls in; ``width_lambda`` is then recorded as the pooled
    value the tiers were measured against.

    Mass is carried on bucket centres and split linearly between the two
    centres its new position falls between, the same convention the consensus
    bridge's recentering uses; anything pushed past either end accumulates in
    that edge bucket, which is what the open-ended edge buckets already are.

    Two consequences of doing this on a histogram rather than a density, both
    of which the fit measured on this same function and so are priced into
    the scalars it chose.  A shift moves the realised mean by less than
    ``delta_c`` whenever the edge buckets hold mass, because "hotter than the
    top bucket" has nowhere further to go.  And a scale narrows by less than
    ``width_lambda``, because splitting each point mass between two centres
    adds back s(1-s)*span^2 of variance.  Both are monotone, which is all the
    grid search needs.

    ``excluded_buckets`` are re-zeroed after the transform.  The hard bound is
    an observation -- the day has already been hotter than these -- and a
    correction fitted on averages does not get to put mass back on an outcome
    the day has ruled out.  When the transform would move everything onto
    excluded buckets the original distribution is returned untouched, because
    a distribution with no mass left is not a correction, it is a failure.
    """

    if not probability:
        raise ValueError("center_width_probability_empty")
    keys, centers = _axis(ordered_keys, key_centers_c)
    if set(keys) != set(probability):
        raise ValueError("center_width_bucket_mismatch")
    shift = float(delta_c)
    lam = float(width_lambda)
    scale = float(station_width_scale)
    if not math.isfinite(shift) or abs(shift) > MAX_ABS_SHIFT_C:
        raise ValueError("center_width_shift_out_of_range")
    if not math.isfinite(lam) or not (
        MIN_WIDTH_LAMBDA <= lam <= MAX_WIDTH_LAMBDA
    ):
        raise ValueError("center_width_lambda_out_of_range")
    if not math.isfinite(scale) or not (
        MIN_STATION_WIDTH_SCALE <= scale <= MAX_STATION_WIDTH_SCALE
    ):
        raise ValueError("center_width_station_scale_out_of_range")
    # The floor is read off the input distribution, before anything moves it:
    # it answers "is this claim narrow enough to disbelieve", and the answer
    # must not depend on the correction being decided.  Zero is the whole
    # history of this layer, and returns a lift of exactly one.
    lift, sigma_c, rung_c = sigma_floor_lift_v1(
        probability,
        ordered_keys=ordered_keys,
        key_centers_c=key_centers_c,
        floor_rungs=sigma_floor_rungs,
    )
    if not math.isfinite(lift) or not (
        1.0 <= lift <= MAX_SIGMA_FLOOR_LIFT
    ):
        raise ValueError("center_width_sigma_floor_lift_out_of_range")
    # Composed after each factor passed its own bounds: the product is what a
    # point's deviation from the mean is multiplied by, and the sum is how far
    # along the axis the whole distribution travels.
    tier_index: int | None = None
    tier_lambda: float | None = None
    sigma_rungs = sigma_c / rung_c if rung_c > 0.0 else 0.0
    if width_tier_lambdas:
        boundaries = tuple(
            float(b) for b in (width_tier_boundaries or WIDTH_TIER_BOUNDARIES_RUNGS)
        )
        lambdas = [float(value) for value in width_tier_lambdas]
        if len(lambdas) != len(boundaries) + 1:
            raise ValueError("center_width_tier_lambdas_invalid")
        if any(
            not math.isfinite(value)
            or not (MIN_WIDTH_LAMBDA <= value <= MAX_WIDTH_LAMBDA)
            for value in lambdas
        ):
            raise ValueError("center_width_lambda_out_of_range")
        tier_index = width_tier_index_v1(sigma_rungs, boundaries)
        tier_lambda = lambdas[tier_index]
        lam = tier_lambda
    composed_lambda = lam * scale * lift
    lam = max(MIN_COMPOSED_WIDTH_LAMBDA, composed_lambda)
    composed_floored = lam != composed_lambda
    fitted_shift = shift

    index = {key: position for position, key in enumerate(keys)}
    total_in = sum(float(value) for value in probability.values())
    if total_in <= 0.0 or not math.isfinite(total_in):
        raise ValueError("center_width_probability_empty")
    mean_c = sum(
        centers[index[str(key)]] * float(value) / total_in
        for key, value in probability.items()
    )

    moved = {key: 0.0 for key in keys}
    for key, raw in probability.items():
        weight = float(raw)
        if weight <= 0.0:
            continue
        origin = centers[index[str(key)]]
        position = mean_c + (origin - mean_c) * lam + shift
        if position <= centers[0]:
            moved[keys[0]] += weight
            continue
        if position >= centers[-1]:
            moved[keys[-1]] += weight
            continue
        for lower_index in range(len(centers) - 1):
            lower = centers[lower_index]
            upper = centers[lower_index + 1]
            if lower <= position <= upper:
                span = upper - lower
                upper_share = (position - lower) / span if span > 0 else 0.0
                moved[keys[lower_index]] += weight * (1.0 - upper_share)
                moved[keys[lower_index + 1]] += weight * upper_share
                break

    excluded = {str(bucket) for bucket in excluded_buckets}
    unknown = excluded - set(keys)
    if unknown:
        raise ValueError("center_width_excluded_bucket_unknown")
    restored = 0.0
    if excluded:
        restored = sum(moved[key] for key in excluded)
        for key in excluded:
            moved[key] = 0.0
    total = sum(moved.values())
    if total <= 0.0 or not math.isfinite(total):
        # Everything landed on ruled-out buckets.  Serve the input.
        return dict(probability), {
            "schema": "center_width_receipt_v1",
            "method_tag": CENTER_WIDTH_METHOD_TAG,
            "state": "REFUSED_EMPTY_AFTER_HARD_BOUND",
            "delta_c": shift,
            "fitted_delta_c": fitted_shift,
            "width_lambda": lam,
            "composed_width_lambda_unfloored": composed_lambda,
            "composed_width_lambda_floor": MIN_COMPOSED_WIDTH_LAMBDA,
            "composed_width_lambda_floored": composed_floored,
            "distribution_mean_c": mean_c,
            "sigma_floor_rungs": float(sigma_floor_rungs),
            "sigma_floor_lift": lift,
            "distribution_sigma_c": sigma_c,
            "bucket_rung_c": rung_c,
            "distribution_sigma_rungs": sigma_rungs,
            "width_tier": tier_index,
            "width_tier_lambda": tier_lambda,
            "pooled_width_lambda": float(width_lambda),
            # Served untouched, so the input is also the output -- recorded
            # anyway so every receipt answers the same question the same way.
            "input_probability": {
                str(key): round(float(value) / total_in, 9)
                for key, value in probability.items()
            },
        }
    adjusted = {key: value / total for key, value in moved.items()}
    return adjusted, {
        "schema": "center_width_receipt_v1",
        "method_tag": CENTER_WIDTH_METHOD_TAG,
        "state": (
            "IDENTITY"
            if shift == 0.0 and lam == 1.0
            else "APPLIED"
        ),
        "station_width_scale": scale,
        "delta_c": shift,
        # Equal to ``delta_c`` since the fleet regime shift that composed onto
        # it was removed (2026-09-17); kept so every receipt, old and new,
        # answers the fitted shift under one key.
        "fitted_delta_c": fitted_shift,
        "width_lambda": lam,
        # Charter 142: the product before the composed floor, the floor, and
        # whether it bound.  ``width_lambda`` above is what was served.
        "composed_width_lambda_unfloored": composed_lambda,
        "composed_width_lambda_floor": MIN_COMPOSED_WIDTH_LAMBDA,
        "composed_width_lambda_floored": composed_floored,
        "distribution_mean_c": mean_c,
        # The floor's three numbers travel together: the parameter, the lift
        # it produced, and the spread it was judged against.  A receipt
        # carrying only the composed lambda could not say whether a wide
        # correction was the fitted scalar or a disbelieved claim.
        "sigma_floor_rungs": float(sigma_floor_rungs),
        "sigma_floor_lift": lift,
        "distribution_sigma_c": sigma_c,
        "bucket_rung_c": rung_c,
        "distribution_sigma_rungs": sigma_rungs,
        "width_tier": tier_index,
        "width_tier_lambda": tier_lambda,
        "pooled_width_lambda": float(width_lambda),
        "mass_returned_to_hard_bound": restored,
        "pre_normalisation_mass": total,
        # The distribution this layer was applied TO (charter 138).  The
        # incumbent veto has to score what identity would have served, and
        # that is exactly this -- but it is not recoverable afterwards: the
        # move is a linear map on the bucket vector, and a contraction is
        # rank-deficient.  At lambda 0.75 on an eleven-bucket axis the map
        # has rank nine, so two dimensions of the input are destroyed and
        # any reconstruction is one of infinitely many.  The mean and sigma
        # above were already kept for the same reason and are not enough to
        # score a bucket.  Additive and absent from every row written before
        # this, which readers take as "solve for it or decline".
        "input_probability": {
            str(key): round(float(value) / total_in, 9)
            for key, value in probability.items()
        },
    }


def bucket_centers_from_intervals(
    intervals: Mapping[str, Sequence[float | None]],
    *,
    ordered_keys: Sequence[str],
    edge_width_c: float = 2.0,
) -> list[float]:
    """Centres for the axis, giving open-ended edge buckets a finite width.

    The edge buckets have no far side; ``edge_width_c`` is what the fit used
    to place them, so serving must use the same convention or the correction
    lands somewhere the measurement never looked.
    """

    centers: list[float] = []
    for key in ordered_keys:
        bounds = intervals.get(str(key))
        if (
            bounds is None
            or isinstance(bounds, (str, bytes, bytearray))
            or len(bounds) != 2
        ):
            raise ValueError("center_width_intervals_malformed")
        lower, upper = bounds
        if lower is None and upper is None:
            raise ValueError("center_width_intervals_unbounded")
        if lower is None:
            centers.append(float(upper) - edge_width_c / 2.0)
        elif upper is None:
            centers.append(float(lower) + edge_width_c / 2.0)
        else:
            centers.append((float(lower) + float(upper)) / 2.0)
    return centers
