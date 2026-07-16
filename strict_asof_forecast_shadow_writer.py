"""Sentinel-gated isolated staging writer for Tier-A forecast observations.

It proves the append-only source-observation boundary before a separately
authorized P1 production migration.  It rejects ordinary databases and never
writes facts, posterior, decisions, or orders.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping

from sequential_bayes_public_ssot_shadow_writer import (
    SHADOW_MANIFEST_STATE,
    SHADOW_MANIFEST_TABLE,
)
from sequential_bayes_shadow_writer import ShadowTransactionIntegrityError


SCHEMA_VERSION = "strict_asof_forecast_shadow_writer_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _json(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _assert_isolated_shadow_db(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, SHADOW_MANIFEST_TABLE):
        raise ShadowTransactionIntegrityError("strict_asof_forecast_shadow_manifest_missing")
    row = conn.execute(
        f"SELECT activation_state FROM {SHADOW_MANIFEST_TABLE} WHERE singleton=1"
    ).fetchone()
    if row is None or _text(row[0]) != SHADOW_MANIFEST_STATE:
        raise ShadowTransactionIntegrityError("strict_asof_forecast_shadow_manifest_invalid")


def initialize_isolated_forecast_capture_staging_schema(conn: sqlite3.Connection) -> None:
    """Create the source-observation staging table only in an isolated DB."""

    _assert_isolated_shadow_db(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS source_observations (
            source_event_id TEXT PRIMARY KEY,
            source_observation_id TEXT NOT NULL UNIQUE,
            source_id TEXT NOT NULL,
            source_class TEXT NOT NULL CHECK(source_class='forecast_weak_source'),
            role TEXT NOT NULL,
            station_id TEXT NOT NULL,
            event_date TEXT NOT NULL,
            valid_time TEXT NOT NULL,
            valid_time_local TEXT,
            fetched_at TEXT NOT NULL,
            parsed_at TEXT NOT NULL,
            response_completed_at TEXT NOT NULL,
            response_duration_ms REAL,
            forecast_peak_c REAL NOT NULL,
            raw_ref TEXT NOT NULL,
            raw_sha256 TEXT NOT NULL,
            parser_version TEXT NOT NULL,
            quality_flags_json TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(source_id, event_date, raw_sha256)
        )
        """
    )


def _row_from_proposal(proposal: Mapping[str, Any]) -> dict[str, Any]:
    value = _mapping(proposal)
    if value.get("ok") is not True or _text(value.get("state")) != "proposal_ready":
        raise ValueError("strict_asof_forecast_proposal_not_ready")
    observation = _mapping(value.get("source_observation"))
    payload = _mapping(observation.get("payload"))
    required = (
        "source_event_id",
        "source_observation_id",
        "source_id",
        "source_class",
        "role",
        "station_id",
        "event_date",
        "valid_time",
        "fetched_at",
        "parsed_at",
        "response_completed_at",
        "forecast_peak_c",
        "raw_ref",
        "raw_sha256",
        "parser_version",
    )
    missing = [key for key in required if observation.get(key) in (None, "")]
    if missing or _text(observation.get("source_class")) != "forecast_weak_source":
        raise ValueError(f"strict_asof_forecast_proposal_fields_invalid:{','.join(missing) or 'source_class'}")
    if _text(payload.get("evidence_tier")) != "A_live_observed_transport":
        raise ValueError("strict_asof_forecast_proposal_not_tier_a")
    if _text(payload.get("first_system_seen_at")) != _text(observation.get("fetched_at")):
        raise ValueError("strict_asof_forecast_proposal_first_seen_binding_mismatch")
    if _text(payload.get("raw_response_sha256")) != _text(observation.get("raw_sha256")):
        raise ValueError("strict_asof_forecast_proposal_raw_hash_binding_mismatch")
    return {
        "source_event_id": _text(observation.get("source_event_id")),
        "source_observation_id": _text(observation.get("source_observation_id")),
        "source_id": _text(observation.get("source_id")),
        "source_class": _text(observation.get("source_class")),
        "role": _text(observation.get("role")),
        "station_id": _text(observation.get("station_id")),
        "event_date": _text(observation.get("event_date")),
        "valid_time": _text(observation.get("valid_time")),
        "valid_time_local": _text(observation.get("valid_time_local")),
        "fetched_at": _text(observation.get("fetched_at")),
        "parsed_at": _text(observation.get("parsed_at")),
        "response_completed_at": _text(observation.get("response_completed_at")),
        "response_duration_ms": observation.get("response_duration_ms"),
        "forecast_peak_c": float(observation.get("forecast_peak_c")),
        "raw_ref": _text(observation.get("raw_ref")),
        "raw_sha256": _text(observation.get("raw_sha256")),
        "parser_version": _text(observation.get("parser_version")),
        "quality_flags_json": _json(observation.get("quality_flags") or []),
        "payload_json": _json(payload),
        "created_at": _text(observation.get("fetched_at")),
    }


def persist_tier_a_forecast_source_observation_proposal(
    conn: sqlite3.Connection,
    proposal: Mapping[str, Any],
) -> dict[str, Any]:
    """Append one Tier-A proposal; exact replay is idempotent, drift fails closed."""

    _assert_isolated_shadow_db(conn)
    if not _table_exists(conn, "source_observations"):
        raise ShadowTransactionIntegrityError("strict_asof_forecast_staging_schema_missing")
    row = _row_from_proposal(proposal)
    existing = conn.execute(
        "SELECT source_observation_id,raw_sha256,payload_json FROM source_observations WHERE source_event_id=?",
        (row["source_event_id"],),
    ).fetchone()
    if existing is not None:
        exact = (
            _text(existing[0]) == row["source_observation_id"]
            and _text(existing[1]) == row["raw_sha256"]
            and _text(existing[2]) == row["payload_json"]
        )
        if not exact:
            raise ShadowTransactionIntegrityError("strict_asof_forecast_source_event_replay_drift")
        return {
            "schema": SCHEMA_VERSION,
            "ok": True,
            "idempotent": True,
            "shadow_only": True,
            "source_event_id": row["source_event_id"],
            "source_observation_id": row["source_observation_id"],
        }
    fields = list(row)
    conn.execute(
        f"INSERT INTO source_observations({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",
        tuple(row[field] for field in fields),
    )
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "idempotent": False,
        "shadow_only": True,
        "source_event_id": row["source_event_id"],
        "source_observation_id": row["source_observation_id"],
    }


__all__ = [
    "SCHEMA_VERSION",
    "initialize_isolated_forecast_capture_staging_schema",
    "persist_tier_a_forecast_source_observation_proposal",
]
