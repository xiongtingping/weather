"""Append-only isolated staging for final X and S source observations."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping

from resolution_final_source_runtime import (
    PHYSICAL_ROLE,
    PHYSICAL_SOURCE_ID,
    SETTLEMENT_ROLE,
    SETTLEMENT_SOURCE_ID,
)
from sequential_bayes_public_ssot_shadow_writer import SHADOW_MANIFEST_STATE, SHADOW_MANIFEST_TABLE
from sequential_bayes_shadow_writer import ShadowTransactionIntegrityError


SCHEMA_VERSION = "resolution_final_source_shadow_writer_v1"
SOURCE_TABLE = "resolution_final_source_observations"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _assert_isolated_shadow_db(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, SHADOW_MANIFEST_TABLE):
        raise ShadowTransactionIntegrityError("resolution_final_source_shadow_manifest_missing")
    row = conn.execute(f"SELECT activation_state FROM {SHADOW_MANIFEST_TABLE} WHERE singleton=1").fetchone()
    if row is None or _text(row[0]) != SHADOW_MANIFEST_STATE:
        raise ShadowTransactionIntegrityError("resolution_final_source_shadow_manifest_invalid")


def initialize_isolated_resolution_final_source_staging_schema(conn: sqlite3.Connection) -> None:
    _assert_isolated_shadow_db(conn)
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {SOURCE_TABLE} (
            source_observation_id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            role TEXT NOT NULL,
            station_id TEXT NOT NULL,
            event_date TEXT NOT NULL,
            raw_ref TEXT NOT NULL,
            raw_sha256 TEXT NOT NULL,
            observation_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(source_id,event_date)
        )
        """
    )
    conn.execute(
        f"""CREATE TRIGGER IF NOT EXISTS trg_{SOURCE_TABLE}_no_update
        BEFORE UPDATE ON {SOURCE_TABLE} BEGIN SELECT RAISE(ABORT, 'resolution_final_source_append_only'); END"""
    )
    conn.execute(
        f"""CREATE TRIGGER IF NOT EXISTS trg_{SOURCE_TABLE}_no_delete
        BEFORE DELETE ON {SOURCE_TABLE} BEGIN SELECT RAISE(ABORT, 'resolution_final_source_append_only'); END"""
    )


def _validate_observation(observation: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    row = dict(_mapping(observation))
    payload = _mapping(row.get("payload"))
    role = _text(row.get("role"))
    expected_source = PHYSICAL_SOURCE_ID if role == PHYSICAL_ROLE else SETTLEMENT_SOURCE_ID if role == SETTLEMENT_ROLE else ""
    scalar_key = "final_physical_high_c" if role == PHYSICAL_ROLE else "settlement_scalar_c"
    reasons: list[str] = []
    for field in ("source_observation_id", "source_id", "station_id", "event_date", "raw_ref", "raw_sha256", "fetched_at"):
        if not _text(row.get(field)):
            reasons.append(f"final_source_{field}_missing")
    if _text(row.get("source_class")) != "settlement_source":
        reasons.append("final_source_class_invalid")
    if not expected_source or _text(row.get("source_id")) != expected_source:
        reasons.append("final_source_id_or_role_invalid")
    if _text(row.get("station_id")).upper() != "ZUUU":
        reasons.append("final_source_station_invalid")
    if payload.get("label_finality") != "final":
        reasons.append("final_source_payload_finality_invalid")
    try:
        float(payload.get(scalar_key))
    except (TypeError, ValueError):
        reasons.append("final_source_scalar_invalid")
    raw_sha = _text(row.get("raw_sha256")).lower()
    if len(raw_sha) != 64 or any(char not in "0123456789abcdef" for char in raw_sha):
        reasons.append("final_source_raw_sha256_invalid")
    if role == PHYSICAL_ROLE:
        if payload.get("physical_x_provenance_schema") != "amsc_station_day_raw_membership_v1":
            reasons.append("physical_x_provenance_schema_invalid")
        if not isinstance(payload.get("strict_asof_training_eligible"), bool):
            reasons.append("physical_x_strict_asof_training_eligibility_invalid")
        if _text(payload.get("physical_x_label_quality")) not in {
            "strict_asof_complete_schedule",
            "complete_schedule_late_or_non_strict_arrival",
        }:
            reasons.append("physical_x_label_quality_invalid")
        try:
            if int(payload.get("late_arrival_count")) < 0:
                raise ValueError
        except (TypeError, ValueError):
            reasons.append("physical_x_late_arrival_count_invalid")
        source_ids = payload.get("physical_source_observation_ids")
        raw_hashes = payload.get("physical_raw_sha256_list")
        if not isinstance(source_ids, list) or not source_ids or any(not _text(value) for value in source_ids):
            reasons.append("physical_x_source_observation_ids_invalid")
        if not isinstance(raw_hashes, list) or not raw_hashes or any(len(_text(value)) != 64 for value in raw_hashes):
            reasons.append("physical_x_raw_sha256_list_invalid")
        if isinstance(source_ids, list) and isinstance(raw_hashes, list) and len(source_ids) != len(raw_hashes):
            reasons.append("physical_x_raw_membership_cardinality_invalid")
    return row, reasons


def persist_final_source_observation_shadow(
    conn: sqlite3.Connection,
    observation: Mapping[str, Any],
) -> dict[str, Any]:
    """Append one final source row; exact retry is idempotent, drift is fatal."""

    _assert_isolated_shadow_db(conn)
    if not _table_exists(conn, SOURCE_TABLE):
        raise ShadowTransactionIntegrityError("resolution_final_source_shadow_staging_schema_missing")
    row, reasons = _validate_observation(observation)
    if reasons:
        raise ValueError("resolution_final_source_observation_invalid:" + ",".join(sorted(set(reasons))))
    encoded = _json(row)
    existing = conn.execute(
        f"SELECT observation_json FROM {SOURCE_TABLE} WHERE source_observation_id=?",
        (_text(row["source_observation_id"]),),
    ).fetchone()
    if existing is not None:
        if _text(existing[0]) != encoded:
            raise ShadowTransactionIntegrityError("resolution_final_source_observation_replay_drift")
        return {"schema": SCHEMA_VERSION, "ok": True, "idempotent": True, "shadow_only": True}
    identity = conn.execute(
        f"SELECT source_observation_id FROM {SOURCE_TABLE} WHERE source_id=? AND event_date=?",
        (_text(row["source_id"]), _text(row["event_date"])[:10]),
    ).fetchone()
    if identity is not None:
        raise ShadowTransactionIntegrityError("resolution_final_source_observation_identity_drift")
    conn.execute(
        f"""INSERT INTO {SOURCE_TABLE}(
            source_observation_id,source_id,role,station_id,event_date,raw_ref,raw_sha256,observation_json,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            _text(row["source_observation_id"]), _text(row["source_id"]), _text(row["role"]),
            _text(row["station_id"]), _text(row["event_date"])[:10], _text(row["raw_ref"]),
            _text(row["raw_sha256"]), encoded, _text(row["fetched_at"]),
        ),
    )
    return {"schema": SCHEMA_VERSION, "ok": True, "idempotent": False, "shadow_only": True}


__all__ = [
    "SCHEMA_VERSION",
    "SOURCE_TABLE",
    "initialize_isolated_resolution_final_source_staging_schema",
    "persist_final_source_observation_shadow",
]
