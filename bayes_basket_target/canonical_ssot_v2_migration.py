"""One-time canonical fact-store rebuild without adapting invalid legacy facts.

The active v1 table contains valid model facts plus truth rows that predate the
required ``source_role`` field.  Those truth rows, their residuals and their
activations cannot be repaired in place.  This migration validates and carries
forward only exact canonical model facts, adds an independently built
historical AWC truth contract, and atomically retires the old table.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3
from typing import Any, Mapping

from .canonical_contract import (
    CONTRACT_VERSION,
    SCHEMA as CANONICAL_CONTRACT_SCHEMA,
    CanonicalActivationFact,
    CanonicalContract,
    CanonicalModelFact,
    CanonicalResidualFact,
    CanonicalTruthFact,
)
from .contracts import ContractError, content_sha256


MIGRATION_SCHEMA = "bayes_basket_target_canonical_ssot_v2_migration_v1"
ACTIVE_TABLE = "canonical_ssot_fact"
STAGING_TABLE = "canonical_ssot_fact_v2_staging"
RETIRED_TABLE = "canonical_ssot_fact_retired_v1"
FACT_CLASSES = {
    "model": CanonicalModelFact,
    "truth": CanonicalTruthFact,
    "residual": CanonicalResidualFact,
    "activation": CanonicalActivationFact,
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _active_snapshot(conn: sqlite3.Connection) -> tuple[list[dict[str, Any]], dict[str, int], str]:
    if not _table_exists(conn, ACTIVE_TABLE):
        raise ContractError("canonical_v2_active_table_missing")
    valid_models: list[dict[str, Any]] = []
    counts = {
        "source_total": 0,
        "valid_model": 0,
        "retired_invalid_truth": 0,
        "retired_legacy_residual": 0,
        "retired_legacy_activation": 0,
    }
    root_rows: list[dict[str, str]] = []
    for fact_id, fact_type, scope_key, content_hash, fact_json, lineage_json, ingested_at in conn.execute(
        f"SELECT fact_id,fact_type,scope_key,content_sha256,fact_json,lineage_json,ingested_at_utc "
        f"FROM {ACTIVE_TABLE} ORDER BY fact_type,scope_key,fact_id"
    ):
        counts["source_total"] += 1
        root_rows.append(
            {
                "fact_id": str(fact_id),
                "fact_type": str(fact_type),
                "scope_key": str(scope_key),
                "content_sha256": str(content_hash),
                "lineage_json_sha256": content_sha256(str(lineage_json)),
                "ingested_at_utc": str(ingested_at),
            }
        )
        try:
            raw = json.loads(str(fact_json))
        except json.JSONDecodeError as exc:
            raise ContractError("canonical_v2_source_fact_json_invalid") from exc
        if not isinstance(raw, Mapping):
            raise ContractError("canonical_v2_source_fact_mapping_required")
        if fact_type == "model":
            model = CanonicalModelFact.from_mapping(raw)
            if (
                str(fact_id) != "model:" + model.content_sha256
                or str(content_hash) != model.content_sha256
                or str(scope_key) != model.scope_key
            ):
                raise ContractError("canonical_v2_source_model_storage_mismatch")
            valid_models.append(model.to_mapping())
            counts["valid_model"] += 1
        elif fact_type == "truth":
            try:
                CanonicalTruthFact.from_mapping(raw)
            except ContractError as exc:
                if str(exc) != "canonical_truth_fact_missing_field:source_role":
                    raise ContractError("canonical_v2_unexpected_truth_error:" + str(exc)) from exc
                counts["retired_invalid_truth"] += 1
            else:
                # A valid truth must be supplied by the new independently
                # verified contract so its source envelope is bound in one
                # place; silently copying it would create a second authority.
                raise ContractError("canonical_v2_preexisting_valid_truth_requires_explicit_contract")
        elif fact_type == "residual":
            CanonicalResidualFact.from_mapping(raw)
            counts["retired_legacy_residual"] += 1
        elif fact_type == "activation":
            CanonicalActivationFact.from_mapping(raw)
            counts["retired_legacy_activation"] += 1
        else:
            raise ContractError("canonical_v2_source_fact_type_invalid")
    return valid_models, counts, content_sha256(root_rows)


def _bootstrap_contract(
    models: list[dict[str, Any]],
    truth_contract: CanonicalContract,
    *,
    as_of_utc: str,
) -> CanonicalContract:
    if (
        truth_contract.model_facts
        or truth_contract.ensemble_member_facts
        or truth_contract.native_arrival_facts
        or truth_contract.ensemble_member_receipt_manifest_facts
        or truth_contract.ensemble_member_arrival_binding_facts
        or truth_contract.residual_facts
        or truth_contract.activation_facts
    ):
        raise ContractError("canonical_v2_truth_contract_not_truth_only")
    identity = {
        "schema": CANONICAL_CONTRACT_SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "as_of_utc": as_of_utc,
        "builder_id": "canonical.ssot.v2.bootstrap.v1",
        "model_facts": models,
        "ensemble_member_facts": [],
        "native_arrival_facts": [],
        "ensemble_member_receipt_manifest_facts": [],
        "ensemble_member_arrival_binding_facts": [],
        "truth_facts": [fact.to_mapping() for fact in truth_contract.truth_facts],
        "residual_facts": [],
        "activation_facts": [],
    }
    return CanonicalContract.from_mapping(
        {**identity, "content_sha256": content_sha256(identity)}
    )


def _fact_rows(contract: CanonicalContract, *, ingested_at_utc: str) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    for fact_type, facts in (
        ("model", contract.model_facts),
        ("truth", contract.truth_facts),
        ("residual", contract.residual_facts),
        ("activation", contract.activation_facts),
    ):
        for fact in facts:
            mapping = fact.to_mapping()
            hash_field = "activation_content_sha256" if fact_type == "activation" else "content_sha256"
            content_hash = str(mapping[hash_field])
            lineage = {
                key: mapping[key]
                for key in (
                    "provider_id",
                    "source_class",
                    "raw_object_sha256",
                    "truth_content_sha256",
                    "source_role",
                    "authority_scope",
                    "oos_role",
                    "native_arrival_indexed",
                    "p1_eligible",
                    "formal_live_authority",
                )
                if key in mapping
            }
            rows.append(
                (
                    f"{fact_type}:{content_hash}",
                    fact_type,
                    fact.scope_key,
                    content_hash,
                    json.dumps(mapping, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    json.dumps(lineage, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    ingested_at_utc,
                )
            )
    return rows


def _create_table(conn: sqlite3.Connection, name: str) -> None:
    conn.execute(
        f"CREATE TABLE {name} ("
        "fact_id TEXT PRIMARY KEY,"
        "fact_type TEXT NOT NULL CHECK(fact_type IN ('model','truth','residual','activation','activation_supersession'))," 
        "scope_key TEXT NOT NULL,"
        "content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),"
        "fact_json TEXT NOT NULL,"
        "lineage_json TEXT NOT NULL,"
        "ingested_at_utc TEXT NOT NULL,"
        "UNIQUE(fact_type,content_sha256),"
        "UNIQUE(fact_type,scope_key))"
    )


def _root(conn: sqlite3.Connection, table: str) -> str:
    rows = [
        {
            "fact_id": str(row[0]),
            "fact_type": str(row[1]),
            "scope_key": str(row[2]),
            "content_sha256": str(row[3]),
        }
        for row in conn.execute(
            f"SELECT fact_id,fact_type,scope_key,content_sha256 FROM {table} "
            "ORDER BY fact_type,scope_key,fact_id"
        )
    ]
    return content_sha256(rows)


def migrate_canonical_ssot_v2(
    conn: sqlite3.Connection,
    truth_contract_raw: Mapping[str, Any],
    *,
    commit: bool = False,
    migrated_at_utc: str | None = None,
) -> dict[str, Any]:
    if conn.in_transaction:
        raise ContractError("canonical_v2_caller_transaction_forbidden")
    if _table_exists(conn, RETIRED_TABLE) or _table_exists(conn, STAGING_TABLE):
        raise ContractError("canonical_v2_migration_already_started")
    truth_contract = CanonicalContract.from_mapping(truth_contract_raw)
    models, source_counts, source_root = _active_snapshot(conn)
    migration_time = migrated_at_utc or _utc_now()
    bootstrap = _bootstrap_contract(
        models,
        truth_contract,
        as_of_utc=truth_contract.as_of_utc,
    )
    rows = _fact_rows(bootstrap, ingested_at_utc=migration_time)
    conn.execute("BEGIN IMMEDIATE" if commit else "BEGIN")
    try:
        _create_table(conn, STAGING_TABLE)
        conn.executemany(
            f"INSERT INTO {STAGING_TABLE}(fact_id,fact_type,scope_key,content_sha256,fact_json,lineage_json,ingested_at_utc) "
            "VALUES(?,?,?,?,?,?,?)",
            rows,
        )
        staging_counts = {
            str(fact_type): int(count)
            for fact_type, count in conn.execute(
                f"SELECT fact_type,COUNT(*) FROM {STAGING_TABLE} GROUP BY fact_type"
            )
        }
        staging_root = _root(conn, STAGING_TABLE)
        if staging_counts != {
            "model": len(bootstrap.model_facts),
            "truth": len(bootstrap.truth_facts),
        }:
            raise ContractError("canonical_v2_staging_count_mismatch")
        if commit:
            conn.execute("DROP TRIGGER IF EXISTS canonical_ssot_fact_no_update")
            conn.execute("DROP TRIGGER IF EXISTS canonical_ssot_fact_no_delete")
            conn.execute(f"ALTER TABLE {ACTIVE_TABLE} RENAME TO {RETIRED_TABLE}")
            conn.execute(f"ALTER TABLE {STAGING_TABLE} RENAME TO {ACTIVE_TABLE}")
            conn.execute(
                "CREATE TRIGGER canonical_ssot_fact_no_update BEFORE UPDATE ON canonical_ssot_fact "
                "BEGIN SELECT RAISE(ABORT,'append_only'); END"
            )
            conn.execute(
                "CREATE TRIGGER canonical_ssot_fact_no_delete BEFORE DELETE ON canonical_ssot_fact "
                "BEGIN SELECT RAISE(ABORT,'append_only'); END"
            )
            conn.execute(
                "CREATE TRIGGER canonical_ssot_fact_retired_v1_no_update BEFORE UPDATE ON canonical_ssot_fact_retired_v1 "
                "BEGIN SELECT RAISE(ABORT,'append_only'); END"
            )
            conn.execute(
                "CREATE TRIGGER canonical_ssot_fact_retired_v1_no_delete BEFORE DELETE ON canonical_ssot_fact_retired_v1 "
                "BEGIN SELECT RAISE(ABORT,'append_only'); END"
            )
            conn.commit()
        else:
            conn.rollback()
    except Exception:
        conn.rollback()
        raise
    identity = {
        "schema": MIGRATION_SCHEMA,
        "state": "COMMITTED" if commit else "VALIDATED_NO_WRITE",
        "source_table": ACTIVE_TABLE,
        "retired_table": RETIRED_TABLE,
        "source_root_sha256": source_root,
        "source_counts": source_counts,
        "truth_contract_sha256": truth_contract.content_sha256,
        "bootstrap_contract_sha256": bootstrap.content_sha256,
        "staging_root_sha256": staging_root,
        "active_counts_after": staging_counts,
        "migrated_at_utc": migration_time,
        "writes_target_database": commit,
        "writes_production_database": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    return {**identity, "receipt_sha256": content_sha256(identity)}


__all__ = [
    "ACTIVE_TABLE",
    "MIGRATION_SCHEMA",
    "RETIRED_TABLE",
    "STAGING_TABLE",
    "migrate_canonical_ssot_v2",
]
