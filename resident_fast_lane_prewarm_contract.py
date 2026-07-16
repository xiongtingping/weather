"""Identity and freshness contract for resident P10 dependencies.

Boolean ``prewarmed=True`` is not sufficient evidence: a resident worker must
prove which forecast/prior/strong-artifact context the probability update is
using and whether that context remains valid at dispatch time. Market context
is a separate X→S→B/decision dependency and can never block the physical p(X)
update. This pure contract does not load, refresh, or mutate any dependency.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Mapping


SCHEMA_VERSION = "resident_fast_lane_prewarm_contract_v1"

# P8/P9 own inference and durable decision intent.  A signer or CLOB
# connection has no Bayes input and must not become a hidden readiness
# dependency of the shadow posterior path. L5 checks it separately.
PROBABILITY_REQUIRED_COMPONENTS: dict[str, tuple[str, ...]] = {
    "forecast_snapshot": ("forecast_snapshot_id", "artifact_sha256", "valid_until"),
    "prior_context": ("prior_context_id", "artifact_sha256", "valid_until"),
    "strong_lr_artifact": ("strong_lr_artifact_sha256", "valid_until"),
}
MARKET_PROJECTION_REQUIRED_COMPONENTS: dict[str, tuple[str, ...]] = {
    "market_context": ("market_snapshot_id", "fresh_until"),
}
EXECUTION_REQUIRED_COMPONENTS: dict[str, tuple[str, ...]] = {
    "resident_signer_connection_pool": ("pool_id", "valid_until"),
}
# The public name means the probability-SSOT set, never the trading set.
REQUIRED_COMPONENTS = PROBABILITY_REQUIRED_COMPONENTS
PROBABILITY_FRESH_UNTIL_FIELD = {
    "forecast_snapshot": "valid_until",
    "prior_context": "valid_until",
    "strong_lr_artifact": "valid_until",
}
MARKET_PROJECTION_FRESH_UNTIL_FIELD = {
    "market_context": "fresh_until",
}
FRESH_UNTIL_FIELD = PROBABILITY_FRESH_UNTIL_FIELD
EXECUTION_FRESH_UNTIL_FIELD = {"resident_signer_connection_pool": "valid_until"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _parse_dt(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _identity_sha256(components: Mapping[str, Mapping[str, Any]]) -> str:
    public = {
        component: {
            key: value
            for key, value in sorted(_mapping(detail).items())
            if key not in {"credential", "secret", "private_key", "access_token"}
        }
        for component, detail in sorted(components.items())
    }
    encoded = json.dumps(public, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _validate_components(
    components: Mapping[str, Any] | None,
    *,
    decision_as_of: Any = "",
    required_components: Mapping[str, tuple[str, ...]],
    freshness_fields: Mapping[str, str],
    purpose: str,
) -> dict[str, Any]:
    """Validate immutable identities and freshness without any clock synthesis."""

    supplied = _mapping(components)
    as_of = _parse_dt(decision_as_of) if _text(decision_as_of) else None
    blockers: list[str] = []
    normalized: dict[str, dict[str, Any]] = {}
    for name, required_fields in required_components.items():
        value = _mapping(supplied.get(name))
        normalized[name] = value
        if value.get("ready") is not True:
            blockers.append(f"resident_prewarm_{name}_not_ready")
        for field in required_fields:
            if not _text(value.get(field)):
                blockers.append(f"resident_prewarm_{name}_{field}_missing")
        # ``ready`` binds the currently activated runtime artifact.  Offline
        # strict-as-of/OOS approval governs version promotion, not whether a
        # live event can be traded after its posterior has been committed.
        freshness_field = freshness_fields[name]
        fresh_until = _parse_dt(value.get(freshness_field))
        if fresh_until is None:
            blockers.append(f"resident_prewarm_{name}_{freshness_field}_invalid")
        elif as_of is not None and fresh_until < as_of:
            blockers.append(f"resident_prewarm_{name}_stale_at_dispatch")
    extra = sorted(set(supplied) - set(required_components))
    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "state": "ready" if not blockers else "blocked",
        "blockers": sorted(set(blockers)),
        "decision_as_of": _text(decision_as_of),
        "dependency_identity_sha256": _identity_sha256(normalized),
        "purpose": purpose,
        "component_ids": {
            name: {
                field: _text(detail.get(field))
                for field in required_components[name]
                if field not in {"valid_until", "fresh_until"}
            }
            for name, detail in normalized.items()
        },
        "extra_component_keys": extra,
        "boundary": {
            "loads_or_refreshes_dependencies": False,
            "accepts_boolean_prewarm_claims": False,
            "contains_credentials": False,
        },
    }


def validate_resident_fast_lane_prewarm(
    components: Mapping[str, Any] | None,
    *,
    decision_as_of: Any = "",
) -> dict[str, Any]:
    """Validate only probability-SSOT dependencies for P0/P8."""

    return _validate_components(
        components,
        decision_as_of=decision_as_of,
        required_components=PROBABILITY_REQUIRED_COMPONENTS,
        freshness_fields=PROBABILITY_FRESH_UNTIL_FIELD,
        purpose="probability_ssot_inference",
    )


def validate_resident_market_projection_prewarm(
    components: Mapping[str, Any] | None,
    *,
    decision_as_of: Any = "",
) -> dict[str, Any]:
    """Validate X→S→B/decision context separately from physical p(X)."""

    return _validate_components(
        components,
        decision_as_of=decision_as_of,
        required_components=MARKET_PROJECTION_REQUIRED_COMPONENTS,
        freshness_fields=MARKET_PROJECTION_FRESH_UNTIL_FIELD,
        purpose="market_projection_and_decision",
    )


def validate_resident_execution_prewarm(
    components: Mapping[str, Any] | None,
    *,
    decision_as_of: Any = "",
) -> dict[str, Any]:
    """Validate L5 signing/connection residency, never a Bayes dependency."""

    return _validate_components(
        components,
        decision_as_of=decision_as_of,
        required_components=EXECUTION_REQUIRED_COMPONENTS,
        freshness_fields=EXECUTION_FRESH_UNTIL_FIELD,
        purpose="l5_execution",
    )


__all__ = [
    "FRESH_UNTIL_FIELD",
    "MARKET_PROJECTION_FRESH_UNTIL_FIELD",
    "MARKET_PROJECTION_REQUIRED_COMPONENTS",
    "PROBABILITY_FRESH_UNTIL_FIELD",
    "PROBABILITY_REQUIRED_COMPONENTS",
    "EXECUTION_FRESH_UNTIL_FIELD",
    "EXECUTION_REQUIRED_COMPONENTS",
    "REQUIRED_COMPONENTS",
    "SCHEMA_VERSION",
    "validate_resident_fast_lane_prewarm",
    "validate_resident_market_projection_prewarm",
    "validate_resident_execution_prewarm",
]
