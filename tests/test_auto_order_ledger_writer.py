import sqlite3

from auto_order_ledger_writer import (
    insert_auto_order_ignore,
    merge_fill_auto_order_lineage_by_order_id,
    update_auto_order_status_by_id,
    update_auto_order_status_by_order_id,
    update_auto_order_strategy_by_id,
)


def _create_table(conn: sqlite3.Connection, *, include_lineage: bool = False) -> None:
    lineage_cols = ""
    if include_lineage:
        lineage_cols = """
            ,
            correlation_id TEXT,
            decision_id TEXT,
            signal_id TEXT,
            order_intent_id TEXT
        """
    conn.execute(
        f"""
        CREATE TABLE auto_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT UNIQUE,
            slug TEXT,
            event_date TEXT,
            outcome TEXT,
            side TEXT,
            action TEXT,
            price REAL,
            size REAL,
            order_type TEXT,
            q_raw REAL,
            q_calibrated REAL,
            q_edge REAL,
            strategy TEXT,
            created_at TEXT,
            order_status TEXT,
            order_checked_at TEXT
            {lineage_cols}
        )
        """
    )


def test_auto_order_insert_ignore_and_status_updates():
    conn = sqlite3.connect(":memory:")
    _create_table(conn)

    assert insert_auto_order_ignore(
        conn,
        order_id="order-1",
        slug="slug",
        event_date="2026-07-03",
        outcome="32",
        side="YES",
        action="entry",
        price=0.42,
        size=5.0,
        order_type="GTC",
        strategy="posterior_target_direct",
        created_at="2026-07-03T16:05:00Z",
    ) == 1
    assert insert_auto_order_ignore(
        conn,
        order_id="order-1",
        slug="slug",
        event_date="2026-07-03",
        outcome="32",
        side="YES",
        action="entry",
        created_at="2026-07-03T16:05:01Z",
    ) == 0

    assert update_auto_order_status_by_order_id(
        conn,
        order_id="order-1",
        order_status="filled",
        order_checked_at="2026-07-03T16:05:30Z",
    ) == 1
    assert update_auto_order_status_by_id(
        conn,
        row_id=1,
        order_status="missing",
        order_checked_at="2026-07-03T16:05:31Z",
        only_if_blank=True,
    ) == 1
    assert update_auto_order_strategy_by_id(conn, row_id=1, strategy="main") == 1

    row = conn.execute(
        "SELECT order_status, order_checked_at, strategy FROM auto_orders WHERE order_id = 'order-1'"
    ).fetchone()
    assert row == ("filled", "2026-07-03T16:05:31Z", "main")


def test_auto_order_insert_writes_lineage_when_columns_exist():
    conn = sqlite3.connect(":memory:")
    _create_table(conn, include_lineage=True)

    assert insert_auto_order_ignore(
        conn,
        order_id="order-2",
        correlation_id="corr-1",
        decision_id="101",
        signal_id="sig-1",
        order_intent_id="intent-1",
        slug="slug",
        event_date="2026-07-03",
        outcome="32",
        side="YES",
        action="entry",
        created_at="2026-07-03T16:05:00Z",
    ) == 1

    row = conn.execute(
        """
        SELECT correlation_id, decision_id, signal_id, order_intent_id
        FROM auto_orders
        WHERE order_id = 'order-2'
        """
    ).fetchone()
    assert row == ("corr-1", "101", "sig-1", "intent-1")


def test_auto_order_insert_ignores_lineage_when_columns_missing():
    conn = sqlite3.connect(":memory:")
    _create_table(conn, include_lineage=False)

    assert insert_auto_order_ignore(
        conn,
        order_id="order-3",
        correlation_id="corr-ignored",
        decision_id="102",
        signal_id="sig-ignored",
        order_intent_id="intent-ignored",
        slug="slug",
        event_date="2026-07-03",
        outcome="32",
        side="YES",
        action="entry",
        created_at="2026-07-03T16:05:00Z",
    ) == 1

    row = conn.execute("SELECT order_id, slug FROM auto_orders WHERE order_id = 'order-3'").fetchone()
    assert row == ("order-3", "slug")


def test_auto_order_lineage_merge_fill_does_not_overwrite_existing():
    conn = sqlite3.connect(":memory:")
    _create_table(conn, include_lineage=True)
    insert_auto_order_ignore(
        conn,
        order_id="order-4",
        decision_id="old-decision",
        slug="slug",
        event_date="2026-07-03",
        outcome="32",
        side="YES",
        action="entry",
        created_at="2026-07-03T16:05:00Z",
    )

    assert merge_fill_auto_order_lineage_by_order_id(
        conn,
        order_id="order-4",
        correlation_id="corr-4",
        decision_id="new-decision",
        signal_id="sig-4",
        order_intent_id="intent-4",
    ) == 1

    row = conn.execute(
        """
        SELECT correlation_id, decision_id, signal_id, order_intent_id
        FROM auto_orders
        WHERE order_id = 'order-4'
        """
    ).fetchone()
    assert row == ("corr-4", "old-decision", "sig-4", "intent-4")
