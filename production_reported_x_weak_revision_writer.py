"""Atomic weak-forecast revision writer for the reported-X successor epoch."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping

from production_probability_successor_activation_contract import (
    validate_successor_activation,
)
from production_reported_x_public_ssot_writer import _assert_boundary
from production_sequential_bayes_public_ssot_writer import (
    ProductionTransactionIntegrityError,
    _insert,
    _insert_public_and_latest,
    _latest_checkpoint,
)
from reported_x_successor_checkpoint_runtime import (
    build_successor_public_ssot_surfaces,
    validate_successor_checkpoint_row,
)
from weak_forecast_revision_schema_contract import audit_weak_forecast_revision_schema


SCHEMA_VERSION = "production_reported_x_weak_revision_writer_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def materialize_reported_x_weak_revision_public_fact_row(
    *,
    activation: Mapping[str, Any],
    previous_fact_json: Any,
    checkpoint_row: Mapping[str, Any],
    update_row: Mapping[str, Any],
    fact_snapshot_id: str,
    slug: str,
    event_date: str,
    fact_as_of: str,
) -> dict[str, Any]:
    """Replace only the forecast-dependent posterior surface in the public SSOT."""

    authorized = validate_successor_activation(activation)
    checkpoint = _mapping(checkpoint_row)
    surfaces = build_successor_public_ssot_surfaces(checkpoint)
    if surfaces.get("ok") is not True:
        raise ProductionTransactionIntegrityError("reported_x_weak_revision_checkpoint_surface_invalid")
    try:
        payload = json.loads(_text(previous_fact_json))
    except (TypeError, ValueError) as exc:
        raise ProductionTransactionIntegrityError("reported_x_weak_revision_previous_public_invalid") from exc
    ssot = payload.get("posterior_ssot_fact") if isinstance(payload, Mapping) else None
    if not isinstance(ssot, dict) or _text(ssot.get("schema")) != _text(authorized.get("public_ssot_schema")):
        raise ProductionTransactionIntegrityError("reported_x_weak_revision_previous_public_schema_invalid")
    ssot.update(
        {
            "fact_snapshot_id": _text(fact_snapshot_id),
            "posterior_run_id": _text(checkpoint.get("posterior_run_id")),
            "posterior_checkpoint_id": _text(checkpoint.get("posterior_checkpoint_id")),
            "joint_posterior_sha256": _text(checkpoint.get("joint_posterior_sha256")),
            "probability_target_type": surfaces["probability_target_type"],
            "reported_physical_x_distribution": surfaces["reported_physical_x_distribution"],
            "reported_x_lower_bound_c": surfaces["reported_x_lower_bound_c"],
            "reported_x_termination": surfaces["reported_x_termination"],
            "committed_at": _text(checkpoint.get("committed_at")),
            "forecast_evidence_snapshot_id": _text(checkpoint.get("forecast_evidence_snapshot_id")),
            "last_weak_forecast_revision_update_id": _text(
                _mapping(update_row).get("weak_forecast_revision_update_id")
            ),
            "latent_probability_public": False,
        }
    )
    ssot.pop("physical_high_distribution", None)
    identity = ssot.get("probability_runtime_identity")
    if not isinstance(identity, dict):
        raise ProductionTransactionIntegrityError("reported_x_weak_revision_runtime_identity_missing")
    identity.update(
        {
            "activation_contract_sha256": _text(authorized.get("activation_contract_sha256")),
            "runtime_manifest_sha256": _text(authorized.get("runtime_manifest_sha256")),
            "bayes_formula_id": _text(authorized.get("bayes_formula_id")),
            "composer_schema": _text(authorized.get("composer_schema")),
            "checkpoint_schema": _text(authorized.get("checkpoint_schema")),
            "projection_schema": _text(authorized.get("projection_schema")),
            "latent_type": _text(authorized.get("latent_type")),
            "target_type": _text(authorized.get("target_type")),
        }
    )
    return {
        "run_date": _text(event_date),
        "slug": _text(slug),
        "event_date": _text(event_date),
        "fact_as_of": _text(fact_as_of),
        "composer_version": "sequential_bayes_v3_reported_x_production",
        "fact_json": _json(payload),
        "created_at": _text(fact_as_of),
        "correlation_id": _text(_mapping(update_row).get("weak_forecast_revision_update_id")),
        "fact_snapshot_id": _text(fact_snapshot_id),
        "posterior_run_id": _text(checkpoint.get("posterior_run_id")),
    }


def commit_reported_x_weak_forecast_revision(
    conn: sqlite3.Connection,
    *,
    activation: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    """CAS predecessor and atomically persist snapshot, update, checkpoint and v3 SSOT."""

    authorized = _assert_boundary(conn, activation)
    if audit_weak_forecast_revision_schema(conn).get("ok") is not True:
        raise ValueError("reported_x_weak_revision_schema_unavailable")
    value = dict(_mapping(candidate))
    if value.get("ok") is not True or _text(value.get("state")) != "candidate_ready" or value.get("places_orders") is not False:
        raise ValueError("reported_x_weak_revision_candidate_not_ready")
    snapshot = dict(_mapping(value.get("snapshot_row")))
    update = dict(_mapping(value.get("update_row")))
    checkpoint = dict(_mapping(value.get("checkpoint_row")))
    validation = validate_successor_checkpoint_row(checkpoint)
    if validation.get("ok") is not True:
        raise ProductionTransactionIntegrityError(
            "reported_x_weak_revision_checkpoint_invalid:" + ",".join(validation.get("issues") or [])
        )
    checkpoint["state"] = "committed"
    target = _text(update.get("target_identity"))
    previous_id = _text(update.get("previous_checkpoint_id"))
    checkpoint_id = _text(checkpoint.get("posterior_checkpoint_id"))
    update_id = _text(update.get("weak_forecast_revision_update_id"))
    if (
        _text(checkpoint.get("previous_checkpoint_id")) != previous_id
        or _text(update.get("successor_checkpoint_id")) != checkpoint_id
        or _text(checkpoint.get("forecast_evidence_snapshot_id")) != _text(snapshot.get("forecast_evidence_snapshot_id"))
        or _text(snapshot.get("weak_likelihood_artifact_sha256")) != _text(authorized.get("weak_likelihood_artifact_sha256"))
    ):
        raise ProductionTransactionIntegrityError("reported_x_weak_revision_candidate_binding_mismatch")
    generations = conn.execute(
        """SELECT p.replay_generation FROM production_initial_posterior_lineage l
           JOIN posterior_checkpoints p ON p.posterior_checkpoint_id=l.posterior_checkpoint_id
           WHERE l.target_identity=? AND l.activation_contract_sha256=?""",
        (target, _text(authorized.get("activation_contract_sha256"))),
    ).fetchall()
    if len(generations) != 1 or int(generations[0][0] or 0) != int(checkpoint.get("replay_generation") or 0):
        raise ProductionTransactionIntegrityError("reported_x_weak_revision_generation_mismatch")
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing_checkpoint = conn.execute(
            "SELECT COUNT(*) FROM posterior_checkpoints WHERE posterior_checkpoint_id=?", (checkpoint_id,)
        ).fetchone()[0]
        existing_update = conn.execute(
            "SELECT COUNT(*) FROM weak_forecast_revision_updates WHERE weak_forecast_revision_update_id=?", (update_id,)
        ).fetchone()[0]
        if bool(existing_checkpoint) != bool(existing_update):
            raise ProductionTransactionIntegrityError("reported_x_weak_revision_partial_state_detected")
        predecessor = conn.execute(
            """SELECT posterior_run_id,joint_posterior_sha256,forecast_evidence_snapshot_id
               FROM posterior_checkpoints WHERE posterior_checkpoint_id=? AND target_identity=?""",
            (previous_id, target),
        ).fetchone()
        latest = _latest_checkpoint(conn, target, _text(authorized.get("activation_contract_sha256")))
        if (
            predecessor is None
            or latest is None
            or _text(latest["posterior_checkpoint_id"]) != previous_id
            or _text(predecessor[0]) != _text(update.get("previous_posterior_run_id"))
            or _text(predecessor[1]) != _text(update.get("previous_posterior_sha256"))
            or _text(predecessor[2]) != _text(update.get("previous_forecast_evidence_snapshot_id"))
        ):
            if existing_checkpoint and _text(latest["posterior_checkpoint_id"] if latest else "") == checkpoint_id:
                conn.commit()
                return {"schema": SCHEMA_VERSION, "ok": True, "state": "already_committed", "idempotent": True, "places_orders": False}
            raise ProductionTransactionIntegrityError("reported_x_weak_revision_predecessor_cas_mismatch")
        old_snapshot = conn.execute(
            "SELECT state FROM weak_forecast_evidence_snapshots WHERE forecast_evidence_snapshot_id=?",
            (_text(update.get("previous_forecast_evidence_snapshot_id")),),
        ).fetchone()
        if old_snapshot is None or _text(old_snapshot[0]) != "active":
            raise ProductionTransactionIntegrityError("reported_x_weak_revision_previous_snapshot_not_active")
        prior_public = conn.execute(
            """SELECT h.fact_json,h.slug,h.event_date FROM event_fact_latest l
               JOIN event_fact_history h ON h.id=l.fact_id
               WHERE l.slug=? AND l.event_date=? AND h.posterior_run_id=?""",
            (_text(value.get("public_slug")), _text(value.get("event_date")), _text(update.get("previous_posterior_run_id"))),
        ).fetchone()
        if prior_public is None:
            raise ProductionTransactionIntegrityError("reported_x_weak_revision_previous_public_not_latest")
        public = materialize_reported_x_weak_revision_public_fact_row(
            activation=authorized,
            previous_fact_json=prior_public[0],
            checkpoint_row=checkpoint,
            update_row=update,
            fact_snapshot_id=_text(value.get("fact_snapshot_id")),
            slug=_text(prior_public[1]),
            event_date=_text(prior_public[2]),
            fact_as_of=_text(value.get("decision_as_of")),
        )
        if not existing_checkpoint:
            _insert(conn, "weak_forecast_evidence_snapshots", snapshot)
            _insert(conn, "weak_forecast_revision_updates", update)
            _insert(conn, "posterior_checkpoints", checkpoint)
        _, public_idempotent, latest_repointed = _insert_public_and_latest(conn, public, update_latest=True)
        conn.execute(
            "UPDATE weak_forecast_evidence_snapshots SET state='superseded' WHERE forecast_evidence_snapshot_id=?",
            (_text(update.get("previous_forecast_evidence_snapshot_id")),),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "committed",
        "idempotent": bool(existing_checkpoint and public_idempotent),
        "posterior_checkpoint_id": checkpoint_id,
        "weak_forecast_revision_update_id": update_id,
        "latest_repointed": latest_repointed,
        "places_orders": False,
    }


__all__ = [
    "SCHEMA_VERSION",
    "commit_reported_x_weak_forecast_revision",
    "materialize_reported_x_weak_revision_public_fact_row",
]
