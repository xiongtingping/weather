"""Pure production-successor v6 Transaction-B candidate compiler."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict
from typing import Any, Iterable, Mapping

from physical_state_bayes_v6.contracts import (
    SCHEMA_VERSION as STATE_SCHEMA,
    CandidateForcingSlice,
    CanonicalObservationEmission,
    DurationClocks,
    JointCell,
)
from physical_state_bayes_v6.core import (
    predict_to_event,
    pre_event_prediction_sha256,
    project_reported_x,
    update_with_observation,
)
from production_probability_v6_activation_contract import (
    CHECKPOINT_SCHEMA,
    STRONG_UPDATE_SCHEMA,
    validate_v6_activation,
)


SCHEMA_VERSION = "production_probability_v6_transaction_b_candidate_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _previous_joint(checkpoint: Mapping[str, Any]) -> tuple[JointCell, ...]:
    raw = checkpoint.get("joint_posterior")
    if not isinstance(raw, list):
        raw = json.loads(_text(checkpoint.get("joint_posterior_json")) or "[]")
    return tuple(
        JointCell(
            y_c=float(row.get("continuous_y_c", row.get("y_c"))),
            state=_text(row.get("state")),
            probability=float(row.get("probability")),
        )
        for row in raw
    )


def _duration_clocks(checkpoint: Mapping[str, Any]) -> DurationClocks:
    raw = checkpoint.get("duration_clocks")
    if not isinstance(raw, Mapping):
        raw = json.loads(_text(checkpoint.get("duration_clocks_json")) or "{}")
    return DurationClocks(
        cloud_suppression_minutes=float(raw.get("cloud_suppression_minutes") or 0.0),
        precipitation_minutes=float(raw.get("precipitation_minutes") or 0.0),
        cb_minutes=float(raw.get("cb_minutes") or 0.0),
        cloud_suppression_active=bool(raw.get("cloud_suppression_active")),
        precipitation_active=bool(raw.get("precipitation_active")),
        cb_active=bool(raw.get("cb_active")),
    )


def build_v6_transaction_b_candidate(
    *,
    activation: Mapping[str, Any],
    previous_checkpoint: Mapping[str, Any],
    canonical_event: Mapping[str, Any],
    candidate_forcing: Iterable[CandidateForcingSlice],
    emission: CanonicalObservationEmission,
    delta_minutes: float,
    decision_as_of: str,
    completed_at: str,
    scenario_control_lineage_sha256: str,
    canonical_emission_input_sha256: str,
) -> dict[str, Any]:
    authorized = validate_v6_activation(activation)
    previous = dict(previous_checkpoint)
    if _text(previous.get("checkpoint_probability_schema")) != CHECKPOINT_SCHEMA:
        raise ValueError("v6_previous_checkpoint_schema_invalid")
    event_id = _text(canonical_event.get("canonical_observation_event_id"))
    event_valid_at = _text(canonical_event.get("valid_time"))
    if event_id != emission.event_id or event_valid_at != emission.event_valid_at:
        raise ValueError("v6_emission_canonical_event_mismatch")
    if emission.observation_artifact_sha256 != _text(authorized.get("observation_artifact_sha256")):
        raise ValueError("v6_emission_artifact_activation_mismatch")
    if len(_text(scenario_control_lineage_sha256)) != 64:
        raise ValueError("v6_scenario_control_lineage_sha256_invalid")
    if len(_text(canonical_emission_input_sha256)) != 64:
        raise ValueError("v6_canonical_emission_input_sha256_invalid")

    prediction = predict_to_event(
        previous_joint=_previous_joint(previous),
        previous_checkpoint_sha256=_text(previous.get("joint_posterior_sha256")),
        previous_reported_regular_max_c=(
            int(previous["reported_x_lower_bound_c"])
            if previous.get("reported_x_lower_bound_c") is not None
            else None
        ),
        previous_y_lower_bound_c=(
            float(previous["latent_observed_lower_bound_c"])
            if previous.get("latent_observed_lower_bound_c") is not None
            else None
        ),
        duration_clocks_before=_duration_clocks(previous),
        event_id=event_id,
        event_valid_at=event_valid_at,
        delta_minutes=delta_minutes,
        forcing_generation_sha256=_text(previous.get("forcing_generation_sha256")),
        transition_artifact_sha256=_text(authorized.get("transition_artifact_sha256")),
        candidate_forcing=candidate_forcing,
    )
    if emission.pre_event_prediction_sha256 != pre_event_prediction_sha256(prediction):
        raise ValueError("v6_emission_not_bound_to_compiled_prediction")
    post = update_with_observation(prediction=prediction, emission=emission)
    reported_x = project_reported_x(post)
    predicted_by_cell = {(row.y_c, row.state): row.probability for row in prediction.joint}
    post_by_cell = {(row.y_c, row.state): row.probability for row in post.joint}
    posterior_delta = []
    for key, pre_probability in sorted(predicted_by_cell.items()):
        post_probability = post_by_cell.get(key, 0.0)
        posterior_delta.append(
            {
                "continuous_y_c": key[0],
                "state": key[1],
                "log_posterior_ratio": (
                    math.log(post_probability / pre_probability)
                    if post_probability > 0.0 and pre_probability > 0.0
                    else None
                ),
                "hard_condition_allowed": post_probability > 0.0,
            }
        )
    joint = [
        {"continuous_y_c": row.y_c, "state": row.state, "probability": row.probability}
        for row in post.joint
    ]
    joint_sha = _sha(joint)
    prediction_json = asdict(prediction)
    clocks_before = asdict(prediction.duration_clocks_before)
    clocks_after = asdict(post.duration_clocks_after)
    raw_scores = [
        {
            "continuous_y_c": y_c,
            "pre_state": state,
            "log_emission": score,
        }
        for (y_c, state), score in sorted(emission.log_emission_by_cell.items())
    ]
    emission_json = {
        "event_id": emission.event_id,
        "pre_event_prediction_sha256": emission.pre_event_prediction_sha256,
        "event_valid_at": emission.event_valid_at,
        "is_regular_metar": emission.is_regular_metar,
        "reported_temperature_c": emission.reported_temperature_c,
        "cloud_suppressed": emission.cloud_suppressed,
        "precipitation_present": emission.precipitation_present,
        "cb_present": emission.cb_present,
        "log_emission_by_cell": raw_scores,
        "observation_artifact_sha256": emission.observation_artifact_sha256,
    }
    previous_run_id = _text(previous.get("posterior_run_id"))
    previous_checkpoint_id = _text(previous.get("posterior_checkpoint_id"))
    update_identity = {
        "schema": STRONG_UPDATE_SCHEMA,
        "previous_posterior_run_id": previous_run_id,
        "previous_posterior_sha256": _text(previous.get("joint_posterior_sha256")),
        "canonical_observation_event_id": event_id,
        "pre_event_prediction_sha256": pre_event_prediction_sha256(prediction),
        "observation_artifact_sha256": emission.observation_artifact_sha256,
    }
    update_id = "strong-v6-" + _sha(update_identity)
    run_id = "posterior-v6-" + _sha({**update_identity, "joint_posterior_sha256": joint_sha})
    checkpoint_id = "checkpoint-v6-" + _sha(
        {"posterior_run_id": run_id, "previous_checkpoint_id": previous_checkpoint_id}
    )
    target_identity = _text(previous.get("target_identity"))
    model_artifact_sha256 = _text(authorized.get("activation_contract_sha256"))
    consumed = list(previous.get("consumed_event_ids") or [])
    if not consumed:
        consumed = json.loads(_text(previous.get("consumed_event_ids_json")) or "[]")
    if event_id in consumed:
        raise ValueError("v6_canonical_event_already_consumed")
    consumed.append(event_id)

    reported_observed = post.observed_reported_regular_max_c
    probability_no_new_reported_high = (
        sum(probability for x, probability in reported_x.items() if x <= reported_observed)
        if reported_observed is not None
        else None
    )
    auxiliary = {
        "schema": "physical_state_bayes_v6_path_auxiliary_v1",
        "termination_risk": {
            "schema": "termination_risk_marginal_v3_physical_state_bayes_v6",
            "state": "available" if probability_no_new_reported_high is not None else "na",
            "probability_no_material_new_daily_high": probability_no_new_reported_high,
            "updates_posterior": False,
            "canonical_observation_event_id": event_id,
        },
        "duration_clocks_after": clocks_after,
        "scenario_control_lineage_sha256": _text(scenario_control_lineage_sha256),
        "canonical_emission_input_sha256": _text(canonical_emission_input_sha256),
        "nwp_hourly_path_likelihood_reapplied": False,
        "current_observation_consumed_once": True,
        "no_post_emission_state_transition": True,
    }
    strong_update_row = {
        "strong_observation_update_id": update_id,
        "target_identity": target_identity,
        "previous_posterior_run_id": previous_run_id,
        "previous_posterior_sha256": _text(previous.get("joint_posterior_sha256")),
        "previous_checkpoint_id": previous_checkpoint_id,
        "canonical_observation_event_id": event_id,
        "forecast_evidence_snapshot_id": _text(previous.get("forecast_evidence_snapshot_id")),
        "prior_context_id": _text(previous.get("prior_context_id")),
        "model_artifact_sha256": model_artifact_sha256,
        "decision_as_of": _text(decision_as_of),
        "state": "applied",
        "raw_log_score_json": _json(raw_scores),
        "raw_log_score_sha256": _sha(raw_scores),
        "normalizer_offset": -post.normalizer_log,
        "posterior_delta_json": _json(posterior_delta),
        "posterior_delta_sha256": _sha(posterior_delta),
        "pre_state_transition_json": _json(prediction.candidate_transition_trace),
        "post_state_transition_json": "{}",
        "source_reliability_before_json": "{}",
        "source_reliability_after_json": "{}",
        "path_auxiliary_json": _json(auxiliary),
        "observed_max_lower_bound_c": post.y_lower_bound_c,
        "quality_json": _json({"canonical_event_state": canonical_event.get("state")}),
        "block_reasons_json": "[]",
        "started_at": _text(decision_as_of),
        "completed_at": _text(completed_at),
        "created_at": _text(completed_at),
        "probability_update_schema": STRONG_UPDATE_SCHEMA,
        "pre_event_prediction_json": _json(prediction_json),
        "pre_event_prediction_sha256": pre_event_prediction_sha256(prediction),
        "observation_emission_json": _json(emission_json),
        "observation_emission_sha256": _sha(emission_json),
        "duration_clocks_before_json": _json(clocks_before),
        "duration_clocks_after_json": _json(clocks_after),
        "forcing_generation_sha256": prediction.forcing_generation_sha256,
        "transition_artifact_sha256": prediction.transition_artifact_sha256,
        "observation_artifact_sha256": emission.observation_artifact_sha256,
        "scenario_control_lineage_sha256": _text(scenario_control_lineage_sha256),
        "canonical_emission_input_sha256": _text(canonical_emission_input_sha256),
        "no_post_emission_transition": 1,
    }
    y_marginal: dict[str, float] = {}
    latent_states: dict[str, float] = {}
    for row in post.joint:
        y_marginal[f"{row.y_c:g}"] = y_marginal.get(f"{row.y_c:g}", 0.0) + row.probability
        latent_states[row.state] = latent_states.get(row.state, 0.0) + row.probability
    checkpoint_row = {
        "posterior_checkpoint_id": checkpoint_id,
        "target_identity": target_identity,
        "posterior_run_id": run_id,
        "previous_checkpoint_id": previous_checkpoint_id,
        "checkpoint_kind": "incremental",
        "forecast_evidence_snapshot_id": _text(previous.get("forecast_evidence_snapshot_id")),
        "prior_context_id": _text(previous.get("prior_context_id")),
        "resolution_contract_id": _text(previous.get("resolution_contract_id")),
        "last_strong_update_id": update_id,
        "last_canonical_event_id": event_id,
        "canonical_sequence_sha256": _sha(consumed),
        "temperature_grid_id": _sha(sorted(y_marginal)),
        "state_grid_id": _sha(sorted(latent_states)),
        "joint_posterior_json": _json(joint),
        "joint_posterior_sha256": joint_sha,
        "physical_high_distribution_json": _json(y_marginal),
        "settlement_scalar_distribution_json": "[]",
        "market_bucket_distribution_json": "{}",
        "latent_post_state_json": _json(latent_states),
        "trajectory_feature_state_json": _json({"duration_clocks": clocks_after}),
        "source_reliability_state_json": "{}",
        "observed_max_lower_bound_c": post.y_lower_bound_c,
        "consumed_event_ids_json": _json(consumed),
        "consumed_ancestor_sha256": _sha(consumed),
        "model_artifact_sha256": model_artifact_sha256,
        "replay_generation": int(previous.get("replay_generation") or 0),
        "supersedes_checkpoint_id": "",
        "state": "production_successor_ready",
        "created_at": _text(completed_at),
        "committed_at": _text(completed_at),
        "checkpoint_probability_schema": CHECKPOINT_SCHEMA,
        "probability_target_type": "amsc_scheduled_regular_metar_reported_daily_max_c",
        "latent_scheduled_slot_high_distribution_json": _json(y_marginal),
        "reported_physical_x_distribution_json": _json({str(k): v for k, v in reported_x.items()}),
        "latent_observed_lower_bound_c": post.y_lower_bound_c,
        "reported_x_lower_bound_c": reported_observed,
        "reported_x_termination_json": _json(auxiliary["termination_risk"]),
        "reported_x_projection_sha256": _sha(reported_x),
        "state_space_schema": STATE_SCHEMA,
        "continuous_y_joint_sha256": joint_sha,
        "duration_clocks_json": _json(clocks_after),
        "last_event_valid_at": event_valid_at,
        "forcing_generation_sha256": prediction.forcing_generation_sha256,
        "transition_artifact_sha256": prediction.transition_artifact_sha256,
        "observation_artifact_sha256": emission.observation_artifact_sha256,
        "pre_event_prediction_sha256": pre_event_prediction_sha256(prediction),
        "scenario_control_lineage_sha256": _text(scenario_control_lineage_sha256),
        "canonical_emission_input_sha256": _text(canonical_emission_input_sha256),
    }
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "production_successor_candidate_ready",
        "candidate_mode": "production_successor",
        "shadow_only": False,
        "strong_update_row": strong_update_row,
        "checkpoint_row": checkpoint_row,
        "reported_physical_x_distribution": reported_x,
        "places_orders": False,
    }


__all__ = ["SCHEMA_VERSION", "build_v6_transaction_b_candidate"]
