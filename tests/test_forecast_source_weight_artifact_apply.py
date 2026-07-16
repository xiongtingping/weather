import json

from forecast_source_weight_artifact_registry import (
    ACTIVE_SCHEMA,
    canonical_artifact_sha256,
    load_approved_forecast_source_weight_artifact,
)
from scripts.forecast_source_weight_artifact_apply import (
    APPLY_CONFIRMATION,
    ROLLBACK_CONFIRMATION,
    apply_approved_artifact,
    rollback_active_artifact,
)


def _artifact(weight):
    artifact = {
        "schema": "forecast_source_contextual_weight_artifact_v1",
        "source_data_policy": "external_only",
        "entities": ["a", "b"],
        "provider_families": {"a": "a", "b": "b"},
        "global_weights": {"a": weight, "b": 1.0 - weight},
        "production_ready": True,
        "runtime_integrated": True,
    }
    artifact["artifact_sha256"] = canonical_artifact_sha256(artifact)
    return artifact


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_apply_is_dry_run_without_exact_confirmation_and_atomic_with_approval(tmp_path):
    registry = tmp_path / "registry"
    pointer = tmp_path / "active.json"
    old = _artifact(0.5)
    new = _artifact(0.54)
    _write(registry / f"{old['artifact_sha256']}.json", old)
    _write(
        pointer,
        {
            "schema": ACTIVE_SCHEMA,
            "artifact_sha256": old["artifact_sha256"],
            "previous_artifact_sha256": "",
            "approval_state": "approved",
            "explicit_user_approval": True,
        },
    )
    packet = {
        "schema": "forecast_source_weight_promotion_packet_v1",
        "promotion_proposal_eligible": True,
        "production_apply_allowed": False,
        "candidate_id": "cal_1234567890abcdef",
        "packet_sha256": "packet-1",
        "blockers": [],
        "parameter_change_request": {
            "artifact_bindings": {
                "artifact_sha256": new["artifact_sha256"],
                "parent_artifact_sha256": old["artifact_sha256"],
            }
        },
    }
    approval = {
        "schema": "forecast_source_weight_approval_v1",
        "state": "approved",
        "explicit_user_approval": True,
        "candidate_id": packet["candidate_id"],
        "packet_sha256": packet["packet_sha256"],
        "approval_id": "approval-1",
        "approved_by": "tester",
        "approved_at": "2026-07-11T00:00:00Z",
    }
    packet_path, artifact_path, approval_path = tmp_path / "packet.json", tmp_path / "artifact.json", tmp_path / "approval.json"
    _write(packet_path, packet)
    _write(artifact_path, new)
    _write(approval_path, approval)

    dry = apply_approved_artifact(
        packet_path=packet_path,
        artifact_path=artifact_path,
        approval_path=approval_path,
        confirmation="",
        active_pointer_path=pointer,
        registry_root=registry,
    )
    assert dry["dry_run"] is True
    assert json.loads(pointer.read_text())["artifact_sha256"] == old["artifact_sha256"]

    applied = apply_approved_artifact(
        packet_path=packet_path,
        artifact_path=artifact_path,
        approval_path=approval_path,
        confirmation=APPLY_CONFIRMATION,
        active_pointer_path=pointer,
        registry_root=registry,
    )
    assert applied["dry_run"] is False
    loaded = load_approved_forecast_source_weight_artifact(active_pointer_path=pointer, registry_root=registry)
    assert loaded["artifact_sha256"] == new["artifact_sha256"]
    assert loaded["approval_state"] == "approved"

    rolled = rollback_active_artifact(
        active_pointer_path=pointer,
        registry_root=registry,
        confirmation=ROLLBACK_CONFIRMATION,
    )
    assert rolled["dry_run"] is False
    assert json.loads(pointer.read_text())["artifact_sha256"] == old["artifact_sha256"]
    assert rolled["rollback_requires_retraining"] is False


def test_tampered_artifact_is_rejected(tmp_path):
    artifact = _artifact(0.5)
    artifact["global_weights"]["a"] = 0.9
    packet_path, artifact_path, approval_path = tmp_path / "packet.json", tmp_path / "artifact.json", tmp_path / "approval.json"
    _write(packet_path, {"schema": "forecast_source_weight_promotion_packet_v1"})
    _write(artifact_path, artifact)
    _write(approval_path, {"schema": "forecast_source_weight_approval_v1"})
    report = apply_approved_artifact(
        packet_path=packet_path,
        artifact_path=artifact_path,
        approval_path=approval_path,
        confirmation=APPLY_CONFIRMATION,
        active_pointer_path=tmp_path / "active.json",
        registry_root=tmp_path / "registry",
    )
    assert report["ok"] is False
    assert "artifact_sha256_mismatch" in report["issues"]


def test_active_pointer_cannot_relabel_research_artifact_as_runtime_ready(tmp_path):
    registry = tmp_path / "registry"
    pointer = tmp_path / "active.json"
    research = _artifact(0.5)
    research["production_ready"] = False
    research["runtime_integrated"] = False
    research["research_only"] = True
    research["artifact_sha256"] = canonical_artifact_sha256(research)
    _write(registry / f"{research['artifact_sha256']}.json", research)
    _write(
        pointer,
        {
            "schema": ACTIVE_SCHEMA,
            "artifact_sha256": research["artifact_sha256"],
            "approval_state": "approved",
            "explicit_user_approval": True,
        },
    )

    assert load_approved_forecast_source_weight_artifact(
        active_pointer_path=pointer,
        registry_root=registry,
    ) is None


def test_first_promotion_can_rollback_to_legacy_no_active_artifact(tmp_path):
    registry = tmp_path / "registry"
    pointer = tmp_path / "active.json"
    new = _artifact(0.54)
    packet = {
        "schema": "forecast_source_weight_promotion_packet_v1",
        "promotion_proposal_eligible": True,
        "production_apply_allowed": False,
        "candidate_id": "cal_first",
        "packet_sha256": "packet-first",
        "blockers": [],
        "parameter_change_request": {
            "artifact_bindings": {
                "artifact_sha256": new["artifact_sha256"],
                "parent_artifact_sha256": "legacy-reference-hash",
                "parent_runtime_mode": "legacy_no_active_artifact",
            }
        },
    }
    approval = {
        "schema": "forecast_source_weight_approval_v1",
        "state": "approved",
        "explicit_user_approval": True,
        "candidate_id": packet["candidate_id"],
        "packet_sha256": packet["packet_sha256"],
        "approval_id": "approval-first",
        "approved_by": "tester",
        "approved_at": "2026-07-11T00:00:00Z",
    }
    packet_path, artifact_path, approval_path = tmp_path / "packet.json", tmp_path / "artifact.json", tmp_path / "approval.json"
    _write(packet_path, packet)
    _write(artifact_path, new)
    _write(approval_path, approval)

    applied = apply_approved_artifact(
        packet_path=packet_path,
        artifact_path=artifact_path,
        approval_path=approval_path,
        confirmation=APPLY_CONFIRMATION,
        active_pointer_path=pointer,
        registry_root=registry,
    )
    assert applied["ok"] is True
    assert json.loads(pointer.read_text())["previous_runtime_mode"] == "legacy_no_active_artifact"

    rolled = rollback_active_artifact(
        active_pointer_path=pointer,
        registry_root=registry,
        confirmation=ROLLBACK_CONFIRMATION,
    )
    assert rolled["ok"] is True
    assert rolled["to_runtime_mode"] == "legacy_no_active_artifact"
    assert pointer.exists() is False
