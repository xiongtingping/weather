from __future__ import annotations

import pytest

from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.metar_path_state import ConditionalMetarPathState
from bayes_basket_target.metar_physical_tmax import (
    EXPECTED_MEMBER_COUNT,
    PHYSICAL_COEFFICIENT_KEYS,
    build_physical_tmax_members,
    predict_tmax_distribution,
    tmax_quantiles,
)


_PRIOR = {0.0: 0.30, 1.0: 0.30, 2.0: 0.25, 3.0: 0.15}
_SIGMA = 0.8
_COEFFICIENTS = {
    "base_distribution_mean": 0.4,
    "momentum_c_per_hour": 0.9,
    "acceleration_c_per_hour2": 0.3,
    "drawdown_from_high_c": -1.1,
    "dewpoint_depression_c": 0.05,
    "cloud_cover_fraction": -1.2,
    "precipitation_present": -1.5,
    "wind_speed_mps": -0.02,
    "remaining_hours": 0.08,
    # Present in artifacts fitted for the trajectory layer, and this system
    # must ignore them rather than quietly consuming the models' answer.
    "root_expected_remaining_warming": 5.0,
    "predicted_peak_remaining_warming_c": 5.0,
    "future_curve_slope_c_per_hour": 5.0,
    "minutes_to_predicted_peak": 5.0,
    "next_curve_delta_from_high_c": 5.0,
}


def _state(**overrides: object) -> ConditionalMetarPathState:
    body: dict[str, object] = {
        "schema": "bayes_basket_metar_conditional_path_state_v1",
        "as_of_utc": "2026-08-12T06:00:00.000Z",
        "minutes_remaining_in_settlement_day": 600.0,
        "running_high_c": 36.5,
        "current_temperature_c": 36.5,
        "drawdown_from_high_c": 0.0,
        "momentum_c_per_hour": 1.2,
        "acceleration_c_per_hour2": -0.2,
        "dewpoint_depression_c": 12.0,
        "cloud_cover_fraction": 0.1,
        "precipitation_present": False,
        "wind_speed_mps": 2.0,
        "wind_gust_mps": 3.0,
        "pressure_hpa": 1004.0,
        # Deliberately a poor model forecast: nothing here may reach the
        # prediction.
        "root_expected_tmax_c": 34.5,
        "root_expected_remaining_warming_c": 0.0,
        "future_curve_sha256": None,
        "future_curve_complete": False,
        "predicted_peak_temperature_c": 0.0,
        "minutes_to_predicted_peak": 0.0,
        "predicted_peak_remaining_warming_c": 0.0,
        "next_curve_delta_from_high_c": 0.0,
        "future_curve_slope_c_per_hour": 0.0,
        "observation_count": 14,
        "complete": True,
    }
    body.update(overrides)
    body["content_sha256"] = content_sha256(
        {key: value for key, value in body.items() if key != "content_sha256"}
    )
    return ConditionalMetarPathState.from_mapping(body)


def _expected(state: ConditionalMetarPathState) -> float:
    distribution = predict_tmax_distribution(
        state,
        remaining_warming_distribution=_PRIOR,
        path_model_coefficients=_COEFFICIENTS,
        path_model_sigma_c=_SIGMA,
    )
    return sum(t * p for t, p in distribution.items())


def test_forecast_reads_no_model_quantity() -> None:
    """The independence claim, enforced rather than documented.

    The coefficient table above prices every model-derived term at 5.0 -- an
    absurd value that would dominate the linear target if any of them were
    read.  Moving the model's own forecast by ten degrees must leave the
    answer bit-identical, because a system whose error tracks the models it is
    meant to check earns its weight from a correlation, not from evidence.
    """

    cold_model = _expected(_state(root_expected_tmax_c=25.0,
                                  root_expected_remaining_warming_c=0.0))
    hot_model = _expected(_state(root_expected_tmax_c=45.0,
                                 root_expected_remaining_warming_c=8.0))
    assert cold_model == pytest.approx(hot_model)

    curved = _expected(_state(
        future_curve_complete=True,
        predicted_peak_remaining_warming_c=9.0,
        minutes_to_predicted_peak=240.0,
        next_curve_delta_from_high_c=9.0,
        future_curve_slope_c_per_hour=9.0,
    ))
    assert curved == pytest.approx(cold_model)


