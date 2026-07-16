from __future__ import annotations

from external_forecast_snapshot_runtime import build_external_forecast_raw_envelope
from strict_asof_forecast_capture_runtime import (
    TIER_A,
    TIER_B,
    TIER_C,
    build_strict_asof_forecast_capture_manifest,
    classify_strict_asof_forecast_capture,
)


def _forecast(envelope: dict, *, source_id: str = "ecmwf_model_peak", event_date: str = "2026-07-12", raw: bool = True) -> dict:
    return {
        "source_id": source_id,
        "target_event_date_local": event_date,
        "forecast_peak_c": 33.0,
        "forecast_snapshot_id": envelope["response_sha256"],
        "source_event_id": f"forward_{source_id}_{event_date}",
        "raw_provider_response_present": raw,
    }


def test_observed_live_transport_with_raw_provider_response_is_the_only_tier_a() -> None:
    envelope = build_external_forecast_raw_envelope(
        source_id="ecmwf_model_peak",
        response_body_utf8='{"raw":"provider"}',
        requested_at="2026-07-11T00:00:00.000Z",
        response_started_at="2026-07-11T00:00:00.010Z",
        response_completed_at="2026-07-11T00:00:00.200Z",
        provider_available_at="2026-07-11T00:00:00.200Z",
        http_status=200,
        parser_version="capture-v2",
        capture_time_provenance="observed_transport",
        response_headers_observed=True,
    )

    capture = classify_strict_asof_forecast_capture(raw_envelope=envelope, normalized_forecast=_forecast(envelope))

    assert capture["evidence_tier"] == TIER_A
    assert capture["strict_training_eligible"] is True
    assert capture["first_system_seen_at"] == "2026-07-11T00:00:00.200Z"
    assert capture["available_at"] == capture["first_system_seen_at"]


def test_observed_transport_without_headers_is_tier_c() -> None:
    envelope = build_external_forecast_raw_envelope(
        source_id="ecmwf_model_peak",
        response_body_utf8='{"raw":"provider"}',
        requested_at="2026-07-11T00:00:00.000Z",
        response_started_at="2026-07-11T00:00:00.010Z",
        response_completed_at="2026-07-11T00:00:00.200Z",
        http_status=200,
        parser_version="capture-v2",
        capture_time_provenance="observed_transport",
    )

    capture = classify_strict_asof_forecast_capture(raw_envelope=envelope, normalized_forecast=_forecast(envelope))

    assert capture["evidence_tier"] == TIER_C
    assert capture["strict_training_eligible"] is False
    assert "live_capture_response_headers_not_observed" in capture["reasons"]


def test_historical_declared_availability_stays_tier_b_even_when_raw_response_is_immutable() -> None:
    envelope = build_external_forecast_raw_envelope(
        source_id="gfs_model_peak",
        response_body_utf8='{"raw":"provider"}',
        requested_at="2026-07-20T00:00:00Z",
        response_started_at="2026-07-20T00:00:00Z",
        response_completed_at="2026-07-20T00:00:01Z",
        provider_available_at="2026-07-11T12:10:00Z",
        provider_available_at_quality="documented_conservative_upper_bound",
        availability_mode="historical_archive_replay",
        http_status=200,
        parser_version="historical-fetch-v1",
        capture_time_provenance="historical_declared",
    )

    capture = classify_strict_asof_forecast_capture(
        raw_envelope=envelope,
        normalized_forecast=_forecast(envelope, source_id="gfs_model_peak"),
    )

    assert capture["evidence_tier"] == TIER_B
    assert capture["strict_training_eligible"] is False
    assert capture["available_at"] == "2026-07-11T12:10:00Z"
    assert "historical_retrieval_cannot_prove_system_first_seen_at_historical_cutoff" in capture["reasons"]


def test_legacy_historical_envelope_without_header_evidence_remains_tier_b_not_tier_a() -> None:
    envelope = build_external_forecast_raw_envelope(
        source_id="gfs_model_peak",
        response_body_utf8='{"raw":"provider"}',
        requested_at="2026-07-20T00:00:00Z",
        response_started_at="2026-07-20T00:00:00Z",
        response_completed_at="2026-07-20T00:00:01Z",
        provider_available_at="2026-07-11T12:10:00Z",
        provider_available_at_quality="documented_conservative_upper_bound",
        availability_mode="historical_archive_replay",
        http_status=200,
        parser_version="historical-fetch-v1",
        capture_time_provenance="historical_declared",
    )
    envelope.pop("response_headers_observed")

    capture = classify_strict_asof_forecast_capture(
        raw_envelope=envelope,
        normalized_forecast=_forecast(envelope, source_id="gfs_model_peak"),
    )

    assert capture["evidence_tier"] == TIER_B
    assert capture["strict_training_eligible"] is False


def test_batch_clock_proxy_or_missing_raw_response_is_tier_c_and_not_counted_in_coverage() -> None:
    envelope = build_external_forecast_raw_envelope(
        source_id="icon_model_peak",
        response_body_utf8="{}",
        requested_at="2026-07-11T00:00:00Z",
        response_started_at="2026-07-11T00:00:00Z",
        response_completed_at="2026-07-11T00:00:00Z",
        http_status=200,
        parser_version="derived-v1",
        capture_time_provenance="batch_clock_proxy",
    )
    blocked = classify_strict_asof_forecast_capture(
        raw_envelope=envelope,
        normalized_forecast=_forecast(envelope, source_id="icon_model_peak", raw=False),
    )
    a = classify_strict_asof_forecast_capture(
        raw_envelope=build_external_forecast_raw_envelope(
            source_id="ecmwf_model_peak",
            response_body_utf8="{}",
            requested_at="2026-07-11T00:00:00Z",
            response_started_at="2026-07-11T00:00:01Z",
            response_completed_at="2026-07-11T00:00:02Z",
            http_status=200,
            parser_version="capture-v2",
            capture_time_provenance="observed_transport",
        ),
        normalized_forecast={},
    )

    assert blocked["evidence_tier"] == TIER_C
    assert blocked["strict_training_eligible"] is False
    assert "live_capture_transport_time_not_observed" in blocked["reasons"]
    # An invalid/missing normalisation cannot inflate full-source daily coverage.
    manifest = build_strict_asof_forecast_capture_manifest(
        [blocked, a],
        required_source_ids=["ecmwf_model_peak", "gfs_model_peak", "icon_model_peak"],
    )
    assert manifest["strict_training_complete_event_day_count"] == 0
    assert manifest["tier_counts"][TIER_C] == 2
