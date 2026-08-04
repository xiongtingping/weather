from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target.canonical_contract import (
    CanonicalContract,
    CONTRACT_VERSION,
    SCHEMA,
)
from bayes_basket_target.canonical_ingress import (
    AWC_DAILY_TRUTH_PROJECTION_SCHEMA,
    adapt_legacy_calibration_packet_to_canonical,
    ingest_canonical_contract,
)
from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.ssot import apply_production_ssot_schema


ROOT = Path(__file__).resolve().parents[1]


def _model() -> dict:
    row = {
        "station_id": "ZUUU",
        "model_id": "ECMWF",
        "ensemble_system_id": "",
        "model_family": "global_deterministic",
        "provider_id": "ECMWF_IFS",
        "source_class": "aggregator_republisher",
        "event_date": "2026-07-23",
        "run_init_utc": "2026-07-22T00:00:00Z",
        "lead_bucket": "d1",
        "run_cycle": "PREVIOUS_DAY1",
        "settlement_timezone": "Asia/Shanghai",
        "forecast_value": 31.5,
        "raw_object_sha256": "a" * 64,
        "oos_role": "historical_strict_oos",
        "closed_day": True,
        "strict_asof": True,
        "native_arrival_indexed": False,
        "p1_eligible": False,
        "formal_live_authority": False,
        "published_at_utc": "",
        "received_at_utc": "",
    }
    row["content_sha256"] = content_sha256(row)
    return row


def _truth() -> dict:
    row = {
        "truth_id": "truth-zuuu-20260723-r1",
        "station_id": "ZUUU",
        "event_date": "2026-07-23",
        "truth_revision_no": 1,
        "truth_value": 32.0,
        "truth_content_sha256": "b" * 64,
        "canonical_committed_at_utc": "2026-07-24T08:00:00Z",
        "station_day_end_utc": "2026-07-24T06:00:00Z",
        "closed_day": True,
        "source_lane": "AWC",
        "source_role": "AWC_HISTORICAL_REPLAY_INGRESS",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "settlement_timezone": "Asia/Shanghai",
    }
    row["content_sha256"] = content_sha256(row)
    return row


