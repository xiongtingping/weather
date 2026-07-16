from __future__ import annotations

from external_forecast_snapshot_runtime import build_external_forecast_raw_envelope
from strict_asof_forecast_capture_runtime import classify_strict_asof_forecast_capture
from strict_asof_forecast_source_observation_runtime import (
    build_tier_a_forecast_source_observation_proposal,
)


def _tier_a_capture() -> tuple[dict, dict]:
    envelope = build_external_forecast_raw_envelope(
        source_id="ecmwf",
        response_body_utf8='{"raw":"provider"}',
        requested_at="2026-07-11T00:00:00.000Z",
        response_started_at="2026-07-11T00:00:00.010Z",
        response_completed_at="2026-07-11T00:00:00.200Z",
        provider_available_at="2026-07-11T00:00:00.200Z",
        http_status=200,
        parser_version="capture-v2",
        source_event_id="forward_ecmwf_2026-07-12",
        capture_time_provenance="observed_transport",
        response_headers_observed=True,
    )
    forecast = {
        "source_id": "ecmwf",
        "target_event_date_local": "2026-07-12",
        "forecast_peak_c": 33.0,
        "forecast_snapshot_id": envelope["response_sha256"],
        "source_event_id": "forward_ecmwf_2026-07-12",
        "raw_provider_response_present": True,
        "hourly_curve": [
            {"valid_time_local": "2026-07-12T14:00:00+08:00", "temperature_c": 32.0},
            {"valid_time_local": "2026-07-12T15:00:00+08:00", "temperature_c": 33.0},
        ],
    }
    return classify_strict_asof_forecast_capture(raw_envelope=envelope, normalized_forecast=forecast), forecast


def test_tier_a_capture_becomes_append_ready_forecast_observation_proposal_with_raw_and_first_seen_identity() -> None:
    capture, forecast = _tier_a_capture()

    proposal = build_tier_a_forecast_source_observation_proposal(
        capture=capture,
        normalized_forecast=forecast,
        station_id="ZUUU",
        raw_ref="research://forecast/ecmwf/20260711T000000Z.json.gz",
    )

    assert proposal["ok"] is True
    observation = proposal["source_observation"]
    assert observation["source_class"] == "forecast_weak_source"
    assert observation["fetched_at"] == "2026-07-11T00:00:00.200Z"
    assert observation["valid_time"] == "2026-07-12T15:00:00+08:00"
    assert observation["payload"]["evidence_tier"] == "A_live_observed_transport"
    assert observation["payload"]["raw_response_sha256"] == capture["raw_response_sha256"]
    assert proposal["boundary"]["append_only_p1_proposal_not_p1_write"] is True


def test_tier_b_and_missing_peak_valid_time_cannot_cross_into_source_observation_proposal() -> None:
    capture, forecast = _tier_a_capture()
    capture["evidence_tier"] = "B_historical_declared_availability"
    capture["strict_training_eligible"] = False

    blocked_tier = build_tier_a_forecast_source_observation_proposal(
        capture=capture,
        normalized_forecast=forecast,
        station_id="ZUUU",
        raw_ref="research://forecast/ecmwf/immutable.json.gz",
    )
    blocked_time = build_tier_a_forecast_source_observation_proposal(
        capture=_tier_a_capture()[0],
        normalized_forecast={**forecast, "hourly_curve": []},
        station_id="ZUUU",
        raw_ref="research://forecast/ecmwf/immutable.json.gz",
    )

    assert blocked_tier["ok"] is False
    assert "forecast_source_observation_capture_not_tier_a" in blocked_tier["block_reasons"]
    assert blocked_time["ok"] is False
    assert "forecast_source_observation_peak_or_daily_window_valid_time_missing" in blocked_time["block_reasons"]


def test_daily_max_window_uses_provider_record_time_without_faking_a_peak_time() -> None:
    capture, forecast = _tier_a_capture()
    daily_window = {
        **forecast,
        "forecast_peak_c": 34.0,
        "hourly_curve": [{"valid_time_local": "2026-07-12T15:00:00+08:00", "temperature_c": 33.0}],
        "forecast_value_kind": "daily_max_event_window",
        "forecast_record_valid_time": "2026-07-12T07:00:00+08:00",
    }

    proposal = build_tier_a_forecast_source_observation_proposal(
        capture=capture,
        normalized_forecast=daily_window,
        station_id="ZUUU",
        raw_ref="research://forecast/twc/20260711T000000Z.json.gz",
    )

    assert proposal["ok"] is True
    observation = proposal["source_observation"]
    assert observation["valid_time"] == "2026-07-12T07:00:00+08:00"
    assert observation["payload"]["forecast_value_kind"] == "daily_max_event_window"
    assert observation["payload"]["forecast_peak_valid_time"] == ""
    assert observation["payload"]["trajectory_eligible"] is False
    assert "daily_max_event_window" in observation["quality_flags"]


def test_taf_transport_identity_is_preserved_but_uses_the_canonical_taf_weak_source() -> None:
    capture, forecast = _tier_a_capture()
    capture["source_id"] = "zuuu_taf_forecast_amsc"
    forecast = {
        **forecast,
        "source_id": "zuuu_taf_forecast_amsc",
        "forecast_snapshot_id": capture["raw_response_sha256"],
    }

    proposal = build_tier_a_forecast_source_observation_proposal(
        capture=capture,
        normalized_forecast=forecast,
        station_id="ZUUU",
        raw_ref="research://forecast/taf/immutable.json.gz",
    )

    assert proposal["ok"] is True
    observation = proposal["source_observation"]
    assert observation["source_id"] == "taf"
    assert observation["payload"]["provider_source_id"] == "zuuu_taf_forecast_amsc"
    assert observation["payload"]["weak_source_id"] == "taf"
