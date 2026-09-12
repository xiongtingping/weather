from __future__ import annotations

import copy
import gzip
import json

from amsc_p10_shadow_bundle_loader import (
    FORWARD_NEW_ONLY,
    REPLAY_ALL,
    load_amsc_p10_shadow_bundle,
)
from amsc_p10_shadow_source_adapter import build_amsc_p10_shadow_arrival_candidates
from amsc_raw_history_archive import archive_amsc_poll
from scripts.fetch_zuuu_metar_taf import (
    build_amsc_observation_bundle,
    source_payloads_from_amsc_payload,
)


PAYLOAD = {
    "code": 200,
    "data": [
        {"cccc": "ZUUU", "content": "METAR ZUUU 051430Z 18003MPS CAVOK 32/20 Q1001 NOSIG="},
        {"cccc": "ZUUU", "content": "SPECI ZUUU 051431Z 18003MPS CAVOK 32/20 Q1001="},
        {"cccc": "ZUUU", "content": "TAF ZUUU 050904Z 0512/0612 20003MPS 6000 SCT040 TX36/0607Z TN27/0522Z="},
    ],
}


def _bundle(archive_dir, *, response_completed_at: str, source_payloads=None) -> dict:
    sources = source_payloads or source_payloads_from_amsc_payload(
        PAYLOAD,
        fetched_at=response_completed_at,
        duration_ms=321.0,
    )
    archived = archive_amsc_poll(
        archive_dir=archive_dir,
        source_payloads=sources,
        poll_started_at="2026-07-05T14:30:55Z",
        response_completed_at=response_completed_at,
        response_status=200,
        request_metadata={"cccc": "ZUUU", "count": "6"},
        fetcher_version="test_fetcher_v1",
    )
    return build_amsc_observation_bundle(
        PAYLOAD,
        fetched_at=response_completed_at,
        duration_ms=321.0,
        source_payloads=sources,
        raw_history_archive=archived,
    )


def test_forward_loader_uses_only_reports_first_seen_in_exact_manifest(tmp_path) -> None:
    bundle = _bundle(tmp_path, response_completed_at="2026-07-05T14:31:00Z")

    loaded = load_amsc_p10_shadow_bundle(
        bundle,
        raw_archive_dir=tmp_path,
        mode=FORWARD_NEW_ONLY,
    )

    assert loaded["ok"] is True
    assert loaded["selected_primary_count"] == 2
    assert loaded["ignored_non_primary_source_observation_ids"]
    selection = build_amsc_p10_shadow_arrival_candidates(loaded["bundle"])
    assert selection["ok"] is True
    assert selection["candidate_count"] == 2
    assert all(
        row["first_transport_seen_at"] == "2026-07-05T14:31:00Z"
        for row in selection["arrival_candidates"]
    )
    assert loaded["boundary"]["t1_synthesized_from_file_read"] is False


def test_forward_loader_excludes_preexisting_reports_but_replay_keeps_original_t1(tmp_path) -> None:
    first = _bundle(tmp_path, response_completed_at="2026-07-05T14:31:00Z")
    source_payloads = source_payloads_from_amsc_payload(
        PAYLOAD,
        fetched_at="2026-07-05T15:01:00Z",
        duration_ms=111.0,
    )
    second = _bundle(
        tmp_path,
        response_completed_at="2026-07-05T15:01:00Z",
        source_payloads=source_payloads,
    )

    forward = load_amsc_p10_shadow_bundle(second, raw_archive_dir=tmp_path, mode=FORWARD_NEW_ONLY)
    replay = load_amsc_p10_shadow_bundle(second, raw_archive_dir=tmp_path, mode=REPLAY_ALL)

    assert first["raw_history_archive"]["objects_written"] == 3
    assert forward["ok"] is True
    assert forward["selected_primary_count"] == 0
    assert len(forward["excluded_preexisting_primary_source_observation_ids"]) == 2
    assert replay["ok"] is True
    assert replay["selected_primary_count"] == 2
    assert {row["payload"]["first_seen_at"] for row in replay["bundle"]["source_observations"]} == {
        "2026-07-05T14:31:00Z"
    }


def test_loader_rejects_tampered_raw_object_without_replacing_t1(tmp_path) -> None:
    bundle = _bundle(tmp_path, response_completed_at="2026-07-05T14:31:00Z")
    primary = next(row for row in bundle["source_observations"] if row["source_id"] == "zuuu_metar_raw_amsc")
    raw_path = tmp_path / primary["payload"]["raw_object_ref"]
    with gzip.open(raw_path, "wt", encoding="utf-8") as handle:
        json.dump({"schema": "tampered", "raw_text": "bad", "raw_sha256": primary["raw_sha256"]}, handle)

    loaded = load_amsc_p10_shadow_bundle(bundle, raw_archive_dir=tmp_path, mode=FORWARD_NEW_ONLY)

    assert loaded["ok"] is True
    assert loaded["selected_primary_count"] == 1
    assert any(
        "amsc_p10_loader_raw_object_schema_invalid" in row["blockers"]
        for row in loaded["rejected_primary_rows"]
    )
