import json

from production_probability_v6_activation_contract import FORMULA_ID
from production_probability_v6_market_bundle_runtime import (
    build_v6_market_transaction_bundle,
)
from tests.test_production_probability_v6_strong_physical_projection_runtime import (
    _base,
)
from tests.test_reported_x_market_realization_runtime import _artifact, _policy, _target


def test_v6_market_bundle_projects_b_and_creates_orderless_atomic_handoff_rows():
    event, update, checkpoint, policy = _base(
        termination=0.2,
        latent_terminal=0.1,
        path="sustained_warming",
    )
    event.update(
        {
            "first_actionable_seen_at": "2025-04-30T08:00:00Z",
            "valid_time": "2025-04-30T08:00:00Z",
        }
    )
    update.update(
        {
            "forecast_evidence_snapshot_id": "forecast-1",
            "prior_context_id": "prior-1",
            "previous_checkpoint_id": "checkpoint-0",
            "previous_posterior_run_id": "posterior-0",
            "previous_posterior_sha256": "0" * 64,
            "completed_at": "2025-04-30T08:00:01Z",
        }
    )
    auxiliary = json.loads(update["path_auxiliary_json"])
    auxiliary["termination_risk"].update(
        {
            "schema": "termination_risk_marginal_v3_physical_state_bayes_v6",
            "state": "available",
        }
    )
    auxiliary["peak_timing_state"] = {
        "schema": "peak_timing_state_l3_v1",
        "ok": True,
        "state": "na",
        "na_reason": "test",
        "canonical_observation_event_id": "event-1",
        "strong_observation_update_id": "update-1",
        "entry_peak_at": "",
        "does_not_update_physical_x": True,
    }
    update["path_auxiliary_json"] = json.dumps(auxiliary)
    checkpoint.update(
        {
            "posterior_checkpoint_id": "checkpoint-1",
            "posterior_run_id": "posterior-1",
            "previous_checkpoint_id": "checkpoint-0",
            "target_identity": "physical_weather_day_v1:ZUUU:2025-04-30",
            "forecast_evidence_snapshot_id": "forecast-1",
            "prior_context_id": "prior-1",
            "resolution_contract_id": "physical-resolution",
            "model_artifact_sha256": "m" * 64,
            "joint_posterior_sha256": "j" * 64,
            "committed_at": "2025-04-30T08:00:01Z",
            "reported_physical_x_distribution_json": json.dumps(
                {"30": 0.2, "31": 0.8}
            ),
            "checkpoint_probability_schema": "posterior_checkpoint_v4_physical_state_bayes_v6",
            "state_space_schema": "physical_state_bayes_v6_production_successor_v1",
            "probability_target_type": "amsc_scheduled_regular_metar_reported_daily_max_c",
            "duration_clocks_json": "{}",
            "reported_x_lower_bound_c": 30,
        }
    )
    previous = {
        **checkpoint,
        "posterior_checkpoint_id": "checkpoint-0",
        "posterior_run_id": "posterior-0",
        "previous_checkpoint_id": "",
        "last_canonical_event_id": "event-0",
        "last_strong_update_id": "update-0",
        "reported_physical_x_distribution_json": json.dumps(
            {"30": 0.5, "31": 0.5}
        ),
    }
    candidate = {
        "ok": True,
        "candidate_mode": "production_successor",
        "shadow_only": False,
        "strong_update_row": update,
        "checkpoint_row": checkpoint,
    }
    activation = {
        "schema": "production_probability_v6_activation_v1",
        "state": "production_enabled",
        "bayes_formula_id": FORMULA_ID,
        "composer_schema": "physical_state_bayes_v6_pre_event_predict_update",
        "checkpoint_schema": "posterior_checkpoint_v4_physical_state_bayes_v6",
        "strong_update_schema": "strong_observation_update_v6_pre_event_predict_emission",
        "public_ssot_schema": "sequential_temperature_posterior_ssot_v4_reported_x",
        "unique_probability_mainline": True,
        "legacy_probability_writer_disabled": True,
        "v5_fallback_forbidden": True,
        "pre_event_predict_does_not_consume_current_observation": True,
        "no_post_emission_state_transition": True,
        "nwp_hourly_curve_not_reapplied_as_independent_likelihood": True,
        "runtime_manifest_sha256": "r" * 64,
        "transition_artifact_sha256": "t" * 64,
        "observation_artifact_sha256": "a" * 64,
        "prior_model_artifact_sha256": "p" * 64,
        "weak_likelihood_artifact_sha256": "w" * 64,
        "production_transaction_b_authorized": True,
        "places_orders": False,
        "authorizes_clob": False,
        "activation_requires_atomic_install": True,
        "supersedes_probability_epoch_schema_on_cutover": "v6",
    }
    from production_probability_v6_activation_contract import activation_sha256

    activation["activation_contract_sha256"] = activation_sha256(activation)
    checkpoint["model_artifact_sha256"] = activation["activation_contract_sha256"]
    update["model_artifact_sha256"] = activation["activation_contract_sha256"]
    result = build_v6_market_transaction_bundle(
        activation=activation,
        previous_checkpoint=previous,
        candidate=candidate,
        physical_resolution_contract={
            "resolution_contract_id": "physical-resolution",
            "authority_contract_id": "zuuu_amsc_wunderground_daily_high_v4",
            "authority_contract_sha256": "a" * 64,
            "resolution_source_id": "wunderground_zuuu_historical_observation_daily_high_final_v3",
            "resolution_local_date": "2025-04-30",
            "settlement_unit": "C",
            "bridge_closed": False,
            "validated": False,
        },
        market_target_contract=_target(),
        bridge_artifact=_artifact(),
        canonical_observation_event=event,
        realization_policy=_policy(),
        fact_snapshot_id="fact-1",
        slug="chengdu-high-2025-04-30",
        event_date="2025-04-30",
        fact_as_of="2025-04-30T08:00:01Z",
        correlation_id="corr-1",
        outbox_event_id="outbox-1",
        inference_decision_deadline_at="2025-04-30T08:00:03Z",
    )

    assert result["ok"] is True
    assert result["places_orders"] is False
    assert result["outbox_row"]["state"] == "pending"
    ssot = json.loads(result["public_fact_row"]["fact_json"])[
        "posterior_ssot_fact"
    ]
    assert ssot["state"] == "available"
    assert ssot["reported_physical_x_distribution"] == {"30": 0.2, "31": 0.8}
    assert abs(sum(ssot["market_bucket_probabilities"].values()) - 1.0) < 1e-8
    assert ssot["current_report_bucket"] == ""
    assert result["reported_x_unchanged"] is True
