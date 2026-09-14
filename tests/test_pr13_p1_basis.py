from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3

import pytest

from bayes_basket_target.awc_observation_builder import (
    EVIDENCE_SCHEMA,
    build_awc_observation_contract,
)
from bayes_basket_target.awc_observation_contract import AWC_ORIGIN_URL, RAW_OBJECT_KIND
from bayes_basket_target.canonical_contract import CanonicalTypedScopeEnvelope
from bayes_basket_target.canonical_expected_scope_manifest import validate_scope_basis
from bayes_basket_target.canonical_ingress import ingest_canonical_contract
from bayes_basket_target.canonical_ssot_v14_migration import (
    TARGET_TABLE_DDL,
    TARGET_TRIGGER_DDL,
)
from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.pr13_p1_basis import (
    EXPECTED_SCOPE_COUNT,
    INTRADAY_BUCKETS,
    PR13P1BasisError,
    build_pr13_p1_readiness_artifacts,
)
from bayes_basket_target.p0_forecast_snapshot import (
    RECEIPT_SCHEMA as P0_FORWARD_RECEIPT_SCHEMA,
    CanonicalP0ForecastSnapshotFact,
)
from bayes_basket_target.station_registry import coordinate_registry_manifest
from tests.pr12_authority_projection_test_support import authority_projection
from bayes_basket_target.fleet import expected_station_count


AS_OF = "2026-08-04T12:00:00Z"


def _seal(value: dict[str, object]) -> dict[str, object]:
    return {**value, "content_sha256": content_sha256(value)}


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _raw_row(index: int) -> dict[str, object]:
    slot = datetime(2026, 8, 1, 4, tzinfo=timezone.utc) + timedelta(hours=index)
    received = slot + timedelta(minutes=1)
    return {
        "observation_id": f"obs-{index}",
        "station_id": "KATL",
        "event_date": "2026-08-01",
        "settlement_timezone": "America/New_York",
        "source_lane": "AWC",
        "report_type": "METAR",
        "observed_at_utc": _stamp(slot),
        "received_at_utc": _stamp(received),
        "first_seen_at_utc": _stamp(received),
        "parsed_at_utc": _stamp(received + timedelta(milliseconds=100)),
        "fetch_started_at_utc": _stamp(slot + timedelta(seconds=30)),
        "fetch_completed_at_utc": _stamp(received),
        "canonical_committed_at_utc": _stamp(received + timedelta(seconds=1)),
        "reported_temperature_c": 25.0,
        "raw_text": f"METAR KATL {index:02d}0000Z 00000KT CAVOK 25/10 A3000",
        "raw_object_kind": RAW_OBJECT_KIND,
        "raw_object_sha256": f"{index + 1:064x}",
        "source_poll_payload_sha256": f"{index + 101:064x}",
        "awc_source_receipt_sha256": f"{index + 201:064x}",
        "origin_source_id": "global_noaa_awc_relay",
        "origin_authority": "NOAA/NWS Aviation Weather Center worldwide relay",
        "origin_url": AWC_ORIGIN_URL,
        "origin_transport": "official_awc_raw_metar_batch_direct",
        "origin_registry_sha256": "a" * 64,
        "origin_record_sha256": f"{index + 301:064x}",
        "origin_committed_at_utc": _stamp(received + timedelta(milliseconds=500)),
        "origin_lineage_ok": True,
        "policy_excluded": False,
        "correction_kind": "NONE",
        "correction_suspected": 0,
    }


