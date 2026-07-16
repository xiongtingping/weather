"""Fail-closed identity contract for the production v6 successor epoch."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping


SCHEMA_VERSION = "production_probability_v6_activation_v1"
FORMULA_ID = (
    "p0_weak_conditioned_continuous_y_x_8_state_then_pre_event_controlled_predict_"
    "then_canonical_joint_emission_then_regular_metar_hard_bound_then_reported_x_projection_v1"
)
COMPOSER_SCHEMA = "physical_state_bayes_v6_pre_event_predict_update"
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
        "bayes_formula_id": FORMULA_ID,
        "composer_schema": COMPOSER_SCHEMA,
        "checkpoint_schema": CHECKPOINT_SCHEMA,
        "strong_update_schema": STRONG_UPDATE_SCHEMA,
        "public_ssot_schema": PUBLIC_SSOT_SCHEMA,
    }
    errors = [
        f"activation_identity_mismatch:{field}"
        for field, expected_value in expected.items()
        if _text(activation.get(field)) != expected_value
    ]
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
    if activation.get("nwp_hourly_curve_is_transition_control_only") is not True:
        errors.append("nwp_duplicate_likelihood_not_forbidden")
    if activation.get("production_transaction_b_authorized") is not True:
        errors.append("production_transaction_b_not_authorized")
    if activation.get("places_orders") is not False or activation.get("authorizes_clob") is not False:
        errors.append("probability_activation_must_not_authorize_clob")
    for field in (
        "runtime_manifest_sha256",
        "transition_artifact_sha256",
        "observation_artifact_sha256",
        "prior_model_artifact_sha256",
        "weak_likelihood_artifact_sha256",
    ):
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
    "FORMULA_ID",
    "PUBLIC_SSOT_SCHEMA",
    "SCHEMA_VERSION",
    "STRONG_UPDATE_SCHEMA",
    "activation_sha256",
    "validate_v6_activation",
]
