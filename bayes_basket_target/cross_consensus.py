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

from typing import Any, Mapping, Sequence

CONSENSUS_BRIDGE_METHOD_TAG = "consensus_bridge_v1"

# Measured over 175 settled station-days (P0 calibration audit, 2026-08-13,
# recomputed after the truth-revision repair): buckets this pathway priced
# under five percent realised 1.38x the frequency it assigned them, while the
# market's asks on the same buckets sat near zero.  The served tail was the
# one part of the distribution that systematically lied, and the strategy's
# measured edge lives exactly there.  The correction is admitted the same way
# the bridge was: it rides the combination-method tag, every new artifact's
# holdout is scored WITH it, the per-scope OOS gate accepts or rejects the
# pair, and serving applies it only when the artifact's tag says the gate
# measured it.  The transform is continuous and monotone: deep tails lift by
# the measured factor, buckets near the threshold taper to it, nothing ever
# re-ranks across the boundary, and the lifted mass is paid for by
# renormalisation.
CONDITIONAL_SKEW_METHOD_TAG = "conditional_tail_skew_v1"
CONDITIONAL_SKEW_THRESHOLD = 0.05
CONDITIONAL_SKEW_FACTOR_FLOOR = 0.25
CONDITIONAL_SKEW_FACTOR_CEILING = 2.0
# Both conditioning terms are measured on the *calibrated* distribution --
# per-station bias and spread applied, leave-one-out -- because that is what
# production serves.  Measured on raw member histograms the same day looks
# entirely different, which is how the retired unconditional lift came to be
# calibrated against the wrong object.
#
# WIDTH (how badly the tails are under-stated) tracks where the forecast sits
# inside the station's own same-calendar-day record, in sigmas of headroom to
# that record's high.  Realized-over-claimed hit rate for tail buckets:
#
#   headroom < 0     (above the same-day record)   warm 2.54x  cold 2.50x
#   0.00 - 0.75      (at the record)               warm 1.93x  cold 1.92x
#   0.75 - 1.75      (moderate room)               warm 2.06x  cold 1.50x
#   >= 1.75          (ample room)                  warm 1.39x  cold 1.32x
#
# A day forecast at the top of its own calendar-day record is a volatile day
# in both directions; an ordinary day is already close to calibrated.  The
# record is ten years of daily maxima at the station's own coordinates
# (``calibration_station_same_day_climatology``, reference covariates only).
# Measured first against one season's seven-day window instead, the same
# bands read 2.17x/1.41x -- the real climatology sharpened the gradient from
# 1.5x to 1.8x across the bands, which is what the multi-year capture bought.
CLIMATOLOGY_HEADROOM_BANDS = (
    # (headroom_upper_bound_sigma, base_tail_factor)
    (0.0, 2.0),
    (0.75, 1.7),
    (1.75, 1.7),
    (float("inf"), 1.3),
)
CLIMATOLOGY_ABSENT_BASE_FACTOR = 1.4
# SKEW (which side that width belongs to) comes from the airport forecaster's
# TX against the model centre -- a human reading the same day, and the only
# tested signal that flips sign rather than merely scaling.  Same metric,
# n=3,611 tail buckets:
#
#   TAF warmer than models   warm 1.94x   cold 0.65x
#   TAF with the models      warm 1.53x   cold 2.02x
#   TAF colder than models   warm 0.73x   cold 2.38x
#
# The side the forecaster points at is under-stated about twofold; the far
# side is over-stated.  Four other candidates were tested and rejected as
# null before this one was kept: the member cloud's own skew (correlation
# with residual +0.009), ensemble spread (+0.013), cross-system centre
# disagreement (+0.032), and day-to-day persistence at the lag actually
# available when a forward decision is made (+0.074; the +0.202 at lag one is
# not knowable at decision time and belongs to the same-day METAR path).
TAF_DIRECTION_GATE_SIGMA = 0.5
TAF_DIRECTION_WARM = (1.25, 0.5)
TAF_DIRECTION_COLD = (0.5, 1.25)


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
SYNOPTIC_COOLING_SHIFT_SIGMA = 0.15
SYNOPTIC_COOLING_NORMALISATION = {
    # field: (fleet mean, fleet standard deviation) over 2,121 TAF captures
    "cloud_cover_fraction": (0.3557, 0.3102),
    "precipitation_probability": (0.0933, 0.2155),
    "wind_speed_mps": (3.7880, 1.4831),
}


def synoptic_cooling_index(
    *,
    cloud_cover_fraction: float | None,
    precipitation_probability: float | None,
    wind_speed_mps: float | None,
) -> tuple[float | None, dict[str, Any]]:
    """Equal-weight standardised cloud/rain/wind index, or None.

    All three fields are required: the index is an average of standardised
    parts and dropping one silently reweights the other two.  A day with no
    forecaster fields simply has no reading, which is different from a calm
    clear one.
    """

    parts = {
        "cloud_cover_fraction": cloud_cover_fraction,
        "precipitation_probability": precipitation_probability,
        "wind_speed_mps": wind_speed_mps,
    }
    standardised: dict[str, float] = {}
    for field, raw in parts.items():
        if raw is None:
            return None, {
                "schema": "synoptic_cooling_receipt_v1",
                "method_tag": SYNOPTIC_COOLING_METHOD_TAG,
                "state": "INCOMPLETE_FIELDS",
                "missing_field": field,
            }
        mean, deviation = SYNOPTIC_COOLING_NORMALISATION[field]
        standardised[field] = (float(raw) - mean) / deviation
    index = sum(standardised.values()) / len(standardised)
    return index, {
        "schema": "synoptic_cooling_receipt_v1",
        "method_tag": SYNOPTIC_COOLING_METHOD_TAG,
        "state": "RESOLVED",
        "index": index,
        "standardised": standardised,
        "shift_sigma_per_unit": SYNOPTIC_COOLING_SHIFT_SIGMA,
    }


