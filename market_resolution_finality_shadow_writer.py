"""Append-only isolated registry for target-bound market finality attestations."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping

from market_resolution_finality_runtime import SCHEMA_VERSION as ATTESTATION_SCHEMA_VERSION
from sequential_bayes_public_ssot_shadow_writer import SHADOW_MANIFEST_STATE, SHADOW_MANIFEST_TABLE
from sequential_bayes_shadow_writer import ShadowTransactionIntegrityError


SCHEMA_VERSION = "market_resolution_finality_shadow_writer_v1"
FINALITY_TABLE = "market_resolution_finality_attestations"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _assert_sentinel(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, SHADOW_MANIFEST_TABLE):
        raise ShadowTransactionIntegrityError("market_finality_shadow_manifest_missing")
    row = conn.execute(f"SELECT activation_state FROM {SHADOW_MANIFEST_TABLE} WHERE singleton=1").fetchone()
    if row is None or _text(row[0]) != SHADOW_MANIFEST_STATE:
        raise ShadowTransactionIntegrityError("market_finality_shadow_manifest_invalid")


def initialize_isolated_market_resolution_finality_schema(conn: sqlite3.Connection) -> None:
    _assert_sentinel(conn)
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS {FINALITY_TABLE} (
            target_contract_sha256 TEXT NOT NULL,
            target_identity TEXT NOT NULL,
            event_date TEXT NOT NULL,
            market_finality_raw_sha256 TEXT NOT NULL,
            market_finality_raw_ref TEXT NOT NULL,
            attestation_json TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(target_contract_sha256,event_date)
        )"""
    )
    conn.execute(
        f"""CREATE TRIGGER IF NOT EXISTS trg_{FINALITY_TABLE}_no_update
        BEFORE UPDATE ON {FINALITY_TABLE} BEGIN SELECT RAISE(ABORT, 'market_finality_append_only'); END"""
    )
    conn.execute(
        f"""CREATE TRIGGER IF NOT EXISTS trg_{FINALITY_TABLE}_no_delete
        BEFORE DELETE ON {FINALITY_TABLE} BEGIN SELECT RAISE(ABORT, 'market_finality_append_only'); END"""
    )


def persist_market_resolution_finality_attestation_shadow(
    conn: sqlite3.Connection,
    attestation: Mapping[str, Any],
    *,
    created_at: str,
) -> dict[str, Any]:
    """Persist a fully-resolved receipt once; exact replay is idempotent."""

    _assert_sentinel(conn)
    if not _table_exists(conn, FINALITY_TABLE):
        raise ShadowTransactionIntegrityError("market_finality_shadow_schema_missing")
    row = dict(_mapping(attestation))
    issues: list[str] = []
    if _text(row.get("schema")) != ATTESTATION_SCHEMA_VERSION or row.get("ok") is not True or row.get("market_resolution_final") is not True:
        issues.append("market_finality_attestation_invalid")
    for field in ("target_contract_sha256", "target_identity", "event_date", "market_finality_raw_sha256", "market_finality_raw_ref", "resolution_state_observed_at"):
        if not _text(row.get(field)):
            issues.append(f"market_finality_{field}_missing")
    if issues:
        raise ValueError("market_finality_attestation_invalid:" + ",".join(sorted(set(issues))))
    encoded = _json(row)
    existing = conn.execute(
        f"SELECT attestation_json FROM {FINALITY_TABLE} WHERE target_contract_sha256=? AND event_date=?",
        (_text(row["target_contract_sha256"]), _text(row["event_date"])[:10]),
    ).fetchone()
    if existing is not None:
        if _text(existing[0]) != encoded:
            raise ShadowTransactionIntegrityError("market_finality_attestation_replay_drift")
        return {"schema": SCHEMA_VERSION, "ok": True, "idempotent": True, "shadow_only": True}
    conn.execute(
        f"""INSERT INTO {FINALITY_TABLE}(
            target_contract_sha256,target_identity,event_date,market_finality_raw_sha256,
            market_finality_raw_ref,attestation_json,observed_at,created_at
        ) VALUES(?,?,?,?,?,?,?,?)""",
        (
            _text(row["target_contract_sha256"]),
            _text(row["target_identity"]),
            _text(row["event_date"])[:10],
            _text(row["market_finality_raw_sha256"]),
            _text(row["market_finality_raw_ref"]),
            encoded,
            _text(row["resolution_state_observed_at"]),
            _text(created_at),
        ),
    )
    return {"schema": SCHEMA_VERSION, "ok": True, "idempotent": False, "shadow_only": True}


def load_market_resolution_finality_attestation_shadow(
    conn: sqlite3.Connection,
    *,
    target_contract_sha256: str,
    event_date: str,
) -> dict[str, Any] | None:
    _assert_sentinel(conn)
    if not _table_exists(conn, FINALITY_TABLE):
        raise ShadowTransactionIntegrityError("market_finality_shadow_schema_missing")
    row = conn.execute(
        f"SELECT attestation_json FROM {FINALITY_TABLE} WHERE target_contract_sha256=? AND event_date=?",
        (_text(target_contract_sha256), _text(event_date)[:10]),
    ).fetchone()
    return dict(json.loads(row[0])) if row is not None else None


def list_market_resolution_finality_attestations_shadow(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """List immutable finality receipts in chronological read-only order."""

    _assert_sentinel(conn)
    if not _table_exists(conn, FINALITY_TABLE):
        raise ShadowTransactionIntegrityError("market_finality_shadow_schema_missing")
    rows = conn.execute(
        f"SELECT attestation_json FROM {FINALITY_TABLE} ORDER BY observed_at,target_contract_sha256"
    ).fetchall()
    return [dict(json.loads(row[0])) for row in rows]


__all__ = [
    "FINALITY_TABLE",
    "SCHEMA_VERSION",
    "initialize_isolated_market_resolution_finality_schema",
    "list_market_resolution_finality_attestations_shadow",
    "load_market_resolution_finality_attestation_shadow",
    "persist_market_resolution_finality_attestation_shadow",
]
