"""Consensus bridge for cross-system probability mixtures.

A weighted mixture of two calibrated systems answers "which system's world is
real" -- it puts mass at each system's center and almost none between them.
Measured against settled truth over 133 station-days, that reading is wrong
for this network: when the calibrated centers disagree by >=2C, truth landed
strictly between them 34.5% of the time while the mixture predicted ~19%, and
when the centers agree the mixture under-states them (truth hit the center
buckets 53-55% against a predicted 26-31%).  The disagreement behaves mostly
like noise around one scenario, not like two scenarios.

The bridge blends the mixture with a consensus component: the same per-system
distributions recentered onto the weighted mean, which preserves each system's
calibrated spread while removing the center disagreement.  The blend weight
rises with the center gap and is zero where the mixture is already calibrated,
so narrow-gap packets are untouched by construction.

Fit/serve consistency: the artifact's ``combination_method`` records whether
its OOS evaluation ran under the bridge.  P0 applies the bridge only when the
artifact it serves was evaluated with it, so the live math is always the math
the gate actually measured; the rollout follows artifact regeneration.
"""

from __future__ import annotations

import math
import os

from datetime import date
from typing import Any, Mapping, Sequence

CONSENSUS_BRIDGE_METHOD_TAG = "consensus_bridge_v1"

# The conditional tail skew (``conditional_tail_skew_v1``) was deleted on
# 2026-09-22 (charter 43 amendment).  Its tag left new artifacts on
# 2026-08-18 (d63426a7) and the last tagged artifact's window closed
# 2026-09-17T09:11:35Z, so serving could no longer reach it; its
# measurements are kept in charter 43 as history.

SYNOPTIC_COOLING_METHOD_TAG = "synoptic_cooling_shift_v1"
# Cloud, rain and wind are the physics the models get wrong in a direction we
# can read.  Measured 2026-08-14 against the standardised residual (truth
# minus calibrated centre, in ensemble sigmas), using the fields the airport
# forecaster publishes with the TX -- available at decision time, unlike the
# reanalysis:
#
#   cloud cover           corr -0.094   increment over the TX gap -0.072
#   precipitation prob.   corr -0.128   increment                 -0.111
#   wind speed            corr -0.137   increment                 -0.142
#   equal-weight index    corr -0.170   increment                 -0.154
#
# Every sign is the physical one: more cloud, rain or mixing and the day runs
# colder than the models say.  Wind carries the most increment, which is the
# interesting part -- the mixing depth is what decides how far the surface can
# run, and it is the least emphasised quantity in a TX forecast.
#
# The index is near-orthogonal to the TX gap itself (increment barely below
# the raw correlation), so it is a second reading of the day rather than the
# same one.  Shifting the centre by 0.15 sigma per index unit gains 0.46%
# CRPS over 1,791 station-days, a smooth interior optimum.
#
# The ceiling for this family is higher than what these fields reach.  With
# perfect foresight of the day's actual shortwave radiation the same shift
# gains 1.35% and correlates +0.293, near-orthogonal to the TX gap (+0.069
# between them).  That is the argument for capturing the models' own radiation
# and wind forecasts, which this lane does not extract today.
#
# 2026-09-23 (user ruling 「TAF 的云、雨、风也应该像 P0 一样，不应该作为 P0 的替补，
# metar 也是同理」): the forecaster's-fields index and its 0.15-sigma-per-unit
# coefficient are deleted.  A source's physics is evidence at that source's
# own stage -- the TAF's cloud and precipitation in P0-M (charter 158), the
# METAR's in P1 -- never a stand-in inside P0.  The measurements above stay as
# the history of why the radiation shift exists.  The stand-in had served no
# P0 vector since 2026-08-20 (118,243 rows).
# The models' own forecast of the day's shortwave radiation is the stronger
# reading of the same physics, and it subsumes the forecaster's fields.
# Measured 2026-08-14 over 5,230 station-days: forecast radiation correlates
# +0.189 with the standardised residual against +0.191 for the reanalysis
# actual -- the forecast loses essentially nothing, because the models get
# the radiation right and mis-translate it to screen temperature.  Shifting
# by 0.15 sigma per standardised unit gains 2.07% CRPS and 12.7% NLL (peak
# 2.09% at 0.20, a flat plateau; 0.15 is the shrunk choice).  Adding the TAF
# cooling index on top moves it to 2.10% -- nothing -- so radiation leads and
# the forecaster's fields are the fallback for a day the radiation capture
# missed.  Radiation also covers every station-day, where a TX exists for
# seventeen stations only.
RADIATION_SHIFT_METHOD_TAG = "forecast_radiation_shift_v1"
# Sigmas of centre shift per standardised unit of forecast radiation.  This
# is pinned into the artifact's combination method, not read as a bare
# constant: a served distribution has to be shifted by the coefficient its own
# holdout was scored under, and an artifact built before the pin existed
# carries none and is served the legacy value below.
# Per lead bucket, because the coefficient's value is not a property of the
# fleet.  A sweep of ten coefficients over a frozen data window found a clean
# single peak at 0.35 -- four consecutive falls to it and five consecutive
# rises after -- but pooled it is worth only +0.008 bits and 63% of scopes get
# *worse*.  Split by lead the gain is not spread thin, it is concentrated:
# h12_24 wins 75% of its scopes at +0.088, while h00_12 and h24_48 are both
# net negative and were being pushed off centre for nothing.
#
# The mechanism fits the shape.  At h12_24 the run is issued the night before
# and the target day's heating is entirely forecast, so radiation is the main
# lever and the coupling error it corrects has full expression.  At h00_12 the
# day is already under way and the models have seen where it is going; at
# h24_48 the radiation forecast is itself losing skill.  Both ends decay.
#
# Held deliberately thin: h12_24 rests on 20 scopes across 7 stations and 6
# days, and removing the single strongest station takes it from p=0.02 to
# p=0.11.  Wired at the user's decision with that stated.  The review test is
# significance that survives dropping any one station.
#
# Charter 160 (2026-09-19, user ruling 「上线」): retired.  Replayed on every
# decision served at 0.35 (9,315 decisions, 765 station-days), 0.15 scores
# RPS -0.032 [-0.051, -0.013] -- about 5% better -- and 0.35 multiplied the
# seasonal part of the old standardisation by 2.3.  Every lead is fitted and
# served at the fleet value again (``radiation_shift_sigmas_for_lead``).
#
# The fleet value.  Raising it to 0.35 was tried and withdrawn.  A probe over
# served envelopes said +0.142 bits, and the gate's own holdout said +0.005
# with an interval spanning zero over 354 paired days.  The probe was wrong
# twice, both mine: it standardised radiation within its own sample rather
# than against the station history serving uses, and it located the settled
# bucket by nearest centre rather than by interval, which differs exactly on
# the edge buckets a large shift pushes hardest into.  The 1.5C shift clamp
# binds at 0.35 too, so part of that optimum was the clamp and not the
# coefficient.
#
# Charter 173 (2026-09-21, user ruling 「批准」): 0.15 -> 0.07.  0.15 was chosen
# on radiation read from a table rewritten until each day's own evening, at a
# width the holdout under-stated by 1.77x (charter 172).  Refitted on the
# radiation each P0 was actually served, in the clearness form, at the served
# width -- 1,533 station-days, 37 event days, 45 stations, day-block
# bootstrap -- the coefficient is 0.073 [0.016, 0.121]; by mean squared
# residual 0.15 scores 1.9077, worse than no shift at all (1.9055), and
# 0.06-0.09 score lowest (1.887).  Charter 173 let every artifact already out
# keep serving its own pin until the rotation replaced it.
#
# Charter 210 (2026-09-23, user ruling 「改P0辐射系数」): every artifact is
# served at the fleet value, whatever it pins.  The rotation had moved 98% of
# served P0 to 0.07 by 09-23, but certificate renewal (「不过期」, same day)
# copies an artifact row whole, so a scope that is renewed rather than
# re-minted would have kept 0.15 for good.  Refitted through 09-23 on the same
# as-of protocol (1,644 station-days, 40 days, 45 stations) the coefficient
# is 0.068 [0.015, 0.118] and 0.07 has the lowest mean squared residual of
# 0 / 0.03 / 0.05 / 0.07 / 0.09 / 0.11 / 0.15; refitting it day by day scores
# worse than holding 0.07 (+0.015 [+0.004, +0.030]).
#
# Charter 222 (2026-09-25, user ruling 「把辐射系数拆成升温、降温两侧」): one
# coefficient per side.  A day forecast clearer than the station's own last
# 180 days (z > 0) is pushed warm at ``RADIATION_SHIFT_SIGMA_WARM``, a cloudier
# one (z < 0) cold at ``RADIATION_SHIFT_SIGMA_COLD``.  The clearness index has
# a ceiling -- a sky cannot be clearer than clear -- and a long overcast tail
# (z skew -1.05, most negative -9.0 against most positive +2.6), so the
# symmetric 0.07 served its largest shifts on the cold side, and that is the
# side the residual does not follow.  Fitted on charter 210's objective (the
# pre-shift residual de-meaned per station against z times the spread, day
# blocks) over a public replica of the 45 stations that reproduces charter
# 210's symmetric fit (0.072 against 0.068): warm 0.124 [0.099, 0.148], cold
# 0.045 [0.024, 0.069], 20,035 station-days, 2025-06..2026-09.
#
# Centred (``_sided_shift``).  z averages zero over the history it is scored
# against, but a pair bends at zero, so uncentred it would average (warm -
# cold) x mean(z+) -- +0.033 sigma, z > 0 on 59% of days -- and move every
# station's mean warm: bias, which charter 160 keeps out of this layer.
# Scored as served (no intercept, the station bias already in the root)
# against the symmetric 0.07: centred +0.0006 [+0.0003, +0.0010] nat bucket
# log score, Brier +0.0001 [-0.0000, +0.0002]; uncentred -0.0001 [-0.0007,
# +0.0005].  Out of sample by halves it is never worse than 0.07 on either
# proxy, where the larger warm sides a fixed-effects fit prefers (0.20-0.25,
# cold 0) lose Brier on one half of each.
# ``docs/research/radiation_term_physical_line_20260925``.
RADIATION_SHIFT_SIGMA_WARM = 0.12
RADIATION_SHIFT_SIGMA_COLD = 0.04
RADIATION_TWO_SIDED_METHOD_TAG = "radiation_two_sided_centred_v1"
# What artifacts built before the coefficient was pinned were scored with.
# Kept separate because the point of the pin is that the two are free to
# differ -- as they now do.
LEGACY_RADIATION_SHIFT_SIGMA = 0.15
RADIATION_MIN_HISTORY_SAMPLES = 30
RADIATION_SIDE_WARM = "WARM"
RADIATION_SIDE_COLD = "COLD"
RADIATION_SIDE_NEUTRAL = "NEUTRAL"


