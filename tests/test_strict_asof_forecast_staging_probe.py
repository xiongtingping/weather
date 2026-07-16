from __future__ import annotations

import gzip
import json

from external_forecast_snapshot_runtime import build_external_forecast_raw_envelope
from scripts.strict_asof_forecast_staging_probe import build_strict_asof_forecast_staging_probe


def _write_tier_a(root, source_id: str, event_date: str) -> None:
    payload = {
        "schema": "forecast_source_forward_capture_payload_v2",
        "derived_forecast": {
            "source_id": source_id,
            "target_event_date_local": event_date,
            "forecast_peak_c": 33.0,
            "forecast_peak_valid_time": f"{event_date}T16:00:00Z",
        },
        "raw_provider_response_utf8": "{}",
    }
    envelope = build_external_forecast_raw_envelope(
        source_id=source_id,
        response_body_utf8=json.dumps(payload),
        requested_at="2026-07-11T00:00:00Z",
        response_started_at="2026-07-11T00:00:00.010Z",
        response_completed_at="2026-07-11T00:00:00.200Z",
        http_status=200,
        parser_version="forward-capture-v2",
        source_event_id=f"forward_{source_id}_{event_date}",
        capture_time_provenance="observed_transport",
        response_headers_observed=True,
    )
    path = root / source_id / f"{source_id}.json.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        json.dump(envelope, handle)


def test_in_memory_probe_proves_complete_tier_a_day_can_reach_canonical_forecast_revisions(tmp_path) -> None:
    sources = ("ecmwf_model_peak", "gfs_model_peak", "icon_model_peak")
    for source_id in sources:
        _write_tier_a(tmp_path, source_id, "2026-07-12")

    result = build_strict_asof_forecast_staging_probe(
        root=tmp_path,
        event_date="2026-07-12",
        station_id="ZUUU",
        required_source_ids=sources,
    )

    assert result["ok"] is True, result
    assert result["state"] == "proved"
    assert result["staged_source_observation_count"] == 3
    assert result["forecast_revision_result"]["ok"] is True
    assert [row["source_id"] for row in result["selected"]] == list(sources)
    assert result["guards"]["writes_archive_db"] is False
    assert result["guards"]["staging_backend"] == "sqlite_memory_isolated_shadow_only"


def test_in_memory_probe_fails_closed_when_complete_source_day_is_missing(tmp_path) -> None:
    _write_tier_a(tmp_path, "ecmwf_model_peak", "2026-07-12")

    result = build_strict_asof_forecast_staging_probe(
        root=tmp_path,
        event_date="2026-07-12",
        required_source_ids=("ecmwf_model_peak", "gfs_model_peak", "icon_model_peak"),
    )

    assert result["ok"] is False
    assert sorted(result["missing_source_ids"]) == ["gfs_model_peak", "icon_model_peak"]
