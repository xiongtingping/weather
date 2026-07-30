from __future__ import annotations

import sqlite3

import pytest

from bayes_basket_target.ssot import upgrade_execution_vector_contract


LEGACY_EMPTY_EXECUTION_VECTOR_DDL = """
CREATE TABLE basket_target_execution_version_vectors (
  order_intent_id TEXT PRIMARY KEY,
  cycle_id TEXT,
  decision_id TEXT,
  training_fact_id TEXT,
  target_price_id TEXT,
  quote_observed_at TEXT,
  quote_expires_at TEXT,
  probability_sha256 TEXT,
  official_ws_quote_sha256 TEXT,
  inventory_sha256 TEXT,
  order_state_sha256 TEXT,
  station_policy_sha256 TEXT,
  cash_policy_decision_sha256 TEXT,
  strict_delta_artifact_sha256 TEXT,
  vector_json TEXT NOT NULL DEFAULT '{}',
  vector_sha256 TEXT NOT NULL CHECK(length(vector_sha256)=64),
  created_at TEXT NOT NULL,
  CHECK('LEGACY_CAS'='LEGACY_CAS')
)
"""


LEGACY_POPULATED_EXECUTION_VECTOR_DDL = """
CREATE TABLE basket_target_execution_version_vectors (
  order_intent_id TEXT PRIMARY KEY,
  cycle_id TEXT,
  decision_id TEXT,
  training_fact_id TEXT,
  vector_policy TEXT NOT NULL
    CHECK(vector_policy IN ('LEGACY_CAS','EXECUTION','NO_EXECUTION')),
  target_price_id TEXT,
  quote_observed_at TEXT,
  quote_expires_at TEXT,
  probability_sha256 TEXT,
  official_ws_quote_sha256 TEXT,
  inventory_sha256 TEXT,
  order_state_sha256 TEXT,
  station_policy_sha256 TEXT,
  cash_policy_decision_sha256 TEXT,
  strict_delta_artifact_sha256 TEXT,
  vector_json TEXT NOT NULL DEFAULT '{}',
  vector_sha256 TEXT NOT NULL CHECK(length(vector_sha256)=64),
  created_at TEXT NOT NULL
)
"""


CURRENT_INDEX_DDL = """
CREATE UNIQUE INDEX IF NOT EXISTS basket_target_no_execution_vector_cycle_idx
ON basket_target_execution_version_vectors(cycle_id)
WHERE vector_policy='NO_EXECUTION'
"""


CURRENT_TRIGGERS_DDL = """
CREATE TRIGGER IF NOT EXISTS basket_target_version_vector_no_update
BEFORE UPDATE ON basket_target_execution_version_vectors
BEGIN
  SELECT RAISE(ABORT,'append_only');
END;
CREATE TRIGGER IF NOT EXISTS basket_target_version_vector_no_delete
BEFORE DELETE ON basket_target_execution_version_vectors
BEGIN
  SELECT RAISE(ABORT,'append_only');
END;
"""


def _seed_parent_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE basket_target_execution_cycles("
        "cycle_id TEXT PRIMARY KEY)"
    )
    conn.execute(
        "CREATE TABLE auto_trade_decisions("
        "decision_id TEXT PRIMARY KEY)"
    )
    conn.execute(
        "CREATE TABLE basket_target_order_intents("
        "order_intent_id TEXT PRIMARY KEY)"
    )


def _seed_parent_rows(
    conn: sqlite3.Connection,
    *,
    cycle_id: str,
    decision_id: str,
    order_intent_id: str | None,
) -> None:
    conn.execute(
        "INSERT INTO basket_target_execution_cycles(cycle_id) VALUES(?)",
        (cycle_id,),
    )
    conn.execute(
        "INSERT INTO auto_trade_decisions(decision_id) VALUES(?)",
        (decision_id,),
    )
    if order_intent_id is not None:
        conn.execute(
            "INSERT INTO basket_target_order_intents(order_intent_id) VALUES(?)",
            (order_intent_id,),
        )