def _contract() -> dict:
    model = _model()
    truth = _truth()
    residual = {
        "residual_id": "res-zuuu-20260723-ecmwf",
        "truth_id": truth["truth_id"],
        "model_fact_content_sha256": model["content_sha256"],
        "station_id": "ZUUU",
        "model_id": "ECMWF",
        "event_date": "2026-07-23",
        "lead_bucket": "d1",
        "run_cycle": "PREVIOUS_DAY1",
        "residual_value": 0.5,
        "paired_asof_utc": "2026-07-22T00:00:00Z",
        "oos_role": "historical_strict_oos",
    }
    residual["content_sha256"] = content_sha256(residual)
    identity = {
        "schema": SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "as_of_utc": "2026-08-03T12:00:00Z",
        "builder_id": "d1.global_deterministic.v1",
        "model_facts": [model],
        "ensemble_member_facts": [],
        "native_arrival_facts": [],
        "ensemble_member_receipt_manifest_facts": [],
        "ensemble_member_arrival_binding_facts": [],
        "truth_facts": [truth],
        "residual_facts": [residual],
        "activation_facts": [],
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _ensemble_contract() -> dict:
    member = {
        "station_id": "KATL",
        "ensemble_system_id": "NOAA_GEFS_05",
        "model_family": "global_ensemble",
        "provider_id": "NOAA",
        "source_class": "native_official",
        "event_date": "2026-07-24",
        "run_init_utc": "2026-07-22T18:00:00Z",
        "lead_bucket": "d2",
        "run_cycle": "18Z",
        "member_id": "c00",
        "member_role": "CONTROL",
        "extraction_version": "gefs.tmax.v1",
        "settlement_timezone": "America/New_York",
        "forecast_value": 31.5,
        "raw_receipt_set_sha256": "f" * 64,
        "source_member_fact_sha": "1" * 64,
        "archive_query_at_known": True,
        "archive_query_at_utc": "2026-07-22T21:05:00Z",
        "oos_role": "historical_strict_oos",
        "closed_day": True,
        "strict_asof": True,
    }
    member["content_sha256"] = content_sha256(member)
    arrival = {
        "native_arrival_id": "noaa-gefs-20260722-18z-d2-pgrb2a",
        "ensemble_system_id": "NOAA_GEFS_05",
        "provider_id": "NOAA",
        "source_class": "native_official",
        "native_object_id": "gefs.20260722/18/atmos/pgrb2ap5",
        "run_init_utc": "2026-07-22T18:00:00Z",
        "lead_bucket": "d2",
        "run_cycle": "18Z",
        "raw_object_sha256": "2" * 64,
        "published_at_known": True,
        "published_at_utc": "2026-07-22T21:00:00Z",
        "received_at_known": True,
        "received_at_utc": "2026-07-22T21:03:00Z",
        "native_arrival_indexed": True,
    }
    arrival["content_sha256"] = content_sha256(arrival)
    identity = {
        "schema": SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "as_of_utc": "2026-08-03T12:00:00Z",
        "builder_id": "gefs.member.arrival.test.v2",
        "model_facts": [],
        "ensemble_member_facts": [member],
        "native_arrival_facts": [arrival],
        "ensemble_member_receipt_manifest_facts": [],
        "ensemble_member_arrival_binding_facts": [],
        "truth_facts": [],
        "residual_facts": [],
        "activation_facts": [],
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _contract_with_activation(*, generation: int, activation_id: str) -> dict:
    packet = _contract()
    activation_identity = {
        "activation_id": activation_id,
        "station_id": "ZUUU",
        "model_family": "global_deterministic",
        "lead_bucket": "d1",
        "run_cycle": "PREVIOUS_DAY1",
        "candidate_models": ["ECMWF"],
        "selected_models": ["ECMWF"],
        "selected_weights": {"ECMWF": 1.0},
        "available_model_set_hash": "c" * 64,
        "selection_evidence_sha256": "d" * 64,
        "artifact_content_sha256": "e" * 64,
        "activation_generation": generation,
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "oos_role": "historical_strict_oos",
    }
    activation = {
        **activation_identity,
        "activation_content_sha256": content_sha256(activation_identity),
    }
    identity = {
        key: value for key, value in packet.items() if key != "content_sha256"
    }
    identity["activation_facts"] = [activation]
    return {**identity, "content_sha256": content_sha256(identity)}


def test_canonical_contract_rejects_legacy_alias_and_ingresses_once() -> None:
    packet = _contract()
    with pytest.raises(ContractError, match="unknown_field"):
        CanonicalContract.from_mapping(
            {**packet, "model_facts": [{**packet["model_facts"][0], "target_local_date": "2026-07-23"}]}
        )

    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        preview = ingest_canonical_contract(conn, packet)
        assert preview["state"] == "VALIDATED_NO_WRITE"
        assert conn.execute("SELECT COUNT(*) FROM canonical_ssot_fact").fetchone()[0] == 0
        applied = ingest_canonical_contract(conn, packet, commit=True)
        assert applied["inserted_count"] == 3
        replay = ingest_canonical_contract(conn, packet, commit=True)
        assert replay["replayed_count"] == 3
        assert conn.execute("SELECT COUNT(*) FROM canonical_ssot_fact").fetchone()[0] == 3


def test_v4_member_and_arrival_use_only_ingress_and_gate_closes_after_call() -> None:
    packet = _ensemble_contract()
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        applied = ingest_canonical_contract(conn, packet, commit=True)
        assert applied["validated_counts"] == {
            "model": 0,
            "ensemble_member": 1,
            "native_arrival": 1,
            "ensemble_member_arrival_binding": 0,
            "truth": 0,
            "residual": 0,
            "activation": 0,
        }
        assert dict(
            conn.execute(
                "SELECT fact_type,COUNT(*) FROM canonical_ssot_fact GROUP BY fact_type"
            )
        ) == {"ensemble_member": 1, "native_arrival": 1}
        with pytest.raises(sqlite3.IntegrityError, match="canonical_ingress_only"):
            conn.execute(
                "INSERT INTO canonical_ssot_fact VALUES(?,?,?,?,?,?,?)",
                (
                    "model:" + "9" * 64,
                    "model",
                    "bypass",
                    "9" * 64,
                    "{}",
                    "{}",
                    "2026-08-04T00:00:00Z",
                ),
            )


def test_production_sources_have_one_canonical_fact_insert_owner() -> None:
    insert_owners: set[str] = set()
    authorization_owners: set[str] = set()
    for root in (ROOT / "bayes_basket_target", ROOT / "scripts"):
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            relative = str(path.relative_to(ROOT))
            if "INSERT INTO canonical_ssot_fact(" in text:
                insert_owners.add(relative)
            if "create_function(" in text and "INGRESS_AUTHORIZATION_FUNCTION" in text:
                authorization_owners.add(relative)
    assert insert_owners == {"bayes_basket_target/canonical_ingress.py"}
    assert authorization_owners == {"bayes_basket_target/canonical_ingress.py"}


def test_legacy_adapter_and_reverse_projection_are_retired_without_writes() -> None:
    legacy_packet = {
        "schema": "bayes_basket_target_calibration_evidence_v1",
        "truth_evidence": [
            {
                "truth_id": "legacy-truth",
                "source_lane": "AMSC",
                "source_role": "CANONICAL_AMSC_AWC_INGRESS",
            }
        ],
        "calibration_model_rows": [
            {
                "model_run_id": "legacy-run",
                "model_family": "global_deterministic",
                "run_cycle": "00Z",
            }
        ],
    }
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        with pytest.raises(ContractError, match="canonical_legacy_adapter_retired"):
            adapt_legacy_calibration_packet_to_canonical(
                conn,
                legacy_packet,
                builder_id="legacy.adapter.test",
                authority_scope_override="PRODUCTION_NO_LIVE_ONLY",
            )
        assert conn.execute("SELECT COUNT(*) FROM canonical_ssot_fact").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM calibration_station_daily_high_truth"
        ).fetchone()[0] == 0

        with pytest.raises(
            ContractError, match="canonical_ingress_legacy_projection_retired"
        ):
            ingest_canonical_contract(
                conn,
                _contract(),
                legacy_projection=legacy_packet,
            )
        assert conn.execute("SELECT COUNT(*) FROM canonical_ssot_fact").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM calibration_station_daily_high_truth"
        ).fetchone()[0] == 0


