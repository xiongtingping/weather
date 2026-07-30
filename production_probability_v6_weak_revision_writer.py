"""Atomic production writer for v6 forecast-only replacement checkpoints."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping

from physical_state_bayes_v6.contracts import SCHEMA_VERSION as STATE_SCHEMA
from identifiable_physical_state_bayes.contracts import (
    SCHEMA_VERSION as IDENTIFIABLE_PHYSICAL_STATE_SCHEMA,
)
from identifiable_physical_state_bayes.serialization import posterior_sha256
from production_probability_v6_activation_contract import (
    IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA,
)
from production_probability_v6_activation_contract import CHECKPOINT_SCHEMA
from production_probability_v6_public_ssot_writer import (
    V6TransactionConflict,
    V6TransactionIntegrityError,
    _active_generation,
    _assert_boundary,
    _insert,
    _text,
    _upsert_public,
    _validate_reported_distribution,
)
from production_probability_v6_weak_revision_runtime import (
    IDENTIFIABLE_PHYSICAL_UPDATE_SCHEMA,
    SCHEMA_VERSION as CANDIDATE_SCHEMA,
    UPDATE_SCHEMA,
)


SCHEMA_VERSION = "production_probability_v6_weak_revision_writer_v1"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def materialize_v6_weak_revision_public_fact_row(
    *,
    activation: Mapping[str, Any],
    previous_fact_json: Any,
    checkpoint_row: Mapping[str, Any],
    update_row: Mapping[str, Any],
    fact_snapshot_id: str,
    slug: str,
    event_date: str,
    fact_as_of: str,
    market_inputs: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    authorized = _assert_activation_only(activation)
    identifiable_physical = (
        _text(authorized.get("composer_schema"))
        == IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA
    )
    checkpoint = dict(_mapping(checkpoint_row))
    update = dict(_mapping(update_row))
    try:
        payload = json.loads(_text(previous_fact_json))
    except (TypeError, ValueError) as exc:
        raise V6TransactionIntegrityError("v6_weak_revision_previous_public_invalid") from exc
    ssot = payload.get("posterior_ssot_fact") if isinstance(payload, Mapping) else None
    if not isinstance(ssot, dict):
        raise V6TransactionIntegrityError("v6_weak_revision_previous_ssot_missing")
    reported = _validate_reported_distribution(checkpoint)
    ssot.update(
        {
            "state": "physical_only_market_projection_na",
            "writer_mode": "production",
            "fact_snapshot_id": _text(fact_snapshot_id),
            "posterior_run_id": _text(checkpoint.get("posterior_run_id")),
            "posterior_checkpoint_id": _text(checkpoint.get("posterior_checkpoint_id")),
            "joint_posterior_sha256": _text(checkpoint.get("joint_posterior_sha256")),
            "reported_physical_x_distribution": reported,
            "reported_x_lower_bound_c": checkpoint.get("reported_x_lower_bound_c"),
            "forecast_evidence_snapshot_id": _text(checkpoint.get("forecast_evidence_snapshot_id")),
            "last_weak_forecast_revision_update_id": _text(update.get("weak_forecast_revision_update_id")),
            "committed_at": _text(checkpoint.get("committed_at")),
            "settlement_scalar_distribution": [],
            "market_bucket_probabilities": {},
            "market_bucket_probabilities_na_reason": "weak_revision_requires_resolution_projection_recomposition",
            "latent_y_public": False,
        }
    )
    if identifiable_physical:
        ssot.update(
            {
                "physical_state_posterior_sha256": _text(
                    checkpoint.get("physical_state_posterior_sha256")
                ),
                "physical_path_projection_sha256": _text(
                    checkpoint.get("physical_path_projection_sha256")
                ),
                "physical_reporting_operator_sha256": _text(
                    checkpoint.get("physical_reporting_operator_sha256")
                ),
                "physical_state_reference_valid_at": _text(
                    checkpoint.get("physical_state_reference_valid_at")
                ),
                "termination_risk": json.loads(
                    _text(checkpoint.get("reported_x_termination_json")) or "{}"
                ),
                "last_canonical_event_id": _text(
                    checkpoint.get("last_canonical_event_id")
                ),
                "last_strong_update_id": _text(
                    checkpoint.get("last_strong_update_id")
                ),
                "market_bucket_probabilities_na_reason": (
                    "weak_revision_market_projection_context_unavailable"
                ),
            }
        )
    identity = ssot.setdefault("probability_runtime_identity", {})
    if not isinstance(identity, dict):
        raise V6TransactionIntegrityError("v6_weak_revision_runtime_identity_invalid")
    identity.update(
        {
            "activation_contract_sha256": _text(authorized.get("activation_contract_sha256")),
            "runtime_manifest_sha256": _text(authorized.get("runtime_manifest_sha256")),
            "bayes_formula_id": _text(authorized.get("bayes_formula_id")),
            "composer_schema": _text(authorized.get("composer_schema")),
            "checkpoint_schema": _text(authorized.get("checkpoint_schema")),
            "transition_artifact_sha256": _text(authorized.get("transition_artifact_sha256")),
            "observation_artifact_sha256": _text(authorized.get("observation_artifact_sha256")),
            "weak_likelihood_artifact_sha256": _text(authorized.get("weak_likelihood_artifact_sha256")),
            "physical_state_model_artifact_sha256": _text(
                authorized.get("physical_state_model_artifact_sha256")
            ),
            "physical_observation_artifact_sha256": _text(
                authorized.get("physical_observation_artifact_sha256")
            ),
            "physical_reporting_operator_sha256": _text(
                authorized.get("physical_reporting_operator_sha256")
            ),
            "v5_fallback_forbidden": True,
        }
    )
    public = {
        "run_date": _text(event_date),
        "slug": _text(slug),
        "event_date": _text(event_date),
        "fact_as_of": _text(fact_as_of),
        "composer_version": (
            "identifiable_physical_state_bayes_production"
            if identifiable_physical
            else "physical_state_bayes_v6_production"
        ),
        "fact_json": _json(payload),
        "created_at": _text(fact_as_of),
        "correlation_id": _text(update.get("weak_forecast_revision_update_id")),
        "fact_snapshot_id": _text(fact_snapshot_id),
        "posterior_run_id": _text(checkpoint.get("posterior_run_id")),
    }
    inputs = _mapping(market_inputs)
    if identifiable_physical and inputs.get("market_projection_available") is True:
        try:
            from production_probability_v6_market_bundle_runtime import (
                project_v6_basket_probabilities_into_public_fact,
            )
            from resolution_bridge_activation_runtime import (
                validate_resolution_bridge_activation,
            )

            context_result = _mapping(inputs.get("market_context_result"))
            context = _mapping(context_result.get("market_context"))
            target = _mapping(context.get("market_target_contract"))
            artifact = _mapping(inputs.get("bridge_artifact"))
            bridge = validate_resolution_bridge_activation(
                _mapping(inputs.get("bridge_activation"))
            )
            if (
                context_result.get("ok") is not True
                or _text(context_result.get("state")) != "available"
                or _text(bridge.get("state")) != "production_enabled"
                or _text(bridge.get("artifact_sha256"))
                != _text(artifact.get("artifact_sha256"))
                or _text(context.get("bridge_artifact_sha256"))
                != _text(artifact.get("artifact_sha256"))
                or _text(context.get("target_contract_sha256"))
                != _text(target.get("target_contract_sha256"))
            ):
                raise ValueError("physical_weak_revision_market_binding_mismatch")
            public, _ = project_v6_basket_probabilities_into_public_fact(
                public_fact_row=public,
                checkpoint_row=checkpoint,
                physical_resolution_contract=_mapping(
                    inputs.get("physical_resolution_contract")
                ),
                market_target_contract=target,
                bridge_artifact=artifact,
                event_date=event_date,
            )
        except (KeyError, TypeError, ValueError) as exc:
            payload = json.loads(_text(public.get("fact_json")) or "{}")
            fallback = payload.get("posterior_ssot_fact")
            if isinstance(fallback, dict):
                fallback["market_bucket_probabilities"] = {}
                fallback["settlement_scalar_distribution"] = []
                fallback["market_bucket_probabilities_na_reason"] = (
                    f"weak_revision_market_projection_blocked:{type(exc).__name__}"
                )
                fallback["market_projection_block_reasons"] = [
                    _text(exc) or type(exc).__name__
                ]
                public["fact_json"] = _json(payload)
    return public


def _assert_activation_only(activation: Mapping[str, Any]) -> dict[str, Any]:
    from production_probability_v6_activation_contract import validate_v6_activation

    return validate_v6_activation(activation)


def _assert_db_bundle(
    conn: sqlite3.Connection,
    *,
    authorized: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    update: Mapping[str, Any],
    checkpoint: Mapping[str, Any],
) -> None:
    identifiable_physical = (
        _text(authorized.get("composer_schema"))
        == IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA
    )
    target = _text(checkpoint.get("target_identity"))
    predecessor_id = _text(update.get("previous_checkpoint_id"))
    predecessor = conn.execute(
        "SELECT posterior_run_id,joint_posterior_sha256,forecast_evidence_snapshot_id,replay_generation "
        "FROM posterior_checkpoints WHERE posterior_checkpoint_id=? AND target_identity=?",
        (predecessor_id, target),
    ).fetchone()
    if predecessor is None:
        raise V6TransactionIntegrityError("v6_weak_revision_predecessor_missing")
    generation = _active_generation(conn, target, _text(authorized.get("activation_contract_sha256")))
    latest = conn.execute(
        "SELECT posterior_checkpoint_id FROM posterior_checkpoints WHERE target_identity=? "
        "AND replay_generation=? ORDER BY rowid DESC LIMIT 1",
        (target, generation),
    ).fetchone()
    if latest is None or _text(latest[0]) != predecessor_id:
        raise V6TransactionConflict("v6_weak_revision_predecessor_is_not_latest")
    bindings = (
        (predecessor[0], update.get("previous_posterior_run_id")),
        (predecessor[1], update.get("previous_posterior_sha256")),
        (predecessor[2], update.get("previous_forecast_evidence_snapshot_id")),
        (predecessor[3], checkpoint.get("replay_generation")),
        (checkpoint.get("previous_checkpoint_id"), predecessor_id),
        (checkpoint.get("forecast_evidence_snapshot_id"), update.get("revised_forecast_evidence_snapshot_id")),
        (snapshot.get("forecast_evidence_snapshot_id"), update.get("revised_forecast_evidence_snapshot_id")),
        (snapshot.get("weak_likelihood_artifact_sha256"), authorized.get("weak_likelihood_artifact_sha256")),
        (update.get("revised_forcing_generation_sha256"), checkpoint.get("forcing_generation_sha256")),
    )
    if any(_text(left) != _text(right) for left, right in bindings):
        raise V6TransactionIntegrityError("v6_weak_revision_bundle_binding_mismatch")
    artifact_bindings = (
        (
            checkpoint.get("physical_state_model_artifact_sha256"),
            authorized.get("physical_state_model_artifact_sha256"),
        ),
        (
            checkpoint.get("physical_observation_artifact_sha256"),
            authorized.get("physical_observation_artifact_sha256"),
        ),
        (
            checkpoint.get("physical_reporting_operator_sha256"),
            authorized.get("physical_reporting_operator_sha256"),
        ),
    ) if identifiable_physical else (
        (
            checkpoint.get("transition_artifact_sha256"),
            authorized.get("transition_artifact_sha256"),
        ),
        (
            checkpoint.get("observation_artifact_sha256"),
            authorized.get("observation_artifact_sha256"),
        ),
    )
    if any(_text(left) != _text(right) for left, right in artifact_bindings):
        raise V6TransactionIntegrityError("v6_weak_revision_artifact_binding_mismatch")
    old_snapshot = conn.execute(
        "SELECT 1 FROM weak_forecast_evidence_snapshots WHERE forecast_evidence_snapshot_id=?",
        (_text(update.get("previous_forecast_evidence_snapshot_id")),),
    ).fetchone()
    # Snapshot rows are content-addressed and may be reused by a new replay
    # generation after an older generation superseded the same row.  The
    # active-generation/latest-checkpoint checks above are the authoritative
    # anti-fork guard; global snapshot state is not generation-scoped.
    if old_snapshot is None:
        raise V6TransactionIntegrityError("v6_weak_revision_previous_snapshot_missing")


def commit_v6_weak_revision_transaction(
    conn: sqlite3.Connection,
    *,
    activation: Mapping[str, Any],
    candidate: Mapping[str, Any],
    public_fact_row: Mapping[str, Any],
) -> dict[str, Any]:
    authorized = _assert_boundary(conn, activation)
    value = dict(_mapping(candidate))
    if (
        _text(value.get("schema")) != CANDIDATE_SCHEMA
        or value.get("ok") is not True
        or _text(value.get("state")) != "production_successor_weak_revision_candidate_ready"
        or value.get("places_orders") is not False
    ):
        raise V6TransactionIntegrityError("v6_weak_revision_candidate_invalid")
    snapshot = dict(_mapping(value.get("snapshot_row")))
    update = dict(_mapping(value.get("update_row")))
    checkpoint = dict(_mapping(value.get("checkpoint_row")))
    public = dict(_mapping(public_fact_row))
    identifiable_physical = (
        _text(authorized.get("composer_schema"))
        == IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA
    )
    expected_update_schema = (
        IDENTIFIABLE_PHYSICAL_UPDATE_SCHEMA
        if identifiable_physical
        else UPDATE_SCHEMA
    )
    expected_state_schema = (
        IDENTIFIABLE_PHYSICAL_STATE_SCHEMA
        if identifiable_physical
        else STATE_SCHEMA
    )
    if (
        _text(update.get("probability_update_schema")) != expected_update_schema
        or int(update.get("no_retroactive_state_transition") or 0) != 1
        or _text(checkpoint.get("checkpoint_probability_schema")) != CHECKPOINT_SCHEMA
        or _text(checkpoint.get("state_space_schema")) != expected_state_schema
    ):
        raise V6TransactionIntegrityError("v6_weak_revision_semantics_invalid")
    if identifiable_physical:
        quality = _mapping(json.loads(_text(update.get("quality_json")) or "{}"))
        state_payload = _mapping(
            json.loads(_text(checkpoint.get("physical_state_posterior_json")) or "{}")
        )
        if (
            quality.get("forecast_revision_used_as_likelihood") is not False
            or quality.get("current_observation_consumed") is not False
            or quality.get("state_advanced_with_superseded_driver_to_effective_time")
            is not True
            or _text(checkpoint.get("physical_state_posterior_sha256"))
            != posterior_sha256(state_payload)
            or _text(checkpoint.get("joint_posterior_sha256"))
            != _text(checkpoint.get("physical_state_posterior_sha256"))
        ):
            raise V6TransactionIntegrityError(
                "physical_weak_revision_state_or_semantics_invalid"
            )
    if _text(public.get("posterior_run_id")) != _text(checkpoint.get("posterior_run_id")):
        raise V6TransactionIntegrityError("v6_weak_revision_public_binding_mismatch")
    reported = _validate_reported_distribution(checkpoint)
    public_payload = _mapping(
        json.loads(_text(public.get("fact_json")) or "{}")
    )
    public_ssot = _mapping(public_payload.get("posterior_ssot_fact"))
    public_reported = {
        str(int(float(key))): float(value)
        for key, value in _mapping(
            public_ssot.get("reported_physical_x_distribution")
        ).items()
    }
    if (
        public_reported != reported
        or _text(public_ssot.get("posterior_checkpoint_id"))
        != _text(checkpoint.get("posterior_checkpoint_id"))
        or _text(public_ssot.get("last_weak_forecast_revision_update_id"))
        != _text(update.get("weak_forecast_revision_update_id"))
    ):
        raise V6TransactionIntegrityError("v6_weak_revision_public_ssot_mismatch")
    if identifiable_physical:
        probabilities = _mapping(public_ssot.get("market_bucket_probabilities"))
        if probabilities:
            try:
                normalized = (
                    abs(sum(float(value) for value in probabilities.values()) - 1.0)
                    <= 1e-8
                )
            except (TypeError, ValueError):
                normalized = False
            boundary = _mapping(public_ssot.get("market_projection_boundary"))
            if (
                not normalized
                or _text(public_ssot.get("strategy_projection_role"))
                != "basket_probability_only"
                or _text(public_ssot.get("realization_state_id"))
                or boundary.get("dynamic_three_leg_realization_consumed") is not False
                or boundary.get("creates_strategy_signal") is not False
            ):
                raise V6TransactionIntegrityError(
                    "physical_weak_revision_basket_projection_invalid"
                )
        elif not _text(public_ssot.get("market_bucket_probabilities_na_reason")):
            raise V6TransactionIntegrityError(
                "physical_weak_revision_market_projection_na_reason_missing"
            )
        if (
            _text(public_ssot.get("physical_state_posterior_sha256"))
            != _text(checkpoint.get("physical_state_posterior_sha256"))
        ):
            raise V6TransactionIntegrityError(
                "physical_weak_revision_public_state_hash_mismatch"
            )
    checkpoint["state"] = "committed"
    conn.execute("BEGIN IMMEDIATE")
    try:
        update_id = _text(update.get("weak_forecast_revision_update_id"))
        checkpoint_id = _text(checkpoint.get("posterior_checkpoint_id"))
        present = (
            int(conn.execute("SELECT COUNT(*) FROM weak_forecast_revision_updates WHERE weak_forecast_revision_update_id=?", (update_id,)).fetchone()[0]),
            int(conn.execute("SELECT COUNT(*) FROM posterior_checkpoints WHERE posterior_checkpoint_id=?", (checkpoint_id,)).fetchone()[0]),
        )
        if any(present) and not all(present):
            raise V6TransactionIntegrityError("v6_weak_revision_partial_state_detected")
        if all(present):
            latest = conn.execute(
                "SELECT posterior_checkpoint_id FROM posterior_checkpoints WHERE target_identity=? "
                "AND replay_generation=? ORDER BY rowid DESC LIMIT 1",
                (_text(checkpoint.get("target_identity")), int(checkpoint.get("replay_generation") or 0)),
            ).fetchone()
            if latest is None or _text(latest[0]) != checkpoint_id:
                raise V6TransactionConflict("v6_weak_revision_idempotent_checkpoint_not_latest")
        else:
            _assert_db_bundle(
                conn,
                authorized=authorized,
                snapshot=snapshot,
                update=update,
                checkpoint=checkpoint,
            )
        if not any(present):
            _insert(conn, "weak_forecast_evidence_snapshots", snapshot)
            _insert(conn, "weak_forecast_revision_updates", update)
            _insert(conn, "posterior_checkpoints", checkpoint)
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
        "production": True,
        "idempotent": bool(all(present) and public_idempotent),
        "posterior_checkpoint_id": _text(checkpoint.get("posterior_checkpoint_id")),
        "weak_forecast_revision_update_id": _text(update.get("weak_forecast_revision_update_id")),
        "creates_outbox": False,
        "places_orders": False,
    }


__all__ = [
    "SCHEMA_VERSION",
    "commit_v6_weak_revision_transaction",
    "materialize_v6_weak_revision_public_fact_row",
]