def _legacy_names(conn: sqlite3.Connection, object_type: str) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type=? ORDER BY name",
            (object_type,),
        )
    }


def _insert_execution_row(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO basket_target_execution_version_vectors("
        "order_intent_id,cycle_id,decision_id,training_fact_id,vector_policy,"
        "target_price_id,quote_observed_at,quote_expires_at,"
        "probability_sha256,official_ws_quote_sha256,inventory_sha256,"
        "order_state_sha256,station_policy_sha256,cash_policy_decision_sha256,"
        "strict_delta_artifact_sha256,vector_json,vector_sha256,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "intent-exec",
            "cycle-exec",
            "decision-exec",
            "fact-exec",
            "EXECUTION",
            "price-1",
            "2026-07-27T00:00:00Z",
            "2026-07-27T00:01:00Z",
            "a" * 64,
            "b" * 64,
            "c" * 64,
            "d" * 64,
            "e" * 64,
            "f" * 64,
            "0" * 64,
            '{"policy":"EXECUTION","evidence":{"source":"legacy","sequence":1}}',
            "1" * 64,
            "2026-07-27T00:00:00Z",
        ),
    )


def _insert_no_execution_row(conn: sqlite3.Connection, *, cycle_id: str) -> None:
    conn.execute(
        "INSERT INTO basket_target_execution_version_vectors("
        "order_intent_id,cycle_id,decision_id,training_fact_id,vector_policy,"
        "target_price_id,quote_observed_at,quote_expires_at,"
        "probability_sha256,official_ws_quote_sha256,inventory_sha256,"
        "order_state_sha256,station_policy_sha256,cash_policy_decision_sha256,"
        "strict_delta_artifact_sha256,vector_json,vector_sha256,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            None,
            cycle_id,
            f"decision-{cycle_id}",
            f"fact-{cycle_id}",
            "NO_EXECUTION",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            '{"policy":"NO_EXECUTION","reason_codes":["missing_quote"]}',
            "2" * 64,
            "2026-07-27T00:00:00Z",
        ),
    )


class _FailingExecuteProxy:
    def __init__(self, conn: sqlite3.Connection, fail_prefix: str) -> None:
        self._conn = conn
        self._fail_prefix = fail_prefix

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor:
        if str(sql).strip().startswith(self._fail_prefix):
            raise RuntimeError("injected_execution_vector_migration_fault")
        return self._conn.execute(sql, parameters)

    def __getattr__(self, name: str) -> object:
        return getattr(self._conn, name)


def test_empty_legacy_shape_rebuilds_without_inventing_rows_and_installs_contract(
    tmp_path,
) -> None:
    db = tmp_path / "legacy.db"
    with sqlite3.connect(db) as conn:
        _seed_parent_tables(conn)
        conn.execute(LEGACY_EMPTY_EXECUTION_VECTOR_DDL)

        result = upgrade_execution_vector_contract(conn)

        assert result == {"state": "migrated_empty_legacy", "rows_migrated": 0}
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_target_execution_version_vectors"
        ).fetchone()[0] == 0


