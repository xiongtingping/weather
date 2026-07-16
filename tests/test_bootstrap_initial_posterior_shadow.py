from __future__ import annotations

import json
import sqlite3

from p10_shadow_runtime_schema import initialize_p10_shadow_runtime_schema
from scripts.bootstrap_initial_posterior_shadow import CONFIRMATION, main
from sequential_bayes_public_ssot_shadow_writer import initialize_isolated_public_ssot_shadow_schema
from strong_observation_lr_runtime_registry import (
    activate_strong_lr_runtime_artifact,
    register_strong_lr_runtime_artifact,
)
from tests.strong_observation_artifact_fixtures import (
    approved_strong_artifact,
    strong_activation_attestation,
)


def _payload() -> dict:
    return {
        "target_identity": "zuuu-2026-07-03",
        "event_date": "2026-07-03",
        "station_id": "ZUUU",
        "decision_as_of": "2026-07-03T06:30:00Z",
        "prior_context": {
            "prior_context_id": "prior-context-immutable-1",
            "artifact_sha256": "prior-artifact-immutable-1",
            "continuous_distribution": {
                "schema": "continuous_temperature_climate_prior_v1",
                "grid": [{"temp_c": 29.0, "mass": 0.4}, {"temp_c": 30.0, "mass": 0.6}],
            },
            "latent_state_prior_by_temperature": {
                "29": {"warming": 1.0},
                "30": {"warming": 1.0},
            },
        },
        "joint_weak_likelihood": {
            "schema": "joint_forecast_weak_likelihood_v1",
            "ok": True,
            "state": "available",
            "artifact_sha256": "weak-artifact-immutable-1",
            "forecast_evidence_snapshot_id": "forecast-snapshot-immutable-1",
            "market_data_consumed": False,
            "observation_data_consumed": False,
            "temperature_grid_c": [29.0, 30.0],
            "joint_forecast_weak_likelihood": [
                {"temperature_c": 29.0, "likelihood": 0.5},
                {"temperature_c": 30.0, "likelihood": 0.5},
            ],
        },
        "resolution_contract": {
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
            "bridge_closed": True,
            "validated": True,
        },
        "source_reliability_initial_state": {"AMSC": {"state": "unknown"}},
    }


def test_bootstrap_p0_is_preview_by_default_and_can_only_write_a_ready_isolated_shadow_db(tmp_path, capsys) -> None:
    db = tmp_path / "isolated.db"
    with sqlite3.connect(db) as conn:
        initialize_isolated_public_ssot_shadow_schema(conn)
        initialize_p10_shadow_runtime_schema(conn)
        artifact = approved_strong_artifact()
        register_strong_lr_runtime_artifact(
            conn,
            artifact=artifact,
            registered_at="2026-07-03T06:00:00Z",
            registered_by="pytest",
            registration_basis="test_runtime_baseline",
            registration_ref="pytest://bootstrap-p0",
        )
        activate_strong_lr_runtime_artifact(
            conn,
            target_identity=_payload()["target_identity"],
            artifact_sha256=artifact["model_artifact_sha256"],
            activated_at="2026-07-03T06:00:00Z",
            activated_by="pytest",
            activation_basis="approved_runtime_baseline",
            activation_ref="pytest://bootstrap-p0-approval",
            activation_evidence=strong_activation_attestation(
                target_identity=_payload()["target_identity"],
                artifact_sha256=artifact["model_artifact_sha256"],
                activation_basis="approved_runtime_baseline",
                activation_ref="pytest://bootstrap-p0-approval",
            ),
        )
    input_path = tmp_path / "p0-input.json"
    input_path.write_text(json.dumps(_payload()), encoding="utf-8")

    assert main(["--input-json", str(input_path), "--shadow-db", str(db)]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["state"] == "preview_ready"
    assert preview["strong_lr_artifact_sha256"] == approved_strong_artifact()["model_artifact_sha256"]
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM posterior_checkpoints").fetchone()[0] == 0

    assert main([
        "--input-json", str(input_path), "--shadow-db", str(db), "--apply", "--confirm", CONFIRMATION,
    ]) == 0
    persisted = json.loads(capsys.readouterr().out)
    assert persisted["ok"] is True and persisted["idempotent"] is False
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM posterior_checkpoints").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM strong_observation_updates").fetchone()[0] == 0


def test_bootstrap_p0_refuses_apply_without_confirmation(tmp_path, capsys) -> None:
    input_path = tmp_path / "p0-input.json"
    input_path.write_text(json.dumps(_payload()), encoding="utf-8")

    assert main(["--input-json", str(input_path), "--shadow-db", str(tmp_path / "any.db"), "--apply"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["blockers"] == ["initial_posterior_shadow_confirmation_invalid"]


def test_bootstrap_p0_rejects_caller_supplied_strong_artifact_even_for_preview(tmp_path, capsys) -> None:
    payload = _payload()
    payload["active_strong_lr_artifact"] = approved_strong_artifact()
    input_path = tmp_path / "p0-input.json"
    input_path.write_text(json.dumps(payload), encoding="utf-8")

    assert main(["--input-json", str(input_path), "--shadow-db", str(tmp_path / "missing.db")]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["blockers"] == ["initial_posterior_input_caller_supplied_strong_artifact_forbidden"]


def test_bootstrap_p0_preview_requires_active_registry_artifact(tmp_path, capsys) -> None:
    db = tmp_path / "isolated.db"
    with sqlite3.connect(db) as conn:
        initialize_isolated_public_ssot_shadow_schema(conn)
        initialize_p10_shadow_runtime_schema(conn)
    input_path = tmp_path / "p0-input.json"
    input_path.write_text(json.dumps(_payload()), encoding="utf-8")

    assert main(["--input-json", str(input_path), "--shadow-db", str(db)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["blockers"] == ["initial_posterior_active_strong_lr_artifact_unavailable"]
