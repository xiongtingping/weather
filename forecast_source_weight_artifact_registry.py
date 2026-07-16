"""Read-only loader and validator for the approved source-weight artifact SSOT."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parent
DEFAULT_REGISTRY_ROOT = ROOT / "data" / "forecast_source_weight_artifacts"
DEFAULT_ACTIVE_POINTER = ROOT / "data" / "forecast_source_weight_active.json"
ACTIVE_SCHEMA = "forecast_source_weight_active_pointer_v1"


def canonical_artifact_sha256(artifact: Mapping[str, Any]) -> str:
    payload = {key: value for key, value in artifact.items() if key != "artifact_sha256"}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def validate_artifact(artifact: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate an immutable artifact's structural and hash contract only.

    Research artifacts are intentionally valid under this base contract.  They
    are *not* thereby eligible to become the active runtime artifact; that
    stronger distinction is enforced by ``validate_runtime_approved_artifact``.
    """

    value = artifact if isinstance(artifact, Mapping) else {}
    issues: list[str] = []
    if value.get("schema") != "forecast_source_contextual_weight_artifact_v1":
        issues.append("artifact_schema_invalid")
    if value.get("source_data_policy") != "external_only":
        issues.append("artifact_not_external_only")
    expected = canonical_artifact_sha256(value)
    actual = str(value.get("artifact_sha256") or "")
    if not actual:
        issues.append("artifact_sha256_missing")
    elif expected != actual:
        issues.append("artifact_sha256_mismatch")
    if not isinstance(value.get("entities"), list) or not value.get("entities"):
        issues.append("artifact_entities_missing")
    if not isinstance(value.get("provider_families"), Mapping):
        issues.append("artifact_provider_families_missing")
    return {"ok": not issues, "issues": issues, "artifact_sha256": actual, "computed_sha256": expected}


def validate_runtime_approved_artifact(artifact: Mapping[str, Any] | None) -> dict[str, Any]:
    """Require an artifact to declare its own approved runtime eligibility.

    An active pointer is approval lineage, not a mechanism to relabel a
    research/static artifact as production-ready in memory.  Keeping this
    check separate preserves read-only research validation while making the
    runtime loader fail closed on a pointer to any research-only candidate.
    """

    value = artifact if isinstance(artifact, Mapping) else {}
    base = validate_artifact(value)
    issues = list(base["issues"])
    if value.get("research_only") is True:
        issues.append("artifact_research_only")
    if value.get("production_ready") is not True:
        issues.append("artifact_production_ready_not_true")
    if value.get("runtime_integrated") is not True:
        issues.append("artifact_runtime_integrated_not_true")
    return {
        "ok": not issues,
        "issues": sorted(set(issues)),
        "artifact_sha256": base["artifact_sha256"],
        "computed_sha256": base["computed_sha256"],
    }


def load_approved_forecast_source_weight_artifact(
    *,
    active_pointer_path: Path = DEFAULT_ACTIVE_POINTER,
    registry_root: Path = DEFAULT_REGISTRY_ROOT,
) -> dict[str, Any] | None:
    """Load a fully approved artifact without mutating any runtime state."""

    pointer_path = Path(active_pointer_path)
    if not pointer_path.exists():
        return None
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(pointer, Mapping) or pointer.get("schema") != ACTIVE_SCHEMA:
        return None
    if pointer.get("approval_state") != "approved" or pointer.get("explicit_user_approval") is not True:
        return None
    artifact_sha = str(pointer.get("artifact_sha256") or "")
    if not artifact_sha:
        return None
    artifact_path = Path(registry_root) / f"{artifact_sha}.json"
    try:
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    contract = validate_runtime_approved_artifact(artifact if isinstance(artifact, Mapping) else None)
    if not contract["ok"] or contract["artifact_sha256"] != artifact_sha:
        return None
    materialized = dict(artifact)
    materialized.update(
        {
            "production_ready": True,
            "runtime_integrated": True,
            "approval_state": "approved",
            "approval_id": str(pointer.get("approval_id") or ""),
            "active_pointer_sha256": hashlib.sha256(pointer_path.read_bytes()).hexdigest(),
        }
    )
    return materialized


__all__ = [
    "ACTIVE_SCHEMA",
    "DEFAULT_ACTIVE_POINTER",
    "DEFAULT_REGISTRY_ROOT",
    "canonical_artifact_sha256",
    "load_approved_forecast_source_weight_artifact",
    "validate_artifact",
    "validate_runtime_approved_artifact",
]