def test_populated_legacy_shape_migrates_losslessly_without_invented_evidence(
    tmp_path,
) -> None:
    db = tmp_path / "legacy.db"
    with sqlite3.connect(db) as conn:
        _seed_parent_tables(conn)
        conn.execute(LEGACY_POPULATED_EXECUTION_VECTOR_DDL)
        _seed_parent_rows(
            conn,
            cycle_id="cycle-exec",
            decision_id="decision-exec",
            order_intent_id="intent-exec",
        )
        _seed_parent_rows(
            conn,
            cycle_id="cycle-noexec",
            decision_id="decision-cycle-noexec",
            order_intent_id=None,
        )
        _insert_execution_row(conn)
        _insert_no_execution_row(conn, cycle_id="cycle-noexec")

        result = upgrade_execution_vector_contract(conn)

        assert result == {"state": "migrated_exact", "rows_migrated": 2}
        rows = conn.execute(
            "SELECT order_intent_id,cycle_id,decision_id,training_fact_id,"
            "vector_policy,target_price_id,quote_observed_at,quote_expires_at,"
            "probability_sha256,official_ws_quote_sha256,inventory_sha256,"
            "order_state_sha256,station_policy_sha256,"
            "cash_policy_decision_sha256,strict_delta_artifact_sha256,"
            "vector_json,vector_sha256,created_at "
            "FROM basket_target_execution_version_vectors "
            "ORDER BY COALESCE(order_intent_id,'~')"
        ).fetchall()
        assert rows == [
            (
                "intent-exec",
                "cycle-exec",
                "decision-exec",
                "fact-exec",
                "EXECUTION",
                "price-1",
                "2026-07-27T00:00:00Z",
                "2026-07-27T00:01:00Z",
                "a" * 64,
                "b" * 64,
                "c" * 64,
                "d" * 64,
                "e" * 64,
                "f" * 64,
                "0" * 64,
                '{"policy":"EXECUTION","evidence":{"source":"legacy","sequence":1}}',
                "1" * 64,
                "2026-07-27T00:00:00Z",
            ),
            (
                None,
                "cycle-noexec",
                "decision-cycle-noexec",
                "fact-cycle-noexec",
                "NO_EXECUTION",
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                '{"policy":"NO_EXECUTION","reason_codes":["missing_quote"]}',
                "2" * 64,
                "2026-07-27T00:00:00Z",
            ),
        ]


def test_repeat_migration_is_idempotent_and_keeps_exact_contract_intact(
    tmp_path,
) -> None:
    db = tmp_path / "legacy.db"
    with sqlite3.connect(db) as conn:
        _seed_parent_tables(conn)
        conn.execute(LEGACY_POPULATED_EXECUTION_VECTOR_DDL)
        _seed_parent_rows(
            conn,
            cycle_id="cycle-exec",
            decision_id="decision-exec",
            order_intent_id="intent-exec",
        )
        _seed_parent_rows(
            conn,
            cycle_id="cycle-noexec",
            decision_id="decision-cycle-noexec",
            order_intent_id=None,
        )
        _insert_execution_row(conn)
        _insert_no_execution_row(conn, cycle_id="cycle-noexec")

        first = upgrade_execution_vector_contract(conn)
        second = upgrade_execution_vector_contract(conn)
        third = upgrade_execution_vector_contract(conn)

        assert first == {"state": "migrated_exact", "rows_migrated": 2}
        assert second == {
            "state": "already_dual_schema",
            "rows_migrated": 0,
        }
        assert third == second
        assert conn.execute(
            "SELECT COUNT(*) FROM basket_target_execution_version_vectors"
        ).fetchone()[0] == 2
        assert "vector_policy" in {
            str(row[1]) for row in conn.execute(
                "PRAGMA table_info(basket_target_execution_version_vectors)"
            )
        }
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"


