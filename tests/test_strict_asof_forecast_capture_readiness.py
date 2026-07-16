from __future__ import annotations

import gzip
import json
from pathlib import Path

from external_forecast_snapshot_runtime import build_external_forecast_raw_envelope
from scripts.strict_asof_forecast_capture_readiness import (
    _normalized_forecast,
    build_strict_asof_forecast_capture_readiness,
)


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle)


def _tier_a(source_id: str, event_date: str) -> dict:
    derived = {
        "schema": "forecast_source_forward_metrics_v1",
        "source_id": source_id,
        "target_event_date_local": event_date,
        "forecast_peak_c": 33.0,
    }
    return build_external_forecast_raw_envelope(
        source_id=source_id,
        response_body_utf8=json.dumps(
            {
                "schema": "forecast_source_forward_capture_payload_v2",
                "derived_forecast": derived,
                "raw_provider_response_utf8": "{\"hourly\":{}}",
            }
        ),
        requested_at="2026-07-11T00:00:00.000Z",
        response_started_at="2026-07-11T00:00:00.010Z",
        response_completed_at="2026-07-11T00:00:00.200Z",
        provider_available_at="2026-07-11T00:00:00.200Z",
        http_status=200,
        parser_version="forward-capture-v2",
        source_event_id=f"forward_{source_id}_{event_date}",
        capture_time_provenance="observed_transport",
        response_headers_observed=True,
    )


def _tier_b() -> dict:
    return build_external_forecast_raw_envelope(
        source_id="gfs_model_peak",
        response_body_utf8=json.dumps(
            {
                "hourly": {
                    "time": ["2026-07-12T12:00", "2026-07-12T13:00"],
                    "temperature_2m": [31.0, 32.0],
                }
            }
        ),
        requested_at="2026-07-20T00:00:00Z",
        response_started_at="2026-07-20T00:00:00Z",
        response_completed_at="2026-07-20T00:00:01Z",
        provider_available_at="2026-07-11T12:10:00Z",
        provider_available_at_quality="documented_conservative_upper_bound",
        availability_mode="historical_archive_replay",
        http_status=200,
        parser_version="historical-v1",
        source_event_id="open_meteo_gfs_model_peak_2026-07-12_2026-07-11T06:00",
        capture_time_provenance="historical_declared",
    )


def test_readiness_counts_only_tier_a_complete_source_days_and_keeps_historical_raw_as_tier_b(tmp_path) -> None:
    root = tmp_path / "raw"
    _write(root / "ecmwf.json.gz", _tier_a("ecmwf_model_peak", "2026-07-12"))
    _write(root / "gfs.json.gz", _tier_a("gfs_model_peak", "2026-07-12"))
    _write(root / "icon.json.gz", _tier_a("icon_model_peak", "2026-07-12"))
    _write(root / "historical-gfs.json.gz", _tier_b())

    report = build_strict_asof_forecast_capture_readiness(
        root,
        required_source_ids=["ecmwf_model_peak", "gfs_model_peak", "icon_model_peak"],
        minimum_complete_event_days=1,
    )

    assert report["ok"] is True
    assert report["manifest"]["strict_training_complete_event_dates"] == ["2026-07-12"]
    assert report["manifest"]["tier_counts"]["A_live_observed_transport"] == 3
    assert report["manifest"]["tier_counts"]["B_historical_declared_availability"] == 1
    assert report["manifest"]["strict_training_complete_event_day_count"] == 1


def test_readiness_reports_blocked_when_existing_history_has_no_tier_a_complete_day(tmp_path) -> None:
    root = tmp_path / "raw"
    _write(root / "historical-gfs.json.gz", _tier_b())

    report = build_strict_asof_forecast_capture_readiness(
        root,
        required_source_ids=["ecmwf_model_peak", "gfs_model_peak", "icon_model_peak"],
        minimum_complete_event_days=1,
    )

    assert report["ok"] is False
    assert "strict_capture_tier_a_coverage_not_ready" in report["blockers"]
    assert report["manifest"]["strict_training_complete_event_day_count"] == 0


def test_capture_contract_readiness_is_distinct_from_minimum_history_threshold(tmp_path) -> None:
    root = tmp_path / "raw"
    for source_id in ("ecmwf_model_peak", "gfs_model_peak", "icon_model_peak"):
        _write(root / f"{source_id}.json.gz", _tier_a(source_id, "2026-07-12"))

    report = build_strict_asof_forecast_capture_readiness(
        root,
        required_source_ids=["ecmwf_model_peak", "gfs_model_peak", "icon_model_peak"],
        minimum_complete_event_days=2,
    )

    assert report["ok"] is False
    assert report["manifest"]["capture_contract_ready"] is True
    assert report["manifest"]["strict_training_complete_event_day_count"] == 1
    assert report["manifest"]["minimum_complete_event_days"] == 2
    assert "strict_capture_tier_a_coverage_not_ready" in report["blockers"]


def test_default_readiness_refuses_to_treat_one_complete_day_as_training_ready(tmp_path) -> None:
    root = tmp_path / "raw"
    for source_id in ("ecmwf_model_peak", "gfs_model_peak", "icon_model_peak"):
        _write(root / f"{source_id}.json.gz", _tier_a(source_id, "2026-07-12"))

    report = build_strict_asof_forecast_capture_readiness(
        root,
        required_source_ids=["ecmwf_model_peak", "gfs_model_peak", "icon_model_peak"],
    )

    assert report["ok"] is False
    assert report["manifest"]["capture_contract_ready"] is True
    assert report["manifest"]["minimum_complete_event_days"] == 365
    assert "strict_capture_tier_a_coverage_not_ready" in report["blockers"]


def test_legacy_twc_daily_capture_is_normalized_as_a_daily_window_not_a_peak_trajectory() -> None:
    payload = {
        "schema": "forecast_source_forward_capture_payload_v2",
        "derived_forecast": {
            "schema": "forecast_source_forward_metrics_v1",
            "source_id": "weather_com_forecast_peak",
            "target_event_date_local": "2026-07-12",
            "forecast_peak_c": 36.0,
            "hourly_curve": [{"valid_time_local": "2026-07-12T15:00:00+08:00", "temperature_c": 35.0}],
            "metrics": {
                "source": "weather_com_forecast_daily_peak",
                "forecast_valid_local": "2026-07-12T07:00:00+08:00",
            },
        },
        "raw_provider_response_utf8": "{}",
    }
    envelope = build_external_forecast_raw_envelope(
        source_id="weather_com_forecast_peak",
        response_body_utf8=json.dumps(payload),
        requested_at="2026-07-11T00:00:00Z",
        response_started_at="2026-07-11T00:00:00.010Z",
        response_completed_at="2026-07-11T00:00:00.200Z",
        http_status=200,
        parser_version="forward-capture-v2",
        source_event_id="forward_weather_com_forecast_peak_2026-07-12",
        capture_time_provenance="observed_transport",
        response_headers_observed=True,
    )

    normalized = _normalized_forecast(envelope)

    assert normalized["forecast_value_kind"] == "daily_max_event_window"
    assert normalized["forecast_record_valid_time"] == "2026-07-12T07:00:00+08:00"
    assert normalized["trajectory_eligible"] is False
    assert normalized["hourly_curve"] == []
