from __future__ import annotations

import sqlite3

from bayes_basket_target.future_day_residue import audit_future_day_residue
from bayes_basket_target.market_registry import (
    apply_target_market_registry_schema,
    load_started_registry_rows,
)


AS_OF = "2026-07-24T20:00:00Z"
FUTURE_DAY = "2026-07-25"
CREATED_BEFORE_START = "2026-07-24T14:26:49.366Z"
SNAPSHOT = "a" * 64


def _base_db(*, include_current_registry: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE basket_target_execution_cycles(
            cycle_id TEXT PRIMARY KEY,
            event_date TEXT NOT NULL,
            settlement_timezone TEXT NOT NULL,
            created_at TEXT NOT NULL,
            state TEXT NOT NULL
        );
        CREATE TABLE auto_trade_decisions(
            cycle_id TEXT NOT NULL,
            event_date TEXT NOT NULL,
            settlement_timezone TEXT NOT NULL,
            created_at TEXT NOT NULL,
            decided_state TEXT NOT NULL,
            will_order INTEGER NOT NULL
        );
        CREATE TABLE basket_target_order_intents(cycle_id TEXT NOT NULL);
        CREATE TABLE basket_target_capital_reservations(cycle_id TEXT NOT NULL);
        CREATE TABLE basket_target_order_reconciliations(cycle_id TEXT NOT NULL);
        """
    )
    apply_target_market_registry_schema(conn)
    conn.execute(
        "INSERT INTO basket_target_market_registry_snapshot "
        "(snapshot_sha256,source_schema,source_scope,exported_at_utc,source_database_path,"
        "row_count,ingested_at_utc,content_sha256) VALUES(?,?,?,?,?,?,?,?)",
        (
            SNAPSHOT,
            "reported_trajectory_market_registry_v1",
            "shared_live_registry_export_only",
            AS_OF,
            "/isolated/source.db",
            1,
            AS_OF,
            SNAPSHOT,
        ),
    )
    if include_current_registry:
        conn.execute(
            "INSERT INTO basket_target_market_registry VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                SNAPSHOT,
                "current",
                "2026-07-24",
                "38°C",
                "current",
                "TEST",
                "America/New_York",
                "C",
                "amsc",
                1,
                "",
                "",
                "b" * 64,
                "yes-current",
                "no-current",
                1,
                1,
                '{"market_id":"current"}',
                AS_OF,
            ),
        )
    return conn


def _add_future_residue(conn: sqlite3.Connection, *, will_order: int = 0) -> None:
    conn.execute(
        "INSERT INTO basket_target_execution_cycles VALUES(?,?,?,?,?)",
        ("cycle-future", FUTURE_DAY, "America/New_York", CREATED_BEFORE_START, "prepared"),
    )
    conn.execute(
        "INSERT INTO auto_trade_decisions VALUES(?,?,?,?,?,?)",
        ("cycle-future", FUTURE_DAY, "America/New_York", CREATED_BEFORE_START, "blocked", will_order),
    )


def test_blocked_no_order_future_residue_is_isolated() -> None:
    conn = _base_db()
    _add_future_residue(conn)

    result = audit_future_day_residue(conn, as_of_utc=AS_OF)

    assert result["checks"]["future_residue_isolated"] is True
    assert result["checks"]["future_decisions_are_blocked_no_order"] is True
    assert result["checks"]["no_order_intents_or_capital_downstream"] is True
    assert result["consumer_filter"]["future_scope_excluded"] is True
    assert result["blockers"] == []
    assert result["checks"]["writes_target_database"] is False


def test_future_order_or_capital_downstream_is_a_blocker() -> None:
    conn = _base_db()
    _add_future_residue(conn, will_order=1)
    conn.execute("INSERT INTO basket_target_order_intents VALUES(?)", ("cycle-future",))
    conn.execute("INSERT INTO basket_target_capital_reservations VALUES(?)", ("cycle-future",))

    result = audit_future_day_residue(conn, as_of_utc=AS_OF)

    assert result["checks"]["future_residue_isolated"] is False
    assert "future_decision_not_blocked_or_will_order_nonzero" in result["blockers"]
    assert "future_residue_has_order_or_capital_downstream" in result["blockers"]


def test_started_registry_consumer_excludes_future_scope() -> None:
    conn = _base_db(include_current_registry=False)
    conn.execute(
        "INSERT INTO basket_target_market_registry VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            SNAPSHOT,
            "future",
            FUTURE_DAY,
            "38°C",
            "future",
            "TEST",
            "America/New_York",
            "C",
            "amsc",
            1,
            "",
            "",
            "c" * 64,
            "yes-future",
            "no-future",
            1,
            1,
            '{"market_id":"future"}',
            AS_OF,
        ),
    )

    rows = load_started_registry_rows(conn, as_of_utc=AS_OF)

    assert rows == []
