from __future__ import annotations

import copy
import sqlite3

import pytest

from sequential_bayes_public_ssot_shadow_writer import initialize_isolated_public_ssot_shadow_schema
from strong_observation_lr_runtime_registry import (
    ACTIVATION_TABLE,
    ARTIFACT_TABLE,
    activate_strong_lr_runtime_artifact,
    assert_active_strong_lr_artifact_binding,
    initialize_strong_lr_runtime_registry_schema,
    load_active_strong_lr_runtime_artifact,
    register_strong_lr_runtime_artifact,
)
from tests.strong_observation_artifact_fixtures import (
    approved_strong_artifact,
    strong_activation_attestation,
)


TARGET = "physical_weather_day_v1:ZUUU:2026-07-03"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    initialize_isolated_public_ssot_shadow_schema(conn)
    initialize_strong_lr_runtime_registry_schema(conn)
    return conn


def _register(conn: sqlite3.Connection, artifact: dict, at: str) -> None:
    register_strong_lr_runtime_artifact(
        conn,
        artifact=artifact,
        registered_at=at,
        registered_by="pytest",
        registration_basis="test_runtime_baseline",
        registration_ref=f"pytest://{artifact['model_artifact_sha256']}",
    )


def test_registry_has_one_active_artifact_per_target_asof_without_mutating_history() -> None:
    conn = _conn()
    first = approved_strong_artifact(model_artifact_sha256="strong-registry-first")
    second = approved_strong_artifact(model_artifact_sha256="strong-registry-second")
    _register(conn, first, "2026-07-03T05:00:00Z")
    _register(conn, second, "2026-07-03T06:00:00Z")
    first_activation = activate_strong_lr_runtime_artifact(
        conn,
        target_identity=TARGET,
        artifact_sha256=first["model_artifact_sha256"],
        activated_at="2026-07-03T05:30:00Z",
        activated_by="pytest",
        activation_basis="approved_runtime_baseline",
        activation_ref="pytest://first-approval",
        activation_evidence=strong_activation_attestation(
            target_identity=TARGET,
            artifact_sha256=first["model_artifact_sha256"],
            activation_basis="approved_runtime_baseline",
            activation_ref="pytest://first-approval",
        ),
    )
    activate_strong_lr_runtime_artifact(
        conn,
        target_identity=TARGET,
        artifact_sha256=second["model_artifact_sha256"],
        activated_at="2026-07-03T06:30:00Z",
        activated_by="pytest",
        activation_basis="promoted_candidate",
        activation_ref="pytest://second-promotion",
        activation_evidence=strong_activation_attestation(
            target_identity=TARGET,
            artifact_sha256=second["model_artifact_sha256"],
            activation_basis="promoted_candidate",
            activation_ref="pytest://second-promotion",
        ),
    )

    historical = load_active_strong_lr_runtime_artifact(
        conn, target_identity=TARGET, as_of="2026-07-03T06:00:00Z"
    )
    latest = load_active_strong_lr_runtime_artifact(conn, target_identity=TARGET)

    assert historical["artifact_sha256"] == first["model_artifact_sha256"]
    assert latest["artifact_sha256"] == second["model_artifact_sha256"]
    assert conn.execute(f"SELECT COUNT(*) FROM {ARTIFACT_TABLE}").fetchone()[0] == 2
    assert conn.execute(f"SELECT COUNT(*) FROM {ACTIVATION_TABLE}").fetchone()[0] == 2
    assert latest["activation_id"] != first_activation["activation_id"]
    assert len(latest["activation_evidence_sha256"]) == 64
    assert (
        latest["activation_evidence"]["promotion_gate"]["candidate_artifact_sha256"]
        == second["model_artifact_sha256"]
    )
    with pytest.raises(sqlite3.IntegrityError, match="append_only"):
        conn.execute(f"UPDATE {ACTIVATION_TABLE} SET activated_by='drift'")


def test_registry_binding_rejects_caller_supplied_nonactive_scorer() -> None:
    conn = _conn()
    active = approved_strong_artifact(model_artifact_sha256="strong-registry-active")
    inactive = approved_strong_artifact(model_artifact_sha256="strong-registry-inactive")
    _register(conn, active, "2026-07-03T05:00:00Z")
    _register(conn, inactive, "2026-07-03T05:00:00Z")
    activate_strong_lr_runtime_artifact(
        conn,
        target_identity=TARGET,
        artifact_sha256=active["model_artifact_sha256"],
        activated_at="2026-07-03T05:30:00Z",
        activated_by="pytest",
        activation_basis="approved_runtime_baseline",
        activation_ref="pytest://active-approval",
        activation_evidence=strong_activation_attestation(
            target_identity=TARGET,
            artifact_sha256=active["model_artifact_sha256"],
            activation_basis="approved_runtime_baseline",
            activation_ref="pytest://active-approval",
        ),
    )

    assert_active_strong_lr_artifact_binding(
        conn,
        target_identity=TARGET,
        artifact_sha256=active["model_artifact_sha256"],
    )
    with pytest.raises(ValueError, match="active_artifact_binding_mismatch"):
        assert_active_strong_lr_artifact_binding(
            conn,
            target_identity=TARGET,
            artifact_sha256=inactive["model_artifact_sha256"],
        )


