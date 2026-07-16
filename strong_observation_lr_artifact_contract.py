"""Immutable contract for the D0 strong-observation likelihood artifact.

The artifact is deliberately narrower than the sequential Bayes composer: it
can turn one canonical observation, its deterministic trajectory, its physical
evidence and the *pre-event* source-reliability state into raw log scores and a
latent post-state proposal.  It can never centre a score, apply a hard mask,
normalise a posterior, or see market / strategy inputs.
"""

from __future__ import annotations

import math
import hashlib
import json
from typing import Any, Mapping

from temperature_trajectory_feature_runtime import OWNER_ID as TRAJECTORY_OWNER_ID


SCHEMA_VERSION = "strong_observation_incremental_lr_artifact_v3"
INPUT_SCHEMA_VERSION = "strong_observation_lr_input_v3"
FEATURE_EVALUATOR_SCHEMA_VERSION = "strong_observation_feature_evaluator_v2"
OPERATIONAL_STATE_AWARE_SCORING_KIND = "operational_physical_state_aware_lr_v2"
OPERATIONAL_STATE_MODEL_SCHEMA = "operational_physical_state_aware_lr_v2"
OPERATIONAL_LATENT_STATES = frozenset({
    "warming",
    "slowing_warming",
    "temporary_platform",
    "temporary_cooling",
    "cooling_then_rebound",
    "weather_suppressed",
    "terminal_cooling",
    "final_locked",
})

