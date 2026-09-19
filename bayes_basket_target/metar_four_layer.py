"""The shared arithmetic of P1's joint observation likelihood, and the root curve's helpers.

Charter 150 (2026-09-19) retired the four-layer chain this module was named
for -- the phase split, the remaining-warming likelihood ratio, the reanchor
translation and the fact mask as separate layers -- and its code with it.
What stays is what the joint likelihood (``p1_observation_likelihood``) and
its feature builder still call:

* ``phase_rise_probability`` -- rung one of the ordered hazard chain, the
  probability the day crosses into another bucket from here;
* ``_continuation_distribution`` -- rungs two and up, how far it goes if it
  does;
* the root curve's parsers (``curve_points``, ``interpolate_curve``,
  ``curve_shape_realized``, ``recent_curve_bias``), one definition for the
  sample builder and for serving.

The module keeps its name because the serving fingerprint hashes the
qualified names of the functions it reaches; moving them would read to the
watchdog as a change of arithmetic that did not happen.
"""
from __future__ import annotations

import json
import math
from datetime import datetime
from typing import Any, Mapping, Sequence

from .contracts import ContractError

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


# Charter 156 (2026-09-19): the fitted bin intercepts are read as points at
# their bins' midpoints and joined by straight lines, so the rate stops
# jumping at a bin edge.  The fit is unchanged; only the reading between
# midpoints is.  ZUUU 2026-09-19 at the 10h edge: 0.797 -> 0.379 in one step
# under the bins, 0.65 -> 0.55 across the same half hour read this way.  An
# artifact that does not say ``clock`` keeps its steps.
CLOCK_MIDPOINT_LINEAR = "midpoint_linear"
# The open last bin has no midpoint; its value is read one hour past its
# lower edge, which is past any local day's hours.
_OPEN_BIN_READ_AT = 1.0


def _midpoint_linear(
    hours: float, edges: Sequence[float], intercepts: Sequence[float]
) -> float:
    points: list[tuple[float, float]] = []
    for index, value in enumerate(intercepts):
        if index + 1 >= len(edges):
            break
        low, high = float(edges[index]), float(edges[index + 1])
        middle = (
            (low + high) / 2.0 if high < 1e8 else low + _OPEN_BIN_READ_AT
        )
        points.append((middle, float(value)))
    if not points:
        raise ContractError("metar_four_layer_phase_intercepts_missing")
    if hours <= points[0][0]:
        return points[0][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if hours <= x1:
            return y0 + (hours - x0) / (x1 - x0) * (y1 - y0)
    return points[-1][1]


def phase_rise_probability(
    features: Mapping[str, float | None], artifact: Mapping[str, Any]
) -> float:
    """P(the day's maximum is still above the running high), from the clock
    and the reports alone."""

    phase = artifact["phase"]
    edges = phase["hours_edges"]
    intercepts = phase["bin_intercepts"]
    hours = _finite("remaining_hours", features["remaining_hours"])
    if phase.get("clock") == CLOCK_MIDPOINT_LINEAR:
        intercept = _midpoint_linear(hours, edges, intercepts)
    else:
        intercept = float(intercepts[_hours_bin(hours, edges)])
    eta = intercept + _linear(
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












__all__ = [
    "curve_points",
    "curve_shape_realized",
    "interpolate_curve",
    "phase_rise_probability",
    "recent_curve_bias",
]
