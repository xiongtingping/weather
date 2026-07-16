from __future__ import annotations

import math

from sequential_remaining_warming_bayes_runtime import (
    SCHEMA_VERSION,
    build_sequential_remaining_warming_posterior,
)


BUCKETS = ["31°C or below", "32°C", "33°C", "34°C", "35°C", "36°C or higher"]


def _forecast_output(*, model_peak=34.0, taf_peak=None):
    grid = [28.0 + index * 0.1 for index in range(121)]
    peaks = [float(model_peak)] + ([float(taf_peak)] if taf_peak is not None else [])
    raw = [sum(math.exp(-0.5 * ((temperature - peak) / 0.9) ** 2) for peak in peaks) for temperature in grid]
    total = sum(raw)
    return {
        "schema": "joint_forecast_weak_likelihood_v1",
        "ok": True,
        "production_model_ready": True,
        "artifact_sha256": "joint-weak-test-artifact",
        "forecast_evidence_snapshot_id": "joint-weak-test-evidence",
        "weak_source_set": {"source_set_key": "gfs" + ("+taf" if taf_peak is not None else "")},
        "joint_forecast_weak_likelihood": [
            {"temperature_c": temperature, "likelihood": value / total}
            for temperature, value in zip(grid, raw)
        ],
        "forecast_weak_likelihood_map": {bucket: 1.0 / len(BUCKETS) for bucket in BUCKETS},
    }


def _metar_output(*, observed=32.0, current=32.0, hard_lock=False, final=None):
    mask = {bucket: 1.0 for bucket in BUCKETS}
    mask["31°C or below"] = 0.0 if observed is not None and observed >= 32.0 else 1.0
    return {
        "ok": True,
        "hard_constraints": {
            "hard_mask": mask,
            "observed_today_max_c": observed,
            "current_temperature_c": current,
            "hard_lock": hard_lock,
            "official_final_result": final,
            "hard_mask_reason": "observed_today_max_c",
        },
        "observation_features": {
            "expected_peak_window_end_hour_local": 17.5,
        },
        "metar_strong_likelihood_map": {bucket: 1.0 / len(BUCKETS) for bucket in BUCKETS},
        "remaining_warming_by_state": ([{
            "state": "S2_open_boundary_layer",
            "state_prob": 1.0,
            "peak_mu_c": float(observed) + 1.5,
            "sigma_c": 0.9,
            "remaining_hours": 3.0,
        }] if observed is not None else []),
    }


def _quality(value=1.0):
    return {"metar_strong": {"quality": value}}


def _build(**overrides):
    kwargs = {
        "buckets": BUCKETS,
        "prior_distribution": {bucket: 1.0 / len(BUCKETS) for bucket in BUCKETS},
        "forecast_output": _forecast_output(),
        "metar_output": _metar_output(),
        "source_observations": [],
        "hard_mask": _metar_output()["hard_constraints"]["hard_mask"],
        "event_date": "2026-07-10",
        "as_of": "2026-07-10T06:30:00Z",
        "station_timezone": "Asia/Shanghai",
        "rounding_rule": "rounded_degrees",
        "phase": "D0_late",
        "phase_reason": "test",
        "hard_mask_reason": "observed_today_max_c",
        "beta_weak": 0.08,
        "beta_strong": 1.0,
        "information_quality": _quality(),
    }
    kwargs.update(overrides)
    return build_sequential_remaining_warming_posterior(**kwargs)


def test_continuous_sequential_runtime_is_normalized_and_retires_bucket_product():
    result = _build()

    assert result["schema"] == SCHEMA_VERSION
    assert result["ok"] is True
    assert result["activation_mode"] == "live_no_shadow"
    assert result["boundary"]["live_primary_composer"] is True
    assert result["boundary"]["live_no_shadow"] is True
    assert result["boundary"]["shadow_posterior_emitted"] is False
    assert result["boundary"]["legacy_bucket_product_used"] is False
    assert result["boundary"]["continuous_before_bucket_integration"] is True
    assert result["layer_contributions"]["metar_strong_likelihood"]["legacy_static_metar_distribution_used"] is False
    assert math.isclose(sum(result["posterior_temperature_distribution_map"].values()), 1.0, abs_tol=1e-9)
    assert math.isclose(result["normalization"]["continuous_mass_sum"], 1.0, abs_tol=1e-9)
    continuous = result["posterior_continuous_distribution"]
    assert continuous["source"] == SCHEMA_VERSION
    assert continuous["quantile_method"] == "continuous_grid_cdf"
    assert len(continuous["grid"]) > 20
    assert result["posterior_quantiles_c"]["0.5"] is not None


