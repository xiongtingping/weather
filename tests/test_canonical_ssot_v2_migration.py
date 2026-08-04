from __future__ import annotations

import json
import sqlite3

import pytest

from bayes_basket_target.canonical_contract import CONTRACT_VERSION, SCHEMA
from bayes_basket_target.canonical_ingress import ingest_canonical_contract
from bayes_basket_target.canonical_ssot_v2_migration import (
    migrate_canonical_ssot_v2,
)
from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.ssot import apply_production_ssot_schema


def _model() -> dict:
    identity = {
        "station_id": "ZBAA",
        "model_id": "ECMWF",
        "ensemble_system_id": "",
        "model_family": "global_deterministic",
        "provider_id": "ECMWF_IFS",
        "source_class": "aggregator_republisher",
        "event_date": "2026-07-01",
        "run_init_utc": "2026-06-30T00:00:00Z",
        "lead_bucket": "d1",
        "run_cycle": "PREVIOUS_DAY1",
        "settlement_timezone": "Asia/Shanghai",
        "forecast_value": 31.0,
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
    return {**identity, "content_sha256": content_sha256(identity)}


def _truth(*, role: str = "AWC_HISTORICAL_REPLAY_INGRESS") -> dict:
    identity = {
        "truth_id": "truth-zbaa-20260701-v2",
        "station_id": "ZBAA",
        "event_date": "2026-07-01",
        "truth_revision_no": 0,
        "truth_value": 32.0,
        "truth_content_sha256": "b" * 64,
        "canonical_committed_at_utc": "2026-08-03T12:00:00Z",
        "station_day_end_utc": "2026-07-01T16:00:00Z",
        "closed_day": True,
        "source_lane": "AWC",
        "source_role": role,
        "authority_scope": (
            "PRODUCTION_NO_LIVE_ONLY"
            if role == "AWC_HISTORICAL_REPLAY_INGRESS"
            else "FORMAL_NATIVE_ELIGIBLE"
        ),
        "settlement_timezone": "Asia/Shanghai",
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _contract(*, models: list[dict], truths: list[dict], builder: str) -> dict:
    identity = {
        "schema": SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "as_of_utc": "2026-08-03T12:00:00Z",
        "builder_id": builder,
        "model_facts": models,
        "ensemble_member_facts": [],
        "native_arrival_facts": [],
        "ensemble_member_receipt_manifest_facts": [],
        "ensemble_member_arrival_binding_facts": [],
        "truth_facts": truths,
        "residual_facts": [],
        "activation_facts": [],
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _insert_invalid_v1_truth(conn: sqlite3.Connection) -> None:
    raw = _truth()
    raw.pop("source_role")
    raw["content_sha256"] = content_sha256(
        {key: value for key, value in raw.items() if key != "content_sha256"}
    )
    content_hash = raw["content_sha256"]
    conn.execute(
        "INSERT INTO canonical_ssot_fact(fact_id,fact_type,scope_key,content_sha256,fact_json,lineage_json,ingested_at_utc) "
        "VALUES(?,?,?,?,?,?,?)",
        (
            "truth:" + content_hash,
            "truth",
            "ZBAA|2026-07-01|0",
            content_hash,
            json.dumps(raw, sort_keys=True, separators=(",", ":")),
            "{}",
            "2026-08-01T00:00:00Z",
        ),
    )
    conn.commit()


def _source_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE canonical_ssot_fact("
        "fact_id TEXT PRIMARY KEY,"
        "fact_type TEXT NOT NULL CHECK(fact_type IN ('model','truth','residual','activation'))," 
        "scope_key TEXT NOT NULL,content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),"
        "fact_json TEXT NOT NULL,lineage_json TEXT NOT NULL,ingested_at_utc TEXT NOT NULL,"
        "UNIQUE(fact_type,content_sha256),UNIQUE(fact_type,scope_key));"
        "CREATE TRIGGER canonical_ssot_fact_no_update BEFORE UPDATE ON canonical_ssot_fact "
        "BEGIN SELECT RAISE(ABORT,'append_only'); END;"
        "CREATE TRIGGER canonical_ssot_fact_no_delete BEFORE DELETE ON canonical_ssot_fact "
        "BEGIN SELECT RAISE(ABORT,'append_only'); END;"
    )
    ingest_canonical_contract(
        conn,
        _contract(models=[_model()], truths=[], builder="model.seed"),
        commit=True,
    )
    _insert_invalid_v1_truth(conn)
    return conn


def test_v2_migration_preview_is_zero_write() -> None:
    conn = _source_db()
    try:
        before = conn.execute("SELECT COUNT(*) FROM canonical_ssot_fact").fetchone()[0]
        receipt = migrate_canonical_ssot_v2(
            conn,
            _contract(models=[], truths=[_truth()], builder="truth.replay"),
            commit=False,
            migrated_at_utc="2026-08-04T00:00:00Z",
        )
        assert receipt["state"] == "VALIDATED_NO_WRITE"
        assert receipt["source_counts"]["retired_invalid_truth"] == 1
        assert receipt["active_counts_after"] == {"model": 1, "truth": 1}
        assert conn.execute("SELECT COUNT(*) FROM canonical_ssot_fact").fetchone()[0] == before
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='canonical_ssot_fact_retired_v1'"
        ).fetchone() is None
    finally:
        conn.close()


def test_v2_migration_atomically_retires_invalid_facts() -> None:
    conn = _source_db()
    try:
        receipt = migrate_canonical_ssot_v2(
            conn,
            _contract(models=[], truths=[_truth()], builder="truth.replay"),
            commit=True,
            migrated_at_utc="2026-08-04T00:00:00Z",
        )
        assert receipt["state"] == "COMMITTED"
        assert conn.execute("SELECT COUNT(*) FROM canonical_ssot_fact").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM canonical_ssot_fact_retired_v1").fetchone()[0] == 2
        active_types = dict(
            conn.execute(
                "SELECT fact_type,COUNT(*) FROM canonical_ssot_fact GROUP BY fact_type"
            )
        )
        assert active_types == {"model": 1, "truth": 1}
        replay = ingest_canonical_contract(
            conn,
            _contract(
                models=[_model()],
                truths=[_truth()],
                builder="post-migration.exact-replay",
            ),
            commit=False,
        )
        assert replay["state"] == "VALIDATED_NO_WRITE"
        assert replay["replayed_count"] == 2
        with pytest.raises(sqlite3.IntegrityError, match="append_only"):
            conn.execute("DELETE FROM canonical_ssot_fact")
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="append_only"):
            conn.execute("DELETE FROM canonical_ssot_fact_retired_v1")
        conn.rollback()
    finally:
        conn.close()


def test_v2_migration_refuses_unowned_valid_truth() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        apply_production_ssot_schema(conn)
        ingest_canonical_contract(
            conn,
            _contract(models=[_model()], truths=[_truth()], builder="already.valid"),
            commit=True,
        )
        with pytest.raises(
            ContractError,
            match="preexisting_valid_truth_requires_explicit_contract",
        ):
            migrate_canonical_ssot_v2(
                conn,
                _contract(models=[], truths=[_truth()], builder="truth.replay"),
                commit=False,
            )
        assert conn.execute("SELECT COUNT(*) FROM canonical_ssot_fact").fetchone()[0] == 2
    finally:
        conn.close()
