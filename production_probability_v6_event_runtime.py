"""Unique pure event composer for the production-successor v6 epoch."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from physical_state_bayes_v6.artifact_contracts import (
    validate_observation_artifact,
    validate_transition_artifact,
)
from physical_state_bayes_v6.contracts import DurationClocks, JointCell
from physical_state_bayes_v6.core import (
    advance_duration_clocks_to_event,
)
from physical_state_bayes_v6.scenario_joint import (
    SCENARIO_JOINT_SCHEMA,
    predict_scenario_joint_to_event,
    scenario_joint_sha256,
)
from physical_state_bayes_v6.forcing_adapter import build_candidate_scenario_controls
from physical_state_bayes_v6.observation_adapter import build_canonical_emission_input
from physical_state_bayes_v6.observation_model import build_joint_observation_emission
from physical_state_bayes_v6.temporal_context import build_observation_temporal_context
from physical_state_bayes_v6.transition_model import build_candidate_forcing_slices
from production_probability_v6_activation_contract import (
    CHECKPOINT_SCHEMA,
    IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA,
    validate_v6_activation,
)
from production_probability_v6_candidate_compiler import (
    build_identifiable_physical_transaction_b_candidate,
    build_v6_transaction_b_candidate,
)
from production_probability_physical_state_composer import (
    compose_physical_canonical_event_extension,
    normalize_physical_predecessor_checkpoint,
)


SCHEMA_VERSION = "production_probability_v6_event_runtime_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def _parse(value: Any, default: Any) -> Any:
    if isinstance(value, (Mapping, list)):
        return value
    try:
        return json.loads(_text(value))
    except (TypeError, ValueError):
        return default


def _joint(checkpoint: Mapping[str, Any]) -> tuple[JointCell, ...]:
    rows = _parse(checkpoint.get("joint_posterior_json"), [])
    return tuple(
        JointCell(
            y_c=float(row["continuous_y_c"]),
            state=_text(row.get("state")),
            probability=float(row["probability"]),
        )
        for row in rows
        if isinstance(row, Mapping)
    )


def _clocks(checkpoint: Mapping[str, Any]) -> DurationClocks:
    value = _parse(checkpoint.get("duration_clocks_json"), {})
    return DurationClocks(
        cloud_suppression_minutes=float(value.get("cloud_suppression_minutes") or 0.0),
        precipitation_minutes=float(value.get("precipitation_minutes") or 0.0),
        cb_minutes=float(value.get("cb_minutes") or 0.0),
        tcu_minutes=float(value.get("tcu_minutes") or 0.0),
        cloud_suppression_active=bool(value.get("cloud_suppression_active")),
        precipitation_active=bool(value.get("precipitation_active")),
        cb_active=bool(value.get("cb_active")),
        tcu_active=bool(value.get("tcu_active")),
        canonical_event_count=int(value.get("canonical_event_count") or 0),
        physical_event_cluster_signature=_text(value.get("physical_event_cluster_signature")),
        physical_event_cluster_minutes=float(value.get("physical_event_cluster_minutes") or 0.0),
        physical_event_cluster_information_spent=float(
            value.get("physical_event_cluster_information_spent") or 0.0
        ),
    )


def compose_v6_canonical_event(
    *,
    activation: Mapping[str, Any],
    previous_checkpoint: Mapping[str, Any],
    canonical_event: Mapping[str, Any],
    pre_event_history: Sequence[Mapping[str, Any]],
    weak_likelihood: Mapping[str, Any],
    transition_artifact: Mapping[str, Any],
    observation_artifact: Mapping[str, Any],
    decision_as_of: str,
    completed_at: str,
    physical_state_model_artifact: Mapping[str, Any] | None = None,
    physical_observation_artifact: Mapping[str, Any] | None = None,
    physical_reporting_operator_artifact: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    authorized = validate_v6_activation(activation)
    previous = dict(previous_checkpoint)
    if _text(previous.get("checkpoint_probability_schema")) != CHECKPOINT_SCHEMA:
        raise ValueError("v6_event_previous_checkpoint_schema_invalid")
    if _text(authorized.get("composer_schema")) == IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA:
        previous = normalize_physical_predecessor_checkpoint(previous)
        event_time = _time(canonical_event.get("valid_time"))
        reference_time = _time(
            previous.get("physical_state_reference_valid_at")
            or previous.get("last_event_valid_at")
        )
        if event_time is None or reference_time is None or event_time < reference_time:
            raise ValueError("physical_event_time_or_predecessor_reference_invalid")
        consumed = _parse(previous.get("consumed_event_ids_json"), [])
        history_ids = [
            _text(row.get("canonical_observation_event_id"))
            for row in pre_event_history
        ]
        if list(consumed) != history_ids:
            raise ValueError(
                "physical_event_pre_event_history_checkpoint_sequence_mismatch"
            )
        current_event_id = _text(
            canonical_event.get("canonical_observation_event_id")
        )
        if not current_event_id or current_event_id in consumed:
            raise ValueError("physical_event_identity_or_duplicate_invalid")
        if _text(weak_likelihood.get("artifact_sha256")) != _text(
            authorized.get("weak_likelihood_artifact_sha256")
        ):
            raise ValueError("physical_event_weak_artifact_activation_mismatch")
        physical_inputs = (
            physical_state_model_artifact,
            physical_observation_artifact,
            physical_reporting_operator_artifact,
        )
        if not all(isinstance(item, Mapping) for item in physical_inputs):
            raise ValueError("physical_event_artifact_bundle_missing")
        bindings = (
            (
                physical_state_model_artifact.get("artifact_sha256"),
                authorized.get("physical_state_model_artifact_sha256"),
                "state_model",
            ),
            (
                physical_observation_artifact.get("artifact_sha256"),
                authorized.get("physical_observation_artifact_sha256"),
                "observation",
            ),
            (
                physical_reporting_operator_artifact.get("artifact_sha256"),
                authorized.get("physical_reporting_operator_sha256"),
                "reporting_operator",
            ),
        )
        for actual, expected, label in bindings:
            if _text(actual) != _text(expected):
                raise ValueError(f"physical_event_{label}_artifact_activation_mismatch")
        for artifact, label in zip(physical_inputs, ("state_model", "observation", "reporting_operator")):
            if (
                _text(artifact.get("state")) != "approved"
                or artifact.get("production_runtime_eligible") is not True
                or artifact.get("shadow_only") is not False
            ):
                raise ValueError(f"physical_event_{label}_artifact_not_production_approved")
        extension = compose_physical_canonical_event_extension(
            previous_checkpoint=previous,
            canonical_event=canonical_event,
            weak_likelihood=weak_likelihood,
            state_model_artifact=dict(physical_state_model_artifact or {}),
            observation_artifact=dict(physical_observation_artifact or {}),
            reporting_operator_artifact=dict(physical_reporting_operator_artifact or {}),
        )
        candidate = build_identifiable_physical_transaction_b_candidate(
            activation=authorized,
            previous_checkpoint=previous,
            canonical_event=canonical_event,
            physical_extension=extension,
            decision_as_of=decision_as_of,
            completed_at=completed_at,
        )
        return {
            **candidate,
            "schema": SCHEMA_VERSION,
            "state": "production_successor_event_candidate_ready",
            "physical_composer_schema": extension["physical_composer_schema"],
            "delta_minutes": float(
                json.loads(extension["physical_pre_event_state_json"])["delta_minutes"]
            ),
            "current_observation_consumed_once": True,
            "nwp_hourly_path_likelihood_reapplied": False,
            "places_orders": False,
        }
    if _text(previous.get("scenario_joint_schema")) != SCENARIO_JOINT_SCHEMA:
        raise ValueError("v6_event_scenario_joint_checkpoint_missing")
    if scenario_joint_sha256(previous.get("scenario_joint_posterior_json")) != _text(
        previous.get("scenario_joint_posterior_sha256")
    ):
        raise ValueError("v6_event_scenario_joint_checkpoint_hash_mismatch")
    if _text(previous.get("scenario_joint_forcing_generation_sha256")) != _text(
        previous.get("forcing_generation_sha256")
    ):
        raise ValueError("v6_event_scenario_joint_forcing_mismatch")
    transition = validate_transition_artifact(transition_artifact)
    observation = validate_observation_artifact(observation_artifact)
    for artifact, label in ((transition, "transition"), (observation, "observation")):
        if (
            artifact.get("production_runtime_eligible") is not True
            or _text(artifact.get("state")) != "approved"
            or artifact.get("shadow_only") is not False
        ):
            raise ValueError(f"v6_event_{label}_artifact_not_production_approved")
    if _text(transition.get("artifact_sha256")) != _text(authorized.get("transition_artifact_sha256")):
        raise ValueError("v6_event_transition_artifact_activation_mismatch")
    if _text(observation.get("artifact_sha256")) != _text(authorized.get("observation_artifact_sha256")):
        raise ValueError("v6_event_observation_artifact_activation_mismatch")
    if _text(weak_likelihood.get("artifact_sha256")) != _text(authorized.get("weak_likelihood_artifact_sha256")):
        raise ValueError("v6_event_weak_artifact_activation_mismatch")

    event_time = _time(canonical_event.get("valid_time"))
    reference_time = _time(previous.get("last_event_valid_at"))
    if event_time is None or reference_time is None or event_time < reference_time:
        raise ValueError("v6_event_time_or_predecessor_reference_invalid")
    delta_minutes = (event_time - reference_time).total_seconds() / 60.0
    consumed = _parse(previous.get("consumed_event_ids_json"), [])
    history_ids = [_text(row.get("canonical_observation_event_id")) for row in pre_event_history]
    if list(consumed) != history_ids:
        raise ValueError("v6_event_pre_event_history_checkpoint_sequence_mismatch")

    observation_input = build_canonical_emission_input(
        canonical_event=canonical_event,
        pre_event_history=pre_event_history,
    )
    scenario_bundle = build_candidate_scenario_controls(
        weak_likelihood=weak_likelihood,
        event_valid_at=observation_input.event_valid_at,
    )
    if scenario_bundle.scenario_paths_sha256 != _text(previous.get("forcing_generation_sha256")):
        raise ValueError("v6_event_forcing_generation_checkpoint_mismatch")
    previous_clocks = _clocks(previous)
    pre_event_clocks = advance_duration_clocks_to_event(previous_clocks, delta_minutes)
    joint = _joint(previous)
    y_grid = sorted({row.y_c for row in joint})
    if not set(y_grid).issubset(set(scenario_bundle.controls_by_y)):
        raise ValueError("v6_event_weak_responsibility_y_grid_mismatch")
    forcing = build_candidate_forcing_slices(
        y_grid_c=y_grid,
        reported_regular_max_c=(
            int(previous["reported_x_lower_bound_c"])
            if previous.get("reported_x_lower_bound_c") is not None
            else None
        ),
        pre_event_duration_clocks=pre_event_clocks,
        scenarios_by_y=scenario_bundle.controls_by_y,
        delta_minutes=delta_minutes,
        transition_artifact=transition,
    )
    prediction, _predicted_scenario_joint = predict_scenario_joint_to_event(
        previous_scenario_joint=previous.get("scenario_joint_posterior_json"),
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
        duration_clocks_before=previous_clocks,
        event_id=observation_input.event_id,
        event_valid_at=observation_input.event_valid_at,
        delta_minutes=delta_minutes,
        forcing_generation_sha256=scenario_bundle.scenario_paths_sha256,
        transition_artifact_sha256=_text(transition.get("artifact_sha256")),
        candidate_forcing=forcing,
    )
    temporal_policy = observation.get("time_conditioned_observation_policy")
    if not isinstance(temporal_policy, Mapping):
        raise ValueError("v6_event_time_conditioned_observation_policy_missing")
    temporal_context = build_observation_temporal_context(
        event_valid_at=observation_input.event_valid_at,
        controls_by_y=scenario_bundle.controls_by_y,
        timezone_name=_text(temporal_policy.get("timezone")),
        station_latitude_deg=float(temporal_policy.get("station_latitude_deg")),
        station_longitude_deg=float(temporal_policy.get("station_longitude_deg")),
        forecast_background_context=(
            weak_likelihood.get("forecast_background_context")
            if isinstance(weak_likelihood.get("forecast_background_context"), Mapping)
            else None
        ),
    )
    emission = build_joint_observation_emission(
        prediction=prediction,
        is_regular_metar=observation_input.is_regular_metar,
        reported_temperature_c=observation_input.reported_temperature_c,
        cloud_suppressed=observation_input.cloud_suppressed,
        precipitation_present=observation_input.precipitation_present,
        cb_present=observation_input.cb_present,
        features=observation_input.features,
        observation_artifact=observation,
        temporal_context=temporal_context,
    )
    candidate = build_v6_transaction_b_candidate(
        activation=authorized,
        previous_checkpoint=previous,
        canonical_event=canonical_event,
        candidate_forcing=forcing,
        emission=emission,
        delta_minutes=delta_minutes,
        decision_as_of=decision_as_of,
        completed_at=completed_at,
        scenario_control_lineage_sha256=scenario_bundle.lineage_sha256,
        canonical_emission_input_sha256=observation_input.input_sha256,
    )
    return {
        **candidate,
        "schema": SCHEMA_VERSION,
        "state": "production_successor_event_candidate_ready",
        "scenario_control_lineage_sha256": scenario_bundle.lineage_sha256,
        "canonical_emission_input_sha256": observation_input.input_sha256,
        "delta_minutes": delta_minutes,
        "current_observation_consumed_once": True,
        "nwp_hourly_path_likelihood_reapplied": False,
        "places_orders": False,
    }


__all__ = ["SCHEMA_VERSION", "compose_v6_canonical_event"]
