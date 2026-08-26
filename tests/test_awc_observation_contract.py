from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from bayes_basket_target.awc_observation_contract import (
    AUTHORITY_SCOPE,
    CONTRACT_SCHEMA,
    COVERAGE_RECEIPT_SCHEMA,
    RAW_OBJECT_KIND,
    CanonicalAWCObservationContract,
)
from bayes_basket_target.awc_observation_builder import BUILDER_RECEIPT_SCHEMA
from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.canonical_ingress import (
    ingest_canonical_contract,
    stage_awc_observation_contract,
)
from bayes_basket_target.canonical_ssot_v11_migration import (
    TARGET_TABLE_DDL as V11_TABLE_DDL,
    TARGET_TRIGGER_DDL as V11_TRIGGER_DDL,
)
from bayes_basket_target.canonical_ssot_v12_migration import (
    TARGET_TABLE_DDL as V12_TABLE_DDL,
    TARGET_TRIGGER_DDL as V12_TRIGGER_DDL,
)
from bayes_basket_target.pr12_awc_observation import (
    build_pr12_expected_scope_bases,
)
from tests.pr12_authority_projection_test_support import authority_projection


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64


def _seal(value: dict[str, object]) -> dict[str, object]:
    result = deepcopy(value)
    result["content_sha256"] = content_sha256(result)
    return result


def _revision(*, report_type: str = "METAR", observed: str = "2026-08-01T12:00:00.000Z") -> dict[str, object]:
    projection_sha = authority_projection()["content_sha256"]
    return _seal(
        {
            "station_id": "KATL",
            "observation_id": "rtobs_1",
            "revision_no": 1,
            "event_date": "2026-08-01",
            "settlement_timezone": "America/New_York",
            "source_lane": "AWC",
            "authority_scope": AUTHORITY_SCOPE,
            "authority_scope_projection_content_sha256": projection_sha,
            "report_type": report_type,
            "sequence": 1,
            "observed_at_utc": observed,
            "received_at_utc": "2026-08-01T12:01:00.000Z",
            "first_seen_at_utc": "2026-08-01T12:01:00.000Z",
            "parsed_at_utc": "2026-08-01T12:01:00.500Z",
            "fetch_started_at_utc": "2026-08-01T12:00:59.000Z",
            "fetch_completed_at_utc": "2026-08-01T12:01:00.250Z",
            "origin_committed_at_utc": "2026-08-01T12:01:00.750Z",
            "source_observation_committed_at_utc": "2026-08-01T12:01:01.000Z",
            "canonical_committed_at_utc": "2026-08-01T12:01:01.000Z",
            "reported_temperature_c": 31.0,
            "raw_object_kind": RAW_OBJECT_KIND,
            "raw_object_sha256": SHA_B,
            "awc_source_receipt_sha256": SHA_C,
            "origin_source_id": "global_noaa_awc_relay",
            "origin_authority": "NOAA/NWS Aviation Weather Center worldwide relay",
            "origin_url": "https://aviationweather.gov/data/api/",
            "origin_transport": "official_awc_raw_metar_batch_direct",
            "origin_registry_sha256": "e" * 64,
            "origin_record_sha256": "f" * 64,
            "correction_kind": "NONE",
            "correction_state": "NO_CORRECTION",
            "supersedes_observation_fact_sha256": None,
            "qc_state": "ACCEPTED",
            "review_receipt_sha256": None,
            "halt_state": "CLEAR",
        }
    )


