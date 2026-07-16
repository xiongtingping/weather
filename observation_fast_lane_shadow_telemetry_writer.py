"""Append-only P10 telemetry for the explicitly isolated shadow fast lane.

Latency telemetry is intentionally outside Transaction B and the P9 intent
transaction: it observes those commits only after they return.  A telemetry
failure can therefore never roll back a durable posterior or intent, and an
unrecorded attempt can never be counted as a P10 acceptance sample.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Mapping

from sequential_bayes_public_ssot_shadow_writer import (
    SHADOW_MANIFEST_STATE,
    SHADOW_MANIFEST_TABLE,
)
from sequential_bayes_shadow_writer import ShadowTransactionIntegrityError


SCHEMA_VERSION = "observation_fast_lane_shadow_telemetry_writer_v1"
PACKET_SCHEMA_VERSION = "observation_fast_lane_shadow_latency_packet_v1"
PACKET_TABLE = "observation_fast_lane_latency_packets"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _assert_isolated_shadow_db(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, SHADOW_MANIFEST_TABLE):
        raise ShadowTransactionIntegrityError("fast_lane_telemetry_shadow_manifest_missing")
    row = conn.execute(
        f"SELECT activation_state FROM {SHADOW_MANIFEST_TABLE} WHERE singleton=1"
    ).fetchone()
    if row is None or _text(row[0]) != SHADOW_MANIFEST_STATE:
        raise ShadowTransactionIntegrityError("fast_lane_telemetry_shadow_manifest_invalid")


def initialize_observation_fast_lane_shadow_telemetry_schema(conn: sqlite3.Connection) -> None:
    """Create the isolated append-only telemetry table and nothing else."""

    _assert_isolated_shadow_db(conn)
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {PACKET_TABLE} (
            correlation_id TEXT PRIMARY KEY,
            canonical_observation_event_id TEXT NOT NULL,
            event_date TEXT NOT NULL,
            event_type TEXT NOT NULL,
            posterior_run_id TEXT NOT NULL,
            fact_snapshot_id TEXT NOT NULL,
            packet_json TEXT NOT NULL,
            packet_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_{PACKET_TABLE}_event
        ON {PACKET_TABLE}(event_date, event_type, created_at)
        """
    )
    conn.commit()


def _event_type(conn: sqlite3.Connection, canonical_observation_event_id: str) -> str:
    row = conn.execute(
        "SELECT report_type FROM canonical_observation_events WHERE canonical_observation_event_id=?",
        (_text(canonical_observation_event_id),),
    ).fetchone()
    return _text(row[0]).upper() if row is not None else "UNKNOWN"


def _normalized_packet(packet: Mapping[str, Any]) -> dict[str, Any]:
    value = _mapping(packet)
    fast_lane = _mapping(value.get("fast_lane"))
    correlation_id = _text(value.get("correlation_id"))
    if not correlation_id or _text(fast_lane.get("correlation_id")) != correlation_id:
        raise ValueError("fast_lane_telemetry_correlation_binding_invalid")
    if _text(fast_lane.get("mode")) != "shadow":
        raise ValueError("fast_lane_telemetry_shadow_mode_required")
    if not _text(fast_lane.get("schema")):
        raise ValueError("fast_lane_telemetry_contract_missing")
    required = (
        "canonical_observation_event_id",
        "event_date",
        "posterior_run_id",
        "fact_snapshot_id",
        "created_at",
    )
    missing = [key for key in required if not _text(value.get(key))]
    if missing:
        raise ValueError("fast_lane_telemetry_identity_missing:" + ",".join(missing))
    return {
        "schema": PACKET_SCHEMA_VERSION,
        "correlation_id": correlation_id,
        "canonical_observation_event_id": _text(value.get("canonical_observation_event_id")),
        "event_date": _text(value.get("event_date")),
        "event_type": _text(value.get("event_type")).upper(),
        "posterior_run_id": _text(value.get("posterior_run_id")),
        "fact_snapshot_id": _text(value.get("fact_snapshot_id")),
        "fast_lane": dict(fast_lane),
        "latency_timestamp_sources": dict(_mapping(value.get("latency_timestamp_sources"))),
        "shadow_only": True,
        "created_at": _text(value.get("created_at")),
    }


def append_observation_fast_lane_shadow_latency_packet(
    conn: sqlite3.Connection,
    *,
    packet: Mapping[str, Any],
) -> dict[str, Any]:
    """Append one immutable post-commit packet, or verify its exact retry."""

    _assert_isolated_shadow_db(conn)
    initialize_observation_fast_lane_shadow_telemetry_schema(conn)
    normalized = _normalized_packet(packet)
    canonical_id = normalized["canonical_observation_event_id"]
    actual_type = _event_type(conn, canonical_id)
    declared_type = normalized["event_type"]
    if actual_type == "UNKNOWN" or (declared_type and declared_type != actual_type):
        raise ShadowTransactionIntegrityError("fast_lane_telemetry_canonical_event_type_binding_invalid")
    normalized["event_type"] = actual_type
    packet_sha256 = _sha256(normalized)
    existing = conn.execute(
        f"SELECT packet_sha256 FROM {PACKET_TABLE} WHERE correlation_id=?",
        (normalized["correlation_id"],),
    ).fetchone()
    if existing is not None:
        if _text(existing[0]) != packet_sha256:
            raise ShadowTransactionIntegrityError("fast_lane_telemetry_existing_packet_conflict")
        return {
            "schema": SCHEMA_VERSION,
            "ok": True,
            "state": "idempotent",
            "correlation_id": normalized["correlation_id"],
            "packet_sha256": packet_sha256,
            "shadow_only": True,
        }
    conn.execute(
        f"""
        INSERT INTO {PACKET_TABLE}(
            correlation_id,canonical_observation_event_id,event_date,event_type,
            posterior_run_id,fact_snapshot_id,packet_json,packet_sha256,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?)
        """,
        (
            normalized["correlation_id"],
            canonical_id,
            normalized["event_date"],
            normalized["event_type"],
            normalized["posterior_run_id"],
            normalized["fact_snapshot_id"],
            _json(normalized),
            packet_sha256,
            normalized["created_at"],
        ),
    )
    conn.commit()
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "appended",
        "correlation_id": normalized["correlation_id"],
        "packet_sha256": packet_sha256,
        "shadow_only": True,
    }


__all__ = [
    "PACKET_SCHEMA_VERSION",
    "PACKET_TABLE",
    "SCHEMA_VERSION",
    "append_observation_fast_lane_shadow_latency_packet",
    "initialize_observation_fast_lane_shadow_telemetry_schema",
]
