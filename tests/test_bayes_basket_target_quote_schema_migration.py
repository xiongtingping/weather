from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target import ContractError
from bayes_basket_target.production_io import load_production_market_snapshot
from bayes_basket_target.ssot import (
    PRODUCTION_TARGET_TABLES,
    _upgrade_quote_freshness_columns,
    apply_production_ssot_schema,
)


LEGACY_TABLE = "basket_official_ws_snapshot"
ARCHIVE_TABLE = "basket_official_ws_snapshot_legacy_no_quote_expiry_v1"
REQUIRED_NEW_COLUMNS = {"quote_expires_at", "quote_freshness_policy_sha256"}


def _open_tmp_target_db(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "quote-schema-migration.db")
    conn.row_factory = sqlite3.Row
    apply_production_ssot_schema(conn)
    return conn


def _replace_with_legacy_snapshot_table(conn: sqlite3.Connection) -> None:
    """Install the pre-expiry schema inside the isolated tmp database."""

    conn.execute("DROP TRIGGER IF EXISTS basket_official_ws_snapshot_no_update")
    conn.execute("DROP TRIGGER IF EXISTS basket_official_ws_snapshot_no_delete")
    conn.execute("DROP INDEX IF EXISTS basket_official_ws_snapshot_asof_idx")
    conn.execute(f"DROP TABLE {LEGACY_TABLE}")
    conn.execute(
        f"""
        CREATE TABLE {LEGACY_TABLE} (
          snapshot_id TEXT PRIMARY KEY,
          market_id TEXT NOT NULL,
          station_id TEXT NOT NULL,
          event_date TEXT NOT NULL,
          settlement_timezone TEXT NOT NULL,
          sequence INTEGER NOT NULL CHECK(sequence>=0),
          observed_at TEXT NOT NULL,
          quote_version TEXT NOT NULL,
          quote_observed_at TEXT NOT NULL,
          quote_source TEXT NOT NULL CHECK(quote_source='OFFICIAL_CLOB_WS'),
          inventory_version TEXT NOT NULL,
          order_state_version TEXT NOT NULL,
          remaining_budget_usdc REAL NOT NULL CHECK(remaining_budget_usdc>=0),
          buckets_json TEXT NOT NULL,
          cash_phase_input_json TEXT NOT NULL,
          content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64),
          state TEXT NOT NULL CHECK(state IN ('COMMITTED','QUARANTINED')),
          created_at TEXT NOT NULL
        )
        """
    )


def _legacy_snapshot_row() -> tuple[object, ...]:
    return (
        "legacy-snapshot-1",
        "market-1",
        "TEST",
        "2026-07-27",
        "Asia/Shanghai",
        0,
        "2026-07-27T05:00:00Z",
        "quote-v1",
        "2026-07-27T05:00:00Z",
        "OFFICIAL_CLOB_WS",
        "inventory-v1",
        "order-state-v1",
        10.0,
        json.dumps([{"bucket": "34°C", "ask_levels": [{"price": 0.4, "size": 1.0}]}]),
        json.dumps({"station_id": "TEST", "event_date_local": "2026-07-27"}),
        "a" * 64,
        "COMMITTED",
        "2026-07-27T05:00:00Z",
    )


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def test_empty_legacy_quote_snapshot_is_replaced_with_new_schema(tmp_path: Path) -> None:
    with _open_tmp_target_db(tmp_path) as conn:
        _replace_with_legacy_snapshot_table(conn)

        result = _upgrade_quote_freshness_columns(conn)

        assert result == {
            "schema": "bayes_basket_quote_freshness_upgrade_v1",
            "state": "replaced_empty_legacy",
            "retired_row_count": 0,
            "audit_table": None,
        }
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_official_ws_snapshot"
        ).fetchone()[0] == 0
        assert not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (ARCHIVE_TABLE,),
        ).fetchone()
        assert REQUIRED_NEW_COLUMNS.issubset(_table_columns(conn, LEGACY_TABLE))


def test_populated_legacy_quote_snapshot_is_archived_and_formal_table_empty(
    tmp_path: Path,
) -> None:
    with _open_tmp_target_db(tmp_path) as conn:
        _replace_with_legacy_snapshot_table(conn)
        conn.execute(
            f"INSERT INTO {LEGACY_TABLE} VALUES ({','.join('?' for _ in range(18))})",
            _legacy_snapshot_row(),
        )

        result = _upgrade_quote_freshness_columns(conn)

        assert result["state"] == "retired_populated_legacy"
        assert result["retired_row_count"] == 1
        assert result["audit_table"] == ARCHIVE_TABLE
        assert conn.execute(
            f"SELECT COUNT(*) FROM {ARCHIVE_TABLE}"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_official_ws_snapshot"
        ).fetchone()[0] == 0
        assert REQUIRED_NEW_COLUMNS.issubset(_table_columns(conn, LEGACY_TABLE))


def test_quote_freshness_upgrade_is_idempotent_on_repeat(tmp_path: Path) -> None:
    with _open_tmp_target_db(tmp_path) as conn:
        _replace_with_legacy_snapshot_table(conn)

        first = _upgrade_quote_freshness_columns(conn)
        formal_columns_after_first = _table_columns(conn, LEGACY_TABLE)
        second = _upgrade_quote_freshness_columns(conn)

        assert first["state"] == "replaced_empty_legacy"
        assert second == {
            "schema": "bayes_basket_quote_freshness_upgrade_v1",
            "state": "already_ready",
            "retired_row_count": 0,
        }
        assert _table_columns(conn, LEGACY_TABLE) == formal_columns_after_first
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_official_ws_snapshot"
        ).fetchone()[0] == 0


def test_archived_legacy_rows_are_not_visible_to_production_loader(
    tmp_path: Path,
) -> None:
    with _open_tmp_target_db(tmp_path) as conn:
        _replace_with_legacy_snapshot_table(conn)
        conn.execute(
            f"INSERT INTO {LEGACY_TABLE} VALUES ({','.join('?' for _ in range(18))})",
            _legacy_snapshot_row(),
        )
        _upgrade_quote_freshness_columns(conn)

        assert LEGACY_TABLE in PRODUCTION_TARGET_TABLES
        assert ARCHIVE_TABLE not in PRODUCTION_TARGET_TABLES
        with pytest.raises(ContractError, match="basket_official_ws_snapshot_missing"):
            load_production_market_snapshot(
                conn,
                station_id="TEST",
                event_date="2026-07-27",
                evaluation_at_utc="2026-07-27T06:00:00Z",
            )
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_official_ws_snapshot"
        ).fetchone()[0] == 0
        assert conn.execute(
            f"SELECT COUNT(*) FROM {ARCHIVE_TABLE}"
        ).fetchone()[0] == 1
