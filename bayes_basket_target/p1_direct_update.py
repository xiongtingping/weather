"""P1's observation update fitted directly on the final P1 (charter 161).

    P1(k)  proportional to  root(k) * M(k) * exp f(k)

    f(k) = rise_k * (x . theta_rise) + slope_k * (x . theta_slope)
           + slope_k^2 * theta_slope_sq + open_top_k * theta_open_top

User ruling 2026-09-20 (「上线b2」).  Charter 150 multiplied the root by
g(d | s)**alpha, where g is a conditional *predictive* distribution of the
remaining rise given the reports -- not a likelihood of the reports given the
day's outcome.  Multiplying a prior by a predictive distribution counts the
training base rate a second time, and one alpha per hour cannot take a
bucket-dependent base back out (review 2026-09-19).  Measured on 9 forward
days, the served P1 was over-confident: calibration slope 0.86, claims of
0.57 realising 0.50 and of 0.80 realising 0.72.

Here the update itself is what is learned: the log-probability the reports
add to each bucket on top of the root, fitted by maximising the final P1's
log score over settled station-days with a ridge that shrinks f toward zero
(the root).  Nothing assumes the root and the reports are independent; the
root's own belief about the rise enters as a feature, so where they overlap
the fit learns to not count it twice.

A bucket is described the way g's served branch reads it
(``p1_observation_likelihood.rise_increment_c`` / ``nearest_rung``): rung 0
is the running high's bucket (and anything within half a degree of the
running high), rung r >= 1 the buckets above it.  ``rise`` is 1[r >= 1],
``slope`` is (r - 1)+, ``open_top`` marks an open top on the rise branch.

x is the state: an intercept, hat functions of the station clock at the
alpha knots, the reports' features and two root features (the masked root's
mass above the running bucket, and its mean minus the running high), all
standardised on the training rows, and the same standardised block again
inside the last six hours and inside six to ten hours -- the reports' slopes
differ at the end of the day, which is what g**alpha(h) carried and a
hour-constant slope could not.

The parameters ride in the P1 observation-likelihood artifact under
``direct_update``, so a packet that carries the artifact rebuilds the
envelope exactly.  They serve only while the block's own state is SERVE;
otherwise P1 is charter 150's g**alpha as before.

The two clock regimes were steps (schema v1), and a step in x is a step in
the served P1: measured 2026-09-21 on the real states within half an hour of
10h, moving only the clock across it moved the belief by a median 8.3
points (26 at most), every time toward more rise above the edge, with no
report in between.  Schema v2 carries ``clock_gate``: the same two regimes,
each edge a linear ramp of half-width w, so f is continuous in the clock.
A v1 block (no gate) is read exactly as it always was.
"""
from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

from . import p1_observation_likelihood as OL
from .contracts import ContractError

BLOCK_KEY = "direct_update"
# The continuous-clock candidate's key in its own file
# (``fit_p1_observation_likelihood.py --candidate-out``), walked forward on the
# served block's days.  Serving never reads it; switching is a refit that
# publishes a v2 block under ``BLOCK_KEY``.
CANDIDATE_BLOCK_KEY = "direct_update_candidate"
SCHEMA = "p1_direct_log_update_v1"
SCHEMA_V2 = "p1_direct_log_update_v2"
RULE = "p1_root_x_mask_x_exp_direct_log_update_v1"
RULE_V2 = "p1_root_x_mask_x_exp_direct_log_update_v2"
UPDATE = "DIRECT_LOG_UPDATE_V1"
GATE_SHADOW = "SHADOW"
CLOCK_GATE_SHAPE = "linear_ramp_v1"
GATE_SERVE = "SERVE"
FEATURES: tuple[str, ...] = tuple(
    name for name in OL.FULL_FEATURES if name != "remaining_hours"
)
ROOT_FEATURES: tuple[str, ...] = ("root_rise_mass", "root_mean_minus_high_c")
STATE_COLUMNS: tuple[str, ...] = FEATURES + ROOT_FEATURES
KNOTS_HOURS: tuple[float, ...] = tuple(float(k) for k in OL.ALPHA_KNOTS_HOURS)
LATE_BELOW_HOURS = 6.0
MID_BELOW_HOURS = 10.0
# Ramps wider than this would overlap between the two edges.
MAX_GATE_HALF_WIDTH_HOURS = (MID_BELOW_HOURS - LATE_BELOW_HOURS) / 2.0
MAX_RUNG = 12
# Charter 274 (user ruling 2026-10-16 「按校准改善上线傍晚臂」): three evening
# columns at the end of x, present when the block carries ``evening``.  The
# current form over-claimed the rise in the evening the further the root sat
# above the running high (root 1-2 C above: said 6.7 % against 2.9 % realised,
# >= 2 C: 8.0 % against 3.6 %); the columns let f fade the root's headroom with
# the clock and with the hours past the peak (docs/research/p1_evening_arm_20261016).
EVENING_SCHEMA = "p1_direct_update_evening_v1"
EVENING_COLUMNS: tuple[str, ...] = ("evening_clock_x_root_above_high", "evening_hours_past_peak",
                                    "evening_hours_past_peak_x_root_above_high")


