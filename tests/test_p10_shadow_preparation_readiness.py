from __future__ import annotations

import json
import sqlite3

from p10_shadow_preparation_readiness import audit_p10_shadow_preparation_readiness
from p10_shadow_runtime_schema import initialize_p10_shadow_runtime_schema
from sequential_bayes_public_ssot_shadow_writer import initialize_isolated_public_ssot_shadow_schema
from strong_observation_lr_runtime_registry import (
    activate_strong_lr_runtime_artifact,
    register_strong_lr_runtime_artifact,
)
from tests.strong_observation_artifact_fixtures import (
    approved_strong_artifact,
    strong_activation_attestation,
)


TARGET = "physical_weather_day_v1:ZUUU:2026-07-03"


def _activate_artifact(conn: sqlite3.Connection, *, basis: str, sha: str) -> None:
    artifact = approved_strong_artifact(model_artifact_sha256=sha)
    register_strong_lr_runtime_artifact(
        conn,
        artifact=artifact,
        registered_at="2026-07-03T04:00:00Z",
        registered_by="pytest",
        registration_basis="test_runtime_artifact",
        registration_ref=f"pytest://{sha}",
    )
    activate_strong_lr_runtime_artifact(
        conn,
        target_identity=TARGET,
        artifact_sha256=sha,
        activated_at="2026-07-03T04:30:00Z",
        activated_by="pytest",
        activation_basis=basis,
        activation_ref=f"pytest://{basis}",
        activation_evidence=strong_activation_attestation(
            target_identity=TARGET,
            artifact_sha256=sha,
            activation_basis=basis,
            activation_ref=f"pytest://{basis}",
        ),
    )


def test_readiness_audit_reports_runtime_and_offline_dependencies_separately(tmp_path) -> None:
    conn = sqlite3.connect(":memory:")
    initialize_isolated_public_ssot_shadow_schema(conn)
    initialize_p10_shadow_runtime_schema(conn)
    labels = tmp_path / "labels.db"
    sqlite3.connect(labels).close()

    result = audit_p10_shadow_preparation_readiness(
        conn,
        final_label_db_path=labels,
        inbox_dir=tmp_path / "inbox",
    )

    assert result["ok"] is False
    assert "p10_preparation_previous_checkpoint_missing" in result["blockers"]
    assert "p10_preparation_persisted_cycle_missing" not in result["blockers"]
    assert "p10_preparation_runtime_strong_lr_artifact_unavailable" in result["blockers"]
    assert "p10_preparation_final_resolution_labels_table_missing" in result["offline_promotion_blockers"]
    assert result["boundary"]["strict_asof_oos_are_offline_promotion_only"] is True
    assert result["shadow_db"]["prepared_cycle_count"] == 0
    assert result["shadow_db"]["prepared_cycle_status"] == "prepared_cycle_missing"
    assert result["boundary"]["prepared_cycle_is_output_not_precondition"] is True


def test_readiness_loads_only_active_registry_baseline_without_making_offline_evidence_a_live_gate(tmp_path) -> None:
    conn = sqlite3.connect(":memory:")
    initialize_isolated_public_ssot_shadow_schema(conn)
    initialize_p10_shadow_runtime_schema(conn)
    _activate_artifact(
        conn,
        basis="approved_runtime_baseline",
        sha="strong-artifact-approved-baseline-1",
    )
    result = audit_p10_shadow_preparation_readiness(conn, target_identity=TARGET)

    strong = result["strong_lr_artifact"]
    assert strong["source"] == "strong_lr_runtime_active_registry"
    assert strong["activation_basis"] == "approved_runtime_baseline"
    assert strong["runtime"]["ok"] is True
    assert strong["offline_artifact_packet"]["ok"] is True
    assert strong["offline_promotion"]["ok"] is True
    assert strong["offline_promotion"]["state"] == "not_applicable_approved_runtime_baseline"
    assert strong["offline_promotion"]["activation_ref"] == "pytest://approved_runtime_baseline"
    assert len(strong["offline_promotion"]["activation_evidence_sha256"]) == 64
    assert "p10_preparation_runtime_strong_lr_artifact_unavailable" not in result["blockers"]
    assert "p10_preparation_offline_strong_lr_promotion_evidence_missing" not in result["offline_promotion_blockers"]


def test_readiness_reports_registry_attested_candidate_promotion(tmp_path) -> None:
    conn = sqlite3.connect(":memory:")
    initialize_isolated_public_ssot_shadow_schema(conn)
    initialize_p10_shadow_runtime_schema(conn)
    _activate_artifact(conn, basis="promoted_candidate", sha="strong-artifact-promoted-1")

    result = audit_p10_shadow_preparation_readiness(conn, target_identity=TARGET)

    promotion = result["strong_lr_artifact"]["offline_promotion"]
    assert promotion["ok"] is True
    assert promotion["state"] == "activation_registry_attested_promotion"
    assert promotion["activation_ref"] == "pytest://promoted_candidate"
    assert len(promotion["activation_evidence_sha256"]) == 64
    assert len(promotion["promotion_gate_sha256"]) == 64
    assert "p10_preparation_offline_strong_lr_promotion_evidence_missing" not in result["offline_promotion_blockers"]


def test_readiness_does_not_treat_legacy_raw_archive_status_as_p10_source_evidence(tmp_path) -> None:
    conn = sqlite3.connect(":memory:")
    initialize_isolated_public_ssot_shadow_schema(conn)
    initialize_p10_shadow_runtime_schema(conn)
    status_path = tmp_path / "status.json"
    status_path.write_text(
        json.dumps({"state": "published", "raw_archive_dir": "/legacy/data/amsc_raw_history"}),
        encoding="utf-8",
    )

    result = audit_p10_shadow_preparation_readiness(
        conn,
        inbox_dir=tmp_path / "inbox",
        source_status_path=status_path,
        source_raw_archive_dir=tmp_path / "p10-raw",
    )

    source = result["source_inbox"]
    assert source["source_status_bound_to_raw_archive"] is False
    assert source["source_status"] == {}
    assert source["unbound_source_status"]["state"] == "published"
