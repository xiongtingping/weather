"""Atomic P0/lineage/public writer for the unique production v6 epoch."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping

from production_probability_v6_public_ssot_writer import (
    V6TransactionIntegrityError,
    _assert_boundary,
    _insert,
    _text,
    _upsert_public,
    _validate_reported_distribution,
)


SCHEMA_VERSION = "production_probability_v6_initial_writer_v1"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def materialize_v6_initial_public_fact_row(
    *,
    activation: Mapping[str, Any],
    checkpoint_row: Mapping[str, Any],
    fact_snapshot_id: str,
    slug: str,
    event_date: str,
    fact_as_of: str,
    correlation_id: str,
) -> dict[str, Any]:
    from production_probability_v6_activation_contract import validate_v6_activation

    authorized = validate_v6_activation(activation)
    checkpoint = dict(_mapping(checkpoint_row))
    reported = _validate_reported_distribution(checkpoint)
    ssot = {
        "schema": _text(authorized.get("public_ssot_schema")),
        "state": "physical_only_market_projection_na",
        "writer_mode": "production",
        "fact_snapshot_id": _text(fact_snapshot_id),
        "posterior_run_id": _text(checkpoint.get("posterior_run_id")),
        "posterior_checkpoint_id": _text(checkpoint.get("posterior_checkpoint_id")),
        "resolution_contract_id": _text(checkpoint.get("resolution_contract_id")),
        "joint_posterior_sha256": _text(checkpoint.get("joint_posterior_sha256")),
        "probability_target_type": _text(checkpoint.get("probability_target_type")),
        "reported_physical_x_distribution": reported,
        "reported_x_lower_bound_c": None,
        "latent_y_public": False,
        "settlement_scalar_distribution": [],
        "market_bucket_probabilities": {},
        "market_bucket_probabilities_na_reason": "resolution_bridge_not_closed",
        "last_canonical_event_id": "",
        "last_strong_update_id": "",
        "committed_at": _text(checkpoint.get("committed_at")),
        "termination_risk": {"state": "na", "updates_posterior": False},
        "duration_clocks": json.loads(_text(checkpoint.get("duration_clocks_json")) or "{}"),
        "forecast_evidence_snapshot_id": _text(checkpoint.get("forecast_evidence_snapshot_id")),
        "probability_runtime_identity": {
            "activation_contract_sha256": _text(authorized.get("activation_contract_sha256")),
            "runtime_manifest_sha256": _text(authorized.get("runtime_manifest_sha256")),
            "bayes_formula_id": _text(authorized.get("bayes_formula_id")),
            "composer_schema": _text(authorized.get("composer_schema")),
            "checkpoint_schema": _text(authorized.get("checkpoint_schema")),
            "transition_artifact_sha256": _text(authorized.get("transition_artifact_sha256")),
            "observation_artifact_sha256": _text(authorized.get("observation_artifact_sha256")),
            "prior_model_artifact_sha256": _text(authorized.get("prior_model_artifact_sha256")),
            "weak_likelihood_artifact_sha256": _text(authorized.get("weak_likelihood_artifact_sha256")),
            "v5_fallback_forbidden": True,
        },
    }
    return {
        "run_date": _text(event_date),
        "slug": _text(slug),
        "event_date": _text(event_date),
        "fact_as_of": _text(fact_as_of),
        "composer_version": "physical_state_bayes_v6_production",
        "fact_json": _json({"posterior_ssot_fact": ssot}),
        "created_at": _text(fact_as_of),
        "correlation_id": _text(correlation_id),
        "fact_snapshot_id": _text(fact_snapshot_id),
        "posterior_run_id": _text(checkpoint.get("posterior_run_id")),
    }


def commit_v6_initial_posterior(
    conn: sqlite3.Connection,
    *,
    activation: Mapping[str, Any],
    p0_candidate: Mapping[str, Any],
    weak_snapshot_row: Mapping[str, Any],
    public_fact_row: Mapping[str, Any],
) -> dict[str, Any]:
    authorized = _assert_boundary(conn, activation)
    value = dict(_mapping(p0_candidate))
    if value.get("ok") is not True or _text(value.get("state")) != "production_successor_p0_candidate_ready":
        raise V6TransactionIntegrityError("v6_initial_candidate_invalid")
    checkpoint = dict(_mapping(value.get("checkpoint_row")))
    lineage = dict(_mapping(value.get("initial_lineage_row")))
    snapshot = dict(_mapping(weak_snapshot_row))
    public = dict(_mapping(public_fact_row))
    target = _text(checkpoint.get("target_identity"))
    if (
        _text(lineage.get("posterior_checkpoint_id")) != _text(checkpoint.get("posterior_checkpoint_id"))
        or _text(lineage.get("activation_contract_sha256")) != _text(authorized.get("activation_contract_sha256"))
        or _text(snapshot.get("forecast_evidence_snapshot_id")) != _text(checkpoint.get("forecast_evidence_snapshot_id"))
        or _text(public.get("posterior_run_id")) != _text(checkpoint.get("posterior_run_id"))
    ):
        raise V6TransactionIntegrityError("v6_initial_bundle_binding_mismatch")
    _validate_reported_distribution(checkpoint)
    checkpoint["state"] = "committed"
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing_lineage = conn.execute(
            "SELECT posterior_checkpoint_id FROM production_initial_posterior_lineage "
            "WHERE target_identity=? AND activation_contract_sha256=?",
            (target, _text(authorized.get("activation_contract_sha256"))),
        ).fetchall()
        if len(existing_lineage) > 1:
            raise V6TransactionIntegrityError("v6_initial_lineage_not_unique")
        checkpoint_id = _text(checkpoint.get("posterior_checkpoint_id"))
        if existing_lineage and _text(existing_lineage[0][0]) != checkpoint_id:
            raise V6TransactionIntegrityError("v6_initial_lineage_identity_drift")
        existing_checkpoint = int(
            conn.execute(
                "SELECT COUNT(*) FROM posterior_checkpoints WHERE posterior_checkpoint_id=?", (checkpoint_id,)
            ).fetchone()[0]
        )
        if bool(existing_lineage) != bool(existing_checkpoint):
            raise V6TransactionIntegrityError("v6_initial_partial_state_detected")
        snapshot_id = _text(snapshot.get("forecast_evidence_snapshot_id"))
        existing_snapshot = conn.execute(
            "SELECT weak_likelihood_sha256,weak_likelihood_artifact_sha256 FROM weak_forecast_evidence_snapshots "
            "WHERE forecast_evidence_snapshot_id=?",
            (snapshot_id,),
        ).fetchone()
        if existing_snapshot is not None and (
            _text(existing_snapshot[0]) != _text(snapshot.get("weak_likelihood_sha256"))
            or _text(existing_snapshot[1]) != _text(snapshot.get("weak_likelihood_artifact_sha256"))
        ):
            raise V6TransactionIntegrityError("v6_initial_weak_snapshot_identity_drift")
        if not existing_checkpoint:
            if existing_snapshot is None:
                _insert(conn, "weak_forecast_evidence_snapshots", snapshot)
            else:
                conn.execute(
                    "UPDATE weak_forecast_evidence_snapshots SET "
                    "forecast_scenario_paths_json=COALESCE(forecast_scenario_paths_json,?),"
                    "forecast_scenario_paths_sha256=COALESCE(forecast_scenario_paths_sha256,?),"
                    "candidate_scenario_responsibilities_json=COALESCE(candidate_scenario_responsibilities_json,?),"
                    "candidate_scenario_responsibilities_sha256=COALESCE(candidate_scenario_responsibilities_sha256,?),"
                    "forecast_background_context_json=COALESCE(forecast_background_context_json,?) "
                    "WHERE forecast_evidence_snapshot_id=?",
                    (
                        snapshot.get("forecast_scenario_paths_json"),
                        snapshot.get("forecast_scenario_paths_sha256"),
                        snapshot.get("candidate_scenario_responsibilities_json"),
                        snapshot.get("candidate_scenario_responsibilities_sha256"),
                        snapshot.get("forecast_background_context_json"),
                        snapshot_id,
                    ),
                )
            _insert(conn, "posterior_checkpoints", checkpoint)
            _insert(conn, "production_initial_posterior_lineage", lineage)
        fact_id, public_idempotent = _upsert_public(conn, public)
        conn.execute(
            "INSERT INTO event_fact_latest(slug,event_date,fact_id,updated_at,fact_snapshot_id,correlation_id) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(slug,event_date) DO UPDATE SET "
            "fact_id=excluded.fact_id,updated_at=excluded.updated_at,fact_snapshot_id=excluded.fact_snapshot_id,correlation_id=excluded.correlation_id",
            (
                _text(public.get("slug")), _text(public.get("event_date")), fact_id,
                _text(public.get("fact_as_of")), _text(public.get("fact_snapshot_id")),
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
        "production": True,
        "idempotent": bool(existing_checkpoint and public_idempotent),
        "posterior_checkpoint_id": _text(checkpoint.get("posterior_checkpoint_id")),
        "creates_outbox": False,
        "places_orders": False,
    }


__all__ = [
    "SCHEMA_VERSION",
    "commit_v6_initial_posterior",
    "materialize_v6_initial_public_fact_row",
]
