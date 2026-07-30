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
    final_locked_reported_x_compatible,
    pre_event_prediction_sha256,
    project_reported_x,
    update_with_observation,
)
from physical_state_bayes_v6.scenario_joint import (
    SCENARIO_JOINT_SCHEMA,
    predict_scenario_joint_to_event,
    scenario_joint_sha256,
    update_scenario_joint_with_observation,
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


def _reported_integer_c(y_c: float) -> int:
    return int(math.floor(float(y_c) + 0.5))


def _event_local_scenario_responsibility_trace(
    *,
    previous_joint: Iterable[JointCell],
    candidate_forcing: Iterable[CandidateForcingSlice],
    emission: CanonicalObservationEmission,
    previous_reported_max_c: int | None,
    previous_y_lower_bound_c: float | None,
    expected_post_joint: Iterable[JointCell],
) -> dict[str, Any]:
    """Explain the exact scenario mixture used by this event update.

    This marginalizes the same per-scenario transition kernels and the same
    joint emission as the production update.  It is event-local: carrying the
    scenario posterior into the next event requires a future checkpoint
    contract and is therefore stated explicitly rather than silently implied.
    """

    prior = tuple(previous_joint)
    forcing = tuple(candidate_forcing)
    by_y = {float(item.y_c): item for item in forcing}
    event_reported = (
        int(emission.reported_temperature_c)
        if emission.is_regular_metar and emission.reported_temperature_c is not None
        else None
    )
    reported_max = max(
        (value for value in (previous_reported_max_c, event_reported) if value is not None),
        default=None,
    )
    event_y_bound = float(event_reported) - 0.5 if event_reported is not None else None
    y_lower_bound = max(
        (value for value in (previous_y_lower_bound_c, event_y_bound) if value is not None),
        default=None,
    )
    max_score = max(float(value) for value in emission.log_emission_by_cell.values())
    before: dict[str, float] = {}
    after_unnormalized: dict[str, float] = {}
    after_cell_unnormalized: dict[tuple[float, str], float] = {}
    before_by_y: dict[float, dict[str, float]] = {}
    after_by_y: dict[float, dict[str, float]] = {}
    for cell in prior:
        item = by_y[cell.y_c]
        for scenario in item.scenarios:
            scenario_id = str(scenario.scenario_id)
            prior_mass = cell.probability * float(scenario.responsibility)
            before[scenario_id] = before.get(scenario_id, 0.0) + prior_mass
            before_by_y.setdefault(cell.y_c, {})[scenario_id] = (
                before_by_y.setdefault(cell.y_c, {}).get(scenario_id, 0.0) + prior_mass
            )
            for target_state, transition_probability in scenario.matrix[cell.state].items():
                if transition_probability <= 0.0:
                    continue
                allowed = (
                    (y_lower_bound is None or cell.y_c >= y_lower_bound - 1e-12)
                    and final_locked_reported_x_compatible(
                        y_c=cell.y_c,
                        state=target_state,
                        observed_reported_regular_max_c=reported_max,
                    )
                )
                if not allowed:
                    continue
                score = float(emission.log_emission_by_cell[(cell.y_c, target_state)])
                mass = prior_mass * float(transition_probability) * math.exp(score - max_score)
                after_unnormalized[scenario_id] = after_unnormalized.get(scenario_id, 0.0) + mass
                after_by_y.setdefault(cell.y_c, {})[scenario_id] = (
                    after_by_y.setdefault(cell.y_c, {}).get(scenario_id, 0.0) + mass
                )
                key = (cell.y_c, target_state)
                after_cell_unnormalized[key] = after_cell_unnormalized.get(key, 0.0) + mass
    total = sum(after_unnormalized.values())
    if total <= 0.0:
        raise ValueError("v6_event_scenario_responsibility_eliminated_all_mass")
    after = {scenario_id: mass / total for scenario_id, mass in after_unnormalized.items()}
    expected = {(cell.y_c, cell.state): cell.probability for cell in expected_post_joint}
    actual = {key: mass / total for key, mass in after_cell_unnormalized.items()}
    max_error = max(abs(actual.get(key, 0.0) - value) for key, value in expected.items())
    rows = []
    for scenario_id in sorted(set(before) | set(after)):
        before_probability = before.get(scenario_id, 0.0)
        after_probability = after.get(scenario_id, 0.0)
        rows.append({
            "scenario_id": scenario_id,
            "probability_before": before_probability,
            "probability_after": after_probability,
            "delta_probability": after_probability - before_probability,
            "log_responsibility_ratio": (
                math.log(after_probability / before_probability)
                if before_probability > 0.0 and after_probability > 0.0 else None
            ),
        })
    return {
        "schema": "v6_event_local_scenario_responsibility_trace_v1",
        "rows": rows,
        "before_probability_total": sum(before.values()),
        "after_probability_total": sum(after.values()),
        "marginal_consistency_max_abs_error": max_error,
        "marginal_matches_production_posterior": max_error <= 1e-9,
        "physical_evidence_enters_through_scenario_transition_then_joint_emission": True,
        "hard_y_lower_bound_applied": y_lower_bound,
        "carried_forward_to_next_event": False,
        "carried_forward_na_reason": "checkpoint_scenario_joint_dimension_not_yet_activated",
        "frontend_recompute_allowed": False,
    }


def _logsumexp(values: Iterable[float]) -> float | None:
    rows = [float(value) for value in values]
    if not rows:
        return None
    maximum = max(rows)
    return maximum + math.log(sum(math.exp(value - maximum) for value in rows))


def _relative_component_log_evidence(
    *,
    cells: Iterable[JointCell],
    component_by_cell: Mapping[tuple[float, str], Mapping[str, float]],
    component: str,
    bucket: int | None,
) -> float | None:
    selected = [
        cell for cell in cells
        if cell.probability > 0.0 and (bucket is None or _reported_integer_c(cell.y_c) == bucket)
    ]
    denominator = sum(cell.probability for cell in selected)
    terms = []
    for cell in selected:
        value = component_by_cell.get((cell.y_c, cell.state), {}).get(component)
        if value is None:
            return None
        terms.append(math.log(cell.probability) + float(value))
    numerator_log = _logsumexp(terms)
    return numerator_log - math.log(denominator) if numerator_log is not None and denominator > 0.0 else None


def _build_bucket_observation_evidence_rows(
    *, prediction: Any, emission: CanonicalObservationEmission, post: Any
) -> list[dict[str, Any]]:
    """Persist a read-only per-X explanation from the same Bayes operation.

    Component scores are exactly additive at the latent (Y,S) cell.  Once S is
    marginalized, each displayed component is a relative log-evidence
    diagnostic and is deliberately not presented as an additive probability
    decomposition.  The combined before/after ratio remains exact.
    """

    pre_by_bucket: dict[int, float] = {}
    post_by_bucket: dict[int, float] = {}
    for cell in prediction.joint:
        bucket = _reported_integer_c(cell.y_c)
        pre_by_bucket[bucket] = pre_by_bucket.get(bucket, 0.0) + cell.probability
    for cell in post.joint:
        bucket = _reported_integer_c(cell.y_c)
        post_by_bucket[bucket] = post_by_bucket.get(bucket, 0.0) + cell.probability
    component_by_cell = emission.log_component_by_cell
    expected_cells = {(cell.y_c, cell.state) for cell in prediction.joint}
    if set(component_by_cell) != expected_cells:
        raise ValueError("v6_observation_component_trace_cell_grid_mismatch")
    component_names = (
        "temperature_momentum_log_score",
        "physical_fields_log_score",
        "temporal_adjustment_log_score",
    )
    for cell_key, components in component_by_cell.items():
        values = [components.get(name) for name in component_names]
        total = components.get("total_log_emission")
        if total is None or any(value is None for value in values):
            raise ValueError("v6_observation_component_trace_incomplete")
        numeric = [float(value) for value in values]
        if not all(math.isfinite(value) for value in (*numeric, float(total))):
            raise ValueError("v6_observation_component_trace_non_finite")
        if abs(sum(numeric) - float(total)) > 1e-9:
            raise ValueError("v6_observation_component_trace_not_additive")
        if abs(float(total) - float(emission.log_emission_by_cell[cell_key])) > 1e-9:
            raise ValueError("v6_observation_component_trace_total_mismatch")
    global_components = {
        name: _relative_component_log_evidence(
            cells=prediction.joint,
            component_by_cell=component_by_cell,
            component=name,
            bucket=None,
        )
        for name in component_names
    }
    previous_max = prediction.previous_reported_regular_max_c
    event_reported = (
        int(emission.reported_temperature_c)
        if emission.is_regular_metar and emission.reported_temperature_c is not None
        else None
    )
    reported_max = max(
        (value for value in (previous_max, event_reported) if value is not None), default=None
    )
    event_y_bound = float(event_reported) - 0.5 if event_reported is not None else None
    y_lower_bound = max(
        (value for value in (prediction.previous_y_lower_bound_c, event_y_bound) if value is not None),
        default=None,
    )
    rows: list[dict[str, Any]] = []
    for bucket in sorted(set(pre_by_bucket) | set(post_by_bucket)):
        before = pre_by_bucket.get(bucket, 0.0)
        after = post_by_bucket.get(bucket, 0.0)
        bucket_cells = [cell for cell in prediction.joint if _reported_integer_c(cell.y_c) == bucket]
        hard_allowed_mass = sum(
            cell.probability
            for cell in bucket_cells
            if (y_lower_bound is None or cell.y_c + 1e-9 >= y_lower_bound)
            and final_locked_reported_x_compatible(
                y_c=cell.y_c,
                state=cell.state,
                observed_reported_regular_max_c=reported_max,
            )
        )
        component_values: dict[str, float | None] = {}
        for name in component_names:
            local = _relative_component_log_evidence(
                cells=prediction.joint,
                component_by_cell=component_by_cell,
                component=name,
                bucket=bucket,
            )
            global_value = global_components[name]
            component_values[name.replace("_log_score", "_log_lr")] = (
                local - global_value if local is not None and global_value is not None else None
            )
        rows.append(
            {
                "reported_x_c": bucket,
                "probability_before": before,
                "hard_allowed_probability_before": hard_allowed_mass,
                "hard_allowed_fraction": hard_allowed_mass / before if before > 0.0 else None,
                "hard_zero": before > 0.0 and hard_allowed_mass <= 0.0,
                **component_values,
                "combined_log_posterior_ratio": (
                    math.log(after / before) if before > 0.0 and after > 0.0 else None
                ),
                "probability_after": after,
                "probability_delta": after - before,
                "probability_delta_pct_points": (after - before) * 100.0,
            }
        )
    return rows


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
        tcu_minutes=float(raw.get("tcu_minutes") or 0.0),
        cloud_suppression_active=bool(raw.get("cloud_suppression_active")),
        precipitation_active=bool(raw.get("precipitation_active")),
        cb_active=bool(raw.get("cb_active")),
        tcu_active=bool(raw.get("tcu_active")),
        canonical_event_count=int(raw.get("canonical_event_count") or 0),
        physical_event_cluster_signature=_text(raw.get("physical_event_cluster_signature")),
        physical_event_cluster_minutes=float(raw.get("physical_event_cluster_minutes") or 0.0),
        physical_event_cluster_information_spent=float(
            raw.get("physical_event_cluster_information_spent") or 0.0
        ),
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
    if _text(previous.get("scenario_joint_schema")) != SCENARIO_JOINT_SCHEMA:
        raise ValueError("v6_previous_scenario_joint_checkpoint_missing")
    if scenario_joint_sha256(previous.get("scenario_joint_posterior_json")) != _text(
        previous.get("scenario_joint_posterior_sha256")
    ):
        raise ValueError("v6_previous_scenario_joint_checkpoint_hash_mismatch")
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

    previous_joint = _previous_joint(previous)
    forcing_rows = tuple(candidate_forcing)
    prediction, predicted_scenario_joint = predict_scenario_joint_to_event(
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
        duration_clocks_before=_duration_clocks(previous),
        event_id=event_id,
        event_valid_at=event_valid_at,
        delta_minutes=delta_minutes,
        forcing_generation_sha256=_text(previous.get("forcing_generation_sha256")),
        transition_artifact_sha256=_text(authorized.get("transition_artifact_sha256")),
        candidate_forcing=forcing_rows,
    )
    if emission.pre_event_prediction_sha256 != pre_event_prediction_sha256(prediction):
        raise ValueError("v6_emission_not_bound_to_compiled_prediction")
    post, scenario_joint, scenario_responsibility_trace = update_scenario_joint_with_observation(
        prediction=prediction,
        predicted_scenario_joint=predicted_scenario_joint,
        emission=emission,
    )
    reported_x = project_reported_x(post)
    bucket_observation_evidence = _build_bucket_observation_evidence_rows(
        prediction=prediction, emission=emission, post=post
    )
    predicted_by_cell = {(row.y_c, row.state): row.probability for row in prediction.joint}
    post_by_cell = {(row.y_c, row.state): row.probability for row in post.joint}
    posterior_delta = []
    for key, pre_probability in sorted(predicted_by_cell.items()):
        post_probability = post_by_cell.get(key, 0.0)
        final_locked_compatible = final_locked_reported_x_compatible(
            y_c=key[0],
            state=key[1],
            observed_reported_regular_max_c=post.observed_reported_regular_max_c,
        )
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
                "final_locked_reported_x_compatible": final_locked_compatible,
            }
        )
    joint = [
        {"continuous_y_c": row.y_c, "state": row.state, "probability": row.probability}
        for row in post.joint
    ]
    joint_sha = _sha(joint)
    scenario_joint_payload = list(scenario_joint)
    scenario_joint_sha = scenario_joint_sha256(scenario_joint)
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
        "tcu_present": emission.tcu_present,
        "convective_evidence_kind": emission.convective_evidence_kind,
        "physical_event_cluster_signature": emission.physical_event_cluster_signature,
        "physical_event_cluster_change_point": emission.physical_event_cluster_change_point,
        "event_information_weight": emission.event_information_weight,
        "observation_feature_trace": dict(emission.observation_feature_trace),
        "cell_log_component_trace": [
            {
                "continuous_y_c": y_c,
                "state": state,
                **dict(components),
            }
            for (y_c, state), components in sorted(emission.log_component_by_cell.items())
        ],
        "bucket_observation_evidence": {
            "schema": "v6_observation_bucket_evidence_v1",
            "rows": bucket_observation_evidence,
            "hard_lower_bound_before_c": prediction.previous_y_lower_bound_c,
            "hard_lower_bound_after_c": post.y_lower_bound_c,
            "reported_x_lower_bound_before_c": prediction.previous_reported_regular_max_c,
            "reported_x_lower_bound_after_c": post.observed_reported_regular_max_c,
            "cell_components_exactly_additive": True,
            "bucket_components_are_relative_marginal_log_evidence": True,
            "bucket_components_not_additive_after_latent_state_marginalization": True,
            "combined_log_posterior_ratio_is_exact": True,
            "frontend_recompute_allowed": False,
        },
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
        "event_local_scenario_responsibility_trace": scenario_responsibility_trace,
        "scenario_control_lineage_sha256": _text(scenario_control_lineage_sha256),
        "canonical_emission_input_sha256": _text(canonical_emission_input_sha256),
        "scenario_joint_posterior_sha256": scenario_joint_sha,
        "scenario_joint_schema": SCENARIO_JOINT_SCHEMA,
        "scenario_joint_forcing_generation_sha256": prediction.forcing_generation_sha256,
        "nwp_hourly_path_likelihood_reapplied": False,
        "current_observation_consumed_once": True,
        "no_post_emission_state_transition": True,
        "final_locked_reported_x_compatibility_hard_condition": True,
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
        "scenario_joint_posterior_json": _json(scenario_joint_payload),
        "scenario_joint_posterior_sha256": scenario_joint_sha,
        "scenario_joint_schema": SCENARIO_JOINT_SCHEMA,
        "scenario_joint_forcing_generation_sha256": prediction.forcing_generation_sha256,
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


