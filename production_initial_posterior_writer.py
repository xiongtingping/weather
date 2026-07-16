"""Atomic production writer for P0 and its public probability SSOT fact."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping

from production_initial_posterior_checkpoint import SCHEMA_VERSION as COMPILER_SCHEMA
from production_probability_activation_contract import (
    ProductionProbabilityActivationError,
    validate_production_probability_activation,
)
from sequential_bayes_shadow_writer import materialize_checkpoint_row


SCHEMA_VERSION = "production_initial_posterior_writer_v1"
LINEAGE_TABLE = "production_initial_posterior_lineage"
LINEAGE_INDEX = "idx_production_initial_lineage_target"
LINEAGE_TABLE_DDL = f"""
    CREATE TABLE IF NOT EXISTS {LINEAGE_TABLE} (
        posterior_checkpoint_id TEXT PRIMARY KEY,
        target_identity TEXT NOT NULL,
        posterior_run_id TEXT NOT NULL UNIQUE,
        prior_context_id TEXT NOT NULL,
        prior_artifact_sha256 TEXT NOT NULL,
        prior_model_artifact_sha256 TEXT NOT NULL,
        weak_likelihood_artifact_sha256 TEXT NOT NULL,
        weak_likelihood_sha256 TEXT NOT NULL,
        forecast_evidence_snapshot_id TEXT NOT NULL,
        strong_lr_artifact_sha256 TEXT NOT NULL,
        source_reliability_initial_state_sha256 TEXT NOT NULL,
        resolution_contract_id TEXT NOT NULL,
        initial_evidence_sha256 TEXT NOT NULL UNIQUE,
        activation_contract_sha256 TEXT NOT NULL,
        lineage_json TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
"""
LINEAGE_INDEX_DDL = f"""
    CREATE INDEX IF NOT EXISTS {LINEAGE_INDEX}
    ON {LINEAGE_TABLE}(target_identity, created_at)
"""
LINEAGE_TRIGGER_DDL = {
    f"{LINEAGE_TABLE}_no_update": f"""
        CREATE TRIGGER IF NOT EXISTS {LINEAGE_TABLE}_no_update
        BEFORE UPDATE ON {LINEAGE_TABLE}
        BEGIN SELECT RAISE(ABORT, '{LINEAGE_TABLE}_append_only'); END
    """,
    f"{LINEAGE_TABLE}_no_delete": f"""
        CREATE TRIGGER IF NOT EXISTS {LINEAGE_TABLE}_no_delete
        BEFORE DELETE ON {LINEAGE_TABLE}
        BEGIN SELECT RAISE(ABORT, '{LINEAGE_TABLE}_append_only'); END
    """,
}


class ProductionInitialPosteriorConflict(RuntimeError):
    pass


class ProductionInitialPosteriorIntegrityError(RuntimeError):
    pass


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _trigger_exists(conn: sqlite3.Connection, trigger: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?", (trigger,)).fetchone())


def initialize_production_initial_posterior_schema(conn: sqlite3.Connection) -> None:
    """Install only the additive P0 lineage table and append-only guards."""

    conn.execute(LINEAGE_TABLE_DDL)
    conn.execute(LINEAGE_INDEX_DDL)
    for sql in LINEAGE_TRIGGER_DDL.values():
        conn.execute(sql)


def _assert_boundary(conn: sqlite3.Connection, activation: Mapping[str, Any]) -> dict[str, Any]:
    if conn.in_transaction:
        raise ProductionInitialPosteriorIntegrityError("production_initial_checkpoint_requires_no_ambient_transaction")
    if _table_exists(conn, "v2_shadow_runtime_manifest"):
        raise ProductionInitialPosteriorIntegrityError("production_initial_checkpoint_refuses_shadow_database")
    required = (
        "posterior_checkpoints", LINEAGE_TABLE, "event_fact_history", "event_fact_latest",
        "weak_forecast_evidence_snapshots",
    )
    missing = [table for table in required if not _table_exists(conn, table)]
    missing_triggers = [
        name for name in (f"{LINEAGE_TABLE}_no_update", f"{LINEAGE_TABLE}_no_delete")
        if not _trigger_exists(conn, name)
    ]
    if missing or missing_triggers:
        detail = ",".join([*(f"table:{item}" for item in missing), *(f"trigger:{item}" for item in missing_triggers)])
        raise ProductionInitialPosteriorIntegrityError("production_initial_checkpoint_schema_missing:" + detail)
    try:
        return validate_production_probability_activation(activation)
    except ProductionProbabilityActivationError as exc:
        raise ProductionInitialPosteriorIntegrityError(
            "production_initial_checkpoint_activation_not_authorized:" + str(exc)
        ) from exc


def _insert(conn: sqlite3.Connection, table: str, row: Mapping[str, Any]) -> None:
    fields = list(row)
    conn.execute(
        f"INSERT INTO {table}({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",
        tuple(row[field] for field in fields),
    )


def _public_fact_row(
    *, initial_checkpoint: Mapping[str, Any], checkpoint_row: Mapping[str, Any], slug: str,
    event_date: str, fact_as_of: str, correlation_id: str,
    activation: Mapping[str, Any],
) -> dict[str, Any]:
    initial = _mapping(initial_checkpoint)
    checkpoint = _mapping(checkpoint_row)
    bridge = _mapping(initial.get("resolution_bridge"))
    authorized = validate_production_probability_activation(activation)
    fact_snapshot_id = _text(_mapping(initial.get("checkpoint")).get("initial_evidence_snapshot_id"))
    na_reason = _text(bridge.get("market_bucket_probabilities_na_reason"))
    state = _text(bridge.get("state"))
    ssot = {
        "schema": "sequential_temperature_posterior_ssot_v2",
        "state": state,
        "writer_mode": "production",
        "checkpoint_kind": "initial",
        "fact_snapshot_id": fact_snapshot_id,
        "posterior_run_id": _text(checkpoint.get("posterior_run_id")),
        "posterior_checkpoint_id": _text(checkpoint.get("posterior_checkpoint_id")),
        "realization_state_id": "",
        "realization_state_na_reason": "initial_checkpoint_has_no_observation_realization",
        "resolution_contract_id": _text(checkpoint.get("resolution_contract_id")),
        "joint_posterior_sha256": _text(checkpoint.get("joint_posterior_sha256")),
        "physical_high_distribution": json.loads(_text(checkpoint.get("physical_high_distribution_json")) or "{}"),
        "settlement_scalar_distribution": json.loads(_text(checkpoint.get("settlement_scalar_distribution_json")) or "[]"),
        "market_bucket_probabilities": json.loads(_text(checkpoint.get("market_bucket_distribution_json")) or "{}"),
        "market_bucket_probabilities_na_reason": na_reason,
        "last_canonical_event_id": "",
        "last_strong_update_id": "",
        "observed_max_lower_bound_c": None,
        "committed_at": _text(checkpoint.get("committed_at")),
        "probability_runtime_identity": {
            "activation_contract_sha256": _text(authorized.get("activation_contract_sha256")),
            "runtime_manifest_sha256": _text(authorized.get("runtime_manifest_sha256")),
            "bayes_formula_id": _text(authorized.get("bayes_formula_id")),
            "composer_schema": _text(authorized.get("composer_schema")),
            "checkpoint_schema": _text(authorized.get("checkpoint_schema")),
            "strong_update_schema": _text(authorized.get("strong_update_schema")),
            "prior_model_artifact_sha256": _text(authorized.get("prior_model_artifact_sha256")),
            "weak_likelihood_artifact_sha256": _text(authorized.get("weak_likelihood_artifact_sha256")),
            "strong_lr_artifact_sha256": _text(authorized.get("strong_lr_artifact_sha256")),
        },
    }
    return {
        "run_date": _text(event_date),
        "slug": _text(slug),
        "event_date": _text(event_date),
        "fact_as_of": _text(fact_as_of),
        "composer_version": "sequential_bayes_v2_production",
        "fact_json": _json({"posterior_ssot_fact": ssot}),
        "created_at": _text(fact_as_of),
        "correlation_id": _text(correlation_id),
        "fact_snapshot_id": fact_snapshot_id,
        "posterior_run_id": _text(checkpoint.get("posterior_run_id")),
    }


def commit_production_initial_posterior(
    conn: sqlite3.Connection,
    *,
    activation: Mapping[str, Any],
    initial_checkpoint: Mapping[str, Any],
    slug: str,
    event_date: str,
    fact_as_of: str,
    correlation_id: str,
) -> dict[str, Any]:
    """Atomically append P0, immutable lineage, and the public SSOT pointer."""

    validated_activation = _assert_boundary(conn, activation)
    value = _mapping(initial_checkpoint)
    if (
        _text(value.get("schema")) != COMPILER_SCHEMA
        or value.get("ok") is not True
        or _text(value.get("compiler_state")) != "candidate_ready"
        or _text(value.get("initial_mode")) != "production"
        or value.get("shadow_only") is not False
    ):
        raise ValueError("production_initial_checkpoint_candidate_not_ready")
    checkpoint = _mapping(value.get("checkpoint"))
    lineage = _mapping(value.get("initial_lineage"))
    weak_snapshot_row = dict(_mapping(value.get("weak_forecast_evidence_snapshot_row")))
    bridge = _mapping(value.get("resolution_bridge"))
    reliability = _mapping(value.get("source_reliability_state"))
    trajectory = _mapping(value.get("trajectory_feature_state"))
    if (
        _text(checkpoint.get("checkpoint_kind")) != "initial"
        or checkpoint.get("consumed_event_ids") != []
        or _text(checkpoint.get("previous_checkpoint_id"))
        or _text(checkpoint.get("model_artifact_sha256")) != _text(validated_activation.get("strong_lr_artifact_sha256"))
    ):
        raise ProductionInitialPosteriorIntegrityError("production_initial_checkpoint_identity_invalid")
    if (
        _text(lineage.get("joint_posterior_sha256")) != _text(checkpoint.get("joint_posterior_sha256"))
        or _text(lineage.get("resolution_contract_id")) != _text(checkpoint.get("resolution_contract_id"))
        or _text(lineage.get("strong_lr_artifact_sha256")) != _text(checkpoint.get("model_artifact_sha256"))
        or _text(value.get("activation_contract_sha256")) != _text(validated_activation.get("activation_contract_sha256"))
    ):
        raise ProductionInitialPosteriorIntegrityError("production_initial_checkpoint_lineage_binding_mismatch")
    if (
        _text(weak_snapshot_row.get("forecast_evidence_snapshot_id")) != _text(lineage.get("forecast_evidence_snapshot_id"))
        or _text(weak_snapshot_row.get("weak_likelihood_artifact_sha256")) != _text(lineage.get("weak_likelihood_artifact_sha256"))
        or _text(weak_snapshot_row.get("weak_likelihood_sha256")) != _text(lineage.get("weak_likelihood_sha256"))
        or _text(weak_snapshot_row.get("target_identity")) != _text(checkpoint.get("target_identity"))
        or _text(weak_snapshot_row.get("state")) != "active"
    ):
        raise ProductionInitialPosteriorIntegrityError("production_initial_weak_snapshot_binding_mismatch")
    checkpoint_row = materialize_checkpoint_row(
        checkpoint,
        resolution_bridge=bridge,
        trajectory_feature_state=trajectory,
        source_reliability_state=reliability,
    )
    checkpoint_row["state"] = "committed"
    target = _text(checkpoint_row.get("target_identity"))
    checkpoint_id = _text(checkpoint_row.get("posterior_checkpoint_id"))
    lineage_row = {
        "posterior_checkpoint_id": checkpoint_id,
        "target_identity": target,
        "posterior_run_id": _text(checkpoint_row.get("posterior_run_id")),
        "prior_context_id": _text(lineage.get("prior_context_id")),
        "prior_artifact_sha256": _text(lineage.get("prior_artifact_sha256")),
        "prior_model_artifact_sha256": _text(lineage.get("prior_model_artifact_sha256")),
        "weak_likelihood_artifact_sha256": _text(lineage.get("weak_likelihood_artifact_sha256")),
        "weak_likelihood_sha256": _text(lineage.get("weak_likelihood_sha256")),
        "forecast_evidence_snapshot_id": _text(lineage.get("forecast_evidence_snapshot_id")),
        "strong_lr_artifact_sha256": _text(lineage.get("strong_lr_artifact_sha256")),
        "source_reliability_initial_state_sha256": _text(lineage.get("source_reliability_initial_state_sha256")),
        "resolution_contract_id": _text(lineage.get("resolution_contract_id")),
        "initial_evidence_sha256": _text(lineage.get("initial_evidence_sha256")),
        "activation_contract_sha256": _text(validated_activation.get("activation_contract_sha256")),
        "lineage_json": _json(lineage),
        "created_at": _text(checkpoint_row.get("committed_at")),
    }
    if any(not _text(item) for item in lineage_row.values()):
        raise ValueError("production_initial_checkpoint_lineage_fields_missing")
    public_row = _public_fact_row(
        initial_checkpoint=value,
        checkpoint_row=checkpoint_row,
        slug=slug,
        event_date=event_date,
        fact_as_of=fact_as_of,
        correlation_id=correlation_id,
        activation=validated_activation,
    )
    if not all(_text(public_row.get(key)) for key in ("slug", "event_date", "fact_as_of", "correlation_id", "fact_snapshot_id")):
        raise ValueError("production_initial_checkpoint_public_fact_identity_missing")

    conn.execute("BEGIN IMMEDIATE")
    try:
        targets = conn.execute(
            """SELECT posterior_checkpoint_id,replay_generation
               FROM posterior_checkpoints WHERE target_identity=?
               ORDER BY replay_generation DESC,rowid DESC""",
            (target,),
        ).fetchall()
        if targets and all(_text(row[0]) != checkpoint_id for row in targets):
            current_activation_count = int(conn.execute(
                f"SELECT COUNT(*) FROM {LINEAGE_TABLE} WHERE target_identity=? AND activation_contract_sha256=?",
                (target, _text(validated_activation.get("activation_contract_sha256"))),
            ).fetchone()[0])
            expected_generation = max(int(row[1] or 0) for row in targets) + 1
            if current_activation_count:
                raise ProductionInitialPosteriorConflict("production_initial_activation_already_initialized")
            if (
                int(checkpoint_row.get("replay_generation") or 0) != expected_generation
                or _text(checkpoint_row.get("supersedes_checkpoint_id")) != _text(targets[0][0])
            ):
                raise ProductionInitialPosteriorConflict("production_initial_successor_generation_invalid")
        existing_checkpoint = conn.execute(
            "SELECT posterior_run_id,joint_posterior_sha256 FROM posterior_checkpoints WHERE posterior_checkpoint_id=?",
            (checkpoint_id,),
        ).fetchone()
        existing_lineage = conn.execute(
            f"SELECT lineage_json,activation_contract_sha256 FROM {LINEAGE_TABLE} WHERE posterior_checkpoint_id=?",
            (checkpoint_id,),
        ).fetchone()
        existing_fact = conn.execute(
            """SELECT id,fact_json,posterior_run_id FROM event_fact_history
               WHERE fact_snapshot_id=? AND fact_snapshot_id<>''""",
            (_text(public_row.get("fact_snapshot_id")),),
        ).fetchone()
        existing_weak_snapshot = conn.execute(
            "SELECT snapshot_sha256 FROM weak_forecast_evidence_snapshots WHERE forecast_evidence_snapshot_id=?",
            (_text(weak_snapshot_row.get("forecast_evidence_snapshot_id")),),
        ).fetchone()
        present = (existing_checkpoint is not None, existing_lineage is not None, existing_fact is not None)
        if any(present) and not all(present):
            raise ProductionInitialPosteriorIntegrityError("production_initial_checkpoint_partial_state_detected")
        if all(present):
            if (
                _text(existing_checkpoint[0]) != _text(checkpoint_row.get("posterior_run_id"))
                or _text(existing_checkpoint[1]) != _text(checkpoint_row.get("joint_posterior_sha256"))
                or _text(existing_lineage[0]) != _json(lineage)
                or _text(existing_lineage[1]) != _text(validated_activation.get("activation_contract_sha256"))
                or _text(existing_fact[1]) != _text(public_row.get("fact_json"))
                or _text(existing_fact[2]) != _text(checkpoint_row.get("posterior_run_id"))
                or existing_weak_snapshot is None
                or _text(existing_weak_snapshot[0]) != _text(weak_snapshot_row.get("snapshot_sha256"))
            ):
                raise ProductionInitialPosteriorIntegrityError("production_initial_checkpoint_existing_identity_drift")
            fact_id = int(existing_fact[0])
            idempotent = True
        else:
            if existing_weak_snapshot is not None and _text(existing_weak_snapshot[0]) != _text(weak_snapshot_row.get("snapshot_sha256")):
                raise ProductionInitialPosteriorIntegrityError("production_initial_weak_snapshot_existing_identity_drift")
            conn.execute(
                "UPDATE weak_forecast_evidence_snapshots SET state='superseded' WHERE target_identity=? AND state='active'",
                (target,),
            )
            if existing_weak_snapshot is None:
                _insert(conn, "weak_forecast_evidence_snapshots", weak_snapshot_row)
            else:
                conn.execute(
                    "UPDATE weak_forecast_evidence_snapshots SET state='active' WHERE forecast_evidence_snapshot_id=?",
                    (_text(weak_snapshot_row.get("forecast_evidence_snapshot_id")),),
                )
            _insert(conn, "posterior_checkpoints", checkpoint_row)
            _insert(conn, LINEAGE_TABLE, lineage_row)
            _insert(conn, "event_fact_history", public_row)
            fact_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            idempotent = False
        conn.execute(
            """INSERT INTO event_fact_latest(slug,event_date,fact_id,updated_at,fact_snapshot_id,correlation_id)
               VALUES(?,?,?,?,?,?) ON CONFLICT(slug,event_date) DO UPDATE SET
                 fact_id=excluded.fact_id,updated_at=excluded.updated_at,
                 fact_snapshot_id=excluded.fact_snapshot_id,correlation_id=excluded.correlation_id""",
            (
                _text(public_row.get("slug")), _text(public_row.get("event_date")), fact_id,
                _text(public_row.get("fact_as_of")), _text(public_row.get("fact_snapshot_id")),
                _text(public_row.get("correlation_id")),
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "committed",
        "production": True,
        "idempotent": idempotent,
        "posterior_checkpoint_id": checkpoint_id,
        "posterior_run_id": _text(checkpoint_row.get("posterior_run_id")),
        "fact_snapshot_id": _text(public_row.get("fact_snapshot_id")),
        "fact_id": fact_id,
        "places_orders": False,
        "creates_outbox": False,
        "creates_realization": False,
        "creates_strong_update": False,
    }


__all__ = [
    "LINEAGE_TABLE",
    "ProductionInitialPosteriorConflict",
    "ProductionInitialPosteriorIntegrityError",
    "SCHEMA_VERSION",
    "commit_production_initial_posterior",
    "initialize_production_initial_posterior_schema",
]
