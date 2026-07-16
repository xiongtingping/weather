from __future__ import annotations

import sqlite3

from initial_posterior_checkpoint_shadow import (
    INITIAL_LINEAGE_TABLE,
    build_initial_posterior_checkpoint,
    persist_initial_posterior_checkpoint_shadow,
)
from p10_shadow_runtime_schema import initialize_p10_shadow_runtime_schema
from sequential_bayes_public_ssot_shadow_writer import initialize_isolated_public_ssot_shadow_schema
from sequential_bayes_shadow_pipeline import run_canonical_event_public_ssot_shadow_update
from tests.test_sequential_bayes_shadow_pipeline import (
    NOW,
    TARGET,
    _artifact,
    _canonical_event_id,
    _realization_policy,
    _resolution_contract,
)
from strong_observation_lr_runtime_registry import (
    activate_strong_lr_runtime_artifact,
    register_strong_lr_runtime_artifact,
)
from tests.strong_observation_artifact_fixtures import strong_activation_attestation


def _p0() -> dict:
    artifact = _artifact()
    return build_initial_posterior_checkpoint(
        target_identity=TARGET,
        event_date="2026-07-03",
        station_id="ZUUU",
        decision_as_of="2026-07-03T06:00:00Z",
        prior_context={
            "prior_context_id": "prior-current",
            "artifact_sha256": "prior-artifact-current",
            "continuous_distribution": {
                "schema": "continuous_temperature_climate_prior_v1",
                "grid": [{"temp_c": 29.0, "mass": 0.5}, {"temp_c": 30.0, "mass": 0.5}],
            },
            "latent_state_prior_by_temperature": {
                "29": {"warming": 1.0},
                "30": {"warming": 1.0},
            },
        },
        joint_weak_likelihood={
            "schema": "joint_forecast_weak_likelihood_v1",
            "ok": True,
            "state": "available",
            "artifact_sha256": "weak-artifact-current",
            "forecast_evidence_snapshot_id": "forecast-current",
            "market_data_consumed": False,
            "observation_data_consumed": False,
            "temperature_grid_c": [29.0, 30.0],
            "joint_forecast_weak_likelihood": [
                {"temperature_c": 29.0, "likelihood": 0.5},
                {"temperature_c": 30.0, "likelihood": 0.5},
            ],
        },
        active_strong_lr_artifact=artifact,
        resolution_contract=_resolution_contract(),
        source_reliability_initial_state={"zuuu_metar_raw_amsc": {"state": "unknown"}},
    )


def test_p0_is_the_only_p8_predecessor_and_first_strong_update_commits_full_public_shadow_ssot() -> None:
    conn = sqlite3.connect(":memory:")
    initialize_isolated_public_ssot_shadow_schema(conn)
    initialize_p10_shadow_runtime_schema(conn)
    artifact = _artifact()
    register_strong_lr_runtime_artifact(
        conn,
        artifact=artifact,
        registered_at="2026-07-03T05:59:00Z",
        registered_by="pytest",
        registration_basis="test_runtime_baseline",
        registration_ref="pytest://p0-p8",
    )
    activate_strong_lr_runtime_artifact(
        conn,
        target_identity=TARGET,
        artifact_sha256=artifact["model_artifact_sha256"],
        activated_at="2026-07-03T05:59:00Z",
        activated_by="pytest",
        activation_basis="approved_runtime_baseline",
        activation_ref="pytest://p0-p8-approval",
        activation_evidence=strong_activation_attestation(
            target_identity=TARGET,
            artifact_sha256=artifact["model_artifact_sha256"],
            activation_basis="approved_runtime_baseline",
            activation_ref="pytest://p0-p8-approval",
        ),
    )
    p0 = _p0()
    assert p0["ok"] is True

    p0_persisted = persist_initial_posterior_checkpoint_shadow(conn, initial_checkpoint=p0)
    assert p0_persisted["ok"] is True
    assert conn.execute("SELECT COUNT(*) FROM posterior_checkpoints").fetchone()[0] == 1
    assert conn.execute(f"SELECT COUNT(*) FROM {INITIAL_LINEAGE_TABLE}").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM event_fact_history").fetchone()[0] == 0

    event_id = _canonical_event_id(conn)
    result = run_canonical_event_public_ssot_shadow_update(
        conn,
        public_slug=TARGET,
        public_event_date="2026-07-03",
        public_fact_as_of=NOW,
        shadow_only=True,
        previous_checkpoint=p0["checkpoint"],
        canonical_observation_event_id=event_id,
        forecast_evidence_snapshot_id="forecast-current",
        prior_context_id="prior-current",
        approved_lr_artifact=_artifact(),
        resolution_contract=_resolution_contract(),
        realization_policy=_realization_policy(),
        temperature_quantization_c=1.0,
        fact_snapshot_id="factsnap_p0_p8_123456",
        correlation_id="corr_p0_p8_123456",
        outbox_event_id="outbox_p0_p8_123456",
        decision_as_of=NOW,
        deadline_at="2026-07-03T07:00:03Z",
        started_at="2026-07-03T06:59:59Z",
        completed_at=NOW,
        source_reliability_before={"zuuu_metar_raw_amsc": {"state": "unknown"}},
        source_reliability_after={"zuuu_metar_raw_amsc": {"state": "unknown"}},
        path_auxiliary={"shadow_only": True},
    )

    assert result["ok"] is True
    assert result["state"] == "public_ssot_shadow_committed"
    assert result["strong_observation_update"]["previous_checkpoint_id"] == p0["checkpoint"]["posterior_checkpoint_id"]
    assert result["checkpoint"]["previous_checkpoint_id"] == p0["checkpoint"]["posterior_checkpoint_id"]
    assert result["checkpoint"]["consumed_event_ids"] == [event_id]
    assert conn.execute("SELECT COUNT(*) FROM strong_observation_updates").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM posterior_checkpoints").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM realization_states").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM posterior_commit_outbox").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM event_fact_history").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM event_fact_latest").fetchone()[0] == 1
    assert result["public_ssot_shadow_only"] is True
