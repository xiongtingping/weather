import math

from metar_strong_likelihood_runtime import (
    CONTRACT_SCHEMA_VERSION,
    SCHEMA_VERSION,
    build_metar_strong_likelihood,
    build_metar_strong_likelihood_contract,
)


BUCKETS = ["29", "30", "31", "32", "33"]


def _prob(result, bucket):
    return result["metar_strong_likelihood_map"][bucket]


def test_open_boundary_layer_with_momentum_supports_high_tail():
    result = build_metar_strong_likelihood(
        buckets=BUCKETS,
        as_of="2026-07-03T12:00:00+08:00",
        observation_inputs={
            "current_temp_c": 28.0,
            "observed_today_max_c": 28.0,
            "dewpoint_c": 22.0,
            "visibility_m": 9999,
            "cloud_text": "FEW011 SCT020",
            "dewpoint_trend_c_per_hour": -1.0,
            "momentum_rate_c_per_hour": 2.0,
            "weather_text": "",
        },
    )

    assert result["schema"] == SCHEMA_VERSION
    assert result["read_only"] is True
    assert result["production_entry_enabled"] is False
    assert result["guards"]["writes_archive_db"] is False
    assert math.isclose(result["normalization"]["state_prob_sum"], 1.0, abs_tol=1e-12)
    assert math.isclose(result["normalization"]["metar_strong_likelihood_sum"], 1.0, abs_tol=1e-12)
    assert result["state_probs"]["S2_open_boundary_layer"] > result["state_probs"]["S0_wet_layer_closed"]
    assert _prob(result, "32") + _prob(result, "33") > _prob(result, "29") + _prob(result, "30")


def test_physical_x_observation_mode_excludes_legacy_probability_overlay():
    result = build_metar_strong_likelihood(
        buckets=BUCKETS,
        as_of="2026-07-03T12:00:00+08:00",
        observation_inputs={
            "current_temp_c": 28.0,
            "observed_today_max_c": 28.0,
            "dewpoint_c": 22.0,
            "visibility_m": 9999,
            "cloud_text": "FEW011 SCT020",
            "dewpoint_trend_c_per_hour": -1.0,
            "momentum_rate_c_per_hour": 2.0,
        },
        allow_legacy_v31_probability_overlay=False,
    )

    assert result["v31_multimodel_posterior"]["state"] == "retired_not_consumed"
    assert result["v31_multimodel_posterior"]["applied_to_metar_strong_likelihood"] is False
    assert result["boundary"]["physical_x_observation_likelihood_excludes_legacy_v31"] is True
    assert _prob(result, "33") > _prob(result, "29")


def test_metar_strong_likelihood_contract_accepts_single_strong_likelihood_channel():
    result = build_metar_strong_likelihood(
        buckets=BUCKETS,
        as_of="2026-07-03T12:00:00+08:00",
        observation_inputs={
            "current_temp_c": 28.0,
            "observed_today_max_c": 28.0,
            "dewpoint_c": 22.0,
            "visibility_m": 9999,
            "cloud_text": "FEW011 SCT020",
            "dewpoint_trend_c_per_hour": -1.0,
            "temperature_momentum_1h_c_per_hour": 1.4,
            "temperature_momentum_30m_c_per_hour": 1.1,
        },
    )

    contract = build_metar_strong_likelihood_contract(result)

    assert contract["schema"] == CONTRACT_SCHEMA_VERSION
    assert contract["state"] == "ready"
    assert contract["issues"] == []
    assert contract["base_observation_present"] is True
    assert contract["remaining_warming_state_count"] == 4
    assert contract["boundary"]["metar_internal_fields_not_independent_likelihoods"] is True


def test_multi_window_temperature_trajectory_is_consumed_once_inside_strong_likelihood():
    result = build_metar_strong_likelihood(
        buckets=BUCKETS,
        as_of="2026-07-03T12:00:00+08:00",
        observation_inputs={
            "current_temp_c": 28.0,
            "observed_today_max_c": 28.0,
            "dewpoint_c": 22.0,
            "visibility_m": 9999,
            "cloud_text": "FEW011 SCT020",
            "dewpoint_trend_c_per_hour": -1.0,
            "temperature_momentum_1h_c_per_hour": 1.4,
            "temperature_momentum_90m_c_per_hour": 1.3,
            "temperature_momentum_30m_c_per_hour": 1.1,
            "temperature_momentum_15m_c_per_hour": 1.0,
            "weather_text": "",
        },
    )

    trajectory = result["temperature_trajectory"]
    assert trajectory["schema"] == "temperature_trajectory_summary_v1"
    assert trajectory["state"] in {"sustained_warming", "emerging_warming"}
    assert trajectory["gate_state"] == "confirmed"
    assert set(trajectory["confirmed_windows"]) >= {"30m", "1h", "90m"}
    assert trajectory["adjusted_rate_c_per_hour"] > 0.5
    assert result["observation_features"]["temperature_trajectory_state"] == trajectory["state"]
    assert result["boundary"]["temperature_trajectory_consumed_once_inside_strong_likelihood"] is True
    assert result["dedupe_trace"][1]["policy"] == (
        "single_internal_metar_strong_likelihood_feature_not_independent_same_level_likelihood"
    )


def test_short_window_only_temperature_momentum_does_not_overconfirm_high_tail():
    short_only = build_metar_strong_likelihood(
        buckets=BUCKETS,
        as_of="2026-07-03T12:00:00+08:00",
        observation_inputs={
            "current_temp_c": 28.0,
            "observed_today_max_c": 28.0,
            "dewpoint_c": 22.0,
            "visibility_m": 9999,
            "cloud_text": "FEW011 SCT020",
            "dewpoint_trend_c_per_hour": -1.0,
            "temperature_momentum_5m_c_per_hour": 2.4,
            "temperature_momentum_3m_c_per_hour": 2.8,
        },
    )
    confirmed = build_metar_strong_likelihood(
        buckets=BUCKETS,
        as_of="2026-07-03T12:00:00+08:00",
        observation_inputs={
            "current_temp_c": 28.0,
            "observed_today_max_c": 28.0,
            "dewpoint_c": 22.0,
            "visibility_m": 9999,
            "cloud_text": "FEW011 SCT020",
            "dewpoint_trend_c_per_hour": -1.0,
            "temperature_momentum_1h_c_per_hour": 1.4,
            "temperature_momentum_30m_c_per_hour": 1.1,
        },
    )

    assert short_only["temperature_trajectory"]["state"] == "short_window_only"
    assert short_only["temperature_trajectory"]["gate_state"] == "short_window_watch"
    assert "temperature_momentum_short_window_only_not_posterior_confirmed" in short_only["na_reasons"]
    assert _prob(confirmed, "33") > _prob(short_only, "33")