def _bounds(revision: dict[str, object]) -> dict[str, object]:
    projection_sha = authority_projection()["content_sha256"]
    coverage = _seal(
        {
            "schema": COVERAGE_RECEIPT_SCHEMA,
            "station_id": "KATL",
            "event_date": "2026-08-01",
            "settlement_timezone": "America/New_York",
            "as_of_utc": "2026-08-02T05:00:00.000Z",
            "expected_hourly_slots": ["2026-08-01T12:00:00.000Z"],
            "observed_hourly_slots": ["2026-08-01T12:00:00.000Z"],
            "expected_hourly_metar_count": 1,
            "observed_hourly_metar_count": 1,
            "coverage_complete": True,
            "source_evidence_receipt_sha256": "9" * 64,
        }
    )
    return _seal(
        {
            "station_id": "KATL",
            "event_date": "2026-08-01",
            "settlement_timezone": "America/New_York",
            "bounds_version": 1,
            "source_lane": "AWC",
            "authority_scope_projection_content_sha256": projection_sha,
            "day_start_utc": "2026-08-01T04:00:00.000Z",
            "day_end_utc": "2026-08-02T04:00:00.000Z",
            "running_max_c": 31.0,
            "max_observation_fact_sha256": revision["content_sha256"],
            "observation_revision_vector_sha256": content_sha256(
                [revision["content_sha256"]]
            ),
            "max_tie_vector_sha256": content_sha256([revision["content_sha256"]]),
            "max_tie_break_rule": "EARLIEST_SEQUENCE_THEN_FACT_SHA",
            "coverage_receipt": coverage,
            "coverage_receipt_sha256": coverage["content_sha256"],
            "correction_state": "NO_CORRECTION",
            "halt_state": "CLEAR",
            "latest_review_receipt_sha256": None,
            "closed_day": True,
            "canonical_committed_at_utc": "2026-08-02T04:05:00.000Z",
            "authority_scope": AUTHORITY_SCOPE,
        }
    )


def _contract(revision: dict[str, object] | None = None) -> dict[str, object]:
    fact = revision or _revision()
    return _seal(
        {
            "schema": CONTRACT_SCHEMA,
            "as_of_utc": "2026-08-02T05:00:00.000Z",
            "builder_id": "pr12_awc_observation_builder_v1",
            "authority_scope_projection": authority_projection(),
            "revision_facts": [fact],
            "day_bounds_facts": [_bounds(fact)],
        }
    )


def test_contract_accepts_exact_metar_and_closed_bounds() -> None:
    parsed = CanonicalAWCObservationContract.from_mapping(_contract())
    assert parsed.revision_facts[0].raw["reported_temperature_c"] == 31.0
    assert parsed.day_bounds_facts[0].raw["source_lane"] == "AWC"


def test_routine_metar_is_formal_at_its_provider_minute() -> None:
    """The 2026-08-10 ruling, pinned where the lane enforces it.

    A routine METAR used to be admitted only at :00, and 18 of the 45
    stations never publish there -- Europe at :20/:50 or :25/:55, the
    ASOS-fed United States at :51-:58 -- so eight of them held no revision
    fact at all.  The rule this replaced was never covered: the case named
    for it supplies 12:30, which trips the timestamp-order check first and
    never reaches the minute at all.
    """

    revision = _revision(observed="2026-08-01T11:51:00.000Z")
    revision["received_at_utc"] = "2026-08-01T11:57:00.000Z"
    revision["first_seen_at_utc"] = "2026-08-01T11:57:00.000Z"
    revision["parsed_at_utc"] = "2026-08-01T11:57:00.500Z"
    revision["fetch_started_at_utc"] = "2026-08-01T11:56:59.000Z"
    revision["fetch_completed_at_utc"] = "2026-08-01T11:57:00.250Z"
    revision["origin_committed_at_utc"] = "2026-08-01T11:57:00.750Z"
    revision["source_observation_committed_at_utc"] = "2026-08-01T11:57:01.000Z"
    revision["canonical_committed_at_utc"] = "2026-08-01T11:57:01.000Z"
    revision = _seal({k: v for k, v in revision.items() if k != "content_sha256"})
    parsed = CanonicalAWCObservationContract.from_mapping(_contract(revision))
    assert parsed.revision_facts[0].raw["observed_at_utc"] == (
        "2026-08-01T11:51:00.000Z"
    )


def test_speci_may_use_non_hour_minute() -> None:
    revision = _revision(report_type="SPECI", observed="2026-08-01T11:43:00.000Z")
    revision["received_at_utc"] = "2026-08-01T11:44:00.000Z"
    revision["first_seen_at_utc"] = "2026-08-01T11:44:00.000Z"
    revision["parsed_at_utc"] = "2026-08-01T11:44:00.500Z"
    revision["fetch_started_at_utc"] = "2026-08-01T11:43:59.000Z"
    revision["fetch_completed_at_utc"] = "2026-08-01T11:44:00.250Z"
    revision["origin_committed_at_utc"] = "2026-08-01T11:44:00.750Z"
    revision["source_observation_committed_at_utc"] = "2026-08-01T11:44:01.000Z"
    revision["canonical_committed_at_utc"] = "2026-08-01T11:44:01.000Z"
    revision = _seal({k: v for k, v in revision.items() if k != "content_sha256"})
    CanonicalAWCObservationContract.from_mapping(_contract(revision))


