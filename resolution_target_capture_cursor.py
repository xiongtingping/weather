"""Immutable forward-only watermark for target-rule raw capture."""

from __future__ import annotations

import sqlite3
from typing import Any

from sequential_bayes_public_ssot_shadow_writer import SHADOW_MANIFEST_STATE, SHADOW_MANIFEST_TABLE
from sequential_bayes_shadow_writer import ShadowTransactionIntegrityError


SCHEMA_VERSION = "resolution_target_capture_cursor_v1"
CURSOR_TABLE = "resolution_target_capture_cursor"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _assert_sentinel(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, SHADOW_MANIFEST_TABLE):
        raise ShadowTransactionIntegrityError("resolution_target_capture_cursor_manifest_missing")
    row = conn.execute(f"SELECT activation_state FROM {SHADOW_MANIFEST_TABLE} WHERE singleton=1").fetchone()
    if row is None or _text(row[0]) != SHADOW_MANIFEST_STATE:
        raise ShadowTransactionIntegrityError("resolution_target_capture_cursor_manifest_invalid")


def initialize_resolution_target_capture_cursor(conn: sqlite3.Connection, *, forward_capture_not_before: str) -> None:
    """Set one immutable forward watermark; historical discovery rows stay out."""

    _assert_sentinel(conn)
    if not _text(forward_capture_not_before):
        raise ValueError("resolution_target_capture_not_before_missing")
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS {CURSOR_TABLE} (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            forward_capture_not_before TEXT NOT NULL,
            schema_version TEXT NOT NULL
        )"""
    )
    conn.execute(
        f"INSERT OR IGNORE INTO {CURSOR_TABLE}(singleton,forward_capture_not_before,schema_version) VALUES(1,?,?)",
        (_text(forward_capture_not_before), SCHEMA_VERSION),
    )
    conn.execute(
        f"""CREATE TRIGGER IF NOT EXISTS trg_{CURSOR_TABLE}_no_update
        BEFORE UPDATE ON {CURSOR_TABLE} BEGIN SELECT RAISE(ABORT, 'resolution_target_capture_cursor_immutable'); END"""
    )
    conn.execute(
        f"""CREATE TRIGGER IF NOT EXISTS trg_{CURSOR_TABLE}_no_delete
        BEFORE DELETE ON {CURSOR_TABLE} BEGIN SELECT RAISE(ABORT, 'resolution_target_capture_cursor_immutable'); END"""
    )


def load_resolution_target_capture_not_before(conn: sqlite3.Connection) -> str:
    """Return the immutable prospective-capture watermark or fail closed."""

    _assert_sentinel(conn)
    if not _table_exists(conn, CURSOR_TABLE):
        raise ShadowTransactionIntegrityError("resolution_target_capture_cursor_missing")
    row = conn.execute(f"SELECT forward_capture_not_before,schema_version FROM {CURSOR_TABLE} WHERE singleton=1").fetchone()
    if row is None or _text(row[1]) != SCHEMA_VERSION or not _text(row[0]):
        raise ShadowTransactionIntegrityError("resolution_target_capture_cursor_invalid")
    return _text(row[0])


__all__ = [
    "CURSOR_TABLE",
    "SCHEMA_VERSION",
    "initialize_resolution_target_capture_cursor",
    "load_resolution_target_capture_not_before",
]