def test_rolling_only_temperature_trajectory_without_multi_window_confirmation_is_capped():
    result = build_metar_strong_likelihood(
        buckets=["36°C", "37°C", "38°C", "39°C", "40°C", "41°C or higher"],
        as_of="2026-07-09T11:30:00+08:00",
        observation_inputs={
            "current_temp_c": 36.0,
            "observed_today_max_c": 36.0,
            "dewpoint_c": 23.0,
            "visibility_m": 9999,
            "cloud_text": "CAVOK",
            "expected_peak_window_end_hour_local": 17.0,
            "temperature_observations": [
                {
                    "temperature_c": 33.0,
                    "dewpoint_c": 24.0,
                    "valid_time": "2026-07-09T01:30:00+00:00",
                    "source_id": "zuuu_metar_raw_amsc",
                },
                {
                    "temperature_c": 33.0,
                    "dewpoint_c": 24.0,
                    "valid_time": "2026-07-09T02:00:00+00:00",
                    "source_id": "zuuu_metar_raw_amsc",
                },
                {
                    "temperature_c": 34.0,
                    "dewpoint_c": 23.0,
                    "valid_time": "2026-07-09T02:30:00+00:00",
                    "source_id": "zuuu_metar_raw_amsc",
                },
                {
                    "temperature_c": 34.0,
                    "dewpoint_c": 23.0,
                    "valid_time": "2026-07-09T03:00:00+00:00",
                    "source_id": "zuuu_metar_raw_amsc",
                },
                {
                    "temperature_c": 36.0,
                    "dewpoint_c": 23.0,
                    "valid_time": "2026-07-09T03:30:00+00:00",
                    "source_id": "zuuu_metar_raw_amsc",
                },
            ],
        },
    )

    trajectory = result["temperature_trajectory"]
    rolling = trajectory["rolling_temperature_observation_summary"]
    assert rolling["state"] == "sustained_warming"
    assert rolling["gate_state"] == "confirmed"
    assert rolling["confidence"] == 0.49
    assert trajectory["state"] == "single_rate_unconfirmed"
    assert trajectory["gate_state"] == "unconfirmed"
    assert trajectory["confirmed_windows"] == []
    assert trajectory["confidence"] == 0.28
    assert trajectory["response_scale"] == 0.24
    assert trajectory["effective_rate_c_per_hour"] == 1.25
    assert trajectory["adjusted_rate_c_per_hour"] == 0.3
    assert "temperature_momentum_single_rate_without_multi_window_confirmation" in result["na_reasons"]
    s2 = [row for row in result["remaining_warming_by_state"] if row["state"] == "S2_open_boundary_layer"][0]
    assert s2["temperature_trajectory_state"] == "single_rate_unconfirmed"
    assert s2["trajectory_adjusted_rate_c_per_hour"] == 0.3


def test_midday_long_window_momentum_without_recent_confirmation_caps_high_tail():
    result = build_metar_strong_likelihood(
        buckets=["35°C", "36°C", "37°C", "38°C", "39°C", "40°C", "41°C", "42°C"],
        as_of="2026-07-07T13:00:00+08:00",
        observation_inputs={
            "current_temp_c": 35.0,
            "observed_today_max_c": 35.0,
            "dewpoint_c": 22.0,
            "visibility_m": 9999,
            "cloud_text": "FEW020",
            "dewpoint_trend_c_per_hour": -0.4,
            "temperature_momentum_6h_c_per_hour": 2.0,
            "temperature_momentum_3h_c_per_hour": 2.0,
            "temperature_momentum_2h_c_per_hour": 2.0,
            "temperature_momentum_1h_c_per_hour": 2.0,
        },
    )

    trajectory = result["temperature_trajectory"]
    assert trajectory["state"] == "midday_recent_confirmation_missing"
    assert trajectory["gate_state"] == "recent_window_watch"
    assert trajectory["gate_reason"] == "peak_window_requires_30m_recent_confirmation"
    assert trajectory["recent_confirmed_windows"] == []
    assert "temperature_momentum_midday_recent_window_confirmation_missing" in result["na_reasons"]
    assert result["remaining_warming_by_state"][2]["temperature_trajectory_state"] == "midday_recent_confirmation_missing"
    assert result["remaining_warming_by_state"][2]["delta_mu_c"] < 2.5
    assert _prob(result, "40°C") + _prob(result, "41°C") + _prob(result, "42°C") < _prob(result, "37°C") + _prob(result, "38°C")


def test_midday_recent_confirmation_guard_starts_ninety_minutes_before_peak_window():
    result = build_metar_strong_likelihood(
        buckets=["36°C", "37°C", "38°C", "39°C", "40°C", "41°C or higher"],
        as_of="2026-07-09T12:30:00+08:00",
        observation_inputs={
            "current_temp_c": 37.0,
            "observed_today_max_c": 37.0,
            "dewpoint_c": 23.0,
            "visibility_m": 9999,
            "cloud_text": "FEW033",
            "expected_peak_window_start_hour_local": 14.0,
            "expected_peak_window_end_hour_local": 17.0,
            "temperature_momentum_6h_c_per_hour": 1.3333333333,
            "temperature_momentum_3h_c_per_hour": 1.0,
            "temperature_momentum_2h_c_per_hour": 1.5,
            "temperature_momentum_1h_c_per_hour": 2.0,
        },
    )

    trajectory = result["temperature_trajectory"]
    assert trajectory["local_hour"] == 12.5
    assert trajectory["state"] == "midday_recent_confirmation_missing"
    assert trajectory["gate_state"] == "recent_window_watch"
    assert trajectory["recent_confirmed_windows"] == []
    assert trajectory["response_scale"] == 0.12
    assert trajectory["adjusted_rate_c_per_hour"] < 0.25
    assert result["metar_strong_likelihood_map"]["41°C or higher"] < 0.2


def test_wet_layer_closed_compresses_remaining_warming():
    result = build_metar_strong_likelihood(
        buckets=BUCKETS,
        as_of="2026-07-03T10:00:00+08:00",
        observation_inputs={
            "current_temp_c": 25.0,
            "observed_today_max_c": 25.0,
            "dewpoint_c": 23.5,
            "visibility_m": 6000,
            "cloud_text": "BKN008 OVC012",
            "weather_text": "BR",
            "dewpoint_trend_c_per_hour": 0.2,
            "momentum_rate_c_per_hour": 0.0,
        },
    )

    assert result["state_probs"]["S0_wet_layer_closed"] > result["state_probs"]["S2_open_boundary_layer"]
    assert result["hard_constraints"]["observed_today_max_c"] == 25.0
    assert result["boundary"]["metar_internal_fields_not_independent_likelihoods"] is True