@pytest.mark.parametrize(
    ("field", "value", "match"),
    (
        ("source_lane", "AMSC", "authority_invalid"),
        ("raw_object_kind", "AWC_MESSAGE", "raw_object_kind_invalid"),
        # Toronto keeps New York's offset, so the fact's own event_date still
        # agrees with it and the per-fact checks pass.  Only the binding to
        # the projection catches it -- which is the check that has to exist
        # now that each station carries its own zone rather than one pinned
        # fleet-wide.
        ("settlement_timezone", "America/Toronto", "settlement_timezone_mismatch"),
        ("observed_at_utc", "2026-08-01T12:30:00.000Z", "timestamp_order_invalid"),
    ),
)
def test_revision_rejects_wrong_lane_object_timezone_or_metar_minute(
    field: str, value: object, match: str
) -> None:
    revision = _revision()
    revision[field] = value
    revision = _seal({k: v for k, v in revision.items() if k != "content_sha256"})
    with pytest.raises(ContractError, match=match):
        CanonicalAWCObservationContract.from_mapping(_contract(revision))


def test_revision_rejects_timestamp_rewind() -> None:
    revision = _revision()
    revision["received_at_utc"] = "2026-08-01T11:59:00.000Z"
    revision = _seal({k: v for k, v in revision.items() if k != "content_sha256"})
    with pytest.raises(ContractError, match="timestamp_order_invalid"):
        CanonicalAWCObservationContract.from_mapping(_contract(revision))


def test_correction_requires_supersession_and_halt() -> None:
    revision = _revision()
    revision.update(
        correction_kind="COR",
        correction_state="REVIEW_REQUIRED",
        qc_state="REVIEW_REQUIRED",
        halt_state="HALTED",
    )
    revision = _seal({k: v for k, v in revision.items() if k != "content_sha256"})
    packet = _seal(
        {
            "schema": CONTRACT_SCHEMA,
                "as_of_utc": "2026-08-02T05:00:00.000Z",
                "builder_id": "pr12_awc_observation_builder_v1",
                "authority_scope_projection": authority_projection(),
                "revision_facts": [revision],
            "day_bounds_facts": [],
        }
    )
    with pytest.raises(ContractError, match="review_state_invalid"):
        CanonicalAWCObservationContract.from_mapping(packet)


def test_bounds_rejects_wrong_max_reference_and_vector() -> None:
    packet = _contract()
    packet["day_bounds_facts"][0]["max_observation_fact_sha256"] = "e" * 64
    packet["day_bounds_facts"][0] = _seal(
        {k: v for k, v in packet["day_bounds_facts"][0].items() if k != "content_sha256"}
    )
    packet = _seal({k: v for k, v in packet.items() if k != "content_sha256"})
    with pytest.raises(ContractError, match="max_reference_missing"):
        CanonicalAWCObservationContract.from_mapping(packet)


def test_contract_rejects_duplicate_scope_and_future_fact() -> None:
    packet = _contract()
    packet["revision_facts"].append(deepcopy(packet["revision_facts"][0]))
    packet = _seal({k: v for k, v in packet.items() if k != "content_sha256"})
    with pytest.raises(ContractError, match="scope_duplicate"):
        CanonicalAWCObservationContract.from_mapping(packet)

    packet = _contract()
    packet["as_of_utc"] = "2026-08-01T12:00:30.000Z"
    packet = _seal({k: v for k, v in packet.items() if k != "content_sha256"})
    with pytest.raises(ContractError, match="future_fact"):
        CanonicalAWCObservationContract.from_mapping(packet)


def test_contract_rejects_hash_tamper() -> None:
    packet = _contract()
    packet["revision_facts"][0]["reported_temperature_c"] = 32
    with pytest.raises(ContractError, match="revision_content_sha256_mismatch"):
        CanonicalAWCObservationContract.from_mapping(packet)


