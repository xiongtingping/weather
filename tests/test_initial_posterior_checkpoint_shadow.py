from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from initial_posterior_checkpoint_shadow import (
    INITIAL_LINEAGE_TABLE,
    build_initial_posterior_checkpoint,
    persist_initial_posterior_checkpoint_shadow,
)
from p10_shadow_runtime_schema import initialize_p10_shadow_runtime_schema
from sequential_bayes_public_ssot_shadow_writer import initialize_isolated_public_ssot_shadow_schema
from sequential_bayes_shadow_writer import ShadowTransactionIntegrityError
from tests.strong_observation_artifact_fixtures import (
    approved_strong_artifact,
    strong_activation_attestation,
)
from strong_observation_lr_runtime_registry import (
    activate_strong_lr_runtime_artifact,
    register_strong_lr_runtime_artifact,
)


TARGET = "zuuu-2026-07-03"
AS_OF = "2026-07-03T06:30:00Z"


def _prior() -> dict:
    return {
        "prior_context_id": "prior-context-immutable-1",
        "artifact_sha256": "prior-artifact-immutable-1",
        "continuous_distribution": {
            "schema": "continuous_temperature_climate_prior_v1",
            "grid": [
                {"temp_c": 29.0, "mass": 0.40},
                {"temp_c": 30.0, "mass": 0.60},
            ],
        },
        "latent_state_prior_by_temperature": {
            "29": {"warming": 0.75, "cooling": 0.25},
            "30": {"warming": 0.25, "cooling": 0.75},
        },
    }


