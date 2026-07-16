from __future__ import annotations

from production_probability_successor_activation_contract import (
    BAYES_FORMULA_ID,
    CHECKPOINT_SCHEMA,
    COMPOSER_SCHEMA,
    LATENT_TYPE,
    PROJECTION_SCHEMA,
    PUBLIC_SSOT_SCHEMA,
    SCHEMA_VERSION as ACTIVATION_SCHEMA,
    STRONG_UPDATE_SCHEMA,
    TARGET_TYPE,
)
from production_probability_successor_runtime_manifest import (
    successor_runtime_manifest_sha256,
)
from production_reported_x_initial_checkpoint import (
    compile_reported_x_initial_checkpoint,
)
from tests.test_initial_posterior_checkpoint_shadow import (
    AS_OF,
    TARGET,
    _resolution_contract,
)
from tests.test_production_initial_posterior import _production_artifacts


def _activation() -> dict:
    prior, weak, strong = _production_artifacts()
    return {
        "schema": ACTIVATION_SCHEMA,
        "state": "production_enabled",
        "activation_effective_at": AS_OF,
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
        "prior_model_artifact_sha256": prior["prior_model_artifact_sha256"],
        "weak_likelihood_artifact_sha256": weak["artifact_sha256"],
        "strong_lr_artifact_sha256": strong["model_artifact_sha256"],
        "runtime_manifest_sha256": successor_runtime_manifest_sha256(),
        "legacy_writer_retirement_receipt_sha256": "e" * 64,
        "places_orders": False,
        "authorizes_clob": False,
    }


def test_reported_x_p0_compiler_emits_integer_public_target_surface() -> None:
    prior, weak, strong = _production_artifacts()
    result = compile_reported_x_initial_checkpoint(
        activation=_activation(),
        target_identity=TARGET,
        event_date="2026-07-03",
        station_id="ZUUU",
        decision_as_of=AS_OF,
        prior_context=prior,
        joint_weak_likelihood=weak,
        active_strong_lr_artifact=strong,
        resolution_contract=_resolution_contract(closed=False),
        source_reliability_initial_state={"AMSC": {"state": "unknown"}},
    )
    assert result["ok"] is True
    assert result["compiler_state"] == "candidate_ready"
    checkpoint = result["checkpoint"]
    assert checkpoint["schema"] == CHECKPOINT_SCHEMA
    assert checkpoint["reported_x_lower_bound_c"] is None
    assert all(float(key).is_integer() for key in checkpoint["reported_physical_x_distribution"])
    assert "physical_high_distribution" not in checkpoint
    assert result["resolution_bridge"]["input_probability_field"] == (
        "reported_physical_x_distribution"
    )
    assert result["places_orders"] is False
