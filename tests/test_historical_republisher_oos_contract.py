from __future__ import annotations

import sqlite3

import pytest

from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.market_registry import (
    apply_target_market_registry_schema,
)
from bayes_basket_target.native_ensemble_oos import (
    HISTORICAL_REPUBLISHER_RECEIPT,
    _evaluate_system,
    _load_cases,
)
from bayes_basket_target.ssot import (
    apply_native_ensemble_foundation_schema,
    apply_target_ssot_schema,
)


SYSTEM_ID = "OPEN_METEO_TEST"
STATION_ID = "TEST"
EVENT_DATE = "2026-06-01"
TRUTH_ID = "truth-test-20260601"
CONTRACT_ID = "republisher-oos-test-20260601-v1"


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    apply_target_ssot_schema(conn)
    apply_target_market_registry_schema(conn)
    apply_native_ensemble_foundation_schema(conn)
    station = {
        "station_id": STATION_ID,
        "icao_code": STATION_ID,
        "city_label": "Test",
        "timezone_name": "Etc/UTC",
        "source_lane": "AWC",
        "settlement_definition": "official_daily_tmax_c",
        "registry_version": "test-v1",
        "source_role": "TEST",
    }
    conn.execute(
        "INSERT INTO calibration_station_registry "
        "(station_id,icao_code,city_label,timezone_name,source_lane,"
        "settlement_definition,registry_version,source_role,content_sha256) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (*station.values(), content_sha256(station)),
    )
    conn.execute(
        "INSERT INTO basket_target_market_registry_snapshot "
        "(snapshot_sha256,source_schema,source_scope,exported_at_utc,"
        "source_database_path,row_count,ingested_at_utc,content_sha256) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (
            "1" * 64,
            "test",
            "shared_live_registry_export_only",
            "2026-06-01T00:00:00Z",
            "/test",
            1,
            "2026-06-01T00:00:01Z",
            "2" * 64,
        ),
    )
    conn.execute(
        "INSERT INTO basket_target_market_registry "
        "(snapshot_sha256,slug,event_date,outcome,city_slug,station_id,"
        "settlement_timezone,settlement_unit,source_lane,source_eligible,"
        "source_block_reason,resolution_url,rules_sha256,yes_token,no_token,"
        "market_active,market_accepting_orders,registry_json,refreshed_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "1" * 64,
            "test-weather",
            EVENT_DATE,
            "20°C",
            "test",
            STATION_ID,
            "Etc/UTC",
            "C",
            "awc",
            1,
            "",
            "https://example.test",
            "3" * 64,
            "yes",
            "no",
            1,
            1,
            "{}",
            "2026-06-01T00:00:02Z",
        ),
    )
    product = {
        "ensemble_system_id": SYSTEM_ID,
        "canonical_name": "Open-Meteo test",
        "independence_family_id": "OPEN_METEO_TEST",
        "upstream_provider_id": "TEST",
        "transport_provider_id": "OPEN_METEO",
        "source_role": "aggregator_republisher",
        "expected_member_count": 2,
        "member_identity_contract_version": "test-v1",
        "publication_contract_version": "test-v1",
        "native_arrival_index_eligible": 0,
        "formal_p0_eligible": 0,
        "valid_from_utc": "2026-01-01T00:00:00Z",
        "valid_until_utc": "9999-12-31T23:59:59Z",
    }
    conn.execute(
        "INSERT INTO calibration_republisher_ensemble_product "
        "(ensemble_system_id,canonical_name,independence_family_id,"
        "upstream_provider_id,transport_provider_id,source_role,"
        "expected_member_count,member_identity_contract_version,"
        "publication_contract_version,native_arrival_index_eligible,"
        "formal_p0_eligible,valid_from_utc,valid_until_utc,content_sha256) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (*product.values(), content_sha256(product)),
    )
    truth = {
        "station_id": STATION_ID,
        "local_date": EVENT_DATE,
        "official_tmax_c": 20.0,
        "source_lane": "AWC",
        "settlement_definition": "official_daily_tmax_c",
        "revision_no": 0,
        "published_at_utc": "2026-06-02T03:00:00Z",
        "received_at_utc": "2026-06-02T03:00:00Z",
        "canonical_committed_at_utc": "2026-06-02T03:00:00Z",
        "timestamps_complete": 1,
        "qc_status": "ACCEPTED",
        "source_role": "TEST",
        "source_payload_sha256": "4" * 64,
        "observation_ids_json": "[]",
        "authority_scope": "FORMAL_NATIVE_ELIGIBLE",
    }
    conn.execute(
        "INSERT INTO calibration_station_daily_high_truth "
        "(truth_id,station_id,local_date,official_tmax_c,source_lane,"
        "settlement_definition,revision_no,published_at_utc,received_at_utc,"
        "canonical_committed_at_utc,timestamps_complete,qc_status,source_role,"
        "source_payload_sha256,observation_ids_json,authority_scope,"
        "content_sha256) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (TRUTH_ID, *truth.values(), content_sha256(truth)),
    )
    return conn


