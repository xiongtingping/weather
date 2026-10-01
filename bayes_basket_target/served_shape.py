"""P0's fitted table layer, in the one order every face applies.

P0's probability passes one table fitted on the stage's own settled record
before it is served: the centre/width scalars move and scale the distribution
on the temperature axis.

Until 2026-09-26 a second table followed it -- the band table, rescaling each
bucket by how far it sat from the peak -- and three places needed that
composition.  Until 2026-09-13 each had its own copy: serving
(``native_ensemble_p0.compile_native_ensemble_p0``) ran centre/width then
band, the OOS gate that certifies an artifact
(``native_ensemble_oos.build_native_ensemble_oos``) ran band then
centre/width, and the subset contest ran band and no centre/width at all --
same table shas, three arithmetics.  So the order came to live here and
nowhere else, and it still does: ``SERVED_TABLE_LAYER_ORDER`` is the contract
and ``apply_served_table_layers`` the only function that applies it.

Charter 232 retired the band layer (user ruling 2026-09-26, 「退役 band 层」):
measured on the market ladder over 43 test days it had no position of its own
above the width layer, and switched on it hurt the short leads (h00_12 Brier
and LogScore both significantly worse), because it was fitted on each
station-day's first decision and applied to every decision after it.

What is deliberately not in here:

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

import dataclasses

from .center_width_calibration import (
    apply_center_width_calibration,
    width_slice_for,
)

SERVED_TABLE_LAYER_ORDER = ("center_width",)


@dataclass(frozen=True)
class CenterWidthLayer:
    """The resolved centre/width inputs, exactly as the transform takes them."""

    delta_c: float = 0.0
    width_lambda: float = 1.0
    station_width_scale: float = 1.0
    sigma_floor_rungs: float = 0.0
    width_tier_lambdas: Sequence[float] | None = None
    width_tier_boundaries: Sequence[float] | None = None
    # Charter 239: the slice this one decision is in, resolved by the caller
    # from the decision's seated count and hours to settlement
    # (``with_width_slice``).  When set, ``width_slice_lambda`` replaces
    # ``width_lambda`` for this decision and the tiers do not apply; the
    # station scale and the floor compose as they always did.
    width_slice: str | None = None
    width_slice_lambda: float | None = None


def with_width_slice(
    layer: CenterWidthLayer | None,
    slices: Mapping[str, Mapping[str, Any]] | None,
    *,
    seated_system_count: int | None,
    hours_to_settlement: float | None,
) -> CenterWidthLayer | None:
    """The layer with the decision's slice resolved onto it (charter 239).

    One resolver for serving, the gate and the fit's reserve, so the three
    read the same lambda for the same decision.  A layer of ``None`` stays
    ``None``: the slices ride on a table row, and a face that applies no row
    applies no slice.
    """

    if layer is None or not slices:
        return layer
    resolved = width_slice_for(
        slices,
        seated_system_count=seated_system_count,
        hours_to_settlement=hours_to_settlement,
    )
    if resolved is None:
        if layer.width_slice is None:
            return layer
        return dataclasses.replace(
            layer, width_slice=None, width_slice_lambda=None
        )
    return dataclasses.replace(
        layer, width_slice=resolved[0], width_slice_lambda=float(resolved[1])
    )


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
    excluded_buckets: Sequence[str] = (),
    skip: str | None = None,
    open_edge_bounds_c: Sequence[float | None] | None = None,
) -> tuple[dict[str, float], dict[str, Any] | None]:
    """Centre/width.  Returns the vector and the layer's receipt.

    ``None`` for the layer means the face does not apply it (no pin, no
    tag); its receipt comes back ``None``.  ``skip`` names a layer to leave
    out, for the gate's layer ablation, which must score the same arithmetic
    minus one layer rather than a re-derivation of it.

    ``open_edge_bounds_c`` (charter 190) is the axis's open ends, as
    ``center_width_calibration.served_bucket_axis`` returns them with the
    centres: serving's market ladder declares them, the holdout's integer
    grid has none and passes nothing.
    """

    if skip is not None and skip not in SERVED_TABLE_LAYER_ORDER:
        raise ValueError(f"served_shape_unknown_layer:{skip}")
    shaped = {str(key): float(value) for key, value in probability.items()}
    center_width_receipt = None
    if center_width is not None and skip != "center_width":
        sliced = center_width.width_slice_lambda is not None
        shaped, center_width_receipt = apply_center_width_calibration(
            shaped,
            delta_c=center_width.delta_c,
            # Charter 239: a resolved slice replaces the stage lambda for
            # this decision, and the tiers (which read the served sigma)
            # stand aside -- two selectors of one lambda would be a
            # composition nobody fitted.
            width_lambda=(
                float(center_width.width_slice_lambda)
                if sliced else center_width.width_lambda
            ),
            station_width_scale=center_width.station_width_scale,
            sigma_floor_rungs=center_width.sigma_floor_rungs,
            width_tier_lambdas=(
                None if sliced else center_width.width_tier_lambdas
            ),
            width_tier_boundaries=center_width.width_tier_boundaries,
            ordered_keys=ordered_keys,
            key_centers_c=key_centers_c,
            excluded_buckets=excluded_buckets,
            open_edge_bounds_c=open_edge_bounds_c,
        )
        # Always written, so a receipt answers "which slice?" the same way
        # whether or not one applied; ``stage_width_lambda`` is the pooled
        # lambda the slice stood in for (``pooled_width_lambda`` inside the
        # kernel's receipt is the lambda the kernel was handed).
        center_width_receipt["width_slice"] = center_width.width_slice
        center_width_receipt["width_slice_lambda"] = (
            float(center_width.width_slice_lambda) if sliced else None
        )
        center_width_receipt["stage_width_lambda"] = float(
            center_width.width_lambda
        )
    return shaped, center_width_receipt