def test_retired_v31_trace_cannot_block_or_change_new_composer():
    metar = _metar_output()
    metar["v31_multimodel_posterior"] = {
        "state": "unavailable",
        "state_reason": "artifact_missing",
        "applied_to_metar_strong_likelihood": False,
    }

    result = _build(metar_output=metar)

    assert result["ok"] is True
    assert result["production_model_ready"] is True
    assert result["production_model_version"] == "joint_weak_physical_strong_composer_v1"
    assert result["production_block_reason"] == ""
    assert result["remaining_warming_source"] == "physical_observation_state_mixture"
    assert "v3_synoptic_posterior" not in result


def test_d1_uses_forecast_error_mixture_and_zero_observation_gate():
    result = _build(
        phase="D1_late",
        as_of="2026-07-09T10:00:00Z",
        metar_output=_metar_output(observed=None, current=None),
        hard_mask={bucket: 1.0 for bucket in BUCKETS},
        forecast_output=_forecast_output(model_peak=33.0, taf_peak=35.0),
        beta_weak=0.85,
        beta_strong=0.0,
        information_quality={"metar_strong": {"quality": 0.0}},
    )

    assert result["observation_information_gate"]["observation_weight"] == 0.0
    assert result["observation_information_gate"]["forecast_climate_weight"] == 1.0
    components = result["forecast_error_components"]
    assert components[0]["model"] == "joint_forecast_weak_likelihood_v1"
    assert result["posterior_temperature_distribution_map"]["33°C"] > 0.0
    assert result["posterior_temperature_distribution_map"]["35°C"] > 0.0


def test_midnight_max_is_pre_dawn_carryover_lower_bound_not_daytime_peak_platform():
    sources = [
        {
            "source_event_id": "midnight_carryover",
            "source_observation_id": "midnight_carryover_obs",
            "source_id": "zuuu_metar_raw_amsc",
            "source_class": "hard_observation_candidate",
            "event_date": "2026-07-11",
            "valid_time": "2026-07-10T16:00:00Z",
            "fetched_at": "2026-07-10T16:01:00Z",
            "temperature_c": 29.0,
        },
        {
            "source_event_id": "one_am_cooling",
            "source_observation_id": "one_am_cooling_obs",
            "source_id": "zuuu_metar_raw_amsc",
            "source_class": "hard_observation_candidate",
            "event_date": "2026-07-11",
            "valid_time": "2026-07-10T17:00:00Z",
            "fetched_at": "2026-07-10T17:01:00Z",
            "temperature_c": 28.0,
        },
    ]
    result = _build(
        event_date="2026-07-11",
        as_of="2026-07-10T17:01:00Z",
        source_observations=sources,
        metar_output=_metar_output(observed=29.0, current=28.0),
    )

    platform = result["observed_peak_platform"]
    assert platform["status"] == "provisional_pre_dawn_carryover_lower_bound"
    assert platform["settlement_calendar_day_lower_bound"] is True
    assert platform["pre_dawn_carryover"] is True
    assert platform["is_daytime_peak_platform"] is False
    assert platform["peak_origin"] == "previous_solar_day_thermal_carryover_after_midnight"
    time_rows = result["predicted_peak_time_posterior"]["distribution"]
    already = next(row for row in time_rows if row["time_state"].startswith("already_observed"))
    assert already["time_state"] == "already_observed_pre_dawn_carryover_lower_bound"
    assert "not_probability_daytime_peak_has_formed" in result["predicted_peak_time_posterior"]["peak_already_probability_semantics"]


