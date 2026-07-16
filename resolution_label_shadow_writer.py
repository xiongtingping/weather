"""Sentinel-gated append-only registry for final X→S→B shadow labels.

The registry is deliberately isolated from ``data/archive.db``.  It lets the
team accumulate audit-ready final labels before a separately accepted P1/P3
deployment, without allowing historical settlement scalars to masquerade as
strict, raw-object-backed training truth.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping

from resolution_bridge_runtime import validate_final_resolution_label
from sequential_bayes_public_ssot_shadow_writer import (
    SHADOW_MANIFEST_STATE,
    SHADOW_MANIFEST_TABLE,
)
from sequential_bayes_shadow_writer import ShadowTransactionIntegrityError


SCHEMA_VERSION = "resolution_label_shadow_writer_v1"
LABEL_TABLE = "resolution_final_labels"


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
        raise ShadowTransactionIntegrityError("resolution_label_shadow_manifest_missing")
    row = conn.execute(
        f"SELECT activation_state FROM {SHADOW_MANIFEST_TABLE} WHERE singleton=1"
    ).fetchone()
    if row is None or _text(row[0]) != SHADOW_MANIFEST_STATE:
        raise ShadowTransactionIntegrityError("resolution_label_shadow_manifest_invalid")


def initialize_isolated_resolution_label_staging_schema(conn: sqlite3.Connection) -> None:
    """Create the immutable label registry only after the isolation sentinel."""

    _assert_isolated_shadow_db(conn)
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {LABEL_TABLE} (
            target_identity TEXT NOT NULL,
            event_date TEXT NOT NULL,
            resolution_contract_id TEXT NOT NULL,
            resolution_contract_sha256 TEXT NOT NULL,
            resolution_label_sha256 TEXT NOT NULL UNIQUE,
            final_physical_high_c REAL NOT NULL,
            settlement_scalar_c REAL NOT NULL,
            market_settlement_bucket TEXT NOT NULL,
            label_available_at TEXT NOT NULL,
            label_finality TEXT NOT NULL CHECK(label_finality='final'),
            label_provenance_json TEXT NOT NULL,
            label_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(target_identity, event_date, resolution_contract_id, resolution_contract_sha256)
        )
        """
    )


def _row_from_label(label: Mapping[str, Any]) -> dict[str, Any]:
    record = _mapping(label)
    validation = validate_final_resolution_label(record)
    if validation.get("ok") is not True:
        reasons = ",".join(str(reason) for reason in list(validation.get("block_reasons") or []))
        raise ValueError(f"resolution_final_label_not_valid:{reasons}")
    return {
        "target_identity": _text(record.get("target_identity")),
        "event_date": _text(record.get("event_date")),
        "resolution_contract_id": _text(record.get("resolution_contract_id")),
        "resolution_contract_sha256": _text(record.get("resolution_contract_sha256")),
        "resolution_label_sha256": _text(record.get("resolution_label_sha256")),
        "final_physical_high_c": float(record["final_physical_high_c"]),
        "settlement_scalar_c": float(record["settlement_scalar_c"]),
        "market_settlement_bucket": _text(record.get("market_settlement_bucket")),
        "label_available_at": _text(record.get("label_available_at")),
        "label_finality": _text(record.get("label_finality")),
        "label_provenance_json": _json(record.get("label_provenance")),
        "label_json": _json(record),
        "created_at": _text(record.get("label_available_at")),
    }


def persist_final_resolution_label_shadow(
    conn: sqlite3.Connection,
    label: Mapping[str, Any],
) -> dict[str, Any]:
    """Append one final label; exact retry is idempotent and any drift fails closed."""

    _assert_isolated_shadow_db(conn)
    if not _table_exists(conn, LABEL_TABLE):
        raise ShadowTransactionIntegrityError("resolution_label_shadow_staging_schema_missing")
    row = _row_from_label(label)
    existing = conn.execute(
        f"""
        SELECT resolution_label_sha256,label_json
        FROM {LABEL_TABLE}
        WHERE target_identity=? AND event_date=?
          AND resolution_contract_id=? AND resolution_contract_sha256=?
        """,
        (
            row["target_identity"],
            row["event_date"],
            row["resolution_contract_id"],
            row["resolution_contract_sha256"],
        ),
    ).fetchone()
    if existing is not None:
        if _text(existing[0]) != row["resolution_label_sha256"] or _text(existing[1]) != row["label_json"]:
            raise ShadowTransactionIntegrityError("resolution_final_label_replay_drift")
        return {
            "schema": SCHEMA_VERSION,
            "ok": True,
            "idempotent": True,
            "shadow_only": True,
            "resolution_label_sha256": row["resolution_label_sha256"],
        }
    contract_id_conflict = conn.execute(
        f"""
        SELECT resolution_contract_sha256
        FROM {LABEL_TABLE}
        WHERE target_identity=? AND event_date=? AND resolution_contract_id=?
        LIMIT 1
        """,
        (row["target_identity"], row["event_date"], row["resolution_contract_id"]),
    ).fetchone()
    if contract_id_conflict is not None:
        raise ShadowTransactionIntegrityError("resolution_final_label_contract_identity_drift")
    fields = list(row)
    conn.execute(
        f"INSERT INTO {LABEL_TABLE}({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",
        tuple(row[field] for field in fields),
    )
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "idempotent": False,
        "shadow_only": True,
        "resolution_label_sha256": row["resolution_label_sha256"],
    }


def persist_final_resolution_label_proposal_shadow(
    conn: sqlite3.Connection,
    proposal: Mapping[str, Any],
) -> dict[str, Any]:
    """Persist only a ready source-observation proposal into the isolated registry."""

    value = _mapping(proposal)
    if value.get("ok") is not True or _text(value.get("state")) != "proposal_ready":
        raise ValueError("resolution_final_label_proposal_not_ready")
    label = _mapping(value.get("resolution_final_label"))
    if _text(value.get("target_identity")) != _text(label.get("target_identity")):
        raise ValueError("resolution_final_label_proposal_target_identity_mismatch")
    if _text(value.get("event_date")) != _text(label.get("event_date")):
        raise ValueError("resolution_final_label_proposal_event_date_mismatch")
    return persist_final_resolution_label_shadow(conn, label)


__all__ = [
    "LABEL_TABLE",
    "SCHEMA_VERSION",
    "initialize_isolated_resolution_label_staging_schema",
    "persist_final_resolution_label_proposal_shadow",
    "persist_final_resolution_label_shadow",
]
