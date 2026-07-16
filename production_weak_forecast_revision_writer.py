"""Atomic production writer for weak forecast revision → posterior SSOT."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping

from production_probability_activation_contract import validate_production_probability_activation
from weak_forecast_revision_schema_contract import audit_weak_forecast_revision_schema


SCHEMA_VERSION = "production_weak_forecast_revision_writer_v1"
BAYES_FORMULA_ID = "p0_prior_x_weak_once_then_canonical_event_raw_log_lr_then_weak_revision_replacement_log_lr_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _insert(conn: sqlite3.Connection, table: str, row: Mapping[str, Any]) -> None:
    fields = list(row)
    conn.execute(
        f"INSERT INTO {table}({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",
        tuple(row[field] for field in fields),
    )


def commit_production_weak_forecast_revision(
    conn: sqlite3.Connection,
    *,
    activation: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    """CAS the predecessor and atomically publish an orderless weak revision."""

    if conn.in_transaction:
        raise ValueError("weak_forecast_revision_writer_requires_no_ambient_transaction")
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='v2_shadow_runtime_manifest'").fetchone():
        raise ValueError("weak_forecast_revision_writer_refuses_shadow_database")
    authorized = validate_production_probability_activation(activation)
    if _text(authorized.get("bayes_formula_id")) != BAYES_FORMULA_ID:
        raise ValueError("weak_forecast_revision_bayes_formula_not_authorized")
    schema = audit_weak_forecast_revision_schema(conn)
    if schema.get("ok") is not True:
        raise ValueError("weak_forecast_revision_schema_unavailable")
    value = dict(candidate)
    if value.get("ok") is not True or _text(value.get("state")) != "candidate_ready" or value.get("places_orders") is not False:
        raise ValueError("weak_forecast_revision_candidate_not_ready")
    snapshot = dict(value["snapshot_row"])
    update = dict(value["update_row"])
    checkpoint = dict(value["checkpoint_row"])
    checkpoint["state"] = "committed"
    target = _text(update.get("target_identity"))
    previous_id = _text(update.get("previous_checkpoint_id"))
    checkpoint_id = _text(checkpoint.get("posterior_checkpoint_id"))
    update_id = _text(update.get("weak_forecast_revision_update_id"))
    active_generations = conn.execute(
        """SELECT p.replay_generation
           FROM production_initial_posterior_lineage l
           JOIN posterior_checkpoints p ON p.posterior_checkpoint_id=l.posterior_checkpoint_id
           WHERE l.target_identity=? AND l.activation_contract_sha256=?""",
        (target, _text(authorized.get("activation_contract_sha256"))),
    ).fetchall()
    if len(active_generations) != 1 or int(active_generations[0][0] or 0) != int(checkpoint.get("replay_generation") or 0):
        raise ValueError("weak_forecast_revision_active_replay_generation_mismatch")
    if _text(snapshot.get("weak_likelihood_artifact_sha256")) != _text(authorized.get("weak_likelihood_artifact_sha256")):
        raise ValueError("weak_forecast_revision_activation_weak_artifact_mismatch")
    if (
        _text(checkpoint.get("previous_checkpoint_id")) != previous_id
        or _text(update.get("successor_checkpoint_id")) != checkpoint_id
        or _text(checkpoint.get("forecast_evidence_snapshot_id")) != _text(snapshot.get("forecast_evidence_snapshot_id"))
        or _text(update.get("revised_forecast_evidence_snapshot_id")) != _text(snapshot.get("forecast_evidence_snapshot_id"))
    ):
        raise ValueError("weak_forecast_revision_candidate_binding_mismatch")
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            "SELECT COUNT(*) FROM posterior_checkpoints WHERE posterior_checkpoint_id=?",
            (checkpoint_id,),
        ).fetchone()[0]
        existing_update = conn.execute(
            "SELECT COUNT(*) FROM weak_forecast_revision_updates WHERE weak_forecast_revision_update_id=?",
            (update_id,),
        ).fetchone()[0]
        if bool(existing) != bool(existing_update):
            raise RuntimeError("weak_forecast_revision_partial_state_detected")
        if existing:
            conn.commit()
            return {"schema": SCHEMA_VERSION, "ok": True, "state": "already_committed", "idempotent": True, "places_orders": False}
        predecessor = conn.execute(
            """SELECT posterior_run_id,joint_posterior_sha256,forecast_evidence_snapshot_id
               FROM posterior_checkpoints WHERE posterior_checkpoint_id=? AND target_identity=?""",
            (previous_id, target),
        ).fetchone()
        latest = conn.execute(
            """SELECT posterior_checkpoint_id FROM posterior_checkpoints
               WHERE target_identity=? AND replay_generation=? AND state='committed'
               ORDER BY rowid DESC LIMIT 1""",
            (target, int(checkpoint.get("replay_generation") or 0)),
        ).fetchone()
        if (
            predecessor is None
            or latest is None
            or _text(latest[0]) != previous_id
            or _text(predecessor[0]) != _text(update.get("previous_posterior_run_id"))
            or _text(predecessor[1]) != _text(update.get("previous_posterior_sha256"))
            or _text(predecessor[2]) != _text(update.get("previous_forecast_evidence_snapshot_id"))
        ):
            raise RuntimeError("weak_forecast_revision_predecessor_cas_mismatch")
        old_snapshot = conn.execute(
            "SELECT state FROM weak_forecast_evidence_snapshots WHERE forecast_evidence_snapshot_id=?",
            (_text(update.get("previous_forecast_evidence_snapshot_id")),),
        ).fetchone()
        if old_snapshot is None or _text(old_snapshot[0]) != "active":
            raise RuntimeError("weak_forecast_revision_previous_snapshot_not_active")
        prior_public = conn.execute(
            """SELECT h.id,h.fact_json,h.slug,h.event_date
               FROM event_fact_latest l JOIN event_fact_history h ON h.id=l.fact_id
               WHERE l.slug=? AND l.event_date=? AND h.posterior_run_id=?""",
            (_text(value.get("public_slug")), _text(value.get("event_date")), _text(update.get("previous_posterior_run_id"))),
        ).fetchone()
        if prior_public is None:
            raise RuntimeError("weak_forecast_revision_previous_public_ssot_not_latest")
        _insert(conn, "weak_forecast_evidence_snapshots", snapshot)
        _insert(conn, "weak_forecast_revision_updates", update)
        _insert(conn, "posterior_checkpoints", checkpoint)
        payload = json.loads(_text(prior_public[1]))
        ssot = payload.get("posterior_ssot_fact")
        if not isinstance(ssot, dict):
            raise RuntimeError("weak_forecast_revision_previous_public_ssot_invalid")
        ssot.update({
            "fact_snapshot_id": _text(value.get("fact_snapshot_id")),
            "posterior_run_id": _text(checkpoint.get("posterior_run_id")),
            "posterior_checkpoint_id": checkpoint_id,
            "joint_posterior_sha256": _text(checkpoint.get("joint_posterior_sha256")),
            "physical_high_distribution": json.loads(_text(checkpoint.get("physical_high_distribution_json"))),
            "committed_at": _text(checkpoint.get("committed_at")),
            "forecast_evidence_snapshot_id": _text(snapshot.get("forecast_evidence_snapshot_id")),
            "last_weak_forecast_revision_update_id": update_id,
        })
        identity = ssot.get("probability_runtime_identity")
        if not isinstance(identity, dict):
            raise RuntimeError("weak_forecast_revision_runtime_identity_missing")
        identity["bayes_formula_id"] = BAYES_FORMULA_ID
        public_row = {
            "run_date": _text(value.get("event_date")),
            "slug": _text(value.get("public_slug")),
            "event_date": _text(value.get("event_date")),
            "fact_as_of": _text(value.get("decision_as_of")),
            "composer_version": "sequential_bayes_v2_production",
            "fact_json": _json(payload),
            "created_at": _text(value.get("decision_as_of")),
            "correlation_id": update_id,
            "fact_snapshot_id": _text(value.get("fact_snapshot_id")),
            "posterior_run_id": _text(checkpoint.get("posterior_run_id")),
        }
        _insert(conn, "event_fact_history", public_row)
        fact_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            """UPDATE event_fact_latest SET fact_id=?,updated_at=?,fact_snapshot_id=?,correlation_id=?
               WHERE slug=? AND event_date=?""",
            (fact_id, public_row["fact_as_of"], public_row["fact_snapshot_id"], update_id, public_row["slug"], public_row["event_date"]),
        )
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
        "idempotent": False,
        "posterior_checkpoint_id": checkpoint_id,
        "weak_forecast_revision_update_id": update_id,
        "fact_snapshot_id": _text(value.get("fact_snapshot_id")),
        "places_orders": False,
    }


__all__ = ["BAYES_FORMULA_ID", "SCHEMA_VERSION", "commit_production_weak_forecast_revision"]