def radiation_shift_sigmas_for_lead(lead_bucket: str | None) -> tuple[float, float]:
    """``(warm, cold)``: the coefficients this lead is fitted and served at.

    Every lead is at the fleet pair (charter 160 retired the per-lead
    values); a function of the lead so the value the holdout scores, the
    value the artifact pins and the value serving reads back stay one call.
    """

    del lead_bucket
    return RADIATION_SHIFT_SIGMA_WARM, RADIATION_SHIFT_SIGMA_COLD


def pinned_radiation_shift_sigmas(combination_method: str) -> tuple[float, float]:
    """``(warm, cold)``: the coefficients an artifact says its holdout was scored under.

    Charter 222 pins both sides; an artifact minted before it pins one
    coefficient, which was both sides; an artifact built before the pin
    existed names none and reads the legacy value on both.  A pin that does
    not parse reads as absent.
    """

    warm = cold = single = None
    for part in str(combination_method).split("::"):
        key, _, value = part.partition("=")
        if key not in (
            "radiation_shift_sigma_warm", "radiation_shift_sigma_cold",
            "radiation_shift_sigma",
        ):
            continue
        try:
            number = float(value)
        except ValueError:
            continue
        if key == "radiation_shift_sigma_warm":
            warm = number
        elif key == "radiation_shift_sigma_cold":
            cold = number
        else:
            single = number
    if warm is not None and cold is not None:
        return warm, cold
    if single is not None:
        return single, single
    return LEGACY_RADIATION_SHIFT_SIGMA, LEGACY_RADIATION_SHIFT_SIGMA


