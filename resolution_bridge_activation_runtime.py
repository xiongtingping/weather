"""Production pointer for the independently promoted X->S bridge artifact."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from resolution_bridge_artifact_runtime import validate_resolution_bridge_artifact


SCHEMA_VERSION = "resolution_bridge_production_activation_v1"
DEFAULT_PATH = Path(__file__).resolve().parent / "config/resolution_bridge_production_activation.json"


class ResolutionBridgeActivationError(ValueError):
    pass


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def activation_sha256(value: Mapping[str, Any]) -> str:
    unsigned = {key: item for key, item in _mapping(value).items() if key != "activation_sha256"}
    return hashlib.sha256(_canonical(unsigned).encode("utf-8")).hexdigest()


def validate_resolution_bridge_activation(value: Mapping[str, Any]) -> dict[str, Any]:
    activation = dict(_mapping(value))
    issues: list[str] = []
    if _text(activation.get("schema")) != SCHEMA_VERSION:
        issues.append("resolution_bridge_activation_schema_invalid")
    state = _text(activation.get("state"))
    if state not in {"disabled", "production_enabled"}:
        issues.append("resolution_bridge_activation_state_invalid")
    if activation.get("places_orders") is not False or activation.get("authorizes_clob") is not False:
        issues.append("resolution_bridge_activation_must_not_authorize_orders")
    if state == "disabled":
        if _text(activation.get("artifact_path")) or _text(activation.get("artifact_sha256")):
            issues.append("disabled_resolution_bridge_must_not_point_to_artifact")
    else:
        if not _text(activation.get("artifact_path")) or len(_text(activation.get("artifact_sha256"))) != 64:
            issues.append("enabled_resolution_bridge_artifact_pointer_invalid")
        if not _text(activation.get("approval_id")) or len(_text(activation.get("approval_receipt_sha256"))) != 64:
            issues.append("enabled_resolution_bridge_approval_lineage_invalid")
    supplied = _text(activation.get("activation_sha256"))
    if supplied and supplied != activation_sha256(activation):
        issues.append("resolution_bridge_activation_sha256_invalid")
    if issues:
        raise ResolutionBridgeActivationError(
            "resolution_bridge_activation_invalid:" + ",".join(sorted(set(issues)))
        )
    activation["activation_sha256"] = activation_sha256(activation)
    return activation


def load_resolution_bridge_activation(path: Path | str = DEFAULT_PATH) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ResolutionBridgeActivationError(
            f"resolution_bridge_activation_unreadable:{type(exc).__name__}"
        ) from exc
    return validate_resolution_bridge_activation(_mapping(value))


def load_active_resolution_bridge_artifact(
    *,
    activation_path: Path | str = DEFAULT_PATH,
    root: Path | str | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    activation = load_resolution_bridge_activation(activation_path)
    if _text(activation.get("state")) == "disabled":
        return activation, None
    base = Path(root).expanduser().resolve() if root is not None else Path(__file__).resolve().parent
    artifact_path = Path(_text(activation.get("artifact_path")))
    artifact_path = artifact_path if artifact_path.is_absolute() else base / artifact_path
    try:
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ResolutionBridgeActivationError(
            f"resolution_bridge_artifact_unreadable:{type(exc).__name__}"
        ) from exc
    try:
        validated = validate_resolution_bridge_artifact(_mapping(artifact), require_production=True)
    except ValueError as exc:
        raise ResolutionBridgeActivationError(str(exc)) from exc
    if _text(validated.get("artifact_sha256")) != _text(activation.get("artifact_sha256")):
        raise ResolutionBridgeActivationError("resolution_bridge_activation_artifact_sha_mismatch")
    if _text(validated.get("approval_id")) != _text(activation.get("approval_id")):
        raise ResolutionBridgeActivationError("resolution_bridge_activation_approval_id_mismatch")
    if _text(validated.get("approval_receipt_sha256")) != _text(activation.get("approval_receipt_sha256")):
        raise ResolutionBridgeActivationError("resolution_bridge_activation_approval_receipt_mismatch")
    return activation, validated


__all__ = [
    "DEFAULT_PATH",
    "ResolutionBridgeActivationError",
    "SCHEMA_VERSION",
    "activation_sha256",
    "load_active_resolution_bridge_artifact",
    "load_resolution_bridge_activation",
    "validate_resolution_bridge_activation",
]