def _evidence() -> dict[str, object]:
    rows = [_raw_row(index) for index in range(24)]
    projection = authority_projection(primary_station="KATL")
    authority = next(
        row for row in projection["rows"] if row["station_id"] == "KATL"
    )
    identity = {
        "schema": EVIDENCE_SCHEMA,
        "as_of_utc": AS_OF,
        "source_database": "/read-only/archive.db",
        "station_registry": {
            "KATL": {
                "station_id": "KATL",
                "settlement_definition": "official_daily_tmax_c",
                "content_sha256": authority[
                    "legacy_station_registry_content_sha256"
                ],
            }
        },
        "authority_scope_projection": projection,
        "station_timezones": {"KATL": "America/New_York"},
        "rows": rows,
        "station_day_halts": [],
        "station_day_reviews": [],
        "extraction_gaps": [],
        "source_read_only": True,
        "source_query_only": True,
    }
    return {**identity, "receipt_sha256": content_sha256(identity)}


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "pr13.db"
    contract, _receipt = build_awc_observation_contract(_evidence())
    with sqlite3.connect(path) as conn:
        conn.executescript(TARGET_TABLE_DDL)
        for ddl in TARGET_TRIGGER_DDL:
            conn.execute(ddl)
        receipt = ingest_canonical_contract(
            conn, contract, commit=True, ingested_at_utc=AS_OF
        )
        assert receipt["inserted_count"] == 25
    return path


def _p0_snapshot(
    *,
    station_id: str = "KATL",
    event_date: str = "2026-08-01",
    settlement_timezone: str = "America/New_York",
    decision_at_utc: str = "2026-08-01T03:00:00.000Z",
    source_class: str = "native_official",
) -> dict[str, object]:
    # Rows of the canonical model-family registry: native ensembles carry a
    # system id and a member vector; the republisher rows are the legacy
    # single-value products, kept so the ineligibility test has a subject.
    if source_class == "aggregator_republisher":
        components = (
            ("AIFS", None, "OPEN_METEO", "ecmwf_aifs025_single", "ECMWF", "30.000000000000"),
            ("ICON", None, "OPEN_METEO", "dwd_icon_global", "DWD", "31.000000000000"),
        )
        model_family = "global_deterministic_ai"
    else:
        components = (
            ("ECMWF", "ECMWF_IFS_ENS_OPEN_PF50", "ECMWF", "ecmwf_ifs_ens_open_pf50", "ECMWF", "30.000000000000"),
            ("ICON", "DWD_ICON_GLOBAL_EPS", "DWD", "dwd_icon_global_eps", "DWD", "31.000000000000"),
        )
        model_family = "global_ensemble"
    receipts = []
    for index, (model_id, system_id, provider_id, product_id, family_id, value) in enumerate(components):
        receipts.append(
            {
                **_seal(
                    {
                        "schema": P0_FORWARD_RECEIPT_SCHEMA,
                        "station_id": station_id,
                        "model_id": model_id,
                        "ensemble_system_id": system_id,
                        "model_family": model_family,
                        "provider_id": provider_id,
                        "provider_product_id": product_id,
                        "independence_family_id": family_id,
                        "source_class": source_class,
                        "event_date": event_date,
                        "settlement_timezone": settlement_timezone,
                        "lead_bucket": "d0",
                        "run_init_utc": None,
                        "run_cycle": None,
                        "model_available_at_utc": "2026-08-01T02:58:00.000Z",
                        "retrieved_at_utc": "2026-08-01T02:59:00.000Z",
                        "forecast_decision_at_utc": decision_at_utc,
                        "forecast_value_c": value,
                        "member_count": 50 - 10 * index,
                        "member_evidence_vector_sha256": f"{index + 1:064x}",
                        "raw_message_vector_sha256": f"{index + 21:064x}",
                        "raw_object_id_vector_sha256": f"{index + 31:064x}",
                        "source_packet_content_sha256": f"{41:064x}",
                        "source_capture_content_sha256": f"{index + 51:064x}",
                        "retained_arrival_evidence_content_sha256": f"{61:064x}",
                        "model_input_content_sha256": f"{index + 11:064x}",
                    }
                )
            }
        )
    scenarios = [
        {"temperature_c": f"{29 + index / 10:.12f}", "probability": "0.050000000000"}
        for index in range(20)
    ]
    bucket_probabilities = [
        {"ordinal": 0, "outcome": "below-30", "probability": "0.500000000000"},
        {"ordinal": 1, "outcome": "30-or-above", "probability": "0.500000000000"},
    ]
    identity = {
        "station_id": station_id,
        "event_date": event_date,
        "settlement_timezone": settlement_timezone,
        "lead_bucket": "d0",
        "lead_bucket_scope": "SAME_SETTLEMENT_DAY",
        "run_cycle": None,
        "run_cycle_scope": "REPUBLISHER_NO_NATIVE_CYCLE",
        "season_bin": "ALL_YEAR",
        "source_class": source_class,
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "event_oos_protocol_version": "strict_historical_awc_v12_v1",
        "calibration_method_version": "EMPIRICAL_RESIDUAL_CDF_V1",
        "available_model_set_hash": "1" * 64,
        "activation_root_content_sha256": "2" * 64,
        "selection_content_sha256": "3" * 64,
        "calibrated_distribution_content_sha256": "4" * 64,
        "component_forward_receipts": receipts,
        "component_forward_receipt_set_sha256": content_sha256(receipts),
        "forecast_decision_at_utc": decision_at_utc,
        "calibrated_point_c": "30.500000000000",
        "temperature_scenarios": scenarios,
        "market_bucket_bridge_content_sha256": "5" * 64,
        "market_registry_snapshot_sha256": "6" * 64,
        "registry_scope_rowset_sha256": "7" * 64,
        "bucket_probabilities": bucket_probabilities,
        "bucket_probability_vector_sha256": content_sha256(bucket_probabilities),
        "state": "MATERIALIZED_UNPUBLISHED",
    }
    fact = {**identity, "content_sha256": content_sha256(identity)}
    return CanonicalP0ForecastSnapshotFact.from_mapping(fact).raw


