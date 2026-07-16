from __future__ import annotations

import pytest

from resolution_bridge_activation_runtime import (
    ResolutionBridgeActivationError,
    validate_resolution_bridge_activation,
)


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
