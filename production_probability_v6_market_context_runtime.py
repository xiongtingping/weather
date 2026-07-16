"""Immutable production context for projecting one physical v6 posterior to B."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from resolution_target_contract_runtime import (
    validate_market_resolution_target_contract_proposal,
)


SCHEMA_VERSION = "production_probability_v6_market_context_v1"
ACTIVATION_SCHEMA = "production_probability_v6_market_context_activation_v1"
DEFAULT_ACTIVATION_PATH = (
    Path(__file__).resolve().parent
    / "config/trading/production_probability_v6_market_context_activation.json"
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def market_context_sha256(value: Mapping[str, Any]) -> str:
    return _sha(
        {
            key: item
            for key, item in _mapping(value).items()
            if key != "market_context_sha256"
        }
    )


def activation_sha256(value: Mapping[str, Any]) -> str:
    return _sha(
        {
            key: item
            for key, item in _mapping(value).items()
            if key != "activation_sha256"
        }
    )


def validate_v6_market_context(value: Mapping[str, Any]) -> dict[str, Any]:
    context = dict(_mapping(value))
    issues: list[str] = []
    if _text(context.get("schema")) != SCHEMA_VERSION:
        issues.append("v6_market_context_schema_invalid")
    if _text(context.get("state")) != "production_approved":
        issues.append("v6_market_context_not_production_approved")
    event_date = _text(context.get("event_date"))[:10]
    target_identity = _text(context.get("target_identity"))
    if target_identity != f"physical_weather_day_v1:ZUUU:{event_date}":
        issues.append("v6_market_context_physical_target_mismatch")
    if not _text(context.get("slug")):
        issues.append("v6_market_context_slug_missing")
    target = dict(_mapping(context.get("market_target_contract")))
    validation = validate_market_resolution_target_contract_proposal(target)
    if validation.get("ok") is not True:
        issues.append("v6_market_context_target_contract_invalid")
    if _text(target.get("resolution_local_date"))[:10] != event_date:
        issues.append("v6_market_context_target_date_mismatch")
    if _text(context.get("target_contract_sha256")) != _text(
        target.get("target_contract_sha256")
    ):
        issues.append("v6_market_context_target_sha_mismatch")
    if len(_text(context.get("bridge_artifact_sha256"))) != 64:
        issues.append("v6_market_context_bridge_artifact_sha_invalid")
    if (
        context.get("places_orders") is not False
        or context.get("authorizes_clob") is not False
        or context.get("blocks_physical_x") is not False
    ):
        issues.append("v6_market_context_boundary_invalid")
    supplied = _text(context.get("market_context_sha256"))
    if supplied != market_context_sha256(context):
        issues.append("v6_market_context_sha_invalid")
    if issues:
        raise ValueError("v6_market_context_invalid:" + ",".join(sorted(set(issues))))
    return context


def validate_v6_market_context_activation(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    activation = dict(_mapping(value))
    issues: list[str] = []
    if _text(activation.get("schema")) != ACTIVATION_SCHEMA:
        issues.append("v6_market_context_activation_schema_invalid")
    state = _text(activation.get("state"))
    if state not in {"disabled", "production_enabled"}:
        issues.append("v6_market_context_activation_state_invalid")
    if (
        activation.get("places_orders") is not False
        or activation.get("authorizes_clob") is not False
        or activation.get("blocks_physical_x") is not False
    ):
        issues.append("v6_market_context_activation_boundary_invalid")
    if state == "disabled":
        if _text(activation.get("context_path")) or _text(
            activation.get("market_context_sha256")
        ):
            issues.append("v6_market_context_disabled_pointer_present")
    elif (
        not _text(activation.get("context_path"))
        or len(_text(activation.get("market_context_sha256"))) != 64
        or len(_text(activation.get("bridge_artifact_sha256"))) != 64
    ):
        issues.append("v6_market_context_enabled_pointer_invalid")
    supplied = _text(activation.get("activation_sha256"))
    if supplied and supplied != activation_sha256(activation):
        issues.append("v6_market_context_activation_sha_invalid")
    if issues:
        raise ValueError(
            "v6_market_context_activation_invalid:" + ",".join(sorted(set(issues)))
        )
    activation["activation_sha256"] = activation_sha256(activation)
    return activation


def load_active_v6_market_context(
    *,
    activation_path: Path | str = DEFAULT_ACTIVATION_PATH,
    root: Path | str | None = None,
    event_date: str,
) -> dict[str, Any]:
    source = Path(activation_path).expanduser().resolve()
    activation = validate_v6_market_context_activation(
        json.loads(source.read_text(encoding="utf-8"))
    )
    if _text(activation.get("state")) == "disabled":
        return {
            "schema": SCHEMA_VERSION,
            "ok": True,
            "state": "disabled",
            "market_context": None,
            "market_projection_available": False,
            "na_reason": "market_projection_context_disabled",
            "blocks_physical_x": False,
            "places_orders": False,
        }
    base = (
        Path(root).expanduser().resolve()
        if root is not None
        else Path(__file__).resolve().parent
    )
    context_path = Path(_text(activation.get("context_path")))
    if not context_path.is_absolute():
        context_path = base / context_path
    context = validate_v6_market_context(
        json.loads(context_path.read_text(encoding="utf-8"))
    )
    if (
        _text(context.get("event_date"))[:10] != _text(event_date)[:10]
        or _text(context.get("market_context_sha256"))
        != _text(activation.get("market_context_sha256"))
        or _text(context.get("bridge_artifact_sha256"))
        != _text(activation.get("bridge_artifact_sha256"))
    ):
        raise ValueError("v6_market_context_activation_binding_mismatch")
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "available",
        "market_context": context,
        "market_projection_available": True,
        "na_reason": "",
        "activation": activation,
        "blocks_physical_x": False,
        "places_orders": False,
    }


def resolve_active_v6_market_context(
    *,
    activation_path: Path | str = DEFAULT_ACTIVATION_PATH,
    root: Path | str | None = None,
    event_date: str,
) -> dict[str, Any]:
    """Resolve the optional market branch without ever blocking physical X.

    Invalid or stale market configuration is an explicit market-projection
    failure, not an exception that may escape into the AMSC physical ingress.
    """

    try:
        return load_active_v6_market_context(
            activation_path=activation_path,
            root=root,
            event_date=event_date,
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "unavailable",
            "market_context": None,
            "market_projection_available": False,
            "na_reason": f"market_projection_context_invalid:{type(exc).__name__}",
            "block_reasons": [_text(exc) or type(exc).__name__],
            "blocks_physical_x": False,
            "places_orders": False,
        }


__all__ = [
    "ACTIVATION_SCHEMA",
    "DEFAULT_ACTIVATION_PATH",
    "SCHEMA_VERSION",
    "activation_sha256",
    "load_active_v6_market_context",
    "market_context_sha256",
    "resolve_active_v6_market_context",
    "validate_v6_market_context",
    "validate_v6_market_context_activation",
]
