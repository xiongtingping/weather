"""Prewarm optional v6 market inputs outside the archive writer lock."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from production_probability_v6_market_context_runtime import (
    DEFAULT_ACTIVATION_PATH as DEFAULT_MARKET_CONTEXT_ACTIVATION,
    resolve_active_v6_market_context,
)
from production_probability_v6_p0_input_runtime import (
    DEFAULT_RESOLUTION_AUTHORITY,
    build_physical_only_resolution_contract,
)
from production_probability_v6_realization_policy_runtime import (
    DEFAULT_PATH as DEFAULT_REALIZATION_POLICY,
    load_v6_realization_policy,
)
from resolution_bridge_activation_runtime import (
    DEFAULT_PATH as DEFAULT_BRIDGE_ACTIVATION,
    load_active_resolution_bridge_artifact,
)


SCHEMA_VERSION = "production_probability_v6_market_inputs_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def prewarm_v6_market_inputs(
    *,
    event_date: str,
    market_context_activation_path: Path | str = DEFAULT_MARKET_CONTEXT_ACTIVATION,
    bridge_activation_path: Path | str = DEFAULT_BRIDGE_ACTIVATION,
    realization_policy_path: Path | str = DEFAULT_REALIZATION_POLICY,
    resolution_authority_path: Path | str = DEFAULT_RESOLUTION_AUTHORITY,
    root: Path | str | None = None,
) -> dict[str, Any]:
    context = resolve_active_v6_market_context(
        activation_path=market_context_activation_path,
        root=root,
        event_date=event_date,
    )
    if context.get("market_projection_available") is not True:
        return {
            "schema": SCHEMA_VERSION,
            "ok": True,
            "state": "physical_only",
            "market_projection_available": False,
            "market_projection_na_reason": _text(context.get("na_reason"))
            or "market_projection_context_unavailable",
            "market_context_result": context,
            "bridge_activation": None,
            "bridge_artifact": None,
            "physical_resolution_contract": None,
            "realization_policy": None,
            "blocks_physical_x": False,
            "places_orders": False,
        }
    try:
        bridge_activation, bridge_artifact = load_active_resolution_bridge_artifact(
            activation_path=bridge_activation_path,
            root=root,
        )
        if bridge_artifact is None:
            raise ValueError("v6_market_inputs_bridge_disabled")
        policy = load_v6_realization_policy(realization_policy_path)
        physical = build_physical_only_resolution_contract(
            resolution_authority_path,
            event_date=event_date,
        )
        market_context = context.get("market_context") or {}
        if (
            _text(market_context.get("bridge_artifact_sha256"))
            != _text(bridge_artifact.get("artifact_sha256"))
            or _text(physical.get("resolution_local_date"))[:10]
            != _text(event_date)[:10]
        ):
            raise ValueError("v6_market_inputs_content_binding_mismatch")
    except (OSError, TypeError, ValueError) as exc:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "market_unavailable_physical_only",
            "market_projection_available": False,
            "market_projection_na_reason": f"market_inputs_unavailable:{type(exc).__name__}",
            "market_block_reasons": [_text(exc) or type(exc).__name__],
            "market_context_result": context,
            "bridge_activation": None,
            "bridge_artifact": None,
            "physical_resolution_contract": None,
            "realization_policy": None,
            "blocks_physical_x": False,
            "places_orders": False,
        }
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "market_inputs_ready",
        "market_projection_available": True,
        "market_projection_na_reason": "",
        "market_context_result": context,
        "bridge_activation": bridge_activation,
        "bridge_artifact": bridge_artifact,
        "physical_resolution_contract": physical,
        "realization_policy": policy,
        "blocks_physical_x": False,
        "places_orders": False,
    }


__all__ = ["SCHEMA_VERSION", "prewarm_v6_market_inputs"]