def test_missing_model_curve_is_not_an_error() -> None:
    """The trajectory conditioner refuses without a curve; this must not.

    That refusal is why the physical evidence went unused on exactly the days
    it was needed: no curve, no correction, and P1 fell back to the model's
    own renormalised tail.
    """

    assert _expected(_state(future_curve_complete=False)) > 36.5


def test_prediction_follows_the_observed_day() -> None:
    still_climbing = _expected(_state())
    turned_over = _expected(_state(
        current_temperature_c=35.0,
        drawdown_from_high_c=1.5,
        momentum_c_per_hour=-0.8,
    ))
    day_nearly_done = _expected(_state(
        minutes_remaining_in_settlement_day=60.0,
        momentum_c_per_hour=-1.5,
        drawdown_from_high_c=2.0,
    ))
    overcast_and_wet = _expected(_state(
        cloud_cover_fraction=0.95,
        precipitation_present=True,
        dewpoint_depression_c=0.5,
    ))

    assert still_climbing > turned_over
    assert still_climbing > day_nearly_done
    assert still_climbing > overcast_and_wet
    # Whatever the state, the day cannot end below what it already reached.
    for value in (still_climbing, turned_over, day_nearly_done,
                  overcast_and_wet):
        assert value >= 36.5


def test_support_never_falls_below_the_running_high() -> None:
    distribution = predict_tmax_distribution(
        _state(),
        remaining_warming_distribution=_PRIOR,
        path_model_coefficients=_COEFFICIENTS,
        path_model_sigma_c=_SIGMA,
    )
    assert min(distribution) >= 36.5
    assert sum(distribution.values()) == pytest.approx(1.0)
    quantiles = tmax_quantiles(distribution)
    assert quantiles[0.05] <= quantiles[0.50] <= quantiles[0.95]

    with pytest.raises(ContractError, match="negative_increment"):
        predict_tmax_distribution(
            _state(),
            remaining_warming_distribution={-1.0: 0.5, 1.0: 0.5},
            path_model_coefficients=_COEFFICIENTS,
            path_model_sigma_c=_SIGMA,
        )


def test_members_declare_their_inputs_and_are_content_addressed() -> None:
    body = build_physical_tmax_members(
        _state(),
        station_id="zuuu",
        event_date="2026-08-12",
        remaining_warming_distribution=_PRIOR,
        path_model_coefficients=_COEFFICIENTS,
        path_model_sigma_c=_SIGMA,
        artifact_content_sha256="a" * 64,
    )
    assert body["station_id"] == "ZUUU"
    assert body["model_inputs_used"] == []
    # One observation stream, one forecast.  Publishing the distribution's
    # quantiles as members would hand the spread channel an assertion instead
    # of a measurement, and equal-weighting them shifts the centre besides.
    assert len(body["members"]) == EXPECTED_MEMBER_COUNT == 1
    assert body["members"][0]["member_role"] == "POINT"
    published = float(body["members"][0]["raw_tmax_c"])
    assert published == pytest.approx(_expected(_state()))
    assert body["diagnostic_tmax_quantiles_c"]
    quantile_mean = sum(
        float(value) for value in body["diagnostic_tmax_quantiles_c"].values()
    ) / len(body["diagnostic_tmax_quantiles_c"])
    assert quantile_mean != pytest.approx(published)
    assert body["content_sha256"] == content_sha256(
        {k: v for k, v in body.items() if k != "content_sha256"}
    )
    assert set(_COEFFICIENTS) - set(PHYSICAL_COEFFICIENT_KEYS)