# These are the evidence families agreed for the strong likelihood.  An
# approved artifact must explicitly handle missing / other values instead of
# quietly omitting a physical observation it was not trained to interpret.
REQUIRED_FEATURE_IDS = (
    "canonical.temperature_c",
    "timing.local_hour",
    "timing.first_cb_local_hour",
    "trajectory.pre_event_path_state",
    "trajectory.current_step_state",
    "trajectory.current_step_rate_c_per_hour",
    "trajectory.slope_60m_c_per_hour",
    "trajectory.slope_120m_c_per_hour",
    "trajectory.acceleration_c_per_hour_squared",
    "trajectory.observed_peak_including_current_c",
    "trajectory.cooling_depth_including_current_c",
    "trajectory.cooling_duration_minutes",
    "candidate.remaining_warming_c",
    "physical.dewpoint_depression_c",
    "physical.cloud_regime",
    "physical.first_cb_event_state",
    "physical.first_cb_timing_phase",
    "physical.first_cb_peak_relation",
    "physical.weather_regime",
    "source_reliability.selected_source_state",
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _artifact_content_sha256(value: Mapping[str, Any]) -> str:
    material = {
        key: item
        for key, item in dict(value).items()
        if key not in {"artifact_sha256", "model_artifact_sha256", "artifact_content_sha256"}
    }
    return hashlib.sha256(json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _forbidden_keys(value: Any, *, path: str = "") -> list[str]:
    forbidden = (
        "market",
        "price",
        "order",
        "position",
        "pnl",
        "edge",
        "lcb",
        "beta_strong",
        "posterior_delta",
        "normalizer",
    )
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            lowered = _text(key).lower()
            child_path = f"{path}.{lowered}" if path else lowered
            if any(token in lowered for token in forbidden):
                found.append(child_path)
            found.extend(_forbidden_keys(child, path=child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_forbidden_keys(child, path=f"{path}[{index}]"))
    return found


def _score_surface_valid(value: Any) -> bool:
    surface = _mapping(value)
    return bool(surface) and all(_finite(score) is not None for score in surface.values())


def _validate_feature_definition(feature_id: str, definition: Mapping[str, Any]) -> list[str]:
    """Validate one trained feature mapping without scoring a live event."""

    issues: list[str] = []
    kind = _text(definition.get("kind"))
    adjustments = _mapping(definition.get("value_score_adjustments"))
    if kind not in {"categorical", "numeric_binned"}:
        return [f"strong_lr_artifact_feature_{feature_id}_kind_invalid"]
    if not adjustments:
        issues.append(f"strong_lr_artifact_feature_{feature_id}_adjustments_missing")
        return issues
    if "__missing__" not in adjustments:
        issues.append(f"strong_lr_artifact_feature_{feature_id}_missing_value_handler_missing")
    if kind == "categorical" and "__other__" not in adjustments:
        issues.append(f"strong_lr_artifact_feature_{feature_id}_other_value_handler_missing")
    bins = definition.get("bins")
    labels: set[str] = set()
    if kind == "numeric_binned":
        if not isinstance(bins, list) or not bins:
            issues.append(f"strong_lr_artifact_feature_{feature_id}_bins_missing")
        else:
            for item in bins:
                bin_row = _mapping(item)
                label = _text(bin_row.get("label"))
                lower = _finite(bin_row.get("lower_inclusive")) if "lower_inclusive" in bin_row else None
                upper = _finite(bin_row.get("upper_exclusive")) if "upper_exclusive" in bin_row else None
                if not label or label in labels or ("lower_inclusive" in bin_row and lower is None) or ("upper_exclusive" in bin_row and upper is None):
                    issues.append(f"strong_lr_artifact_feature_{feature_id}_bin_invalid")
                    continue
                if lower is not None and upper is not None and lower >= upper:
                    issues.append(f"strong_lr_artifact_feature_{feature_id}_bin_interval_invalid")
                labels.add(label)
            if len(labels) != len(bins):
                issues.append(f"strong_lr_artifact_feature_{feature_id}_bin_labels_invalid")
            for label in labels:
                if label not in adjustments:
                    issues.append(f"strong_lr_artifact_feature_{feature_id}_bin_adjustment_missing")
    for value_label, adjustment in adjustments.items():
        if not _text(value_label) or not _score_surface_valid(
            _mapping(adjustment).get("raw_log_score_adjustment_by_temperature_and_pre_state")
        ):
            issues.append(f"strong_lr_artifact_feature_{feature_id}_adjustment_surface_invalid")
            break
    return issues


def _validate_operational_state_model(value: Mapping[str, Any]) -> list[str]:
    if _text(value.get("scoring_kind")) != OPERATIONAL_STATE_AWARE_SCORING_KIND:
        return []
    issues: list[str] = []
    model = _mapping(value.get("operational_state_model"))
    if _text(model.get("schema")) != OPERATIONAL_STATE_MODEL_SCHEMA:
        issues.append("strong_lr_operational_state_model_schema_invalid")
    profiles = _mapping(model.get("state_profiles"))
    if set(profiles) != set(OPERATIONAL_LATENT_STATES):
        issues.append("strong_lr_operational_state_profiles_incomplete_or_extra")
    for state in OPERATIONAL_LATENT_STATES:
        profile = _mapping(profiles.get(state))
        if any(
            _finite(profile.get(key)) is None
            for key in (
                "step_rate",
                "trend_rate",
                "remaining_capacity_fraction",
                "remaining_floor_c",
            )
        ):
            issues.append(f"strong_lr_operational_state_profile_invalid:{state}")
    positive = (
        "step_rate_sigma_c_per_hour",
        "trend_rate_sigma_c_per_hour",
        "remaining_sigma_c",
        "state_information_scale",
        "maximum_relative_log_penalty",
    )
    if any((_finite(model.get(key)) or 0.0) <= 0.0 for key in positive):
        issues.append("strong_lr_operational_state_positive_parameter_invalid")
    nonnegative = (
        "early_day_terminal_penalty",
        "late_day_warming_penalty_per_hour",
        "precipitation_warming_penalty",
        "clear_weather_suppressed_penalty",
        "early_first_cb_warming_penalty",
        "peak_window_first_cb_warming_penalty",
        "late_first_cb_warming_penalty",
    )
    if any(_finite(model.get(key)) is None or float(model.get(key)) < 0.0 for key in nonnegative):
        issues.append("strong_lr_operational_state_penalty_parameter_invalid")
    persistence = _finite(model.get("post_state_persistence"))
    probability_floor = _finite(model.get("post_state_probability_floor"))
    if (
        model.get("dynamic_post_state_transition") is not True
        or (_finite(model.get("post_state_target_logit_scale")) or 0.0) <= 0.0
        or persistence is None
        or not 0.0 <= persistence < 1.0
        or probability_floor is None
        or not 0.0 <= probability_floor < 1.0 / len(OPERATIONAL_LATENT_STATES)
    ):
        issues.append("strong_lr_operational_state_dynamic_transition_parameter_invalid")
    baseline = _mapping(value.get("baseline_contract"))
    if (
        baseline.get("temperature_and_latent_state_are_jointly_scored") is not True
        or baseline.get("state_profiles_cover_exact_latent_state_grid") is not True
        or baseline.get("legacy_temperature_only_runtime_fallback_forbidden") is not True
        or baseline.get("post_state_transition_is_event_conditioned_and_row_normalized") is not True
        or baseline.get("identity_state_transition_fallback_forbidden") is not True
        or baseline.get("first_cb_is_temporal_evidence_not_boolean_gate") is not True
        or baseline.get("first_cb_only_penalizes_unrealized_remaining_warming") is not True
        or baseline.get("first_cb_never_reverses_observed_max_hard_condition") is not True
    ):
        issues.append("strong_lr_operational_state_baseline_boundary_invalid")
    return issues


def _validate_strong_observation_lr_artifact(
    artifact: Mapping[str, Any] | None,
    *,
    require_release_evidence: bool,
) -> dict[str, Any]:
    """Validate structural runtime safety, optionally with offline packet fields.

    A v1 static temperature/state table is intentionally invalid here.  It
    was a persistence-only placeholder and did not consume D0 trajectory or
    physical evidence, therefore cannot be called a strong likelihood.
    """

    value = _mapping(artifact)
    issues: list[str] = []
    if value.get("schema") != SCHEMA_VERSION:
        issues.append("strong_lr_artifact_schema_invalid")
    for key in ("artifact_sha256", "model_artifact_sha256", "feature_builder_version"):
        if not _text(value.get(key)):
            issues.append(f"strong_lr_artifact_{key}_missing")
    computed_content_sha = _artifact_content_sha256(value)
    content_sha = _text(value.get("artifact_content_sha256"))
    requires_content_sha = bool(_mapping(value.get("fit_lineage")))
    if requires_content_sha and not content_sha:
        issues.append("strong_lr_artifact_content_sha256_missing")
    if content_sha and content_sha != computed_content_sha:
        issues.append("strong_lr_artifact_content_sha256_mismatch")
    if content_sha and not (_text(value.get("artifact_sha256")) == _text(value.get("model_artifact_sha256")) == content_sha):
        issues.append("strong_lr_artifact_identity_mismatch")
    elif not content_sha and _text(value.get("artifact_sha256")) != _text(value.get("model_artifact_sha256")):
        issues.append("strong_lr_artifact_identity_mismatch")
    if require_release_evidence:
        for key in ("training_manifest_sha256", "oos_evaluation_sha256"):
            if not _text(value.get(key)):
                issues.append(f"strong_lr_artifact_{key}_missing")
        if _text(value.get("approval_state")) != "approved":
            issues.append("strong_lr_artifact_not_approved")
        if _text(value.get("training_data_policy")) != "strict_asof_canonical_event_v2":
            issues.append("strong_lr_artifact_training_data_policy_invalid")
    input_contract = _mapping(value.get("input_contract"))
    if _text(input_contract.get("trajectory_owner_id")) != TRAJECTORY_OWNER_ID:
        issues.append("strong_lr_artifact_trajectory_owner_invalid")
    if input_contract.get("d0_source_reliability_is_pre_state_only") is not True:
        issues.append("strong_lr_artifact_source_reliability_timing_invalid")
    if input_contract.get("canonical_observation_is_only_fact_source") is not True:
        issues.append("strong_lr_artifact_canonical_fact_source_contract_invalid")
    runtime_contract = _mapping(value.get("runtime_contract"))
    required_runtime = {
        "output": "raw_log_score_and_post_state_transition_only",
        "composer_owns_normalizer": True,
        "composer_owns_posterior_delta": True,
        "composer_owns_hard_condition": True,
        "same_event_cannot_update_its_pre_state": True,
        "creates_independent_posterior": False,
        "creates_strategy_signal": False,
    }
    for key, expected in required_runtime.items():
        if runtime_contract.get(key) != expected:
            issues.append(f"strong_lr_artifact_runtime_contract_{key}_invalid")
    raw_scores = _mapping(value.get("raw_log_score_by_pre_state"))
    by_cell = _mapping(value.get("raw_log_score_by_temperature_and_pre_state"))
    if not raw_scores and not by_cell and _finite(value.get("default_raw_log_score")) is None:
        issues.append("strong_lr_artifact_base_raw_log_score_surface_missing_or_invalid")
    if raw_scores and not _score_surface_valid(raw_scores):
        issues.append("strong_lr_artifact_raw_log_score_surface_invalid")
    if by_cell and not _score_surface_valid(by_cell):
        issues.append("strong_lr_artifact_cell_raw_log_score_surface_invalid")
    transitions = _mapping(value.get("post_state_transition"))
    if not transitions:
        issues.append("strong_lr_artifact_post_state_transition_missing")
    for pre_state, raw_row in transitions.items():
        row = _mapping(raw_row)
        probabilities = [_finite(probability) for probability in row.values()]
        if not _text(pre_state) or not row or any(probability is None or probability < 0.0 for probability in probabilities):
            issues.append("strong_lr_artifact_post_state_transition_invalid")
            break
        if abs(sum(float(probability) for probability in probabilities if probability is not None) - 1.0) > 1e-8:
            issues.append("strong_lr_artifact_post_state_transition_not_normalized")
            break
    evaluator = _mapping(value.get("feature_evaluator"))
    if _text(evaluator.get("schema")) != FEATURE_EVALUATOR_SCHEMA_VERSION:
        issues.append("strong_lr_artifact_feature_evaluator_schema_invalid")
    if require_release_evidence:
        for key in ("training_feature_manifest_sha256", "oos_feature_ablation_sha256"):
            if not _text(evaluator.get(key)):
                issues.append(f"strong_lr_artifact_feature_evaluator_{key}_missing")
    feature_ids = tuple(_text(item) for item in evaluator.get("required_feature_ids") or [])
    if feature_ids != REQUIRED_FEATURE_IDS:
        issues.append("strong_lr_artifact_required_feature_set_invalid")
    definitions = _mapping(evaluator.get("feature_definitions"))
    if set(definitions) != set(REQUIRED_FEATURE_IDS):
        issues.append("strong_lr_artifact_feature_definitions_incomplete_or_extra")
    for feature_id in REQUIRED_FEATURE_IDS:
        issues.extend(_validate_feature_definition(feature_id, _mapping(definitions.get(feature_id))))
    issues.extend(_validate_operational_state_model(value))
    artifact_scoring_surface = {key: item for key, item in value.items() if key != "runtime_contract"}
    if _forbidden_keys(artifact_scoring_surface):
        issues.append("strong_lr_artifact_contains_forbidden_market_or_composer_surface")
    return {
        "schema": SCHEMA_VERSION,
        "ok": not issues,
        "state": "ready" if not issues else "blocked",
        "validation_scope": "offline_artifact_packet_structure" if require_release_evidence else "runtime_structural_only",
        "issues": sorted(set(issues)),
        "artifact_sha256": _text(value.get("artifact_sha256")),
        "read_only": True,
    }


def validate_strong_observation_lr_artifact(artifact: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate the offline evidence *packet* fields for a new artifact.

    It proves the artifact contains the required immutable references but does
    not resolve/recalculate them.  Full strict-as-of/OOS/ablation promotion is
    owned by ``strong_observation_artifact_promotion_gate_v1`` and must never
    be called from the live decision/order path.  Live safety is based on the
    committed posterior, current data freshness, risk and execution facts.
    """

    return _validate_strong_observation_lr_artifact(artifact, require_release_evidence=True)


def validate_strong_observation_lr_runtime_artifact(artifact: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate only runtime structure; strict-as-of/OOS never block a trade."""

    return _validate_strong_observation_lr_artifact(artifact, require_release_evidence=False)


def build_strong_observation_lr_input(
    *,
    canonical_observation_event: Mapping[str, Any],
    trajectory_feature_state: Mapping[str, Any],
    forecast_evidence_snapshot_id: str,
    prior_context_id: str,
    source_reliability_pre_state: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind the only permitted inputs for one raw log-LR evaluation."""

    event = _mapping(canonical_observation_event)
    trajectory = _mapping(trajectory_feature_state)
    issues: list[str] = []
    if _text(event.get("state")) not in {"accepted_primary", "accepted_substitute"}:
        issues.append("strong_lr_input_canonical_event_not_accepted")
    event_id = _text(event.get("canonical_observation_event_id"))
    if not event_id:
        issues.append("strong_lr_input_canonical_event_id_missing")
    if trajectory.get("ok") is not True or _text(trajectory.get("owner_id")) != TRAJECTORY_OWNER_ID:
        issues.append("strong_lr_input_trajectory_owner_or_state_invalid")
    if _text(trajectory.get("temporal_role")) != "pre_event":
        issues.append("strong_lr_input_trajectory_not_pre_event")
    if _text(trajectory.get("target_identity")) != _text(event.get("target_identity")):
        issues.append("strong_lr_input_trajectory_target_mismatch")
    if _text(trajectory.get("station_id")).upper() != _text(event.get("station_id")).upper():
        issues.append("strong_lr_input_trajectory_station_mismatch")
    if event_id and event_id in set(trajectory.get("canonical_sequence_event_ids") or []):
        issues.append("strong_lr_input_trajectory_contains_current_event")
    if not _text(forecast_evidence_snapshot_id):
        issues.append("strong_lr_input_forecast_snapshot_id_missing")
    if not _text(prior_context_id):
        issues.append("strong_lr_input_prior_context_id_missing")
    if _forbidden_keys(source_reliability_pre_state):
        issues.append("strong_lr_input_source_reliability_contains_forbidden_surface")
    if issues:
        return {
            "schema": INPUT_SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": sorted(set(issues)),
            "read_only": True,
        }
    parsed = dict(_mapping(event.get("parsed_observation")))
    parsed.setdefault("valid_time", _text(event.get("valid_time")))
    parsed.setdefault("station_id", _text(event.get("station_id")).upper())
    parsed.setdefault("report_type", _text(event.get("report_type")))
    return {
        "schema": INPUT_SCHEMA_VERSION,
        "ok": True,
        "state": "available",
        "canonical_observation_event_id": event_id,
        "canonical_observation": dict(parsed),
        "selected_source_id": _text(event.get("selected_source_id")),
        "trajectory_feature_state": dict(trajectory),
        "forecast_evidence_snapshot_id": _text(forecast_evidence_snapshot_id),
        "prior_context_id": _text(prior_context_id),
        "source_reliability_pre_state": dict(source_reliability_pre_state),
        "boundary": {
            "observations_are_primary_fact_input": True,
            "trajectory_is_pre_event_only": True,
            "trajectory_consumed_once_inside_strong_artifact": True,
            "source_reliability_is_pre_state_only": True,
            "outputs_must_be_raw_log_score_and_post_state_proposal": True,
            "composer_owns_normalizer_delta_and_hard_condition": True,
        },
        "read_only": True,
    }


__all__ = [
    "FEATURE_EVALUATOR_SCHEMA_VERSION",
    "INPUT_SCHEMA_VERSION",
    "REQUIRED_FEATURE_IDS",
    "SCHEMA_VERSION",
    "build_strong_observation_lr_input",
    "validate_strong_observation_lr_artifact",
    "validate_strong_observation_lr_runtime_artifact",
]
