import gzip
import json
import shutil

import pytest

from amsc_raw_history_archive import (
    archive_amsc_poll,
    attach_archive_metadata,
    audit_amsc_raw_history_archive,
    build_amsc_daily_coverage_manifest,
    load_amsc_daily_coverage_snapshot,
    persist_amsc_daily_coverage_snapshot,
    raw_text_sha256,
)


METAR_1430 = "METAR ZUUU 051430Z 18003MPS 140V210 CAVOK 32/20 Q1001 NOSIG="
METAR_1430_CORRECTED = "METAR ZUUU 051430Z 18003MPS 140V210 CAVOK 33/20 Q1001 NOSIG="
TAF = "TAF ZUUU 050904Z 0512/0612 20003MPS 6000 SCT040 TX36/0607Z TN27/0522Z="


def _sources(*raw_values):
    sources = []
    for raw_text in raw_values:
        report_type = "TAF" if raw_text.startswith("TAF ") else "METAR"
        sources.append(
            {
                "source_id": "zuuu_taf_forecast_amsc" if report_type == "TAF" else "zuuu_metar_raw_amsc",
                "station_id": "ZUUU",
                "report_type": report_type,
                "raw_text": raw_text,
                "raw_ref": raw_text,
                "valid_time_utc": "2026-07-05T14:30:00Z",
                "valid_time_local": "2026-07-05T22:30:00+08:00",
            }
        )
    return sources


def test_raw_archive_is_content_addressed_and_manifest_is_credential_free(tmp_path):
    archive_dir = tmp_path / "amsc_raw_history"
    first = archive_amsc_poll(
        archive_dir=archive_dir,
        source_payloads=_sources(METAR_1430, TAF, METAR_1430),
        poll_started_at="2026-07-05T14:30:55Z",
        response_completed_at="2026-07-05T14:31:00Z",
        response_status=200,
        request_metadata={"cccc": "ZUUU", "count": "6", "tt": "SA,FC,FT,", "token": "secret-token", "cookie": "x"},
        fetcher_version="test_fetcher_v1",
    )

    assert first["objects_written"] == 2
    assert first["objects_reused"] == 0
    assert first["raw_object_count"] == 2
    assert len(first["coverage_manifest_refs"]) == 1
    assert first["coverage_manifest_refs"][0].startswith("coverage_v2/2026/07/05/")
    assert first["guards"]["credentials_persisted"] is False

    manifest = json.loads((archive_dir / first["manifest_ref"]).read_text(encoding="utf-8"))
    assert manifest["returned_raw_sha256_list"] == sorted(manifest["returned_raw_sha256_list"])
    assert "token" not in json.dumps(manifest).lower()
    assert "cookie" not in json.dumps(manifest).lower()

    raw_sha = raw_text_sha256(METAR_1430)
    object_path = archive_dir / first["raw_objects_by_sha256"][raw_sha]["raw_object_ref"]
    with gzip.open(object_path, "rt", encoding="utf-8") as handle:
        stored = json.load(handle)
    assert stored["raw_text"] == METAR_1430
    assert stored["raw_sha256"] == raw_sha
    assert stored["first_seen_at"] == "2026-07-05T14:31:00Z"

    second = archive_amsc_poll(
        archive_dir=archive_dir,
        source_payloads=_sources(METAR_1430, TAF),
        poll_started_at="2026-07-05T15:00:55Z",
        response_completed_at="2026-07-05T15:01:00Z",
        response_status=200,
        request_metadata={"cccc": "ZUUU", "count": "6", "token": "new-secret"},
        fetcher_version="test_fetcher_v1",
    )
    assert second["objects_written"] == 0
    assert second["objects_reused"] == 2
    assert second["coverage_manifest_refs"] == first["coverage_manifest_refs"]
    assert second["raw_objects_by_sha256"][raw_sha]["first_seen_at"] == "2026-07-05T14:31:00Z"
    audit = audit_amsc_raw_history_archive(archive_dir)
    assert audit["ok"] is True
    assert audit["coverage_manifest_count"] == 1
    loaded_coverage = load_amsc_daily_coverage_snapshot(archive_dir, event_date="2026-07-05")
    assert loaded_coverage is not None
    assert loaded_coverage["coverage_sha256"] in first["coverage_manifest_refs"][0]
    assert loaded_coverage["snapshot_generated_at"] == "2026-07-05T14:31:00Z"