def test_canonical_contract_stages_only_inside_caller_transaction() -> None:
    packet = _contract()
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)

        with pytest.raises(ContractError, match="outer_transaction_required"):
            ingest_canonical_contract(
                conn,
                packet,
                commit=True,
                finalize_transaction=False,
            )
        with pytest.raises(ContractError, match="uncommitted_finalize_mode_invalid"):
            ingest_canonical_contract(
                conn,
                packet,
                commit=False,
                finalize_transaction=False,
            )
        conn.execute("BEGIN")
        with pytest.raises(
            ContractError,
            match="caller_transaction_requires_finalize_false",
        ):
            ingest_canonical_contract(conn, packet, commit=True)
        assert conn.in_transaction is True
        assert conn.execute("SELECT COUNT(*) FROM canonical_ssot_fact").fetchone()[0] == 0
        conn.rollback()

        conn.execute("BEGIN")
        staged = ingest_canonical_contract(
            conn,
            packet,
            commit=True,
            finalize_transaction=False,
        )
        assert staged["state"] == "STAGED_IN_CALLER_TRANSACTION"
        assert staged["inserted_count"] == 3
        assert conn.in_transaction is True
        assert conn.execute("SELECT COUNT(*) FROM canonical_ssot_fact").fetchone()[0] == 3
        conn.rollback()
        assert conn.execute("SELECT COUNT(*) FROM canonical_ssot_fact").fetchone()[0] == 0

        conn.execute("BEGIN")
        committed_by_caller = ingest_canonical_contract(
            conn,
            packet,
            commit=True,
            finalize_transaction=False,
        )
        assert committed_by_caller["state"] == "STAGED_IN_CALLER_TRANSACTION"
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM canonical_ssot_fact").fetchone()[0] == 3

        replay = ingest_canonical_contract(conn, packet, commit=True)
        assert replay["state"] == "COMMITTED"
        assert replay["inserted_count"] == 0
        assert replay["replayed_count"] == 3
        assert conn.execute("SELECT COUNT(*) FROM canonical_ssot_fact").fetchone()[0] == 3