def radiation_side(standardised: float) -> str:
    """Which side of the station's own history the day's radiation sits on."""

    if standardised > 0.0:
        return RADIATION_SIDE_WARM
    if standardised < 0.0:
        return RADIATION_SIDE_COLD
    return RADIATION_SIDE_NEUTRAL


def _sided_shift(
    standardised: float,
    shift_sigma_per_unit: float,
    cold_shift_sigma_per_unit: float | None,
    history_standardised: "Sequence[float]",
) -> dict[str, Any]:
    """The shift and the receipt fields that say which side paid for it.

    ``cold_shift_sigma_per_unit`` None is one coefficient on both sides --
    every caller that standardises at a unit coefficient, and every pin
    written before charter 222.  Two different sides are centred on the
    station's own history (``history_standardised``: each history day
    standardised as the day itself is), so the term moves days relative to
    the station's usual one and never the station's mean.  One coefficient
    needs no centre: the history's z average zero by construction, and it is
    left at exactly 0.0 so a symmetric shift stays bit-identical.
    """

    warm = float(shift_sigma_per_unit)
    cold = warm if cold_shift_sigma_per_unit is None else float(cold_shift_sigma_per_unit)
    coefficient = cold if standardised < 0.0 else warm
    centre = 0.0
    if cold != warm and history_standardised:
        centre = sum(
            (cold if z < 0.0 else warm) * z for z in history_standardised
        ) / len(history_standardised)
    return {
        "shift_sigma": coefficient * standardised - centre,
        "shift_sigma_per_unit": coefficient,
        "shift_sigma_per_unit_warm": warm,
        "shift_sigma_per_unit_cold": cold,
        "radiation_side": radiation_side(standardised),
        "side_centre_sigma": centre,
    }


def forecast_radiation_shift_sigma(
    *,
    radiation_sum: float | None,
    station_history: "Sequence[float]",
    shift_sigma_per_unit: float,
    cold_shift_sigma_per_unit: float | None = None,
) -> tuple[float | None, dict[str, Any]]:
    """Standardise the day's forecast radiation against the station's own.

    Radiation is a site quantity -- latitude, season and climate set its
    scale -- so it is standardised per station against that station's own
    captured history rather than against a fleet constant.  Returns the shift
    in ensemble sigmas (positive means warmer than the models say), or None
    when the day or the history is missing.  ``shift_sigma_per_unit`` is the
    warm side's coefficient and ``cold_shift_sigma_per_unit`` the cold
    side's (charter 222), centred on the same history; without the latter
    one coefficient serves both.  No default: a coefficient left to a
    default is how a scored value and a served one drift apart.
    """

    if radiation_sum is None:
        return None, {
            "schema": "forecast_radiation_receipt_v1",
            "method_tag": RADIATION_SHIFT_METHOD_TAG,
            "state": "NO_FORECAST_RADIATION",
        }
    values = [float(value) for value in station_history]
    if len(values) < RADIATION_MIN_HISTORY_SAMPLES:
        return None, {
            "schema": "forecast_radiation_receipt_v1",
            "method_tag": RADIATION_SHIFT_METHOD_TAG,
            "state": "HISTORY_TOO_THIN",
            "sample_count": len(values),
        }
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    deviation = math.sqrt(variance)
    if deviation <= 0.0:
        return None, {
            "schema": "forecast_radiation_receipt_v1",
            "method_tag": RADIATION_SHIFT_METHOD_TAG,
            "state": "HISTORY_DEGENERATE",
        }
    standardised = (float(radiation_sum) - mean) / deviation
    sided = _sided_shift(
        standardised, shift_sigma_per_unit, cold_shift_sigma_per_unit,
        [(value - mean) / deviation for value in values],
    )
    return sided["shift_sigma"], {
        "schema": "forecast_radiation_receipt_v1",
        "method_tag": RADIATION_SHIFT_METHOD_TAG,
        "state": "RESOLVED",
        "radiation_sum": float(radiation_sum),
        "standardised": standardised,
        "sample_count": len(values),
        **sided,
    }


# Charter 160 (2026-09-19): the radiation is standardised as a clearness index
# (Kt), the day's forecast radiation over what the top of the atmosphere
# receives at this latitude on this date, against the station's own Kt history
# before the event day.  The absolute standardisation above compared a
# September day with a history that runs through July, so the sun's own
# seasonal descent read as cloud: 9 of every 10 units of September's negative
# z were astronomy (fleet z -0.44 absolute, -0.05 as Kt), and that part
# carries no information -- regressed on the truth residual it takes +0.04
# [-0.10, +0.16] against +0.21 [+0.13, +0.28] for the de-seasoned part.
# Replayed bit-exactly over 946 first decisions (45 stations, 23 event days):
# RPS -3.8e-3 [-9.7, +1.7] overall, -7.4e-3 [-16.6, +1.8] from 09-07; the one
# arm better than the incumbent on every cut, including the bias-refusal days
# where a trailing 30/45-day window was significantly worse.  Dividing by the
# top of the atmosphere rather than by a clear-sky surface value differs by an
# atmospheric transmission of about 0.75, which the per-station
# standardisation cancels.  The incumbent's z is kept beside it on every
# receipt as the shadow arm (``legacy_v1``) so it can be re-scored.
RADIATION_CLEARNESS_METHOD_TAG = "forecast_radiation_clearness_v2"
SOLAR_CONSTANT_MJ_PER_M2_MIN = 0.0820  # FAO-56 eq. 21


def extraterrestrial_radiation_mj(
    latitude_deg: float | None, local_date: str | None
) -> float | None:
    """Daily top-of-atmosphere shortwave radiation, MJ/m^2 (FAO-56 eq. 21)."""

    try:
        phi = math.radians(float(latitude_deg))
        day = date.fromisoformat(str(local_date)[:10])
    except (TypeError, ValueError):
        return None
    if not math.isfinite(phi):
        return None
    doy = day.timetuple().tm_yday
    dr = 1.0 + 0.033 * math.cos(2.0 * math.pi * doy / 365.0)
    decl = 0.409 * math.sin(2.0 * math.pi * doy / 365.0 - 1.39)
    ws = math.acos(max(-1.0, min(1.0, -math.tan(phi) * math.tan(decl))))
    value = (24.0 * 60.0 / math.pi) * SOLAR_CONSTANT_MJ_PER_M2_MIN * dr * (
        ws * math.sin(phi) * math.sin(decl)
        + math.cos(phi) * math.cos(decl) * math.sin(ws)
    )
    return value if value > 0.0 and math.isfinite(value) else None