def _ingress_db(*, v12: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.create_function("canonical_ingress_authorized", 0, lambda: 1)
    conn.execute(V12_TABLE_DDL if v12 else V11_TABLE_DDL)
    for ddl in V12_TRIGGER_DDL if v12 else V11_TRIGGER_DDL:
        conn.execute(ddl)
    return conn


def test_v12_ingress_commit_and_replay() -> None:
    conn = _ingress_db()
    receipt = ingest_canonical_contract(
        conn,
        _contract(),
        commit=True,
        ingested_at_utc="2026-08-02T05:00:00Z",
    )
    assert receipt["inserted_count"] == 2
    assert receipt["state"] == "COMMITTED"
    replay = ingest_canonical_contract(
        conn,
        _contract(),
        commit=True,
        ingested_at_utc="2026-08-02T05:00:00Z",
    )
    assert replay["inserted_count"] == 0
    assert replay["replayed_count"] == 2
    assert conn.execute("SELECT COUNT(*) FROM canonical_ssot_fact").fetchone()[0] == 2


def test_v11_ingress_rejects_awc_contract_without_write() -> None:
    conn = _ingress_db(v12=False)
    with pytest.raises(ContractError, match="canonical_awc_observation_v12_required"):
        ingest_canonical_contract(
            conn,
            _contract(),
            commit=True,
            ingested_at_utc="2026-08-02T05:00:00Z",
        )
    assert conn.execute("SELECT COUNT(*) FROM canonical_ssot_fact").fetchone()[0] == 0


def test_v12_ingress_preview_is_zero_write() -> None:
    conn = _ingress_db()
    receipt = ingest_canonical_contract(conn, _contract(), commit=False)
    assert receipt["state"] == "VALIDATED_NO_WRITE"
    assert receipt["inserted_count"] == 0
    assert conn.execute("SELECT COUNT(*) FROM canonical_ssot_fact").fetchone()[0] == 0


def test_pr12_stage_requires_outer_transaction_and_caller_can_rollback() -> None:
    conn = _ingress_db()
    with pytest.raises(ContractError, match="outer_transaction_required"):
        stage_awc_observation_contract(
            conn, _contract(), ingested_at_utc="2026-08-02T05:00:00Z"
        )
    conn.execute("BEGIN")
    receipt = stage_awc_observation_contract(
        conn, _contract(), ingested_at_utc="2026-08-02T05:00:00Z"
    )
    assert receipt["state"] == "STAGED_IN_CALLER_TRANSACTION"
    assert receipt["writes_legacy_projection"] is False
    assert conn.execute("SELECT COUNT(*) FROM canonical_ssot_fact").fetchone()[0] == 2
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM canonical_ssot_fact").fetchone()[0] == 0


def _builder_receipt(packet: dict[str, object]) -> dict[str, object]:
    identity = {
        "schema": BUILDER_RECEIPT_SCHEMA,
        "state": "MATERIALIZED_UNPUBLISHED",
        "authority_scope": AUTHORITY_SCOPE,
        "evidence_receipt_sha256": "8" * 64,
        "contract_content_sha256": packet["content_sha256"],
        "revision_fact_count": 1,
        "day_bounds_fact_count": 1,
        "coverage_receipts": [packet["day_bounds_facts"][0]["coverage_receipt"]],
        "terminal_gaps": [
            {
                "station_id": "KATL",
                "failure_code": "DATA_MISSING",
                "detail": "fixture gap",
            }
        ],
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "publishes_pr04_pointer": False,
        "starts_services": False,
    }
    return {**identity, "receipt_sha256": content_sha256(identity)}


def test_pr12_expected_bases_come_from_domain_contract_not_observed_rows() -> None:
    packet = _contract()
    receipt = _builder_receipt(packet)
    observation, day, terminal = build_pr12_expected_scope_bases(
        packet, receipt, release_as_of_utc="2026-08-02T05:00:00Z"
    )
    assert len(observation["expected_keys"]) == 1
    key = observation["expected_keys"][0]["natural_key"]
    assert key["observed_at_utc"] == "2026-08-01T12:00:00Z"
    assert key["canonical_committed_at_utc"] == "2026-08-01T12:01:01Z"
    assert len(day["expected_keys"]) == 1
    assert observation["unresolved_input_receipts"] == [receipt["receipt_sha256"]]
    assert terminal["state"] == "BLOCKED"
    assert terminal["terminal_gap_counts_by_code"] == {"DATA_MISSING": 1}
    assert terminal["publishes_pr04_pointer"] is False


def test_pr12_expected_basis_rejects_tampered_builder_receipt() -> None:
    packet = _contract()
    receipt = _builder_receipt(packet)
    receipt["revision_fact_count"] = 2
    with pytest.raises(ContractError, match="builder_receipt_hash_mismatch"):
        build_pr12_expected_scope_bases(
            packet, receipt, release_as_of_utc="2026-08-02T05:00:00Z"
        )
