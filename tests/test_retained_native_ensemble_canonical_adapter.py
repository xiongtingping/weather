from __future__ import annotations

from copy import deepcopy
import json
import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target.fleet import expected_station_count
from bayes_basket_target.canonical_contract import CanonicalContract
from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.model_system_coverage import canonical_station_ids
from bayes_basket_target.migration_quiescence import (
    synthetic_writer_quiescence_receipt,
)
from bayes_basket_target.retained_native_ensemble_canonical_adapter import (
    CANONICAL_OOS_ROLE,
    LEGACY_IDENTITY_FIELDS,
    SOURCE_SPECS,
    adapt_retained_packet_to_canonical_candidates,
)
from scripts.apply_canonical_contract_schema import migrate as apply_contract_schema
from scripts import ingest_retained_native_ensemble_members as ingest_module
from scripts.ingest_retained_native_ensemble_members import (
    CONFIRMATION,
    ingest_retained_packets,
)


SCHEMA = "eccc_geps_official_historical_multistation_members_v1"
SPEC = SOURCE_SPECS[SCHEMA]


def _packet() -> tuple[dict[str, object], dict[tuple[str, str, str, str, str, str], dict[str, object]]]:
    event_date = "2026-07-03"
    run_init = "2026-07-01T00:00:00Z"
    archive_query = "2026-07-31T07:00:30Z"
    source_prefix = "https://dd.weather.gc.ca/20260701/WXO-DD/ensemble/geps/"
    captures: list[dict[str, object]] = []
    legacy: dict[tuple[str, str, str, str, str, str], dict[str, object]] = {}
    for station_index, station_id in enumerate(canonical_station_ids()):
        members = [
            {
                "ensemble_system_id": SPEC.ensemble_system_id,
                "member_id": "control" if index == 0 else f"perturbed_{index:02d}",
                "member_role": "CONTROL" if index == 0 else "PERTURBED",
                "raw_tmax_c": 20.0 + station_index / 100 + index / 10,
            }
            for index in range(SPEC.expected_member_count)
        ]
        member_identity = [
            {
                "member_id": item["member_id"],
                "member_role": item["member_role"],
                "raw_tmax_c": item["raw_tmax_c"],
            }
            for item in members
        ]
        receipts = [
            {
                "forecast_hour": 54,
                "object_key": f"object-{station_id}",
                "object_sha256": content_sha256({"object": station_id}),
            }
        ]
        receipt_set_sha = content_sha256(receipts)
        capture_identity: dict[str, object] = {
            "schema": SCHEMA,
            "station_id": station_id,
            "event_date": event_date,
            "settlement_timezone": "America/New_York",
            "run_init_utc": run_init,
            "run_cycle": SPEC.run_cycle,
            "archive_lead_bucket": SPEC.lead_bucket,
            "ensemble_system_id": SPEC.ensemble_system_id,
            "independence_family_id": SPEC.independence_family_id,
            "member_count": SPEC.expected_member_count,
            "members": members,
            "member_set_sha256": content_sha256(member_identity),
            "field_receipt_count": len(receipts),
            "field_receipt_set_sha256": receipt_set_sha,
            "field_receipts": receipts,
            "archive_query_at_utc": archive_query,
            "source_prefix": source_prefix,
            "source_class": "OFFICIAL_NATIVE_ARCHIVE",
            "native_run_init_known": True,
            "native_publication_at_known": False,
            "offline_chronological_oos_eligible": True,
            "training_only": True,
            "holdout_eligible": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
            "live_submit_authorized": False,
        }
        captures.append(
            {
                **capture_identity,
                "content_sha256": content_sha256(capture_identity),
            }
        )
        for member in members:
            legacy_identity: dict[str, object] = {
                "ensemble_system_id": SPEC.ensemble_system_id,
                "station_id": station_id,
                "target_local_date": event_date,
                "archive_lead_bucket": SPEC.lead_bucket,
                "run_cycle": SPEC.run_cycle,
                "member_id": member["member_id"],
                "member_role": member["member_role"],
                "raw_tmax_c": member["raw_tmax_c"],
                "settlement_definition": "official_daily_tmax_c",
                "extraction_version": SPEC.extraction_version,
                "provider_id": SPEC.provider_id,
                "transport_provider_id": SPEC.transport_provider_id,
                "native_run_init_utc": run_init,
                "offline_oos_eligible": 1,
                "archive_query_at_utc": archive_query,
                "source_url": source_prefix,
                "raw_object_ref": SPEC.raw_object_ref_prefix + receipt_set_sha,
                "source_object_sha256": receipt_set_sha,
                "qc_status": "ACCEPTED",
                "authority_scope": "CALIBRATION_TRAIN_ONLY",
                "holdout_eligible": 0,
            }
            assert tuple(legacy_identity) == LEGACY_IDENTITY_FIELDS
            row = {
                **legacy_identity,
                "content_sha256": content_sha256(legacy_identity),
            }
            key = (
                SPEC.ensemble_system_id,
                station_id,
                event_date,
                SPEC.lead_bucket,
                SPEC.run_cycle,
                str(member["member_id"]),
            )
            legacy[key] = row
    packet_identity: dict[str, object] = {
        "schema": SCHEMA,
        "event_date": event_date,
        "run_init_utc": run_init,
        "run_cycle": SPEC.run_cycle,
        "ensemble_system_id": SPEC.ensemble_system_id,
        "independence_family_id": SPEC.independence_family_id,
        "station_count": expected_station_count(),
        "captures": captures,
        "training_only": True,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "live_submit_authorized": False,
    }
    return (
        {**packet_identity, "content_sha256": content_sha256(packet_identity)},
        legacy,
    )