def test_convective_cutoff_raises_s3_and_limits_high_buckets():
    result = build_metar_strong_likelihood(
        buckets=BUCKETS,
        as_of="2026-07-03T14:30:00+08:00",
        observation_inputs={
            "current_temp_c": 30.0,
            "observed_today_max_c": 30.0,
            "dewpoint_c": 24.0,
            "visibility_m": 7000,
            "cloud_text": "BKN020CB OVC030",
            "weather_text": "TSRA",
            "dewpoint_trend_c_per_hour": 0.8,
            "momentum_rate_c_per_hour": -1.0,
        },
    )

    assert result["state_probs"]["S3_convective_cutoff"] > 0.5
    assert _prob(result, "33") < _prob(result, "30") + _prob(result, "31")
    s3 = [row for row in result["remaining_warming_by_state"] if row["state"] == "S3_convective_cutoff"][0]
    assert s3["delta_mu_c"] < 0.5
    termination = result["termination_risk"]
    assert termination["schema"] == "strong_likelihood_termination_risk_v1"
    assert termination["probability_no_material_new_daily_high"] is not None
    assert termination["probability_no_material_new_daily_high"] > 0.5
    assert termination["derived_from_same_remaining_distribution_not_second_likelihood"] is True
    assert result["recoil_state_ssot"]["termination_risk"] == termination


def test_speci_is_a_same_grade_strong_observation_and_accelerates_termination_risk():
    result = build_metar_strong_likelihood(
        buckets=BUCKETS,
        event_date="2026-07-03",
        as_of="2026-07-03T06:31:00Z",
        source_observations=[
            {
                "source_event_id": "srcevt_regular_before",
                "source_observation_id": "srcobs_regular_before",
                "source_id": "zuuu_metar_raw_amsc",
                "source_class": "hard_observation_candidate",
                "role": "metar_hard_observation_primary",
                "event_date": "2026-07-03",
                "valid_time": "2026-07-03T06:00:00Z",
                "fetched_at": "2026-07-03T06:01:00Z",
                "temperature_c": 34.0,
                "payload": {"report_type": "METAR", "cloud_text": "FEW030", "visibility_m": 9999},
            },
            {
                "source_event_id": "srcevt_speci_cutoff",
                "source_observation_id": "srcobs_speci_cutoff",
                "source_id": "zuuu_metar_raw_amsc",
                "source_class": "hard_observation_candidate",
                "role": "metar_hard_observation_primary",
                "event_date": "2026-07-03",
                "valid_time": "2026-07-03T06:30:00Z",
                "fetched_at": "2026-07-03T06:31:00Z",
                "temperature_c": 32.0,
                "payload": {
                    "report_type": "SPECI", "weather_text": "TSRA", "cloud_text": "BKN020CB",
                    "visibility_m": 6000, "pressure_hpa": 1001.0, "wind_speed_mps": 8.0,
                },
            },
        ],
    )
    termination = result["termination_risk"]
    assert termination["speci_accelerated"] is True
    assert set(termination["included_report_types"]) == {"METAR", "SPECI"}
    assert result["physical_cutoff_evidence"]["convective_or_rain_cutoff"] is True


def test_late_platform_temperature_trajectory_compresses_remaining_warming():
    result = build_metar_strong_likelihood(
        buckets=BUCKETS,
        as_of="2026-07-03T14:30:00+08:00",
        observation_inputs={
            "current_temp_c": 30.0,
            "observed_today_max_c": 30.0,
            "dewpoint_c": 22.0,
            "visibility_m": 9999,
            "cloud_text": "FEW020",
            "temperature_momentum_1h_c_per_hour": 0.05,
            "temperature_momentum_30m_c_per_hour": 0.03,
        },
    )

    assert result["temperature_trajectory"]["state"] == "platform"
    s2 = [row for row in result["remaining_warming_by_state"] if row["state"] == "S2_open_boundary_layer"][0]
    assert s2["temperature_trajectory_state"] == "platform"
    assert s2["trajectory_adjusted_rate_c_per_hour"] == 0.0
    assert s2["delta_mu_c"] < 1.0


def test_pre_peak_integer_plateau_keeps_residual_warming_without_temperature_value_hardcode():
    plateau_temp_c = 29.0
    prior_temp_c = plateau_temp_c - 1.0
    result = build_metar_strong_likelihood(
        buckets=[
            f"{int(plateau_temp_c)}°C",
            f"{int(plateau_temp_c + 1)}°C",
            f"{int(plateau_temp_c + 2)}°C",
            f"{int(plateau_temp_c + 3)}°C",
        ],
        as_of="2026-07-09T13:30:00+08:00",
        observation_inputs={
            "current_temp_c": plateau_temp_c,
            "observed_today_max_c": plateau_temp_c,
            "dewpoint_c": plateau_temp_c - 16.0,
            "visibility_m": 9999,
            "cloud_text": "FEW033",
            "expected_peak_window_start_hour_local": 14.0,
            "expected_peak_window_end_hour_local": 17.0,
            "temperature_observations": [
                {
                    "temperature_c": prior_temp_c,
                    "valid_time": "2026-07-09T03:30:00Z",
                    "source_id": "zuuu_metar_raw_amsc",
                },
                {
                    "temperature_c": prior_temp_c,
                    "valid_time": "2026-07-09T04:00:00Z",
                    "source_id": "zuuu_metar_raw_amsc",
                },
                {
                    "temperature_c": plateau_temp_c,
                    "valid_time": "2026-07-09T04:30:00Z",
                    "source_id": "zuuu_metar_raw_amsc",
                },
                {
                    "temperature_c": plateau_temp_c,
                    "valid_time": "2026-07-09T05:00:00Z",
                    "source_id": "zuuu_metar_raw_amsc",
                },
                {
                    "temperature_c": plateau_temp_c,
                    "valid_time": "2026-07-09T05:30:00Z",
                    "source_id": "zuuu_metar_raw_amsc",
                },
            ],
            "temperature_momentum_30m_c_per_hour": 0.0,
            "temperature_momentum_1h_c_per_hour": 0.0,
            "temperature_momentum_90m_c_per_hour": 0.666667,
            "temperature_momentum_2h_c_per_hour": 0.5,
            "temperature_momentum_150m_c_per_hour": 1.2,
            "temperature_momentum_3h_c_per_hour": 1.0,
            "temperature_momentum_210m_c_per_hour": 1.142857,
            "temperature_momentum_4h_c_per_hour": 0.75,
            "temperature_momentum_270m_c_per_hour": 0.888889,
            "temperature_momentum_5h_c_per_hour": 1.2,
            "temperature_momentum_330m_c_per_hour": 1.090909,
            "temperature_momentum_6h_c_per_hour": 1.333333,
        },
    )

    trajectory = result["temperature_trajectory"]
    rolling = trajectory["rolling_temperature_observation_summary"]
    assert rolling["state"] == "platform"
    assert rolling["rolling_slope_c_per_hour"] == 0.0
    assert trajectory["state"] == "pre_peak_integer_plateau"
    assert trajectory["gate_state"] == "platform_watch"
    assert trajectory["gate_reason"] == "integer_metar_plateau_before_peak_window_end"
    assert trajectory["response_scale"] == 0.18
    assert trajectory["adjusted_rate_c_per_hour"] > 0.0
    assert "integer_metar_pre_peak_plateau_without_30m_recent_confirmation" in result["na_reasons"]
    s2 = [row for row in result["remaining_warming_by_state"] if row["state"] == "S2_open_boundary_layer"][0]
    assert s2["temperature_trajectory_state"] == "pre_peak_integer_plateau"
    assert s2["trajectory_adjusted_rate_c_per_hour"] == trajectory["adjusted_rate_c_per_hour"]
    assert s2["delta_mu_c"] > 1.0