def _insert_historical_rows(
    conn: sqlite3.Connection,
    *,
    contract_id: str | None,
    historical_oos_eligible: int,
    archive_query_at: str = "2026-05-31T12:00:00Z",
) -> None:
    for index, member_id in enumerate(("control", "perturbed_01")):
        identity = {
            "system": SYSTEM_ID,
            "station": STATION_ID,
            "day": EVENT_DATE,
            "member": member_id,
            "index": index,
        }
        conn.execute(
            "INSERT INTO calibration_republisher_ensemble_historical_member_run "
            "(historical_member_run_id,ensemble_system_id,station_id,"
            "target_local_date,archive_lead_bucket,run_cycle,member_id,"
            "member_role,raw_tmax_c,settlement_definition,extraction_version,"
            "provider_id,transport_provider_id,native_run_init_utc,"
            "offline_oos_eligible,archive_query_at_utc,source_url,"
            "raw_object_ref,source_object_sha256,qc_status,authority_scope,"
            "historical_oos_contract_id,historical_oos_eligible,"
            "holdout_eligible,content_sha256) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "historical-member-" + str(index),
                SYSTEM_ID,
                STATION_ID,
                EVENT_DATE,
                "d0",
                "AGGREGATOR_UNKNOWN_NATIVE_CYCLE",
                member_id,
                "CONTROL" if index == 0 else "PERTURBED",
                20.0 + index * 0.1,
                "official_daily_tmax_c",
                "test-v1",
                "TEST",
                "OPEN_METEO",
                None,
                0,
                archive_query_at,
                "https://example.test",
                "historical-test",
                str(index + 5) * 64,
                "ACCEPTED",
                "CALIBRATION_TRAIN_ONLY",
                contract_id,
                historical_oos_eligible,
                0,
                content_sha256(identity),
            ),
        )


def _insert_contract(
    conn: sqlite3.Connection,
    *,
    contract_id: str = CONTRACT_ID,
    truth_asof: str = "2026-06-02T03:00:00Z",
    eligible: int = 1,
) -> None:
    identity = {
        "contract": contract_id,
        "system": SYSTEM_ID,
        "station": STATION_ID,
        "day": EVENT_DATE,
        "truth_asof": truth_asof,
    }
    conn.execute(
        "INSERT INTO calibration_republisher_historical_oos_contract "
        "(historical_oos_contract_id,contract_version,ensemble_system_id,"
        "station_id,target_local_date,archive_lead_bucket,run_cycle,"
        "station_day_closed,station_day_end_utc,station_day_closed_at_utc,"
        "awc_truth_id,awc_truth_revision_no,awc_truth_asof_utc,"
        "awc_truth_source_lane,receipt_class,temporal_scope,future_event_count,"
        "historical_oos_eligible,republisher_receipt_sha256,content_sha256,"
        "created_at_utc) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            contract_id,
            "v1",
            SYSTEM_ID,
            STATION_ID,
            EVENT_DATE,
            "d0",
            "AGGREGATOR_UNKNOWN_NATIVE_CYCLE",
            1,
            "2026-06-02T00:00:00Z",
            "2026-06-02T04:00:00Z",
            TRUTH_ID,
            0,
            truth_asof,
            "AWC",
            HISTORICAL_REPUBLISHER_RECEIPT,
            "CLOSED_HISTORICAL_AWC_TRUTH",
            0,
            eligible,
            "6" * 64,
            content_sha256(identity),
            "2026-06-02T05:00:00Z",
        ),
    )


def test_forward_republisher_rows_never_retro_promote_after_close() -> None:
    conn = _db()
    for index, member_id in enumerate(("control", "perturbed_01")):
        conn.execute(
            "INSERT INTO calibration_republisher_ensemble_member_run "
            "(republisher_member_run_id,ensemble_system_id,station_id,"
            "target_local_date,member_id,member_role,received_at_utc,"
            "lead_bucket,raw_tmax_c,settlement_definition,extraction_version,"
            "upstream_provider_id,transport_provider_id,aggregator_snapshot_at_utc,"
            "source_url,raw_object_ref,source_object_sha256,qc_status,"
            "authority_scope,content_sha256) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "forward-member-" + str(index),
                SYSTEM_ID,
                STATION_ID,
                EVENT_DATE,
                member_id,
                "CONTROL" if index == 0 else "PERTURBED",
                "2026-05-31T12:00:00Z",
                "d0",
                20.0,
                "official_daily_tmax_c",
                "test-v1",
                "TEST",
                "OPEN_METEO",
                "2026-05-31T12:00:00Z",
                "https://example.test",
                "forward-test",
                str(index + 7) * 64,
                "ACCEPTED",
                "PRODUCTION_NO_LIVE_CANDIDATE",
                content_sha256({"forward": index}),
            ),
        )
    conn.commit()
    cases = _load_cases(
        conn,
        station_id=STATION_ID,
        as_of_utc="2026-06-10T00:00:00Z",
    )
    assert cases == {}


