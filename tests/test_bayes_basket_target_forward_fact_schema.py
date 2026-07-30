from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target.ssot import (
    _upgrade_empty_policy_training_fact_schema,
    apply_production_ssot_schema,
)


FACT_TABLE = "basket_policy_training_fact"
REQUIRED_FORWARD_COLUMNS = {
    "decision_scope_key",
    "captured_at_utc",
    "source_sequence",
    "quote_observed_at",
    "quote_expires_at",
    "quote_freshness_policy_sha256",
}


def _open_target_db(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "forward-fact-schema.db")
    apply_production_ssot_schema(conn)
    return conn


def _replace_with_legacy_fact_table(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TRIGGER IF EXISTS basket_training_fact_no_update")
    conn.execute("DROP TRIGGER IF EXISTS basket_training_fact_no_delete")
    conn.execute("DROP INDEX IF EXISTS basket_policy_training_fact_scope_idx")
    conn.execute(f"DROP TABLE {FACT_TABLE}")
    conn.execute(
        f"""
        CREATE TABLE {FACT_TABLE} (
          fact_id TEXT PRIMARY KEY,
          station_id TEXT NOT NULL,
          event_date TEXT NOT NULL,
          decision_at_utc TEXT NOT NULL,
          stage TEXT NOT NULL,
          outcome_json TEXT NOT NULL,
          partition_key TEXT NOT NULL,
          content_sha256 TEXT NOT NULL CHECK(length(content_sha256)=64)
        )
        """
    )


def _fact_values(*, fact_id: str = "fact-1", scope: str = "scope-1") -> dict[str, object]:
    sha_a = "a" * 64
    sha_b = "b" * 64
    sha_c = "c" * 64
    sha_d = "d" * 64
    sha_e = "e" * 64
    return {
        "fact_id": fact_id,
        "station_id": "TEST",
        "event_date": "2026-07-27",
        "settlement_timezone": "Asia/Shanghai",
        "decision_at_utc": "2026-07-27T05:00:00Z",
        "decision_scope_key": scope,
        "captured_at_utc": "2026-07-27T05:00:01Z",
        "source_sequence": 7,
        "quote_observed_at": "2026-07-27T05:00:00Z",
        "quote_expires_at": "2026-07-27T05:00:05Z",
        "quote_freshness_policy_sha256": sha_e,
        "stage": "P0-M",
        "probability_envelope_id": "env-1",
        "posterior_version": "posterior-v1",
        "posterior_sha256": sha_a,
        "official_exclusion_mask_sha256": sha_b,
        "ws_snapshot_id": "ws-1",
        "quote_version": "quote-v1",
        "quote_sha256": sha_c,
        "inventory_snapshot_id": "inventory-1",
        "inventory_version": "inventory-v1",
        "inventory_sha256": sha_d,
        "order_state_snapshot_id": "order-state-1",
        "order_state_version": "order-state-v1",
        "order_state_sha256": sha_a,
        "cycle_id": "cycle-1",
        "decision_id": "decision-1",
        "decision_sha256": sha_b,
        "plan_sha256": sha_c,
        "intent_count": 0,
        "intent_set_sha256": sha_d,
        "policy_version_vector_json": "{}",
        "feature_json": "{}",
        "partition_key": "2026/TEST",
        "authority_scope": "PRODUCTION_NO_LIVE_FORWARD",
        "created_at_utc": "2026-07-27T05:00:01Z",
        "content_sha256": sha_e,
    }


def _insert_fact(conn: sqlite3.Connection, values: dict[str, object]) -> None:
    columns = tuple(values)
    conn.execute(
        f"INSERT INTO {FACT_TABLE}({','.join(columns)}) VALUES "
        f"({','.join('?' for _ in columns)})",
        tuple(values[column] for column in columns),
    )


def _columns(conn: sqlite3.Connection) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({FACT_TABLE})")}


def test_empty_legacy_fact_is_replaced_with_bound_forward_schema(
    tmp_path: Path,
) -> None:
    with _open_target_db(tmp_path) as conn:
        _replace_with_legacy_fact_table(conn)

        _upgrade_empty_policy_training_fact_schema(conn)

        assert REQUIRED_FORWARD_COLUMNS.issubset(_columns(conn))
        info = {
            str(row[1]): int(row[3] or 0)
            for row in conn.execute(f"PRAGMA table_info({FACT_TABLE})")
        }
        assert all(info[column] == 1 for column in REQUIRED_FORWARD_COLUMNS)
        assert conn.execute(f"SELECT COUNT(*) FROM {FACT_TABLE}").fetchone()[0] == 0
        assert conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (FACT_TABLE,),
        ).fetchone()[0].lower().count(
            "check(length(quote_freshness_policy_sha256)=64)"
        ) == 1

        unique_indexes = []
        for row in conn.execute(f"PRAGMA index_list({FACT_TABLE})"):
            if int(row[2] or 0) == 1:
                unique_indexes.append(
                    [
                        str(item[2])
                        for item in conn.execute(f"PRAGMA index_info({row[1]})")
                    ]
                )
        assert ["decision_scope_key"] in unique_indexes


