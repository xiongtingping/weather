"""Versioned, market-independent buckets for the physical daily maximum X."""

from __future__ import annotations

import hashlib
import json
from typing import Any


SCHEMA_VERSION = "physical_x_temperature_bucket_contract_v1"
DEFAULT_LOWER_C = -20
DEFAULT_UPPER_C = 60
DEFAULT_GRID_STEP_C = 0.25


def _sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _label(value: int) -> str:
    return f"{value}°C"


def build_physical_x_bucket_contract(
    *,
    lower_c: int = DEFAULT_LOWER_C,
    upper_c: int = DEFAULT_UPPER_C,
    grid_step_c: float = DEFAULT_GRID_STEP_C,
) -> dict[str, Any]:
    """Return a complete rounded-C physical X domain, never a market domain.

    The endpoints are conservative physical guardrails, not market outcome
    labels.  Both tails are represented, so integrating a weak distribution
    cannot silently discard probability beyond a market's temporary listing.
    """

    try:
        lower, upper = int(lower_c), int(upper_c)
        step = float(grid_step_c)
    except (TypeError, ValueError):
        lower, upper, step = 0, 0, 0.0
    if lower >= upper or step <= 0.0 or step > 1.0 or abs(round(1.0 / step) * step - 1.0) > 1e-9:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": ["physical_x_bucket_contract_configuration_invalid"],
            "read_only": True,
        }
    buckets = [f"{lower}°C or below", *[_label(value) for value in range(lower + 1, upper)], f"{upper}°C or higher"]
    multiplier = int(round(1.0 / step))
    temperature_grid = [round(index / multiplier, 10) for index in range(lower * multiplier, upper * multiplier + 1)]
    core = {
        "schema": SCHEMA_VERSION,
        "rounding_rule": "rounded_degrees",
        "lower_guardrail_c": lower,
        "upper_guardrail_c": upper,
        "temperature_grid_step_c": step,
        "buckets": buckets,
        "temperature_grid_c": temperature_grid,
    }
    return {
        **core,
        "bucket_contract_sha256": _sha(core),
        "ok": True,
        "state": "available",
        "read_only": True,
        "boundary": {
            "physical_x_only": True,
            "market_outcome_labels_not_consumed": True,
            "market_rule_or_settlement_not_consumed": True,
            "writes_posterior": False,
            "places_orders": False,
        },
    }


__all__ = [
    "DEFAULT_GRID_STEP_C",
    "DEFAULT_LOWER_C",
    "DEFAULT_UPPER_C",
    "SCHEMA_VERSION",
    "build_physical_x_bucket_contract",
]