def clearness_index(
    radiation_sum: float | None,
    latitude_deg: float | None,
    local_date: str | None,
) -> float | None:
    """Forecast radiation as a fraction of the top of the atmosphere's."""

    if radiation_sum is None:
        return None
    top = extraterrestrial_radiation_mj(latitude_deg, local_date)
    if top is None:
        return None
    value = float(radiation_sum) / top
    return value if math.isfinite(value) else None


# Charter 172 (2026-09-21): an artifact whose holdout read the radiation as it was
# known at each day's own cutoff (the version table, not the row the table
# holds today) and scaled the shift by the blended distribution's own spread,
# the scale serving uses.  Serving arithmetic is unchanged by it; the tag says
# which certificate the artifact carries.
RADIATION_ASOF_METHOD_TAG = "radiation_asof_distribution_sigma_v1"


def distribution_sigma_c(
    probabilities: "Sequence[float]", centers_c: "Sequence[float]"
) -> float:
    """A bucketed distribution's own spread in degrees.

    The one scale the radiation shift is expressed in, on the serving path
    and in the holdout alike.  Until 2026-09-21 the holdout stood in half the
    systems' centre range for it: measured over 153 station-days that was a
    median 0.64C against the served 1.14C, so every certified shift was
    scored at about 0.56 of the size it served at.
    """

    values = [float(weight) for weight in probabilities]
    centers = [float(center) for center in centers_c]
    if len(values) != len(centers):
        raise ValueError("distribution_sigma_c_length_mismatch")
    mean = sum(weight * center for weight, center in zip(values, centers))
    variance = sum(
        weight * (center - mean) ** 2 for weight, center in zip(values, centers)
    )
    return math.sqrt(variance) if variance > 0.0 else 0.0


def served_radiation_shift_sigmas(combination_method: str) -> tuple[float, float]:
    """``(warm, cold)``: the coefficients P0 serves an artifact at -- the fleet pair, always.

    Charter 210 (2026-09-23, user ruling 「改P0辐射系数」).  Until then an
    artifact scored in the clearness form was served its own pin and an older
    one a frozen 0.15 (charters 160 and 173), so a coefficient change reached
    serving only as the OOS rotation re-minted each scope -- and certificate
    renewal copies the pinned method whole, so a renewed scope never would.
    The pins are still read (``pinned_radiation_shift_sigmas``): the receipt
    records them and the absolute-form shadow arm is computed at them.
    Charter 222 split the one coefficient into a warm and a cold side; the
    rule that the pin is recorded, not served, is unchanged.
    """

    del combination_method  # the pin is recorded, not served
    return RADIATION_SHIFT_SIGMA_WARM, RADIATION_SHIFT_SIGMA_COLD


def forecast_clearness_shift_sigma(
    *,
    radiation_sum: float | None,
    event_date: str,
    latitude_deg: float | None,
    station_history: "Sequence[tuple[str, float]]",
    shift_sigma_per_unit: float,
    cold_shift_sigma_per_unit: float | None = None,
) -> tuple[float | None, dict[str, Any]]:
    """Charter 160: the radiation shift on the clearness index.

    ``station_history`` is ``(local_date, radiation_sum)`` rows; only days
    strictly before the event day count (the old reader also took the
    forward-captured rows for the days after it).  Returns the shift in
    ensemble sigmas and a receipt, or ``(None, receipt)`` when the day, its
    latitude or thirty days of history is missing -- the caller decides the
    fallback.  Charter 222: ``shift_sigma_per_unit`` is the warm side's
    coefficient (the day clearer than the station's history) and
    ``cold_shift_sigma_per_unit`` the cold side's, centred on the same
    history; without the latter one coefficient serves both, which is how a
    caller standardises at 1.0.
    """

    base = {
        "schema": "forecast_radiation_receipt_v1",
        "method_tag": RADIATION_CLEARNESS_METHOD_TAG,
    }
    if radiation_sum is None:
        return None, {**base, "state": "NO_FORECAST_RADIATION"}
    top = extraterrestrial_radiation_mj(latitude_deg, event_date)
    if top is None:
        return None, {**base, "state": "NO_LATITUDE"}
    today = float(radiation_sum) / top
    values = []
    for local_date, value in station_history:
        if str(local_date)[:10] >= str(event_date)[:10] or value is None:
            continue
        kt = clearness_index(value, latitude_deg, local_date)
        if kt is not None:
            values.append(kt)
    if len(values) < RADIATION_MIN_HISTORY_SAMPLES:
        return None, {
            **base, "state": "HISTORY_TOO_THIN", "sample_count": len(values),
        }
    mean = sum(values) / len(values)
    deviation = math.sqrt(sum((v - mean) ** 2 for v in values) / len(values))
    if deviation <= 0.0:
        return None, {**base, "state": "HISTORY_DEGENERATE"}
    standardised = (today - mean) / deviation
    sided = _sided_shift(
        standardised, shift_sigma_per_unit, cold_shift_sigma_per_unit,
        [(value - mean) / deviation for value in values],
    )
    return sided["shift_sigma"], {
        **base,
        "state": "RESOLVED",
        "radiation_sum": float(radiation_sum),
        "extraterrestrial_mj": top,
        "clearness_index": today,
        "history_clearness_mean": mean,
        "history_clearness_sd": deviation,
        "standardised": standardised,
        "sample_count": len(values),
        **sided,
    }


