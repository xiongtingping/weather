"""Single target-owned boundary for exact-run model capture."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .contracts import ContractError
from .resource_bounds import (
    MAX_TARGET_PROCESS_RSS_BYTES,
    MAX_TARGET_RESEARCH_RAW_BYTES,
    MIN_TARGET_FREE_BYTES,
    MIB,
    load_bounded_json,
)


SCHEMA = "bayes_basket_target_model_arrival_capture_v1"
EXPECTED_SCOPE = "canonical_45_station"
EXPECTED_STATION_COUNT = 45
EXPECTED_PROVIDERS = frozenset({
    "ecmwf_ifs025_official",
    "ecmwf_aifs025_official",
    "gfs_0p25_official",
    "icon_global_official",
    "gem_gdps_official",
    "cma_grapes_global_open_meteo",
})
MAX_RESPONSE_BYTES_HARD = 256 * MIB
MAX_CAPTURE_BYTES_HARD = 512 * MIB
REQUIRED_FALSE_BOUNDARIES = (
    "writes_target_database",
    "writes_production_database",
    "calls_order_gateway",
    "places_orders",
    "live_order_authorized",
    "protected_production_line_touched",
)


def normalize_resource_budget(value: Any) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise ContractError("target_capture_resource_budget_missing")
    upper_bounds = {
        "max_process_rss_bytes": MAX_TARGET_PROCESS_RSS_BYTES,
        "max_raw_bytes": MAX_TARGET_RESEARCH_RAW_BYTES,
        "max_response_bytes": MAX_RESPONSE_BYTES_HARD,
        "max_capture_bytes": MAX_CAPTURE_BYTES_HARD,
    }
    normalized: dict[str, int] = {}
    for key, hard_limit in upper_bounds.items():
        raw = value.get(key)
        if isinstance(raw, bool):
            raise ContractError(f"target_capture_resource_budget_{key}_invalid")
        try:
            parsed = int(raw)
        except (TypeError, ValueError) as exc:
            raise ContractError(f"target_capture_resource_budget_{key}_invalid") from exc
        if parsed <= 0 or parsed > hard_limit:
            raise ContractError(f"target_capture_resource_budget_{key}_exceeds_hard_limit")
        normalized[key] = parsed
    try:
        min_free_bytes = int(value.get("min_free_bytes"))
    except (TypeError, ValueError) as exc:
        raise ContractError("target_capture_resource_budget_min_free_bytes_invalid") from exc
    if min_free_bytes < MIN_TARGET_FREE_BYTES:
        raise ContractError("target_capture_resource_budget_min_free_bytes_below_hard_floor")
    normalized["min_free_bytes"] = min_free_bytes
    if normalized["max_capture_bytes"] < normalized["max_response_bytes"]:
        raise ContractError("target_capture_resource_budget_capture_below_response")
    return normalized


def load_target_capture_config(path: Path | str) -> dict[str, Any]:
    try:
        value = load_bounded_json(Path(path).expanduser().resolve())
    except Exception as exc:
        raise ContractError("target_capture_config_unreadable") from exc
    if not isinstance(value, Mapping) or value.get("schema") != SCHEMA:
        raise ContractError("target_capture_config_invalid")
    if value.get("enabled") is not True:
        raise ContractError("target_capture_disabled")
    if value.get("target_only") is not True:
        raise ContractError("target_capture_not_target_only")
    if (
        str(value.get("scope") or "") != EXPECTED_SCOPE
        or int(value.get("station_count") or 0) != EXPECTED_STATION_COUNT
    ):
        raise ContractError("target_capture_scope_invalid")
    providers = frozenset(str(item).strip() for item in value.get("providers") or ())
    if providers != EXPECTED_PROVIDERS:
        raise ContractError("target_capture_provider_set_invalid")
    boundary = value.get("boundary") if isinstance(value.get("boundary"), Mapping) else {}
    if any(boundary.get(key) is not False for key in REQUIRED_FALSE_BOUNDARIES):
        raise ContractError("target_capture_boundary_invalid")
    return {
        **dict(value),
        "providers": sorted(providers),
        "resource_budget": normalize_resource_budget(value.get("resource_budget")),
    }


__all__ = [
    "EXPECTED_PROVIDERS",
    "SCHEMA",
    "load_target_capture_config",
    "normalize_resource_budget",
]