def test_raw_archive_preserves_corrections_and_attaches_lineage(tmp_path):
    archive_dir = tmp_path / "amsc_raw_history"
    sources = _sources(METAR_1430, METAR_1430_CORRECTED)
    for source in sources:
        source["as_of_eligible_at"] = "2026-07-05T15:01:00Z"
    result = archive_amsc_poll(
        archive_dir=archive_dir,
        source_payloads=sources,
        poll_started_at="2026-07-05T14:30:55Z",
        response_completed_at="2026-07-05T14:31:00Z",
        response_status=200,
        request_metadata={"cccc": "ZUUU", "count": "6"},
        fetcher_version="test_fetcher_v1",
    )

    assert result["objects_written"] == 2
    assert raw_text_sha256(METAR_1430) != raw_text_sha256(METAR_1430_CORRECTED)

    attached = attach_archive_metadata(sources, result)
    assert {row["raw_sha256"] for row in attached} == {
        raw_text_sha256(METAR_1430),
        raw_text_sha256(METAR_1430_CORRECTED),
    }
    assert all(row["raw_object_ref"].startswith("objects/") for row in attached)
    assert all(row["first_seen_at"] == "2026-07-05T14:31:00Z" for row in attached)
    assert all(row["as_of_eligible_at"] == "2026-07-05T14:31:00Z" for row in attached)
    assert all(row["arrival_time_quality"] == "observed_first_seen" for row in attached)
    assert all(row["transport"]["raw_archive_manifest_ref"] == result["manifest_ref"] for row in attached)


def test_raw_archive_audit_detects_object_tampering(tmp_path):
    archive_dir = tmp_path / "amsc_raw_history"
    result = archive_amsc_poll(
        archive_dir=archive_dir,
        source_payloads=_sources(METAR_1430),
        poll_started_at="2026-07-05T14:30:55Z",
        response_completed_at="2026-07-05T14:31:00Z",
        response_status=200,
        request_metadata={"cccc": "ZUUU"},
        fetcher_version="test_fetcher_v1",
    )
    raw_sha = raw_text_sha256(METAR_1430)
    object_path = archive_dir / result["raw_objects_by_sha256"][raw_sha]["raw_object_ref"]
    with gzip.open(object_path, "wt", encoding="utf-8") as handle:
        json.dump({"schema": "bad", "raw_text": "tampered", "raw_sha256": raw_sha}, handle)

    audit = audit_amsc_raw_history_archive(archive_dir)
    assert audit["ok"] is False
    assert any(issue.startswith("object_hash_invalid:") for issue in audit["issues"])


def test_daily_coverage_manifest_separates_metar_corrections_from_taf_and_late_arrival(tmp_path):
    archive_dir = tmp_path / "amsc_raw_history"
    first = "METAR ZUUU 050800Z 18003MPS CAVOK 30/20 Q1001 NOSIG="
    corrected = "METAR ZUUU 050800Z 18003MPS CAVOK 31/20 Q1001 NOSIG="
    second = "METAR ZUUU 050830Z 18003MPS CAVOK 32/20 Q1001 NOSIG="
    sources = [
        {
            "source_id": "zuuu_metar_raw_amsc",
            "station_id": "ZUUU",
            "report_type": "METAR",
            "raw_text": first,
            "valid_time_utc": "2026-07-05T08:00:00Z",
            "valid_time_local": "2026-07-05T16:00:00+08:00",
        },
        {
            "source_id": "zuuu_metar_raw_amsc",
            "station_id": "ZUUU",
            "report_type": "METAR",
            "raw_text": corrected,
            "valid_time_utc": "2026-07-05T08:00:00Z",
            "valid_time_local": "2026-07-05T16:00:00+08:00",
        },
        {
            "source_id": "zuuu_metar_raw_amsc",
            "station_id": "ZUUU",
            "report_type": "METAR",
            "raw_text": second,
            "valid_time_utc": "2026-07-05T08:30:00Z",
            "valid_time_local": "2026-07-05T16:30:00+08:00",
        },
        {
            "source_id": "zuuu_taf_forecast_amsc",
            "station_id": "ZUUU",
            "report_type": "TAF",
            "raw_text": TAF,
            "valid_time_utc": "2026-07-05T08:30:00Z",
            "valid_time_local": "2026-07-05T16:30:00+08:00",
        },
    ]
    archive_amsc_poll(
        archive_dir=archive_dir,
        source_payloads=sources,
        poll_started_at="2026-07-05T10:00:55Z",
        response_completed_at="2026-07-05T10:01:00Z",
        response_status=200,
        request_metadata={"cccc": "ZUUU"},
        fetcher_version="test_fetcher_v1",
    )

    coverage = build_amsc_daily_coverage_manifest(archive_dir, event_date="2026-07-05")

    assert coverage["event_raw_object_count"] == 3
    assert coverage["distinct_valid_time_count"] == 2
    assert coverage["max_gap_seconds"] == 1800
    assert coverage["correction_count"] == 1
    assert coverage["date_quality"]["non_metar_object_count"] == 1
    assert coverage["late_arrival_count"] == 3
    assert coverage["replay_eligibility"]["a_class_as_of_replay_eligible"] is False
    assert coverage["guards"]["writes_posterior"] is False