def gate_half_width(block: Mapping[str, Any]) -> float:
    """The block's ramp half-width in hours; 0.0 (the v1 steps) without a gate."""

    gate = block.get("clock_gate")
    if gate is None:
        return 0.0
    if not isinstance(gate, Mapping) or gate.get("shape") != CLOCK_GATE_SHAPE:
        raise ContractError("p1_direct_update_clock_gate_invalid")
    width = float(gate.get("half_width_hours"))
    if not (0.0 < width <= MAX_GATE_HALF_WIDTH_HOURS):
        raise ContractError("p1_direct_update_clock_gate_invalid")
    return width


def clock_gates(clock_hours: float, half_width_hours: float = 0.0) -> tuple[float, float]:
    """``(late, mid)``: how much of each regime's block the clock switches on.

    Half-width 0 is v1: late = 1[h < 6], mid = 1[6 <= h < 10].  Otherwise
    each edge becomes a ramp over [edge - w, edge + w], and at every clock
    late + mid is what the step would have been away from the edges.
    """

    hours = float(clock_hours)
    width = float(half_width_hours)
    if width <= 0.0:
        late = 1.0 if hours < LATE_BELOW_HOURS else 0.0
        mid = 1.0 if LATE_BELOW_HOURS <= hours < MID_BELOW_HOURS else 0.0
        return late, mid

    def below(edge: float) -> float:
        return min(1.0, max(0.0, (edge + width - hours) / (2.0 * width)))

    below_late = below(LATE_BELOW_HOURS)
    return below_late, below(MID_BELOW_HOURS) - below_late


def x_layout(block: Mapping[str, Any] | None = None) -> list[str]:
    """The names of x's columns, in order -- written into the block.  A block
    carrying ``evening`` (charter 274) adds the three evening columns."""

    return (
        ["intercept"]
        + [f"clock_hat_{knot:g}h" for knot in KNOTS_HOURS]
        + list(STATE_COLUMNS)
        + [f"late_x_{name}" for name in STATE_COLUMNS]
        + [f"mid_x_{name}" for name in STATE_COLUMNS]
        + (list(EVENING_COLUMNS) if block is not None and "evening" in block else [])
    )


def evening_raw(clock_hours: float, root_mean_minus_high_c: float, fleet_peak_local_h: float) -> list[float]:
    """The three evening columns before standardising: the station clock times
    the root's headroom over the running high, the hours past the fleet's peak
    on the station clock, and their product with the headroom."""

    past = max(0.0, 24.0 - float(fleet_peak_local_h) - float(clock_hours))
    headroom = float(root_mean_minus_high_c)
    return [float(clock_hours) * headroom, past, past * headroom]