NEIGHBOUR_DIRECTION_METHOD_TAG = "neighbour_direction_v1"
# RETIRED 2026-09-18 (charter 147, operator ruling: "去掉并下线，不删代码，这是
# 人为加的硬指标").  What the table below describes is a hand-fitted constant
# stamped into code on 2026-08-15 and never refitted: it patches the served
# histogram after the fact, on residuals of our own output, anchored on
# whichever bucket the calibrated centre happens to fall in.  Re-measured on
# 2026-09-18 over 1,479 served P0 vectors (485 station-days, 11 days, day
# bootstrap): worth 0.85% of RPS fleet-wide, all of it from h12_24 and the
# 45-degree band, indistinguishable from zero in the other three bands; its
# +2 factor was fitted when that offset over-claimed (0.89) and the offset now
# under-claims by half (realised/claimed 1.49 [1.13, 1.90]); the bucket anchor
# is a step (25.51 -> anchor 26, 25.49 -> anchor 25) that 20% of served
# vectors sit within 0.1C of.  Its mean push is +0.04C against the +1C bias
# calibration -- this is not where a day's warmth comes from.
#
# ``off`` (the default) makes ``apply_neighbour_direction_tilt`` identity on
# every path -- serving, the OOS gate and its ablation arm alike -- and a new
# artifact no longer carries the method tag, so the served math stays the
# gated math.  ``on`` is the research arm that restores the table verbatim.
# The asymmetry it patched is real at the source (truth minus calibrated
# centre: median +0.1C, skew -0.37 [-0.73, +0.03], lead-dependent) and
# belongs in the member calibration as a third fitted moment beside bias and
# spread, applied on the temperature axis before bucketing; that is the rule
# this constant was standing in for.
NEIGHBOUR_DIRECTION_MODE = (
    os.environ.get("PM_NEIGHBOUR_DIRECTION", "").strip().lower() or "off"
)
if NEIGHBOUR_DIRECTION_MODE not in {"off", "on"}:
    raise RuntimeError(
        f"unknown neighbour direction mode: {NEIGHBOUR_DIRECTION_MODE}"
    )
NEIGHBOUR_DIRECTION_RETIRED_STATE = "RETIRED_CHARTER_147"
# Charter 43 (2026-08-14/15), retired by charter 147 above; kept as the
# research arm's record.  The served distribution is symmetric about its
# centre and the truth is not,
# and how far it leans depends on where the station sits.  Every number here
# is measured in-pipeline -- this ensemble, this inflation, this truth lane --
# because the factors describe our own distribution's defect rather than the
# atmosphere: a pipeline scoring a single Gaussian against a reanalysis
# disagrees with this one in sign on two of the four offsets, for reasons
# that are mechanical (its far buckets are under-claimed where our inflated
# mixture over-claims them, and it scores against a grid box where we score
# against an airport thermometer that runs hot on clear calm afternoons).
#
# Fitted over 5,233 station-days with intervals bootstrapped over days --
# stations on one day share weather, so the day is the independent unit and
# there are 47 of them.  A band-offset cell has to clear two hurdles to be
# served on its own: its interval must exclude 1.0, and its conservative end
# (the end nearest one) must be at least as strong as the fleet factor.  The
# second hurdle is what keeps 1,500 station-days of one band from overriding
# 5,233 of all of them on a weaker reading.
#
#   band      offset  claimed  factor  width  own evidence  beats fleet
#   00-25     -2       9.4%    0.66    0.35      yes           yes
#   00-25     -1/+1   20-23%   ~1.01   0.32      no             -
#   25-35     -2      10.4%    0.71    0.31      yes           no
#   25-35     +1      21.0%    1.14    0.24      yes           no
#   35-45     -2       9.7%    0.66    0.38      yes           yes
#   35-45     -1      22.1%    0.88    0.23      yes           no
#   45+       -1      23.0%    0.64    0.28      yes           yes
#   45+       +1      19.1%    1.57    0.45      yes           yes
#   every band +2      ~9%     0.80-0.88  0.33-0.64  no        -
#
# Four of sixteen cells clear both.  The rest are not silent for one reason
# but three: some bands genuinely have no lean at that offset (the tropics at
# plus or minus one), some have a real lean the fleet factor already carries
# better, and every plus-or-minus-two cell is simply too thin -- nine percent
# of claimed mass makes a ratio whose interval spans 0.33 to 0.64, wide
# enough to swallow the effect.  As truth accumulates those intervals narrow
# and cells qualify on their own; the rule does not change, the table does.
#
# Measured on the production temporal holdout: 0.247% CRPS and 0.277% NLL,
# against 0.189%/0.167% for banding the lower neighbour alone and
# 0.133%/0.098% fleet-wide.
NEIGHBOUR_DIRECTION_FLEET = {
    -2: 0.867,
    -1: 0.980,
    1: 1.030,
    2: 1.000,
}
NEIGHBOUR_DIRECTION_BY_BAND = {
    "lat_00_25": {-2: 0.838, -1: 0.980, 1: 1.030, 2: 1.000},
    "lat_25_35": {-2: 0.867, -1: 0.980, 1: 1.030, 2: 1.000},
    "lat_35_45": {-2: 0.852, -1: 0.980, 1: 1.030, 2: 1.000},
    "lat_45_plus": {-2: 0.867, -1: 0.797, 1: 1.348, 2: 1.000},
}


def neighbour_direction_band(latitude: float | None) -> str:
    """The latitude band whose factors a station is served.

    Hemispheres share a band -- the lean tracks distance from the equator,
    not which side of it -- and an unknown latitude serves the band whose
    cells are closest to the fleet, since not knowing where a station is is
    not evidence about its shape.
    """

    if latitude is None:
        return "lat_25_35"
    magnitude = abs(float(latitude))
    if magnitude < 25.0:
        return "lat_00_25"
    if magnitude < 35.0:
        return "lat_25_35"
    if magnitude < 45.0:
        return "lat_35_45"
    return "lat_45_plus"


def neighbour_direction_serves(artifact_combination_method: str) -> bool:
    """Whether P0 runs the tilt function for this artifact at all.

    Retired: always, so every envelope carries the retirement receipt and a
    replay can see which rule shaped it.  Research arm: only artifacts whose
    holdout was scored with the tag, exactly as before.
    """

    if NEIGHBOUR_DIRECTION_MODE == "off":
        return True
    return NEIGHBOUR_DIRECTION_METHOD_TAG in str(artifact_combination_method)


def neighbour_direction_method_tag_suffix() -> str:
    """What the OOS gate appends to an artifact's combination method.

    Empty while retired: the holdout scored an untilted blend, and an artifact
    that said otherwise would serve the tilt the day the research arm came
    back on, on a certificate that never priced it.
    """

    if NEIGHBOUR_DIRECTION_MODE == "off":
        return ""
    return "::" + NEIGHBOUR_DIRECTION_METHOD_TAG


