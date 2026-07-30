from __future__ import annotations

import pytest
import json

from resolution_bridge_activation_runtime import (
    ResolutionBridgeActivationError,
    activation_sha256,
    load_active_resolution_bridge_artifact,
    validate_resolution_bridge_activation,
)


def _enabled(pointer):
    core = {
        "schema": "resolution_bridge_production_activation_v1",
        "state": "production_enabled",
        "artifact_path": pointer,
        "artifact_sha256": "a" * 64,
        "approval_id": "approved",
        "approval_receipt_sha256": "b" * 64,
        "places_orders": False,
        "authorizes_clob": False,
    }
    return {**core, "activation_sha256": activation_sha256(core)}


def test_disabled_bridge_activation_is_orderless_and_has_no_pointer() -> None:
    result = validate_resolution_bridge_activation({
        "schema": "resolution_bridge_production_activation_v1",
        "state": "disabled",
        "artifact_path": "",
        "artifact_sha256": "",
        "approval_id": "",
        "approval_receipt_sha256": "",
        "places_orders": False,
        "authorizes_clob": False,
    })
    assert result["state"] == "disabled"
    assert len(result["activation_sha256"]) == 64


def test_disabled_bridge_cannot_smuggle_artifact_pointer() -> None:
    with pytest.raises(ResolutionBridgeActivationError):
        validate_resolution_bridge_activation({
            "schema": "resolution_bridge_production_activation_v1",
            "state": "disabled",
            "artifact_path": "candidate.json",
            "artifact_sha256": "a" * 64,
            "places_orders": False,
            "authorizes_clob": False,
        })


@pytest.mark.parametrize("pointer", ["/Volumes/archive/bridge.json", "../bridge.json"])
def test_enabled_bridge_rejects_nonlocal_pointer_before_open(tmp_path, pointer) -> None:
    activation_path = tmp_path / "activation.json"
    activation_path.write_text(json.dumps(_enabled(pointer)))
    with pytest.raises(ValueError, match="runtime_pointer_must_be_repo_relative"):
        load_active_resolution_bridge_artifact(
            activation_path=activation_path, root=tmp_path
        )


def test_enabled_bridge_rejects_symlink_pointer_without_following_it(tmp_path) -> None:
    outside = tmp_path.parent / "external-bridge-do-not-read.json"
    outside.write_text("not-an-artifact")
    (tmp_path / "bridge.json").symlink_to(outside)
    activation_path = tmp_path / "activation.json"
    activation_path.write_text(json.dumps(_enabled("bridge.json")))
    with pytest.raises(ValueError, match="runtime_pointer_symlink_forbidden"):
        load_active_resolution_bridge_artifact(
            activation_path=activation_path, root=tmp_path
        )