def _insert_p0_snapshot(path: Path, fact: dict[str, object]) -> None:
    with sqlite3.connect(path) as conn:
        conn.create_function("canonical_ingress_authorized", 0, lambda: 1)
        conn.execute(
            "INSERT INTO canonical_ssot_fact VALUES(?,?,?,?,?,?,?)",
            (
                "p0_forecast_snapshot:" + str(fact["content_sha256"]),
                "p0_forecast_snapshot",
                "|".join(
                    str(fact[key])
                    for key in ("station_id", "event_date", "forecast_decision_at_utc")
                ),
                fact["content_sha256"],
                json.dumps(fact, sort_keys=True, separators=(",", ":")),
                json.dumps({"consumable": False}, sort_keys=True),
                AS_OF,
            ),
        )


def test_registry_contract_produces_one_blocked_scope_per_fleet_lead(tmp_path: Path) -> None:
    path = _db(tmp_path)
    before = path.stat()
    expected, envelope, receipt = build_pr13_p1_readiness_artifacts(
        path, as_of_utc=AS_OF
    )
    after = path.stat()

    assert EXPECTED_SCOPE_COUNT == expected_station_count() * 4
    assert len(expected["expected_keys"]) == expected_station_count() * 4
    assert len(envelope["records"]) == expected_station_count() * 4
    assert receipt["terminal_scope_count"] == expected_station_count() * 4
    assert receipt["blocker_counts"] == {"ACTIVATION_ROOT_MISSING": expected_station_count() * 4}
    assert receipt["formal_eligible_scope_count"] == 0
    assert receipt["canonical_awc_observation_fact_count"] == 24
    assert receipt["canonical_awc_observation_station_day_count"] == 1
    assert receipt["canonical_awc_observation_station_count"] == 1
    assert receipt["canonical_p0_snapshot_fact_count"] == 0
    assert receipt["canonical_p0_snapshot_fact_type"] == "p0_forecast_snapshot"
    assert receipt["v14_p0_forecast_snapshot_consumer_ready"] is True
    assert receipt["p1_event_root_basis_count"] == 24
    assert receipt["p1_event_root_materialized_unpublished_count"] == 0
    assert receipt["p1_event_root_blocked_count"] == 24
    assert {
        tuple(row["blockers"]) for row in receipt["p1_event_root_basis"]
    } == {("P1_PARENT_MISSING",)}
    assert receipt["state"] == receipt["release_state"] == "BLOCKED"
    assert expected["content_sha256"] == receipt["expected_basis_content_sha256"]
    assert envelope["content_sha256"] == receipt["typed_envelope_content_sha256"]
    validate_scope_basis(expected)
    CanonicalTypedScopeEnvelope.from_mapping(envelope)
    assert (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)