def apply_neighbour_direction_tilt(
    probability: "Mapping[str, float]",
    *,
    ordinals: "Mapping[str, int]",
    center_bucket: str,
    latitude: float | None,
) -> tuple[dict[str, float], dict[str, Any]]:
    """Lean the buckets around the centre by the station's latitude band.

    Identity while retired (charter 147): the vector comes back renormalised
    and untouched, with a receipt that says so, whatever the artifact's tag,
    the station's band or the centre bucket.
    """

    if not probability:
        raise ValueError("neighbour_direction_probability_empty")
    if NEIGHBOUR_DIRECTION_MODE == "off":
        total = sum(float(v) for v in probability.values())
        if total <= 0:
            raise ValueError("neighbour_direction_probability_empty")
        return {str(k): float(v) / total for k, v in probability.items()}, {
            "schema": "neighbour_direction_receipt_v1",
            "method_tag": NEIGHBOUR_DIRECTION_METHOD_TAG,
            "state": NEIGHBOUR_DIRECTION_RETIRED_STATE,
            "mode": NEIGHBOUR_DIRECTION_MODE,
            "band": neighbour_direction_band(latitude),
            "latitude": None if latitude is None else float(latitude),
            "center_bucket": str(center_bucket),
            "applied_factors": {},
        }
    if center_bucket not in ordinals:
        return {str(k): float(v) for k, v in probability.items()}, {
            "schema": "neighbour_direction_receipt_v1",
            "method_tag": NEIGHBOUR_DIRECTION_METHOD_TAG,
            "state": "NO_CENTER_BUCKET",
        }
    band = neighbour_direction_band(latitude)
    factors = NEIGHBOUR_DIRECTION_BY_BAND[band]
    center_ordinal = int(ordinals[center_bucket])
    tilted: dict[str, float] = {}
    applied: dict[str, float] = {}
    for key, raw in probability.items():
        offset = int(ordinals[key]) - center_ordinal
        factor = factors.get(offset, 1.0)
        if factor != 1.0:
            applied[str(key)] = factor
        tilted[str(key)] = float(raw) * factor
    total = sum(tilted.values())
    if total <= 0:
        raise ValueError("neighbour_direction_probability_empty")
    return {key: value / total for key, value in tilted.items()}, {
        "schema": "neighbour_direction_receipt_v1",
        "method_tag": NEIGHBOUR_DIRECTION_METHOD_TAG,
        "state": "APPLIED" if applied else "NO_NEIGHBOUR_IN_SUPPORT",
        "band": band,
        "latitude": None if latitude is None else float(latitude),
        "center_bucket": str(center_bucket),
        "applied_factors": applied,
    }


BAND_RELIABILITY_METHOD_TAG = "band_reliability_v1"
# Per-band reliability of the final claimed probability, measured against
# settled truth: the last P0 snapshot of each station-day, realised hit rate
# over summed claim per claimed-probability band, intervals bootstrapped over
# days (stations on one day share weather; 15 settled days, 254 station-days,
# 2026-07-28..08-14).
#
#   band          claimed ratio  95% interval  sign-stable halves  served
#   [0.00, 0.02)      1.74       [0.62, 3.20]        no             1.0
#   [0.02, 0.12)      0.69       [0.55, 0.86]        yes (.80/.61)  0.86
#   [0.12, 0.20)      1.23       [1.08, 1.39]        yes (1.28/1.20) 1.08
#   [0.20, 0.35)      1.12       [0.90, 1.33]        --             1.10*
#   [0.35, 1.00]      0.72       [0.25, 1.03]        --             1.0
#
# Every band carries a factor; a band earns a non-identity one only when its
# interval excludes 1.0 AND its ratio keeps the same sign of miscalibration
# across a chronological half-split, and it serves the interval end nearest
# one.  Both hurdles are load-bearing: fitting the point estimates on all
# bands scores -0.97 NLL/day on the forward window, because the deep-tail
# ratio (2.07x on the first nine days, no tail hits on the last six) does
# not persist -- the same non-persistence that killed the per-side inflation
# candidate.  The shoulder and core cells, domain-capped so the shoulder may
# only shrink and the core may only lift, score +0.13 NLL/day on the 60/40
# forward split and +0.17 with 11/15 day wins on leave-one-day-out.
# Renormalisation pays the shoulder's surplus mostly into the core, which is
# the direction the measurement says the mass belongs.
#
# *The [0.20, 0.35) cell qualified through the second evidence lane: the OOS
# prediction archive (24 event days, 2,408 predictions, model-bucket
# geometry) measures it 1.18 [1.10, 1.27] with stable halves (1.11/1.26),
# and transferring its conservative end onto the serving geometry improves
# the forward test window from +0.34 to +0.46 NLL/day.  The two lanes agree
# on the shoulder (0.69/0.69) and the core (1.23/1.30), which is what makes
# the transfer admissible.  Two cells measured there do NOT transfer and
# stay identity: [0.35, 1.00] (0.75, significant there, but applying its
# shrink to serving scores worse than leaving it alone) and the deep tail,
# which REVERSES sign between geometries -- narrow model buckets far from
# centre over-claim (0.30) while open-ended market tail buckets aggregate
# the whole tail and under-claim (1.74) -- so no shared factor can serve
# both and the archive's extra sample cannot help.  As truth accumulates
# the intervals narrow and cells qualify on their own; the rule does not
# change, the table does.
#
# Two extensions were measured and REJECTED on 2026-08-15; the numbers are
# recorded so nobody re-litigates them on instinct:
#
# * A separate gate-geometry table.  Fitting the archive's own factors and
#   applying them only in gate scoring measures +3.5 NLL/day on its 60/40
#   split (10/10 test days) -- and is still wrong, because nothing in
#   production serves the model-bucket image: the gate would be scoring a
#   transform that never runs, breaking the one invariant the admission
#   machinery rests on (the gate scores exactly what serving applies, same
#   code path, same constants).  The gate keeps applying this serving
#   table, whose cells never conflict in sign with the gate geometry's own
#   measurement.  Revisit only if a served consumer of model-bucket
#   geometry ever exists.
#
# * Stage-specific tables for P0-M and P1.  P0-M has 3 settled station-days
#   (nothing to fit).  P1, at 152 station-days, is simply better
#   calibrated than P0 -- shoulder 0.72 [0.50, 1.28], core 0.95, and
#   [0.20, 0.35) at 1.02 -- with no cell clearing both hurdles, which also
#   confirms that copying P0's factors onto other stages would be wrong.
#   ``scripts/refit_band_reliability.py`` re-measures all of this against
#   accumulated truth; the daily calibration cron flags drift.
#
# 2026-08-15, review step removed (user ruling): the LIVE table is
# DB-resident and refits itself -- the daily cron fits under the same
# admission rule (band_reliability_store) and activates the result, and
# every artifact pins the exact table version its holdout was scored
# against.  The constants below are the bootstrap vintage: they serve
# artifacts whose method carries the bare tag (pre-store), and tests.
# They are not maintained by hand any more; the store is the authority.
BAND_RELIABILITY_FACTORS = (
    ((0.00, 0.02), 1.0),
    ((0.02, 0.12), 0.86),
    ((0.12, 0.20), 1.08),
    ((0.20, 0.35), 1.10),
    ((0.35, 1.01), 1.0),
)