def _weak(*, likelihood_29: float = 0.75, artifact_sha256: str = "weak-artifact-immutable-1") -> dict:
    surface = [
        {"temperature_c": 29.0, "likelihood": likelihood_29},
        {"temperature_c": 30.0, "likelihood": 1.0 - likelihood_29},
    ]
    surface_sha = hashlib.sha256(
        json.dumps([[29.0, likelihood_29], [30.0, 1.0 - likelihood_29]], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "schema": "joint_forecast_weak_likelihood_v1",
        "ok": True,
        "state": "available",
        "artifact_sha256": artifact_sha256,
        "forecast_evidence_snapshot_id": "forecast-snapshot-immutable-1",
        "market_data_consumed": False,
        "observation_data_consumed": False,
        "temperature_grid_c": [29.0, 30.0],
        "joint_forecast_weak_likelihood": surface,
        "joint_forecast_weak_likelihood_sha256": surface_sha,
        "weak_source_set": {"source_set_key": "empty"},
        "source_expert_trace": [],
    }


def _resolution_contract(*, closed: bool = True) -> dict:
    return {
        "resolution_contract_id": "resolution-contract-immutable-1",
        "resolution_source_id": "wunderground-zuuu-daily-history",
        "resolution_local_date": "2026-07-03",
        "settlement_unit": "C",
        "physical_to_settlement_kernel": {
            "29": [{"settlement_value_c": 29.0, "probability": 1.0}],
            "30": [{"settlement_value_c": 30.0, "probability": 1.0}],
        },
        "bucket_contract": {
            "lt30": {"lower_inclusive": None, "upper_exclusive": 30.0},
            "ge30": {"lower_inclusive": 30.0, "upper_exclusive": None},
        },
        "bridge_closed": closed,
        "validated": closed,
    }


def _strong_artifact() -> dict:
    return approved_strong_artifact(
        post_state_transition={"warming": {"warming": 1.0}, "cooling": {"cooling": 1.0}}
    )


def _activate_strong_artifact(conn: sqlite3.Connection, artifact: dict | None = None) -> None:
    value = artifact or _strong_artifact()
    register_strong_lr_runtime_artifact(
        conn,
        artifact=value,
        registered_at="2026-07-03T06:00:00Z",
        registered_by="pytest",
        registration_basis="test_runtime_baseline",
        registration_ref="pytest://strong-lr",
    )
    activate_strong_lr_runtime_artifact(
        conn,
        target_identity=TARGET,
        artifact_sha256=value["model_artifact_sha256"],
        activated_at="2026-07-03T06:00:00Z",
        activated_by="pytest",
        activation_basis="approved_runtime_baseline",
        activation_ref="pytest://strong-lr-approval",
        activation_evidence=strong_activation_attestation(
            target_identity=TARGET,
            artifact_sha256=value["model_artifact_sha256"],
            activation_basis="approved_runtime_baseline",
            activation_ref="pytest://strong-lr-approval",
        ),
    )


def _build(**overrides) -> dict:
    values = {
        "target_identity": TARGET,
        "event_date": "2026-07-03",
        "station_id": "ZUUU",
        "decision_as_of": AS_OF,
        "prior_context": _prior(),
        "joint_weak_likelihood": _weak(),
        "active_strong_lr_artifact": _strong_artifact(),
        "resolution_contract": _resolution_contract(),
        "source_reliability_initial_state": {"AMSC": {"state": "unknown"}},
    }
    values.update(overrides)
    return build_initial_posterior_checkpoint(**values)


def test_p0_is_only_prior_times_weak_before_any_observation_and_ignores_oos_at_runtime() -> None:
    # Promotion evidence is deliberately absent.  Structural runtime validity
    # remains enough: strict as-of/OOS must never become a live-event gate.
    artifact = approved_strong_artifact(
        post_state_transition={"warming": {"warming": 1.0}, "cooling": {"cooling": 1.0}}
    )
    for key in ("approval_state", "training_manifest_sha256", "oos_evaluation_sha256", "training_data_policy"):
        artifact.pop(key, None)
    artifact["feature_evaluator"].pop("training_feature_manifest_sha256")
    artifact["feature_evaluator"].pop("oos_feature_ablation_sha256")

    result = _build(active_strong_lr_artifact=artifact)

    assert result["ok"] is True
    assert result["checkpoint"]["checkpoint_kind"] == "initial"
    assert result["checkpoint"]["consumed_event_ids"] == []
    assert result["checkpoint"]["observed_max_lower_bound_c"] is None
    assert result["boundary"]["consumes_d0_observation"] is False
    assert result["boundary"]["strict_asof_oos_runtime_gate"] is False


def test_p0_accepts_explicit_safety_boundary_metadata_on_prior() -> None:
    prior = _prior()
    prior["boundary"] = {
        "places_orders": False,
        "writes_posterior": False,
        "market_or_trading_not_consumed": True,
    }

    result = _build(prior_context=prior)

    assert result["ok"] is True
    # normalize([.40*.75, .60*.25]) = [.666..., .333...]
    assert result["checkpoint"]["physical_high_distribution"] == pytest.approx({"29": 2.0 / 3.0, "30": 1.0 / 3.0})
    assert result["resolution_bridge"]["market_bucket_probabilities"] == pytest.approx({"lt30": 2.0 / 3.0, "ge30": 1.0 / 3.0})


def test_p0_keeps_x_available_when_the_x_to_s_to_b_bridge_is_unclosed_and_rejects_current_fact_contamination() -> None:
    unclosed = _build(resolution_contract=_resolution_contract(closed=False))
    contaminated = _build(source_reliability_initial_state={"current_observation_temperature_c": 31.0})

    assert unclosed["ok"] is True
    assert unclosed["checkpoint"]["physical_high_distribution"] == pytest.approx({"29": 2.0 / 3.0, "30": 1.0 / 3.0})
    assert unclosed["resolution_bridge"]["state"] == "physical_only_market_projection_na"
    assert unclosed["resolution_bridge"]["market_bucket_probabilities"] == {}
    assert unclosed["resolution_bridge"]["market_bucket_probabilities_na_reason"] == "x_to_s_to_b_bridge_unavailable"
    assert unclosed["checkpoint"]["market_bucket_projection_state"] == "physical_only_market_projection_na"
    assert contaminated["ok"] is False
    assert "initial_source_reliability_contains_forbidden_runtime_surface" in contaminated["block_reasons"]


def test_x_only_p0_persists_without_a_market_bucket_projection() -> None:
    conn = sqlite3.connect(":memory:")
    initialize_isolated_public_ssot_shadow_schema(conn)
    initialize_p10_shadow_runtime_schema(conn)
    _activate_strong_artifact(conn)

    initial = _build(resolution_contract=_resolution_contract(closed=False))
    persisted = persist_initial_posterior_checkpoint_shadow(conn, initial_checkpoint=initial)

    assert persisted["ok"] is True
    row = conn.execute("SELECT market_bucket_distribution_json FROM posterior_checkpoints").fetchone()
    assert row is not None and row[0] == "{}"


def test_p0_rejects_a_latent_state_that_the_active_strong_artifact_cannot_transition() -> None:
    result = _build(active_strong_lr_artifact=approved_strong_artifact())

    assert result["ok"] is False
    assert "initial_strong_artifact_transition_missing_initial_state" in result["block_reasons"]


def test_p0_persists_one_atomic_append_only_predecessor_and_refuses_target_forks() -> None:
    conn = sqlite3.connect(":memory:")
    initialize_isolated_public_ssot_shadow_schema(conn)
    initialize_p10_shadow_runtime_schema(conn)
    _activate_strong_artifact(conn)
    initial = _build()

    first = persist_initial_posterior_checkpoint_shadow(conn, initial_checkpoint=initial)
    retry = persist_initial_posterior_checkpoint_shadow(conn, initial_checkpoint=initial)

    assert first["ok"] is True and first["idempotent"] is False
    assert retry["ok"] is True and retry["idempotent"] is True
    assert conn.execute("SELECT COUNT(*) FROM posterior_checkpoints").fetchone()[0] == 1
    assert conn.execute(f"SELECT COUNT(*) FROM {INITIAL_LINEAGE_TABLE}").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM strong_observation_updates").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM realization_states").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM posterior_commit_outbox").fetchone()[0] == 0

    with pytest.raises(sqlite3.IntegrityError, match="initial_posterior_lineage_append_only"):
        conn.execute(f"UPDATE {INITIAL_LINEAGE_TABLE} SET created_at='drift'")

    conflicting = _build(joint_weak_likelihood=_weak(likelihood_29=0.25, artifact_sha256="weak-artifact-immutable-2"))
    with pytest.raises(ShadowTransactionIntegrityError, match="initial_checkpoint_target_already_has_different_checkpoint"):
        persist_initial_posterior_checkpoint_shadow(conn, initial_checkpoint=conflicting)

    reliability_drift = _build(source_reliability_initial_state={"AMSC": {"state": "degraded"}})
    assert reliability_drift["checkpoint"]["posterior_checkpoint_id"] != initial["checkpoint"]["posterior_checkpoint_id"]
    with pytest.raises(ShadowTransactionIntegrityError, match="initial_checkpoint_target_already_has_different_checkpoint"):
        persist_initial_posterior_checkpoint_shadow(conn, initial_checkpoint=reliability_drift)