def bucket_bases(
    *,
    bucket_ordinals: Mapping[str, int],
    bucket_centers_c: Mapping[str, float],
    bucket_upper_bounds_c: Mapping[str, Any],
    running_high_c: float,
    running_high_bucket: str,
    hard_excluded_buckets: Sequence[str] = (),
) -> dict[str, tuple[int, float, float, float]]:
    """``bucket -> (rung, rise, slope, open_top)`` for every bucket.

    Excluded buckets read rung -1 and zero bases: the mask removes them, and
    a zero update keeps them out of the arithmetic.
    """

    if str(running_high_bucket) not in bucket_ordinals:
        raise ContractError("p1_direct_update_running_high_bucket_unknown")
    excluded = {str(bucket) for bucket in hard_excluded_buckets}
    floor = int(bucket_ordinals[str(running_high_bucket)])
    out: dict[str, tuple[int, float, float, float]] = {}
    for bucket, ordinal in bucket_ordinals.items():
        bucket = str(bucket)
        if bucket in excluded:
            out[bucket] = (-1, 0.0, 0.0, 0.0)
            continue
        increment = OL.rise_increment_c(
            ordinal, floor, bucket_centers_c[bucket], running_high_c
        )
        rung = 0 if increment is None else int(
            OL.nearest_rung(increment, range(1, MAX_RUNG + 1))
        )
        open_top = float(rung >= 1 and bucket_upper_bounds_c.get(bucket) is None)
        out[bucket] = (
            rung, float(rung >= 1), float(max(rung - 1, 0)), open_top,
        )
    return out


def root_features(
    root_probabilities: Mapping[str, float],
    bases: Mapping[str, tuple[int, float, float, float]],
    *,
    bucket_centers_c: Mapping[str, float],
    running_high_c: float,
) -> dict[str, float]:
    """The masked root's mass above the running bucket and its mean over the high."""

    live = [bucket for bucket, base in bases.items() if base[0] >= 0]
    mass = sum(float(root_probabilities.get(bucket, 0.0)) for bucket in live)
    if not mass > 0.0:
        raise ContractError("p1_direct_update_root_zero_on_support")
    above = sum(
        float(root_probabilities.get(bucket, 0.0))
        for bucket in live if bases[bucket][1] > 0.0
    )
    mean = sum(
        float(root_probabilities.get(bucket, 0.0)) * float(bucket_centers_c[bucket])
        for bucket in live
    ) / mass
    return {
        "root_rise_mass": above / mass,
        "root_mean_minus_high_c": mean - float(running_high_c),
    }


def state_vector(
    features: Mapping[str, float | None],
    root: Mapping[str, float],
    clock_hours: float,
    block: Mapping[str, Any],
) -> list[float]:
    """x, standardised with the block's own means and scales (``x_layout``)."""

    fallbacks = block["fallbacks"]
    raw = [
        float(features[name]) if features.get(name) is not None else float(fallbacks[name])
        for name in FEATURES
    ] + [float(root[name]) for name in ROOT_FEATURES]
    means = [float(value) for value in block["mean"]]
    scales = [float(value) for value in block["scale"]]
    standard = [(value - m) / s for value, m, s in zip(raw, means, scales)]
    hats = [
        OL._piecewise_linear(
            float(clock_hours), KNOTS_HOURS,
            [1.0 if j == k else 0.0 for j in range(len(KNOTS_HOURS))],
        )
        for k in range(len(KNOTS_HOURS))
    ]
    late, mid = clock_gates(clock_hours, gate_half_width(block))
    evening = []
    if "evening" in block:
        spec = block["evening"]
        raw_evening = evening_raw(clock_hours, float(root["root_mean_minus_high_c"]), float(spec["fleet_peak_local_h"]))
        evening = [(value - float(m)) / float(s) for value, m, s in zip(raw_evening, spec["mean"], spec["scale"])]
    return (
        [1.0] + hats + standard
        + [late * value for value in standard]
        + [mid * value for value in standard]
        + evening
    )