BAND_RELIABILITY_METHOD_TAG_V2 = "band_reliability_v2"
# The axis (user ruling, 2026-09-07: "band 表换轴").  A band table is a
# recalibration map on a distribution over an ORDERED axis, and the disease
# it treats is over-dispersion -- too little mass on the centre, too much
# four buckets out.  That is a statement about POSITION, and keying the
# cells by the claimed probability instead has two costs that were measured
# rather than argued:
#
# * the key is not invariant under the map.  A bucket is put in the
#   [0.40,0.45) cell because it claims 0.44; multiply by 1.05, renormalise,
#   and it claims 0.46 and belongs to a cell whose factor it never got.
#   Fitted on 1204 day-ahead station-days, applied once and re-measured,
#   the cell ratios land at 0.79-1.10 -- a table fitted to make them 1.0
#   does not make them 1.0.  Distance from the centre does not move when
#   the centre's own factor is the largest, so the distance table is a
#   fixed point of itself.
#
# * it pools things that behave differently.  On 2026-09-05 the [5-10%)
#   cell held VILK's bucket one step off centre (7.0%), ZHHH's three steps
#   off (7.2%) and ZHHH's five steps off (6.1%); those three realise
#   1.151, 0.783 and 0.490 of their claims.  Three-fold cross-validation by
#   day: the distance axis scores +1.64% NLL against +1.10% for the claim
#   axis, with five cells against eleven, and stacking the two is WORSE
#   than distance alone (+1.26%) -- once position is conditioned on, the
#   level carries nothing.  A single global temperature p^k, k=1.35, buys
#   +1.05%, which is what eleven claim-level parameters were spending
#   themselves on.
#
# Charter 43's ``neighbour_direction_v1`` is untouched and stays upstream:
# it is SIGNED and fixes asymmetry, this is UNSIGNED and fixes width, and
# the factors below were measured on vectors that already carry it, so the
# two stack rather than double-count.  The corroboration is that the signed
# version of this table scores WORSE out of sample (+1.33%): the direction
# signal has already been taken, and what is left is symmetric.
BAND_RELIABILITY_AXIS_CLAIM = "claim"
BAND_RELIABILITY_AXIS_CENTRE_DISTANCE = "centre_distance"


def _centre_distances(
    probability: "Mapping[str, float]",
    ordinals: "Mapping[str, int]",
) -> dict[str, float]:
    """How many buckets each key sits from the distribution's own peak.

    The peak, not the mean: the mean moves under the map and the argmax
    does not, which is the whole reason this axis is stable.  Ties break on
    the ordinal so two runs of the same vector agree.
    """

    ranked = [
        (float(value), -int(ordinals[key]), str(key))
        for key, value in probability.items()
        if key in ordinals
    ]
    if not ranked:
        raise ValueError("band_reliability_ordinals_missing")
    centre = max(ranked)[2]
    centre_ordinal = int(ordinals[centre])
    return {
        str(key): float(abs(int(ordinals[key]) - centre_ordinal))
        for key in probability
        if key in ordinals
    }


def apply_band_reliability(
    probability: "Mapping[str, float]",
    *,
    factors: "Sequence[tuple[tuple[float, float], float]] | None" = None,
    ordinals: "Mapping[str, int] | None" = None,
) -> tuple[dict[str, float], dict[str, Any]]:
    """Rescale each bucket by its cell's factor.

    Two axes, one lookup.  With ``ordinals`` the cell is read from the
    bucket's distance from the distribution's peak (the axis the table is
    fitted on since 2026-09-07); without them it is read from the claim
    itself, which is what every artifact pinned to a ``band_reliability_v1``
    table was gated against and must keep being served.  Charter 60: an
    artifact is served with the exact table its evidence was scored
    against, and that now includes which axis the table was scored on.

    This must stay the outermost transform either way: the factors were
    measured on served distributions, and moving the layer inward would
    rescale claims the measurement never saw.

    ``factors`` defaults to the static bootstrap table below; production
    passes the DB-resident table the artifact was gated against (see
    ``band_reliability_store``), so a refit never changes an already-served
    artifact's math.
    """

    if not probability:
        raise ValueError("band_reliability_probability_empty")
    table = (
        BAND_RELIABILITY_FACTORS if factors is None else tuple(factors)
    )
    if ordinals is None:
        axis = BAND_RELIABILITY_AXIS_CLAIM
        keyed = {str(key): float(raw) for key, raw in probability.items()}
        tag = BAND_RELIABILITY_METHOD_TAG
    else:
        axis = BAND_RELIABILITY_AXIS_CENTRE_DISTANCE
        keyed = _centre_distances(probability, ordinals)
        tag = BAND_RELIABILITY_METHOD_TAG_V2
    adjusted: dict[str, float] = {}
    applied: dict[str, float] = {}
    for key, raw in probability.items():
        value = keyed.get(str(key))
        factor = 1.0
        if value is not None:
            for (lower, upper), band_factor in table:
                if lower <= value < upper:
                    factor = band_factor
                    break
        if factor != 1.0:
            applied[str(key)] = factor
        adjusted[str(key)] = float(raw) * factor
    total = sum(adjusted.values())
    if total <= 0:
        raise ValueError("band_reliability_probability_empty")
    served = {key: value / total for key, value in adjusted.items()}
    receipt: dict[str, Any] = {
        # The inverse reads ``applied_factors`` and
        # ``pre_normalisation_mass`` only, so it is axis-blind by
        # construction and one function keeps serving both vintages.
        "schema": "band_reliability_receipt_v1",
        "method_tag": tag,
        "axis": axis,
        "state": "APPLIED" if applied else "NO_BAND_IN_SUPPORT",
        "applied_factors": applied,
        "pre_normalisation_mass": total,
    }
    if ordinals is not None:
        # The distance cells are read from the INPUT peak, and the output
        # peak is not guaranteed to be the same bucket: nothing makes the
        # centre cell's factor the largest (live P0 table 4f522947: centre
        # 1.0, one step out 1.0475), and 168 of 2,507 distance-axis P0 rows
        # served 2026-09-12..13 moved their peak.  Both are recorded so a
        # reader keys cells on the anchor the table was applied through,
        # not on a peak it recomputes from the served vector.
        receipt["anchor_bucket"] = next(
            key for key, distance in keyed.items() if distance == 0.0
        )
        receipt["served_peak_bucket"] = max(
            (float(value), -int(ordinals[key]), str(key))
            for key, value in served.items()
            if key in ordinals
        )[2]
    return served, receipt


