"""Atomic P0 writer for the report-defined-X successor activation."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Mapping

from initial_posterior_checkpoint_shadow import materialize_checkpoint_row
from production_initial_posterior_writer import (
    LINEAGE_TABLE,
    ProductionInitialPosteriorConflict,
    ProductionInitialPosteriorIntegrityError,
)
from production_reported_x_initial_checkpoint import SCHEMA_VERSION as COMPILER_SCHEMA
from production_reported_x_public_ssot_writer import (
    _assert_boundary,
    materialize_reported_x_initial_public_fact_row,
)
from reported_x_successor_checkpoint_runtime import validate_successor_checkpoint_row


SCHEMA_VERSION = "production_reported_x_initial_writer_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _insert(conn: sqlite3.Connection, table: str, row: Mapping[str, Any]) -> None:
    fields = list(row)
    conn.execute(
        f"INSERT INTO {table}({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",
        tuple(row[field] for field in fields),
    )


def _checkpoint_row(value: Mapping[str, Any]) -> dict[str, Any]:
    checkpoint = dict(_mapping(value.get("checkpoint")))
    bridge = _mapping(value.get("resolution_bridge"))
    trajectory = _mapping(value.get("trajectory_feature_state"))
    reliability = _mapping(value.get("source_reliability_state"))
    successor_columns = dict(_mapping(value.get("successor_checkpoint_columns")))
    # Reuse the stable base-row materializer for lineage/storage fields only.
    # The ambiguous legacy probability column is deliberately empty.
    base_checkpoint = {**checkpoint, "physical_high_distribution": {}}
    row = materialize_checkpoint_row(
        base_checkpoint,
        resolution_bridge=bridge,
        trajectory_feature_state=trajectory,
        source_reliability_state=reliability,
    )
    row.update(successor_columns)
    row["physical_high_distribution_json"] = "{}"
    latent = json.loads(row["latent_scheduled_slot_high_distribution_json"])
    row["temperature_grid_id"] = _sha(sorted(latent, key=float))
    row["state"] = "committed"
    validation = validate_successor_checkpoint_row(row)
    if validation.get("ok") is not True:
        raise ProductionInitialPosteriorIntegrityError(
            "reported_x_initial_checkpoint_row_invalid:"
            + ",".join(validation.get("issues") or [])
        )
    return row


def commit_reported_x_initial_posterior(
    conn: sqlite3.Connection,
    *,
    activation: Mapping[str, Any],
    initial_checkpoint: Mapping[str, Any],
    slug: str,
    event_date: str,
    fact_as_of: str,
    correlation_id: str,
) -> dict[str, Any]:
    authorized = _assert_boundary(conn, activation)
    value = _mapping(initial_checkpoint)
    if (
        _text(value.get("schema")) != COMPILER_SCHEMA
        or value.get("ok") is not True
        or _text(value.get("compiler_state")) != "candidate_ready"
        or _text(value.get("initial_mode")) != "production"
        or value.get("shadow_only") is not False
    ):
        raise ValueError("reported_x_initial_candidate_not_ready")
    checkpoint = _mapping(value.get("checkpoint"))
    lineage = dict(_mapping(value.get("initial_lineage")))
    weak_snapshot = dict(_mapping(value.get("weak_forecast_evidence_snapshot_row")))
    bridge = _mapping(value.get("resolution_bridge"))
    if (
        _text(checkpoint.get("checkpoint_kind")) != "initial"
        or checkpoint.get("consumed_event_ids") != []
        or _text(checkpoint.get("previous_checkpoint_id"))
        or _text(checkpoint.get("model_artifact_sha256"))
        != _text(authorized.get("strong_lr_artifact_sha256"))
    ):
        raise ProductionInitialPosteriorIntegrityError(
            "reported_x_initial_checkpoint_identity_invalid"
        )
    if (
        _text(lineage.get("joint_posterior_sha256"))
        != _text(checkpoint.get("joint_posterior_sha256"))
        or _text(lineage.get("resolution_contract_id"))
        != _text(checkpoint.get("resolution_contract_id"))
        or _text(lineage.get("reported_x_projection_sha256"))
        != _text(checkpoint.get("reported_x_projection_sha256"))
        or _text(value.get("activation_contract_sha256"))
        != _text(authorized.get("activation_contract_sha256"))
    ):
        raise ProductionInitialPosteriorIntegrityError(
            "reported_x_initial_lineage_binding_mismatch"
        )
    row = _checkpoint_row(value)
    target = _text(row.get("target_identity"))
    checkpoint_id = _text(row.get("posterior_checkpoint_id"))
    lineage_row = {
        "posterior_checkpoint_id": checkpoint_id,
        "target_identity": target,
        "posterior_run_id": _text(row.get("posterior_run_id")),
        "prior_context_id": _text(lineage.get("prior_context_id")),
        "prior_artifact_sha256": _text(lineage.get("prior_artifact_sha256")),
        "prior_model_artifact_sha256": _text(lineage.get("prior_model_artifact_sha256")),
        "weak_likelihood_artifact_sha256": _text(
            lineage.get("weak_likelihood_artifact_sha256")
        ),
        "weak_likelihood_sha256": _text(lineage.get("weak_likelihood_sha256")),
        "forecast_evidence_snapshot_id": _text(
            lineage.get("forecast_evidence_snapshot_id")
        ),
        "strong_lr_artifact_sha256": _text(lineage.get("strong_lr_artifact_sha256")),
        "source_reliability_initial_state_sha256": _text(
            lineage.get("source_reliability_initial_state_sha256")
        ),
        "resolution_contract_id": _text(lineage.get("resolution_contract_id")),
        "initial_evidence_sha256": _text(lineage.get("initial_evidence_sha256")),
        "activation_contract_sha256": _text(
            authorized.get("activation_contract_sha256")
        ),
        "lineage_json": _json(lineage),
        "created_at": _text(row.get("committed_at")),
    }
    if any(not _text(item) for item in lineage_row.values()):
        raise ValueError("reported_x_initial_lineage_fields_missing")
    if (
        _text(weak_snapshot.get("forecast_evidence_snapshot_id"))
        != _text(lineage.get("forecast_evidence_snapshot_id"))
        or _text(weak_snapshot.get("weak_likelihood_artifact_sha256"))
        != _text(lineage.get("weak_likelihood_artifact_sha256"))
        or _text(weak_snapshot.get("target_identity")) != target
        or _text(weak_snapshot.get("state")) != "active"
    ):
        raise ProductionInitialPosteriorIntegrityError(
            "reported_x_initial_weak_snapshot_binding_mismatch"
        )
    public = materialize_reported_x_initial_public_fact_row(
        activation=authorized,
        checkpoint_row=row,
        resolution_bridge=bridge,
        fact_snapshot_id=_text(checkpoint.get("initial_evidence_snapshot_id")),
        slug=slug,
        event_date=event_date,
        fact_as_of=fact_as_of,
        correlation_id=correlation_id,
    )
    conn.execute("BEGIN IMMEDIATE")
    try:
        targets = conn.execute(
            "SELECT posterior_checkpoint_id,replay_generation FROM posterior_checkpoints "
            "WHERE target_identity=? ORDER BY replay_generation DESC,rowid DESC",
            (target,),
        ).fetchall()
        if targets and all(_text(item[0]) != checkpoint_id for item in targets):
            current = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM {LINEAGE_TABLE} WHERE target_identity=? "
                    "AND activation_contract_sha256=?",
                    (target, _text(authorized.get("activation_contract_sha256"))),
                ).fetchone()[0]
            )
            expected_generation = max(int(item[1] or 0) for item in targets) + 1
            if current:
                raise ProductionInitialPosteriorConflict(
                    "reported_x_initial_activation_already_initialized"
                )
            if (
                int(row.get("replay_generation") or 0) != expected_generation
                or _text(row.get("supersedes_checkpoint_id")) != _text(targets[0][0])
            ):
                raise ProductionInitialPosteriorConflict(
                    "reported_x_initial_successor_generation_invalid"
                )
        existing_checkpoint = conn.execute(
            "SELECT posterior_run_id,joint_posterior_sha256,reported_x_projection_sha256 "
            "FROM posterior_checkpoints WHERE posterior_checkpoint_id=?",
            (checkpoint_id,),
        ).fetchone()
        existing_lineage = conn.execute(
            f"SELECT lineage_json,activation_contract_sha256 FROM {LINEAGE_TABLE} "
            "WHERE posterior_checkpoint_id=?",
            (checkpoint_id,),
        ).fetchone()
        existing_fact = conn.execute(
            "SELECT id,fact_json,posterior_run_id FROM event_fact_history "
            "WHERE fact_snapshot_id=? AND fact_snapshot_id<>''",
            (_text(public.get("fact_snapshot_id")),),
        ).fetchone()
        existing_weak = conn.execute(
            "SELECT snapshot_sha256 FROM weak_forecast_evidence_snapshots "
            "WHERE forecast_evidence_snapshot_id=?",
            (_text(weak_snapshot.get("forecast_evidence_snapshot_id")),),
        ).fetchone()
        present = (
            existing_checkpoint is not None,
            existing_lineage is not None,
            existing_fact is not None,
        )
        if any(present) and not all(present):
            raise ProductionInitialPosteriorIntegrityError(
                "reported_x_initial_partial_state_detected"
            )
        if all(present):
            if (
                _text(existing_checkpoint[0]) != _text(row.get("posterior_run_id"))
                or _text(existing_checkpoint[1]) != _text(row.get("joint_posterior_sha256"))
                or _text(existing_checkpoint[2])
                != _text(row.get("reported_x_projection_sha256"))
                or _text(existing_lineage[0]) != _json(lineage)
                or _text(existing_lineage[1])
                != _text(authorized.get("activation_contract_sha256"))
                or _text(existing_fact[1]) != _text(public.get("fact_json"))
                or _text(existing_fact[2]) != _text(row.get("posterior_run_id"))
                or existing_weak is None
                or _text(existing_weak[0]) != _text(weak_snapshot.get("snapshot_sha256"))
            ):
                raise ProductionInitialPosteriorIntegrityError(
                    "reported_x_initial_existing_identity_drift"
                )
            fact_id = int(existing_fact[0])
            idempotent = True
        else:
            if existing_weak is not None and _text(existing_weak[0]) != _text(
                weak_snapshot.get("snapshot_sha256")
            ):
                raise ProductionInitialPosteriorIntegrityError(
                    "reported_x_initial_weak_snapshot_identity_drift"
                )
            conn.execute(
                "UPDATE weak_forecast_evidence_snapshots SET state='superseded' "
                "WHERE target_identity=? AND state='active'",
                (target,),
            )
            if existing_weak is None:
                _insert(conn, "weak_forecast_evidence_snapshots", weak_snapshot)
            else:
                conn.execute(
                    "UPDATE weak_forecast_evidence_snapshots SET state='active' "
                    "WHERE forecast_evidence_snapshot_id=?",
                    (_text(weak_snapshot.get("forecast_evidence_snapshot_id")),),
                )
            _insert(conn, "posterior_checkpoints", row)
            _insert(conn, LINEAGE_TABLE, lineage_row)
            _insert(conn, "event_fact_history", public)
            fact_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            idempotent = False
        conn.execute(
            "INSERT INTO event_fact_latest(slug,event_date,fact_id,updated_at,fact_snapshot_id,correlation_id) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(slug,event_date) DO UPDATE SET "
            "fact_id=excluded.fact_id,updated_at=excluded.updated_at," 
            "fact_snapshot_id=excluded.fact_snapshot_id,correlation_id=excluded.correlation_id",
            (
                _text(public.get("slug")),
                _text(public.get("event_date")),
                fact_id,
                _text(public.get("fact_as_of")),
                _text(public.get("fact_snapshot_id")),
                _text(public.get("correlation_id")),
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
        "idempotent": idempotent,
        "posterior_checkpoint_id": checkpoint_id,
        "posterior_run_id": _text(row.get("posterior_run_id")),
        "fact_snapshot_id": _text(public.get("fact_snapshot_id")),
        "fact_id": fact_id,
        "places_orders": False,
        "creates_outbox": False,
        "creates_realization": False,
        "creates_strong_update": False,
    }


__all__ = ["SCHEMA_VERSION", "commit_reported_x_initial_posterior"]