def test_activation_generation_rewind_is_preflighted_with_zero_partial_writes() -> None:
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        g4 = _contract_with_activation(generation=4, activation_id="activation-g4")
        first = ingest_canonical_contract(conn, g4, commit=True)
        assert first["inserted_count"] == 4
        before = conn.execute(
            "SELECT fact_id,fact_type,scope_key,content_sha256,fact_json,lineage_json "
            "FROM canonical_ssot_fact ORDER BY fact_id"
        ).fetchall()

        g3 = _contract_with_activation(generation=3, activation_id="activation-g3")
        with pytest.raises(
            ContractError, match="canonical_activation_generation_rewind"
        ):
            ingest_canonical_contract(conn, g3, commit=True)

        after = conn.execute(
            "SELECT fact_id,fact_type,scope_key,content_sha256,fact_json,lineage_json "
            "FROM canonical_ssot_fact ORDER BY fact_id"
        ).fetchall()
        assert after == before
        assert not any("activation-g3" in str(row) for row in after)

        replay = ingest_canonical_contract(conn, g4, commit=True)
        assert replay["inserted_count"] == 0
        assert replay["replayed_count"] == 4


def _formal_truth_packet_and_projection() -> tuple[dict, dict]:
    truth = _truth()
    truth["source_role"] = "CANONICAL_AWC_SETTLEMENT_TRUTH_INGRESS_V1"
    truth["authority_scope"] = "FORMAL_NATIVE_ELIGIBLE"
    truth["content_sha256"] = content_sha256(
        {key: value for key, value in truth.items() if key != "content_sha256"}
    )
    identity = {
        "schema": SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "as_of_utc": "2026-08-03T12:00:00Z",
        "builder_id": "awc.closed_day.truth.v1",
        "model_facts": [],
        "ensemble_member_facts": [],
        "native_arrival_facts": [],
        "ensemble_member_receipt_manifest_facts": [],
        "ensemble_member_arrival_binding_facts": [],
        "truth_facts": [truth],
        "residual_facts": [],
        "activation_facts": [],
    }
    packet = {**identity, "content_sha256": content_sha256(identity)}
    legacy_identity = {
        "truth_id": truth["truth_id"],
        "station_id": truth["station_id"],
        "local_date": truth["event_date"],
        "settlement_timezone": truth["settlement_timezone"],
        "official_tmax_c": truth["truth_value"],
        "source_lane": "AWC",
        "settlement_definition": "official_daily_tmax_c",
        "revision_no": truth["truth_revision_no"],
        "published_at_utc": "2026-07-23T23:00:00Z",
        "received_at_utc": "2026-07-24T07:00:00Z",
        "canonical_committed_at_utc": truth["canonical_committed_at_utc"],
        "timestamps_complete": 1,
        "qc_status": "ACCEPTED",
        "source_role": truth["source_role"],
        "source_payload_sha256": truth["truth_content_sha256"],
        "observation_ids_json": "{\"observations\":[\"obs-1\"]}",
        "authority_scope": truth["authority_scope"],
    }
    projection = {
        "schema": AWC_DAILY_TRUTH_PROJECTION_SCHEMA,
        "truth_rows": [
            {
                **truth,
                "settlement_definition": legacy_identity[
                    "settlement_definition"
                ],
                "published_at_utc": legacy_identity["published_at_utc"],
                "received_at_utc": legacy_identity["received_at_utc"],
                "timestamps_complete": 1,
                "qc_status": "ACCEPTED",
                "observation_ids_json": legacy_identity["observation_ids_json"],
                "content_sha256": content_sha256(legacy_identity),
            }
        ],
    }
    return packet, projection


def _insert_awc_registry(conn: sqlite3.Connection) -> None:
    identity = {
        "station_id": "ZUUU",
        "icao_code": "ZUUU",
        "city_label": "Chengdu",
        "timezone_name": "Asia/Shanghai",
        "source_lane": "AWC",
        "settlement_definition": "official_daily_tmax_c",
        "registry_version": "canonical-test-v1",
        "source_role": "CANONICAL_AWC_SETTLEMENT_TRUTH_INGRESS_V1",
    }
    conn.execute(
        "INSERT INTO calibration_station_registry("
        "station_id,icao_code,city_label,timezone_name,source_lane,"
        "settlement_definition,registry_version,source_role,content_sha256"
        ") VALUES(?,?,?,?,?,?,?,?,?)",
        (*identity.values(), content_sha256(identity)),
    )