def test_observed_max_masks_impossible_lower_buckets_once():
    result = build_metar_strong_likelihood(
        buckets=BUCKETS,
        as_of="2026-07-03T15:00:00+08:00",
        observation_inputs={
            "current_temp_c": 31.0,
            "observed_today_max_c": 31.0,
            "dewpoint_c": 22.0,
            "visibility_m": 9999,
            "cloud_text": "FEW020",
            "momentum_rate_c_per_hour": 0.3,
        },
    )

    assert result["hard_constraints"]["masked_zero_buckets"] == ["29", "30"]
    assert result["hard_constraints"]["hard_mask"]["29"] == 0.0
    assert result["hard_constraints"]["hard_mask"]["30"] == 0.0
    assert _prob(result, "29") == 0.0
    assert _prob(result, "30") == 0.0
    assert "observed_today_max_c" in result["hard_constraints"]["hard_mask_reason"]


def test_only_hard_observation_sources_are_included():
    result = build_metar_strong_likelihood(
        buckets=BUCKETS,
        as_of="2026-07-03T12:00:00+08:00",
        source_observations=[
            {
                "source_event_id": "srcevt_awc",
                "source_id": "awc_cache_metars_csv_gz",
                "source_class": "hard_observation_candidate",
                "temperature_c": 28.0,
                "dewpoint_c": 22.0,
                "observed_today_max_c": 28.0,
                "payload": {
                    "visibility_m": 9999,
                    "cloud_text": "FEW020",
                    "momentum_rate_c_per_hour": 1.0,
                },
            },
            {
                "source_event_id": "srcevt_twc_current",
                "source_id": "twc_current_icao_metric",
                "source_class": "diagnostic_current_source",
                "temperature_c": 31.0,
                "observed_today_max_c": 31.0,
            },
            {
                "source_event_id": "srcevt_taf",
                "source_id": "zuuu_taf_forecast_peak",
                "source_class": "forecast_weak_source",
                "forecast_peak_c": 33.0,
            },
        ],
    )

    included = [row for row in result["source_trace"] if row["included"]]
    excluded = [row for row in result["source_trace"] if not row["included"]]
    assert [row["source_id"] for row in included] == ["awc_cache_metars_csv_gz"]
    assert len(excluded) == 2
    assert all(row["exclusion_reason"] == "source_class_not_hard_observation" for row in excluded)
    assert result["hard_constraints"]["observed_today_max_c"] == 28.0


def test_amsc_metar_is_primary_hard_observation_and_taf_is_excluded_from_strong_layer():
    result = build_metar_strong_likelihood(
        buckets=["35", "36", "37", "38"],
        as_of="2026-07-05T14:30:00+08:00",
        source_observations=[
            {
                "source_event_id": "srcevt_amsc_metar",
                "source_id": "zuuu_metar_raw_amsc",
                "source_class": "hard_observation_candidate",
                "temperature_c": 36.0,
                "dewpoint_c": 22.0,
                "observed_today_max_c": 36.0,
                "valid_time": "2026-07-05T06:30:00Z",
                "payload": {"visibility_m": 9999, "cloud_text": "FEW020"},
            },
            {
                "source_event_id": "srcevt_amsc_taf",
                "source_id": "zuuu_taf_forecast_amsc",
                "source_class": "forecast_weak_source",
                "forecast_peak_c": 38.0,
            },
        ],
    )

    included = [row for row in result["source_trace"] if row["included"]]
    excluded = [row for row in result["source_trace"] if not row["included"]]
    assert [row["source_id"] for row in included] == ["zuuu_metar_raw_amsc"]
    assert [row["source_id"] for row in excluded] == ["zuuu_taf_forecast_amsc"]
    assert excluded[0]["exclusion_reason"] == "source_class_not_hard_observation"
    assert result["hard_constraints"]["current_temperature_c"] == 36.0
    assert result["hard_constraints"]["observed_today_max_c"] == 36.0


def test_amsc_metar_overrides_reference_awc_generic_observation_inputs_in_strong_layer():
    result = build_metar_strong_likelihood(
        buckets=["35", "36", "37", "38"],
        as_of="2026-07-05T14:30:00+08:00",
        observation_inputs={
            "current_temp_c": 35.0,
            "observed_temp_source": "zuuu_metar_raw_awc",
            "observed_today_max_c": 35.0,
            "observed_today_max_source": "zuuu_metar_raw_awc",
            "zuuu_metar_raw_awc_today_max_c": 35.0,
        },
        source_observations=[
            {
                "source_event_id": "srcevt_awc_metar",
                "source_id": "zuuu_metar_raw_awc",
                "source_class": "hard_observation_candidate",
                "temperature_c": 35.0,
                "observed_today_max_c": 35.0,
                "valid_time": "2026-07-05T06:00:00Z",
            },
            {
                "source_event_id": "srcevt_amsc_metar",
                "source_id": "zuuu_metar_raw_amsc",
                "source_class": "hard_observation_candidate",
                "temperature_c": 36.0,
                "observed_today_max_c": 36.0,
                "valid_time": "2026-07-05T06:30:00Z",
            },
        ],
    )

    assert result["hard_constraints"]["current_temperature_c"] == 36.0
    assert result["hard_constraints"]["observed_today_max_c"] == 36.0
    included = [row for row in result["source_trace"] if row["included"]]
    assert [row["source_id"] for row in included] == ["zuuu_metar_raw_awc", "zuuu_metar_raw_amsc"]


