from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from amsc_primary_raw_coverage_reader import load_amsc_primary_raw_coverage_observations
from amsc_raw_history_archive import archive_amsc_poll, load_amsc_daily_coverage_snapshot
from zuuu_metar_reporting_schedule import expected_regular_metar_slots_utc


DAY = "2026-07-03"


def _raw(valid_time: str, temperature: int) -> dict:
    valid = datetime.fromisoformat(valid_time.replace("Z", "+00:00"))
    return {
        "source_id": "zuuu_metar_raw_amsc",
        "station_id": "ZUUU",
        "report_type": "METAR",
        "raw_text": f"METAR ZUUU {valid.day:02d}{valid.hour:02d}{valid.minute:02d}Z 00000MPS CAVOK {temperature:02d}/20 Q1000=",
        "valid_time_utc": valid_time,
        "valid_time_local": valid.astimezone().isoformat(),
    }


def test_raw_coverage_reader_is_complete_without_archive_db_fact_rows(tmp_path) -> None:
    archive_dir = tmp_path / "amsc"
    slots = expected_regular_metar_slots_utc(DAY)
    sources = [_raw(slot, 20 + index % 10) for index, slot in enumerate(slots)]
    latest_valid = datetime.fromisoformat(slots[-1].replace("Z", "+00:00"))
    archive_amsc_poll(
        archive_dir=archive_dir,
        source_payloads=sources,
        poll_started_at=(latest_valid + timedelta(minutes=1)).isoformat(),
        response_completed_at=(latest_valid + timedelta(minutes=2)).isoformat(),
        response_status=200,
        request_metadata={"cccc": "ZUUU"},
        fetcher_version="test",
    )
    coverage = load_amsc_daily_coverage_snapshot(archive_dir, event_date=DAY)

    result = load_amsc_primary_raw_coverage_observations(
        archive_dir,
        event_date=DAY,
        coverage_manifest=coverage or {},
    )

    assert result["ok"] is True
    assert result["source_observation_count"] == 44
    assert max(row["temperature_c"] for row in result["source_observations"]) == 29.0
    assert result["boundary"]["reads_archive_db"] is False
    assert all(row["raw_manifest_sha256"] for row in result["source_observations"])


def test_raw_coverage_reader_rejects_manifest_membership_loss(tmp_path) -> None:
    archive_dir = tmp_path / "amsc"
    slot = expected_regular_metar_slots_utc(DAY)[0]
    result = archive_amsc_poll(
        archive_dir=archive_dir,
        source_payloads=[_raw(slot, 20)],
        poll_started_at="2026-07-03T00:01:00Z",
        response_completed_at="2026-07-03T00:02:00Z",
        response_status=200,
        request_metadata={"cccc": "ZUUU"},
        fetcher_version="test",
    )
    coverage = load_amsc_daily_coverage_snapshot(archive_dir, event_date=DAY)
    manifest_path = archive_dir / result["manifest_ref"]
    manifest_path.write_text(json.dumps({"schema": "bad"}), encoding="utf-8")

    read = load_amsc_primary_raw_coverage_observations(
        archive_dir,
        event_date=DAY,
        coverage_manifest=coverage or {},
    )

    assert read["ok"] is False
    assert read["blocked"][0]["block_reasons"] == ["raw_object_signed_poll_membership_missing"]


def test_raw_coverage_reader_rejects_coverage_content_identity_drift(tmp_path) -> None:
    archive_dir = tmp_path / "amsc"
    slot = expected_regular_metar_slots_utc(DAY)[0]
    archive_amsc_poll(
        archive_dir=archive_dir,
        source_payloads=[_raw(slot, 20)],
        poll_started_at="2026-07-03T00:01:00Z",
        response_completed_at="2026-07-03T00:02:00Z",
        response_status=200,
        request_metadata={"cccc": "ZUUU"},
        fetcher_version="test",
    )
    coverage = dict(load_amsc_daily_coverage_snapshot(archive_dir, event_date=DAY) or {})
    coverage["late_arrival_count"] = 0

    read = load_amsc_primary_raw_coverage_observations(
        archive_dir,
        event_date=DAY,
        coverage_manifest=coverage,
    )

    assert read["ok"] is False
    assert "raw_coverage_reader:coverage_sha256_mismatch" in read["block_reasons"]
