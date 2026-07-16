from __future__ import annotations

import pytest

from production_probability_successor_activation_contract import (
    BAYES_FORMULA_ID,
    CHECKPOINT_SCHEMA,
    COMPOSER_SCHEMA,
    LATENT_TYPE,
    PROJECTION_SCHEMA,
    PUBLIC_SSOT_SCHEMA,
    SCHEMA_VERSION,
    STRONG_UPDATE_SCHEMA,
    SuccessorActivationError,
    TARGET_TYPE,
    validate_successor_activation,
)
from production_probability_successor_runtime_manifest import (
    SUCCESSOR_EXCLUDED_LEGACY_FILES,
    SUCCESSOR_FILES,
    audit_successor_runtime_manifest_dependency_closure,
)


def _contract() -> dict:
    return {
        "schema": SCHEMA_VERSION,
        "state": "production_enabled",
        "activation_effective_at": "2026-07-15T02:00:00Z",
        "cutover_approval_id": "ADR-0002-reported-x-successor",
        "bayes_formula_id": BAYES_FORMULA_ID,
        "composer_schema": COMPOSER_SCHEMA,
        "checkpoint_schema": CHECKPOINT_SCHEMA,
        "strong_update_schema": STRONG_UPDATE_SCHEMA,
        "public_ssot_schema": PUBLIC_SSOT_SCHEMA,
        "projection_schema": PROJECTION_SCHEMA,
        "latent_type": LATENT_TYPE,
        "target_type": TARGET_TYPE,
        "production_transaction_b_authorized": True,
        "unique_probability_mainline": True,
        "legacy_probability_writer_disabled": True,
        "no_order_validation_accepted": True,
        "speci_soft_evidence_only": True,
        "regular_metar_only_x_hard_bound": True,
        "bridge_consumes_reported_x_only": True,
        "public_ssot_exposes_reported_x_only": True,
        "prior_model_artifact_sha256": "a" * 64,
        "weak_likelihood_artifact_sha256": "b" * 64,
        "strong_lr_artifact_sha256": "c" * 64,
        "runtime_manifest_sha256": "d" * 64,
        "legacy_writer_retirement_receipt_sha256": "e" * 64,
        "places_orders": False,
        "authorizes_clob": False,
    }


def test_successor_activation_freezes_target_and_projection_semantics() -> None:
    result = validate_successor_activation(_contract())
    assert result["checkpoint_schema"] == CHECKPOINT_SCHEMA
    assert result["target_type"] == TARGET_TYPE
    assert result["speci_soft_evidence_only"] is True
    assert len(result["activation_contract_sha256"]) == 64


def test_successor_activation_rejects_old_public_ssot_or_bridge_semantics() -> None:
    value = _contract()
    value["public_ssot_schema"] = "sequential_temperature_posterior_ssot_v2"
    value["bridge_consumes_reported_x_only"] = False
    with pytest.raises(SuccessorActivationError):
        validate_successor_activation(value)


def test_successor_manifest_excludes_retired_weak_writer_epoch() -> None:
    retired = {
        "production_weak_forecast_revision_writer.py",
        "production_weak_forecast_revision_event_runtime.py",
    }
    assert retired == set(SUCCESSOR_EXCLUDED_LEGACY_FILES)
    assert retired.isdisjoint(SUCCESSOR_FILES)
    assert "taf_revision_candidate_selection_runtime.py" in SUCCESSOR_FILES


def test_successor_manifest_binds_complete_local_dependency_closure() -> None:
    result = audit_successor_runtime_manifest_dependency_closure()
    assert result["ok"] is True, result
    assert result["missing_runtime_files"] == []
    assert result["retired_legacy_files_reachable"] == []
