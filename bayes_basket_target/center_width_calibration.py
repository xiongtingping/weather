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
# Charter 190 (user ruling 2026-09-21: 甲).  An open-ended edge bucket ("22°C
# or below") is an event over a half-line, not a point.  Carried as a point on
# its key centre, a lambda-0.75 contraction moved ZUUU 2026-09-22's 11.6%
# "22°C or below" from 22.0 to 22.9 and split 91% of it into 23°C, serving
# 23 13.0% over 24 11.3% -- a peak no member made -- and zeroed the top edge
# outright.  Over the stable pool (8,032 P0 packets, 09-16..09-21) the
# point rule pushed a median 83% of every open tail >=1% into its neighbour,
# raised that neighbour over its inner neighbour in 108 packets (26
# station-days, 17 of 45 stations) and served an open tail holding >=0.5%
# of the input as <=1e-6 in 1,346 (16.8%); the truth landed on one of those
# zeroed edges in 57 packets (KSEA 09-19, SBGR 09-20).  So an open bucket's
# mass is carried as a tail: exponential, meeting its neighbour's density at
# the shared edge (scale = tail mass x rung / neighbour mass -- charter 53:
# an open bucket's width is not a constant), mapped point by point through
# the same x -> mean + lambda (x - mean) + delta, and re-binned on the real
# bucket edges.  Interior buckets keep the point-and-linear-split
# arithmetic bit for bit.
OPEN_EDGE_TAIL_RULE = "open_edge_exponential_tail_v1"
# Where the served axis puts an open bucket's key centre: half a degree
# beyond its finite bound, the offset ``native_ensemble_p0`` has always
# served.  The key centre still sets the distribution's mean and sigma --
# the anchor, the floor and the tier read those -- it just no longer says
# where the tail's mass goes.
SERVED_OPEN_EDGE_OFFSET_C = 0.5


def served_bucket_axis(
    intervals: Mapping[str, Sequence[float | None]],
    ordered_keys: Sequence[str],
) -> tuple[list[float], tuple[float | None, float | None]]:
    """The width layer's axis: key centres, and the open edges' finite bounds.

    One function for serving and for the fit (charter 190).  Until
    2026-09-21 the fit placed an open bucket's centre a whole degree beyond
    its bound (``center_width_store.EDGE_WIDTH_C = 2.0``) while serving
    placed it half a degree beyond, so the scalars were chosen on a mean, a
    sigma and a transport the served distribution never had.

    Returns ``(centres, (lower_edge, upper_edge))``: ``lower_edge`` is the
    upper bound of an open first bucket, ``upper_edge`` the lower bound of an
    open last bucket, None where that end is closed.
    """

    keys = [str(key) for key in ordered_keys]
    centers: list[float] = []
    for key in keys:
        bounds = intervals.get(key)
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
            centers.append(float(upper) - SERVED_OPEN_EDGE_OFFSET_C)
        elif upper is None:
            centers.append(float(lower) + SERVED_OPEN_EDGE_OFFSET_C)
        else:
            centers.append((float(lower) + float(upper)) / 2.0)
    if not keys:
        return centers, (None, None)
    first = intervals[keys[0]]
    last = intervals[keys[-1]]
    lower_edge = float(first[1]) if first[0] is None else None
    upper_edge = float(last[0]) if last[1] is None else None
    return centers, (lower_edge, upper_edge)


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