def test_expected_scope_set_is_registry_cross_frozen_four_buckets(tmp_path: Path) -> None:
    expected, envelope, receipt = build_pr13_p1_readiness_artifacts(
        _db(tmp_path), as_of_utc=AS_OF
    )
    keys = {
        (
            row["natural_key"]["station_id"],
            row["natural_key"]["intraday_bucket"],
        )
        for row in expected["expected_keys"]
    }
    assert keys == {
        (station, bucket)
        for station in coordinate_registry_manifest()["station_ids"]
        for bucket in INTRADAY_BUCKETS
    }
    assert all(
        row["natural_key"] == {
            "station_id": row["natural_key"]["station_id"],
            "source_lane": "AWC",
            "season": "JJA",
            "intraday_bucket": row["natural_key"]["intraday_bucket"],
            "maturity": "D0",
            "settlement_definition": "official_daily_tmax_c",
        }
        for row in expected["expected_keys"]
    )
    katl = [row for row in receipt["terminal_scopes"] if row["station_id"] == "KATL"]
    kaus = [row for row in receipt["terminal_scopes"] if row["station_id"] == "KAUS"]
    assert len(katl) == len(kaus) == 4
    assert {row["observation_fact_count"] for row in katl} == {24}
    assert {row["observation_fact_count"] for row in kaus} == {0}
    assert {tuple(row["blockers"]) for row in envelope["records"]} == {
        ("ACTIVATION_ROOT_MISSING",)
    }


def test_v14_snapshot_binds_exact_station_day_timezone_before_awc_revision(
    tmp_path: Path,
) -> None:
    path = _db(tmp_path)
    snapshot = _p0_snapshot()
    _insert_p0_snapshot(path, snapshot)
    before = path.stat()
    _expected, _envelope, receipt = build_pr13_p1_readiness_artifacts(
        path, as_of_utc=AS_OF
    )
    after = path.stat()
    roots = receipt["p1_event_root_basis"]
    assert len(roots) == 24
    assert receipt["p1_event_root_materialized_unpublished_count"] == 24
    assert receipt["p1_event_root_blocked_count"] == 0
    assert {row["state"] for row in roots} == {"MATERIALIZED_UNPUBLISHED"}
    assert {row["p0_forecast_snapshot_content_sha256"] for row in roots} == {
        snapshot["content_sha256"]
    }
    assert all(
        row["p0_forecast_decision_at_utc"]
        <= row["observation_canonical_committed_at_utc"]
        for row in roots
    )
    assert all(row["consumable"] is False for row in roots)
    assert receipt["blocker_counts"] == {"ACTIVATION_ROOT_MISSING": expected_station_count() * 4}
    assert (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)


def test_v14_snapshot_wrong_event_date_cannot_cross_bind(tmp_path: Path) -> None:
    path = _db(tmp_path)
    _insert_p0_snapshot(path, _p0_snapshot(event_date="2026-08-02"))
    _expected, _envelope, receipt = build_pr13_p1_readiness_artifacts(
        path, as_of_utc=AS_OF
    )
    assert receipt["canonical_p0_snapshot_fact_count"] == 1
    assert receipt["p1_event_root_materialized_unpublished_count"] == 0
    assert receipt["p1_event_root_blocked_count"] == 24
    assert {
        tuple(row["blockers"]) for row in receipt["p1_event_root_basis"]
    } == {("P1_PARENT_MISSING",)}


