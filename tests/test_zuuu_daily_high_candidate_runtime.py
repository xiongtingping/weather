from zuuu_daily_high_candidate_runtime import (
    build_zuuu_daily_high_candidate,
    build_zuuu_daily_high_candidate_contract,
    canonical_observation_snapshot,
)
from metar_strong_likelihood_runtime import build_metar_strong_likelihood


BUCKETS = ["32°C", "33°C", "34°C", "35°C", "36°C", "37°C", "38°C", "39°C", "40°C", "41°C or higher"]


def _row(valid_time: str, temp: float, *, fetched_at: str | None = None, source_id: str = "zuuu_metar_raw_amsc", raw: str = "", **extra):
    return {
        "source_id": source_id,
        "source_class": "hard_observation_candidate",
        "station_id": "ZUUU",
        "event_date": valid_time[:10],
        "valid_time": valid_time,
        "valid_time_local": valid_time.replace("Z", "+00:00"),
        "fetched_at": fetched_at or valid_time,
        "parsed_at": fetched_at or valid_time,
        "response_completed_at": fetched_at or valid_time,
        "temperature_c": temp,
        "raw_ref": raw,
        "payload": extra,
        **extra,
    }


def test_canonical_snapshot_uses_raw_valid_time_primary_source_and_latest_correction():
    rows = [
        _row(
            "2026-07-06T10:00:00Z",
            33.0,
            fetched_at="2026-07-06T10:01:00Z",
            source_id="zuuu_metar_raw_awc",
            raw="METAR ZUUU 060900Z 00000MPS CAVOK 33/20 Q1000=",
            source_observation_id="awc",
        ),
        _row(
            "2026-07-06T10:00:00Z",
            34.0,
            fetched_at="2026-07-06T10:02:00Z",
            raw="METAR ZUUU 061000Z 00000MPS CAVOK 34/20 Q1000=",
            source_observation_id="amsc-old",
        ),
        _row(
            "2026-07-06T10:00:00Z",
            35.0,
            fetched_at="2026-07-06T10:03:00Z",
            raw="METAR ZUUU 061000Z 00000MPS CAVOK 35/20 Q1000=",
            source_observation_id="amsc-correction",
        ),
        _row(
            "2026-07-06T11:00:00Z",
            36.0,
            fetched_at="2026-07-06T11:05:00Z",
            raw="METAR ZUUU 061100Z 00000MPS CAVOK 36/20 Q1000=",
            source_observation_id="late",
        ),
        _row(
            "2026-07-06T10:15:00Z",
            34.0,
            fetched_at="2026-07-06T10:31:00Z",
            raw="METAR ZUUU 061015Z 00000MPS CAVOK 34/20 Q1000=",
            source_observation_id="late-arrival",
        ),
        _row(
            "2026-07-06T10:30:00Z",
            34.0,
            fetched_at="2026-07-06T10:31:00Z",
            raw="METAR ZUUU 061030Z 00000MPS CAVOK 34/20 Q1000=",
            source_observation_id="adapter",
            parser_version="posterior_ssot_projection_fact_feature_source_adapter_v1",
        ),
    ]
    snapshot = canonical_observation_snapshot(
        source_observations=rows,
        as_of="2026-07-06T10:30:00Z",
        event_date="2026-07-06",
    )

    assert snapshot["distinct_valid_time_count"] == 2
    point = snapshot["selected_points"][-1]
    assert point["temperature_c"] == 35.0
    assert point["source_observation_id"] == "amsc-correction"
    assert point["verified_metar_valid_time"] == "2026-07-06T10:00:00Z"
    assert {item["reason"] for item in snapshot["excluded_observations"]} >= {
        "future_valid_time",
        "late_arrival_after_cutoff",
        "adapter_derived_row_excluded",
    }


def test_early_integer_plateau_keeps_positive_tail_and_does_not_hard_cutoff():
    rows = [
        _row("2026-07-07T02:00:00Z", 31.0, raw="METAR ZUUU 070200Z 00000MPS CAVOK 31/20 Q1000="),
        _row("2026-07-07T02:30:00Z", 31.0, raw="METAR ZUUU 070230Z 00000MPS CAVOK 31/20 Q1000="),
        _row("2026-07-07T03:00:00Z", 31.0, raw="METAR ZUUU 070300Z 00000MPS CAVOK 31/20 Q1000="),
    ]
    candidate = build_zuuu_daily_high_candidate(
        buckets=BUCKETS,
        source_observations=rows,
        as_of="2026-07-07T03:05:00Z",
        event_date="2026-07-07",
    )

    trajectory = candidate["temperature_trajectory_posterior"]
    remaining = candidate["remaining_daily_high_distribution"]
    assert trajectory["local_phase"] == "morning_warming"
    assert trajectory["state_probs"]["quantized_plateau_watch"] > trajectory["state_probs"]["plateau_near_daily_high"]
    assert remaining["remaining_increment_positive_quantiles_c"]["p90"] > 0.0
    assert candidate["physical_environment_state_posterior"]["physical_cutoff_confirmed"] is False
    assert abs(sum(candidate["metar_strong_likelihood_candidate"]["likelihood_map"].values()) - 1.0) < 1e-6