def test_populated_legacy_fact_isolated_and_fails_closed_without_fabrication(
    tmp_path: Path,
) -> None:
    with _open_target_db(tmp_path) as conn:
        _replace_with_legacy_fact_table(conn)
        conn.execute(
            f"INSERT INTO {FACT_TABLE} VALUES (?,?,?,?,?,?,?,?)",
            (
                "legacy-fact",
                "TEST",
                "2026-07-27",
                "2026-07-27T05:00:00Z",
                "P1",
                "legacy-outcome",
                "2026/TEST",
                "f" * 64,
            ),
        )

        with pytest.raises(
            RuntimeError,
            match="policy_training_exact_version_upgrade_requires_empty_tables",
        ):
            _upgrade_empty_policy_training_fact_schema(conn)

        assert _columns(conn) == {
            "fact_id",
            "station_id",
            "event_date",
            "decision_at_utc",
            "stage",
            "outcome_json",
            "partition_key",
            "content_sha256",
        }
        assert conn.execute(
            f"SELECT fact_id,outcome_json FROM {FACT_TABLE}"
        ).fetchone() == ("legacy-fact", "legacy-outcome")
        assert not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND "
            "name='basket_policy_training_fact_legacy_forward_v1'"
        ).fetchone()


def test_forward_fact_upgrade_is_idempotent_on_repeat(tmp_path: Path) -> None:
    with _open_target_db(tmp_path) as conn:
        _replace_with_legacy_fact_table(conn)

        _upgrade_empty_policy_training_fact_schema(conn)
        after_first = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (FACT_TABLE,),
        ).fetchone()[0]
        columns_after_first = _columns(conn)

        _upgrade_empty_policy_training_fact_schema(conn)

        assert _columns(conn) == columns_after_first
        assert conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (FACT_TABLE,),
        ).fetchone()[0] == after_first
        assert conn.execute(f"SELECT COUNT(*) FROM {FACT_TABLE}").fetchone()[0] == 0


def test_canonical_forward_fact_is_append_only_idempotent_by_scope_and_sha(
    tmp_path: Path,
) -> None:
    with _open_target_db(tmp_path) as conn:
        first = _fact_values()
        _insert_fact(conn, first)

        duplicate_scope = _fact_values(fact_id="fact-2", scope="scope-1")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_fact(conn, duplicate_scope)

        invalid_sha = _fact_values(fact_id="fact-3", scope="scope-3")
        invalid_sha["quote_freshness_policy_sha256"] = "bad"
        with pytest.raises(sqlite3.IntegrityError):
            _insert_fact(conn, invalid_sha)

        with pytest.raises(sqlite3.IntegrityError, match="append_only"):
            conn.execute(
                f"UPDATE {FACT_TABLE} SET feature_json='{{}}' WHERE fact_id=?",
                ("fact-1",),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append_only"):
            conn.execute(
                f"DELETE FROM {FACT_TABLE} WHERE fact_id=?",
                ("fact-1",),
            )
        assert conn.execute(f"SELECT COUNT(*) FROM {FACT_TABLE}").fetchone()[0] == 1