def test_republisher_p0_snapshot_is_explicitly_ineligible_for_p1(
    tmp_path: Path,
) -> None:
    path = _db(tmp_path)
    snapshot = _p0_snapshot(source_class="aggregator_republisher")
    _insert_p0_snapshot(path, snapshot)
    _expected, _envelope, receipt = build_pr13_p1_readiness_artifacts(
        path, as_of_utc=AS_OF
    )

    assert receipt["canonical_p0_snapshot_fact_count"] == 1
    assert receipt["p1_event_root_materialized_unpublished_count"] == 0
    assert receipt["p1_event_root_blocked_count"] == 24
    assert {
        tuple(row["blockers"]) for row in receipt["p1_event_root_basis"]
    } == {("P1_PARENT_SOURCE_INELIGIBLE",)}
    assert receipt["canonical_p0_snapshot_inventory"] == [
        {
            "fact_id": "p0_forecast_snapshot:" + snapshot["content_sha256"],
            "content_sha256": snapshot["content_sha256"],
            "source_class": "aggregator_republisher",
            "p1_parent_source_eligible": False,
        }
    ]


def test_legacy_probability_rows_cannot_change_expected_or_terminal_set(
    tmp_path: Path,
) -> None:
    path = _db(tmp_path)
    baseline = build_pr13_p1_readiness_artifacts(path, as_of_utc=AS_OF)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE basket_probability_envelope("
            "envelope_id TEXT PRIMARY KEY,stage TEXT,station_id TEXT,event_date TEXT)"
        )
        conn.execute(
            "INSERT INTO basket_probability_envelope VALUES(?,?,?,?)",
            ("legacy-p1", "P1", "KATL", "2026-08-01"),
        )
    current = build_pr13_p1_readiness_artifacts(path, as_of_utc=AS_OF)
    assert current[0] == baseline[0]
    assert current[1] == baseline[1]
    assert current[2]["terminal_scopes"] == baseline[2]["terminal_scopes"]
    assert current[2]["blocker_counts"] == baseline[2]["blocker_counts"]
    assert current[2]["source_inventory_content_sha256"] == baseline[2][
        "source_inventory_content_sha256"
    ]


def test_corrupted_canonical_awc_row_fails_closed(tmp_path: Path) -> None:
    path = _db(tmp_path)
    with sqlite3.connect(path) as conn:
        fact_id, fact_json = conn.execute(
            "SELECT fact_id,fact_json FROM canonical_ssot_fact "
            "WHERE fact_type='truth_observation_revision' LIMIT 1"
        ).fetchone()
        raw = json.loads(fact_json)
        raw["source_lane"] = "AMSC"
        conn.execute("DROP TRIGGER canonical_ssot_fact_no_update")
        conn.execute(
            "UPDATE canonical_ssot_fact SET fact_json=? WHERE fact_id=?",
            (json.dumps(raw, sort_keys=True), fact_id),
        )
    with pytest.raises(PR13P1BasisError, match="canonical_v14_schema_invalid"):
        build_pr13_p1_readiness_artifacts(path, as_of_utc=AS_OF)


def test_malformed_legacy_p0_corrupts_v14_instead_of_becoming_a_parent(
    tmp_path: Path,
) -> None:
    path = _db(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TRIGGER canonical_ssot_fact_ingress_only")
        conn.execute(
            "INSERT INTO canonical_ssot_fact VALUES(?,?,?,?,?,?,?)",
            (
                "p0_snapshot:" + "f" * 64,
                "p0_snapshot",
                "malformed-p0-scope",
                "f" * 64,
                "{}",
                "{}",
                AS_OF,
            ),
        )
    with pytest.raises(PR13P1BasisError, match="canonical_v14_schema_invalid"):
        build_pr13_p1_readiness_artifacts(path, as_of_utc=AS_OF)


def test_artifacts_are_deterministic_for_same_read_only_snapshot(tmp_path: Path) -> None:
    path = _db(tmp_path)
    first = build_pr13_p1_readiness_artifacts(path, as_of_utc=AS_OF)
    second = build_pr13_p1_readiness_artifacts(path, as_of_utc=AS_OF)
    assert first == second
