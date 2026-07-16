from __future__ import annotations

from external_forecast_snapshot_runtime import (
    build_external_forecast_raw_envelope,
    envelope_as_of_eligible_at,
    validate_external_forecast_raw_envelope,
)


def test_envelope_redacts_credentials_hashes_body_and_uses_provider_availability():
    envelope = build_external_forecast_raw_envelope(
        source_id="twc_forecast_peak",
        response_body_utf8='{"temperature": 31}',
        requested_at="2026-07-11T00:00:00Z",
        response_started_at="2026-07-11T00:00:01Z",
        response_completed_at="2026-07-11T00:00:02Z",
        provider_available_at="2026-07-11T00:00:05Z",
        http_status=200,
        parser_version="test-v1",
        request_url="https://api.weather.com/v3/wx?apiKey=secret&units=m",
        request_parameters={"api_key": "secret", "icao": "ZUUU"},
    )

    assert envelope["provider_family"] == "twc_provider"
    assert "secret" not in envelope["request_url_redacted"]
    assert envelope["request_parameters"]["api_key"] == "[REDACTED]"
    assert envelope_as_of_eligible_at(envelope) == "2026-07-11T00:00:05.000000Z"
    contract = validate_external_forecast_raw_envelope(envelope)
    assert contract["ok"] is True
    assert not any(contract["guards"].values())


def test_envelope_contract_rejects_time_reversal_and_body_tampering():
    envelope = build_external_forecast_raw_envelope(
        source_id="gfs_model_peak",
        response_body_utf8="original",
        requested_at="2026-07-11T00:00:02Z",
        response_started_at="2026-07-11T00:00:01Z",
        response_completed_at="2026-07-11T00:00:00Z",
        http_status=200,
        parser_version="test-v1",
    )
    envelope["response_body_utf8"] = "tampered"
    contract = validate_external_forecast_raw_envelope(envelope)

    assert contract["ok"] is False
    assert "response_started_before_request" in contract["issues"]
    assert "response_completed_before_start" in contract["issues"]
    assert "response_sha256_mismatch" in contract["issues"]


def test_historical_replay_keeps_retrieval_time_separate_from_past_availability():
    envelope = build_external_forecast_raw_envelope(
        source_id="gfs_model_peak",
        response_body_utf8="{}",
        requested_at="2026-07-20T00:00:00Z",
        response_started_at="2026-07-20T00:00:01Z",
        response_completed_at="2026-07-20T00:00:02Z",
        provider_available_at="2026-07-10T12:10:00Z",
        provider_available_at_quality="documented_conservative_upper_bound",
        availability_mode="historical_archive_replay",
        http_status=200,
        parser_version="test-v1",
    )
    assert envelope["as_of_eligible_at"] == "2026-07-10T12:10:00.000000Z"
    assert validate_external_forecast_raw_envelope(envelope)["ok"] is True
