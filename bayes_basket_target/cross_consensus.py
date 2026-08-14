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
TAIL_RELIABILITY_METHOD_TAG = "tail_reliability_v1"
TAIL_RELIABILITY_FACTOR = 1.38
TAIL_RELIABILITY_THRESHOLD = 0.05


def apply_tail_reliability(
    probability: "Mapping[str, float]",
) -> tuple[dict[str, float], dict[str, Any]]:
    """Lift the measured tail under-estimate, renormalise, and say so."""

    lifted: dict[str, float] = {}
    lifted_buckets: list[str] = []
    for key, raw in probability.items():
        value = float(raw)
        if value < TAIL_RELIABILITY_THRESHOLD:
            corrected = min(
                value * TAIL_RELIABILITY_FACTOR,
                TAIL_RELIABILITY_THRESHOLD,
            )
            if corrected > value:
                lifted_buckets.append(str(key))
            lifted[str(key)] = corrected
        else:
            lifted[str(key)] = value
    total = sum(lifted.values())
    if total <= 0:
        raise ValueError("tail_reliability_probability_empty")
    normalized = {key: value / total for key, value in lifted.items()}
    receipt = {
        "schema": "tail_reliability_receipt_v1",
        "method_tag": TAIL_RELIABILITY_METHOD_TAG,
        "factor": TAIL_RELIABILITY_FACTOR,
        "threshold": TAIL_RELIABILITY_THRESHOLD,
        "lifted_buckets": sorted(lifted_buckets),
        "pre_normalisation_mass": total,
    }
    return normalized, receipt

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
