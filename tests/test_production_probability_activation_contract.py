from __future__ import annotations

import json

import pytest

from production_probability_activation_contract import (
    ProductionProbabilityActivationError,
    load_production_probability_activation,
    validate_production_probability_activation,
)


def _enabled() -> dict:
    return {"schema": "production_probability_runtime_activation_v2", "state": "production_enabled", "production_transaction_b_authorized": True, "unique_probability_mainline": True, "legacy_probability_writer_disabled": True, "no_order_validation_accepted": True, "cutover_approval_id": "ADR-0002-production", "activation_effective_at": "2026-07-14T00:00:00Z", "prior_model_artifact_sha256": "a" * 64, "weak_likelihood_artifact_sha256": "b" * 64, "strong_lr_artifact_sha256": "c" * 64, "runtime_manifest_sha256": "d" * 64, "legacy_writer_retirement_receipt_sha256": "e" * 64, "bayes_formula_id": "p0_prior_x_weak_once_then_canonical_event_raw_log_lr_then_weak_revision_replacement_log_lr_v1", "composer_schema": "sequential_bayes_filter_v3", "checkpoint_schema": "posterior_checkpoint_v2", "strong_update_schema": "strong_observation_update_v3", "public_ssot_schema": "sequential_temperature_posterior_ssot_v2", "places_orders": False, "authorizes_clob": False}


def test_enabled_contract_is_frozen_and_cannot_authorize_orders() -> None:
    result = validate_production_probability_activation(_enabled())
    assert result["activation_contract_sha256"]
    dangerous = _enabled(); dangerous["places_orders"] = True
    with pytest.raises(ProductionProbabilityActivationError, match="must_not_authorize_orders"):
        validate_production_probability_activation(dangerous)


def test_default_contract_is_disabled_and_cannot_enable_writer(tmp_path) -> None:
    path = tmp_path / "activation.json"
    path.write_text(json.dumps({"schema": "production_probability_runtime_activation_v2", "state": "disabled"}), encoding="utf-8")
    with pytest.raises(ProductionProbabilityActivationError, match="not_enabled"):
        load_production_probability_activation(path)
