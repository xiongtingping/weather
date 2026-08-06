"""Append-only current source-authority projection for calibration stations.

``calibration_station_registry`` is an immutable imported identity snapshot.
It may therefore retain a historical AMSC value even after the operational
SSOT changes.  This module appends a signed global-AWC authority projection
instead of rewriting that record, and provides the one fail-closed resolver
used by historical AWC recovery.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3
from typing import Any, Mapping, Sequence

from .contracts import ContractError, canonical_json, content_sha256


SCHEMA = "calibration_station_global_awc_source_authority_v1"
SOURCE_LANE = "AWC"
MAX_STATIONS = 45
AUTHORITY_FIELDS = {
    "authority_id", "station_id", "source_lane", "authority_version",
    "activated_at_utc", "legacy_registry_content_sha256", "lineage_json",
    "lineage", "content_sha256",
}


def _utc(value: Any) -> str:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError("station_source_authority_activated_at_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError("station_source_authority_activated_at_timezone_required")
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _sha(value: Any) -> str:
    text = str(value or "").strip().lower()
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ContractError("station_source_authority_hash_invalid")
    return text


def _table_exists(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='calibration_station_source_authority'"
    ).fetchone() is not None


def resolve_global_awc_source_authority(
    conn: sqlite3.Connection,
    *,
    station_id: str,
) -> Mapping[str, Any]:
    """Return exactly one current global-AWC authority, otherwise fail closed."""

    station = str(station_id or "").strip().upper()
    if not station or not _table_exists(conn):
        raise ContractError("station_source_authority_missing")
    rows = conn.execute(
        "SELECT authority_id,source_lane,authority_version,activated_at_utc,"
        "legacy_registry_content_sha256,lineage_json,content_sha256 "
        "FROM calibration_station_source_authority WHERE station_id=? "
        "ORDER BY activated_at_utc DESC,authority_id DESC LIMIT 2",
        (station,),
    ).fetchall()
    if not rows:
        raise ContractError("station_source_authority_missing")
    newest = rows[0]
    if str(newest[1]).upper() != SOURCE_LANE:
        raise ContractError("station_source_authority_not_awc")
    try:
        lineage = json.loads(str(newest[5]))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ContractError("station_source_authority_lineage_invalid") from exc
    identity = {
        "authority_id": str(newest[0]),
        "station_id": station,
        "source_lane": SOURCE_LANE,
        "authority_version": str(newest[2]),
        "activated_at_utc": str(newest[3]),
        "legacy_registry_content_sha256": _sha(newest[4]),
        "lineage_json": canonical_json(lineage),
    }
    return validate_global_awc_source_authority(
        {**identity, "lineage": lineage, "content_sha256": str(newest[6])},
        station_id=station,
    )


def validate_global_awc_source_authority(
    raw: Mapping[str, Any], *, station_id: str | None = None,
) -> Mapping[str, Any]:
    """Validate one portable authority projection without consulting another table."""

    if set(raw) != AUTHORITY_FIELDS:
        raise ContractError("station_source_authority_fields_mismatch")
    value = dict(raw)
    station = str(value.get("station_id") or "").strip().upper()
    if not station or (station_id is not None and station != str(station_id).strip().upper()):
        raise ContractError("station_source_authority_station_mismatch")
    lineage = value.get("lineage")
    if not isinstance(lineage, Mapping) or canonical_json(lineage) != value.get("lineage_json"):
        raise ContractError("station_source_authority_lineage_invalid")
    if (
        value.get("source_lane") != SOURCE_LANE
        or lineage.get("schema") != SCHEMA
        or lineage.get("current_ssot_awc_global_formal") is not True
        or lineage.get("amsc_role") != "retired_history_only"
        or lineage.get("legacy_registry_content_sha256")
        != value.get("legacy_registry_content_sha256")
    ):
        raise ContractError("station_source_authority_not_awc")
    identity = {
        key: value[key] for key in (
            "authority_id", "station_id", "source_lane", "authority_version",
            "activated_at_utc", "legacy_registry_content_sha256", "lineage_json",
        )
    }
    identity["station_id"] = station
    identity["activated_at_utc"] = _utc(identity["activated_at_utc"])
    identity["legacy_registry_content_sha256"] = _sha(
        identity["legacy_registry_content_sha256"]
    )
    supplied = _sha(value.get("content_sha256"))
    if content_sha256(identity) != supplied:
        raise ContractError("station_source_authority_content_hash_invalid")
    return {**identity, "lineage": dict(lineage), "content_sha256": supplied}


def migrate_global_awc_source_authorities(
    conn: sqlite3.Connection,
    *,
    station_ids: Sequence[str],
    authority_version: str,
    activated_at_utc: str,
    lane_map_content_sha256: str,
    commit: bool = False,
) -> Mapping[str, Any]:
    """Append the current global-AWC projection for one exact station set."""

    stations = sorted({str(value or "").strip().upper() for value in station_ids if str(value or "").strip()})
    if len(stations) != MAX_STATIONS:
        raise ContractError("station_source_authority_45station_scope_required")
    version = str(authority_version or "").strip()
    if not version:
        raise ContractError("station_source_authority_version_invalid")
    activated = _utc(activated_at_utc)
    lane_map_sha = _sha(lane_map_content_sha256)
    if not _table_exists(conn):
        raise ContractError("station_source_authority_schema_missing")
    registry_rows = conn.execute(
        "SELECT station_id,source_lane,content_sha256 FROM calibration_station_registry "
        "WHERE station_id IN (" + ",".join("?" for _ in stations) + ")",
        tuple(stations),
    ).fetchall()
    registry = {str(row[0]): (str(row[1]).upper(), _sha(row[2])) for row in registry_rows}
    if set(registry) != set(stations):
        raise ContractError("station_source_authority_registry_scope_incomplete")

    rows: list[dict[str, Any]] = []
    for station in stations:
        previous_lane, registry_sha = registry[station]
        lineage = {
            "schema": SCHEMA,
            "current_ssot_awc_global_formal": True,
            "legacy_registry_source_lane": previous_lane,
            "legacy_registry_content_sha256": registry_sha,
            "station_lane_map_content_sha256": lane_map_sha,
            "amsc_role": "retired_history_only",
            "migration_kind": "append_only_authority_projection",
        }
        seed = {
            "station_id": station,
            "authority_version": version,
            "legacy_registry_content_sha256": registry_sha,
            "station_lane_map_content_sha256": lane_map_sha,
        }
        authority_id = "station_authority_" + content_sha256(seed)[:40]
        identity = {
            "authority_id": authority_id,
            "station_id": station,
            "source_lane": SOURCE_LANE,
            "authority_version": version,
            "activated_at_utc": activated,
            "legacy_registry_content_sha256": registry_sha,
            "lineage_json": canonical_json(lineage),
        }
        rows.append({**identity, "content_sha256": content_sha256(identity)})

    results: list[dict[str, Any]] = []
    owns_transaction = False
    if commit and not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
        owns_transaction = True
    try:
        for row in rows:
            existing = conn.execute(
                "SELECT authority_id,source_lane,authority_version,activated_at_utc,"
                "legacy_registry_content_sha256,lineage_json,content_sha256 "
                "FROM calibration_station_source_authority WHERE station_id=? "
                "AND authority_version=? LIMIT 2",
                (row["station_id"], row["authority_version"]),
            ).fetchall()
            if len(existing) > 1:
                raise ContractError("station_source_authority_existing_ambiguous")
            if existing:
                actual = existing[0]
                expected = tuple(row[key] for key in (
                    "authority_id", "source_lane", "authority_version", "activated_at_utc",
                    "legacy_registry_content_sha256", "lineage_json", "content_sha256",
                ))
                if tuple(actual) != expected:
                    raise ContractError("station_source_authority_existing_conflict")
                results.append({"station_id": row["station_id"], "state": "idempotent", "authority_id": row["authority_id"]})
                continue
            if commit:
                conn.execute(
                    "INSERT INTO calibration_station_source_authority("
                    "authority_id,station_id,source_lane,authority_version,activated_at_utc,"
                    "legacy_registry_content_sha256,lineage_json,content_sha256"
                    ") VALUES(?,?,?,?,?,?,?,?)",
                    tuple(row[key] for key in (
                        "authority_id", "station_id", "source_lane", "authority_version",
                        "activated_at_utc", "legacy_registry_content_sha256", "lineage_json",
                        "content_sha256",
                    )),
                )
            results.append({"station_id": row["station_id"], "state": "committed" if commit else "validated_no_write", "authority_id": row["authority_id"]})
        if commit and owns_transaction:
            conn.commit()
    except Exception:
        if commit and owns_transaction:
            conn.rollback()
        raise
    return {
        "schema": SCHEMA,
        "state": "COMMITTED" if commit else "VALIDATED_NO_WRITE",
        "authority_version": version,
        "activated_at_utc": activated,
        "station_count": len(rows),
        "wrote_count": sum(row["state"] == "committed" for row in results),
        "idempotent_count": sum(row["state"] == "idempotent" for row in results),
        "rows": results,
        "boundary": {"append_only": True, "registry_update": False, "registry_delete": False, "calls_order_gateway": False, "orders_submitted": 0},
    }
