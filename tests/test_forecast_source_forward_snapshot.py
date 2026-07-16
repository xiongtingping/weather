from __future__ import annotations

import gzip
import json
from pathlib import Path

import scripts.forecast_source_forward_snapshot as snapshot_writer
from external_forecast_snapshot_runtime import build_external_forecast_raw_envelope


def _envelope() -> dict:
    return build_external_forecast_raw_envelope(
        source_id="gfs_model_peak",
        response_body_utf8='{"hourly": {"temperature_2m": [30]}}',
        requested_at="2026-07-11T00:00:00Z",
        response_started_at="2026-07-11T00:00:01Z",
        response_completed_at="2026-07-11T00:00:02Z",
        http_status=200,
        parser_version="test-v1",
    )


def test_forward_snapshot_is_dry_run_by_default(tmp_path, monkeypatch):
    allowed = tmp_path / "research" / "raw"
    monkeypatch.setattr(snapshot_writer, "DEFAULT_RESEARCH_RAW_ROOT", allowed)

    result = snapshot_writer.write_forward_snapshot(_envelope(), output_root=allowed)

    assert result["state"] == "dry_run"
    assert result["writes_research_files"] is False
    assert not allowed.exists()


def test_forward_snapshot_write_is_explicit_and_idempotent(tmp_path, monkeypatch):
    allowed = tmp_path / "research" / "raw"
    monkeypatch.setattr(snapshot_writer, "DEFAULT_RESEARCH_RAW_ROOT", allowed)
    envelope = _envelope()

    first = snapshot_writer.write_forward_snapshot(
        envelope,
        output_root=allowed,
        allow_research_write=True,
    )
    second = snapshot_writer.write_forward_snapshot(
        envelope,
        output_root=allowed,
        allow_research_write=True,
    )

    assert first["state"] == "written"
    assert second["state"] == "already_present"
    with gzip.open(first["output_path"], "rt", encoding="utf-8") as handle:
        stored = json.load(handle)
    assert stored["response_sha256"] == envelope["response_sha256"]
    assert not any(first["guards"].values())


def test_forward_snapshot_publishes_gzip_only_after_complete_temporary_write(tmp_path, monkeypatch):
    allowed = tmp_path / "research" / "raw"
    monkeypatch.setattr(snapshot_writer, "DEFAULT_RESEARCH_RAW_ROOT", allowed)
    original_replace = snapshot_writer.os.replace
    seen = {}

    def assert_atomic_replace(source, destination):
        seen["source_exists"] = source.exists()
        seen["destination_existed_before_replace"] = destination.exists()
        assert source.name.startswith(".") and ".tmp." in source.name
        original_replace(source, destination)

    monkeypatch.setattr(snapshot_writer.os, "replace", assert_atomic_replace)

    result = snapshot_writer.write_forward_snapshot(
        _envelope(),
        output_root=allowed,
        allow_research_write=True,
    )

    assert result["state"] == "written"
    assert seen == {"source_exists": True, "destination_existed_before_replace": False}
    assert not list(Path(result["output_path"]).parent.glob(".*.tmp.*"))


def test_forward_metrics_snapshot_dedupes_unchanged_poll_bodies(tmp_path, monkeypatch):
    allowed = tmp_path / "research" / "raw"
    monkeypatch.setattr(snapshot_writer, "DEFAULT_RESEARCH_RAW_ROOT", allowed)
    kwargs = {
        "source_id": "twc_forecast_hourly_peak",
        "target_event_date_local": "2026-07-12",
        "station_id": "ZUUU",
        "metrics": {
            "value_c": 36.0,
            "source_url": "https://example.test/forecast?apiKey=secret",
            "hourly_curve": [{"valid_time_local": "2026-07-12T14:00:00+08:00", "temperature_c": 36.0}],
        },
        "output_root": allowed,
        "allow_research_write": True,
    }
    first = snapshot_writer.write_forward_metrics_snapshot(observed_at="2026-07-11T12:00:00Z", **kwargs)
    second = snapshot_writer.write_forward_metrics_snapshot(observed_at="2026-07-11T12:30:00Z", **kwargs)

    assert first["state"] == "written"
    assert second["state"] == "already_current"
    assert len(list(allowed.glob("**/*.json.gz"))) == 1
    with gzip.open(first["output_path"], "rt", encoding="utf-8") as handle:
        stored = json.load(handle)
    assert "secret" not in stored["request_url_redacted"]
    assert "secret" not in stored["response_body_utf8"]


def test_forward_metrics_snapshot_preserves_observed_transport_and_raw_provider_response_for_future_tier_a_review(tmp_path, monkeypatch):
    allowed = tmp_path / "research" / "raw"
    monkeypatch.setattr(snapshot_writer, "DEFAULT_RESEARCH_RAW_ROOT", allowed)

    result = snapshot_writer.write_forward_metrics_snapshot(
        source_id="ecmwf_model_peak",
        target_event_date_local="2026-07-12",
        station_id="ZUUU",
        metrics={"forecast_peak_c": 33.0, "source_url": "https://example.test/model"},
        observed_at="2026-07-11T12:00:00.200Z",
        requested_at="2026-07-11T12:00:00.000Z",
        response_started_at="2026-07-11T12:00:00.010Z",
        response_completed_at="2026-07-11T12:00:00.200Z",
        capture_time_provenance="observed_transport",
        raw_provider_response_utf8='{"hourly":{"temperature_2m":[33.0]}}',
        output_root=allowed,
        allow_research_write=True,
    )

    assert result["state"] == "written"
    with gzip.open(result["output_path"], "rt", encoding="utf-8") as handle:
        stored = json.load(handle)
    payload = json.loads(stored["response_body_utf8"])
    assert stored["capture_time_provenance"] == "observed_transport"
    assert stored["requested_at"] == "2026-07-11T12:00:00.000Z"
    assert payload["schema"] == "forecast_source_forward_capture_payload_v2"
    assert payload["raw_provider_response_utf8"] == '{"hourly":{"temperature_2m":[33.0]}}'
    assert payload["derived_forecast"]["forecast_peak_c"] == 33.0


def test_forward_metrics_snapshot_rejects_credential_shaped_raw_provider_response(tmp_path, monkeypatch):
    allowed = tmp_path / "research" / "raw"
    monkeypatch.setattr(snapshot_writer, "DEFAULT_RESEARCH_RAW_ROOT", allowed)

    result = snapshot_writer.write_forward_metrics_snapshot(
        source_id="weather_com_forecast_peak",
        target_event_date_local="2026-07-12",
        station_id="ZUUU",
        metrics={"forecast_peak_c": 33.0, "source_url": "https://example.test/model"},
        observed_at="2026-07-11T12:00:00.200Z",
        requested_at="2026-07-11T12:00:00.000Z",
        response_started_at="2026-07-11T12:00:00.010Z",
        response_completed_at="2026-07-11T12:00:00.200Z",
        capture_time_provenance="observed_transport",
        raw_provider_response_utf8='{"apiKey":"secret","temperatureMax":[33.0]}',
        output_root=allowed,
        allow_research_write=True,
    )

    assert result["ok"] is False
    assert result["state_reason"] == "external_forecast_envelope_contract_failed"
    assert "raw_provider_response_contains_credential_shaped_field" in result["contract"]["issues"]
    assert not allowed.exists()