def test_historical_republisher_contract_is_required_and_strict_asof() -> None:
    conn = _db()
    _insert_contract(conn)
    _insert_historical_rows(
        conn,
        contract_id=CONTRACT_ID,
        historical_oos_eligible=1,
    )
    conn.commit()
    cases = _load_cases(
        conn,
        station_id=STATION_ID,
        as_of_utc="2026-06-10T00:00:00Z",
    )
    flattened = [case for rows in cases.values() for case in rows]
    assert len(flattened) == 1
    assert flattened[0]["holdout_eligible"] is True
    assert flattened[0]["evidence_timing_class"] == (
        HISTORICAL_REPUBLISHER_RECEIPT
    )

    # A contract whose truth as-of is in the future is never made eligible by
    # the later station-day closure; this is the strict-as-of guard.
    late = _db()
    _insert_contract(late, contract_id="late-contract", truth_asof="2026-06-11T00:00:00Z")
    _insert_historical_rows(
        late,
        contract_id="late-contract",
        historical_oos_eligible=1,
    )
    late.commit()
    late_cases = _load_cases(
        late,
        station_id=STATION_ID,
        as_of_utc="2026-06-10T00:00:00Z",
    )
    late_flattened = [case for rows in late_cases.values() for case in rows]
    assert len(late_flattened) == 1
    assert late_flattened[0]["holdout_eligible"] is False
    assert late_flattened[0]["evidence_timing_class"] != (
        HISTORICAL_REPUBLISHER_RECEIPT
    )


def test_closed_historical_contract_allows_retrospective_archive_query() -> None:
    conn = _db()
    _insert_contract(conn)
    _insert_historical_rows(
        conn,
        contract_id=CONTRACT_ID,
        historical_oos_eligible=1,
        archive_query_at="2026-06-03T12:00:00Z",
    )
    conn.commit()
    cases = _load_cases(
        conn,
        station_id=STATION_ID,
        as_of_utc="2026-06-10T00:00:00Z",
    )
    flattened = [case for rows in cases.values() for case in rows]
    assert len(flattened) == 1
    assert flattened[0]["holdout_eligible"] is True
    assert flattened[0]["evidence_timing_class"] == (
        HISTORICAL_REPUBLISHER_RECEIPT
    )


def test_republisher_batch_receipts_split_chronologically_for_training() -> None:
    """A single retrospective batch receipt must not erase all train days."""

    cases = []
    for offset, day in enumerate(
        ("2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04")
    ):
        cases.append({
            "event_date": day,
            "aggregator_snapshot_at_utc": "2026-06-10T00:00:00Z",
            "values": [20.0 + offset, 20.1 + offset],
            "member_set_sha256": "a" * 64,
            "member_run_content_shas": ["b" * 64],
            "holdout_eligible": True,
            "evidence_timing_class": HISTORICAL_REPUBLISHER_RECEIPT,
            "transport_provider_id": "OPEN_METEO",
            "actual_tmax_c": 20.0 + offset,
            "historical_closed_day": True,
            "future_event_count": 0,
            "historical_oos_contract_ids": [
                f"contract-{offset}"
            ],
        })

    result = _evaluate_system(
        system_id="OPEN_METEO_TEST",
        cases=cases,
        min_train_days=2,
        min_holdout_days=1,
    )
    assert result["state"] == "OOS_READY"
    assert result["train_n"] == 3
    assert result["holdout_n"] == 1
    assert result["train_end_date"] == "2026-06-03"
    assert result["holdout_start_date"] == "2026-06-04"


def test_historical_contract_is_append_only() -> None:
    conn = _db()
    _insert_contract(conn)
    with pytest.raises(sqlite3.IntegrityError, match="append_only"):
        conn.execute(
            "UPDATE calibration_republisher_historical_oos_contract "
            "SET contract_version='v2' WHERE historical_oos_contract_id=?",
            (CONTRACT_ID,),
        )
