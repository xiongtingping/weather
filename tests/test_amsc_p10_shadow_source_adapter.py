from __future__ import annotations

import copy

from amsc_p10_shadow_source_adapter import (
    build_amsc_p10_shadow_arrival_candidates,
    dispatch_amsc_p10_shadow_bundle,
)
from source_observation_runtime import build_source_observation


ARCHIVE_MANIFEST_REF = "manifests/2026/07/03/fixture.json"
ARCHIVE_MANIFEST_SHA256 = "fixture-archive-manifest-sha256"


def _primary(
    *,
    report_type: str = "METAR",
    valid_time: str = "2026-07-03T06:30:00Z",
    first_seen_at: str = "2026-07-03T06:31:00Z",
    response_completed_at: str = "2026-07-03T06:31:00Z",
    raw_ref: str = "fixture://amsc-p10-primary",
    raw_object_ref: str = "objects/aa/fixture.json.gz",
) -> dict:
    return build_source_observation(
        source_id="zuuu_metar_raw_amsc",
        station_id="ZUUU",
        event_date="2026-07-03",
        valid_time=valid_time,
        fetched_at=response_completed_at,
        response_completed_at=response_completed_at,
        temperature_c=30.0,
        dewpoint_c=22.0,
        raw_ref=raw_ref,
        raw_payload={"report_type": report_type, "raw_ref": raw_ref},
        payload={
            "report_type": report_type,
            "arrival_time_quality": "observed_first_seen",
            "first_seen_at": first_seen_at,
            "as_of_eligible_at": first_seen_at,
            "raw_object_ref": raw_object_ref,
            "transport": {
                "raw_archive_manifest_ref": ARCHIVE_MANIFEST_REF,
                "raw_archive_manifest_sha256": ARCHIVE_MANIFEST_SHA256,
            },
        },
    ).as_dict()


def _bundle(*observations: dict) -> dict:
    archive_objects = {
        row["raw_sha256"]: {
            "raw_sha256": row["raw_sha256"],
            "raw_object_ref": row["payload"].get("raw_object_ref", ""),
            "first_seen_at": row["payload"].get("first_seen_at", ""),
            "arrival_time_quality": row["payload"].get("arrival_time_quality", ""),
        }
        for row in observations
        if row.get("source_id") == "zuuu_metar_raw_amsc"
    }
    return {
        "schema": "amsc_zuuu_metar_taf_observation_bundle_v1",
        "read_only": True,
        "station_id": "ZUUU",
        "fetched_at": "2026-07-03T06:40:00Z",
        "source_observations": list(observations),
        "raw_history_archive": {
            "manifest_ref": ARCHIVE_MANIFEST_REF,
            "manifest_sha256": ARCHIVE_MANIFEST_SHA256,
            "raw_objects_by_sha256": archive_objects,
        },
    }


def test_adapter_splits_only_primary_reports_in_persisted_t1_order_without_resetting_t1() -> None:
    later = _primary(
        report_type="SPECI",
        valid_time="2026-07-03T07:00:00Z",
        first_seen_at="2026-07-03T07:01:00Z",
        response_completed_at="2026-07-03T07:10:00Z",
        raw_ref="fixture://amsc-p10-later",
        raw_object_ref="objects/bb/later.json.gz",
    )
    earlier = _primary(
        valid_time="2026-07-03T06:30:00Z",
        first_seen_at="2026-07-03T06:31:00Z",
        # The retrieval response is deliberately later than immutable T1.
        response_completed_at="2026-07-03T06:40:00Z",
        raw_ref="fixture://amsc-p10-earlier",
        raw_object_ref="objects/aa/earlier.json.gz",
    )
    taf = build_source_observation(
        source_id="zuuu_taf_forecast_amsc",
        station_id="ZUUU",
        event_date="2026-07-03",
        valid_time="2026-07-03T06:00:00Z",
        fetched_at="2026-07-03T06:40:00Z",
        forecast_peak_c=34.0,
        raw_ref="fixture://taf",
        raw_payload={"report_type": "TAF"},
        payload={"report_type": "TAF"},
    ).as_dict()
    awc = build_source_observation(
        source_id="zuuu_metar_raw_awc",
        station_id="ZUUU",
        event_date="2026-07-03",
        valid_time="2026-07-03T06:30:00Z",
        fetched_at="2026-07-03T06:40:00Z",
        temperature_c=30.0,
        raw_ref="fixture://awc",
        raw_payload={"report_type": "METAR"},
        payload={"report_type": "METAR"},
    ).as_dict()

    result = build_amsc_p10_shadow_arrival_candidates(_bundle(later, taf, awc, earlier))

    assert result["ok"] is True
    assert result["candidate_count"] == 2
    assert [item["first_transport_seen_at"] for item in result["arrival_candidates"]] == [
        "2026-07-03T06:31:00Z",
        "2026-07-03T07:01:00Z",
    ]
    first = result["arrival_candidates"][0]
    assert first["response_completed_at"] == "2026-07-03T06:40:00Z"
    assert first["first_transport_seen_at"] != first["response_completed_at"]
    assert {item["source_id"] for item in result["ignored_non_primary_rows"]} == {
        "zuuu_taf_forecast_amsc",
        "zuuu_metar_raw_awc",
    }
    assert result["boundary"]["acquisition_batch_is_not_an_arrival"] is True
    assert result["boundary"]["persistent_physical_deduplication_owned_by_journal"] is True