def ladder_rung_width_c(closed_centers_c: Sequence[float]) -> float | None:
    """A ladder's rung, read off the centres of its closed buckets.

    The one reading of the rung (charter 198): the width layer's kernel and
    the trading layer's rung-counted rules (``runtime._bucket_rung_width_c``)
    both call this, so a Celsius ladder reads 1.0 and a Fahrenheit one 10/9
    in both places.  The centres are sorted, the steps between neighbours
    sorted, and the upper median taken -- the runtime's reading, unchanged.
    ``None`` when fewer than two centres or a non-positive median step leave
    nothing to read.

    Charter 53 forbids deriving this from the unit: a Fahrenheit market's rung
    is 10/9 C, and reading it off the ladder gets that right without anyone
    writing the constant down.
    """

    centers = sorted(float(center) for center in closed_centers_c)
    if len(centers) < 2:
        return None
    steps = sorted(
        upper - lower for lower, upper in zip(centers, centers[1:])
    )
    rung = steps[len(steps) // 2]
    return rung if rung > 0.0 else None


def bucket_rung_width_c(centers: Sequence[float]) -> float:
    """The vocabulary's own rung, read off the axis it is serving.

    ``centers`` is a width-layer axis: ``served_bucket_axis``'s key centres,
    or the holdout's integer grid.  Its two end buckets are dropped and the
    rest read by ``ladder_rung_width_c``.  On a served axis the ends are the
    open buckets ("22C or below"), whose key centre sits
    ``SERVED_OPEN_EDGE_OFFSET_C`` (0.5 C) past the finite bound -- not half a
    rung on a Fahrenheit ladder -- so the edge step is 0.5 + 5/9 = 1.056 C
    against a 10/9 C rung.  Until charter 198 this took the smallest step,
    which read exactly that 1.056 on every Fahrenheit ladder (charter 190's
    offset broke the "edge steps are always wider" premise the minimum rested
    on) while the trading layer read 1.111 off the same ladder.  On a closed
    axis the ends are ordinary buckets and dropping them loses nothing.  An
    axis of three or fewer buckets has no interior step, so all of it is read.
    """

    values = [float(center) for center in centers]
    interior = values[1:-1] if len(values) >= 4 else values
    rung = ladder_rung_width_c(interior)
    if rung is None:
        raise ValueError("center_width_bucket_axis_invalid")
    return rung


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


def _open_edges(
    open_edge_bounds_c: Sequence[float | None] | None,
    centers: Sequence[float],
) -> tuple[float | None, float | None]:
    """The declared open edges, checked against the axis they bound.

    An open first bucket's bound must lie strictly between its key centre and
    the next one (the served axis puts it half a degree past the centre);
    anything else is an axis the tail cannot be placed on, and failing here
    is better than re-binning a tail across the wrong edge.
    """

    if open_edge_bounds_c is None:
        return None, None
    if (
        isinstance(open_edge_bounds_c, (str, bytes, bytearray))
        or len(open_edge_bounds_c) != 2
    ):
        raise ValueError("center_width_open_edge_bounds_invalid")
    lower, upper = open_edge_bounds_c
    lower = None if lower is None else float(lower)
    upper = None if upper is None else float(upper)
    if lower is not None and not (
        math.isfinite(lower) and centers[0] < lower < centers[1]
    ):
        raise ValueError("center_width_open_edge_bounds_invalid")
    if upper is not None and not (
        math.isfinite(upper) and centers[-2] < upper < centers[-1]
    ):
        raise ValueError("center_width_open_edge_bounds_invalid")
    if (
        len(centers) == 2
        and lower is not None
        and upper is not None
        and lower != upper
    ):
        raise ValueError("center_width_open_edge_bounds_invalid")
    return lower, upper


def _bucket_edges_c(
    centers: Sequence[float], lower: float | None, upper: float | None
) -> list[float]:
    """The edges between adjacent buckets, in order (one fewer than buckets).

    Interior edges are the midpoints of adjacent centres, which is exact for
    every ladder in production: interior buckets are contiguous and of one
    width (charter 53).  A declared open edge is its own bound.
    """

    edges = [
        (float(a) + float(b)) / 2.0 for a, b in zip(centers, centers[1:])
    ]
    if lower is not None:
        edges[0] = lower
    if upper is not None:
        edges[-1] = upper
    return edges


def _open_edge_tail_landing(
    weight: float,
    *,
    side: str,
    bound_c: float,
    adjacent_weight: float,
    rung_c: float,
    edges_c: Sequence[float],
    mean_c: float,
    lam: float,
    shift: float,
) -> tuple[list[float], float | None]:
    """Where an open bucket's mass lands under x -> mean + lam (x - mean) + shift.

    The tail is exponential and meets its neighbour's density at the shared
    edge: density ``adjacent_weight / rung_c`` there, total mass ``weight``,
    so its scale is ``weight * rung_c / adjacent_weight``.  A heavy tail over
    a thin neighbour is long and little of it crosses; a thin tail beside a
    heavy neighbour sits against the edge and follows it.  The mass below
    (above) each edge after the map is the tail's mass below (above) that
    edge's preimage, so each bucket gets the difference -- the same map every
    interior centre is moved by, applied to a half-line instead of a point.

    A neighbour with no mass gives a density of zero at the edge: the tail
    is infinitely long and nothing crosses.  Returns the per-bucket landing
    (buckets in axis order) and the scale, None in that case.
    """

    size = len(edges_c) + 1
    landing = [0.0] * size
    if adjacent_weight <= 0.0:
        landing[0 if side == "lower" else size - 1] = weight
        return landing, None
    scale = weight * rung_c / adjacent_weight

    def preimage(x: float) -> float:
        return mean_c + (x - shift - mean_c) / lam

    previous = 0.0
    if side == "lower":
        for position, edge in enumerate(edges_c):
            source = preimage(edge)
            cumulative = (
                weight if source >= bound_c
                else weight * math.exp((source - bound_c) / scale)
            )
            landing[position] = cumulative - previous
            previous = cumulative
        landing[size - 1] = weight - previous
        return landing, scale
    for position in range(size - 2, -1, -1):
        source = preimage(edges_c[position])
        cumulative = (
            weight if source <= bound_c
            else weight * math.exp(-(source - bound_c) / scale)
        )
        landing[position + 1] = cumulative - previous
        previous = cumulative
    landing[0] = weight - previous
    return landing, scale


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
    open_edge_bounds_c: Sequence[float | None] | None = None,
) -> tuple[dict[str, float], dict[str, Any]]:
    """Shift by ``delta_c`` and scale about the mean by ``width_lambda``.

    ``open_edge_bounds_c`` (charter 190) declares which end buckets are open
    and where their finite bound is -- ``served_bucket_axis`` returns it
    beside the centres.  A declared open bucket's mass is carried as a tail
    (``_open_edge_tail_landing``) rather than as a point on its key centre.
    Everything else is unchanged: the mean, sigma, floor and tier are read
    off the key centres exactly as before, and interior buckets move as
    before bit for bit, so an axis with no open end declared -- the holdout's
    integer grid -- or an open end holding no mass serves what it always
    served.  Two things do change where a tail holds mass: the served mean
    read on key centres is no longer exactly ``mean + delta`` (a tail is not
    a point on its centre; measured on the stable pool, median -0.004C, p10
    -0.031, p90 +0.021), and the realised sigma ratio moves with it (median
    0.791 -> 0.799).

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

    open_lower, open_upper = _open_edges(open_edge_bounds_c, centers)
    open_edges_declared = open_lower is not None or open_upper is not None
    edges_c = (
        _bucket_edges_c(centers, open_lower, open_upper)
        if open_edges_declared else []
    )
    tail_scale: dict[str, float | None] = {"lower": None, "upper": None}
    tail_released: dict[str, float] = {"lower": 0.0, "upper": 0.0}

    moved = {key: 0.0 for key in keys}
    for key, raw in probability.items():
        weight = float(raw)
        if weight <= 0.0:
            continue
        position_index = index[str(key)]
        side = (
            "lower" if open_lower is not None and position_index == 0
            else "upper"
            if open_upper is not None and position_index == len(keys) - 1
            else None
        )
        if side is not None:
            adjacent = keys[1] if side == "lower" else keys[-2]
            landing, scale = _open_edge_tail_landing(
                weight,
                side=side,
                bound_c=open_lower if side == "lower" else open_upper,
                adjacent_weight=float(probability.get(adjacent, 0.0)),
                rung_c=rung_c,
                edges_c=edges_c,
                mean_c=mean_c,
                lam=lam,
                shift=shift,
            )
            for bucket_index, share in enumerate(landing):
                moved[keys[bucket_index]] += share
            tail_scale[side] = scale
            tail_released[side] = weight - landing[position_index]
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
    # Charter 190: present only where an open end was declared, so a caller
    # on a closed axis (the holdout's integer grid) keeps its receipt as is.
    # ``open_edge_mass_released`` is how much of each open bucket's own mass
    # the map carried out of it; the scale is None where its neighbour held
    # nothing (an infinitely long tail, nothing crosses).
    open_edge_receipt: dict[str, Any] = (
        {
            "open_edge_rule": OPEN_EDGE_TAIL_RULE,
            "open_edge_bounds_c": [open_lower, open_upper],
            "open_edge_tail_scale_c": dict(tail_scale),
            "open_edge_mass_released": dict(tail_released),
        }
        if open_edges_declared else {}
    )
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
            **open_edge_receipt,
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
        **open_edge_receipt,
    }


def center_width_adjusted_grid(
    probability: Mapping[str, float],
    *,
    ordered_keys: Sequence[str],
    key_centers_c: Sequence[float],
    shifts: Sequence[float],
    lambdas: Sequence[float],
    excluded_buckets: Sequence[str] = (),
    station_width_scale: float = 1.0,
    sigma_floor_rungs: float = 0.0,
    open_edge_bounds_c: Sequence[float | None] | None = None,
):
    """``apply_center_width_calibration``'s adjusted vector at many pairs at once.

    Row ``g`` is, bit for bit, the first element of
    ``apply_center_width_calibration(probability, delta_c=shifts[g],
    width_lambda=lambdas[g], ...)`` with the buckets in axis order (charter
    202 follow-up, 2026-09-23).  The width lane scores a 33 x 29 grid, then a
    tier grid, then refits the whole grid on every rolling-origin prefix; once
    the evidence stopped resetting at every bump it had ten days of cells and
    the scalar path needed hours, so the daily refit could never finish inside
    its slot.  Nothing here is a second rule: every source bucket is walked
    in the scalar function's order and every value is formed by the same
    operations in the same order, vectorised only across the pairs -- which
    is why ``math.exp`` is applied element by element rather than numpy's own
    exponential, whose last bit is not the platform's, and why every total is
    Python's own compensated ``sum`` row by row.  No tier lambdas: the
    lane searches tiers one scalar lambda at a time.  Pinned against the
    scalar function by tests/test_center_width_adjusted_grid.py.
    """

    import numpy as np

    if not probability:
        raise ValueError("center_width_probability_empty")
    keys, centers = _axis(ordered_keys, key_centers_c)
    if set(keys) != set(probability):
        raise ValueError("center_width_bucket_mismatch")
    shift = np.asarray([float(value) for value in shifts], dtype=float)
    lam_in = np.asarray([float(value) for value in lambdas], dtype=float)
    if shift.shape != lam_in.shape or shift.ndim != 1:
        raise ValueError("center_width_grid_shape")
    if not np.isfinite(shift).all() or (np.abs(shift) > MAX_ABS_SHIFT_C).any():
        raise ValueError("center_width_shift_out_of_range")
    if not np.isfinite(lam_in).all() or (
        (lam_in < MIN_WIDTH_LAMBDA) | (lam_in > MAX_WIDTH_LAMBDA)
    ).any():
        raise ValueError("center_width_lambda_out_of_range")
    scale = float(station_width_scale)
    if not math.isfinite(scale) or not (
        MIN_STATION_WIDTH_SCALE <= scale <= MAX_STATION_WIDTH_SCALE
    ):
        raise ValueError("center_width_station_scale_out_of_range")
    lift, _sigma_c, rung_c = sigma_floor_lift_v1(
        probability,
        ordered_keys=ordered_keys,
        key_centers_c=key_centers_c,
        floor_rungs=sigma_floor_rungs,
    )
    if not math.isfinite(lift) or not (1.0 <= lift <= MAX_SIGMA_FLOOR_LIFT):
        raise ValueError("center_width_sigma_floor_lift_out_of_range")
    lam = np.maximum(MIN_COMPOSED_WIDTH_LAMBDA, lam_in * scale * lift)
    index = {key: position for position, key in enumerate(keys)}
    total_in = sum(float(value) for value in probability.values())
    if total_in <= 0.0 or not math.isfinite(total_in):
        raise ValueError("center_width_probability_empty")
    mean_c = sum(
        centers[index[str(key)]] * float(value) / total_in
        for key, value in probability.items()
    )
    open_lower, open_upper = _open_edges(open_edge_bounds_c, centers)
    open_edges_declared = open_lower is not None or open_upper is not None
    edges_c = (
        _bucket_edges_c(centers, open_lower, open_upper)
        if open_edges_declared else []
    )
    count = len(keys)
    pairs = shift.shape[0]
    centers_array = np.asarray(centers, dtype=float)
    moved = np.zeros((pairs, count))
    for key, raw in probability.items():
        weight = float(raw)
        if weight <= 0.0:
            continue
        position_index = index[str(key)]
        side = (
            "lower" if open_lower is not None and position_index == 0
            else "upper"
            if open_upper is not None and position_index == count - 1
            else None
        )
        if side is not None:
            adjacent = keys[1] if side == "lower" else keys[-2]
            landing = _open_edge_tail_landing_grid(
                weight,
                side=side,
                bound_c=open_lower if side == "lower" else open_upper,
                adjacent_weight=float(probability.get(adjacent, 0.0)),
                rung_c=rung_c,
                edges_c=edges_c,
                mean_c=mean_c,
                lam=lam,
                shift=shift,
            )
            for bucket_index in range(count):
                moved[:, bucket_index] += landing[:, bucket_index]
            continue
        origin = centers[position_index]
        position = mean_c + (origin - mean_c) * lam + shift
        low = position <= centers[0]
        high = ~low & (position >= centers[-1])
        middle = ~(low | high)
        moved[low, 0] += weight
        moved[high, count - 1] += weight
        rows = np.nonzero(middle)[0]
        if rows.size:
            inner = position[rows]
            lower_index = np.searchsorted(centers_array, inner, side="left") - 1
            lower = centers_array[lower_index]
            span = centers_array[lower_index + 1] - lower
            upper_share = np.where(span > 0, (inner - lower) / span, 0.0)
            moved[rows, lower_index] += weight * (1.0 - upper_share)
            moved[rows, lower_index + 1] += weight * upper_share
    excluded = {str(bucket) for bucket in excluded_buckets}
    if excluded - set(keys):
        raise ValueError("center_width_excluded_bucket_unknown")
    for key in excluded:
        moved[:, index[key]] = 0.0
    # Python's ``sum`` over floats is compensated (Neumaier, 3.12+), not a
    # left-to-right add, so the scalar total is reproduced row by row with
    # the same ``sum`` over the same order rather than with numpy.
    total = np.asarray([sum(row) for row in moved.tolist()], dtype=float)
    refused = ~(total > 0.0) | ~np.isfinite(total)
    safe_total = np.where(refused, 1.0, total)
    adjusted = moved / safe_total[:, None]
    if refused.any():
        # Everything landed on ruled-out buckets: apply serves the input.
        # Flagged, because apply returns that input in the caller's own key
        # order and a caller summing it must follow that order to agree.
        adjusted[refused] = np.asarray(
            [float(probability[key]) for key in keys], dtype=float
        )
    return adjusted, refused


def _open_edge_tail_landing_grid(
    weight: float,
    *,
    side: str,
    bound_c: float,
    adjacent_weight: float,
    rung_c: float,
    edges_c: Sequence[float],
    mean_c: float,
    lam,
    shift,
):
    """``_open_edge_tail_landing`` at every (lam, shift) pair; same arithmetic."""

    import numpy as np

    size = len(edges_c) + 1
    pairs = shift.shape[0]
    landing = np.zeros((pairs, size))
    if adjacent_weight <= 0.0:
        landing[:, 0 if side == "lower" else size - 1] = weight
        return landing
    scale = weight * rung_c / adjacent_weight

    def _tail(past, exponent):
        # ``math.exp`` only where the scalar path takes it: a row past the
        # bound never evaluates the exponential there, and could overflow.
        out = np.full(past.shape, weight, dtype=float)
        for row in np.nonzero(~past)[0].tolist():
            out[row] = weight * math.exp(float(exponent[row]))
        return out

    previous = np.zeros(pairs)
    # The exponent is formed for every pair and used only where the row is
    # not past the bound (the scalar path forms it only there); a past row's
    # quotient may overflow and is discarded.
    with np.errstate(over="ignore", divide="ignore", invalid="ignore"):
        if side == "lower":
            for position, edge in enumerate(edges_c):
                source = mean_c + (edge - shift - mean_c) / lam
                past = source >= bound_c
                cumulative = _tail(past, (source - bound_c) / scale)
                landing[:, position] = cumulative - previous
                previous = cumulative
            landing[:, size - 1] = weight - previous
            return landing
        for position in range(size - 2, -1, -1):
            source = mean_c + (edges_c[position] - shift - mean_c) / lam
            past = source <= bound_c
            cumulative = _tail(past, -(source - bound_c) / scale)
            landing[:, position + 1] = cumulative - previous
            previous = cumulative
        landing[:, 0] = weight - previous
        return landing


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
