"""Pure P0 compiler for the production-successor v6 probability epoch."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from typing import Any, Iterable, Mapping

from physical_state_bayes_v6.contracts import SCHEMA_VERSION as STATE_SCHEMA, DurationClocks, JointCell
from physical_state_bayes_v6.scenario_joint import (
    SCENARIO_JOINT_SCHEMA,
    initialize_scenario_joint,
    scenario_joint_sha256,
)
from production_probability_v6_activation_contract import CHECKPOINT_SCHEMA, validate_v6_activation
from production_probability_v6_activation_contract import IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA
from production_probability_physical_state_composer import compile_physical_p0_extension


SCHEMA_VERSION = "production_probability_v6_p0_candidate_v1"
P0_MAX_PRUNED_MASS = 1e-10


def _text(value: Any) -> str:
    return str(value or "").strip()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _validate_joint(rows: Iterable[JointCell]) -> tuple[JointCell, ...]:
    joint = tuple(rows)
    if not joint or abs(sum(row.probability for row in joint) - 1.0) > 1e-9:
        raise ValueError("v6_p0_joint_not_normalized")
    if len({(row.y_c, row.state) for row in joint}) != len(joint):
        raise ValueError("v6_p0_joint_cell_duplicate")
    return joint


def _sparsify_joint(rows: tuple[JointCell, ...]) -> tuple[tuple[JointCell, ...], float, int]:
    """Drop smallest cells under a frozen total-probability error budget."""

    ordered = sorted(enumerate(rows), key=lambda item: (item[1].probability, item[0]))
    removed: set[int] = set()
    pruned_mass = 0.0
    for index, row in ordered:
        if row.probability <= 0.0:
            removed.add(index)
            continue
        if pruned_mass + row.probability > P0_MAX_PRUNED_MASS:
            break
        removed.add(index)
        pruned_mass += row.probability
    kept = [row for index, row in enumerate(rows) if index not in removed]
    normalizer = sum(row.probability for row in kept)
    if not kept or normalizer <= 0.0 or pruned_mass > P0_MAX_PRUNED_MASS + 1e-18:
        raise ValueError("v6_p0_sparsification_invalid")
    return (
        tuple(JointCell(row.y_c, row.state, row.probability / normalizer) for row in kept),
        pruned_mass,
        len(removed),
    )


def compile_v6_p0_candidate(
    *,
    activation: Mapping[str, Any],
    target_identity: str,
    event_date: str,
    decision_as_of: str,
    state_reference_valid_at: str,
    prior_context_id: str,
    prior_model_artifact_sha256: str,
    forecast_evidence_snapshot_id: str,
    weak_likelihood_artifact_sha256: str,
    resolution_contract_id: str,
    forcing_generation_sha256: str,
    initial_joint: Iterable[JointCell],
    candidate_scenario_responsibilities: Mapping[str, Any],
    replay_generation: int,
    supersedes_checkpoint_id: str = "",
    prior_context: Mapping[str, Any] | None = None,
    weak_likelihood: Mapping[str, Any] | None = None,
    physical_state_model_artifact: Mapping[str, Any] | None = None,
    physical_reporting_operator_artifact: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    authorized = validate_v6_activation(activation)
    if _text(prior_model_artifact_sha256) != _text(authorized.get("prior_model_artifact_sha256")):
        raise ValueError("v6_p0_prior_artifact_activation_mismatch")
    if _text(weak_likelihood_artifact_sha256) != _text(authorized.get("weak_likelihood_artifact_sha256")):
        raise ValueError("v6_p0_weak_artifact_activation_mismatch")
    if int(replay_generation) < 0:
        raise ValueError("v6_p0_replay_generation_invalid")
    if int(replay_generation) > 0 and not _text(supersedes_checkpoint_id):
        raise ValueError("v6_p0_successor_missing_superseded_checkpoint")
    required = (
        target_identity,
        event_date,
        decision_as_of,
        state_reference_valid_at,
        prior_context_id,
        forecast_evidence_snapshot_id,
        resolution_contract_id,
        forcing_generation_sha256,
    )
    if any(not _text(value) for value in required):
        raise ValueError("v6_p0_identity_missing")
    if _text(authorized.get("composer_schema")) == IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA:
        legacy_rows = tuple(initial_joint)
        if legacy_rows:
            raise ValueError("physical_p0_legacy_result_space_joint_forbidden")
        if not all(
            isinstance(value, Mapping)
            for value in (
                prior_context,
                weak_likelihood,
                physical_state_model_artifact,
                physical_reporting_operator_artifact,
            )
        ):
            raise ValueError("physical_p0_input_contract_missing")
        return _compile_identifiable_physical_p0_candidate(
            authorized=authorized,
            target_identity=target_identity,
            event_date=event_date,
            decision_as_of=decision_as_of,
            state_reference_valid_at=state_reference_valid_at,
            prior_context_id=prior_context_id,
            prior_model_artifact_sha256=prior_model_artifact_sha256,
            forecast_evidence_snapshot_id=forecast_evidence_snapshot_id,
            weak_likelihood_artifact_sha256=weak_likelihood_artifact_sha256,
            resolution_contract_id=resolution_contract_id,
            forcing_generation_sha256=forcing_generation_sha256,
            replay_generation=replay_generation,
            supersedes_checkpoint_id=supersedes_checkpoint_id,
            prior_context=dict(prior_context or {}),
            weak_likelihood=dict(weak_likelihood or {}),
            state_model_artifact=dict(physical_state_model_artifact or {}),
            reporting_operator_artifact=dict(physical_reporting_operator_artifact or {}),
        )
    dense_joint = _validate_joint(initial_joint)
    joint, pruned_mass, pruned_cell_count = _sparsify_joint(dense_joint)
    joint_payload = [
        {"continuous_y_c": row.y_c, "state": row.state, "probability": row.probability}
        for row in joint
    ]
    joint_sha = _sha(joint_payload)
    scenario_joint = initialize_scenario_joint(joint, candidate_scenario_responsibilities)
    scenario_joint_payload = list(scenario_joint)
    scenario_joint_sha = scenario_joint_sha256(scenario_joint)
    y_marginal: dict[str, float] = {}
    x_marginal: dict[str, float] = {}
    state_marginal: dict[str, float] = {}
    for row in joint:
        y_key = f"{row.y_c:g}"
        x_key = str(int(row.y_c + 0.5))
        y_marginal[y_key] = y_marginal.get(y_key, 0.0) + row.probability
        x_marginal[x_key] = x_marginal.get(x_key, 0.0) + row.probability
        state_marginal[row.state] = state_marginal.get(row.state, 0.0) + row.probability
    lineage_core = {
        "target_identity": _text(target_identity),
        "event_date": _text(event_date),
        "prior_context_id": _text(prior_context_id),
        "prior_model_artifact_sha256": _text(prior_model_artifact_sha256),
        "forecast_evidence_snapshot_id": _text(forecast_evidence_snapshot_id),
        "weak_likelihood_artifact_sha256": _text(weak_likelihood_artifact_sha256),
        "resolution_contract_id": _text(resolution_contract_id),
        "forcing_generation_sha256": _text(forcing_generation_sha256),
        "scenario_joint_posterior_sha256": scenario_joint_sha,
        "state_reference_valid_at": _text(state_reference_valid_at),
        "transition_artifact_sha256": _text(authorized.get("transition_artifact_sha256")),
        "observation_artifact_sha256": _text(authorized.get("observation_artifact_sha256")),
        "activation_contract_sha256": _text(authorized.get("activation_contract_sha256")),
        "joint_posterior_sha256": joint_sha,
        "replay_generation": int(replay_generation),
        "p0_numeric_sparsification": {
            "max_pruned_mass": P0_MAX_PRUNED_MASS,
            "actual_pruned_mass": pruned_mass,
            "pruned_cell_count": pruned_cell_count,
            "dense_cell_count": len(dense_joint),
            "sparse_cell_count": len(joint),
        },
    }
    run_id = "posterior-v6-p0-" + _sha(lineage_core)
    checkpoint_id = "checkpoint-v6-p0-" + _sha(
        {"posterior_run_id": run_id, "supersedes_checkpoint_id": _text(supersedes_checkpoint_id)}
    )
    model_artifact_sha256 = _text(authorized.get("activation_contract_sha256"))
    clocks = asdict(DurationClocks())
    checkpoint = {
        "posterior_checkpoint_id": checkpoint_id,
        "target_identity": _text(target_identity),
        "posterior_run_id": run_id,
        "previous_checkpoint_id": "",
        "checkpoint_kind": "initial",
        "forecast_evidence_snapshot_id": _text(forecast_evidence_snapshot_id),
        "prior_context_id": _text(prior_context_id),
        "resolution_contract_id": _text(resolution_contract_id),
        "last_strong_update_id": "",
        "last_canonical_event_id": "",
        "canonical_sequence_sha256": _sha([]),
        "temperature_grid_id": _sha(sorted(y_marginal, key=float)),
        "state_grid_id": _sha(sorted(state_marginal)),
        "joint_posterior_json": _json(joint_payload),
        "joint_posterior_sha256": joint_sha,
        "physical_high_distribution_json": _json(y_marginal),
        "settlement_scalar_distribution_json": "[]",
        "market_bucket_distribution_json": "{}",
        "latent_post_state_json": _json(state_marginal),
        "trajectory_feature_state_json": _json({"duration_clocks": clocks}),
        "source_reliability_state_json": "{}",
        "observed_max_lower_bound_c": None,
        "consumed_event_ids_json": "[]",
        "consumed_ancestor_sha256": _sha([]),
        "model_artifact_sha256": model_artifact_sha256,
        "replay_generation": int(replay_generation),
        "supersedes_checkpoint_id": _text(supersedes_checkpoint_id),
        "state": "production_successor_ready",
        "created_at": _text(decision_as_of),
        "committed_at": _text(decision_as_of),
        "checkpoint_probability_schema": CHECKPOINT_SCHEMA,
        "probability_target_type": "amsc_scheduled_regular_metar_reported_daily_max_c",
        "latent_scheduled_slot_high_distribution_json": _json(y_marginal),
        "reported_physical_x_distribution_json": _json(x_marginal),
        "latent_observed_lower_bound_c": None,
        "reported_x_lower_bound_c": None,
        "reported_x_termination_json": _json(
            {"schema": "termination_risk_marginal_v3_physical_state_bayes_v6", "state": "na", "updates_posterior": False}
        ),
        "reported_x_projection_sha256": _sha(x_marginal),
        "state_space_schema": STATE_SCHEMA,
        "continuous_y_joint_sha256": joint_sha,
        "duration_clocks_json": _json(clocks),
        "last_event_valid_at": _text(state_reference_valid_at),
        "forcing_generation_sha256": _text(forcing_generation_sha256),
        "transition_artifact_sha256": _text(authorized.get("transition_artifact_sha256")),
        "observation_artifact_sha256": _text(authorized.get("observation_artifact_sha256")),
        "pre_event_prediction_sha256": "",
        "p0_pruned_probability_mass": pruned_mass,
        "p0_pruned_cell_count": pruned_cell_count,
        "scenario_joint_posterior_json": _json(scenario_joint_payload),
        "scenario_joint_posterior_sha256": scenario_joint_sha,
        "scenario_joint_schema": SCENARIO_JOINT_SCHEMA,
        "scenario_joint_forcing_generation_sha256": _text(forcing_generation_sha256),
    }
    lineage = {
        "posterior_checkpoint_id": checkpoint_id,
        "target_identity": _text(target_identity),
        "posterior_run_id": run_id,
        "prior_context_id": _text(prior_context_id),
        "prior_artifact_sha256": _text(prior_model_artifact_sha256),
        "prior_model_artifact_sha256": _text(prior_model_artifact_sha256),
        "weak_likelihood_artifact_sha256": _text(weak_likelihood_artifact_sha256),
        "weak_likelihood_sha256": _sha(joint_payload),
        "forecast_evidence_snapshot_id": _text(forecast_evidence_snapshot_id),
        "strong_lr_artifact_sha256": _text(authorized.get("observation_artifact_sha256")),
        "source_reliability_initial_state_sha256": _sha({}),
        "resolution_contract_id": _text(resolution_contract_id),
        "initial_evidence_sha256": _sha(lineage_core),
        "activation_contract_sha256": _text(authorized.get("activation_contract_sha256")),
        "lineage_json": _json(lineage_core),
        "created_at": _text(decision_as_of),
        "probability_epoch_schema": CHECKPOINT_SCHEMA,
        "transition_artifact_sha256": _text(authorized.get("transition_artifact_sha256")),
        "observation_artifact_sha256": _text(authorized.get("observation_artifact_sha256")),
        "forcing_generation_sha256": _text(forcing_generation_sha256),
    }
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "production_successor_p0_candidate_ready",
        "checkpoint_row": checkpoint,
        "initial_lineage_row": lineage,
        "reported_physical_x_distribution": x_marginal,
        "places_orders": False,
    }


def _compile_identifiable_physical_p0_candidate(
    *,
    authorized: Mapping[str, Any],
    target_identity: str,
    event_date: str,
    decision_as_of: str,
    state_reference_valid_at: str,
    prior_context_id: str,
    prior_model_artifact_sha256: str,
    forecast_evidence_snapshot_id: str,
    weak_likelihood_artifact_sha256: str,
    resolution_contract_id: str,
    forcing_generation_sha256: str,
    replay_generation: int,
    supersedes_checkpoint_id: str,
    prior_context: Mapping[str, Any],
    weak_likelihood: Mapping[str, Any],
    state_model_artifact: Mapping[str, Any],
    reporting_operator_artifact: Mapping[str, Any],
) -> dict[str, Any]:
    if _text(state_model_artifact.get("artifact_sha256")) != _text(
        authorized.get("physical_state_model_artifact_sha256")
    ):
        raise ValueError("physical_p0_state_model_artifact_activation_mismatch")
    if _text(reporting_operator_artifact.get("artifact_sha256")) != _text(
        authorized.get("physical_reporting_operator_sha256")
    ):
        raise ValueError("physical_p0_reporting_artifact_activation_mismatch")
    for artifact, label in (
        (state_model_artifact, "state_model"),
        (reporting_operator_artifact, "reporting_operator"),
    ):
        if (
            _text(artifact.get("state")) != "approved"
            or artifact.get("production_runtime_eligible") is not True
            or artifact.get("shadow_only") is not False
        ):
            raise ValueError(f"physical_p0_{label}_artifact_not_production_approved")
    extension = compile_physical_p0_extension(
        target_identity=_text(target_identity),
        state_reference_valid_at=_text(state_reference_valid_at),
        prior_context=prior_context,
        weak_likelihood=weak_likelihood,
        state_model_artifact=state_model_artifact,
        reporting_operator_artifact=reporting_operator_artifact,
    )
    if _text(extension.get("physical_driver_generation_sha256")) != _text(forcing_generation_sha256):
        raise ValueError("physical_p0_driver_generation_mismatch")
    reported = {
        str(int(key)): float(value)
        for key, value in dict(extension["reported_physical_x_distribution"]).items()
    }
    if not reported or abs(sum(reported.values()) - 1.0) > 1e-9:
        raise ValueError("physical_p0_reported_x_distribution_invalid")
    state_sha = _text(extension.get("physical_state_posterior_sha256"))
    lineage_core = {
        "target_identity": _text(target_identity),
        "event_date": _text(event_date),
        "prior_context_id": _text(prior_context_id),
        "prior_model_artifact_sha256": _text(prior_model_artifact_sha256),
        "forecast_evidence_snapshot_id": _text(forecast_evidence_snapshot_id),
        "weak_likelihood_artifact_sha256": _text(weak_likelihood_artifact_sha256),
        "resolution_contract_id": _text(resolution_contract_id),
        "forcing_generation_sha256": _text(forcing_generation_sha256),
        "state_reference_valid_at": _text(state_reference_valid_at),
        "physical_state_model_artifact_sha256": _text(
            authorized.get("physical_state_model_artifact_sha256")
        ),
        "physical_observation_artifact_sha256": _text(
            authorized.get("physical_observation_artifact_sha256")
        ),
        "physical_reporting_operator_sha256": _text(
            authorized.get("physical_reporting_operator_sha256")
        ),
        "activation_contract_sha256": _text(authorized.get("activation_contract_sha256")),
        "physical_state_posterior_sha256": state_sha,
        "physical_path_projection_sha256": _text(extension.get("physical_path_projection_sha256")),
        "replay_generation": int(replay_generation),
        "legacy_result_space_joint_consumed": False,
    }
    run_id = "posterior-physical-p0-" + _sha(lineage_core)
    checkpoint_id = "checkpoint-physical-p0-" + _sha(
        {"posterior_run_id": run_id, "supersedes_checkpoint_id": _text(supersedes_checkpoint_id)}
    )
    projection = json.loads(_text(extension.get("physical_path_projection_json")) or "{}")
    latent_peak = projection.get("latent_continuous_peak_distribution") or {}
    state_payload = _text(extension.get("physical_state_posterior_json"))
    checkpoint = {
        "posterior_checkpoint_id": checkpoint_id,
        "target_identity": _text(target_identity),
        "posterior_run_id": run_id,
        "previous_checkpoint_id": "",
        "checkpoint_kind": "initial",
        "forecast_evidence_snapshot_id": _text(forecast_evidence_snapshot_id),
        "prior_context_id": _text(prior_context_id),
        "resolution_contract_id": _text(resolution_contract_id),
        "last_strong_update_id": "",
        "last_canonical_event_id": "",
        "canonical_sequence_sha256": _sha([]),
        "temperature_grid_id": _sha(sorted(reported, key=float)),
        "state_grid_id": _sha("identifiable_physical_state_v1"),
        "joint_posterior_json": state_payload,
        "joint_posterior_sha256": state_sha,
        "physical_high_distribution_json": _json(latent_peak),
        "settlement_scalar_distribution_json": "[]",
        "market_bucket_distribution_json": "{}",
        "latent_post_state_json": state_payload,
        "trajectory_feature_state_json": "{}",
        "source_reliability_state_json": "{}",
        "observed_max_lower_bound_c": None,
        "consumed_event_ids_json": "[]",
        "consumed_ancestor_sha256": _sha([]),
        "model_artifact_sha256": _text(authorized.get("activation_contract_sha256")),
        "replay_generation": int(replay_generation),
        "supersedes_checkpoint_id": _text(supersedes_checkpoint_id),
        "state": "production_successor_ready",
        "created_at": _text(decision_as_of),
        "committed_at": _text(decision_as_of),
        "checkpoint_probability_schema": CHECKPOINT_SCHEMA,
        "probability_target_type": "amsc_scheduled_regular_metar_reported_daily_max_c",
        "latent_scheduled_slot_high_distribution_json": _json(latent_peak),
        "reported_physical_x_distribution_json": _json(reported),
        "latent_observed_lower_bound_c": None,
        "reported_x_lower_bound_c": None,
        "reported_x_termination_json": _json({"state": "na", "updates_posterior": False}),
        "reported_x_projection_sha256": _sha(reported),
        "state_space_schema": "identifiable_physical_state_bayes_v1",
        "continuous_y_joint_sha256": "",
        "duration_clocks_json": "{}",
        "last_event_valid_at": _text(state_reference_valid_at),
        "forcing_generation_sha256": _text(forcing_generation_sha256),
        "transition_artifact_sha256": "",
        "observation_artifact_sha256": "",
        "pre_event_prediction_sha256": "",
        "p0_pruned_probability_mass": 0.0,
        "p0_pruned_cell_count": 0,
        "scenario_joint_posterior_json": "{}",
        "scenario_joint_posterior_sha256": "",
        "scenario_joint_schema": "",
        "scenario_joint_forcing_generation_sha256": "",
        "physical_composer_schema": _text(extension.get("physical_composer_schema")),
        "physical_state_posterior_schema": _text(extension.get("physical_state_posterior_schema")),
        "physical_state_posterior_json": state_payload,
        "physical_state_posterior_sha256": state_sha,
        "physical_path_projection_json": _text(extension.get("physical_path_projection_json")),
        "physical_path_projection_sha256": _text(extension.get("physical_path_projection_sha256")),
        "physical_state_model_artifact_sha256": _text(
            extension.get("physical_state_model_artifact_sha256")
        ),
        "physical_observation_artifact_sha256": _text(
            authorized.get("physical_observation_artifact_sha256")
        ),
        "physical_reporting_operator_sha256": _text(
            extension.get("physical_reporting_operator_sha256")
        ),
        "physical_driver_generation_sha256": _text(
            extension.get("physical_driver_generation_sha256")
        ),
        "physical_state_reference_valid_at": _text(state_reference_valid_at),
        "physical_last_weak_advance_json": "{}",
        "physical_last_weak_advance_sha256": "",
        "physical_forcing_rebind_json": "{}",
        "physical_forcing_rebind_sha256": "",
    }
    lineage = {
        "posterior_checkpoint_id": checkpoint_id,
        "target_identity": _text(target_identity),
        "posterior_run_id": run_id,
        "prior_context_id": _text(prior_context_id),
        "prior_artifact_sha256": _text(prior_model_artifact_sha256),
        "prior_model_artifact_sha256": _text(prior_model_artifact_sha256),
        "weak_likelihood_artifact_sha256": _text(weak_likelihood_artifact_sha256),
        "weak_likelihood_sha256": _sha(weak_likelihood),
        "forecast_evidence_snapshot_id": _text(forecast_evidence_snapshot_id),
        "strong_lr_artifact_sha256": _text(
            authorized.get("physical_observation_artifact_sha256")
        ),
        "source_reliability_initial_state_sha256": _sha({}),
        "resolution_contract_id": _text(resolution_contract_id),
        "initial_evidence_sha256": _sha(lineage_core),
        "activation_contract_sha256": _text(authorized.get("activation_contract_sha256")),
        "lineage_json": _json(lineage_core),
        "created_at": _text(decision_as_of),
        "probability_epoch_schema": CHECKPOINT_SCHEMA,
        "transition_artifact_sha256": "",
        "observation_artifact_sha256": "",
        "forcing_generation_sha256": _text(forcing_generation_sha256),
        "physical_state_model_artifact_sha256": _text(
            authorized.get("physical_state_model_artifact_sha256")
        ),
        "physical_observation_artifact_sha256": _text(
            authorized.get("physical_observation_artifact_sha256")
        ),
        "physical_reporting_operator_sha256": _text(
            authorized.get("physical_reporting_operator_sha256")
        ),
    }
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "production_successor_p0_candidate_ready",
        "checkpoint_row": checkpoint,
        "initial_lineage_row": lineage,
        "reported_physical_x_distribution": reported,
        "places_orders": False,
    }


__all__ = ["P0_MAX_PRUNED_MASS", "SCHEMA_VERSION", "compile_v6_p0_candidate"]