def test_archive_audit_detects_tampered_persisted_coverage_snapshot(tmp_path):
    archive_dir = tmp_path / "amsc_raw_history"
    result = archive_amsc_poll(
        archive_dir=archive_dir,
        source_payloads=_sources(METAR_1430),
        poll_started_at="2026-07-05T14:30:55Z",
        response_completed_at="2026-07-05T14:31:00Z",
        response_status=200,
        request_metadata={"cccc": "ZUUU"},
        fetcher_version="test_fetcher_v1",
    )
    coverage_path = archive_dir / result["coverage_manifest_refs"][0]
    coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
    coverage["coverage_sha256"] = "tampered"
    coverage_path.write_text(json.dumps(coverage), encoding="utf-8")

    audit = audit_amsc_raw_history_archive(archive_dir)

    assert audit["ok"] is False
    assert any(issue.startswith("coverage_hash_invalid:") for issue in audit["issues"])


def test_coverage_snapshot_loader_prefers_later_poll_when_correction_changes_coverage(tmp_path):
    archive_dir = tmp_path / "amsc_raw_history"
    archive_amsc_poll(
        archive_dir=archive_dir,
        source_payloads=_sources(METAR_1430),
        poll_started_at="2026-07-05T14:30:55Z",
        response_completed_at="2026-07-05T14:31:00Z",
        response_status=200,
        request_metadata={"cccc": "ZUUU"},
        fetcher_version="test_fetcher_v1",
    )
    second = archive_amsc_poll(
        archive_dir=archive_dir,
        source_payloads=_sources(METAR_1430, METAR_1430_CORRECTED),
        poll_started_at="2026-07-05T15:00:55Z",
        response_completed_at="2026-07-05T15:01:00Z",
        response_status=200,
        request_metadata={"cccc": "ZUUU"},
        fetcher_version="test_fetcher_v1",
    )

    coverage = load_amsc_daily_coverage_snapshot(archive_dir, event_date="2026-07-05")

    assert len(second["coverage_manifest_refs"]) == 1
    assert coverage is not None
    assert coverage["snapshot_generated_at"] == "2026-07-05T15:01:00Z"
    assert coverage["correction_count"] == 1
    assert len(list((archive_dir / "coverage_v2").glob("**/*.json"))) == 2


def test_controlled_coverage_v2_materialization_is_idempotent_and_preserves_late_state(tmp_path):
    archive_dir = tmp_path / "amsc_raw_history"
    archive_amsc_poll(
        archive_dir=archive_dir,
        source_payloads=_sources(METAR_1430),
        poll_started_at="2026-07-05T16:00:55Z",
        response_completed_at="2026-07-05T16:01:00Z",
        response_status=200,
        request_metadata={"cccc": "ZUUU"},
        fetcher_version="test_fetcher_v1",
    )
    shutil.rmtree(archive_dir / "coverage_v2")

    first = persist_amsc_daily_coverage_snapshot(
        archive_dir,
        event_date="2026-07-05",
        snapshot_generated_at="2026-07-06T01:00:00Z",
    )
    second = persist_amsc_daily_coverage_snapshot(
        archive_dir,
        event_date="2026-07-05",
        snapshot_generated_at="2026-07-06T02:00:00Z",
    )

    assert first["state"] == "materialized"
    assert first["snapshot_written"] is True
    assert second["state"] == "idempotent_reuse"
    assert second["snapshot_reused"] is True
    assert second["coverage_manifest_ref"] == first["coverage_manifest_ref"]
    assert len(list((archive_dir / "coverage_v2").glob("**/*.json"))) == 1
    coverage = load_amsc_daily_coverage_snapshot(archive_dir, event_date="2026-07-05")
    assert coverage is not None
    assert coverage["snapshot_writer"] == "amsc_daily_coverage_v2_materializer_v1"
    assert coverage["day_state"] == "historical_window_or_late_arrival_only"
    assert coverage["late_arrival_count"] == 1
    assert coverage["replay_eligibility"]["a_class_as_of_replay_eligible"] is False
    assert audit_amsc_raw_history_archive(archive_dir)["ok"] is True


def test_controlled_coverage_v2_materialization_rejects_missing_raw_and_wrong_schedule_station(tmp_path):
    archive_dir = tmp_path / "amsc_raw_history"

    with pytest.raises(ValueError, match="coverage_materialization_no_raw_metar"):
        persist_amsc_daily_coverage_snapshot(archive_dir, event_date="2026-07-05")
    with pytest.raises(ValueError, match="coverage_materialization_station_not_supported_by_schedule"):
        persist_amsc_daily_coverage_snapshot(archive_dir, event_date="2026-07-05", station_id="KJFK")
    with pytest.raises(ValueError, match="coverage_materialization_event_date_invalid"):
        persist_amsc_daily_coverage_snapshot(archive_dir, event_date="2026-99-99")
