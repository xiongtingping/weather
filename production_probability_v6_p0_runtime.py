"""Pure P0 compiler for the production-successor v6 probability epoch."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from typing import Any, Iterable, Mapping

from physical_state_bayes_v6.contracts import SCHEMA_VERSION as STATE_SCHEMA, DurationClocks, JointCell
from production_probability_v6_activation_contract import CHECKPOINT_SCHEMA, validate_v6_activation


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
    replay_generation: int,
    supersedes_checkpoint_id: str = "",
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
    dense_joint = _validate_joint(initial_joint)
    joint, pruned_mass, pruned_cell_count = _sparsify_joint(dense_joint)
    joint_payload = [
        {"continuous_y_c": row.y_c, "state": row.state, "probability": row.probability}
        for row in joint
    ]
    joint_sha = _sha(joint_payload)
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


__all__ = ["P0_MAX_PRUNED_MASS", "SCHEMA_VERSION", "compile_v6_p0_candidate"]
