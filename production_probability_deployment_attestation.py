"""Audit operational deployment patches without rotating the Bayes epoch."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parent
DEFAULT_PATH = ROOT / "config/production_probability_deployment_attestation.json"
SCHEMA_VERSION = "production_probability_deployment_attestation_v1"
CHANGE_SCOPE = "operational_ingress_supervision_only"


class ProductionProbabilityDeploymentAttestationError(ValueError):
    pass


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def attestation_sha256(value: Mapping[str, Any]) -> str:
    payload = {key: item for key, item in _mapping(value).items() if key != "attestation_sha256"}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_deployment_attestation(
    value: Mapping[str, Any],
    *,
    activation: Mapping[str, Any],
    current_runtime_manifest_sha256: str,
    current_retirement_receipt_sha256: str,
) -> dict[str, Any]:
    attestation = dict(_mapping(value))
    blockers: list[str] = []
    if _text(attestation.get("schema")) != SCHEMA_VERSION:
        blockers.append("schema_invalid")
    if _text(attestation.get("state")) != "deployment_approved":
        blockers.append("state_not_approved")
    if _text(attestation.get("change_scope")) != CHANGE_SCOPE:
        blockers.append("change_scope_invalid")
    if attestation.get("probability_semantics_changed") is not False:
        blockers.append("probability_semantics_must_be_unchanged")
    if attestation.get("places_orders") is not False or attestation.get("authorizes_clob") is not False:
        blockers.append("orders_must_not_be_authorized")
    expected = {
        "base_activation_contract_sha256": _text(activation.get("activation_contract_sha256")),
        "runtime_manifest_sha256": _text(activation.get("runtime_manifest_sha256")),
        "retirement_receipt_sha256": _text(current_retirement_receipt_sha256),
    }
    if expected["runtime_manifest_sha256"] != _text(current_runtime_manifest_sha256):
        blockers.append("probability_runtime_manifest_drift")
    for key, expected_value in expected.items():
        if not expected_value or _text(attestation.get(key)) != expected_value:
            blockers.append(f"{key}_mismatch")
    if not _text(attestation.get("approval_id")):
        blockers.append("approval_id_missing")
    try:
        approved_at = datetime.fromisoformat(_text(attestation.get("approved_at")).replace("Z", "+00:00"))
        if approved_at.tzinfo is None:
            blockers.append("approved_at_timezone_missing")
    except ValueError:
        blockers.append("approved_at_invalid")
    claimed_sha = _text(attestation.get("attestation_sha256"))
    if claimed_sha and claimed_sha != attestation_sha256(attestation):
        blockers.append("attestation_sha256_mismatch")
    if blockers:
        raise ProductionProbabilityDeploymentAttestationError(
            "production_probability_deployment_attestation_invalid:" + ",".join(sorted(set(blockers)))
        )
    attestation["attestation_sha256"] = attestation_sha256(attestation)
    return attestation


def load_deployment_attestation(
    path: Path | str = DEFAULT_PATH,
    *,
    activation: Mapping[str, Any],
    current_runtime_manifest_sha256: str,
    current_retirement_receipt_sha256: str,
) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).expanduser().resolve().read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProductionProbabilityDeploymentAttestationError(
            f"production_probability_deployment_attestation_unreadable:{type(exc).__name__}"
        ) from exc
    return validate_deployment_attestation(
        _mapping(value), activation=activation,
        current_runtime_manifest_sha256=current_runtime_manifest_sha256,
        current_retirement_receipt_sha256=current_retirement_receipt_sha256,
    )


def resolve_retirement_deployment_authorization(
    *,
    activation: Mapping[str, Any],
    current_runtime_manifest_sha256: str,
    current_retirement_receipt_sha256: str,
    path: Path | str = DEFAULT_PATH,
) -> dict[str, Any]:
    activation_receipt = _text(activation.get("legacy_writer_retirement_receipt_sha256"))
    current_receipt = _text(current_retirement_receipt_sha256)
    if activation_receipt and activation_receipt == current_receipt:
        return {
            "authorized": True, "source": "immutable_activation_contract",
            "authorized_retirement_receipt_sha256": activation_receipt,
            "attestation_sha256": "", "approved_at": "", "na_reason": "",
        }
    try:
        attestation = load_deployment_attestation(
            path, activation=activation,
            current_runtime_manifest_sha256=current_runtime_manifest_sha256,
            current_retirement_receipt_sha256=current_receipt,
        )
    except ProductionProbabilityDeploymentAttestationError as exc:
        return {
            "authorized": False, "source": "none",
            "authorized_retirement_receipt_sha256": activation_receipt,
            "attestation_sha256": "", "approved_at": "", "na_reason": str(exc),
        }
    return {
        "authorized": True, "source": "operational_deployment_attestation",
        "authorized_retirement_receipt_sha256": _text(attestation.get("retirement_receipt_sha256")),
        "attestation_sha256": _text(attestation.get("attestation_sha256")),
        "approved_at": _text(attestation.get("approved_at")), "na_reason": "",
    }


__all__ = [
    "CHANGE_SCOPE", "DEFAULT_PATH", "SCHEMA_VERSION",
    "ProductionProbabilityDeploymentAttestationError", "attestation_sha256",
    "load_deployment_attestation", "resolve_retirement_deployment_authorization",
    "validate_deployment_attestation",
]