def test_hard_observation_final_flag_from_source_observation_locks_strong_likelihood_one_hot():
    result = build_metar_strong_likelihood(
        buckets=["36°C", "37°C", "38°C"],
        as_of="2026-07-05T21:05:00+08:00",
        source_observations=[
            {
                "source_event_id": "srcevt_awc_final",
                "source_id": "zuuu_metar_raw_awc",
                "source_class": "hard_observation_candidate",
                "temperature_c": 36.0,
                "observed_today_max_c": 37.0,
                "valid_time": "2026-07-05T13:00:00Z",
                "payload": {
                    "observed_today_max_final": True,
                    "hard_source_final_daily_max": True,
                    "temperature_observation_final_daily_max": True,
                    "raw_text": "ZUUU 051300Z 00000MPS 9999 FEW020 36/22 Q1004",
                },
            }
        ],
    )

    assert result["hard_constraints"]["observed_today_max_final"] is True
    assert result["hard_constraints"]["hard_lock"] is True
    assert result["hard_constraints"]["official_final_result"] == 37.0
    assert result["metar_strong_likelihood_map"] == {"36°C": 0.0, "37°C": 1.0, "38°C": 0.0}
    assert result["hard_constraints"]["hard_mask"] == {"36°C": 0.0, "37°C": 1.0, "38°C": 0.0}
    assert result["observation_features"]["cloud_text"] == "ZUUU 051300Z 00000MPS 9999 FEW020 36/22 Q1004"


def test_integer_metar_one_degree_rebound_from_rolling_peak_is_platform_noise_not_cooling():
    peak_temp_c = 34.0
    one_degree_noise_c = 1.0
    result = build_metar_strong_likelihood(
        buckets=["32", "33", "34", "35"],
        as_of="2026-07-05T14:30:00+08:00",
        source_observations=[
            {
                "source_event_id": "srcevt_amsc_1400",
                "source_id": "zuuu_metar_raw_amsc",
                "source_class": "hard_observation_candidate",
                "temperature_c": peak_temp_c,
                "dewpoint_c": 22.0,
                "valid_time": "2026-07-05T06:00:00Z",
                "payload": {"visibility_m": 9999, "cloud_text": "FEW020"},
            },
            {
                "source_event_id": "srcevt_amsc_1430",
                "source_id": "zuuu_metar_raw_amsc",
                "source_class": "hard_observation_candidate",
                "temperature_c": peak_temp_c - one_degree_noise_c,
                "dewpoint_c": 22.0,
                "valid_time": "2026-07-05T06:30:00Z",
                "payload": {"visibility_m": 9999, "cloud_text": "FEW020"},
            },
            {
                "source_event_id": "srcevt_awc_reference_noise",
                "source_id": "zuuu_metar_raw_awc",
                "source_class": "hard_observation_candidate",
                "temperature_c": peak_temp_c - 3.0,
                "dewpoint_c": 22.0,
                "valid_time": "2026-07-05T06:45:00Z",
                "payload": {"visibility_m": 9999, "cloud_text": "FEW020"},
            },
            {
                "source_event_id": "srcevt_amsc_1500",
                "source_id": "zuuu_metar_raw_amsc",
                "source_class": "hard_observation_candidate",
                "temperature_c": peak_temp_c,
                "dewpoint_c": 22.0,
                "valid_time": "2026-07-05T07:00:00Z",
                "payload": {"visibility_m": 9999, "cloud_text": "FEW020"},
            },
        ],
    )

    trajectory = result["temperature_trajectory"]
    rolling = trajectory["rolling_temperature_observation_summary"]
    assert rolling["schema"] == "metar_integer_temperature_rolling_trajectory_v1"
    assert rolling["state"] == "platform"
    assert rolling["rebound_pattern"] is True
    assert [point["source_id"] for point in rolling["points"]] == ["zuuu_metar_raw_amsc"] * 3
    assert rolling["platform_end_confirmed"] is False
    assert trajectory["state"] == "platform"
    assert trajectory["gate_state"] == "not_warming"
    assert result["hard_constraints"]["observed_today_max_c"] == peak_temp_c
    assert result["observation_features"]["temperature_trajectory_state"] == "platform"


def test_integer_metar_platform_ends_only_after_two_reports_drop_at_least_two_c():
    peak_temp_c = 34.0
    platform_end_drop_c = 2.0
    result = build_metar_strong_likelihood(
        buckets=["31", "32", "33", "34"],
        as_of="2026-07-05T15:00:00+08:00",
        source_observations=[
            {
                "source_event_id": "srcevt_1400",
                "source_id": "zuuu_metar_raw_amsc",
                "source_class": "hard_observation_candidate",
                "temperature_c": peak_temp_c,
                "valid_time": "2026-07-05T06:00:00Z",
                "payload": {"visibility_m": 9999, "cloud_text": "FEW020"},
            },
            {
                "source_event_id": "srcevt_1430",
                "source_id": "zuuu_metar_raw_amsc",
                "source_class": "hard_observation_candidate",
                "temperature_c": peak_temp_c - platform_end_drop_c,
                "valid_time": "2026-07-05T06:30:00Z",
                "payload": {"visibility_m": 9999, "cloud_text": "FEW020"},
            },
            {
                "source_event_id": "srcevt_1500",
                "source_id": "zuuu_metar_raw_amsc",
                "source_class": "hard_observation_candidate",
                "temperature_c": peak_temp_c - platform_end_drop_c,
                "valid_time": "2026-07-05T07:00:00Z",
                "payload": {"visibility_m": 9999, "cloud_text": "FEW020"},
            },
        ],
    )

    rolling = result["temperature_trajectory"]["rolling_temperature_observation_summary"]
    assert rolling["platform_end_confirmed"] is True
    assert rolling["platform_end_consecutive_reports"] == 2
    assert result["temperature_trajectory"]["state"] == "cooling"