class _CommitFaultConnection:
    """Delegate an in-memory SQLite connection but fail only final commit."""

    def __init__(self, inner: sqlite3.Connection) -> None:
        self._inner = inner

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    def commit(self) -> None:
        raise sqlite3.OperationalError("injected_commit_fault")


def _projection_table_counts(conn: sqlite3.Connection) -> tuple[int, int]:
    return (
        conn.execute("SELECT COUNT(*) FROM canonical_ssot_fact").fetchone()[0],
        conn.execute(
            "SELECT COUNT(*) FROM calibration_station_daily_high_truth"
        ).fetchone()[0],
    )


def test_canonical_contract_stages_awc_truth_projection_with_caller_transaction() -> None:
    packet, projection = _formal_truth_packet_and_projection()
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        _insert_awc_registry(conn)
        conn.commit()

        preview = ingest_canonical_contract(
            conn,
            packet,
            awc_daily_truth_projection=projection,
        )
        assert preview["state"] == "VALIDATED_NO_WRITE"
        assert preview["awc_daily_truth_projection"]["state"] == "VALIDATED_NO_WRITE"
        assert conn.execute("SELECT COUNT(*) FROM canonical_ssot_fact").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM calibration_station_daily_high_truth").fetchone()[0] == 0

        conn.execute("BEGIN")
        staged = ingest_canonical_contract(
            conn,
            packet,
            commit=True,
            finalize_transaction=False,
            awc_daily_truth_projection=projection,
        )
        assert staged["state"] == "STAGED_IN_CALLER_TRANSACTION"
        assert staged["awc_daily_truth_projection"]["state"] == "COMMITTED"
        assert conn.execute("SELECT COUNT(*) FROM canonical_ssot_fact").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM calibration_station_registry").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM calibration_station_daily_high_truth").fetchone()[0] == 1
        conn.rollback()
        assert conn.execute("SELECT COUNT(*) FROM canonical_ssot_fact").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM calibration_station_registry").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM calibration_station_daily_high_truth").fetchone()[0] == 0

        conn.execute("BEGIN")
        committed_by_caller = ingest_canonical_contract(
            conn,
            packet,
            commit=True,
            finalize_transaction=False,
            awc_daily_truth_projection=projection,
        )
        assert committed_by_caller["state"] == "STAGED_IN_CALLER_TRANSACTION"
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM canonical_ssot_fact").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM calibration_station_registry").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM calibration_station_daily_high_truth").fetchone()[0] == 1


def test_awc_truth_commit_fault_rolls_back_then_replays_then_faults_again() -> None:
    packet, projection = _formal_truth_packet_and_projection()
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        _insert_awc_registry(conn)
        conn.commit()

        with pytest.raises(sqlite3.OperationalError, match="injected_commit_fault"):
            ingest_canonical_contract(
                _CommitFaultConnection(conn),
                packet,
                commit=True,
                awc_daily_truth_projection=projection,
            )
        assert conn.in_transaction is False
        assert _projection_table_counts(conn) == (0, 0)

        first = ingest_canonical_contract(
            conn,
            packet,
            commit=True,
            awc_daily_truth_projection=projection,
        )
        assert first["inserted_count"] == 1
        assert first["awc_daily_truth_projection"]["inserted_count"] == 1
        assert _projection_table_counts(conn) == (1, 1)

        replay = ingest_canonical_contract(
            conn,
            packet,
            commit=True,
            awc_daily_truth_projection=projection,
        )
        assert replay["inserted_count"] == 0
        assert replay["replayed_count"] == 1
        assert replay["awc_daily_truth_projection"]["replayed_count"] == 1
        assert _projection_table_counts(conn) == (1, 1)

        with pytest.raises(sqlite3.OperationalError, match="injected_commit_fault"):
            ingest_canonical_contract(
                _CommitFaultConnection(conn),
                packet,
                commit=True,
                awc_daily_truth_projection=projection,
            )
        assert conn.in_transaction is False
        assert _projection_table_counts(conn) == (1, 1)


