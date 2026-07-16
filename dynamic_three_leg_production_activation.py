"""Strict activation artifact for the unique production three-leg lane."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = "dynamic_three_leg_production_activation_v1"
DEFAULT_PATH = Path(__file__).resolve().parent / "config" / "trading" / "dynamic_three_leg_production_activation.json"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def materialize_dynamic_three_leg_activation(value: Mapping[str, Any]) -> dict[str, Any]:
    core = dict(value)
    core.pop("activation_contract_sha256", None)
    return {**core, "activation_contract_sha256": _sha(core)}


def validate_dynamic_three_leg_activation(value: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(value)
    supplied = _text(row.pop("activation_contract_sha256", ""))
    expected = _sha(row)
    issues = []
    if row.get("schema") != SCHEMA_VERSION or _text(row.get("state")) != "enabled":
        issues.append("dynamic_three_leg_activation_not_enabled")
    if not _text(row.get("strategy_artifact_sha256")) or not _text(row.get("cutover_artifact_sha256")):
        issues.append("dynamic_three_leg_activation_artifact_identity_missing")
    if row.get("risk_reservation_write_authorized") is not True:
        issues.append("dynamic_three_leg_activation_risk_reservation_authority_missing")
    if row.get("live_decision_write_authorized") is not True or row.get("live_submit_authorized") is not True:
        issues.append("dynamic_three_leg_activation_live_authority_missing")
    if not supplied or supplied != expected:
        issues.append("dynamic_three_leg_activation_contract_sha256_invalid")
    if issues:
        raise ValueError(",".join(sorted(set(issues))))
    return {**row, "activation_contract_sha256": supplied}


def load_dynamic_three_leg_activation(path: str | Path = DEFAULT_PATH) -> dict[str, Any]:
    raw = json.loads(Path(path).expanduser().resolve().read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError("dynamic_three_leg_activation_not_mapping")
    return validate_dynamic_three_leg_activation(raw)


__all__ = [
    "DEFAULT_PATH",
    "SCHEMA_VERSION",
    "load_dynamic_three_leg_activation",
    "materialize_dynamic_three_leg_activation",
    "validate_dynamic_three_leg_activation",
]
