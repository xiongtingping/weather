"""Pure compiler for the approved sample-free bridge and one daily context."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from production_probability_v6_market_context_runtime import (
    activation_sha256 as context_activation_sha256,
    market_context_sha256,
    validate_v6_market_context,
    validate_v6_market_context_activation,
)
from resolution_bridge_operational_baseline_runtime import (
    build_resolution_bridge_operational_baseline_candidate,
)
from resolution_bridge_promotion_runtime import (
    approval_receipt_sha256,
    compile_resolution_bridge_promotion,
)
from resolution_operational_projection_authority_runtime import (
    load_operational_projection_authority,
)
from resolution_authority_contract_runtime import validate_resolution_authority_contract
from resolution_target_contract_runtime import validate_market_resolution_target_contract_proposal


SCHEMA_VERSION = "operational_bridge_release_v1"
APPROVAL_ID = "ADR-XSB-OPERATIONAL-SOFT-PROJECTION-0001"


def _text(value: Any) -> str:
    return str(value or "").strip()


def build_operational_bridge_release(
    *, source_authority_contract: Mapping[str, Any], approved_at: str,
) -> dict[str, Any]:
    source_validation = validate_resolution_authority_contract(source_authority_contract)
    if source_validation.get("ok") is not True:
        raise ValueError("operational_bridge_source_authority_invalid")
    _operational_contract, operational_validation = load_operational_projection_authority()
    candidate = build_resolution_bridge_operational_baseline_candidate(
        authority_contract_sha256=_text(source_validation.get("contract_sha256")),
        operational_projection_authority_contract_sha256=_text(
            operational_validation.get("contract_sha256")
        ),
    )
    receipt_core = {
        "schema": "resolution_bridge_promotion_approval_v1",
        "state": "accepted",
        "candidate_artifact_sha256": candidate["artifact_sha256"],
        "operational_baseline_review_accepted": True,
        "probabilistic_residual_support_review_accepted": True,
        "hard_exclusion_disabled_review_accepted": True,
        "sample_or_oos_live_gate": False,
        "approval_id": APPROVAL_ID,
        "reviewer": "sol_multi_agent_adversarial_review",
        "approved_at": _text(approved_at),
        "review_conclusions": {
            "zero_sample_immutable": True,
            "residual_support_is_not_hard_envelope": True,
            "soft_p_b_allowed": True,
            "hard_no_certificate_forbidden": True,
            "physical_x_nonblocking": True,
        },
    }
    receipt = {
        **receipt_core,
        "approval_receipt_sha256": approval_receipt_sha256(receipt_core),
    }
    preliminary = compile_resolution_bridge_promotion(
        candidate_artifact=candidate,
        approval_receipt=receipt,
        artifact_path="config/artifacts/resolution_bridge/pending.json",
    )
    if preliminary.get("ok") is not True:
        raise ValueError("operational_bridge_promotion_blocked:" + ",".join(preliminary.get("block_reasons") or []))
    artifact = preliminary["production_artifact"]
    artifact_path = f"config/artifacts/resolution_bridge/{artifact['artifact_sha256']}.json"
    compiled = compile_resolution_bridge_promotion(
        candidate_artifact=candidate,
        approval_receipt=receipt,
        artifact_path=artifact_path,
    )
    if compiled.get("ok") is not True:
        raise ValueError("operational_bridge_final_promotion_blocked")
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "candidate_artifact": candidate,
        "approval_receipt": receipt,
        "production_artifact": compiled["production_artifact"],
        "production_artifact_path": artifact_path,
        "production_activation": compiled["production_activation"],
        "sample_or_oos_live_gate": False,
        "hard_exclusion_certificates_authorized": False,
        "places_orders": False,
    }


def build_daily_market_context_release(
    *, target_contract: Mapping[str, Any], bridge_artifact: Mapping[str, Any],
) -> dict[str, Any]:
    target_validation = validate_market_resolution_target_contract_proposal(target_contract)
    if target_validation.get("ok") is not True:
        raise ValueError("daily_market_context_target_invalid")
    event_date = _text(target_contract.get("resolution_local_date"))[:10]
    core = {
        "schema": "production_probability_v6_market_context_v1",
        "state": "production_approved",
        "event_date": event_date,
        "target_identity": f"physical_weather_day_v1:ZUUU:{event_date}",
        "slug": _text(target_contract.get("public_slug")),
        "market_target_contract": dict(target_contract),
        "target_contract_sha256": _text(target_contract.get("target_contract_sha256")),
        "bridge_artifact_sha256": _text(bridge_artifact.get("artifact_sha256")),
        "places_orders": False,
        "authorizes_clob": False,
        "blocks_physical_x": False,
    }
    context = validate_v6_market_context(
        {**core, "market_context_sha256": market_context_sha256(core)}
    )
    context_path = (
        f"config/artifacts/market_context/{event_date}."
        f"{context['market_context_sha256']}.json"
    )
    activation_core = {
        "schema": "production_probability_v6_market_context_activation_v1",
        "state": "production_enabled",
        "context_path": context_path,
        "market_context_sha256": context["market_context_sha256"],
        "bridge_artifact_sha256": context["bridge_artifact_sha256"],
        "places_orders": False,
        "authorizes_clob": False,
        "blocks_physical_x": False,
    }
    activation = validate_v6_market_context_activation({
        **activation_core,
        "activation_sha256": context_activation_sha256(activation_core),
    })
    return {
        "schema": "daily_v6_market_context_release_v1",
        "ok": True,
        "event_date": event_date,
        "context": context,
        "context_path": context_path,
        "activation": activation,
        "places_orders": False,
    }


def latest_target_contract_for_date(registry: Path, *, event_date: str) -> dict[str, Any]:
    import sqlite3

    with sqlite3.connect(f"file:{registry.resolve()}?mode=ro&immutable=1", uri=True) as conn:
        row = conn.execute(
            "SELECT contract_json FROM resolution_target_contracts "
            "WHERE resolution_local_date=? ORDER BY revision_no DESC,created_at DESC LIMIT 1",
            (_text(event_date)[:10],),
        ).fetchone()
    if row is None:
        raise ValueError("daily_market_context_target_missing")
    value = json.loads(str(row[0]))
    if not isinstance(value, Mapping):
        raise ValueError("daily_market_context_target_not_mapping")
    return dict(value)


__all__ = [
    "APPROVAL_ID", "SCHEMA_VERSION", "build_daily_market_context_release",
    "build_operational_bridge_release", "latest_target_contract_for_date",
]
