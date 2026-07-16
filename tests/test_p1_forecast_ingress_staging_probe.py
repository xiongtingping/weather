from __future__ import annotations

import gzip
import json

from external_forecast_snapshot_runtime import build_external_forecast_raw_envelope
from scripts.p1_forecast_ingress_staging_probe import build_p1_forecast_ingress_staging_probe


def _write_capture(root, source_id: str, peak: float, valid_time: str) -> None:
    payload = {
        "schema": "forecast_source_forward_capture_payload_v2",
        "derived_forecast": {
            "source_id": source_id,
            "target_event_date_local": "2026-07-12",
            "forecast_peak_c": peak,
            "hourly_curve": [{"valid_time_local": valid_time, "temperature_c": peak}],
        },
        "raw_provider_response_utf8": '{"hourly":{"temperature_2m":[33.0]}}',
    }
    envelope = build_external_forecast_raw_envelope(
        source_id=source_id,
        response_body_utf8=json.dumps(payload),
        requested_at="2026-07-11T00:00:00.000Z",
        response_started_at="2026-07-11T00:00:00.010Z",
        response_completed_at="2026-07-11T00:00:00.200Z",
        http_status=200,
        parser_version="forward-capture-v2",
        source_event_id=f"forward_{source_id}_2026-07-12",
        capture_time_provenance="observed_transport",
        response_headers_observed=True,
    )
    path = root / source_id / "capture.json.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(envelope, handle)


def test_memory_probe_proves_raw_tier_a_capture_to_latest_visible_forecast_revisions(tmp_path) -> None:
    _write_capture(tmp_path, "ecmwf", 33.0, "2026-07-12T15:00:00+08:00")
    _write_capture(tmp_path, "gfs", 32.0, "2026-07-12T14:00:00+08:00")

    result = build_p1_forecast_ingress_staging_probe(
        root=tmp_path,
        event_date="2026-07-12",
        station_id="ZUUU",
        target_identity="highest-temperature-in-chengdu-on-july-12-2026",
    )

    assert result["ok"] is True, result
    assert result["state"] == "proved"
    assert result["eligible_count"] == 2
    assert result["loaded_source_observation_count"] == 2
    revisions = result["forecast_revision_result"]
    assert revisions["ok"] is True
    assert [row["source_id"] for row in revisions["source_revisions"]] == ["ecmwf", "gfs"]
    assert result["guards"]["writes_archive_db"] is False
    assert result["guards"]["creates_posterior"] is False


def test_memory_probe_fails_closed_without_target_day_tier_a_capture(tmp_path) -> None:
    result = build_p1_forecast_ingress_staging_probe(root=tmp_path, event_date="2026-07-12")

    assert result["ok"] is False
    assert result["block_reasons"] == ["tier_a_forecast_proposals_missing_for_event_date"]
