from __future__ import annotations

import json
from pathlib import Path

import pytest

from production_probability_deployment_attestation import (
    CHANGE_SCOPE,
    ProductionProbabilityDeploymentAttestationError,
    attestation_sha256,
    resolve_retirement_deployment_authorization,
    validate_deployment_attestation,
)


def _activation() -> dict:
    return {
        "activation_contract_sha256": "a" * 64,
        "runtime_manifest_sha256": "b" * 64,
        "legacy_writer_retirement_receipt_sha256": "c" * 64,
    }


def _attestation() -> dict:
    value = {
        "schema": "production_probability_deployment_attestation_v1",
        "state": "deployment_approved",
        "approval_id": "unit-operational-lock-recovery",
        "approved_at": "2026-07-14T10:00:00Z",
        "change_scope": CHANGE_SCOPE,
        "base_activation_contract_sha256": "a" * 64,
        "runtime_manifest_sha256": "b" * 64,
        "retirement_receipt_sha256": "d" * 64,
        "probability_semantics_changed": False,
        "places_orders": False,
        "authorizes_clob": False,
    }
    value["attestation_sha256"] = attestation_sha256(value)
    return value


def test_operational_attestation_authorizes_receipt_patch_without_rotating_activation(tmp_path: Path) -> None:
    path = tmp_path / "attestation.json"
    path.write_text(json.dumps(_attestation()), encoding="utf-8")

    result = resolve_retirement_deployment_authorization(
        activation=_activation(),
        current_runtime_manifest_sha256="b" * 64,
        current_retirement_receipt_sha256="d" * 64,
        path=path,
    )

    assert result["authorized"] is True
    assert result["source"] == "operational_deployment_attestation"
    assert result["authorized_retirement_receipt_sha256"] == "d" * 64


def test_operational_attestation_cannot_change_probability_runtime_or_order_boundary() -> None:
    value = _attestation()
    value["probability_semantics_changed"] = True
    value["places_orders"] = True
    value["attestation_sha256"] = attestation_sha256(value)

    with pytest.raises(ProductionProbabilityDeploymentAttestationError):
        validate_deployment_attestation(
            value,
            activation=_activation(),
            current_runtime_manifest_sha256="b" * 64,
            current_retirement_receipt_sha256="d" * 64,
        )