def test_predicted_peak_window_controls_remaining_temperature_integration():
    result = build_metar_strong_likelihood(
        buckets=["34", "35", "36", "37"],
        as_of="2026-07-05T14:30:00+08:00",
        observation_inputs={
            "current_temp_c": 34.0,
            "observed_today_max_c": 34.0,
            "dewpoint_c": 21.0,
            "visibility_m": 9999,
            "cloud_text": "FEW020",
            "expected_peak_hour_local": 15.0,
            "temperature_momentum_1h_c_per_hour": 1.0,
            "temperature_momentum_30m_c_per_hour": 0.8,
            "temperature_momentum_source": "zuuu_metar_raw_amsc",
        },
    )

    window = result["predicted_peak_window"]
    assert window["basis"] == "forecast_peak_hour_expanded_window_not_single_point"
    assert window["start_hour_local"] == 14.0
    assert window["end_hour_local"] == 17.0
    assert window["single_point_forbidden"] is True
    assert result["temperature_trajectory"]["predicted_peak_window"]["end_hour_local"] == 17.0
    assert result["observation_features"]["hours_to_peak_window"] == 2.5
    assert {row["remaining_hours"] for row in result["remaining_warming_by_state"]} == {2.5}


def test_generated_taf_peak_window_is_rebuilt_from_peak_hour_policy():
    result = build_metar_strong_likelihood(
        buckets=["37", "38", "39"],
        as_of="2026-07-09T08:00:00+08:00",
        observation_inputs={
            "current_temp_c": 31.0,
            "observed_today_max_c": 32.0,
            "expected_peak_hour_local": 15.0,
            "expected_peak_window_start_hour_local": 14.0,
            "expected_peak_window_end_hour_local": 16.5,
            "expected_peak_window_source": "zuuu_taf_forecast_amsc_TX_expanded_window",
            "expected_peak_window_basis": "taf_tx_time_expanded_not_single_point",
            "temperature_momentum_source": "zuuu_metar_raw_amsc",
        },
    )

    window = result["predicted_peak_window"]
    assert window["start_hour_local"] == 14.0
    assert window["end_hour_local"] == 17.0
    assert result["observation_features"]["expected_peak_window_end_hour_local"] == 17.0


def test_amsc_1530_1700_replay_recoil_lifts_39_path_at_1630_platform_noise():
    result = build_metar_strong_likelihood(
        buckets=["37°C", "38°C", "39°C", "40°C"],
        as_of="2026-07-08T16:30:00+08:00",
        observation_inputs={
            "taf_forecast_peak_local": "2026-07-08T17:00:00+08:00",
            "dewpoint_c": 21.0,
            "visibility_m": 9999,
            "cloud_text": "SCT040",
            "weather_text": "",
        },
        source_observations=[
            {
                "source_event_id": "srcevt_amsc_1530",
                "source_id": "zuuu_metar_raw_amsc",
                "source_class": "hard_observation_candidate",
                "temperature_c": 38.0,
                "dewpoint_c": 21.0,
                "valid_time": "2026-07-08T07:30:00Z",
                "payload": {"visibility_m": 9999, "cloud_text": "SCT040", "weather_text": ""},
            },
            {
                "source_event_id": "srcevt_amsc_1600",
                "source_id": "zuuu_metar_raw_amsc",
                "source_class": "hard_observation_candidate",
                "temperature_c": 38.0,
                "dewpoint_c": 22.0,
                "valid_time": "2026-07-08T08:00:00Z",
                "payload": {"visibility_m": 9999, "cloud_text": "SCT040", "weather_text": ""},
            },
            {
                "source_event_id": "srcevt_amsc_1630",
                "source_id": "zuuu_metar_raw_amsc",
                "source_class": "hard_observation_candidate",
                "temperature_c": 37.0,
                "dewpoint_c": 21.0,
                "observed_today_max_c": 38.0,
                "valid_time": "2026-07-08T08:30:00Z",
                "payload": {"visibility_m": 9999, "cloud_text": "SCT040", "weather_text": ""},
            },
        ],
    )

    trajectory = result["temperature_trajectory"]
    rolling = trajectory["rolling_temperature_observation_summary"]
    s2 = [row for row in result["remaining_warming_by_state"] if row["state"] == "S2_open_boundary_layer"][0]

    assert rolling["state"] == "platform"
    assert rolling["latest_drop_from_rolling_max_c"] == 1.0
    assert rolling["platform_end_confirmed"] is False
    assert result["platform_recoil_active"] is True
    assert result["platform_recoil_watch"]["reason"] == "hard_metar_near_high_platform_before_peak_window_end"
    assert result["recoil_state_ssot"]["state"] == "platform_noise"
    assert result["recoil_state_ssot"]["state_label"] == "平台波动"
    assert result["recoil_state_ssot"]["action"] == "protect_high_tail_expand_uncertainty_slight_residual_warming"
    assert result["recoil_state_ssot"]["physical_cutoff_evidence"]["true_cooling_or_cutoff_confirmed"] is False
    assert s2["platform_recoil_applied"] is True
    assert s2["recoil_state"] == "platform_noise"
    assert s2["recoil_momentum_treatment"] == "protect_high_tail"
    assert s2["raw_delta_mu_c"] < s2["delta_mu_c"]
    assert s2["peak_mu_c"] >= 38.55
    assert result["metar_strong_likelihood_map"]["39°C"] > result["metar_strong_likelihood_map"]["40°C"]


