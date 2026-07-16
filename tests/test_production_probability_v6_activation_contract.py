from __future__ import annotations

import pytest

from production_probability_v6_activation_contract import (
    CHECKPOINT_SCHEMA,
    COMPOSER_SCHEMA,
    FORMULA_ID,
    PUBLIC_SSOT_SCHEMA,
    SCHEMA_VERSION,
    STRONG_UPDATE_SCHEMA,
    activation_sha256,
    validate_v6_activation,
)


def _activation() -> dict[str, object]:
    value = {
        "schema": SCHEMA_VERSION,
        "state": "production_enabled",
        "bayes_formula_id": FORMULA_ID,
        "composer_schema": COMPOSER_SCHEMA,
        "checkpoint_schema": CHECKPOINT_SCHEMA,
        "strong_update_schema": STRONG_UPDATE_SCHEMA,
        "public_ssot_schema": PUBLIC_SSOT_SCHEMA,
        "unique_probability_mainline": True,
        "legacy_probability_writer_disabled": True,
        "v5_fallback_forbidden": True,
        "pre_event_predict_does_not_consume_current_observation": True,
        "no_post_emission_state_transition": True,
        "nwp_hourly_curve_is_transition_control_only": True,
        "runtime_manifest_sha256": "b" * 64,
        "transition_artifact_sha256": "c" * 64,
        "observation_artifact_sha256": "d" * 64,
        "prior_model_artifact_sha256": "f" * 64,
        "weak_likelihood_artifact_sha256": "e" * 64,
        "production_transaction_b_authorized": True,
        "places_orders": False,
        "authorizes_clob": False,
    }
    value["activation_contract_sha256"] = activation_sha256(value)
    return value


def test_v6_activation_freezes_the_new_epoch_identity() -> None:
    assert validate_v6_activation(_activation())["composer_schema"] == COMPOSER_SCHEMA


def test_v6_activation_forbids_v5_fallback() -> None:
    value = _activation()
    value["v5_fallback_forbidden"] = False
    value["activation_contract_sha256"] = activation_sha256(value)
    with pytest.raises(ValueError, match="v5_fallback_not_forbidden"):
        validate_v6_activation(value)


def test_v6_activation_forbids_post_emission_transition() -> None:
    value = _activation()
    value["no_post_emission_state_transition"] = False
    value["activation_contract_sha256"] = activation_sha256(value)
    with pytest.raises(ValueError, match="post_emission_transition_not_retired"):
        validate_v6_activation(value)