# Zero through the strata the mixture already predicts correctly (<1C and
# 1-2C measured 0.0%/0.0% and 5.0%/5.5% valley hit against prediction), full
# strength by 2C where under-prediction is unambiguous.  0.4 reproduces the
# observed valley rates: 2-3C stratum 0.6*0.173 + 0.4*~0.45 ~= 0.28 against
# 27.8% observed; >=3C moves 22.9% -> ~0.36 against 45.5% observed (n=11),
# deliberately short of the point estimate rather than tuned onto it.
CONSENSUS_BRIDGE_LAMBDA_MAX = 0.4
CONSENSUS_BRIDGE_GAP_START_C = 1.5
CONSENSUS_BRIDGE_GAP_FULL_C = 2.0


def consensus_bridge_lambda(gap_c: float) -> float:
    """Blend weight for the consensus component at this center gap."""

    gap = float(gap_c)
    if gap <= CONSENSUS_BRIDGE_GAP_START_C:
        return 0.0
    if gap >= CONSENSUS_BRIDGE_GAP_FULL_C:
        return CONSENSUS_BRIDGE_LAMBDA_MAX
    return CONSENSUS_BRIDGE_LAMBDA_MAX * (
        (gap - CONSENSUS_BRIDGE_GAP_START_C)
        / (CONSENSUS_BRIDGE_GAP_FULL_C - CONSENSUS_BRIDGE_GAP_START_C)
    )


def shift_probability(
    probability: Mapping[str, float],
    *,
    delta_c: float,
    ordered_keys: Sequence[str],
    key_centers_c: Sequence[float],
) -> dict[str, float]:
    """Move a bucket histogram by ``delta_c`` with linear mass splitting.

    Mass leaving one bucket center lands between two adjacent centers and is
    divided in proportion to proximity; anything pushed past either end
    accumulates in the edge bucket, matching how the open-ended edge buckets
    absorb their tails.
    """

    keys = [str(key) for key in ordered_keys]
    centers = [float(center) for center in key_centers_c]
    if len(keys) != len(centers) or len(keys) < 2:
        raise ValueError("consensus_bridge_bucket_axis_invalid")
    shifted = {key: 0.0 for key in keys}
    for key, mass in probability.items():
        weight = float(mass)
        if weight <= 0.0:
            continue
        try:
            position = centers[keys.index(str(key))] + float(delta_c)
        except ValueError:
            raise ValueError("consensus_bridge_bucket_unknown") from None
        if position <= centers[0]:
            shifted[keys[0]] += weight
            continue
        if position >= centers[-1]:
            shifted[keys[-1]] += weight
            continue
        for index in range(len(centers) - 1):
            lower = centers[index]
            upper = centers[index + 1]
            if lower <= position <= upper:
                span = upper - lower
                upper_share = (position - lower) / span if span > 0 else 0.0
                shifted[keys[index]] += weight * (1.0 - upper_share)
                shifted[keys[index + 1]] += weight * upper_share
                break
    return shifted


def bridged_cross_probability(
    *,
    system_probabilities: Mapping[str, Mapping[str, float]],
    system_weights: Mapping[str, float],
    system_centers_c: Mapping[str, float],
    ordered_keys: Sequence[str],
    key_centers_c: Sequence[float],
) -> tuple[dict[str, float], dict[str, Any]]:
    """Blend the plain mixture with the recentered consensus component.

    Returns the final probability plus a receipt stating the gap, the blend
    weight and both components' mass on the buckets strictly between the
    center buckets, so a lineage reader can see exactly what the bridge did.
    """

    keys = [str(key) for key in ordered_keys]
    mixture = {
        key: sum(
            float(system_weights[system])
            * float(system_probabilities[system].get(key, 0.0))
            for system in system_weights
        )
        for key in keys
    }
    total = sum(mixture.values())
    if total <= 0:
        raise ValueError("consensus_bridge_mixture_empty")
    mixture = {key: value / total for key, value in mixture.items()}

    centers = {
        system: float(center)
        for system, center in system_centers_c.items()
    }
    consensus_center_c = sum(
        float(system_weights[system]) * centers[system]
        for system in system_weights
    )
    gap_c = max(centers.values()) - min(centers.values())
    blend = consensus_bridge_lambda(gap_c)
    if blend <= 0.0:
        receipt = {
            "method_tag": CONSENSUS_BRIDGE_METHOD_TAG,
            "center_gap_c": round(gap_c, 6),
            "lambda": 0.0,
            "consensus_center_c": round(consensus_center_c, 6),
        }
        return mixture, receipt

    consensus = {key: 0.0 for key in keys}
    for system, weight in system_weights.items():
        recentered = shift_probability(
            system_probabilities[system],
            delta_c=consensus_center_c - centers[system],
            ordered_keys=keys,
            key_centers_c=key_centers_c,
        )
        for key in keys:
            consensus[key] += float(weight) * recentered[key]
    consensus_total = sum(consensus.values())
    if consensus_total <= 0:
        raise ValueError("consensus_bridge_consensus_empty")
    consensus = {
        key: value / consensus_total for key, value in consensus.items()
    }

    blended = {
        key: (1.0 - blend) * mixture[key] + blend * consensus[key]
        for key in keys
    }
    receipt = {
        "method_tag": CONSENSUS_BRIDGE_METHOD_TAG,
        "center_gap_c": round(gap_c, 6),
        "lambda": round(blend, 6),
        "consensus_center_c": round(consensus_center_c, 6),
    }
    return blended, receipt
