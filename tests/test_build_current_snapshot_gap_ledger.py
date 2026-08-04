from __future__ import annotations

import json
import os

import pytest

from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.station_registry import coordinate_registry_manifest
from scripts.build_current_snapshot_gap_ledger import build


EVENT_DATE = "2026-08-03"
AS_OF = "2026-08-03T02:00:00Z"
BUCKETS = ("NIGHT", "MORNING", "AFTERNOON", "EVENING")


def _write_hashed(path, value):
    identity = dict(value)
    payload = {**identity, "content_sha256": content_sha256(identity)}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path, payload


def _inbox(tmp_path, *, schema: str, packets: list[dict], name: str):
    return _write_hashed(tmp_path / name, {
        "schema": "native_ensemble_forward_inbox_v1",
        "packets": packets,
    })[0]


def _p1_pair(tmp_path):
    stations = coordinate_registry_manifest()["station_ids"]
    audit_scopes = [
        {"station_id": station, "local_time_bucket": bucket}
        for station in stations for bucket in BUCKETS
    ]
    audit_path, audit = _write_hashed(tmp_path / "p1-audit.json", {
        "schema": "bayes_basket_target_p1_trajectory_45station_audit_v1",
        "as_of_utc": "2026-08-02T00:00:00.000Z",
        "formal_eligible_scope_count": 0,
        "scope_count": 180,
        "scopes": audit_scopes,
    })
    scopes = []
    for station in stations:
        for bucket in BUCKETS:
            scopes.append({
                "station_id": station,
                "local_time_bucket": bucket,
                "repair_steps": [{
                    "blocker": "arrival_lineage_missing",
                    "action": "CAPTURE_FORWARD_NATIVE_HOURLY_CURVE_WITH_ARRIVAL",
                    "required_evidence": "same-capture official arrival receipt",
                }],
            })
    repair_path, _ = _write_hashed(tmp_path / "p1-repair.json", {
        "schema": "bayes_basket_target_p1_trajectory_repair_plan_v1",
        "as_of_utc": "2026-08-02T00:00:00.000Z",
        "audit_content_sha256": audit["content_sha256"],
        "scope_count": 180,
        "scopes": scopes,
        "usable_candidate_pointer_count": 0,
        "safe_write_state": "NO_SAFE_FORMAL_WRITE_REQUIRED",
    })
    return audit_path, repair_path


def test_current_ledger_separates_stale_official_from_missing_fallback_receipts(tmp_path):
    stations = coordinate_registry_manifest()["station_ids"]
    official = [{
        "schema": "official_native_ensemble_forward_packet_v1",
        "station_id": station,
        "event_date": EVENT_DATE,
        "received_at_utc": "2026-08-03T00:00:00Z",
        "snapshot_content_sha256": f"official-{index}",
    } for index, station in enumerate(stations)]
    republisher = [{
        "schema": "open_meteo_native_ensemble_forward_packet_v1",
        "station_id": station,
        "event_date": EVENT_DATE,
        "received_at_utc": None,
        "packet_content_sha256": f"republisher-{index}",
    } for index, station in enumerate(stations[:36])]
    official_path = _inbox(tmp_path, schema="official", packets=official, name="official.json")
    republisher_path = _inbox(tmp_path, schema="republisher", packets=republisher, name="republisher.json")
    receipt_path, _ = _write_hashed(tmp_path / "503.json", {
        "schema": "official_native_http_capture_receipt_v1",
        "station_id": stations[0],
        "event_date": EVENT_DATE,
        "http_status": 503,
    })
    audit_path, repair_path = _p1_pair(tmp_path)

    ledger = build(
        official_inbox=official_path,
        republisher_inbox=republisher_path,
        as_of_utc=AS_OF,
        max_age_seconds=3600,
        event_date=EVENT_DATE,
        official_capture_receipts=[receipt_path],
        p1_audit=audit_path,
        p1_repair=repair_path,
    )

    assert ledger["summary"]["full_45_station_coverage_proven"] is True
    assert ledger["summary"]["official"] == {
        "packet_present_count": 45,
        "fresh_count": 0,
        "stale_count": 45,
        "timestamp_missing_count": 0,
        "missing_count": 0,
        "retained_http_503_count": 1,
    }
    assert ledger["summary"]["fallback"] == {
        "packet_present_count": 36,
        "fresh_count": 0,
        "stale_count": 0,
        "timestamp_missing_count": 36,
        "missing_count": 9,
    }
    assert ledger["summary"]["both_currently_unavailable_count"] == 45
    assert ledger["stations"][0]["official"]["http_503_evidence"]["state"] == "OFFICIAL_HTTP_503_RETAINED"
    assert ledger["stations"][1]["official"]["http_503_evidence"]["state"] == "OFFICIAL_CAPTURE_RECEIPT_MISSING_OR_NON503"
    assert ledger["stations"][0]["fallback"]["reason"] == "FALLBACK_RECEIPT_TIMESTAMP_MISSING"
    assert ledger["stations"][40]["fallback"]["reason"] == "FALLBACK_PACKET_MISSING"
    assert ledger["p1"]["p1_rebuild_state"] == "BLOCKED_NO_APPENDABLE_ROOT"
    assert ledger["p1"]["old_root_rebind_authorized"] is False
    assert ledger["p1"]["appendable_queue_count"] == 180
    assert all(item["old_root_rebind_authorized"] is False for item in ledger["p1"]["appendable_queue"])
    assert ledger["boundary"]["writes_p1"] is False


