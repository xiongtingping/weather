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


def _parse(value: Any, default: Any) -> Any:
    if isinstance(value, (Mapping, list)):
        return value
    try:
        return json.loads(_text(value))
    except (TypeError, ValueError):
        return default


def materialize_v6_initial_public_fact_row(
    *,
    activation: Mapping[str, Any],
    checkpoint_row: Mapping[str, Any],
    fact_snapshot_id: str,
    slug: str,
    event_date: str,
    fact_as_of: str,
    correlation_id: str,
    market_inputs: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    from production_probability_v6_activation_contract import validate_v6_activation
    from production_probability_v6_activation_contract import (
        IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA,
    )

    authorized = validate_v6_activation(activation)
    identifiable_physical = (
        _text(authorized.get("composer_schema"))
        == IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA
    )
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
        # A market-B republish is a projection of the already committed
        # physical checkpoint.  It must carry the checkpoint's hard bound and
        # canonical/strong-update identity; otherwise the market successor
        # can overwrite event_fact_latest while silently erasing the physical
        # lineage that proves why buckets below the bound are impossible.
        "observed_max_lower_bound_c": checkpoint.get("observed_max_lower_bound_c"),
        "reported_x_lower_bound_c": checkpoint.get("reported_x_lower_bound_c"),
        "hard_mask_reason": (
            "observed_contract_reported_x_lower_bound+frozen_reporting_operator"
            if identifiable_physical and checkpoint.get("reported_x_lower_bound_c") is not None
            else (
                "observed_reported_x_lower_bound+final_locked_reported_x_compatibility"
                if checkpoint.get("reported_x_lower_bound_c") is not None
                else "none"
            )
        ),
        "latent_y_public": False,
        "settlement_scalar_distribution": [],
        "market_bucket_probabilities": {},
        "market_bucket_probabilities_na_reason": "resolution_bridge_not_closed",
        "last_canonical_event_id": _text(checkpoint.get("last_canonical_event_id")),
        "last_strong_update_id": _text(checkpoint.get("last_strong_update_id")),
        "committed_at": _text(checkpoint.get("committed_at")),
        "termination_risk": {"state": "na", "updates_posterior": False},
        "physical_state_posterior_sha256": _text(checkpoint.get("physical_state_posterior_sha256")),
        "physical_path_projection_sha256": _text(checkpoint.get("physical_path_projection_sha256")),
        "physical_reporting_operator_sha256": _text(checkpoint.get("physical_reporting_operator_sha256")),
        "physical_state_reference_valid_at": _text(checkpoint.get("physical_state_reference_valid_at")),
        "reported_x_termination": json.loads(
            _text(checkpoint.get("reported_x_termination_json")) or "{}"
        ),
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
        },
    }
    inputs = _mapping(market_inputs)
    if identifiable_physical and inputs.get("market_projection_available") is True:
        try:
            from posterior_market_projection_runtime import (
                build_posterior_market_projection,
            )
            from production_probability_v6_market_bundle_runtime import (
                _validate_physical_resolution_binding,
            )
            from resolution_bridge_activation_runtime import (
                validate_resolution_bridge_activation,
            )

            context_result = _mapping(inputs.get("market_context_result"))
            context = _mapping(context_result.get("market_context"))
            target = _mapping(context.get("market_target_contract"))
            bridge_artifact = _mapping(inputs.get("bridge_artifact"))
            physical_contract = _mapping(inputs.get("physical_resolution_contract"))
            bridge_activation = validate_resolution_bridge_activation(
                _mapping(inputs.get("bridge_activation"))
            )
            if (
                context_result.get("ok") is not True
                or _text(context_result.get("state")) != "available"
                or _text(bridge_activation.get("state")) != "production_enabled"
                or _text(bridge_activation.get("artifact_sha256"))
                != _text(bridge_artifact.get("artifact_sha256"))
                or _text(context.get("bridge_artifact_sha256"))
                != _text(bridge_artifact.get("artifact_sha256"))
                or _text(context.get("target_contract_sha256"))
                != _text(target.get("target_contract_sha256"))
            ):
                raise ValueError("v6_initial_market_content_binding_mismatch")
            _validate_physical_resolution_binding(
                physical_resolution_contract=physical_contract,
                market_target_contract=target,
                bridge_artifact=bridge_artifact,
                checkpoint=checkpoint,
                event_date=event_date,
            )
            projection = build_posterior_market_projection(
                reported_physical_x_distribution=reported,
                physical_resolution_contract=physical_contract,
                market_target_contract=target,
                bridge_artifact=bridge_artifact,
            )
            bridge = _mapping(projection.get("resolution_bridge"))
            probabilities = dict(
                _mapping(bridge.get("market_bucket_probabilities"))
            )
            if (
                projection.get("ok") is not True
                or bridge.get("bridge_closed") is not True
                or not probabilities
                or abs(sum(float(value) for value in probabilities.values()) - 1.0)
                > 1e-8
            ):
                raise ValueError("v6_initial_closed_projection_unavailable")
            ranked = sorted(
                (
                    (str(bucket), float(probability))
                    for bucket, probability in probabilities.items()
                ),
                key=lambda row: (-row[1], row[0]),
            )
            ssot.update(
                {
                    "state": "available",
                    "strategy_projection_role": "basket_probability_only",
                    "realization_state_id": "",
                    "realization_state_na_reason": (
                        "retired_dynamic_three_leg_not_consumed"
                    ),
                    "physical_resolution_contract_id": _text(
                        checkpoint.get("resolution_contract_id")
                    ),
                    "resolution_contract_id": _text(
                        bridge.get("resolution_contract_id")
                    ),
                    "target_contract_sha256": _text(
                        projection.get("target_contract_sha256")
                    ),
                    "bridge_artifact_sha256": _text(
                        projection.get("bridge_artifact_sha256")
                    ),
                    "settlement_scalar_distribution": list(
                        bridge.get("settlement_scalar_distribution") or []
                    ),
                    "market_bucket_probabilities": probabilities,
                    "market_top_bucket": ranked[0][0],
                    "market_top_probability": ranked[0][1],
                    "market_top_probability_pct": ranked[0][1] * 100.0,
                    "market_bucket_probabilities_na_reason": "",
                    "market_projection_boundary": {
                        "reported_x_unchanged": True,
                        "market_b_is_projection_not_second_weather_posterior": True,
                        "market_price_consumed": False,
                        "dynamic_three_leg_realization_consumed": False,
                        "creates_strategy_signal": False,
                    },
                }
            )
        except (KeyError, TypeError, ValueError) as exc:
            ssot["market_bucket_probabilities_na_reason"] = (
                f"p0_market_projection_branch_blocked:{type(exc).__name__}"
            )
            ssot["market_projection_block_reasons"] = [
                _text(exc) or type(exc).__name__
            ]
    elif inputs:
        ssot["market_bucket_probabilities_na_reason"] = _text(
            inputs.get("market_projection_na_reason")
        ) or "resolution_bridge_not_closed"
    return {
        "run_date": _text(event_date),
        "slug": _text(slug),
        "event_date": _text(event_date),
        "fact_as_of": _text(fact_as_of),
        "composer_version": (
            "identifiable_physical_state_bayes_production"
            if identifiable_physical
            else "physical_state_bayes_v6_production"
        ),
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
    reported = _validate_reported_distribution(checkpoint)
    from production_probability_v6_activation_contract import (
        IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA,
    )

    public_payload = _mapping(_parse(public.get("fact_json"), {}))
    public_ssot = _mapping(public_payload.get("posterior_ssot_fact"))
    if (
        _text(authorized.get("composer_schema"))
        == IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA
    ):
        probabilities = _mapping(public_ssot.get("market_bucket_probabilities"))
        normalized = False
        if probabilities:
            try:
                normalized = (
                    abs(sum(float(item) for item in probabilities.values()) - 1.0)
                    <= 1e-8
                )
            except (TypeError, ValueError):
                normalized = False
        if (
            dict(_mapping(public_ssot.get("reported_physical_x_distribution")))
            != reported
            or (
                _text(public_ssot.get("state")) == "available"
                and (
                    not normalized
                    or _text(public_ssot.get("strategy_projection_role"))
                    != "basket_probability_only"
                    or _text(public_ssot.get("realization_state_id"))
                    or len(_text(public_ssot.get("bridge_artifact_sha256"))) != 64
                )
            )
        ):
            raise V6TransactionIntegrityError(
                "physical_initial_public_ssot_binding_invalid"
            )
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
                    "forecast_background_context_json=COALESCE(forecast_background_context_json,?),"
                    "source_expert_trace_json=COALESCE(source_expert_trace_json,?),"
                    "forecast_multimodality_trace_json=COALESCE(forecast_multimodality_trace_json,?),"
                    "weak_model_diagnostics_json=COALESCE(weak_model_diagnostics_json,?),"
                    "prior_context_audit_json=COALESCE(prior_context_audit_json,?) "
                    "WHERE forecast_evidence_snapshot_id=?",
                    (
                        snapshot.get("forecast_scenario_paths_json"),
                        snapshot.get("forecast_scenario_paths_sha256"),
                        snapshot.get("candidate_scenario_responsibilities_json"),
                        snapshot.get("candidate_scenario_responsibilities_sha256"),
                        snapshot.get("forecast_background_context_json"),
                        snapshot.get("source_expert_trace_json"),
                        snapshot.get("forecast_multimodality_trace_json"),
                        snapshot.get("weak_model_diagnostics_json"),
                        snapshot.get("prior_context_audit_json"),
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