def test_metar_points_are_latest_as_of_and_distinct_valid_time_updates_once():
    sources = [
        {
            "source_event_id": "awc_same_time",
            "source_id": "zuuu_metar_raw_awc",
            "source_class": "hard_observation_candidate",
            "event_date": "2026-07-10",
            "valid_time": "2026-07-10T05:00:00+00:00",
            "fetched_at": "2026-07-10T05:01:00Z",
            "temperature_c": 31.0,
        },
        {
            "source_event_id": "amsc_same_time",
            "source_id": "zuuu_metar_raw_amsc",
            "source_class": "hard_observation_candidate",
            "event_date": "2026-07-10",
            "valid_time": "2026-07-10T05:00:00Z",
            "fetched_at": "2026-07-10T05:02:00Z",
            "temperature_c": 32.0,
        },
        {
            "source_event_id": "amsc_next",
            "source_id": "zuuu_metar_raw_amsc",
            "source_class": "hard_observation_candidate",
            "event_date": "2026-07-10",
            "valid_time": "2026-07-10T05:30:00Z",
            "fetched_at": "2026-07-10T05:31:00Z",
            "temperature_c": 32.0,
            "payload": {"cloud_text": "CAVOK"},
        },
        {
            "source_event_id": "future",
            "source_id": "zuuu_metar_raw_amsc",
            "source_class": "hard_observation_candidate",
            "event_date": "2026-07-10",
            "valid_time": "2026-07-10T07:00:00Z",
            "fetched_at": "2026-07-10T07:01:00Z",
            "temperature_c": 33.0,
        },
    ]
    result = _build(source_observations=sources)
    snapshot = result["canonical_metar_snapshot"]

    assert snapshot["distinct_valid_time_count"] == 2
    assert [row["source_event_id"] for row in snapshot["points"]] == ["amsc_same_time", "amsc_next"]
    assert result["sequential_state_filter"]["point_count"] == 2
    assert result["sequential_state_filter"]["boundary"]["distinct_valid_times_update_once"] is True
    assert any(row["reason"] == "future_valid_time" for row in snapshot["excluded"])


def test_observed_max_hard_floor_and_zero_inflated_plateau_keep_impossible_buckets_zero():
    sources = [
        {
            "source_event_id": f"plateau_{index}",
            "source_id": "zuuu_metar_raw_amsc",
            "source_class": "hard_observation_candidate",
            "event_date": "2026-07-10",
            "valid_time": f"2026-07-10T0{4 + index}:00:00Z",
            "fetched_at": f"2026-07-10T0{4 + index}:01:00Z",
            "temperature_c": 32.0,
            "observed_today_max_c": 32.0,
            "payload": {"cloud_text": "CAVOK"},
        }
        for index in range(3)
    ]
    result = _build(source_observations=sources)
    posterior = result["posterior_temperature_distribution_map"]

    assert posterior["31°C or below"] == 0.0
    assert all(
        row["mass"] == 0.0
        for row in result["posterior_continuous_distribution"]["grid"]
        if row["temp_c"] < 31.5
    )
    assert result["hard_constraints"]["hard_constraint_violation_count"] == 0
    assert result["remaining_warming_mixture"]["zero_inflated"] is False
    assert result["remaining_warming_mixture"]["state_components"][0]["state"] == "S2_open_boundary_layer"
    assert result["sequential_state_filter"]["boundary"]["integer_plateau_is_interval_observation"] is True


def test_hard_lock_collapses_continuous_mass_and_bucket_probability():
    metar = _metar_output(observed=33.0, current=32.0, hard_lock=True, final=33.0)
    hard_mask = {bucket: (1.0 if bucket == "33°C" else 0.0) for bucket in BUCKETS}
    result = _build(metar_output=metar, hard_mask=hard_mask)

    assert result["observation_information_gate"]["observation_weight"] == 1.0
    assert result["posterior_temperature_distribution_map"]["33°C"] == 1.0
    assert sum(value for key, value in result["posterior_temperature_distribution_map"].items() if key != "33°C") == 0.0
    assert result["hard_constraints"]["hard_lock"] is True


