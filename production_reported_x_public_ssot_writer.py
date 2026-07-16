"""Atomic physical-only Transaction B writer for the reported-X successor."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping

from production_probability_successor_activation_contract import (
    PUBLIC_SSOT_SCHEMA,
    validate_successor_activation,
)
from production_probability_successor_runtime_manifest import (
    successor_runtime_manifest_sha256,
)
from production_sequential_bayes_public_ssot_writer import (
    ProductionTransactionIntegrityError,
    _assert_bundle,
    _insert,
    _insert_public_and_latest,
    _latest_checkpoint,
)
from reported_x_successor_checkpoint_runtime import (
    build_successor_public_ssot_surfaces,
    validate_successor_checkpoint_row,
)
from reported_x_successor_schema_contract import verify_schema


SCHEMA_VERSION = "production_reported_x_public_ssot_writer_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _parse_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    parsed = json.loads(_text(value) or "{}")
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _assert_boundary(
    conn: sqlite3.Connection,
    activation: Mapping[str, Any],
) -> dict[str, Any]:
    if conn.in_transaction:
        raise ValueError("reported_x_writer_requires_no_ambient_transaction")
    if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='v2_shadow_runtime_manifest'"
    ).fetchone():
        raise ValueError("reported_x_writer_refuses_shadow_database")
    authorized = validate_successor_activation(activation)
    current_manifest = successor_runtime_manifest_sha256()
    if _text(authorized.get("runtime_manifest_sha256")) != current_manifest:
        raise ValueError("reported_x_writer_runtime_manifest_drift")
    schema = verify_schema(conn)
    if schema.get("ok") is not True:
        raise ValueError("reported_x_writer_schema_unavailable")
    required = (
        "production_initial_posterior_lineage",
        "strong_observation_updates",
        "posterior_checkpoints",
        "event_fact_history",
        "event_fact_latest",
    )
    missing = [
        table
        for table in required
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is None
    ]
    if missing:
        raise ValueError("reported_x_writer_required_tables_missing:" + ",".join(missing))
    return authorized


def materialize_reported_x_public_physical_fact_row(
    *,
    activation: Mapping[str, Any],
    strong_update_row: Mapping[str, Any],
    checkpoint_row: Mapping[str, Any],
    fact_snapshot_id: str,
    slug: str,
    event_date: str,
    fact_as_of: str,
    correlation_id: str,
    market_bucket_probabilities_na_reason: str,
    resident_fast_lane_prewarm_dependency_identity: str = "",
) -> dict[str, Any]:
    authorized = validate_successor_activation(activation)
    checkpoint = _mapping(checkpoint_row)
    validated = validate_successor_checkpoint_row(checkpoint)
    surfaces = build_successor_public_ssot_surfaces(checkpoint)
    if validated.get("ok") is not True or surfaces.get("ok") is not True:
        raise ProductionTransactionIntegrityError(
            "reported_x_public_ssot_checkpoint_surface_invalid"
        )
    auxiliary = _parse_object(_mapping(strong_update_row).get("path_auxiliary_json"))
    termination = _mapping(auxiliary.get("termination_risk"))
    timing = _mapping(auxiliary.get("peak_timing_state"))
    likelihood_identity = _mapping(auxiliary.get("strong_likelihood_update_identity"))
    if (
        _text(likelihood_identity.get("schema"))
        != "strong_likelihood_cumulative_replacement_identity_v1"
        or _text(likelihood_identity.get("likelihood_update_kind"))
        != "cumulative_evidence_replacement_delta_log_lr_v1"
        or not _text(likelihood_identity.get("current_cumulative_raw_log_score_sha256"))
        or not _text(likelihood_identity.get("conditional_innovation_log_score_sha256"))
    ):
        raise ProductionTransactionIntegrityError(
            "reported_x_public_ssot_strong_likelihood_identity_invalid"
        )
    if (
        _text(termination.get("schema")) != "termination_risk_marginal_v2_reported_x"
        or _text(termination.get("state")) != "available"
        or termination.get("updates_posterior") is not False
        or _text(termination.get("canonical_observation_event_id"))
        != _text(checkpoint.get("last_canonical_event_id"))
    ):
        raise ProductionTransactionIntegrityError(
            "reported_x_public_ssot_termination_contract_invalid"
        )
    if (
        _text(timing.get("schema")) != "peak_timing_state_l3_v1"
        or timing.get("ok") is not True
        or _text(timing.get("state")) not in {"available", "na"}
        or _text(timing.get("canonical_observation_event_id"))
        != _text(checkpoint.get("last_canonical_event_id"))
        or _text(timing.get("strong_observation_update_id"))
        != _text(checkpoint.get("last_strong_update_id"))
    ):
        raise ProductionTransactionIntegrityError(
            "reported_x_public_ssot_peak_timing_contract_invalid"
        )
    required_identity = (
        checkpoint.get("posterior_checkpoint_id"),
        checkpoint.get("posterior_run_id"),
        checkpoint.get("resolution_contract_id"),
        checkpoint.get("joint_posterior_sha256"),
        checkpoint.get("committed_at"),
        fact_snapshot_id,
        slug,
        event_date,
        fact_as_of,
        correlation_id,
        market_bucket_probabilities_na_reason,
    )
    if any(not _text(value) for value in required_identity):
        raise ValueError("reported_x_public_ssot_identity_missing")
    ssot: dict[str, Any] = {
        "schema": PUBLIC_SSOT_SCHEMA,
        "state": "physical_only_market_projection_na",
        "writer_mode": "production",
        "fact_snapshot_id": _text(fact_snapshot_id),
        "posterior_run_id": _text(checkpoint.get("posterior_run_id")),
        "posterior_checkpoint_id": _text(checkpoint.get("posterior_checkpoint_id")),
        "realization_state_id": "",
        "realization_state_na_reason": "market_bucket_projection_unavailable",
        "resolution_contract_id": _text(checkpoint.get("resolution_contract_id")),
        "joint_posterior_sha256": _text(checkpoint.get("joint_posterior_sha256")),
        "probability_target_type": surfaces["probability_target_type"],
        "reported_physical_x_distribution": surfaces[
            "reported_physical_x_distribution"
        ],
        "reported_x_lower_bound_c": surfaces["reported_x_lower_bound_c"],
        "settlement_scalar_distribution": [],
        "settlement_scalar_distribution_na_reason": _text(
            market_bucket_probabilities_na_reason
        ),
        "market_bucket_probabilities": {},
        "market_bucket_probabilities_na_reason": _text(
            market_bucket_probabilities_na_reason
        ),
        "last_canonical_event_id": _text(checkpoint.get("last_canonical_event_id")),
        "last_strong_update_id": _text(checkpoint.get("last_strong_update_id")),
        "committed_at": _text(checkpoint.get("committed_at")),
        "remaining_warming_marginal": list(
            auxiliary.get("remaining_warming_marginal") or []
        ),
        "termination_risk": dict(termination),
        "strong_likelihood_update_identity": dict(likelihood_identity),
        "peak_timing_state": dict(timing),
        "latent_probability_public": False,
        "probability_runtime_identity": {
            "activation_contract_sha256": _text(
                authorized.get("activation_contract_sha256")
            ),
            "runtime_manifest_sha256": _text(authorized.get("runtime_manifest_sha256")),
            "bayes_formula_id": _text(authorized.get("bayes_formula_id")),
            "composer_schema": _text(authorized.get("composer_schema")),
            "checkpoint_schema": _text(authorized.get("checkpoint_schema")),
            "strong_update_schema": _text(authorized.get("strong_update_schema")),
            "projection_schema": _text(authorized.get("projection_schema")),
            "latent_type": _text(authorized.get("latent_type")),
            "target_type": _text(authorized.get("target_type")),
            "prior_model_artifact_sha256": _text(
                authorized.get("prior_model_artifact_sha256")
            ),
            "weak_likelihood_artifact_sha256": _text(
                authorized.get("weak_likelihood_artifact_sha256")
            ),
            "strong_lr_artifact_sha256": _text(
                authorized.get("strong_lr_artifact_sha256")
            ),
        },
    }
    if _text(resident_fast_lane_prewarm_dependency_identity):
        ssot["runtime_dependency_lineage"] = {
            "resident_fast_lane_prewarm_dependency_identity": _text(
                resident_fast_lane_prewarm_dependency_identity
            )
        }
    return {
        "run_date": _text(event_date),
        "slug": _text(slug),
        "event_date": _text(event_date),
        "fact_as_of": _text(fact_as_of),
        "composer_version": "sequential_bayes_v3_reported_x_production",
        "fact_json": _json({"posterior_ssot_fact": ssot}),
        "created_at": _text(fact_as_of),
        "correlation_id": _text(correlation_id),
        "fact_snapshot_id": _text(fact_snapshot_id),
        "posterior_run_id": _text(checkpoint.get("posterior_run_id")),
    }


def materialize_reported_x_initial_public_fact_row(
    *,
    activation: Mapping[str, Any],
    checkpoint_row: Mapping[str, Any],
    resolution_bridge: Mapping[str, Any],
    fact_snapshot_id: str,
    slug: str,
    event_date: str,
    fact_as_of: str,
    correlation_id: str,
) -> dict[str, Any]:
    authorized = validate_successor_activation(activation)
    checkpoint = _mapping(checkpoint_row)
    surfaces = build_successor_public_ssot_surfaces(checkpoint)
    if surfaces.get("ok") is not True:
        raise ProductionTransactionIntegrityError(
            "reported_x_initial_public_ssot_checkpoint_surface_invalid"
        )
    bridge = _mapping(resolution_bridge)
    if (
        bridge.get("ok") is not True
        or bridge.get("bridge_closed") is not False
        or _text(bridge.get("input_probability_field"))
        != "reported_physical_x_distribution"
    ):
        raise ProductionTransactionIntegrityError(
            "reported_x_initial_public_ssot_bridge_invalid"
        )
    if any(
        not _text(value)
        for value in (
            checkpoint.get("posterior_checkpoint_id"),
            checkpoint.get("posterior_run_id"),
            checkpoint.get("resolution_contract_id"),
            checkpoint.get("joint_posterior_sha256"),
            checkpoint.get("committed_at"),
            fact_snapshot_id,
            slug,
            event_date,
            fact_as_of,
            correlation_id,
        )
    ):
        raise ValueError("reported_x_initial_public_ssot_identity_missing")
    na_reason = _text(bridge.get("market_bucket_probabilities_na_reason"))
    ssot = {
        "schema": PUBLIC_SSOT_SCHEMA,
        "state": _text(bridge.get("state")),
        "writer_mode": "production",
        "checkpoint_kind": "initial",
        "fact_snapshot_id": _text(fact_snapshot_id),
        "posterior_run_id": _text(checkpoint.get("posterior_run_id")),
        "posterior_checkpoint_id": _text(checkpoint.get("posterior_checkpoint_id")),
        "realization_state_id": "",
        "realization_state_na_reason": "initial_checkpoint_has_no_observation_realization",
        "resolution_contract_id": _text(checkpoint.get("resolution_contract_id")),
        "joint_posterior_sha256": _text(checkpoint.get("joint_posterior_sha256")),
        "probability_target_type": surfaces["probability_target_type"],
        "reported_physical_x_distribution": surfaces[
            "reported_physical_x_distribution"
        ],
        "reported_x_lower_bound_c": None,
        "reported_x_termination": surfaces["reported_x_termination"],
        "settlement_scalar_distribution": [],
        "market_bucket_probabilities": {},
        "market_bucket_probabilities_na_reason": na_reason,
        "last_canonical_event_id": "",
        "last_strong_update_id": "",
        "committed_at": _text(checkpoint.get("committed_at")),
        "latent_probability_public": False,
        "probability_runtime_identity": {
            "activation_contract_sha256": _text(
                authorized.get("activation_contract_sha256")
            ),
            "runtime_manifest_sha256": _text(authorized.get("runtime_manifest_sha256")),
            "bayes_formula_id": _text(authorized.get("bayes_formula_id")),
            "composer_schema": _text(authorized.get("composer_schema")),
            "checkpoint_schema": _text(authorized.get("checkpoint_schema")),
            "strong_update_schema": _text(authorized.get("strong_update_schema")),
            "projection_schema": _text(authorized.get("projection_schema")),
            "latent_type": _text(authorized.get("latent_type")),
            "target_type": _text(authorized.get("target_type")),
            "prior_model_artifact_sha256": _text(
                authorized.get("prior_model_artifact_sha256")
            ),
            "weak_likelihood_artifact_sha256": _text(
                authorized.get("weak_likelihood_artifact_sha256")
            ),
            "strong_lr_artifact_sha256": _text(
                authorized.get("strong_lr_artifact_sha256")
            ),
        },
    }
    return {
        "run_date": _text(event_date),
        "slug": _text(slug),
        "event_date": _text(event_date),
        "fact_as_of": _text(fact_as_of),
        "composer_version": "sequential_bayes_v3_reported_x_production",
        "fact_json": _json({"posterior_ssot_fact": ssot}),
        "created_at": _text(fact_as_of),
        "correlation_id": _text(correlation_id),
        "fact_snapshot_id": _text(fact_snapshot_id),
        "posterior_run_id": _text(checkpoint.get("posterior_run_id")),
    }


def commit_reported_x_public_physical_transaction(
    conn: sqlite3.Connection,
    *,
    activation: Mapping[str, Any],
    expected_previous_checkpoint_id: str,
    expected_previous_posterior_run_id: str,
    strong_update_row: Mapping[str, Any],
    checkpoint_row: Mapping[str, Any],
    public_fact_row: Mapping[str, Any],
) -> dict[str, Any]:
    authorized = _assert_boundary(conn, activation)
    checkpoint = dict(_mapping(checkpoint_row))
    public = _mapping(public_fact_row)
    if _text(checkpoint.get("state")) not in {"production_ready", "committed"}:
        raise ProductionTransactionIntegrityError(
            "reported_x_transaction_checkpoint_state_invalid"
        )
    if validate_successor_checkpoint_row(checkpoint).get("ok") is not True:
        raise ProductionTransactionIntegrityError(
            "reported_x_transaction_checkpoint_invalid"
        )
    checkpoint["state"] = "committed"
    if _text(public.get("posterior_run_id")) != _text(
        checkpoint.get("posterior_run_id")
    ):
        raise ProductionTransactionIntegrityError(
            "reported_x_transaction_public_fact_binding_mismatch"
        )
    conn.execute("BEGIN IMMEDIATE")
    try:
        ids = _assert_bundle(
            conn,
            expected_previous_checkpoint_id=expected_previous_checkpoint_id,
            expected_previous_posterior_run_id=expected_previous_posterior_run_id,
            validated_activation=authorized,
            strong_update_row=strong_update_row,
            checkpoint_row=checkpoint,
            realization_state_row=None,
            outbox_row=None,
            outbox_execution_contract_row=None,
        )
        existing = [
            int(
                conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {column}=?", (identity,)
                ).fetchone()[0]
            )
            for table, column, identity in (
                ("strong_observation_updates", "strong_observation_update_id", ids["update"]),
                ("posterior_checkpoints", "posterior_checkpoint_id", ids["checkpoint"]),
            )
        ]
        if any(existing) and not all(existing):
            raise ProductionTransactionIntegrityError(
                "reported_x_transaction_partial_state_detected"
            )
        if not any(existing):
            _insert(conn, "strong_observation_updates", strong_update_row)
            _insert(conn, "posterior_checkpoints", checkpoint)
        latest = _latest_checkpoint(
            conn,
            _text(checkpoint.get("target_identity")),
            _text(authorized.get("activation_contract_sha256")),
        )
        publish_latest = _text(
            latest["posterior_checkpoint_id"] if latest else ""
        ) == _text(checkpoint.get("posterior_checkpoint_id"))
        fact_id, public_idempotent, latest_repointed = _insert_public_and_latest(
            conn,
            public,
            update_latest=publish_latest,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "committed",
        "idempotent": bool(any(existing) and public_idempotent),
        "production": True,
        "physical_only": True,
        "fact_id": fact_id,
        "ids": ids,
        "latest_repointed": latest_repointed,
        "places_orders": False,
    }


__all__ = [
    "SCHEMA_VERSION",
    "commit_reported_x_public_physical_transaction",
    "materialize_reported_x_initial_public_fact_row",
    "materialize_reported_x_public_physical_fact_row",
]