def test_registry_refuses_nonisolated_database_and_old_artifact_schema() -> None:
    normal = sqlite3.connect(":memory:")
    with pytest.raises(ValueError, match="shadow_manifest_missing"):
        initialize_strong_lr_runtime_registry_schema(normal)

    conn = _conn()
    old = approved_strong_artifact()
    old["schema"] = "strong_observation_incremental_lr_artifact_v2"
    with pytest.raises(ValueError, match="artifact_invalid"):
        _register(conn, old, "2026-07-03T05:00:00Z")


def test_registry_rejects_promoted_candidate_without_verifiable_frozen_gate() -> None:
    conn = _conn()
    artifact = approved_strong_artifact(model_artifact_sha256="strong-registry-promotion-proof")
    _register(conn, artifact, "2026-07-03T05:00:00Z")
    evidence = strong_activation_attestation(
        target_identity=TARGET,
        artifact_sha256=artifact["model_artifact_sha256"],
        activation_basis="promoted_candidate",
        activation_ref="pytest://promotion-proof",
    )
    invalid = copy.deepcopy(evidence)
    invalid["promotion_gate"]["oos_evaluation_sha256"] = "fabricated-drift"

    with pytest.raises(ValueError, match="promotion_gate_invalid"):
        activate_strong_lr_runtime_artifact(
            conn,
            target_identity=TARGET,
            artifact_sha256=artifact["model_artifact_sha256"],
            activated_at="2026-07-03T05:30:00Z",
            activated_by="pytest",
            activation_basis="promoted_candidate",
            activation_ref="pytest://promotion-proof",
            activation_evidence=invalid,
        )


def test_registry_rejects_attestation_created_after_activation() -> None:
    conn = _conn()
    artifact = approved_strong_artifact(model_artifact_sha256="strong-registry-future-proof")
    _register(conn, artifact, "2026-07-03T05:00:00Z")
    evidence = strong_activation_attestation(
        target_identity=TARGET,
        artifact_sha256=artifact["model_artifact_sha256"],
        activation_basis="approved_runtime_baseline",
        activation_ref="pytest://future-proof",
    )
    evidence["attested_at"] = "2026-07-03T06:00:00Z"

    with pytest.raises(ValueError, match="attestation_identity_invalid"):
        activate_strong_lr_runtime_artifact(
            conn,
            target_identity=TARGET,
            artifact_sha256=artifact["model_artifact_sha256"],
            activated_at="2026-07-03T05:30:00Z",
            activated_by="pytest",
            activation_basis="approved_runtime_baseline",
            activation_ref="pytest://future-proof",
            activation_evidence=evidence,
        )


def test_registry_activation_is_idempotent_but_refuses_same_timestamp_fork() -> None:
    conn = _conn()
    first = approved_strong_artifact(model_artifact_sha256="strong-registry-idempotent-first")
    second = approved_strong_artifact(model_artifact_sha256="strong-registry-idempotent-second")
    _register(conn, first, "2026-07-03T05:00:00Z")
    _register(conn, second, "2026-07-03T05:00:00Z")
    kwargs = {
        "target_identity": TARGET,
        "artifact_sha256": first["model_artifact_sha256"],
        "activated_at": "2026-07-03T05:30:00Z",
        "activated_by": "pytest",
        "activation_basis": "approved_runtime_baseline",
        "activation_ref": "pytest://idempotent",
        "activation_evidence": strong_activation_attestation(
            target_identity=TARGET,
            artifact_sha256=first["model_artifact_sha256"],
            activation_basis="approved_runtime_baseline",
            activation_ref="pytest://idempotent",
        ),
    }
    created = activate_strong_lr_runtime_artifact(conn, **kwargs)
    repeated = activate_strong_lr_runtime_artifact(conn, **kwargs)
    assert repeated["state"] == "already_active_event"
    assert repeated["activation_id"] == created["activation_id"]

    with pytest.raises(ValueError, match="activation_time_collision"):
        activate_strong_lr_runtime_artifact(
            conn,
            target_identity=TARGET,
            artifact_sha256=second["model_artifact_sha256"],
            activated_at="2026-07-03T05:30:00+00:00",
            activated_by="pytest",
            activation_basis="approved_runtime_baseline",
            activation_ref="pytest://same-time-fork",
            activation_evidence=strong_activation_attestation(
                target_identity=TARGET,
                artifact_sha256=second["model_artifact_sha256"],
                activation_basis="approved_runtime_baseline",
                activation_ref="pytest://same-time-fork",
            ),
        )
