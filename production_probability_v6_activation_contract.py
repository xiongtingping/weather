"""Fail-closed identity contract for the production v6 successor epoch."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping


SCHEMA_VERSION = "production_probability_v6_activation_v1"
FORMULA_ID = (
    "p0_weak_conditioned_continuous_y_x_8_state_then_pre_event_controlled_predict_"
    "then_quantized_robust_time_conditioned_all_weather_forecast_regime_conditioned_"
    "canonical_joint_emission_then_regular_metar_hard_bound_then_reported_x_projection_v4"
)
LEGACY_CORRELATION_GUARDED_FORMULA_ID = (
    "p0_weak_conditioned_continuous_y_x_8_state_then_pre_event_controlled_predict_"
    "then_quantized_robust_time_conditioned_all_weather_forecast_regime_conditioned_"
    "canonical_joint_emission_then_physical_event_cluster_budget_tempering_then_"
    "candidate_conditional_y_recentering_then_monotone_high_temperature_penalty_"
    "then_regular_metar_hard_bound_then_reported_x_projection_v6"
)
CORRELATION_GUARDED_FORMULA_ID = (
    "p0_weak_conditioned_continuous_y_x_8_state_x_source_mode_scenario_joint_then_"
    "pre_event_scenario_controlled_predict_then_quantized_robust_time_conditioned_"
    "all_weather_regime_joint_emission_then_physical_event_cluster_budget_tempering_"
    "then_monotone_candidate_penalty_then_regular_metar_hard_bound_then_persistent_"
    "scenario_responsibility_checkpoint_then_reported_x_projection_v7"
)
IDENTIFIABLE_PHYSICAL_FORMULA_ID = (
    "station_basin_season_hierarchical_prior_then_weak_multiscenario_physical_driver_"
    "then_pre_event_identifiable_T_q_Gnet_W_Ccp_R_predict_then_single_current_canonical_"
    "joint_emission_then_quantized_temperature_condition_then_contract_reported_x_hard_"
    "bound_then_frozen_slot_encoding_qc_missing_operator_then_reported_x_projection_v1"
)
COMPOSER_SCHEMA = "physical_state_bayes_v6_pre_event_predict_update"
IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA = (
    "identifiable_physical_state_bayes_v1_pre_event_predict_update"
)
CHECKPOINT_SCHEMA = "posterior_checkpoint_v4_physical_state_bayes_v6"
STRONG_UPDATE_SCHEMA = "strong_observation_update_v6_pre_event_predict_emission"
PUBLIC_SSOT_SCHEMA = "sequential_temperature_posterior_ssot_v4_reported_x"


def _text(value: Any) -> str:
    return str(value or "").strip()


def activation_sha256(value: Mapping[str, Any]) -> str:
    payload = {key: item for key, item in dict(value).items() if key != "activation_contract_sha256"}
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def validate_v6_activation(value: Mapping[str, Any]) -> dict[str, Any]:
    activation = dict(value)
    expected = {
        "schema": SCHEMA_VERSION,
        "state": "production_enabled",
        "checkpoint_schema": CHECKPOINT_SCHEMA,
        "strong_update_schema": STRONG_UPDATE_SCHEMA,
        "public_ssot_schema": PUBLIC_SSOT_SCHEMA,
    }
    errors = [
        f"activation_identity_mismatch:{field}"
        for field, expected_value in expected.items()
        if _text(activation.get(field)) != expected_value
    ]
    composer = _text(activation.get("composer_schema"))
    if composer not in {COMPOSER_SCHEMA, IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA}:
        errors.append("activation_identity_mismatch:composer_schema")
    if composer == IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA:
        if _text(activation.get("bayes_formula_id")) != IDENTIFIABLE_PHYSICAL_FORMULA_ID:
            errors.append("activation_identity_mismatch:bayes_formula_id")
        if activation.get("physical_state_vector") != [
            "latent_temperature",
            "specific_humidity",
            "net_temperature_tendency",
            "surface_wetness_memory",
            "cold_pool_memory",
            "regime_state",
        ]:
            errors.append("activation_physical_state_vector_invalid")
        if activation.get("frozen_reporting_operator_enabled") is not True:
            errors.append("activation_reporting_operator_not_frozen")
        if int(activation.get("current_event_joint_consumption_count") or 0) != 1:
            errors.append("activation_current_event_consumption_count_invalid")
    elif _text(activation.get("bayes_formula_id")) not in {
        FORMULA_ID,
        CORRELATION_GUARDED_FORMULA_ID,
        LEGACY_CORRELATION_GUARDED_FORMULA_ID,
    }:
        errors.append("activation_identity_mismatch:bayes_formula_id")
    if activation.get("unique_probability_mainline") is not True:
        errors.append("activation_not_unique_probability_mainline")
    if activation.get("legacy_probability_writer_disabled") is not True:
        errors.append("legacy_probability_writer_not_disabled")
    if activation.get("v5_fallback_forbidden") is not True:
        errors.append("v5_fallback_not_forbidden")
    if activation.get("pre_event_predict_does_not_consume_current_observation") is not True:
        errors.append("pre_event_evidence_boundary_not_frozen")
    if activation.get("no_post_emission_state_transition") is not True:
        errors.append("post_emission_transition_not_retired")
    if activation.get("nwp_hourly_curve_not_reapplied_as_independent_likelihood") is not True:
        errors.append("nwp_duplicate_likelihood_not_forbidden")
    if activation.get("production_transaction_b_authorized") is not True:
        errors.append("production_transaction_b_not_authorized")
    if activation.get("places_orders") is not False or activation.get("authorizes_clob") is not False:
        errors.append("probability_activation_must_not_authorize_clob")
    sha_fields = [
        "runtime_manifest_sha256",
        "prior_model_artifact_sha256",
        "weak_likelihood_artifact_sha256",
    ]
    sha_fields.extend(
        [
            "physical_state_model_artifact_sha256",
            "physical_observation_artifact_sha256",
            "physical_reporting_operator_sha256",
        ]
        if composer == IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA
        else ["transition_artifact_sha256", "observation_artifact_sha256"]
    )
    for field in sha_fields:
        if len(_text(activation.get(field))) != 64:
            errors.append(f"activation_sha256_invalid:{field}")
    declared = _text(activation.get("activation_contract_sha256"))
    computed = activation_sha256(activation)
    if declared and declared != computed:
        errors.append("activation_contract_sha256_mismatch")
    if errors:
        raise ValueError(";".join(errors))
    activation["activation_contract_sha256"] = computed
    return activation


__all__ = [
    "CHECKPOINT_SCHEMA",
    "COMPOSER_SCHEMA",
    "CORRELATION_GUARDED_FORMULA_ID",
    "FORMULA_ID",
    "IDENTIFIABLE_PHYSICAL_COMPOSER_SCHEMA",
    "IDENTIFIABLE_PHYSICAL_FORMULA_ID",
    "LEGACY_CORRELATION_GUARDED_FORMULA_ID",
    "PUBLIC_SSOT_SCHEMA",
    "SCHEMA_VERSION",
    "STRONG_UPDATE_SCHEMA",
    "activation_sha256",
    "validate_v6_activation",
]
