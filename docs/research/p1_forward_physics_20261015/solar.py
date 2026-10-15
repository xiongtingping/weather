"""Solar geometry and clear-sky irradiance for the 48 stations (phase 0).

No fitted parameter.  Elevation angle from the NOAA SPA-lite formulas
(declination + equation of time, good to ~0.3 deg), clear-sky global
horizontal irradiance from Haurwitz with a small altitude term.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

SOLAR_CONSTANT = 1361.0


def _day_of_year(t: datetime) -> float:
    start = datetime(t.year, 1, 1, tzinfo=timezone.utc)
    return (t - start).total_seconds() / 86400.0 + 1.0


def elevation_deg(lat: float, lon: float, t_utc: datetime) -> float:
    """Sun elevation above the horizon in degrees (negative at night)."""
    d = _day_of_year(t_utc)
    gamma = 2.0 * math.pi / 365.0 * (d - 1.0 + (t_utc.hour - 12.0) / 24.0)
    eqtime = 229.18 * (
        0.000075 + 0.001868 * math.cos(gamma) - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma) - 0.040849 * math.sin(2 * gamma)
    )
    decl = (
        0.006918 - 0.399912 * math.cos(gamma) + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma) + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma) + 0.00148 * math.sin(3 * gamma)
    )
    minutes = t_utc.hour * 60.0 + t_utc.minute + t_utc.second / 60.0
    tst = minutes + eqtime + 4.0 * lon
    ha = math.radians(tst / 4.0 - 180.0)
    phi = math.radians(lat)
    cos_z = math.sin(phi) * math.sin(decl) + math.cos(phi) * math.cos(decl) * math.cos(ha)
    cos_z = max(-1.0, min(1.0, cos_z))
    return 90.0 - math.degrees(math.acos(cos_z))


def clear_sky_ghi(lat: float, lon: float, elev_m: float, t_utc: datetime) -> float:
    """Haurwitz clear-sky GHI (W/m2) with a mild altitude correction."""
    h = elevation_deg(lat, lon, t_utc)
    if h <= 0.0:
        return 0.0
    s = math.sin(math.radians(h))
    base = 1098.0 * s * math.exp(-0.059 / s)
    return base * (1.0 + 0.1 * max(0.0, elev_m) / 8000.0)


def hourly_s0_mean(lat: float, lon: float, elev_m: float, hour_start_utc: datetime, steps: int = 6) -> float:
    """Mean clear-sky GHI over [hour_start, hour_start + 1h)."""
    vals = []
    for i in range(steps):
        t = hour_start_utc + timedelta(minutes=(i + 0.5) * 60.0 / steps)
        vals.append(clear_sky_ghi(lat, lon, elev_m, t))
    return sum(vals) / len(vals)


def integral_mj(lat: float, lon: float, elev_m: float, t0_utc: datetime, t1_utc: datetime, step_min: float = 10.0) -> float:
    """∫ S0 dt from t0 to t1 in MJ/m2 (0 when t1 <= t0)."""
    if t1_utc <= t0_utc:
        return 0.0
    total = 0.0
    t = t0_utc
    dt = timedelta(minutes=step_min)
    while t < t1_utc:
        t_next = min(t + dt, t1_utc)
        mid = t + (t_next - t) / 2
        total += clear_sky_ghi(lat, lon, elev_m, mid) * (t_next - t).total_seconds()
        t = t_next
    return total / 1e6
