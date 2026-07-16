"""Content-addressed policy for v6 L3 realization projection only."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = "realization_state_policy_v2"
DEFAULT_PATH = (
    Path(__file__).resolve().parent
    / "config/trading/production_probability_v6_realization_policy.json"
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )


def artifact_sha256(value: Mapping[str, Any]) -> str:
    unsigned = {
        key: item for key, item in _mapping(value).items() if key != "artifact_sha256"
    }
    return hashlib.sha256(_canonical(unsigned).encode("utf-8")).hexdigest()


def validate_v6_realization_policy(value: Mapping[str, Any]) -> dict[str, Any]:
    policy = dict(_mapping(value))
    issues: list[str] = []
    if _text(policy.get("schema")) != SCHEMA_VERSION:
        issues.append("v6_realization_policy_schema_invalid")
    if _text(policy.get("approval_state")) != "approved":
        issues.append("v6_realization_policy_not_approved")
    if _text(policy.get("scope")) != "production_v6_market_projection_only":
        issues.append("v6_realization_policy_scope_invalid")
    for key in ("credible_probability_floor", "strong_exclusion_probability_ceiling"):
        try:
            number = float(policy.get(key))
        except (TypeError, ValueError):
            number = float("nan")
        if not math.isfinite(number) or not 0.0 <= number <= 1.0:
            issues.append(f"v6_realization_policy_{key}_invalid")
    try:
        support = float(policy.get("strong_support_log_probability_ratio_floor"))
        exclusion = float(policy.get("strong_exclusion_log_probability_ratio_ceiling"))
    except (TypeError, ValueError):
        support = exclusion = float("nan")
    if not math.isfinite(support):
        issues.append("v6_realization_policy_support_ratio_invalid")
    if not math.isfinite(exclusion) or exclusion >= 0.0:
        issues.append("v6_realization_policy_exclusion_ratio_invalid")
    if (
        policy.get("ev_edge_or_market_price_consumed") is not False
        or policy.get("places_orders") is not False
        or policy.get("authorizes_clob") is not False
    ):
        issues.append("v6_realization_policy_boundary_invalid")
    supplied = _text(policy.get("artifact_sha256"))
    if len(supplied) != 64 or supplied != artifact_sha256(policy):
        issues.append("v6_realization_policy_artifact_sha_invalid")
    if issues:
        raise ValueError("v6_realization_policy_invalid:" + ",".join(sorted(set(issues))))
    return policy


def load_v6_realization_policy(
    path: Path | str = DEFAULT_PATH,
) -> dict[str, Any]:
    source = Path(path).expanduser().resolve()
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"v6_realization_policy_unreadable:{type(exc).__name__}"
        ) from exc
    return validate_v6_realization_policy(_mapping(value))


__all__ = [
    "DEFAULT_PATH",
    "SCHEMA_VERSION",
    "artifact_sha256",
    "load_v6_realization_policy",
    "validate_v6_realization_policy",
]