def test_short_pullback_and_direct_weather_without_thermal_response_do_not_confirm_cutoff():
    rows = [
        _row("2026-07-06T02:00:00Z", 34.0, raw="METAR ZUUU 060200Z 00000MPS CAVOK 34/20 Q1000="),
        _row("2026-07-06T02:30:00Z", 34.0, raw="METAR ZUUU 060230Z 00000MPS CAVOK 34/20 Q1000="),
        _row("2026-07-06T03:00:00Z", 33.0, raw="METAR ZUUU 060300Z 00000MPS 9999 TSRA 33/20 Q1001="),
    ]
    candidate = build_zuuu_daily_high_candidate(
        buckets=BUCKETS,
        source_observations=rows,
        as_of="2026-07-06T03:05:00Z",
        event_date="2026-07-06",
    )

    trajectory = candidate["temperature_trajectory_posterior"]
    physical = candidate["physical_environment_state_posterior"]
    assert trajectory["state_transition_trace"]["single_report_direct_cutoff_forbidden"] is True
    assert physical["physical_cutoff_confirmed"] is False
    assert physical["physical_cutoff_probability"] > 0.0
    assert candidate["remaining_daily_high_distribution"]["remaining_increment_positive_quantiles_c"]["p90"] > 0.0


def test_sustained_cooling_with_independent_dynamic_support_can_confirm_cutoff():
    rows = [
        _row("2026-07-07T09:00:00Z", 37.0, raw="METAR ZUUU 070900Z 18002MPS 9000 FEW020 37/22 Q0998=", wind_dir_degrees=180.0, visibility_m=9000.0, pressure_hpa=998.0),
        _row("2026-07-07T09:30:00Z", 35.0, raw="METAR ZUUU 070930Z 25008MPS 6000 BKN010 TSRA 35/22 Q1000=", wind_dir_degrees=250.0, visibility_m=6000.0, pressure_hpa=1000.0),
        _row("2026-07-07T10:00:00Z", 33.0, raw="METAR ZUUU 071000Z 27012MPS 3500 OVC008 TSRA 33/22 Q1002=", wind_dir_degrees=270.0, visibility_m=3500.0, pressure_hpa=1002.0),
    ]
    candidate = build_zuuu_daily_high_candidate(
        buckets=BUCKETS,
        source_observations=rows,
        as_of="2026-07-07T10:05:00Z",
        event_date="2026-07-07",
    )

    physical = candidate["physical_environment_state_posterior"]
    assert physical["physical_cutoff_confirmed"] is True
    assert "sustained_temperature_cooling" in physical["evidence_codes"]
    assert "wind_shift" in physical["evidence_codes"]
    assert physical["evidence_consumption_trace"]["direct_weather_alone_cannot_confirm_cutoff"] is True


def test_candidate_ignores_forecast_peak_fields_and_marks_shadow_only():
    rows = [
        _row("2026-07-08T06:00:00Z", 36.0, raw="METAR ZUUU 080600Z 00000MPS CAVOK 36/20 Q1000=", taf_forecast_peak_c=46.0),
        _row("2026-07-08T06:30:00Z", 37.0, raw="METAR ZUUU 080630Z 00000MPS CAVOK 37/20 Q1000=", taf_forecast_peak_c=46.0),
    ]
    candidate = build_zuuu_daily_high_candidate(
        buckets=BUCKETS,
        source_observations=rows,
        as_of="2026-07-08T06:35:00Z",
        event_date="2026-07-08",
    )

    assert candidate["candidate_only"] is True
    assert candidate["production_applied"] is False
    assert candidate["temperature_trajectory_posterior"]["local_phase"] == "broad_peak"
    assert candidate["metar_strong_likelihood_candidate"]["time_conditioning_trace"]["local_phase_consumed_once_in_remaining_path"] is True


def test_production_runtime_exposes_candidate_without_replacing_production_likelihood():
    rows = [
        _row("2026-07-08T06:00:00Z", 36.0, raw="METAR ZUUU 080600Z 00000MPS CAVOK 36/20 Q1000="),
        _row("2026-07-08T06:30:00Z", 37.0, raw="METAR ZUUU 080630Z 00000MPS CAVOK 37/20 Q1000="),
    ]
    result = build_metar_strong_likelihood(
        buckets=BUCKETS,
        source_observations=rows,
        as_of="2026-07-08T06:35:00Z",
        event_date="2026-07-08",
    )

    candidate = result["daily_high_observation_candidate"]
    assert result["runtime_policy_version"] != candidate["policy_version"]
    assert candidate["candidate_only"] is True
    assert candidate["production_applied"] is False
    assert abs(result["metar_strong_likelihood_candidate"]["likelihood_sum"] - 1.0) < 1e-6
    assert result["boundary"]["daily_high_observation_candidate_shadow_only"] is True
    contract = build_zuuu_daily_high_candidate_contract(candidate)
    assert contract["ok"] is True
    assert contract["candidate_only"] is True
    assert contract["production_applied"] is False


def test_candidate_contract_rejects_probability_drift_and_any_production_apply_flag():
    candidate = build_zuuu_daily_high_candidate(
        buckets=BUCKETS,
        source_observations=[
            _row("2026-07-08T06:00:00Z", 36.0, raw="METAR ZUUU 080600Z 00000MPS CAVOK 36/20 Q1000="),
            _row("2026-07-08T06:30:00Z", 37.0, raw="METAR ZUUU 080630Z 00000MPS CAVOK 37/20 Q1000="),
        ],
        as_of="2026-07-08T06:35:00Z",
        event_date="2026-07-08",
    )
    candidate["production_applied"] = True
    candidate["remaining_daily_high_distribution"]["bucket_probabilities"]["32°C"] = 0.5

    contract = build_zuuu_daily_high_candidate_contract(candidate)

    assert contract["ok"] is False
    assert "production_applied_guard_invalid" in contract["issues"]
    assert "remaining_bucket_probabilities_not_normalized" in contract["issues"]