def test_sequential_state_consumes_full_metar_evidence_and_keeps_trend_weak():
    sources = [
        {
            "source_event_id": "before_convection",
            "source_id": "zuuu_metar_raw_amsc",
            "source_class": "hard_observation_candidate",
            "event_date": "2026-07-10",
            "valid_time": "2026-07-10T05:00:00Z",
            "fetched_at": "2026-07-10T05:01:00Z",
            "temperature_c": 35.0,
            "dewpoint_c": 24.0,
            "payload": {
                "visibility_m": 9999.0,
                "cloud_text": "FEW040",
                "weather_text": "",
                "cloud_cover_pct": 15.0,
                "low_cloud_cover_pct": 5.0,
                "wind_speed_mps": 2.0,
                "wind_dir_deg": 180.0,
                "pressure_hpa": 998.0,
            },
        },
        {
            "source_event_id": "convection",
            "source_id": "zuuu_metar_raw_amsc",
            "source_class": "hard_observation_candidate",
            "event_date": "2026-07-10",
            "valid_time": "2026-07-10T05:30:00Z",
            "fetched_at": "2026-07-10T05:31:00Z",
            "temperature_c": 33.0,
            "dewpoint_c": 25.0,
            "payload": {
                "visibility_m": 6000.0,
                "cloud_text": "BKN030CB",
                "weather_text": "TSRA",
                "metar_trend_text": "BECMG TL0700 FEW040",
                "metar_trend_cloud_text": "FEW040",
                "metar_trend_weather_text": "",
                "cloud_cover_pct": 75.0,
                "low_cloud_cover_pct": 75.0,
                "wind_speed_mps": 7.0,
                "wind_dir_deg": 40.0,
                "wind_gust_mps": 8.0,
                "wind_variable": True,
                "wind_variable_from_deg": 330.0,
                "wind_variable_to_deg": 60.0,
                "pressure_hpa": 1000.0,
            },
        },
    ]

    result = _build(source_observations=sources)
    trace = result["sequential_state_filter"]["trace"]
    evidence = trace[-1]["evidence_inputs"]

    assert evidence["availability"] == {
        "temperature": True,
        "dewpoint": True,
        "cloud": True,
        "current_weather": True,
        "metar_trend": True,
        "visibility": True,
        "wind_speed": True,
        "wind_direction": True,
        "wind_gust": True,
        "pressure": True,
    }
    assert evidence["convective"] == 1.0
    assert evidence["precip"] == 1.0
    assert evidence["trend_convective_weak"] == 0.0
    assert evidence["visibility_poor"] == 1.0
    assert evidence["outflow_wind"] == 1.0
    assert evidence["pressure_rising"] > 0.0
    assert evidence["cloud_growth"] > 0.0
    assert result["sequential_state_filter"]["dominant_state"] == "convective_cutoff"
    assert result["sequential_state_filter"]["boundary"]["current_observation_separated_from_metar_trend"] is True


def test_metar_trend_thunderstorm_is_weak_context_not_current_precipitation():
    sources = [
        {
            "source_event_id": "trend_only",
            "source_id": "zuuu_metar_raw_amsc",
            "source_class": "hard_observation_candidate",
            "event_date": "2026-07-10",
            "valid_time": "2026-07-10T06:00:00Z",
            "fetched_at": "2026-07-10T06:01:00Z",
            "temperature_c": 32.0,
            "dewpoint_c": 24.0,
            "payload": {
                "visibility_m": 9999.0,
                "cloud_text": "FEW040",
                "weather_text": "",
                "metar_trend_text": "BECMG TL0730 -TSRA FEW030CB",
                "metar_trend_cloud_text": "FEW030CB",
                "metar_trend_weather_text": "TSRA",
                "wind_speed_mps": 3.0,
                "pressure_hpa": 998.0,
            },
        }
    ]

    result = _build(source_observations=sources)
    evidence = result["sequential_state_filter"]["trace"][-1]["evidence_inputs"]

    assert evidence["precip"] == 0.0
    assert evidence["convective"] == 0.0
    assert evidence["trend_convective_weak"] == 1.0


