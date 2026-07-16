from forecast_source_curve_tournament_runtime import evaluate_curve_reliability


def _inputs():
    observations = [
        {"valid_time": "2026-07-11T01:00:00Z", "temperature_c": 25.0},
        {"valid_time": "2026-07-11T02:00:00Z", "temperature_c": 26.0},
        {"valid_time": "2026-07-11T03:00:00Z", "temperature_c": 27.0},
    ]
    forecasts = []
    for hour, observed in ((1, 25.0), (2, 26.0), (3, 27.0)):
        valid = f"2026-07-11T{hour:02d}:00:00Z"
        forecasts.extend(
            [
                {"source_id": "wu", "valid_time": valid, "issued_at": "2026-07-10T20:00:00Z", "temperature_c": observed, "forecast_snapshot_id": f"wu-{hour}"},
                {"source_id": "twc", "valid_time": valid, "issued_at": "2026-07-10T20:00:00Z", "temperature_c": observed + 4.0, "forecast_snapshot_id": f"twc-{hour}"},
                {"source_id": "gfs", "valid_time": valid, "issued_at": "2026-07-10T20:00:00Z", "temperature_c": observed, "forecast_snapshot_id": f"gfs-{hour}"},
            ]
        )
    return observations, forecasts


def test_curve_reweighting_competes_across_families_but_preserves_total_precision():
    observations, forecasts = _inputs()
    report = evaluate_curve_reliability(
        base_weights={"wu": 0.3, "twc": 0.3, "gfs": 0.4},
        provider_families={"wu": "weather_company", "twc": "weather_company", "gfs": "gfs"},
        observations=observations,
        forecasts=forecasts,
    )
    assert report["state"] == "available"
    assert report["effective_weights"]["wu"] > report["effective_weights"]["twc"]
    assert report["family_precision_after"]["weather_company"] < report["family_precision_before"]["weather_company"]
    assert report["family_precision_after"]["gfs"] > report["family_precision_before"]["gfs"]
    assert abs(sum(report["effective_weights"].values()) - 1.0) < 1e-10
    assert report["contract"]["curve_reweighting_changes_total_weak_precision"] is False
    assert report["contract"]["provider_family_dedup_preserved"] is True
    assert report["contract"]["hard_lock_created"] is False
    assert report["contract"]["research_only"] is True
    assert report["contract"]["production_weight_input"] is False
    assert report["contract"]["writes_joint_weak_likelihood"] is False
    assert report["contract"]["writes_bias_state"] is False
    assert report["contract"]["writes_posterior_or_order"] is False
    assert report["contract"]["forecast_source_promoted_to_fact"] is False


def test_future_published_forecast_is_excluded_not_scored_as_error():
    observations, forecasts = _inputs()
    forecasts.append(
        {"source_id": "wu", "valid_time": "2026-07-11T03:00:00Z", "issued_at": "2026-07-11T03:01:00Z", "temperature_c": 27.0}
    )
    report = evaluate_curve_reliability(
        base_weights={"wu": 0.5, "twc": 0.5},
        provider_families={"wu": "weather_company", "twc": "weather_company"},
        observations=observations,
        forecasts=forecasts,
    )
    assert report["prequential_excluded_counts"]["forecast_published_after_observation_valid_time"] == 1
    assert report["source_update_counts"]["wu"] == 3


def test_missing_source_is_coverage_na_and_never_permanently_zero():
    observations, forecasts = _inputs()
    forecasts = [row for row in forecasts if row["source_id"] == "wu"]
    report = evaluate_curve_reliability(
        base_weights={"wu": 0.5, "twc": 0.5},
        provider_families={"wu": "weather_company", "twc": "weather_company"},
        observations=observations,
        forecasts=forecasts,
    )
    assert report["reliability_multipliers"]["twc"] is None
    assert report["source_missing_counts"]["twc"] == 3
    assert abs(report["effective_weights"]["twc"] - 0.5) < 1e-10


def test_same_curve_state_cannot_be_consumed_twice():
    observations, forecasts = _inputs()
    first = evaluate_curve_reliability(
        base_weights={"wu": 0.5, "twc": 0.5},
        provider_families={"wu": "weather_company", "twc": "weather_company"},
        observations=observations,
        forecasts=forecasts,
    )
    second = evaluate_curve_reliability(
        base_weights={"wu": 0.5, "twc": 0.5},
        provider_families={"wu": "weather_company", "twc": "weather_company"},
        observations=observations,
        forecasts=forecasts,
        consumed_state_ids=[first["curve_state_id"]],
    )
    assert second["state"] == "unavailable"
    assert second["state_reason"] == "curve_state_already_consumed"
    assert second["contract"]["amsc_curve_reliability_consumed_once"] is False


def test_unique_model_families_can_win_and_lose_from_amsc_curve_fit():
    observations = [
        {"valid_time": "2026-07-11T01:00:00Z", "temperature_c": 25.0},
        {"valid_time": "2026-07-11T02:00:00Z", "temperature_c": 26.0},
    ]
    forecasts = [
        {"source_id": source, "valid_time": observation["valid_time"], "issued_at": "2026-07-10T20:00:00Z", "temperature_c": observation["temperature_c"] + error}
        for observation in observations
        for source, error in (("ecmwf", 0.0), ("gfs", 4.0), ("icon", 2.0))
    ]
    report = evaluate_curve_reliability(
        base_weights={"ecmwf": 1 / 3, "gfs": 1 / 3, "icon": 1 / 3},
        provider_families={"ecmwf": "ecmwf", "gfs": "gfs", "icon": "icon"},
        observations=observations,
        forecasts=forecasts,
    )

    assert report["effective_weights"]["ecmwf"] > report["effective_weights"]["gfs"]
    assert report["effective_weights"]["gfs"] < 1 / 3
    assert abs(sum(report["effective_weights"].values()) - 1.0) < 1e-10
