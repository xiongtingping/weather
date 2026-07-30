"""Target-only contract for binding all native models to one forecast run.

The resident ZUUU evidence collector has a different scope and is deliberately
not used here. The contract identifies formal ``production_no_live`` model
input; it is not a shadow/research branch. This module is pure validation:
no network, SQLite, or service-control side effects.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .contracts import ContractError, content_sha256


SCHEMA = "bayes_basket_target_model_run_contract_v1"
PROVIDERS = (
    "ecmwf_ifs025_official",
    "gfs_0p25_official",
    "icon_global_official",
)
ALLOWED_RUN_HOURS_UTC = frozenset({0, 6, 12, 18})
MAX_STATIONS = 45


def _text(name: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"{name}_missing")
    return result


def normalize_run_initialization(value: Any) -> str:
    raw = _text("run_initialization_utc", value)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError("run_initialization_utc_invalid_iso8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError("run_initialization_utc_timezone_required")
    parsed = parsed.astimezone(timezone.utc)
    if parsed.minute or parsed.second or parsed.microsecond or parsed.hour not in ALLOWED_RUN_HOURS_UTC:
        raise ContractError("run_initialization_utc_not_official_cycle")
    return parsed.isoformat(timespec="seconds").replace("+00:00", "Z")


def _target_date(value: Any) -> str:
    result = _text("target_local_date", value)
    try:
        datetime.strptime(result, "%Y-%m-%d")
    except ValueError as exc:
        raise ContractError("target_local_date_invalid") from exc
    return result


def _stations(values: Sequence[Any]) -> list[str]:
    normalized = sorted({_text("station_id", value).upper() for value in values})
    if not normalized:
        raise ContractError("station_ids_missing")
    if len(normalized) > MAX_STATIONS:
        raise ContractError("station_ids_exceed_target_scope")
    return normalized


def build_model_run_contract(
    *,
    target_local_date: str,
    run_initialization_utc: str,
    station_ids: Sequence[Any],
    target_timezone: str,
    market_registry_snapshot_sha256: str = "",
) -> dict[str, Any]:
    """Build a content-addressed, non-authorizing target run manifest."""

    run = normalize_run_initialization(run_initialization_utc)
    stations = _stations(station_ids)
    timezone_name = _text("target_timezone", target_timezone)
    registry_sha = str(market_registry_snapshot_sha256 or "").strip().lower()
    if registry_sha and (len(registry_sha) != 64 or any(ch not in "0123456789abcdef" for ch in registry_sha)):
        raise ContractError("market_registry_snapshot_sha256_invalid")
    identity = {
        "schema": SCHEMA,
        "scope": "bayes_basket_target_only",
        "target_local_date": _target_date(target_local_date),
        "target_timezone": timezone_name,
        "run_initialization_utc": run,
        "market_registry_snapshot_sha256": registry_sha,
        "providers": list(PROVIDERS),
        "station_ids": stations,
        "boundary": {
            "target_only": True,
            "execution_mode": "production_no_live",
            "formal_probability_input_eligible": True,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "live_order_authorized": False,
            "live_submit_authorized": False,
            "unrelated_production_line_touched": False,
        },
    }
    digest = content_sha256(identity)
    return {**identity, "contract_sha256": digest}


def validate_capture_request(
    contract: Mapping[str, Any],
    *,
    provider_id: str,
    station_id: str,
    target_local_date: str,
    run_initialization_utc: str,
    target_timezone: str,
) -> dict[str, Any]:
    """Fail closed unless one capture request matches the shared run manifest."""

    if not isinstance(contract, Mapping) or contract.get("schema") != SCHEMA:
        raise ContractError("model_run_contract_schema_invalid")
    expected_hash = str(contract.get("contract_sha256") or "").strip().lower()
    identity = {key: contract.get(key) for key in (
        "schema", "scope", "target_local_date", "target_timezone",
        "run_initialization_utc", "market_registry_snapshot_sha256",
        "providers", "station_ids", "boundary",
    )}
    if expected_hash != content_sha256(identity):
        raise ContractError("model_run_contract_sha256_mismatch")
    expected_run = normalize_run_initialization(contract.get("run_initialization_utc"))
    expected_date = _target_date(contract.get("target_local_date"))
    expected_timezone = _text("target_timezone", contract.get("target_timezone"))
    expected_provider_set = tuple(str(item).strip().lower() for item in contract.get("providers") or ())
    if expected_provider_set != PROVIDERS:
        raise ContractError("model_run_contract_provider_set_invalid")
    expected_stations = _stations(contract.get("station_ids") or ())
    if str(provider_id).strip().lower() not in PROVIDERS:
        raise ContractError("model_run_contract_provider_invalid")
    if str(station_id).strip().upper() not in expected_stations:
        raise ContractError("model_run_contract_station_not_in_scope")
    if _target_date(target_local_date) != expected_date:
        raise ContractError("model_run_contract_target_date_mismatch")
    if normalize_run_initialization(run_initialization_utc) != expected_run:
        raise ContractError("model_run_contract_run_mismatch")
    if _text("target_timezone", target_timezone) != expected_timezone:
        raise ContractError("model_run_contract_timezone_mismatch")
    boundary = contract.get("boundary", {})
    if (
        boundary.get("execution_mode") != "production_no_live"
        or boundary.get("formal_probability_input_eligible") is not True
        or boundary.get("live_submit_authorized") is not False
        or boundary.get("unrelated_production_line_touched") is not False
    ):
        raise ContractError("model_run_contract_production_no_live_boundary_invalid")
    return dict(contract)


__all__ = [
    "ALLOWED_RUN_HOURS_UTC",
    "MAX_STATIONS",
    "PROVIDERS",
    "SCHEMA",
    "build_model_run_contract",
    "normalize_run_initialization",
    "validate_capture_request",
]