def test_amsc_recoil_does_not_lift_true_two_report_cooling():
    result = build_metar_strong_likelihood(
        buckets=["37°C", "38°C", "39°C", "40°C"],
        as_of="2026-07-08T16:30:00+08:00",
        observation_inputs={
            "taf_forecast_peak_local": "2026-07-08T17:00:00+08:00",
            "dewpoint_c": 21.0,
            "visibility_m": 9999,
            "cloud_text": "SCT040",
            "weather_text": "",
        },
        source_observations=[
            {
                "source_event_id": "srcevt_amsc_1530",
                "source_id": "zuuu_metar_raw_amsc",
                "source_class": "hard_observation_candidate",
                "temperature_c": 39.0,
                "valid_time": "2026-07-08T07:30:00Z",
            },
            {
                "source_event_id": "srcevt_amsc_1600",
                "source_id": "zuuu_metar_raw_amsc",
                "source_class": "hard_observation_candidate",
                "temperature_c": 37.0,
                "valid_time": "2026-07-08T08:00:00Z",
            },
            {
                "source_event_id": "srcevt_amsc_1630",
                "source_id": "zuuu_metar_raw_amsc",
                "source_class": "hard_observation_candidate",
                "temperature_c": 37.0,
                "observed_today_max_c": 39.0,
                "valid_time": "2026-07-08T08:30:00Z",
            },
        ],
    )

    rolling = result["temperature_trajectory"]["rolling_temperature_observation_summary"]
    s2 = [row for row in result["remaining_warming_by_state"] if row["state"] == "S2_open_boundary_layer"][0]

    assert rolling["state"] == "cooling"
    assert rolling["platform_end_confirmed"] is True
    assert result["platform_recoil_active"] is False
    assert result["platform_recoil_watch"]["reason"] == "rolling_or_time_cooling_confirmed"
    assert result["recoil_state_ssot"]["state"] == "confirmed_cooling_cutoff"
    assert result["recoil_state_ssot"]["state_label"] == "真实降温/截断"
    assert result["recoil_state_ssot"]["physical_cutoff_evidence"]["magnitude_cooling_confirmed"] is True
    assert "rolling_drop_ge_2c_two_reports_without_rebound" in result["recoil_state_ssot"]["physical_cutoff_evidence"]["evidence_codes"]
    assert s2["platform_recoil_applied"] is False
    assert s2["recoil_state"] == "confirmed_cooling_cutoff"
    assert s2["recoil_action"] == "disable_recoil_compress_high_tail"
    assert s2["delta_mu_c"] < s2["raw_delta_mu_c"]


def test_amsc_single_large_drop_is_pullback_watch_not_confirmed_cooling():
    result = build_metar_strong_likelihood(
        buckets=["36°C", "37°C", "38°C", "39°C"],
        as_of="2026-07-08T16:30:00+08:00",
        observation_inputs={
            "taf_forecast_peak_local": "2026-07-08T17:00:00+08:00",
            "dewpoint_c": 21.0,
            "visibility_m": 9999,
            "cloud_text": "SCT040",
            "weather_text": "",
        },
        source_observations=[
            {
                "source_event_id": "srcevt_amsc_1530",
                "source_id": "zuuu_metar_raw_amsc",
                "source_class": "hard_observation_candidate",
                "temperature_c": 38.0,
                "valid_time": "2026-07-08T07:30:00Z",
            },
            {
                "source_event_id": "srcevt_amsc_1600",
                "source_id": "zuuu_metar_raw_amsc",
                "source_class": "hard_observation_candidate",
                "temperature_c": 38.0,
                "valid_time": "2026-07-08T08:00:00Z",
            },
            {
                "source_event_id": "srcevt_amsc_1630",
                "source_id": "zuuu_metar_raw_amsc",
                "source_class": "hard_observation_candidate",
                "temperature_c": 36.0,
                "observed_today_max_c": 38.0,
                "valid_time": "2026-07-08T08:30:00Z",
            },
        ],
    )

    rolling = result["temperature_trajectory"]["rolling_temperature_observation_summary"]
    s2 = [row for row in result["remaining_warming_by_state"] if row["state"] == "S2_open_boundary_layer"][0]

    assert rolling["platform_end_confirmed"] is False
    assert result["platform_recoil_active"] is False
    assert result["recoil_state_ssot"]["state"] == "pullback_watch"
    assert result["recoil_state_ssot"]["state_label"] == "待确认回落"
    assert result["recoil_state_ssot"]["action"] == "lower_momentum_confidence_do_not_reverse"
    assert result["recoil_state_ssot"]["physical_cutoff_evidence"]["true_cooling_or_cutoff_confirmed"] is False
    assert s2["recoil_momentum_treatment"] == "lower_confidence_without_reversing"
    assert s2["platform_recoil_applied"] is False


def test_amsc_physical_cutoff_evidence_disables_platform_recoil():
    result = build_metar_strong_likelihood(
        buckets=["37°C", "38°C", "39°C", "40°C"],
        as_of="2026-07-08T16:30:00+08:00",
        observation_inputs={
            "taf_forecast_peak_local": "2026-07-08T17:00:00+08:00",
            "dewpoint_c": 22.0,
            "visibility_m": 7000,
            "cloud_text": "BKN030CB",
            "weather_text": "TSRA",
            "pressure_trend_hpa_per_hour": 1.3,
            "outflow_wind_observed": True,
        },
        source_observations=[
            {
                "source_event_id": "srcevt_amsc_1530",
                "source_id": "zuuu_metar_raw_amsc",
                "source_class": "hard_observation_candidate",
                "temperature_c": 38.0,
                "valid_time": "2026-07-08T07:30:00Z",
                "payload": {"visibility_m": 9999, "cloud_text": "SCT040", "weather_text": ""},
            },
            {
                "source_event_id": "srcevt_amsc_1600",
                "source_id": "zuuu_metar_raw_amsc",
                "source_class": "hard_observation_candidate",
                "temperature_c": 38.0,
                "valid_time": "2026-07-08T08:00:00Z",
                "payload": {"visibility_m": 9999, "cloud_text": "SCT040", "weather_text": ""},
            },
            {
                "source_event_id": "srcevt_amsc_1630",
                "source_id": "zuuu_metar_raw_amsc",
                "source_class": "hard_observation_candidate",
                "temperature_c": 37.0,
                "observed_today_max_c": 38.0,
                "valid_time": "2026-07-08T08:30:00Z",
                "payload": {"visibility_m": 7000, "cloud_text": "BKN030CB", "weather_text": "TSRA"},
            },
        ],
    )

    evidence = result["recoil_state_ssot"]["physical_cutoff_evidence"]
    s2 = [row for row in result["remaining_warming_by_state"] if row["state"] == "S2_open_boundary_layer"][0]

    assert result["platform_recoil_active"] is False
    assert result["recoil_state_ssot"]["state"] == "confirmed_cooling_cutoff"
    assert evidence["physical_cutoff_confirmed"] is True
    assert evidence["convective_or_rain_cutoff"] is True
    assert "cb_observed" in evidence["evidence_codes"]
    assert "thunderstorm_observed" in evidence["evidence_codes"]
    assert "outflow_wind_observed" in evidence["evidence_codes"]
    assert result["state_probs"]["S3_convective_cutoff"] > result["state_probs"]["S2_open_boundary_layer"]
    assert s2["platform_recoil_applied"] is False