def test_awc_truth_projection_conflict_rolls_back_canonical_append() -> None:
    packet, projection = _formal_truth_packet_and_projection()
    raw = dict(projection["truth_rows"][0])
    columns = (
        "truth_id",
        "station_id",
        "local_date",
        "settlement_timezone",
        "official_tmax_c",
        "source_lane",
        "settlement_definition",
        "revision_no",
        "published_at_utc",
        "received_at_utc",
        "canonical_committed_at_utc",
        "timestamps_complete",
        "qc_status",
        "source_role",
        "source_payload_sha256",
        "observation_ids_json",
        "authority_scope",
        "content_sha256",
    )
    raw["official_tmax_c"] = float(raw["truth_value"]) + 1.0
    raw["content_sha256"] = "c" * 64
    values = (
        raw["truth_id"],
        raw["station_id"],
        raw["event_date"],
        raw["settlement_timezone"],
        raw["official_tmax_c"],
        raw["source_lane"],
        raw["settlement_definition"],
        raw["truth_revision_no"],
        raw["published_at_utc"],
        raw["received_at_utc"],
        raw["canonical_committed_at_utc"],
        raw["timestamps_complete"],
        raw["qc_status"],
        raw["source_role"],
        raw["truth_content_sha256"],
        raw["observation_ids_json"],
        raw["authority_scope"],
        raw["content_sha256"],
    )
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        _insert_awc_registry(conn)
        conn.execute(
            "INSERT INTO calibration_station_daily_high_truth("
            + ",".join(columns)
            + ") VALUES("
            + ",".join("?" for _ in columns)
            + ")",
            values,
        )
        conn.commit()

        with pytest.raises(
            ContractError,
            match="canonical_awc_truth_projection_content_conflict",
        ):
            ingest_canonical_contract(
                conn,
                packet,
                commit=True,
                awc_daily_truth_projection=projection,
            )
        assert _projection_table_counts(conn) == (0, 1)


def test_awc_truth_projection_rejects_legacy_source_role_and_rolls_back() -> None:
    packet, projection = _formal_truth_packet_and_projection()
    projection["truth_rows"][0]["source_role"] = "CANONICAL_AMSC_AWC_INGRESS"
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        _insert_awc_registry(conn)
        conn.commit()

        with pytest.raises(
            ContractError,
            match="canonical_awc_truth_projection_canonical_mismatch:source_role",
        ):
            ingest_canonical_contract(
                conn,
                packet,
                commit=True,
                awc_daily_truth_projection=projection,
            )
        assert conn.execute("SELECT COUNT(*) FROM canonical_ssot_fact").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM calibration_station_daily_high_truth"
        ).fetchone()[0] == 0


def test_republisher_cannot_claim_native_authority() -> None:
    packet = _contract()
    packet["model_facts"][0]["native_arrival_indexed"] = True
    packet["model_facts"][0]["content_sha256"] = content_sha256(
        {key: value for key, value in packet["model_facts"][0].items() if key != "content_sha256"}
    )
    packet["content_sha256"] = content_sha256(
        {key: value for key, value in packet.items() if key != "content_sha256"}
    )
    with pytest.raises(ContractError, match="republisher_authority_forbidden"):
        CanonicalContract.from_mapping(packet)


def test_canonical_contract_rejects_amsc_truth() -> None:
    packet = _contract()
    truth = packet["truth_facts"][0]
    truth["source_lane"] = "AMSC"
    truth["content_sha256"] = content_sha256(
        {key: value for key, value in truth.items() if key != "content_sha256"}
    )
    packet["content_sha256"] = content_sha256(
        {key: value for key, value in packet.items() if key != "content_sha256"}
    )
    with pytest.raises(ContractError, match="canonical_truth_source_lane_invalid"):
        CanonicalContract.from_mapping(packet)


def test_canonical_contract_rejects_residual_timezone_mismatch() -> None:
    packet = _contract()
    model = packet["model_facts"][0]
    model["settlement_timezone"] = "America/New_York"
    model["content_sha256"] = content_sha256(
        {key: value for key, value in model.items() if key != "content_sha256"}
    )
    residual = packet["residual_facts"][0]
    residual["model_fact_content_sha256"] = model["content_sha256"]
    residual["content_sha256"] = content_sha256(
        {key: value for key, value in residual.items() if key != "content_sha256"}
    )
    packet["content_sha256"] = content_sha256(
        {key: value for key, value in packet.items() if key != "content_sha256"}
    )
    with pytest.raises(ContractError, match="canonical_residual_settlement_timezone_mismatch"):
        CanonicalContract.from_mapping(packet)
