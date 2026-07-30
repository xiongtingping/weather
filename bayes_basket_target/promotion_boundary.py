"""Separate research promotion from the currently active runtime artifact.

M8-10 requires a failed calibration/OOS gate to block only a *candidate*
promotion.  A runtime must consume an already activated artifact and must not
consult the research gate while compiling a decision.  This module provides a
small content-addressed boundary receipt used by the offline audit.
"""

from __future__ import annotations

from typing import Any, Mapping

from .contracts import ContractError, content_sha256


SCHEMA = "bayes_basket_target_promotion_boundary_v1"


def _text(name: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"{name}_missing")
    return result


def _checks(value: Any) -> dict[str, bool]:
    if not isinstance(value, Mapping) or not value:
        raise ContractError("promotion_boundary_research_checks_missing")
    result = {str(key): item for key, item in value.items()}
    if any(not isinstance(item, bool) for item in result.values()):
        raise ContractError("promotion_boundary_research_check_invalid")
    return result


def build_promotion_boundary_decision(
    *,
    active_artifact: Mapping[str, Any],
    research_gate: Mapping[str, Any],
    candidate_artifact: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a decision where research state cannot disable active runtime."""

    if not isinstance(active_artifact, Mapping) or not isinstance(candidate_artifact, Mapping):
        raise ContractError("promotion_boundary_artifact_mapping_required")
    active_id = _text("active_artifact_id", active_artifact.get("artifact_id"))
    active_sha = _text("active_artifact_sha256", active_artifact.get("content_sha256"))
    if len(active_sha) != 64:
        raise ContractError("promotion_boundary_active_artifact_sha256_invalid")
    active_state = str(active_artifact.get("state") or "").upper()
    if active_state not in {"ACTIVE", "ENABLED", "PROMOTED"}:
        raise ContractError("promotion_boundary_active_artifact_not_active")
    if active_artifact.get("research_gate_dependency", False) is not False:
        raise ContractError("promotion_boundary_active_reads_research_gate")
    checks = _checks(research_gate.get("checks"))
    candidate_id = _text("candidate_artifact_id", candidate_artifact.get("artifact_id"))
    request = research_gate.get("promotion_requested", False)
    if not isinstance(request, bool):
        raise ContractError("promotion_boundary_promotion_requested_invalid")
    candidate_allowed = bool(request and all(checks.values()))
    identity = {
        "schema": SCHEMA,
        "active_artifact_id": active_id,
        "active_artifact_sha256": active_sha,
        "active_runtime_allowed": True,
        "active_runtime_reads_research_gate": False,
        "candidate_artifact_id": candidate_id,
        "research_checks": checks,
        "promotion_requested": request,
        "candidate_promotion_allowed": candidate_allowed,
        "live_order_authorized": False,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


__all__ = ["SCHEMA", "build_promotion_boundary_decision"]
