import hashlib
import json

from weak_forecast_revision_transaction_runtime import compile_weak_forecast_revision_candidate


def _sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def test_revision_candidate_replaces_weak_surface_and_preserves_strong_lineage():
    joint = [
        {"temperature_c": 33.0, "state": "warming", "probability": 0.2},
        {"temperature_c": 33.0, "state": "terminated", "probability": 0.2},
        {"temperature_c": 34.0, "state": "warming", "probability": 0.3},
        {"temperature_c": 34.0, "state": "terminated", "probability": 0.3},
    ]
    previous = {
        "posterior_checkpoint_id": "pchk-old",
        "target_identity": "physical_weather_day_v1:ZUUU:2026-07-15",
        "posterior_run_id": "post-old",
        "checkpoint_kind": "incremental",
        "forecast_evidence_snapshot_id": "forev-old",
        "prior_context_id": "prior-1",
        "resolution_contract_id": "resolution-1",
        "last_strong_update_id": "strong-1",
        "last_canonical_event_id": "canobs-1",
        "canonical_sequence_sha256": "c" * 64,
        "temperature_grid_id": "t" * 64,
        "state_grid_id": "s" * 64,
        "joint_posterior_json": json.dumps(joint),
        "joint_posterior_sha256": _sha(joint),
        "physical_high_distribution_json": json.dumps({"33": 0.4, "34": 0.6}),
        "settlement_scalar_distribution_json": "[]",
        "market_bucket_distribution_json": "{}",
        "latent_post_state_json": json.dumps({"warming": 0.5, "terminated": 0.5}),
        "trajectory_feature_state_json": "{}",
        "source_reliability_state_json": "{}",
        "observed_max_lower_bound_c": 32.0,
        "consumed_event_ids_json": json.dumps(["canobs-1"]),
        "consumed_ancestor_sha256": "a" * 64,
        "model_artifact_sha256": "m" * 64,
        "replay_generation": 0,
        "supersedes_checkpoint_id": "",
        "state": "committed",
        "created_at": "2026-07-15T00:00:00Z",
        "committed_at": "2026-07-15T00:00:00Z",
    }
    old = {
        "forecast_evidence_snapshot_id": "forev-old",
        "target_identity": previous["target_identity"],
        "event_date": "2026-07-15",
        "weak_likelihood_artifact_sha256": "w" * 64,
        "weak_likelihood_sha256": "1" * 64,
        "likelihood_by_x_json": json.dumps({"33": 1.0, "34": 1.0}),
    }
    new = {
        **old,
        "forecast_evidence_snapshot_id": "forev-new",
        "weak_likelihood_sha256": "2" * 64,
        "likelihood_by_x_json": json.dumps({"33": 2.0, "34": 0.5}),
        "decision_as_of": "2026-07-15T00:30:01Z",
    }
    result = compile_weak_forecast_revision_candidate(
        previous_checkpoint_row=previous,
        previous_snapshot_row=old,
        revised_snapshot_row=new,
        trigger_source_id="zuuu_taf_forecast_amsc",
        trigger_source_revision_id="tafrev-2",
        revision_kind="amendment",
        public_slug="physical-high-zuuu-2026-07-15",
        decision_as_of="2026-07-15T00:30:01Z",
        replay_generation=0,
    )
    checkpoint = result["checkpoint_row"]
    distribution = json.loads(checkpoint["physical_high_distribution_json"])
    assert distribution["33"] > distribution["34"]
    assert checkpoint["last_strong_update_id"] == "strong-1"
    assert checkpoint["last_canonical_event_id"] == "canobs-1"
    assert checkpoint["forecast_evidence_snapshot_id"] == "forev-new"
    assert result["update_row"]["revision_kind"] == "amendment"
    assert result["places_orders"] is False