def test_forecast_peak_window_does_not_cut_off_late_remaining_warming_support():
    result = _build(as_of="2026-07-10T12:00:00Z")  # 20:00 Asia/Shanghai

    mixture = result["remaining_warming_mixture"]
    assert mixture["forecast_peak_window_end_hour_local"] == 17.5
    assert mixture["observation_horizon_end_hour_local"] == 23.5
    assert mixture["forecast_peak_window_is_weak_context_not_support_cutoff"] is True
    assert len(mixture["state_components"]) == 1
    assert mixture["state_components"][0]["state"] == "S2_open_boundary_layer"
    assert 0.0 < mixture["peak_already_probability"] < 1.0
    peak_time = result["predicted_peak_time_posterior"]
    assert peak_time["predicted_not_observed"] is True
    assert peak_time["cannot_finalize_daily_peak"] is True
    assert peak_time["peak_already_probability"] == mixture["peak_already_probability"]
    assert math.isclose(sum(row["probability"] for row in peak_time["distribution"]), 1.0, abs_tol=1e-9)
    assert any(
        row.get("time_state") == "future_half_hour_slot" and row.get("hour_local", 0.0) > 20.0
        for row in peak_time["distribution"]
    )


def test_observed_peak_platform_uses_complete_canonical_day_and_overrides_stale_bundle_max():
    sources = [
        {
            "source_event_id": "before_peak",
            "source_observation_id": "obs_before",
            "source_id": "zuuu_metar_raw_amsc",
            "source_class": "hard_observation_candidate",
            "event_date": "2026-07-10",
            "valid_time": "2026-07-10T04:00:00Z",
            "fetched_at": "2026-07-10T04:01:00Z",
            "temperature_c": 35.0,
        },
        {
            "source_event_id": "peak_first",
            "source_observation_id": "obs_peak_first",
            "source_id": "zuuu_metar_raw_amsc",
            "source_class": "hard_observation_candidate",
            "event_date": "2026-07-10",
            "valid_time": "2026-07-10T04:30:00Z",
            "fetched_at": "2026-07-10T04:31:00Z",
            "temperature_c": 36.0,
        },
        {
            "source_event_id": "peak_last",
            "source_observation_id": "obs_peak_last",
            "source_id": "zuuu_metar_raw_amsc",
            "source_class": "hard_observation_candidate",
            "event_date": "2026-07-10",
            "valid_time": "2026-07-10T05:00:00Z",
            "fetched_at": "2026-07-10T05:01:00Z",
            "temperature_c": 36.0,
        },
        {
            "source_event_id": "after_peak",
            "source_observation_id": "obs_after",
            "source_id": "zuuu_metar_raw_amsc",
            "source_class": "hard_observation_candidate",
            "event_date": "2026-07-10",
            "valid_time": "2026-07-10T05:30:00Z",
            "fetched_at": "2026-07-10T05:31:00Z",
            "temperature_c": 33.0,
        },
    ]
    result = _build(
        source_observations=sources,
        metar_output=_metar_output(observed=33.0, current=33.0),
        as_of="2026-07-10T06:00:00Z",
    )

    platform = result["observed_peak_platform"]
    assert result["hard_constraints"]["observed_today_max_c"] == 36.0
    assert platform["status"] == "provisional"
    assert platform["observed_peak_to_date_c"] == 36.0
    assert platform["observed_peak_first_time_local"] == "2026-07-10T12:30:00+08:00"
    assert platform["observed_peak_last_time_local"] == "2026-07-10T13:00:00+08:00"
    assert platform["observed_peak_report_count"] == 2
    assert platform["source_observation_ids"] == ["obs_peak_first", "obs_peak_last"]
    assert result["predicted_peak_time_posterior"]["live_update_source"] == (
        "sequential_metar_state_filter_all_physical_channels"
    )


def test_predicted_peak_time_is_explicit_weak_prior_not_observed_peak():
    metar = _metar_output()
    metar["observation_features"].update(
        {
            "expected_peak_hour_local": 15.0,
            "expected_peak_window_start_hour_local": 14.0,
            "expected_peak_window_end_hour_local": 17.0,
            "expected_peak_window_source": "taf_weak_prediction",
        }
    )
    result = _build(metar_output=metar)
    predicted = result["predicted_peak_time_prior"]

    assert predicted["role"] == "weak_forecast_timing_prior"
    assert predicted["predicted_not_observed"] is True
    assert predicted["cannot_finalize_daily_peak"] is True
    assert predicted["mode_hour_local"] == 15.0
    assert math.isclose(sum(row["probability"] for row in predicted["distribution"]), 1.0, abs_tol=1e-9)