def test_adapter_requires_raw_object_lineage_and_never_fabricates_an_arrival_from_poll_timestamp() -> None:
    without_archive = _primary(raw_object_ref="")
    result = build_amsc_p10_shadow_arrival_candidates(_bundle(without_archive))

    assert result["ok"] is True
    assert result["candidate_count"] == 0
    assert result["rejected_primary_rows"][0]["blockers"] == [
        "amsc_p10_shadow_adapter_raw_object_lineage_missing"
    ]
    assert result["state"] == "ready_with_rejections"


def test_adapter_rejects_t1_or_manifest_that_does_not_match_the_raw_archive_receipt() -> None:
    source = _primary()
    tampered = _bundle(source)
    tampered["source_observations"][0]["payload"]["first_seen_at"] = "2026-07-03T06:35:00Z"

    result = build_amsc_p10_shadow_arrival_candidates(tampered)

    assert result["candidate_count"] == 0
    assert "amsc_p10_shadow_adapter_archive_first_seen_mismatch" in result["rejected_primary_rows"][0]["blockers"]


def test_adapter_deduplicates_identical_row_only_inside_a_batch_and_leaves_cross_batch_to_journal() -> None:
    source = _primary()
    result = build_amsc_p10_shadow_arrival_candidates(_bundle(source, copy.deepcopy(source)))

    assert result["candidate_count"] == 1
    assert result["duplicate_in_bundle_source_observation_ids"] == [source["source_observation_id"]]
    assert result["boundary"]["persistent_physical_deduplication_owned_by_journal"] is True


class _Host:
    def __init__(self) -> None:
        self.received: list[dict] = []

    def process_source_observation(self, *, source_observation: dict) -> dict:
        self.received.append(copy.deepcopy(source_observation))
        return {
            "ok": True,
            "state": "dispatched",
            "shadow_only": True,
            "places_orders": False,
        }


def test_adapter_dispatches_each_candidate_synchronously_to_host_not_the_poll_batch() -> None:
    host = _Host()
    first = _primary(raw_ref="fixture://first", raw_object_ref="objects/aa/first.json.gz")
    second = _primary(
        report_type="SPECI",
        valid_time="2026-07-03T07:00:00Z",
        first_seen_at="2026-07-03T07:01:00Z",
        response_completed_at="2026-07-03T07:01:00Z",
        raw_ref="fixture://second",
        raw_object_ref="objects/bb/second.json.gz",
    )

    result = dispatch_amsc_p10_shadow_bundle(_bundle(second, first), host=host)

    assert result["ok"] is True
    assert result["dispatch_count"] == 2
    assert [row["payload"]["first_seen_at"] for row in host.received] == [
        "2026-07-03T06:31:00Z",
        "2026-07-03T07:01:00Z",
    ]
    assert result["boundary"]["network_acquisition"] is False
    assert result["boundary"]["production_archive_write"] is False
    assert result["boundary"]["clob"] is False


class _DeadlineThenSuccessHost:
    def __init__(self) -> None:
        self.received: list[str] = []

    def process_source_observation(self, *, source_observation: dict) -> dict:
        self.received.append(source_observation["source_observation_id"])
        if len(self.received) == 1:
            return {
                "ok": False,
                "state": "dispatched_or_blocked",
                "result": {"journal": {"state": "deadline_blocked"}},
            }
        return {"ok": True, "state": "dispatched"}


def test_terminal_deadline_block_does_not_hold_a_later_physical_arrival_but_retryable_failure_would() -> None:
    host = _DeadlineThenSuccessHost()
    first = _primary(raw_ref="fixture://deadline", raw_object_ref="objects/aa/deadline.json.gz")
    second = _primary(
        report_type="SPECI",
        valid_time="2026-07-03T07:00:00Z",
        first_seen_at="2026-07-03T07:01:00Z",
        response_completed_at="2026-07-03T07:01:00Z",
        raw_ref="fixture://after-deadline",
        raw_object_ref="objects/bb/after-deadline.json.gz",
    )

    result = dispatch_amsc_p10_shadow_bundle(_bundle(first, second), host=host)

    assert result["ok"] is False
    assert result["state"] == "dispatched_with_terminal_deadline_block"
    assert host.received == [first["source_observation_id"], second["source_observation_id"]]
    assert result["dispatches"][0]["terminal_deadline_blocked"] is True
