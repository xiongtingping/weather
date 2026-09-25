"""A daily-maximum forecast built from METAR alone.

The trajectory layer that already exists answers a different question than its
name suggests.  It learns ``P(remaining warming | physical state)`` and then
spends that answer as a *likelihood ratio against the model's prior*, so its
output can only redistribute mass the models already placed.  Where the models
were wrong about the whole day that is worth very little: at ZUUU on
2026-08-12 the P0 prior held 76% of its mass at or below 37C, the day settled
at 38C, and no bounded ratio moves a posterior into a bucket the prior priced
near zero.  The measured consequence is that P1 selects the settled bucket
about as often as P0 does -- 75% against 74% -- which is another way of saying
the observation stage was contributing nothing to selection.

This module asks the question directly instead.  Given what the airport has
actually reported so far today -- how warm it already got, whether it is still
climbing, how dry the air is, how much cloud is over it, how long until the
day's warming window closes -- it returns a distribution over the day's
maximum.  It reads no model output: not the root probabilities, not the
expected tmax, not the hourly curve.  ``ConditionalMetarPathState`` carries
``root_expected_tmax_c`` and this module deliberately ignores it, because a
forecast that consults the models cannot serve as independent evidence about
them, and independence is the entire reason to build it.  Measured across the
same station-days, the aerodrome TAF -- the one existing source that owes
nothing to NWP -- runs a mean error of -0.012C while every model family runs
-0.5C to -1.9C, which is what an independent source is worth here.

The estimator is the one the trajectory training already fits, used without
the ratio: the day's maximum is the running high plus the remaining warming,
so a distribution over remaining warming conditioned on the current state is a
distribution over the maximum.  Two facts come free from that construction
rather than being imposed: the maximum can never fall below what has already
been observed, and as the day runs out the distribution collapses onto the
running high on its own.
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

from .contracts import ContractError, content_sha256
from .metar_path_state import ConditionalMetarPathState


SCHEMA = "bayes_basket_metar_physical_tmax_v1"
SYSTEM_ID = "METAR_PHYSICAL_PATH_TMAX"
INDEPENDENCE_FAMILY_ID = "METAR_PHYSICAL"
PROVIDER_ID = "AWC_METAR_PHYSICAL_PATH"
# v2 because v1's member rows are poisoned and their slots are taken.  The
# first projection read anchors from ``calibration_station_registry``, whose
# timezone_name column says America/New_York for 33 of 45 stations -- so a
# Helsinki "noon" was 19:00 local and the forecast had already seen the day's
# peak.  The member archive is append-only with a UNIQUE slot per (station,
# day, lead, member, extraction_version), so the honest recompute lives under
# a new version and the contaminated v1 rows stay visible as audit evidence
# that no scope contract will ever pair again.
#
# v3 because v2's noon prefix dropped the day's first report.  The planner
# selected [local midnight, local noon] with a text comparison, and the
# archive spells ``...T16:00:00.000Z`` where the bound read ``...T16:00:00Z``:
# '.' sorts before 'Z', so the local-midnight report fell outside its own day
# on 1,229 of 2,090 station-days and the noon running high was wrong on 20
# (LEMD 08-27 read 21 where the reports said 23; SAEZ the same day, 13 for
# 15).  User ruling 2026-09-25: 「修正并重算 8 月 1 日以来的历史」 -- every day
# since EARLIEST_EVENT_DATE is recomputed under v3, v2 stays as audit.
#
# v4 because v3 recomputed every day with today's parameters: the trajectory
# artifacts were last refit around 2026-09-01, so v3's 08-01..09-01 were
# forecast by parameters trained on those same days -- in-sample for this
# system's own record.  User ruling 2026-09-25, 「按当时参数重算」: each day
# uses the artifact available at its own local noon (``valid_from_utc``);
# days before any artifact existed (about 08-09..08-17 by station) are not
# forecast.  v3 stays as audit.
METHOD_VERSION = "metar_physical_tmax_from_conditioned_remaining_warming_v4"
# One member, because there is one forecast.
#
# An earlier draft published five quantiles of the conditioned distribution as
# members, which was wrong twice over.  Members in this system are exchangeable
# draws whose spread is *evidence about* uncertainty -- fifty perturbed model
# integrations, each a genuine alternative history.  Quantiles of a single
# prediction are not that; they are the prediction's own claim about itself,
# and feeding them into the channel that estimates spread would have the
# calibration fitting a correction on top of an assertion rather than on top of
# a measurement.  Second, evenly spaced quantiles are not exchangeable: giving
# q05 and q95 the same weight as q50 overweights the tails against the actual
# density, which on a real ZUUU state moved the published centre from 38.51C to
# 38.30C -- a fifth of a bucket, in a fixed direction.
#
# METAR is one observation stream and yields one number.  The uncertainty
# around it is established the way every other system's is: measured from its
# own out-of-sample record as ``train_rmse_c``, not self-reported.
EXPECTED_MEMBER_COUNT = 1
POINT_MEMBER_ID = "physical_path_expected"
# Retained for diagnostics and audit, never published as members.
DIAGNOSTIC_QUANTILES = (0.05, 0.25, 0.50, 0.75, 0.95)
MAX_DISTRIBUTION_POINTS = 512
# The predictors, and deliberately only these.
#
# The existing conditioner could not be reused: its v3 coefficient set demands
# four features derived from the model's hourly curve and refuses outright
# when that curve is absent, and even its base set carries
# ``root_expected_remaining_warming`` -- the models' own answer to the very
# question being asked.  Conditioning on that would make this system's error
# correlated with the errors it exists to catch, and the blend downstream
# treats its members as an independent family.  So the model terms are not
# merely optional here, they are excluded.
#
# What remains is what the airport reported: how fast it is climbing, whether
# the climb is flattening, whether it has already turned over, how much room
# the dew point leaves, what the cloud and wind are doing, and how much of the
# day is left.
PHYSICAL_COEFFICIENT_KEYS = (
    "base_distribution_mean",
    "momentum_c_per_hour",
    "acceleration_c_per_hour2",
    "drawdown_from_high_c",
    "dewpoint_depression_c",
    "cloud_cover_fraction",
    "precipitation_present",
    "wind_speed_mps",
    "remaining_hours",
)


def _finite(name: str, value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"metar_physical_tmax_{name}_invalid") from exc
    if not math.isfinite(result):
        raise ContractError(f"metar_physical_tmax_{name}_invalid")
    return result


def condition_on_physical_state(
    base_distribution: Mapping[float, float],
    path_state: ConditionalMetarPathState,
    *,
    coefficients: Mapping[str, Any],
    sigma_c: float,
) -> dict[float, float]:
    """Reweight the remaining-warming prior on observed conditions only.

    Same shape as the trajectory layer's conditioner -- a linear target for
    the remaining warming, then a Gaussian reweighting of the fitted prior
    around it -- with the model-derived terms removed rather than defaulted to
    zero.  Missing coefficients are treated as absent predictors, so an
    artifact fitted before this system existed still conditions on whatever
    physical terms it does carry instead of failing closed on the ones it
    never had.
    """

    parsed_base = {
        _finite("increment_c", key): _finite("increment_probability", value)
        for key, value in base_distribution.items()
    }
    if not parsed_base or sum(parsed_base.values()) <= 0.0:
        raise ContractError("metar_physical_tmax_base_distribution_invalid")
    sigma = _finite("sigma_c", sigma_c)
    if sigma <= 0.0:
        raise ContractError("metar_physical_tmax_sigma_invalid")
    beta = {
        key: _finite(f"coefficient_{key}", coefficients[key])
        for key in PHYSICAL_COEFFICIENT_KEYS
        if key in coefficients
    }
    base_mean = sum(
        increment * probability
        for increment, probability in parsed_base.items()
    ) / sum(parsed_base.values())
    observed = {
        "base_distribution_mean": base_mean,
        "momentum_c_per_hour": path_state.momentum_c_per_hour,
        "acceleration_c_per_hour2": path_state.acceleration_c_per_hour2,
        "drawdown_from_high_c": path_state.drawdown_from_high_c,
        "dewpoint_depression_c": path_state.dewpoint_depression_c,
        "cloud_cover_fraction": path_state.cloud_cover_fraction,
        "precipitation_present": float(path_state.precipitation_present),
        "wind_speed_mps": path_state.wind_speed_mps,
        "remaining_hours": (
            path_state.minutes_remaining_in_settlement_day / 60.0
        ),
    }
    target = sum(
        weight * _finite(f"state_{key}", observed[key])
        for key, weight in beta.items()
    )
    # The prior's own support bounds the answer: the fit may say the day has
    # four more degrees in it, but if no fold ever warmed more than three the
    # honest statement is three, not an extrapolation past every observation
    # the estimator was built from.
    target = max(min(parsed_base), min(max(parsed_base), target))
    weighted = {
        increment: probability
        * math.exp(-0.5 * ((increment - target) / sigma) ** 2)
        for increment, probability in parsed_base.items()
    }
    total = sum(weighted.values())
    if total <= 0.0:
        raise ContractError("metar_physical_tmax_conditioned_zero_mass")
    return {
        increment: value / total for increment, value in weighted.items()
    }


def predict_tmax_distribution(
    path_state: ConditionalMetarPathState,
    *,
    remaining_warming_distribution: Mapping[float, float],
    path_model_coefficients: Mapping[str, Any],
    path_model_sigma_c: float,
) -> dict[float, float]:
    """Distribution over today's maximum, in celsius, from METAR alone.

    ``remaining_warming_distribution`` is the artifact-owned prior fitted by
    the dated OOS folds; conditioning it on the current path is the same step
    the trajectory layer performs.  What differs is what happens next: the
    conditioned increments are added to the observed running high and returned
    as absolute temperatures, with no model prior multiplied in.
    """

    if not path_state.complete:
        raise ContractError("metar_physical_tmax_path_state_incomplete")
    conditioned = condition_on_physical_state(
        remaining_warming_distribution,
        path_state,
        coefficients=path_model_coefficients,
        sigma_c=_finite("sigma_c", path_model_sigma_c),
    )
    if not conditioned:
        raise ContractError("metar_physical_tmax_distribution_empty")
    if len(conditioned) > MAX_DISTRIBUTION_POINTS:
        raise ContractError("metar_physical_tmax_distribution_unbounded")
    running_high = _finite("running_high_c", path_state.running_high_c)
    absolute: dict[float, float] = {}
    for increment, probability in conditioned.items():
        step = _finite("increment_c", increment)
        if step < 0.0:
            # The day's maximum is a running maximum.  A negative increment is
            # not a cool outcome, it is an impossible one.
            raise ContractError("metar_physical_tmax_negative_increment")
        weight = _finite("increment_probability", probability)
        if weight < 0.0:
            raise ContractError("metar_physical_tmax_negative_probability")
        absolute[running_high + step] = absolute.get(
            running_high + step, 0.0
        ) + weight
    total = sum(absolute.values())
    if total <= 0.0:
        raise ContractError("metar_physical_tmax_distribution_degenerate")
    return {
        temperature: weight / total
        for temperature, weight in sorted(absolute.items())
    }


def tmax_quantiles(
    distribution: Mapping[float, float],
    *,
    quantiles: Sequence[float] = DIAGNOSTIC_QUANTILES,
) -> dict[float, float]:
    """Interpolation-free quantiles of a discrete temperature distribution.

    The support is a physical grid of achievable temperatures, so a quantile
    is reported as a point that the distribution actually places mass on
    rather than a value between two of them.
    """

    ordered = sorted(distribution.items())
    result: dict[float, float] = {}
    for q in quantiles:
        level = _finite("quantile", q)
        if not 0.0 < level < 1.0:
            raise ContractError("metar_physical_tmax_quantile_out_of_range")
        cumulative = 0.0
        chosen = ordered[-1][0]
        for temperature, weight in ordered:
            cumulative += weight
            if cumulative >= level:
                chosen = temperature
                break
        result[level] = chosen
    return result


def build_physical_tmax_members(
    path_state: ConditionalMetarPathState,
    *,
    station_id: str,
    event_date: str,
    remaining_warming_distribution: Mapping[float, float],
    path_model_coefficients: Mapping[str, Any],
    path_model_sigma_c: float,
    artifact_content_sha256: str,
) -> dict[str, Any]:
    """One station-day's single point member for the cross-system blend.

    Shaped like the aerodrome TAF's point entry so this system is admitted,
    weighted and out-of-sample scored by exactly the machinery that already
    judges the models, rather than by a private path of its own.  Whether it
    earns weight is then a measured question, which is the point.

    The published value is the distribution's mean rather than its median,
    because the weight this system receives downstream is set by its RMSE, and
    the mean is the estimator that minimises exactly that.  The quantiles ride
    along as diagnostics so a reviewer can see the shape behind the number
    without the blend mistaking that shape for sampled evidence.
    """

    distribution = predict_tmax_distribution(
        path_state,
        remaining_warming_distribution=remaining_warming_distribution,
        path_model_coefficients=path_model_coefficients,
        path_model_sigma_c=path_model_sigma_c,
    )
    expected = sum(
        temperature * weight for temperature, weight in distribution.items()
    )
    members = [
        {
            "member_id": POINT_MEMBER_ID,
            "member_role": "POINT",
            "raw_tmax_c": float(expected),
        }
    ]
    quantile_points = tmax_quantiles(distribution)
    body = {
        "schema": SCHEMA,
        "ensemble_system_id": SYSTEM_ID,
        "independence_family_id": INDEPENDENCE_FAMILY_ID,
        "provider_id": PROVIDER_ID,
        "method_version": METHOD_VERSION,
        "station_id": str(station_id).strip().upper(),
        "event_date": str(event_date),
        "as_of_utc": path_state.as_of_utc,
        "running_high_c": f"{path_state.running_high_c:.12g}",
        "expected_tmax_c": f"{expected:.12g}",
        "members": members,
        # Shape, labelled as diagnosis rather than evidence.  Nothing
        # downstream reads this to estimate spread; ``train_rmse_c`` fitted
        # from this system's own out-of-sample record does that.
        "diagnostic_tmax_quantiles_c": {
            f"q{int(round(level * 100)):02d}": f"{temperature:.12g}"
            for level, temperature in sorted(quantile_points.items())
        },
        # Named so an auditor can see at a glance that no model quantity
        # entered this forecast; the claim is worth nothing if it is only in
        # the docstring.
        "model_inputs_used": [],
        "path_state_sha256": path_state.content_sha256,
        "likelihood_artifact_sha256": str(artifact_content_sha256),
    }
    body["content_sha256"] = content_sha256(body)
    return body


__all__ = [
    "INDEPENDENCE_FAMILY_ID",
    "DIAGNOSTIC_QUANTILES",
    "EXPECTED_MEMBER_COUNT",
    "POINT_MEMBER_ID",
    "METHOD_VERSION",
    "PROVIDER_ID",
    "SCHEMA",
    "SYSTEM_ID",
    "build_physical_tmax_members",
    "predict_tmax_distribution",
    "tmax_quantiles",
]
