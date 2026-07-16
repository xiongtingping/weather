from __future__ import annotations

from amsc_raw_history_archive import archive_amsc_poll
from scripts.amsc_taf_strict_asof_capture_probe import build_amsc_taf_strict_asof_capture_probe


TAF = "TAF ZUUU 050904Z 0512/0612 20003MPS 6000 SCT040 TX36/0607Z TN27/0522Z="


def _source() -> dict:
    return {
        "source_id": "zuuu_taf_forecast_amsc",
        "station_id": "ZUUU",
        "report_type": "TAF",
        "raw_text": TAF,
        "valid_time_utc": "2026-07-06T07:00:00Z",
        "valid_time_local": "2026-07-06T15:00:00+08:00",
    }


def test_read_only_probe_counts_only_first_transport_eligible_taf(tmp_path) -> None:
    archive_dir = tmp_path / "archive"
    archive_amsc_poll(
        archive_dir=archive_dir,
        source_payloads=[_source()],
        poll_started_at="2026-07-05T14:30:55Z",
        response_started_at="2026-07-05T14:30:56Z",
        response_headers_observed=True,
        response_completed_at="2026-07-05T14:31:00Z",
        response_status=200,
        request_metadata={"cccc": "ZUUU"},
        fetcher_version="test-fetcher",
    )

    report = build_amsc_taf_strict_asof_capture_probe(archive_dir)

    assert report["ok"] is True
    assert report["taf_raw_object_count"] == 1
    assert report["strict_training_eligible_count"] == 1
    assert report["rows"][0]["event_date"] == "2026-07-06"
    assert report["guards"]["writes_archive_db"] is False


def test_read_only_probe_keeps_late_or_old_transport_taf_blocked(tmp_path) -> None:
    archive_dir = tmp_path / "archive"
    archive_amsc_poll(
        archive_dir=archive_dir,
        source_payloads=[_source()],
        poll_started_at="2026-07-07T14:30:55Z",
        response_started_at="2026-07-07T14:30:56Z",
        response_headers_observed=True,
        response_completed_at="2026-07-07T14:31:00Z",
        response_status=200,
        request_metadata={"cccc": "ZUUU"},
        fetcher_version="test-fetcher",
    )

    report = build_amsc_taf_strict_asof_capture_probe(archive_dir)

    assert report["strict_training_eligible_count"] == 0
    assert report["rows"][0]["state"] == "blocked"
    assert "amsc_taf_first_seen_after_forecast_peak" in report["rows"][0]["block_reasons"]
