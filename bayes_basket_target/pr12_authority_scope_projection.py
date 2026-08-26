"""Sealed PR12 source/day authority derived from the four current doctrines."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path
from typing import Any, Mapping, Sequence

from zoneinfo import ZoneInfo

from .contracts import ContractError, content_sha256
from .station_source_authority import validate_global_awc_source_authority


SCHEMA = "bayes_basket_target_pr12_authority_scope_projection_v1"
BUILDER_ID = "pr12.authority_scope_projection.v1"
AUTHORITY_VERSION = "FOUR_DOCTRINE_MARKET_LISTED_DAY_20260826_V1"
SETTLEMENT_TIMEZONE_SOURCE = "basket_target_market_registry.settlement_timezone"
REQUIRED_OVERRIDE = "canonical-day 口径换代（当前有效）"
REQUIRED_DOCTRINE_SHA256 = {
    "station_model_calibration_ssot_v1.md": "46c7fd6fbfdd13840daf471aa8345918278e68844a516f69b6ebea903301c36b",
    "global_station_executable_basket_formula_v1.md": "ad309d0e4675d4ea5bc2ca4c0dbfd0f110a4110dae5a6e16cbd7acfa719f3eab",
    "station_basket_policy_hierarchy_v1.md": "8043b0dce987587c46903a5cc6180b861ca90a8976793ed1013b597086b7dce1",
    "weather_basket_trading_operating_system_v1.md": "6e6f5303b0841ccb52827029227a037864d70c9b5840f179bcb96e606334da4b",
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


def _is_timezone(value: Any) -> bool:
    """A row's zone is checked for being a zone, not for being one zone."""

    name = str(value or "").strip()
    if not name:
        return False
    try:
        ZoneInfo(name)
    except Exception:
        return False
    return True


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
        # The doctrine no longer names one timezone, so there is no timezone
        # literal to require here.  It names where the timezone comes from,
        # and that is checked by reading it from there rather than by
        # grepping the prose for a constant.
        if actual_sha != expected_sha or REQUIRED_OVERRIDE not in text:
            raise ContractError("pr12_authority_projection_doctrine_invalid:" + name)
        receipts.append({"document_name": name, "raw_sha256": actual_sha})
    return receipts


def build_pr12_authority_scope_projection(
    *, doctrine_paths: Sequence[Path], station_registry: Mapping[str, Mapping[str, Any]],
    station_source_authorities: Mapping[str, Mapping[str, Any]],
    station_settlement_timezones: Mapping[str, str], as_of_utc: str,
) -> dict[str, Any]:
    """Seal the day authority each station's own market listing declares.

    Until 2026-08-26 this projection pinned every station to America/New_York,
    under the 2026-08-05 canonical-day override.  The listings say otherwise:
    only KATL, KLGA and KMIA settle on an Eastern day, and the other 42 settle
    on their own local one, which is also what the live probability path and
    the settlement truth lane have used all along.  Following the override
    therefore had this lane grouping a Tokyo station's reports into an Eastern
    day -- RJTT 2026-07-18 reads 32C over that window and 29C over its own,
    and 29C is what the market pays on.  The override was amended the same
    day; the timezone now comes from the listing, per station.
    """

    receipts = _doctrine_receipts(doctrine_paths)
    stations = sorted(station_registry)
    if len(stations) != 45 or set(stations) != set(station_source_authorities):
        raise ContractError("pr12_authority_projection_45station_scope_required")
    if set(stations) != set(station_settlement_timezones):
        raise ContractError("pr12_authority_projection_timezone_scope_required")
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
        zone = str(station_settlement_timezones[station] or "").strip()
        try:
            ZoneInfo(zone)
        except Exception as exc:
            raise ContractError(
                "pr12_authority_projection_settlement_timezone_invalid:" + station
            ) from exc
        row_identity = {
            "station_id": station,
            "source_lane": "AWC",
            "source_authority_version": authority["authority_version"],
            "source_authority_content_sha256": authority["content_sha256"],
            "legacy_station_registry_content_sha256": registry["content_sha256"],
            "settlement_definition": "official_daily_tmax_c",
            "settlement_timezone": zone,
            "settlement_timezone_authority": AUTHORITY_VERSION,
        }
        rows.append({**row_identity, "content_sha256": content_sha256(row_identity)})
    identity = {
        "schema": SCHEMA,
        "builder_id": BUILDER_ID,
        "as_of_utc": _utc(as_of_utc),
        "state": "MATERIALIZED_UNPUBLISHED",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        # No single timezone at the top any more -- naming one is what made
        # this projection wrong for 42 stations.  What is fleet-wide is where
        # the per-station value comes from.
        "settlement_timezone_source": SETTLEMENT_TIMEZONE_SOURCE,
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
        "settlement_timezone_source", "settlement_timezone_authority",
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
        or value.get("settlement_timezone_source") != SETTLEMENT_TIMEZONE_SOURCE
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
            or not _is_timezone(item.get("settlement_timezone"))
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
