from __future__ import annotations

import json

import pytest

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
from production_reported_x_public_ssot_writer import (
    materialize_reported_x_initial_public_fact_row,
    materialize_reported_x_public_physical_fact_row,
)
from reported_x_successor_checkpoint_runtime import (
    materialize_successor_checkpoint_columns,
)


def _activation() -> dict:
    return {
        "schema": ACTIVATION_SCHEMA,
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
        "runtime_manifest_sha256": successor_runtime_manifest_sha256(),
        "legacy_writer_retirement_receipt_sha256": "e" * 64,
        "places_orders": False,
        "authorizes_clob": False,
    }


def _checkpoint() -> dict:
    joint = [
        {"temperature_c": 30.75, "state": "terminated", "probability": 0.7},
        {"temperature_c": 31.75, "state": "warming", "probability": 0.3},
    ]
    columns = materialize_successor_checkpoint_columns(
        {
            "posterior_checkpoint_id": "pchk-current",
            "joint_posterior_sha256": "f" * 64,
            "joint_posterior": joint,
            "observed_max_lower_bound_c": 30.5,
        },
        canonical_events=[
            {
                "canonical_observation_event_id": "event-1",
                "report_type": "METAR",
                "valid_time": "2026-07-15T01:00:00Z",
                "parsed_observation": {"temperature_c": 31.0},
            }
        ],
    )["columns"]
    return {
        "posterior_checkpoint_id": "pchk-current",
        "posterior_run_id": "run-current",
        "resolution_contract_id": "resolution-1",
        "joint_posterior_sha256": "f" * 64,
        "last_canonical_event_id": "event-1",
        "last_strong_update_id": "update-1",
        "committed_at": "2026-07-15T01:00:01Z",
        **columns,
    }


def _update() -> dict:
    return {
        "path_auxiliary_json": json.dumps(
            {
                "termination_risk": {
                    "schema": "termination_risk_marginal_v2_reported_x",
                    "state": "available",
                    "updates_posterior": False,
                    "canonical_observation_event_id": "event-1",
                    "probability_no_material_new_daily_high": 0.7,
                    "probability_material_new_daily_high": 0.3,
                },
                "peak_timing_state": {
                    "schema": "peak_timing_state_l3_v1",
                    "ok": True,
                    "state": "na",
                    "canonical_observation_event_id": "event-1",
                    "strong_observation_update_id": "update-1",
                },
                "strong_likelihood_update_identity": {
                    "schema": "strong_likelihood_cumulative_replacement_identity_v1",
                    "likelihood_update_kind": "cumulative_evidence_replacement_delta_log_lr_v1",
                    "current_cumulative_raw_log_score_sha256": "a" * 64,
                    "previous_cumulative_raw_log_score_sha256": "",
                    "conditional_innovation_log_score_sha256": "b" * 64,
                    "previous_checkpoint_id": "pchk-previous",
                },
            }
        )
    }


def test_public_fact_exposes_integer_x_and_never_latent_z() -> None:
    result = materialize_reported_x_public_physical_fact_row(
        activation=_activation(),
        strong_update_row=_update(),
        checkpoint_row=_checkpoint(),
        fact_snapshot_id="fact-1",
        slug="chengdu-high-2026-07-15",
        event_date="2026-07-15",
        fact_as_of="2026-07-15T01:00:01Z",
        correlation_id="corr-1",
        market_bucket_probabilities_na_reason="bridge_open",
    )
    ssot = json.loads(result["fact_json"])["posterior_ssot_fact"]
    assert ssot["schema"] == PUBLIC_SSOT_SCHEMA
    assert ssot["reported_physical_x_distribution"] == {"31": 0.7, "32": 0.3}
    assert ssot["reported_x_lower_bound_c"] == 31
    assert ssot["latent_probability_public"] is False
    assert "physical_high_distribution" not in ssot
    assert "latent_scheduled_slot_high_distribution" not in ssot
    assert ssot["strong_likelihood_update_identity"]["likelihood_update_kind"] == "cumulative_evidence_replacement_delta_log_lr_v1"


def test_public_fact_rejects_old_termination_schema() -> None:
    update = _update()
    auxiliary = json.loads(update["path_auxiliary_json"])
    auxiliary["termination_risk"]["schema"] = "termination_risk_marginal_v1"
    update["path_auxiliary_json"] = json.dumps(auxiliary)
    with pytest.raises(Exception, match="termination_contract_invalid"):
        materialize_reported_x_public_physical_fact_row(
            activation=_activation(),
            strong_update_row=update,
            checkpoint_row=_checkpoint(),
            fact_snapshot_id="fact-1",
            slug="chengdu-high-2026-07-15",
            event_date="2026-07-15",
            fact_as_of="2026-07-15T01:00:01Z",
            correlation_id="corr-1",
            market_bucket_probabilities_na_reason="bridge_open",
        )


def test_initial_public_fact_is_reported_x_only_and_has_no_observed_bound() -> None:
    checkpoint = _checkpoint()
    # P0 has no consumed METAR and therefore no hard bound or available
    # termination marginal.
    p0_joint = [
        {"temperature_c": 30.75, "state": "neutral", "probability": 0.7},
        {"temperature_c": 31.75, "state": "neutral", "probability": 0.3},
    ]
    columns = materialize_successor_checkpoint_columns(
        {
            "posterior_checkpoint_id": "pchk-p0",
            "joint_posterior_sha256": "f" * 64,
            "joint_posterior": p0_joint,
            "observed_max_lower_bound_c": None,
        },
        canonical_events=[],
    )["columns"]
    checkpoint.update(
        {
            "posterior_checkpoint_id": "pchk-p0",
            "posterior_run_id": "run-p0",
            "last_canonical_event_id": "",
            "last_strong_update_id": "",
            **columns,
        }
    )
    result = materialize_reported_x_initial_public_fact_row(
        activation=_activation(),
        checkpoint_row=checkpoint,
        resolution_bridge={
            "ok": True,
            "state": "physical_only_market_projection_na",
            "bridge_closed": False,
            "input_probability_field": "reported_physical_x_distribution",
            "market_bucket_probabilities_na_reason": "bridge_open",
        },
        fact_snapshot_id="fact-p0",
        slug="chengdu-high-2026-07-15",
        event_date="2026-07-15",
        fact_as_of="2026-07-15T00:00:00Z",
        correlation_id="corr-p0",
    )
    ssot = json.loads(result["fact_json"])["posterior_ssot_fact"]
    assert ssot["reported_x_lower_bound_c"] is None
    assert ssot["reported_x_termination"]["state"] == "na"
    assert "physical_high_distribution" not in ssot
