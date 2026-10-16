"""Charter 273: the spread-adjusted linear pool (SLP) replaces the P0 width table layer.

A linear pool of individually calibrated systems is wider than any of them:
the between-system spread adds to each one's own.  The table layer took that
width back after pooling with one scalar (lambda) and then needed a tail mix,
a far-peak ceiling and a mode-to-shoulder share to undo what the scalar did to
the mode and the tails.  SLP takes it back before pooling instead: each
seated system's spread factor is multiplied by ``c`` for the decision's
horizon, the narrowed systems are pooled, bridged and radiation-shifted as
before, and no table layer follows (the model-zero floor still does).

Measured on a strict as-of replay 08-12..10-14
(``docs/research/p0_slp_strict_replay_20261016``): the walk-forward ``c``
settled at 0.8 the day before (54 evaluated days, LogScore -0.0122
significant against the tables as they stood, tie with today's tables) and at
1.0 two days out (20 days, crosses zero); on the served-comparison rebuild
(``p0_spread_adjusted_pool_20261016``) the decision-day optimum was 0.6, with
no gain over the current chain.  User ruling 2026-10-16 「上slp」「全部提前量都上」
puts it on every horizon at those values.

A fixed rule like the far-peak ceiling: the holdout scores it, the
certificate carries the tag, serving applies it exactly where the tag is.
"""

from __future__ import annotations

import math
import os

from .center_width_calibration import mode_shoulder_bin_for

SLP_METHOD_TAG = "spread_adjusted_pool_v1"
# Same horizon bins as charter 271 (hours to settlement: <24, 24-48, >=48).
SLP_SHRINK_BY_BIN = {
    "h0_24": 0.6,
    "h24_48": 0.8,
    "h48_plus": 1.0,
}
# A decision whose horizon is unknown (no settlement clock) is not narrowed.
SLP_SHRINK_UNKNOWN_HORIZON = 1.0
# The clip the replay applied to the narrowed factor.
SLP_SPREAD_FACTOR_BOUNDS = (0.25, 5.0)
SLP_MODE = os.environ.get("PM_P0_SLP", "").strip().lower() or "serve"
if SLP_MODE not in {"serve", "off"}:
    raise RuntimeError(f"unknown P0 SLP mode: {SLP_MODE}")


def slp_serves() -> bool:
    return SLP_MODE == "serve"


def slp_method_tag_suffix() -> str:
    """What a certificate appends for the holdout it scored: the tag while
    the rule serves, nothing while it is off."""

    return "::" + SLP_METHOD_TAG if slp_serves() else ""


def slp_bin_for(hours_to_settlement: float | None) -> str | None:
    return mode_shoulder_bin_for(hours_to_settlement)


def slp_shrink_for(hours_to_settlement: float | None) -> float:
    name = slp_bin_for(hours_to_settlement)
    if name is None:
        return SLP_SHRINK_UNKNOWN_HORIZON
    return float(SLP_SHRINK_BY_BIN[name])


def slp_spread_factor(spread_factor: float, shrink: float) -> float:
    """The narrowed factor a seated system is dressed with."""

    if float(shrink) == 1.0:
        return float(spread_factor)
    value = float(spread_factor) * float(shrink)
    if not math.isfinite(value):
        raise ValueError("slp_spread_factor_not_finite")
    low, high = SLP_SPREAD_FACTOR_BOUNDS
    return min(high, max(low, value))
