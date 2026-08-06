"""Sealed PR12 source/day authority derived from the four current doctrines."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contracts import ContractError, content_sha256
from .station_source_authority import validate_global_awc_source_authority


SCHEMA = "bayes_basket_target_pr12_authority_scope_projection_v1"
BUILDER_ID = "pr12.authority_scope_projection.v1"
SETTLEMENT_TIMEZONE = "America/New_York"
AUTHORITY_VERSION = "FOUR_DOCTRINE_CANONICAL_DAY_OVERRIDE_20260805_V1"
REQUIRED_OVERRIDE = "canonical-day / release override（当前有效）"
REQUIRED_DOCTRINE_SHA256 = {
    "station_model_calibration_ssot_v1.md": "2ab20b3072e1d55f42eef3685bb033dd442d2818968eed1ece3019a84a2aa0a2",
    "global_station_executable_basket_formula_v1.md": "e96633224256ffb25d688943774687d359a70caa68afe37edb332c8ab90264a9",
    "station_basket_policy_hierarchy_v1.md": "2a13ddec9bbbfdd61e754519c74a4bbe746a5f88eef37ea73049780de5b92d07",
    "weather_basket_trading_operating_system_v1.md": "7cdee3e467d678836e3fec39dde537e9df61a067261bc89db2f00850e7d72feb",
}


def _utc(value: Any) -> str:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError("pr12_authority_projection_as_of_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError("pr12_authority_projection_as_of_timezone_required")
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _doctrine_receipts(paths: Sequence[Path]) -> list[dict[str, str]]:
    by_name = {path.name: path for path in paths}
    if set(by_name) != set(REQUIRED_DOCTRINE_SHA256):
        raise ContractError("pr12_authority_projection_doctrine_set_mismatch")
    receipts: list[dict[str, str]] = []
    for name, expected_sha in sorted(REQUIRED_DOCTRINE_SHA256.items()):
        path = by_name[name].expanduser().resolve(strict=True)
        payload = path.read_bytes()
        actual_sha = hashlib.sha256(payload).hexdigest()
        text = payload.decode("utf-8")
        if (
            actual_sha != expected_sha
            or REQUIRED_OVERRIDE not in text
            or SETTLEMENT_TIMEZONE not in text
        ):
            raise ContractError("pr12_authority_projection_doctrine_invalid:" + name)
        receipts.append({"document_name": name, "raw_sha256": actual_sha})
    return receipts


def build_pr12_authority_scope_projection(
    *, doctrine_paths: Sequence[Path], station_registry: Mapping[str, Mapping[str, Any]],
    station_source_authorities: Mapping[str, Mapping[str, Any]], as_of_utc: str,
) -> dict[str, Any]:
    receipts = _doctrine_receipts(doctrine_paths)
    stations = sorted(station_registry)
    if len(stations) != 45 or set(stations) != set(station_source_authorities):
        raise ContractError("pr12_authority_projection_45station_scope_required")
    rows: list[dict[str, Any]] = []
    for station in stations:
        registry = station_registry[station]
        authority = validate_global_awc_source_authority(
            station_source_authorities[station], station_id=station
        )
        if (
            set(registry) != {"station_id", "settlement_definition", "content_sha256"}
            or registry.get("station_id") != station
            or registry.get("settlement_definition") != "official_daily_tmax_c"
            or len(str(registry.get("content_sha256") or "")) != 64
            or authority["legacy_registry_content_sha256"]
            != registry["content_sha256"]
        ):
            raise ContractError("pr12_authority_projection_station_identity_invalid")
        row_identity = {
            "station_id": station,
            "source_lane": "AWC",
            "source_authority_version": authority["authority_version"],
            "source_authority_content_sha256": authority["content_sha256"],
            "legacy_station_registry_content_sha256": registry["content_sha256"],
            "settlement_definition": "official_daily_tmax_c",
            "settlement_timezone": SETTLEMENT_TIMEZONE,
            "settlement_timezone_authority": AUTHORITY_VERSION,
        }
        rows.append({**row_identity, "content_sha256": content_sha256(row_identity)})
    identity = {
        "schema": SCHEMA,
        "builder_id": BUILDER_ID,
        "as_of_utc": _utc(as_of_utc),
        "state": "MATERIALIZED_UNPUBLISHED",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "settlement_timezone": SETTLEMENT_TIMEZONE,
        "settlement_timezone_authority": AUTHORITY_VERSION,
        "doctrine_receipts": receipts,
        "doctrine_receipt_set_sha256": content_sha256(receipts),
        "station_count": len(rows),
        "station_authority_rowset_sha256": content_sha256(rows),
        "source_authority_vector_sha256": content_sha256([
            [row["station_id"], row["source_authority_content_sha256"]]
            for row in rows
        ]),
        "station_registry_vector_sha256": content_sha256([
            [row["station_id"], row["legacy_station_registry_content_sha256"]]
            for row in rows
        ]),
        "rows": rows,
        "consumable": False,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def validate_pr12_authority_scope_projection(raw: Mapping[str, Any]) -> dict[str, Any]:
    expected_top_fields = {
        "schema", "builder_id", "as_of_utc", "state", "authority_scope",
        "settlement_timezone", "settlement_timezone_authority",
        "doctrine_receipts", "doctrine_receipt_set_sha256", "station_count",
        "station_authority_rowset_sha256", "source_authority_vector_sha256",
        "station_registry_vector_sha256", "rows", "consumable", "content_sha256",
    }
    if set(raw) != expected_top_fields:
        raise ContractError("pr12_authority_projection_fields_mismatch")
    value = dict(raw)
    supplied = str(value.pop("content_sha256", "")).lower()
    if supplied != content_sha256(value):
        raise ContractError("pr12_authority_projection_content_hash_mismatch")
    if (
        value.get("schema") != SCHEMA
        or value.get("builder_id") != BUILDER_ID
        or value.get("state") != "MATERIALIZED_UNPUBLISHED"
        or value.get("authority_scope") != "PRODUCTION_NO_LIVE_ONLY"
        or value.get("settlement_timezone") != SETTLEMENT_TIMEZONE
        or value.get("settlement_timezone_authority") != AUTHORITY_VERSION
        or value.get("consumable") is not False
    ):
        raise ContractError("pr12_authority_projection_contract_invalid")
    value["as_of_utc"] = _utc(value["as_of_utc"])
    receipts = value.get("doctrine_receipts")
    if not isinstance(receipts, list) or {
        item.get("document_name"): item.get("raw_sha256") for item in receipts
        if isinstance(item, Mapping)
    } != REQUIRED_DOCTRINE_SHA256:
        raise ContractError("pr12_authority_projection_doctrine_receipts_invalid")
    if value.get("doctrine_receipt_set_sha256") != content_sha256(receipts):
        raise ContractError("pr12_authority_projection_doctrine_receipts_invalid")
    rows = value.get("rows")
    if (
        not isinstance(rows, list) or len(rows) != 45
        or value.get("station_count") != 45
        or value.get("station_authority_rowset_sha256") != content_sha256(rows)
    ):
        raise ContractError("pr12_authority_projection_45station_scope_required")
    stations: set[str] = set()
    expected_row_fields = {
        "station_id", "source_lane", "source_authority_version",
        "source_authority_content_sha256", "legacy_station_registry_content_sha256",
        "settlement_definition", "settlement_timezone",
        "settlement_timezone_authority", "content_sha256",
    }
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != expected_row_fields:
            raise ContractError("pr12_authority_projection_row_invalid")
        item = dict(row)
        row_sha = str(item.pop("content_sha256", "")).lower()
        station = str(item.get("station_id") or "").upper()
        if (
            not station or station in stations or row_sha != content_sha256(item)
            or item.get("source_lane") != "AWC"
            or item.get("settlement_definition") != "official_daily_tmax_c"
            or item.get("settlement_timezone") != SETTLEMENT_TIMEZONE
            or item.get("settlement_timezone_authority") != AUTHORITY_VERSION
            or len(str(item.get("source_authority_content_sha256") or "")) != 64
            or len(str(item.get("legacy_station_registry_content_sha256") or "")) != 64
        ):
            raise ContractError("pr12_authority_projection_row_invalid")
        stations.add(station)
    if [str(row["station_id"]) for row in rows] != sorted(stations):
        raise ContractError("pr12_authority_projection_row_order_invalid")
    if value.get("source_authority_vector_sha256") != content_sha256([
        [row["station_id"], row["source_authority_content_sha256"]] for row in rows
    ]) or value.get("station_registry_vector_sha256") != content_sha256([
        [row["station_id"], row["legacy_station_registry_content_sha256"]] for row in rows
    ]):
        raise ContractError("pr12_authority_projection_parent_vector_mismatch")
    return {**value, "content_sha256": supplied}


__all__ = [
    "AUTHORITY_VERSION", "BUILDER_ID", "SCHEMA",
    "build_pr12_authority_scope_projection", "validate_pr12_authority_scope_projection",
]
