"""The two fitted table layers of P0, in the one order every face applies.

P0's probability passes two tables fitted on the stage's own settled record
before it is served: the centre/width scalars move and scale the distribution
on the temperature axis, and the band table rescales each bucket by how far it
sits from the peak.  The band factors are fitted on the claim the served
distribution makes, so geometry comes first and the claim correction last.

Three places need that composition and until 2026-09-13 each had its own
copy.  Serving (``native_ensemble_p0.compile_native_ensemble_p0``) ran
centre/width then band.  The OOS gate that certifies an artifact
(``native_ensemble_oos.build_native_ensemble_oos``) ran band then centre/width,
because f81cd0d1 appended the width pin after the band call when it first
pinned the scalars into the gated math.  The subset contest that picks the
seated systems (``_select_minimum_crps_subset``) ran band and no centre/width
at all.  Same table shas, three arithmetics: with the live P0 band table
(``4f522947``) and lambda 0.75, a seven-bucket vector's centre bucket reads
0.35951 in the gate and 0.35378 served, and on 2026-09-13 44 of 45 P0 rows
carried both layers non-identity.

So the order lives here and nowhere else.  ``SERVED_TABLE_LAYER_ORDER`` is the
contract; ``apply_served_table_layers`` is the only function that applies the
two tables in sequence.

What is deliberately not in here:

* the fleet regime shift (``regime_delta_c``) is a serving-only input.  It
  reads how far the fleet settled from yesterday's served distribution, which
  does not exist when an artifact is gated, so the gate passes zero and
  serving passes the day's value through the same layer;
* the upstream position and lean layers (radiation shift, neighbour direction
  tilt, the withdrawn conditional tail skew).  They run on each face's own
  bucket grid -- integer degree labels in the holdout, market intervals in
  serving -- and keep their own implementations; only the neighbour tilt's
  anchor is shared, through ``model_center_c``.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .center_width_calibration import apply_center_width_calibration
from .cross_consensus import (
    BAND_RELIABILITY_AXIS_CENTRE_DISTANCE,
    apply_band_reliability,
)

SERVED_TABLE_LAYER_ORDER = ("center_width", "band_reliability")


@dataclass(frozen=True)
class CenterWidthLayer:
    """The resolved centre/width inputs, exactly as the transform takes them."""

    delta_c: float = 0.0
    width_lambda: float = 1.0
    station_width_scale: float = 1.0
    regime_delta_c: float = 0.0
    sigma_floor_rungs: float = 0.0
    width_tier_lambdas: Sequence[float] | None = None
    width_tier_boundaries: Sequence[float] | None = None


@dataclass(frozen=True)
class BandLayer:
    """A band table and the axis it was fitted on.

    ``factors`` is required and may be empty (a pin to a table the lane
    vetoed loads as the empty table -- identity, with its sha kept).  It may
    NOT be None: that used to fall through to ``apply_band_reliability``'s
    pre-store bootstrap constants, and on 2026-09-15 that is how a vetoed P0
    kept rescaling for six hours -- hand-written 2026-08 factors on the claim
    axis, while the lane had been on centre_distance since 09-07.  No table
    means no layer, so callers pass None for the LAYER, never for its
    factors.
    """

    factors: Sequence[tuple[tuple[float, float], float]]
    axis: str

    def __post_init__(self) -> None:
        if self.factors is None:
            raise ValueError("band_layer_factors_required")


def model_center_c(
    system_weights: Mapping[str, float],
    system_centers_c: Mapping[str, float],
    system_ids: Sequence[str] | None = None,
) -> float:
    """The seated blend's temperature: calibrated centres at the seat weights.

    Serving anchors the neighbour-direction tilt on the bucket holding this
    number, not on the mean of the shifted histogram -- the open edge bucket
    accumulates whole tails and would drag a mean-based anchor off the models
    (the ZUUU case in ``compile_native_ensemble_p0``).  The holdout reads the
    same number so the tilt it scores leans around the same bucket.

    ``system_ids`` fixes the summation order; serving passes its own so the
    float it records is the one it always recorded.
    """

    return sum(
        system_weights[system] * system_centers_c[system]
        for system in (system_weights if system_ids is None else system_ids)
    )


def apply_served_table_layers(
    probability: Mapping[str, float],
    *,
    ordered_keys: Sequence[str],
    key_centers_c: Sequence[float],
    center_width: CenterWidthLayer | None,
    band: BandLayer | None,
    excluded_buckets: Sequence[str] = (),
    skip: str | None = None,
) -> tuple[dict[str, float], dict[str, Any] | None, dict[str, Any] | None]:
    """Centre/width, then band.  Returns the vector and both receipts.

    ``None`` for a layer means the face does not apply it (no pin, no tag);
    its receipt comes back ``None``.  ``skip`` names one layer to leave out,
    for the gate's layer ablation, which must score the same arithmetic
    minus one layer rather than a re-derivation of it.
    """

    if skip is not None and skip not in SERVED_TABLE_LAYER_ORDER:
        raise ValueError(f"served_shape_unknown_layer:{skip}")
    shaped = {str(key): float(value) for key, value in probability.items()}
    center_width_receipt = None
    if center_width is not None and skip != "center_width":
        shaped, center_width_receipt = apply_center_width_calibration(
            shaped,
            delta_c=center_width.delta_c,
            width_lambda=center_width.width_lambda,
            station_width_scale=center_width.station_width_scale,
            regime_delta_c=center_width.regime_delta_c,
            sigma_floor_rungs=center_width.sigma_floor_rungs,
            width_tier_lambdas=center_width.width_tier_lambdas,
            width_tier_boundaries=center_width.width_tier_boundaries,
            ordered_keys=ordered_keys,
            key_centers_c=key_centers_c,
            excluded_buckets=excluded_buckets,
        )
    band_receipt = None
    if band is not None and skip != "band_reliability":
        shaped, band_receipt = apply_band_reliability(
            shaped,
            factors=band.factors,
            ordinals=(
                {str(key): index for index, key in enumerate(ordered_keys)}
                if band.axis == BAND_RELIABILITY_AXIS_CENTRE_DISTANCE
                else None
            ),
        )
    return shaped, center_width_receipt, band_receipt
