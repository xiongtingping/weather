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
) -> tuple[dict[str, float], dict[str, Any]]:
    """Shift by ``delta_c`` and scale about the mean by ``width_lambda``.

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
    if not math.isfinite(shift) or abs(shift) > MAX_ABS_SHIFT_C:
        raise ValueError("center_width_shift_out_of_range")
    if not math.isfinite(lam) or not (
        MIN_WIDTH_LAMBDA <= lam <= MAX_WIDTH_LAMBDA
    ):
        raise ValueError("center_width_lambda_out_of_range")

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
            "width_lambda": lam,
            "distribution_mean_c": mean_c,
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
        "delta_c": shift,
        "width_lambda": lam,
        "distribution_mean_c": mean_c,
        "mass_returned_to_hard_bound": restored,
        "pre_normalisation_mass": total,
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
