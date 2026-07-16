from __future__ import annotations

import gzip
import json

from amsc_raw_history_archive import archive_amsc_poll, raw_text_sha256
from amsc_taf_strict_asof_capture_runtime import (
    build_amsc_taf_strict_asof_capture_proposal,
    persist_amsc_taf_strict_asof_capture_proposal,
)
from strict_asof_forecast_capture_runtime import TIER_A, TIER_C


TAF = "TAF ZUUU 050904Z 0512/0612 20003MPS 6000 SCT040 TX36/0607Z TN27/0522Z="


def _source() -> dict:
    return {
        "source_id": "zuuu_taf_forecast_amsc",
        "station_id": "ZUUU",
        "report_type": "TAF",
        "raw_text": TAF,
        "raw_ref": TAF,
        "valid_time_utc": "2026-07-06T07:00:00Z",
        "valid_time_local": "2026-07-06T15:00:00+08:00",
    }


def _load_receipt(archive_dir, result):
    raw_sha = raw_text_sha256(TAF)
    object_ref = result["raw_objects_by_sha256"][raw_sha]["raw_object_ref"]
    with gzip.open(archive_dir / object_ref, "rt", encoding="utf-8") as handle:
        raw_object = json.load(handle)
    manifest = json.loads((archive_dir / result["manifest_ref"]).read_text(encoding="utf-8"))
    return raw_object, object_ref, manifest


def test_first_transport_receipt_builds_tier_a_taf_capture_before_forecast_peak(tmp_path) -> None:
    archive_dir = tmp_path / "archive"
    result = archive_amsc_poll(
        archive_dir=archive_dir,
        source_payloads=[_source()],
        poll_started_at="2026-07-05T14:30:55Z",
        response_started_at="2026-07-05T14:30:56Z",
        response_headers_observed=True,
        response_completed_at="2026-07-05T14:31:00Z",
        response_status=200,
        request_metadata={"cccc": "ZUUU", "count": "6"},
        fetcher_version="test-fetcher",
    )
    raw_object, object_ref, manifest = _load_receipt(archive_dir, result)

    proposal = build_amsc_taf_strict_asof_capture_proposal(
        raw_object=raw_object,
        raw_object_ref=object_ref,
        first_poll_manifest=manifest,
    )

    assert proposal["ok"] is True
    assert proposal["capture"]["evidence_tier"] == TIER_A
    assert proposal["capture"]["strict_training_eligible"] is True
    assert proposal["normalized_forecast"]["forecast_peak_c"] == 36.0
    assert proposal["normalized_forecast"]["forecast_peak_valid_time"] == "2026-07-06T07:00:00Z"
    assert proposal["normalized_forecast"]["raw_object_sha256"] == raw_text_sha256(TAF)
    assert proposal["raw_envelope"]["response_sha256"] != raw_text_sha256(TAF)
    assert proposal["boundary"]["writes_archive_db"] is False
    dry = persist_amsc_taf_strict_asof_capture_proposal(proposal)
    assert dry["ok"] is True
    assert dry["state"] == "dry_run"
    assert dry["writes_research_files"] is False


def test_late_first_seen_taf_is_demoted_and_cannot_become_tier_a_history(tmp_path) -> None:
    archive_dir = tmp_path / "archive"
    result = archive_amsc_poll(
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
    raw_object, object_ref, manifest = _load_receipt(archive_dir, result)

    proposal = build_amsc_taf_strict_asof_capture_proposal(
        raw_object=raw_object,
        raw_object_ref=object_ref,
        first_poll_manifest=manifest,
    )

    assert proposal["ok"] is False
    assert "amsc_taf_first_seen_after_forecast_peak" in proposal["block_reasons"]
    assert proposal["capture"] == {}


def test_missing_response_start_or_later_refetch_receipt_is_rejected(tmp_path) -> None:
    archive_dir = tmp_path / "archive"
    first = archive_amsc_poll(
        archive_dir=archive_dir,
        source_payloads=[_source()],
        poll_started_at="2026-07-05T14:30:55Z",
        response_completed_at="2026-07-05T14:31:00Z",
        response_status=200,
        request_metadata={"cccc": "ZUUU"},
        fetcher_version="test-fetcher",
    )
    raw_object, object_ref, missing_start_manifest = _load_receipt(archive_dir, first)
    missing_start = build_amsc_taf_strict_asof_capture_proposal(
        raw_object=raw_object,
        raw_object_ref=object_ref,
        first_poll_manifest=missing_start_manifest,
    )
    assert missing_start["ok"] is False
    assert "amsc_taf_first_manifest_response_start_not_observed" in missing_start["block_reasons"]

    later = archive_amsc_poll(
        archive_dir=archive_dir,
        source_payloads=[_source()],
        poll_started_at="2026-07-05T15:00:55Z",
        response_started_at="2026-07-05T15:00:56Z",
        response_completed_at="2026-07-05T15:01:00Z",
        response_status=200,
        request_metadata={"cccc": "ZUUU"},
        fetcher_version="test-fetcher",
    )
    raw_object, object_ref, later_manifest = _load_receipt(archive_dir, later)
    later_receipt = build_amsc_taf_strict_asof_capture_proposal(
        raw_object=raw_object,
        raw_object_ref=object_ref,
        first_poll_manifest=later_manifest,
    )
    assert later_receipt["ok"] is False
    assert "amsc_taf_first_manifest_not_object_first_arrival" in later_receipt["block_reasons"]
    assert later_receipt["capture"] == {}