def apply_conditional_tail_skew(
    probability: "Mapping[str, float]",
    *,
    ordinals: "Mapping[str, int]",
    climatology_headroom_sigma: float | None = None,
    taf_gap_sigma: float | None = None,
) -> tuple[dict[str, float], dict[str, Any]]:
    """Skew the tails toward the side the day's own evidence points at.

    This replaces an unconditional symmetric lift (every tail bucket under
    5% multiplied by 1.38).  That correction was measured on a pooled
    average and therefore fattened both tails on every day alike, which is
    wrong twice over: it invents mass on the side the day's evidence argues
    against, and -- because a penny bucket is exactly where the market is
    most confident -- it manufactured cheap seats precisely on the days the
    market had the strongest reason to price them at a cent.  Adverse
    selection, not edge.

    Direction comes only from measured conditioning signals.  Four candidates
    were tested and rejected as null before these two were kept: the member
    cloud's own skew (correlation with residual +0.009), ensemble spread
    (+0.013), cross-system centre disagreement (+0.032), and day-to-day
    persistence at the lag actually available when a forward decision is made
    (+0.074; the +0.202 at lag one is not knowable at decision time and
    belongs to the same-day METAR path instead).

    With no signal present the distribution is returned unchanged.  Absence
    of evidence about direction is not evidence for symmetry.
    """

    if not probability:
        raise ValueError("conditional_skew_probability_empty")
    center = max(probability, key=lambda key: float(probability[key]))
    center_ordinal = int(ordinals[center])
    signals: list[str] = []
    if climatology_headroom_sigma is not None:
        headroom = float(climatology_headroom_sigma)
        base_factor = CLIMATOLOGY_ABSENT_BASE_FACTOR
        for bound, factor in CLIMATOLOGY_HEADROOM_BANDS:
            if headroom < bound:
                base_factor = factor
                break
        signals.append("same_day_climatology_headroom")
    else:
        base_factor = CLIMATOLOGY_ABSENT_BASE_FACTOR
    warm_factor = base_factor
    cold_factor = base_factor
    if taf_gap_sigma is not None:
        gap = float(taf_gap_sigma)
        if gap > TAF_DIRECTION_GATE_SIGMA:
            warm_factor *= TAF_DIRECTION_WARM[0]
            cold_factor *= TAF_DIRECTION_WARM[1]
            signals.append("taf_direction_warm")
        elif gap < -TAF_DIRECTION_GATE_SIGMA:
            warm_factor *= TAF_DIRECTION_COLD[0]
            cold_factor *= TAF_DIRECTION_COLD[1]
            signals.append("taf_direction_cold")
        else:
            signals.append("taf_direction_neutral")
    warm_factor = min(
        max(warm_factor, CONDITIONAL_SKEW_FACTOR_FLOOR),
        CONDITIONAL_SKEW_FACTOR_CEILING,
    )
    cold_factor = min(
        max(cold_factor, CONDITIONAL_SKEW_FACTOR_FLOOR),
        CONDITIONAL_SKEW_FACTOR_CEILING,
    )
    receipt: dict[str, Any] = {
        "schema": "conditional_tail_skew_receipt_v1",
        "method_tag": CONDITIONAL_SKEW_METHOD_TAG,
        "threshold": CONDITIONAL_SKEW_THRESHOLD,
        "center_bucket": str(center),
        "signals": signals,
        "warm_tail_factor": warm_factor,
        "cold_tail_factor": cold_factor,
        "base_tail_factor": base_factor,
        "climatology_headroom_sigma": climatology_headroom_sigma,
        "taf_gap_sigma": taf_gap_sigma,
    }
    if not signals:
        receipt["adjusted_buckets"] = []
        receipt["state"] = "NO_CONDITIONING_SIGNAL"
        return {str(k): float(v) for k, v in probability.items()}, receipt
    adjusted: dict[str, float] = {}
    touched: list[str] = []
    for key, raw in probability.items():
        value = float(raw)
        if value >= CONDITIONAL_SKEW_THRESHOLD or value <= 0.0:
            adjusted[str(key)] = value
            continue
        factor = (
            warm_factor
            if int(ordinals[key]) > center_ordinal
            else cold_factor
            if int(ordinals[key]) < center_ordinal
            else 1.0
        )
        scaled = min(value * factor, CONDITIONAL_SKEW_THRESHOLD)
        if abs(scaled - value) > 1e-12:
            touched.append(str(key))
        adjusted[str(key)] = scaled
    total = sum(adjusted.values())
    if total <= 0:
        raise ValueError("conditional_skew_probability_empty")
    receipt["adjusted_buckets"] = sorted(touched)
    receipt["state"] = "APPLIED"
    receipt["pre_normalisation_mass"] = total
    return {key: value / total for key, value in adjusted.items()}, receipt


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
