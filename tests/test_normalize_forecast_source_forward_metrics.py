import json

from external_forecast_snapshot_runtime import build_external_forecast_raw_envelope
from scripts.normalize_forecast_source_forward_metrics import normalize_forward_metrics_envelopes


def test_normalizes_only_forward_metrics_envelopes_with_immutable_lineage():
    body = json.dumps(
        {
            "schema": "forecast_source_forward_metrics_v1",
            "source_id": "twc_forecast_hourly_peak",
            "provider_family": "twc_provider",
            "station_id": "ZUUU",
            "target_event_date_local": "2026-07-12",
            "forecast_peak_c": 36.0,
            "hourly_curve": [{"valid_time_local": "2026-07-12T14:00:00+08:00", "temperature_c": 36.0}],
        }
    )
    envelope = build_external_forecast_raw_envelope(
        source_id="twc_forecast_hourly_peak",
        response_body_utf8=body,
        requested_at="2026-07-11T12:00:00Z",
        response_started_at="2026-07-11T12:00:00Z",
        response_completed_at="2026-07-11T12:00:00Z",
        provider_available_at="2026-07-11T12:00:00Z",
        http_status=200,
        parser_version="forecast_source_forward_metrics_v1",
    )

    report = normalize_forward_metrics_envelopes([envelope])

    assert report["forecast_snapshot_count"] == 1
    row = report["forecast_snapshots"][0]
    assert row["source_id"] == "twc_forecast_hourly_peak"
    assert row["lineage_quality"] == "immutable_envelope"
    assert row["hourly_curve"][0]["temperature_c"] == 36.0


def test_normalizer_unwraps_transport_observed_raw_provider_capture_payload_v2():
    derived = {
        "schema": "forecast_source_forward_metrics_v1",
        "source_id": "ecmwf_model_peak",
        "provider_family": "nwp_ecmwf",
        "station_id": "ZUUU",
        "target_event_date_local": "2026-07-12",
        "forecast_peak_c": 33.0,
        "hourly_curve": [],
    }
    body = json.dumps(
        {
            "schema": "forecast_source_forward_capture_payload_v2",
            "derived_forecast": derived,
            "raw_provider_response_utf8": '{"hourly":{"temperature_2m":[33.0]}}',
        }
    )
    envelope = build_external_forecast_raw_envelope(
        source_id="ecmwf_model_peak",
        response_body_utf8=body,
        requested_at="2026-07-11T12:00:00.000Z",
        response_started_at="2026-07-11T12:00:00.010Z",
        response_completed_at="2026-07-11T12:00:00.200Z",
        provider_available_at="2026-07-11T12:00:00.200Z",
        http_status=200,
        parser_version="forecast_source_forward_metrics_v2",
        capture_time_provenance="observed_transport",
    )

    report = normalize_forward_metrics_envelopes([envelope])

    assert report["forecast_snapshot_count"] == 1
    row = report["forecast_snapshots"][0]
    assert row["raw_provider_response_present"] is True
    assert row["capture_time_provenance"] == "observed_transport"


def test_daily_max_window_does_not_relabel_a_separate_hourly_curve_as_its_peak_trajectory():
    payload = {
        "schema": "forecast_source_forward_metrics_v1",
        "source_id": "weather_com_forecast_peak",
        "provider_family": "twc_provider",
        "station_id": "ZUUU",
        "target_event_date_local": "2026-07-12",
        "forecast_peak_c": 36.0,
        "hourly_curve": [{"valid_time_local": "2026-07-12T15:00:00+08:00", "temperature_c": 35.0}],
        "metrics": {
            "source": "weather_com_forecast_daily_peak",
            "forecast_valid_local": "2026-07-12T07:00:00+08:00",
        },
    }
    envelope = build_external_forecast_raw_envelope(
        source_id="weather_com_forecast_peak",
        response_body_utf8=json.dumps(payload),
        requested_at="2026-07-11T12:00:00Z",
        response_started_at="2026-07-11T12:00:00.010Z",
        response_completed_at="2026-07-11T12:00:00.200Z",
        http_status=200,
        parser_version="forecast_source_forward_metrics_v2",
    )

    row = normalize_forward_metrics_envelopes([envelope])["forecast_snapshots"][0]

    assert row["forecast_value_kind"] == "daily_max_event_window"
    assert row["forecast_record_valid_time"] == "2026-07-12T07:00:00+08:00"
    assert row["trajectory_eligible"] is False
    assert row["hourly_curve"] == []
