"""Strict activation contract for the independent top-YES wave exit lane."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = "market_alpha_take_profit_production_activation_v1"
DEFAULT_PATH = Path(__file__).resolve().parent / "config" / "trading" / "market_alpha_take_profit_production_activation.json"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def materialize_market_alpha_take_profit_activation(value: Mapping[str, Any]) -> dict[str, Any]:
    core = dict(value)
    core.pop("activation_contract_sha256", None)
    return {**core, "activation_contract_sha256": _sha(core)}


def validate_market_alpha_take_profit_activation(value: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(value)
    supplied = _text(row.pop("activation_contract_sha256", ""))
    expected = _sha(row)
    issues: list[str] = []
    if row.get("schema") != SCHEMA_VERSION or _text(row.get("state")) != "enabled":
        issues.append("market_alpha_activation_not_enabled")
    if not _text(row.get("strategy_artifact_sha256")) or not _text(row.get("cutover_artifact_sha256")):
        issues.append("market_alpha_activation_artifact_identity_missing")
    if row.get("live_decision_write_authorized") is not True or row.get("live_submit_authorized") is not True:
        issues.append("market_alpha_activation_live_authority_missing")
    if _text(row.get("reason_family")) != "market_alpha_take_profit":
        issues.append("market_alpha_activation_reason_family_invalid")
    if _text(row.get("reason_code")) != "market_alpha_take_profit_top_yes_wave_completed":
        issues.append("market_alpha_activation_reason_code_invalid")
    try:
        target = float(row.get("frozen_take_profit_price"))
    except (TypeError, ValueError):
        target = -1.0
    if abs(target - 0.90) > 1e-12:
        issues.append("market_alpha_activation_take_profit_price_invalid")
    if not supplied or supplied != expected:
        issues.append("market_alpha_activation_contract_sha256_invalid")
    if issues:
        raise ValueError(",".join(sorted(set(issues))))
    return {**row, "activation_contract_sha256": supplied}


def load_market_alpha_take_profit_activation(path: str | Path = DEFAULT_PATH) -> dict[str, Any]:
    raw = json.loads(Path(path).expanduser().resolve().read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError("market_alpha_activation_not_mapping")
    return validate_market_alpha_take_profit_activation(raw)


__all__ = [
    "DEFAULT_PATH",
    "SCHEMA_VERSION",
    "load_market_alpha_take_profit_activation",
    "materialize_market_alpha_take_profit_activation",
    "validate_market_alpha_take_profit_activation",
]