def log_update(
    block: Mapping[str, Any],
    x: Sequence[float],
    bases: Mapping[str, tuple[int, float, float, float]],
) -> dict[str, float]:
    """f(k) per bucket."""

    rise_u = math.fsum(float(t) * v for t, v in zip(block["theta_rise"], x))
    slope_u = math.fsum(float(t) * v for t, v in zip(block["theta_slope"], x))
    squared = float(block["theta_slope_sq"])
    top = float(block["theta_open_top"])
    return {
        bucket: (
            rise * rise_u + slope * slope_u + slope * slope * squared
            + open_top * top
        )
        for bucket, (_rung, rise, slope, open_top) in bases.items()
    }


def serving_block(artifact: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    """The block when it is well formed and its own gate says SERVE; else None."""

    block = (artifact or {}).get(BLOCK_KEY)
    if not isinstance(block, Mapping) or block.get("state") != GATE_SERVE:
        return None
    validate_block(block)
    return block


def block_rule(block: Mapping[str, Any]) -> str:
    """The receipt's rule: v2 names the continuous clock."""

    return RULE_V2 if block.get("schema") == SCHEMA_V2 else RULE


def validate_block(block: Mapping[str, Any]) -> None:
    """Raise ContractError unless the block is a well-formed v1 or v2 block.

    v1 must carry no gate (a gate it did not fit under would change what it
    serves); v2 must carry a valid one.
    """

    schema = block.get("schema")
    if schema == SCHEMA:
        if "clock_gate" in block:
            raise ContractError("p1_direct_update_clock_gate_invalid")
    elif schema == SCHEMA_V2:
        # A null gate would read as the v1 steps; v2 must carry a real one.
        if not isinstance(block.get("clock_gate"), Mapping):
            raise ContractError("p1_direct_update_clock_gate_invalid")
    else:
        raise ContractError("p1_direct_update_schema_invalid")
    gate_half_width(block)
    if "evening" in block:
        spec = block["evening"]
        if (
            not isinstance(spec, Mapping) or spec.get("schema") != EVENING_SCHEMA
            or len(spec.get("mean") or []) != len(EVENING_COLUMNS)
            or len(spec.get("scale") or []) != len(EVENING_COLUMNS)
            or any(not float(s) > 0.0 for s in spec["scale"])
            or not all(math.isfinite(float(v)) for v in list(spec["mean"]) + list(spec["scale"]) + [spec.get("fleet_peak_local_h")])
        ):
            raise ContractError("p1_direct_update_evening_invalid")
    width = len(x_layout(block))
    if (
        list(block.get("x_layout") or []) != x_layout(block)
        or len(block.get("theta_rise") or []) != width
        or len(block.get("theta_slope") or []) != width
        or len(block.get("mean") or []) != len(STATE_COLUMNS)
        or len(block.get("scale") or []) != len(STATE_COLUMNS)
        or any(not float(s) > 0.0 for s in block["scale"])
    ):
        raise ContractError("p1_direct_update_block_invalid")
    numbers = (
        list(block["theta_rise"]) + list(block["theta_slope"])
        + [block["theta_slope_sq"], block["theta_open_top"]]
        + list(block["mean"]) + list(block["scale"])
    )
    if not all(math.isfinite(float(value)) for value in numbers):
        raise ContractError("p1_direct_update_block_invalid")


def compile_direct_update(
    features: Mapping[str, float | None],
    artifact: Mapping[str, Any],
    *,
    root_probabilities: Mapping[str, float],
    bucket_ordinals: Mapping[str, int],
    bucket_centers_c: Mapping[str, float],
    bucket_upper_bounds_c: Mapping[str, Any],
    running_high_c: float,
    running_high_bucket: str,
    hard_excluded_buckets: Sequence[str] = (),
    station_id: str | None = None,
) -> tuple[dict[str, float], dict[str, Any]]:
    """exp f per bucket (best bucket 1) and its receipt.

    The receipt is P1's observation receipt (schema and state as charter
    150's, so the order gate reads it unchanged) with ``update`` and ``rule``
    naming this arithmetic.  Raises ContractError on a malformed block or a
    root with no mass on the support; the caller decides what a refusal
    serves.
    """

    block = serving_block(artifact)
    if block is None:
        raise ContractError("p1_direct_update_not_serving")
    bases = bucket_bases(
        bucket_ordinals=bucket_ordinals, bucket_centers_c=bucket_centers_c,
        bucket_upper_bounds_c=bucket_upper_bounds_c,
        running_high_c=running_high_c, running_high_bucket=running_high_bucket,
        hard_excluded_buckets=hard_excluded_buckets,
    )
    root = root_features(
        root_probabilities, bases,
        bucket_centers_c=bucket_centers_c, running_high_c=running_high_c,
    )
    hours = OL.clock_hours(features["remaining_hours"], artifact, station_id)
    x = state_vector(features, root, hours, block)
    f = log_update(block, x, bases)
    live = [bucket for bucket, base in bases.items() if base[0] >= 0]
    top = max(f[bucket] for bucket in live)
    likelihoods = {bucket: math.exp(value - top) for bucket, value in f.items()}
    weighted = {
        bucket: float(root_probabilities.get(bucket, 0.0)) * likelihoods[bucket]
        for bucket in live
    }
    total = sum(weighted.values())
    if not total > 0.0:
        raise ContractError("p1_direct_update_zero_on_support")
    receipt = {
        "schema": OL.RECEIPT_SCHEMA,
        "rule": block_rule(block),
        "update": UPDATE,
        "state": "APPLIED",
        "artifact_sha256": str(artifact.get("content_sha256") or ""),
        "trained_through": str(block.get("trained_through") or ""),
        "lambda": block.get("lambda"),
        **({"clock_gate": dict(block["clock_gate"])} if "clock_gate" in block else {}),
        # Charter 274: the evening columns' raw values, when the block has them.
        **({"evening": {
            "schema": EVENING_SCHEMA,
            "values": dict(zip(EVENING_COLUMNS, (round(v, 4) for v in evening_raw(
                hours, root["root_mean_minus_high_c"], block["evening"]["fleet_peak_local_h"])))),
        }} if "evening" in block else {}),
        "station_clock_offset_h": OL.station_clock_offset(artifact, station_id),
        "clock_hours": round(hours, 4),
        "running_high_c": float(running_high_c),
        "running_high_bucket": str(running_high_bucket),
        # What the board shows as 「再升」: here the updated belief that the day
        # settles above the running high's bucket (before the zero floor), not
        # g's observation-only rate -- this update has no separate rate.
        "rise_probability": sum(
            value for bucket, value in weighted.items() if bases[bucket][1] > 0.0
        ) / total,
        "rise_probability_basis": "posterior_before_floor",
        "features": {
            name: (None if value is None else round(float(value), 4))
            for name, value in sorted(features.items())
        },
        "fleet_mean_features": sorted(
            name for name in FEATURES if features.get(name) is None
        ),
        "root_features": {name: round(value, 6) for name, value in root.items()},
        "rung_by_bucket": {bucket: base[0] for bucket, base in sorted(bases.items())},
        "log_update_by_bucket": {
            bucket: round(value, 6) for bucket, value in sorted(f.items())
            if bases[bucket][0] >= 0
        },
        "likelihood_by_bucket": {
            bucket: round(value, 6) for bucket, value in sorted(likelihoods.items())
            if bases[bucket][0] >= 0
        },
    }
    return likelihoods, receipt


__all__ = [
    "BLOCK_KEY",
    "CANDIDATE_BLOCK_KEY",
    "CLOCK_GATE_SHAPE",
    "EVENING_COLUMNS",
    "EVENING_SCHEMA",
    "FEATURES",
    "GATE_SERVE",
    "ROOT_FEATURES",
    "RULE",
    "SCHEMA",
    "STATE_COLUMNS",
    "UPDATE",
    "bucket_bases",
    "compile_direct_update",
    "evening_raw",
    "log_update",
    "root_features",
    "serving_block",
    "state_vector",
    "x_layout",
]