def _rehash(value: dict[str, object]) -> None:
    value["content_sha256"] = content_sha256(
        {key: item for key, item in value.items() if key != "content_sha256"}
    )


def _v4_database(
    path: Path,
    legacy: dict[tuple[str, str, str, str, str, str], dict[str, object]],
) -> Path:
    path.touch()
    assert apply_contract_schema(path, apply=True)["state"] == "APPLIED"
    columns = (*LEGACY_IDENTITY_FIELDS, "content_sha256")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE calibration_native_ensemble_historical_member_run("
            + ",".join(f'"{column}"' for column in columns)
            + ")"
        )
        placeholders = ",".join("?" for _ in columns)
        conn.executemany(
            "INSERT INTO calibration_native_ensemble_historical_member_run VALUES("
            + placeholders
            + ")",
            [tuple(row[column] for column in columns) for row in legacy.values()],
        )
    return path


def _packet_path(path: Path, packet: dict[str, object]) -> Path:
    path.write_text(
        json.dumps(packet, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    return path


def test_exact_retained_members_become_non_oos_canonical_candidates() -> None:
    packet, legacy = _packet()
    contract, receipt = adapt_retained_packet_to_canonical_candidates(
        packet,
        legacy_rows=legacy,
        as_of_utc="2026-08-04T00:00:00Z",
        builder_id="test-retained-adapter",
    )
    parsed = CanonicalContract.from_mapping(contract)
    assert len(parsed.ensemble_member_facts) == expected_station_count() * SPEC.expected_member_count
    assert len(parsed.native_arrival_facts) == 0
    assert {item.oos_role for item in parsed.ensemble_member_facts} == {
        CANONICAL_OOS_ROLE
    }
    assert {item.strict_asof for item in parsed.ensemble_member_facts} == {False}
    assert {item.closed_day for item in parsed.ensemble_member_facts} == {True}
    assert receipt["blockers"] == ["LINEAGE_MISMATCH"]
    assert receipt["strict_asof_member_count"] == 0
    assert receipt["native_arrival_indexed_count"] == 0


def test_receipt_set_and_legacy_semantics_must_match_exactly() -> None:
    packet, legacy = _packet()
    broken_packet = deepcopy(packet)
    capture = broken_packet["captures"][0]
    assert isinstance(capture, dict)
    capture["field_receipts"][0]["object_key"] = "changed"
    _rehash(capture)
    _rehash(broken_packet)
    with pytest.raises(ContractError, match="receipt_set_sha_mismatch"):
        adapt_retained_packet_to_canonical_candidates(
            broken_packet,
            legacy_rows=legacy,
            as_of_utc="2026-08-04T00:00:00Z",
            builder_id="test-retained-adapter",
        )

    packet, legacy = _packet()
    first_key = next(iter(legacy))
    broken_row = dict(legacy[first_key])
    broken_row["source_object_sha256"] = "f" * 64
    _rehash(broken_row)
    legacy[first_key] = broken_row
    with pytest.raises(ContractError, match="legacy_field_mismatch:source_object_sha256"):
        adapt_retained_packet_to_canonical_candidates(
            packet,
            legacy_rows=legacy,
            as_of_utc="2026-08-04T00:00:00Z",
            builder_id="test-retained-adapter",
        )


def test_source_class_and_closed_day_fail_closed() -> None:
    packet, legacy = _packet()
    republisher = deepcopy(packet)
    capture = republisher["captures"][0]
    assert isinstance(capture, dict)
    capture["source_class"] = "aggregator_republisher"
    _rehash(capture)
    _rehash(republisher)
    with pytest.raises(ContractError, match="capture_scope_mismatch:source_class"):
        adapt_retained_packet_to_canonical_candidates(
            republisher,
            legacy_rows=legacy,
            as_of_utc="2026-08-04T00:00:00Z",
            builder_id="test-retained-adapter",
        )

    packet, legacy = _packet()
    with pytest.raises(ContractError, match="event_day_not_closed"):
        adapt_retained_packet_to_canonical_candidates(
            packet,
            legacy_rows=legacy,
            as_of_utc="2026-07-03T12:00:00Z",
            builder_id="test-retained-adapter",
        )


def test_ingress_preview_is_zero_write_then_apply_and_replay_are_exact(
    tmp_path: Path,
) -> None:
    packet, legacy = _packet()
    database = _v4_database(tmp_path / "ssot.db", legacy)
    source = _packet_path(tmp_path / "packet.json", packet)
    before = database.read_bytes()

    preview = ingest_retained_packets(
        database,
        packet_paths=(source,),
        as_of_utc="2026-08-04T00:00:00Z",
        apply=False,
    )
    assert database.read_bytes() == before
    assert preview["state"] == "PREVIEW_VALIDATED_NO_WRITE"
    assert preview["canonical_member_fact_count"] == expected_station_count() * SPEC.expected_member_count
    assert preview["insertable_or_inserted_count"] == expected_station_count() * SPEC.expected_member_count
    assert preview["replayed_count"] == 0
    assert preview["writes_database"] is False

    applied = ingest_retained_packets(
        database,
        packet_paths=(source,),
        as_of_utc="2026-08-04T00:00:00Z",
        apply=True,
        confirmation=CONFIRMATION,
        writer_quiescence_receipt=synthetic_writer_quiescence_receipt(database),
    )
    assert applied["state"] == "COMMITTED"
    assert applied["insertable_or_inserted_count"] == expected_station_count() * SPEC.expected_member_count
    assert applied["replayed_count"] == 0
    assert applied["fact_type_counts_after"]["ensemble_member"] == expected_station_count() * SPEC.expected_member_count
    assert applied["canonical_native_arrival_fact_count"] == 0
    assert applied["strict_asof_member_count"] == 0

    replay = ingest_retained_packets(
        database,
        packet_paths=(source,),
        as_of_utc="2026-08-04T00:00:00Z",
        apply=True,
        confirmation=CONFIRMATION,
        writer_quiescence_receipt=synthetic_writer_quiescence_receipt(database),
    )
    assert replay["insertable_or_inserted_count"] == 0
    assert replay["replayed_count"] == expected_station_count() * SPEC.expected_member_count
    assert replay["writes_database"] is False
    assert replay["fact_type_counts_after"] == applied["fact_type_counts_after"]


def test_ingress_apply_requires_confirmation_and_quiescence(tmp_path: Path) -> None:
    packet, legacy = _packet()
    database = _v4_database(tmp_path / "ssot.db", legacy)
    source = _packet_path(tmp_path / "packet.json", packet)
    with pytest.raises(ContractError, match="apply_confirmation_invalid"):
        ingest_retained_packets(
            database,
            packet_paths=(source,),
            as_of_utc="2026-08-04T00:00:00Z",
            apply=True,
            confirmation="wrong",
            writer_quiescence_receipt=synthetic_writer_quiescence_receipt(database),
        )
    with pytest.raises(ContractError, match="writer_quiescence_required"):
        ingest_retained_packets(
            database,
            packet_paths=(source,),
            as_of_utc="2026-08-04T00:00:00Z",
            apply=True,
            confirmation=CONFIRMATION,
        )


def test_ingress_failure_rolls_back_the_complete_packet_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packet, legacy = _packet()
    database = _v4_database(tmp_path / "ssot.db", legacy)
    first = _packet_path(tmp_path / "first.json", packet)
    second = _packet_path(tmp_path / "second.json", packet)
    real_ingress = ingest_module.ingest_canonical_contract
    calls = 0

    def fail_second(*args: object, **kwargs: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ContractError("injected_second_packet_failure")
        return real_ingress(*args, **kwargs)

    monkeypatch.setattr(ingest_module, "ingest_canonical_contract", fail_second)
    with pytest.raises(ContractError, match="injected_second_packet_failure"):
        ingest_retained_packets(
            database,
            packet_paths=(first, second),
            as_of_utc="2026-08-04T00:00:00Z",
            apply=True,
            confirmation=CONFIRMATION,
            writer_quiescence_receipt=synthetic_writer_quiescence_receipt(database),
        )
    assert calls == 2
    with sqlite3.connect(database) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM canonical_ssot_fact WHERE fact_type='ensemble_member'"
        ).fetchone()[0] == 0