def test_pre_dawn_carryover_cooling_and_cb_do_not_declare_daytime_peak_cutoff():
    result = build_metar_strong_likelihood(
        buckets=["28°C", "29°C", "30°C", "31°C", "32°C", "33°C"],
        as_of="2026-07-11T01:00:00+08:00",
        observation_inputs={
            "current_temp_c": 28.0,
            "observed_today_max_c": 29.0,
            "dewpoint_c": 24.0,
            "visibility_m": 9999,
            "cloud_text": "FEW040TCU",
            "weather_text": "",
        },
        source_observations=[
            {
                "source_event_id": "srcevt_midnight_carryover",
                "source_id": "zuuu_metar_raw_amsc",
                "source_class": "hard_observation_candidate",
                "temperature_c": 29.0,
                "valid_time": "2026-07-10T16:00:00Z",
                "payload": {"visibility_m": 9999, "cloud_text": "FEW023TCU"},
            },
            {
                "source_event_id": "srcevt_one_am_cooling",
                "source_id": "zuuu_metar_raw_amsc",
                "source_class": "hard_observation_candidate",
                "temperature_c": 28.0,
                "observed_today_max_c": 29.0,
                "valid_time": "2026-07-10T17:00:00Z",
                "payload": {"visibility_m": 9999, "cloud_text": "FEW040TCU"},
            },
        ],
    )

    recoil = result["recoil_state_ssot"]
    evidence = recoil["physical_cutoff_evidence"]
    assert evidence["cb_observed"] is True
    assert evidence["physical_cutoff_confirmed_raw"] is True
    assert evidence["physical_cutoff_confirmed"] is False
    assert evidence["pre_dawn_carryover_phase"] is True
    assert evidence["pre_dawn_carryover_peak"] is True
    assert evidence["pre_dawn_cutoff_suppressed"] is True
    assert evidence["daytime_peak_cutoff_eligible"] is False
    assert recoil["state"] == "pre_dawn_carryover"
    assert recoil["state_label"] == "跨午夜热量延续"
    assert recoil["action"] == "retain_calendar_day_floor_do_not_infer_daytime_peak_recoil"
    assert recoil["reason"] == "pre_dawn_carryover_lower_bound_not_daytime_peak_recoil"


def test_morning_report_does_not_relabel_midnight_carryover_as_daytime_peak_recoil():
    result = build_metar_strong_likelihood(
        buckets=["25°C", "26°C", "27°C", "28°C", "29°C", "30°C", "31°C"],
        as_of="2026-07-11T07:30:00+08:00",
        observation_inputs={
            "current_temp_c": 25.0,
            "observed_today_max_c": 29.0,
            "observed_today_max_at": "2026-07-10T16:00:00Z",
            "dewpoint_c": 23.0,
            "visibility_m": 9999,
            "cloud_text": "FEW008 FEW033TCU BKN033",
            "pressure_trend_hpa_per_hour": 1.0,
        },
        source_observations=[
            {
                "source_event_id": "srcevt_midnight_carryover",
                "source_id": "zuuu_metar_raw_amsc",
                "source_class": "hard_observation_candidate",
                "temperature_c": 29.0,
                "valid_time": "2026-07-10T16:00:00Z",
                "payload": {"visibility_m": 9999, "cloud_text": "FEW023TCU"},
            },
            {
                "source_event_id": "srcevt_morning",
                "source_id": "zuuu_metar_raw_amsc",
                "source_class": "hard_observation_candidate",
                "temperature_c": 25.0,
                "observed_today_max_c": 29.0,
                "valid_time": "2026-07-10T23:30:00Z",
                "payload": {
                    "visibility_m": 9999,
                    "cloud_text": "FEW008 FEW033TCU BKN033",
                    "wind_speed_mps": 3.0,
                    "wind_gust_mps": 10.0,
                    "pressure_hpa": 1001.0,
                },
            },
        ],
    )

    evidence = result["recoil_state_ssot"]["physical_cutoff_evidence"]
    assert evidence["pre_dawn_carryover_phase"] is False
    assert evidence["pre_dawn_carryover_peak"] is True
    assert evidence["physical_cutoff_confirmed"] is True
    assert result["recoil_state_ssot"]["state"] == "pre_dawn_carryover"
    assert result["recoil_state_ssot"]["reason"] == "pre_dawn_carryover_lower_bound_not_daytime_peak_recoil"


def test_previous_local_day_metar_cannot_raise_current_day_hard_floor():
    result = build_metar_strong_likelihood(
        buckets=["28°C", "29°C", "30°C", "31°C", "32°C"],
        event_date="2026-07-11",
        as_of="2026-07-11T08:00:00+08:00",
        observation_inputs={
            "current_temp_c": 24.0,
            "observed_today_max_c": 29.0,
            "observed_today_max_source": "zuuu_metar_raw_amsc",
        },
        source_observations=[
            {
                "source_event_id": "srcevt_previous_day_31",
                "source_id": "zuuu_metar_raw_amsc",
                "source_class": "hard_observation_candidate",
                "event_date": "2026-07-10",
                "temperature_c": 31.0,
                "valid_time": "2026-07-10T14:30:00Z",
            },
            {
                "source_event_id": "srcevt_current_day_midnight_29",
                "source_id": "zuuu_metar_raw_amsc",
                "source_class": "hard_observation_candidate",
                "event_date": "2026-07-11",
                "temperature_c": 29.0,
                "valid_time": "2026-07-10T16:00:00Z",
            },
            {
                "source_event_id": "srcevt_current_day_0800_24",
                "source_id": "zuuu_metar_raw_amsc",
                "source_class": "hard_observation_candidate",
                "event_date": "2026-07-11",
                "temperature_c": 24.0,
                "valid_time": "2026-07-11T00:00:00Z",
            },
        ],
    )

    assert result["hard_constraints"]["observed_today_max_c"] == 29.0
    assert result["hard_constraints"]["hard_mask"]["29°C"] == 1.0
    assert result["hard_constraints"]["hard_mask"]["30°C"] == 1.0
    previous_trace = next(row for row in result["source_trace"] if row["source_event_id"] == "srcevt_previous_day_31")
    assert previous_trace["included"] is False
    assert previous_trace["exclusion_reason"] == "event_date_mismatch"


def test_missing_observation_returns_neutral_with_na_reasons_and_dedupe_trace():
    result = build_metar_strong_likelihood(
        buckets=BUCKETS,
        as_of="2026-07-03T12:00:00+08:00",
        observation_inputs={},
    )

    assert "current_temp_and_observed_today_max_missing" in result["na_reasons"]
    assert result["metar_strong_likelihood_map"] == {bucket: 0.2 for bucket in BUCKETS}
    assert {row["legacy_channel"] for row in result["dedupe_trace"]} == {
        "observed_nowcast",
        "temperature_trajectory",
        "physical_likelihood",
    }
    assert result["boundary"]["weak_forecast_sources_excluded"] is True
