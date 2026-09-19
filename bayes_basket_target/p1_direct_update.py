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
"""
from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

from . import p1_observation_likelihood as OL
from .contracts import ContractError

BLOCK_KEY = "direct_update"
SCHEMA = "p1_direct_log_update_v1"
RULE = "p1_root_x_mask_x_exp_direct_log_update_v1"
UPDATE = "DIRECT_LOG_UPDATE_V1"
GATE_SERVE = "SERVE"
FEATURES: tuple[str, ...] = tuple(
    name for name in OL.FULL_FEATURES if name != "remaining_hours"
)
ROOT_FEATURES: tuple[str, ...] = ("root_rise_mass", "root_mean_minus_high_c")
STATE_COLUMNS: tuple[str, ...] = FEATURES + ROOT_FEATURES
KNOTS_HOURS: tuple[float, ...] = tuple(float(k) for k in OL.ALPHA_KNOTS_HOURS)
LATE_BELOW_HOURS = 6.0
MID_BELOW_HOURS = 10.0
MAX_RUNG = 12


def x_layout() -> list[str]:
    """The names of x's columns, in order -- written into the block."""

    return (
        ["intercept"]
        + [f"clock_hat_{knot:g}h" for knot in KNOTS_HOURS]
        + list(STATE_COLUMNS)
        + [f"late_x_{name}" for name in STATE_COLUMNS]
        + [f"mid_x_{name}" for name in STATE_COLUMNS]
    )


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
    late = 1.0 if clock_hours < LATE_BELOW_HOURS else 0.0
    mid = 1.0 if LATE_BELOW_HOURS <= clock_hours < MID_BELOW_HOURS else 0.0
    return (
        [1.0] + hats + standard
        + [late * value for value in standard]
        + [mid * value for value in standard]
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
    if block.get("schema") != SCHEMA:
        raise ContractError("p1_direct_update_schema_invalid")
    width = len(x_layout())
    if (
        list(block.get("x_layout") or []) != x_layout()
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
    return block


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
        "rule": RULE,
        "update": UPDATE,
        "state": "APPLIED",
        "artifact_sha256": str(artifact.get("content_sha256") or ""),
        "trained_through": str(block.get("trained_through") or ""),
        "lambda": block.get("lambda"),
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
    "FEATURES",
    "GATE_SERVE",
    "ROOT_FEATURES",
    "RULE",
    "SCHEMA",
    "STATE_COLUMNS",
    "UPDATE",
    "bucket_bases",
    "compile_direct_update",
    "log_update",
    "root_features",
    "serving_block",
    "state_vector",
    "x_layout",
]
