"""Physics core of the same-day Tmax model (ARCHITECTURE.md section A),
first form: a slab mixed layer growing into the morning stable layer
("encroachment").  With a constant sensible-heat share f of the incoming
shortwave and a morning potential-temperature lapse rate g, the layer's
temperature after heat Q (J/m^2) has entered is

    T(Q) = T_morning + sqrt(2 * g * f * Q / (rho * cp)),

so the day's rise is a + c * sqrt(Q) with c = sqrt(2 g f / (rho cp)) per
station (a absorbs advection, the morning-sounding offset and the
06:00-reading's bias).  The same-day increment is the core run twice, the
model's own absolute errors cancelling:

    dT(H) = c * ( sqrt(Q_obs[06..H] + Q_fc[H+1..PEAK]) - sqrt(Q_fc[06..PEAK]) ).

A watt added at 08:00 and at 13:00 count the same here (heat stays in the
layer), and each counts less the sunnier the day already is (the square
root): neither holds in the board's straight line.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

HOURS = list(range(6, 18))          # stamps 06..17 local, hour ending
PEAK_HOUR = 15                      # heat counted up to the hour ending 15:00
MJ_PER_WM2_HOUR = 0.0036
RHO_CP = 1.2 * 1005.0               # J m^-3 K^-1


def heat_mj(sw: Sequence[float], last_hour: int = PEAK_HOUR) -> float:
    """MJ/m^2 of shortwave over stamps 06..last_hour."""

    return MJ_PER_WM2_HOUR * sum(v for h, v in zip(HOURS, sw) if h <= last_hour)


def blended_heat(sw_obs: Sequence[float], sw_fc: Sequence[float], measured_to: int) -> float:
    """Measured up to ``measured_to``, forecast after it, both to PEAK_HOUR."""

    return MJ_PER_WM2_HOUR * sum((o if h <= measured_to else f) for h, o, f in zip(HOURS, sw_obs, sw_fc)
                                 if h <= PEAK_HOUR)


def delta_t(c: float, sw_obs: Sequence[float], sw_fc: Sequence[float], measured_to: int) -> float:
    q_fc = heat_mj(sw_fc)
    q_mix = blended_heat(sw_obs, sw_fc, measured_to)
    return c * (math.sqrt(max(q_mix, 0.0)) - math.sqrt(max(q_fc, 0.0)))


def implied_g_f(c: float) -> float:
    """g * f (K/m) implied by c (K per sqrt(MJ/m^2)): c^2 * rho cp / 2 / 1e6."""

    return c * c * RHO_CP / 2.0 / 1e6
