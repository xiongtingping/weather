"""Pure promotion compiler for a reviewed X->S bridge candidate."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Mapping

from resolution_bridge_activation_runtime import SCHEMA_VERSION as ACTIVATION_SCHEMA, activation_sha256
from resolution_bridge_artifact_runtime import (
    CANDIDATE_STATE,
    PRODUCTION_STATE,
    artifact_sha256,
    validate_resolution_bridge_artifact,
)


SCHEMA_VERSION = "resolution_bridge_promotion_v1"
APPROVAL_SCHEMA = "resolution_bridge_promotion_approval_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _time(value: Any) -> bool:
    try:
        parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def approval_receipt_sha256(value: Mapping[str, Any]) -> str:
    return _sha({key: item for key, item in _mapping(value).items() if key != "approval_receipt_sha256"})


def compile_resolution_bridge_promotion(
    *,
    candidate_artifact: Mapping[str, Any],
    approval_receipt: Mapping[str, Any],
    artifact_path: str,
) -> dict[str, Any]:
    try:
        candidate = validate_resolution_bridge_artifact(candidate_artifact)
    except ValueError as exc:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": [str(exc)], "places_orders": False}
    receipt = dict(_mapping(approval_receipt))
    blockers: list[str] = []
    if _text(candidate.get("state")) != CANDIDATE_STATE:
        blockers.append("bridge_promotion_input_not_candidate")
    if _text(receipt.get("schema")) != APPROVAL_SCHEMA or _text(receipt.get("state")) != "accepted":
        blockers.append("bridge_promotion_approval_not_accepted")
    if _text(receipt.get("candidate_artifact_sha256")) != _text(candidate.get("artifact_sha256")):
        blockers.append("bridge_promotion_candidate_sha_mismatch")
    if receipt.get("oos_review_accepted") is not True or receipt.get("coverage_review_accepted") is not True:
        blockers.append("bridge_promotion_reviews_not_accepted")
    if not _text(receipt.get("approval_id")) or not _text(receipt.get("reviewer")) or not _time(receipt.get("approved_at")):
        blockers.append("bridge_promotion_approval_lineage_invalid")
    supplied_receipt_sha = _text(receipt.get("approval_receipt_sha256"))
    if supplied_receipt_sha != approval_receipt_sha256(receipt):
        blockers.append("bridge_promotion_approval_receipt_sha_invalid")
    if not _text(artifact_path):
        blockers.append("bridge_promotion_artifact_path_missing")
    if blockers:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": sorted(set(blockers)),
            "places_orders": False,
        }
    production_core = {
        **candidate,
        "state": PRODUCTION_STATE,
        "bridge_closed": True,
        "validated": True,
        "production_runtime_eligible": True,
        "shadow_only": False,
        "approval_id": _text(receipt.get("approval_id")),
        "approval_receipt_sha256": supplied_receipt_sha,
        "approved_at": _text(receipt.get("approved_at")),
    }
    production_core.pop("artifact_sha256", None)
    artifact = {**production_core, "artifact_sha256": artifact_sha256(production_core)}
    try:
        artifact = validate_resolution_bridge_artifact(artifact, require_production=True)
    except ValueError as exc:
        return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "block_reasons": [str(exc)], "places_orders": False}
    activation_core = {
        "schema": ACTIVATION_SCHEMA,
        "state": "production_enabled",
        "artifact_path": _text(artifact_path),
        "artifact_sha256": _text(artifact.get("artifact_sha256")),
        "approval_id": _text(receipt.get("approval_id")),
        "approval_receipt_sha256": supplied_receipt_sha,
        "places_orders": False,
        "authorizes_clob": False,
    }
    activation = {**activation_core, "activation_sha256": activation_sha256(activation_core)}
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "promotion_compiled",
        "production_artifact": artifact,
        "production_activation": activation,
        "places_orders": False,
        "boundary": {
            "writes_files": False,
            "writes_posterior": False,
            "enables_market_projection_only": True,
            "authorizes_clob": False,
            "places_orders": False,
        },
    }


__all__ = [
    "APPROVAL_SCHEMA",
    "SCHEMA_VERSION",
    "approval_receipt_sha256",
    "compile_resolution_bridge_promotion",
]