def test_fallback_freshness_uses_aggregator_snapshot_clock(tmp_path):
    station = coordinate_registry_manifest()["station_ids"][0]
    official_path = _inbox(tmp_path, schema="official", packets=[], name="official.json")
    republisher_path = _inbox(tmp_path, schema="republisher", packets=[{
        "schema": "open_meteo_native_ensemble_forward_packet_v1",
        "station_id": station,
        "event_date": EVENT_DATE,
        "received_at_utc": None,
        "aggregator_snapshot_at_utc": "2026-08-03T01:00:00Z",
        "packet_content_sha256": "republisher-snapshot-clock",
    }], name="republisher.json")
    audit_path, repair_path = _p1_pair(tmp_path)
    ledger = build(
        official_inbox=official_path,
        republisher_inbox=republisher_path,
        as_of_utc="2026-08-03T02:00:00Z",
        max_age_seconds=3600,
        event_date=EVENT_DATE,
        p1_audit=audit_path,
        p1_repair=repair_path,
    )
    assert ledger["summary"]["fallback"]["fresh_count"] == 1
    assert ledger["stations"][0]["fallback"]["received_at_utc"] == "2026-08-03T01:00:00.000Z"


def test_current_ledger_refuses_mixed_p1_audit_and_repair_evidence(tmp_path):
    stations = coordinate_registry_manifest()["station_ids"]
    packet = {
        "schema": "official_native_ensemble_forward_packet_v1",
        "station_id": stations[0],
        "event_date": EVENT_DATE,
        "received_at_utc": "2026-08-03T01:59:00Z",
        "snapshot_content_sha256": "a",
    }
    official_path = _inbox(tmp_path, schema="official", packets=[packet], name="official.json")
    republisher_path = _inbox(tmp_path, schema="republisher", packets=[], name="republisher.json")
    audit_path, repair_path = _p1_pair(tmp_path)
    repair = json.loads(repair_path.read_text(encoding="utf-8"))
    repair["as_of_utc"] = "2026-08-02T00:00:00.001Z"
    repair["content_sha256"] = content_sha256({key: value for key, value in repair.items() if key != "content_sha256"})
    repair_path.write_text(json.dumps(repair), encoding="utf-8")

    with pytest.raises(ContractError, match="p1_evidence_pair_as_of_mismatch"):
        build(
            official_inbox=official_path,
            republisher_inbox=republisher_path,
            as_of_utc=AS_OF,
            max_age_seconds=3600,
            event_date=EVENT_DATE,
            p1_audit=audit_path,
            p1_repair=repair_path,
        )


def test_current_ledger_refuses_p1_pair_after_outer_as_of(tmp_path):
    official_path = _inbox(tmp_path, schema="official", packets=[], name="official.json")
    republisher_path = _inbox(tmp_path, schema="republisher", packets=[], name="republisher.json")
    audit_path, repair_path = _p1_pair(tmp_path)
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    audit["as_of_utc"] = "2026-08-03T02:00:00.001Z"
    audit["content_sha256"] = content_sha256({
        key: value for key, value in audit.items() if key != "content_sha256"
    })
    audit_path.write_text(json.dumps(audit), encoding="utf-8")
    repair = json.loads(repair_path.read_text(encoding="utf-8"))
    repair["as_of_utc"] = audit["as_of_utc"]
    repair["audit_content_sha256"] = audit["content_sha256"]
    repair["content_sha256"] = content_sha256({
        key: value for key, value in repair.items() if key != "content_sha256"
    })
    repair_path.write_text(json.dumps(repair), encoding="utf-8")
    # An old filesystem mtime must not make a future declared P1 clock
    # admissible: only the content-addressed as_of values define the boundary.
    os.utime(audit_path, (1, 1))
    os.utime(repair_path, (1, 1))

    with pytest.raises(ContractError, match="p1_evidence_pair_after_outer_as_of"):
        build(
            official_inbox=official_path,
            republisher_inbox=republisher_path,
            as_of_utc=AS_OF,
            max_age_seconds=3600,
            event_date=EVENT_DATE,
            p1_audit=audit_path,
            p1_repair=repair_path,
        )


def test_freshness_boundary_is_inclusive_then_fails_closed_one_second_later(tmp_path):
    station = coordinate_registry_manifest()["station_ids"][0]
    official_path = _inbox(tmp_path, schema="official", packets=[{
        "schema": "official_native_ensemble_forward_packet_v1",
        "station_id": station,
        "event_date": EVENT_DATE,
        "received_at_utc": "2026-08-03T00:00:00Z",
        "snapshot_content_sha256": "official-boundary",
    }], name="official.json")
    republisher_path = _inbox(tmp_path, schema="republisher", packets=[], name="republisher.json")
    audit_path, repair_path = _p1_pair(tmp_path)

    at_limit = build(
        official_inbox=official_path,
        republisher_inbox=republisher_path,
        as_of_utc="2026-08-03T01:00:00Z",
        max_age_seconds=3600,
        event_date=EVENT_DATE,
        p1_audit=audit_path,
        p1_repair=repair_path,
    )
    beyond_limit = build(
        official_inbox=official_path,
        republisher_inbox=republisher_path,
        as_of_utc="2026-08-03T01:00:00.001Z",
        max_age_seconds=3600,
        event_date=EVENT_DATE,
        p1_audit=audit_path,
        p1_repair=repair_path,
    )

    assert at_limit["stations"][0]["official"]["state"] == "FRESH"
    assert at_limit["stations"][0]["official"]["age_seconds"] == 3600
    assert beyond_limit["stations"][0]["official"]["state"] == "STALE"
    assert beyond_limit["stations"][0]["official"]["reason"] == "OFFICIAL_SNAPSHOT_STALE"