def build_identifiable_physical_transaction_b_candidate(
    *,
    activation: Mapping[str, Any],
    previous_checkpoint: Mapping[str, Any],
    canonical_event: Mapping[str, Any],
    physical_extension: Mapping[str, Any],
    decision_as_of: str,
    completed_at: str,
) -> dict[str, Any]:
    """Build the same Transaction-B rows from the replacement composer.

    No legacy Y/state update is evaluated on this branch.  The generic legacy
    columns remain populated only as storage compatibility fields; the
    authoritative internal hash is ``physical_state_posterior_sha256``.
    """

    from identifiable_physical_state_bayes.contracts import SCHEMA_VERSION as PHYSICAL_SCHEMA
    from production_probability_physical_state_composer import COMPOSER_SCHEMA as PHYSICAL_COMPOSER
    from production_probability_v6_activation_contract import (
        IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA,
    )

    authorized = validate_v6_activation(activation)
    if _text(authorized.get("composer_schema")) != IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA:
        raise ValueError("physical_candidate_activation_composer_invalid")
    previous = dict(previous_checkpoint)
    extension = dict(physical_extension)
    if _text(extension.get("physical_composer_schema")) != PHYSICAL_COMPOSER:
        raise ValueError("physical_candidate_extension_schema_invalid")
    if int(extension.get("physical_current_event_consumption_count") or 0) != 1:
        raise ValueError("physical_candidate_event_consumption_count_invalid")
    invariants = extension.get("invariant_report")
    if not isinstance(invariants, Mapping) or any(
        invariants.get(name) is not True
        for name in (
            "pre_event_predict_excluded_current_observation",
            "current_event_consumed_exactly_once",
            "non_contract_speci_does_not_raise_x_bound",
            "no_post_emission_transition",
            "joint_normalized",
        )
    ):
        raise ValueError("physical_candidate_invariant_report_invalid")
    event_id = _text(canonical_event.get("canonical_observation_event_id"))
    event_valid_at = _text(canonical_event.get("valid_time"))
    if not event_id or event_valid_at != _text(extension.get("last_event_valid_at")):
        raise ValueError("physical_candidate_event_identity_invalid")
    consumed = json.loads(_text(previous.get("consumed_event_ids_json")) or "[]")
    if event_id in consumed:
        raise ValueError("physical_candidate_event_already_consumed")
    consumed.append(event_id)
    state_payload = _text(extension.get("physical_state_posterior_json"))
    state_sha = _text(extension.get("physical_state_posterior_sha256"))
    if len(state_sha) != 64 or not state_payload:
        raise ValueError("physical_candidate_state_identity_invalid")
    reported = {
        str(int(key)): float(value)
        for key, value in dict(extension.get("reported_physical_x_distribution") or {}).items()
    }
    if not reported or abs(sum(reported.values()) - 1.0) > 1e-9:
        raise ValueError("physical_candidate_reported_x_distribution_invalid")
    lower = extension.get("reported_x_lower_bound_c")
    if lower is not None and any(int(key) < int(lower) for key in reported):
        raise ValueError("physical_candidate_reported_x_hard_bound_violation")
    prediction = json.loads(_text(extension.get("physical_pre_event_state_json")) or "{}")
    likelihood = json.loads(_text(extension.get("physical_observation_likelihood_json")) or "{}")
    conditioned = json.loads(_text(extension.get("physical_post_event_state_json")) or "{}")
    projection = json.loads(_text(extension.get("physical_path_projection_json")) or "{}")
    if not all((prediction, likelihood, conditioned, projection)):
        raise ValueError("physical_candidate_audit_payload_missing")
    previous_weights = {
        _text(row.get("component_id")): float(row.get("normalized_weight") or 0.0)
        for row in (
            json.loads(_text(previous.get("physical_state_posterior_json")) or "{}").get("components")
            or []
        )
        if isinstance(row, Mapping)
    }
    next_weights = {
        _text(row.get("component_id")): float(row.get("normalized_weight") or 0.0)
        for row in (json.loads(state_payload).get("components") or [])
        if isinstance(row, Mapping)
    }
    posterior_delta = {
        "schema": "identifiable_physical_component_weight_delta_v1",
        "previous_component_weights": previous_weights,
        "post_component_weights": next_weights,
        "component_identity_changes_on_predict_and_condition": True,
    }
    update_identity = {
        "schema": STRONG_UPDATE_SCHEMA,
        "composer_schema": PHYSICAL_COMPOSER,
        "previous_posterior_run_id": _text(previous.get("posterior_run_id")),
        "previous_physical_state_posterior_sha256": _text(
            previous.get("physical_state_posterior_sha256")
        ),
        "canonical_observation_event_id": event_id,
        "physical_pre_event_state_sha256": _text(
            extension.get("physical_pre_event_state_sha256")
        ),
        "physical_observation_likelihood_sha256": _text(
            extension.get("physical_observation_likelihood_sha256")
        ),
        "physical_state_model_artifact_sha256": _text(
            extension.get("physical_state_model_artifact_sha256")
        ),
        "physical_observation_artifact_sha256": _text(
            extension.get("physical_observation_artifact_sha256")
        ),
    }
    update_id = "strong-physical-" + _sha(update_identity)
    run_id = "posterior-physical-" + _sha(
        {**update_identity, "physical_state_posterior_sha256": state_sha}
    )
    checkpoint_id = "checkpoint-physical-" + _sha(
        {
            "posterior_run_id": run_id,
            "previous_checkpoint_id": _text(previous.get("posterior_checkpoint_id")),
        }
    )
    observed_probability_no_new_high = None
    if lower is not None:
        observed_probability_no_new_high = sum(
            probability for bucket, probability in reported.items() if int(bucket) <= int(lower)
        )
    termination = {
        "schema": "termination_risk_marginal_v3_physical_state_bayes_v6",
        "state": "available" if lower is not None else "na",
        "probability_no_material_new_daily_high": observed_probability_no_new_high,
        "updates_posterior": False,
        "canonical_observation_event_id": event_id,
    }
    auxiliary = {
        "schema": "identifiable_physical_state_path_auxiliary_v1",
        "termination_risk": termination,
        "physical_composer_schema": PHYSICAL_COMPOSER,
        "physical_path_projection_sha256": _text(
            extension.get("physical_path_projection_sha256")
        ),
        "current_observation_consumed_once": True,
        "current_observation_consumption_count": 1,
        "nwp_hourly_path_likelihood_reapplied": False,
        "no_post_emission_state_transition": True,
        "reported_x_hard_condition_applied": lower is not None,
        "invariant_report": dict(invariants),
    }
    raw_scores = [
        {
            "component_id": _text(row.get("component_id")),
            "log_likelihood": float(row.get("log_likelihood") or 0.0),
            "component_trace": row.get("component_trace") or {},
        }
        for row in likelihood.get("component_conditioning") or []
        if isinstance(row, Mapping)
    ]
    normalizer_log = float(conditioned.get("normalizer_log") or 0.0)
    update_row = {
        "strong_observation_update_id": update_id,
        "target_identity": _text(previous.get("target_identity")),
        "previous_posterior_run_id": _text(previous.get("posterior_run_id")),
        "previous_posterior_sha256": _text(previous.get("joint_posterior_sha256")),
        "previous_checkpoint_id": _text(previous.get("posterior_checkpoint_id")),
        "canonical_observation_event_id": event_id,
        "forecast_evidence_snapshot_id": _text(previous.get("forecast_evidence_snapshot_id")),
        "prior_context_id": _text(previous.get("prior_context_id")),
        "model_artifact_sha256": _text(authorized.get("activation_contract_sha256")),
        "decision_as_of": _text(decision_as_of),
        "state": "applied",
        "raw_log_score_json": _json(raw_scores),
        "raw_log_score_sha256": _sha(raw_scores),
        "normalizer_offset": -normalizer_log,
        "posterior_delta_json": _json(posterior_delta),
        "posterior_delta_sha256": _sha(posterior_delta),
        "pre_state_transition_json": _text(extension.get("physical_pre_event_state_json")),
        "post_state_transition_json": "{}",
        "source_reliability_before_json": "{}",
        "source_reliability_after_json": "{}",
        "path_auxiliary_json": _json(auxiliary),
        "observed_max_lower_bound_c": (
            float(lower) - 0.5 if lower is not None else None
        ),
        "quality_json": _json({"canonical_event_state": canonical_event.get("state")}),
        "block_reasons_json": "[]",
        "started_at": _text(decision_as_of),
        "completed_at": _text(completed_at),
        "created_at": _text(completed_at),
        "probability_update_schema": STRONG_UPDATE_SCHEMA,
        "pre_event_prediction_json": _text(extension.get("physical_pre_event_state_json")),
        "pre_event_prediction_sha256": _text(extension.get("physical_pre_event_state_sha256")),
        "observation_emission_json": _text(
            extension.get("physical_observation_likelihood_json")
        ),
        "observation_emission_sha256": _text(
            extension.get("physical_observation_likelihood_sha256")
        ),
        "duration_clocks_before_json": "{}",
        "duration_clocks_after_json": "{}",
        "forcing_generation_sha256": _text(
            extension.get("physical_driver_generation_sha256")
        ),
        "transition_artifact_sha256": "",
        "observation_artifact_sha256": "",
        "scenario_control_lineage_sha256": "",
        "canonical_emission_input_sha256": _sha(canonical_event),
        "no_post_emission_transition": 1,
        "physical_pre_event_state_json": _text(
            extension.get("physical_pre_event_state_json")
        ),
        "physical_pre_event_state_sha256": _text(
            extension.get("physical_pre_event_state_sha256")
        ),
        "physical_observation_likelihood_json": _text(
            extension.get("physical_observation_likelihood_json")
        ),
        "physical_observation_likelihood_sha256": _text(
            extension.get("physical_observation_likelihood_sha256")
        ),
        "physical_post_event_state_json": _text(
            extension.get("physical_post_event_state_json")
        ),
        "physical_post_event_state_sha256": _text(
            extension.get("physical_post_event_state_sha256")
        ),
        "physical_current_event_consumption_count": 1,
    }
    checkpoint_row = {
        "posterior_checkpoint_id": checkpoint_id,
        "target_identity": _text(previous.get("target_identity")),
        "posterior_run_id": run_id,
        "previous_checkpoint_id": _text(previous.get("posterior_checkpoint_id")),
        "checkpoint_kind": "incremental",
        "forecast_evidence_snapshot_id": _text(previous.get("forecast_evidence_snapshot_id")),
        "prior_context_id": _text(previous.get("prior_context_id")),
        "resolution_contract_id": _text(previous.get("resolution_contract_id")),
        "last_strong_update_id": update_id,
        "last_canonical_event_id": event_id,
        "canonical_sequence_sha256": _sha(consumed),
        "temperature_grid_id": _sha(sorted(reported, key=float)),
        "state_grid_id": _sha(PHYSICAL_SCHEMA),
        "joint_posterior_json": state_payload,
        "joint_posterior_sha256": state_sha,
        "physical_high_distribution_json": _json(
            projection.get("latent_continuous_peak_distribution") or {}
        ),
        "settlement_scalar_distribution_json": "[]",
        "market_bucket_distribution_json": "{}",
        "latent_post_state_json": state_payload,
        "trajectory_feature_state_json": "{}",
        "source_reliability_state_json": "{}",
        "observed_max_lower_bound_c": float(lower) - 0.5 if lower is not None else None,
        "consumed_event_ids_json": _json(consumed),
        "consumed_ancestor_sha256": _sha(consumed),
        "model_artifact_sha256": _text(authorized.get("activation_contract_sha256")),
        "replay_generation": int(previous.get("replay_generation") or 0),
        "supersedes_checkpoint_id": "",
        "state": "production_successor_ready",
        "created_at": _text(completed_at),
        "committed_at": _text(completed_at),
        "checkpoint_probability_schema": CHECKPOINT_SCHEMA,
        "probability_target_type": "amsc_scheduled_regular_metar_reported_daily_max_c",
        "latent_scheduled_slot_high_distribution_json": _json(
            projection.get("latent_continuous_peak_distribution") or {}
        ),
        "reported_physical_x_distribution_json": _json(reported),
        "latent_observed_lower_bound_c": float(lower) - 0.5 if lower is not None else None,
        "reported_x_lower_bound_c": int(lower) if lower is not None else None,
        "reported_x_termination_json": _json(termination),
        "reported_x_projection_sha256": _sha(reported),
        "state_space_schema": PHYSICAL_SCHEMA,
        "continuous_y_joint_sha256": "",
        "duration_clocks_json": "{}",
        "last_event_valid_at": event_valid_at,
        "forcing_generation_sha256": _text(
            extension.get("physical_driver_generation_sha256")
        ),
        "transition_artifact_sha256": "",
        "observation_artifact_sha256": "",
        "pre_event_prediction_sha256": _text(
            extension.get("physical_pre_event_state_sha256")
        ),
        "scenario_control_lineage_sha256": "",
        "canonical_emission_input_sha256": _sha(canonical_event),
        "p0_pruned_probability_mass": 0.0,
        "p0_pruned_cell_count": 0,
        "scenario_joint_posterior_json": "{}",
        "scenario_joint_posterior_sha256": "",
        "scenario_joint_schema": "",
        "scenario_joint_forcing_generation_sha256": "",
        "physical_composer_schema": PHYSICAL_COMPOSER,
        "physical_state_posterior_schema": PHYSICAL_SCHEMA,
        "physical_state_posterior_json": state_payload,
        "physical_state_posterior_sha256": state_sha,
        "physical_path_projection_json": _text(
            extension.get("physical_path_projection_json")
        ),
        "physical_path_projection_sha256": _text(
            extension.get("physical_path_projection_sha256")
        ),
        "physical_state_model_artifact_sha256": _text(
            extension.get("physical_state_model_artifact_sha256")
        ),
        "physical_observation_artifact_sha256": _text(
            extension.get("physical_observation_artifact_sha256")
        ),
        "physical_reporting_operator_sha256": _text(
            extension.get("physical_reporting_operator_sha256")
        ),
        "physical_driver_generation_sha256": _text(
            extension.get("physical_driver_generation_sha256")
        ),
        "physical_state_reference_valid_at": event_valid_at,
        "physical_last_weak_advance_json": _text(
            previous.get("physical_last_weak_advance_json")
        ) or "{}",
        "physical_last_weak_advance_sha256": _text(
            previous.get("physical_last_weak_advance_sha256")
        ),
        "physical_forcing_rebind_json": _text(
            previous.get("physical_forcing_rebind_json")
        ) or "{}",
        "physical_forcing_rebind_sha256": _text(
            previous.get("physical_forcing_rebind_sha256")
        ),
    }
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "production_successor_candidate_ready",
        "candidate_mode": "identifiable_physical_state",
        "shadow_only": False,
        "strong_update_row": update_row,
        "checkpoint_row": checkpoint_row,
        "reported_physical_x_distribution": reported,
        "places_orders": False,
    }


__all__ = [
    "SCHEMA_VERSION",
    "build_identifiable_physical_transaction_b_candidate",
    "build_v6_transaction_b_candidate",
]