def test_midflight_failure_rolls_back_to_legacy_shape(tmp_path) -> None:
    db = tmp_path / "legacy.db"
    with sqlite3.connect(db) as conn:
        _seed_parent_tables(conn)
        conn.execute(LEGACY_POPULATED_EXECUTION_VECTOR_DDL)
        conn.execute(CURRENT_INDEX_DDL)
        conn.executescript(CURRENT_TRIGGERS_DDL)
        _seed_parent_rows(
            conn,
            cycle_id="cycle-exec",
            decision_id="decision-exec",
            order_intent_id="intent-exec",
        )
        _insert_execution_row(conn)
        conn.execute(
            "INSERT INTO basket_target_execution_version_vectors("
            "order_intent_id,cycle_id,decision_id,training_fact_id,vector_policy,"
            "target_price_id,quote_observed_at,quote_expires_at,"
            "probability_sha256,official_ws_quote_sha256,inventory_sha256,"
            "order_state_sha256,station_policy_sha256,cash_policy_decision_sha256,"
            "strict_delta_artifact_sha256,vector_json,vector_sha256,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                None,
                "cycle-noexec",
                "decision-noexec",
                "fact-noexec",
                "NO_EXECUTION",
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                '{"policy":"NO_EXECUTION","reason_codes":["missing_quote"]}',
                "3" * 64,
                "2026-07-27T00:00:00Z",
            ),
        )
        conn.execute(
            "INSERT INTO basket_target_execution_cycles(cycle_id) VALUES(?)",
            ("cycle-noexec",),
        )
        conn.execute(
            "INSERT INTO auto_trade_decisions(decision_id) VALUES(?)",
            ("decision-noexec",),
        )
        conn.execute(
            "SAVEPOINT execution_vector_rollback"
        )
        proxy = _FailingExecuteProxy(
            conn, "DROP TABLE basket_target_execution_version_vectors_retired"
        )
        with pytest.raises(RuntimeError, match="injected_execution_vector_migration_fault"):
            upgrade_execution_vector_contract(proxy)
        conn.execute("ROLLBACK TO execution_vector_rollback")
        conn.execute("RELEASE execution_vector_rollback")

        assert conn.execute(
            "SELECT COUNT(*) FROM basket_target_execution_version_vectors"
        ).fetchone()[0] == 2
        assert "vector_policy" in {
            str(row[1]) for row in conn.execute(
                "PRAGMA table_info(basket_target_execution_version_vectors)"
            )
        }
        assert _legacy_names(conn, "trigger") == {
            "basket_target_version_vector_no_delete",
            "basket_target_version_vector_no_update",
        }
        assert "basket_target_no_execution_vector_cycle_idx" in _legacy_names(
            conn, "index"
        )


def test_legacy_table_with_existing_current_partial_index_retains_uniqueness_contract(
    tmp_path,
) -> None:
    db = tmp_path / "legacy.db"
    with sqlite3.connect(db) as conn:
        _seed_parent_tables(conn)
        conn.execute(LEGACY_POPULATED_EXECUTION_VECTOR_DDL)
        conn.execute(CURRENT_INDEX_DDL)
        _seed_parent_rows(
            conn,
            cycle_id="cycle-noexec",
            decision_id="decision-noexec",
            order_intent_id=None,
        )
        conn.execute(
            "INSERT INTO basket_target_execution_version_vectors("
            "order_intent_id,cycle_id,decision_id,training_fact_id,vector_policy,"
            "target_price_id,quote_observed_at,quote_expires_at,"
            "probability_sha256,official_ws_quote_sha256,inventory_sha256,"
            "order_state_sha256,station_policy_sha256,cash_policy_decision_sha256,"
            "strict_delta_artifact_sha256,vector_json,vector_sha256,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                None,
                "cycle-noexec",
                "decision-noexec",
                "fact-noexec",
                "NO_EXECUTION",
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                '{"policy":"NO_EXECUTION","reason_codes":["missing_quote"]}',
                "4" * 64,
                "2026-07-27T00:00:00Z",
            ),
        )

        result = upgrade_execution_vector_contract(conn)

        assert result == {"state": "migrated_exact", "rows_migrated": 1}
        assert "basket_target_no_execution_vector_cycle_idx" in _legacy_names(
            conn, "index"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO basket_target_execution_version_vectors("
                "order_intent_id,cycle_id,decision_id,training_fact_id,vector_policy,"
                "target_price_id,quote_observed_at,quote_expires_at,"
                "probability_sha256,official_ws_quote_sha256,inventory_sha256,"
                "order_state_sha256,station_policy_sha256,cash_policy_decision_sha256,"
                "strict_delta_artifact_sha256,vector_json,vector_sha256,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    None,
                    "cycle-noexec",
                    "decision-noexec-2",
                    "fact-noexec-2",
                    "NO_EXECUTION",
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    '{"policy":"NO_EXECUTION","reason_codes":["missing_quote"]}',
                    "5" * 64,
                    "2026-07-27T00:00:00Z",
                ),
            )
