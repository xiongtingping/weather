"""Content-addressed activation contract for the basket execution lane."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = "basket_yes_production_activation_v1"
DEFAULT_PATH = Path(__file__).resolve().parents[1] / "config" / "trading" / "basket_yes_v1.json"


def _canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def activation_contract_sha256(value: Mapping[str, Any]) -> str:
    body = {key: item for key, item in dict(value).items() if key != "activation_contract_sha256"}
    return hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()


def validate_activation(value: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(value)
    supplied = str(row.pop("activation_contract_sha256", "") or "").strip()
    blockers: list[str] = []
    if row.get("schema") != SCHEMA_VERSION:
        blockers.append("basket_activation_schema_invalid")
    if row.get("state") not in {"disabled", "enabled"}:
        blockers.append("basket_activation_state_invalid")
    if not isinstance(row.get("production_strategy_allowlist"), list) or row.get("production_strategy_allowlist") != ["basket_yes"]:
        blockers.append("basket_activation_strategy_allowlist_invalid")
    enabled = row.get("state") == "enabled"
    execution_mode = row.get("execution_mode", "production_live")
    if execution_mode not in {"production_live", "production_no_live"}:
        blockers.append("basket_activation_execution_mode_invalid")
    if enabled and execution_mode == "production_live" and (
        row.get("live_decision_write_authorized") is not True
        or row.get("live_submit_authorized") is not True
    ):
        blockers.append("basket_activation_live_authority_incomplete")
    if enabled and execution_mode == "production_no_live" and (
        row.get("live_submit_authorized") is not False
        or row.get("places_orders") is not False
        or row.get("calls_order_gateway") is not False
    ):
        blockers.append("basket_activation_no_live_boundary_invalid")
    if not isinstance(row.get("basket"), Mapping):
        blockers.append("basket_activation_basket_policy_missing")
    elif row["basket"].get("order_type") != "FOK" or row["basket"].get("all_legs_required") is not True:
        blockers.append("basket_activation_atomic_leg_policy_invalid")
    expected = activation_contract_sha256(row)
    if supplied != expected:
        blockers.append("basket_activation_contract_sha256_mismatch")
    if blockers:
        raise ValueError(";".join(sorted(set(blockers))))
    return {**row, "activation_contract_sha256": supplied}


def load_activation(path: Path | str = DEFAULT_PATH) -> dict[str, Any]:
    parsed = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(parsed, Mapping):
        raise ValueError("basket_activation_not_mapping")
    return validate_activation(parsed)


__all__ = ["DEFAULT_PATH", "SCHEMA_VERSION", "activation_contract_sha256", "load_activation", "validate_activation"]
