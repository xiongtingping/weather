import sqlite3

from edge_trade_ledger_writer import (
    clear_balance_skip_after_no_position,
    copy_acceptance_clear_target,
    copy_acceptance_seed_old,
    insert_edge_trade_row,
    reconcile_exit_no_position_blockers,
    reconcile_pending_entry_order_id,
    upsert_edge_trade_runtime_row,
)


def _create_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE edge_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT,
            event_date TEXT,
            outcome TEXT,
            side TEXT,
            state TEXT,
            entry_at TEXT,
            last_order_id TEXT,
            entry_price REAL,
            entry_size REAL,
            last_action TEXT,
            last_action_price REAL,
            last_action_size REAL,
            last_action_order_type TEXT,
            last_action_at TEXT,
            last_status TEXT,
            last_error TEXT,
            exit_reasons TEXT,
            exit_reason_codes TEXT,
            exit_reason_family_codes TEXT,
            exit_triggered_at TEXT,
            stop_loss_probe_at TEXT,
            exit_skip_reason TEXT,
            exit_skip_detail TEXT,
            exit_skip_at TEXT,
            trend_limit_step INTEGER,
            entry_retry_count INTEGER,
            exit_retry_count INTEGER,
            position_size REAL,
            avg_price REAL,
            peak_price REAL,
            realized_pnl REAL,
            unrealized_pnl REAL,
            mark_price REAL,
            last_fill_at TEXT,
            updated_at TEXT,
            UNIQUE(slug, event_date, outcome, side)
        )
        """
    )


def test_copy_acceptance_helpers_preserve_expression_semantics():
    conn = sqlite3.connect(":memory:")
    _create_table(conn)
    conn.execute(
        """
        INSERT INTO edge_trades(slug, event_date, outcome, side, state, position_size, avg_price)
        VALUES ('s', '2026-07-03', '32', 'YES', 'entry', 3, 0.44)
        """
    )
    conn.execute(
        """
        INSERT INTO edge_trades(slug, event_date, outcome, side, state, position_size, avg_price)
        VALUES ('s', '2026-07-03T00:00:00', '31', 'YES', 'exit', 0.5, 0)
        """
    )

    assert copy_acceptance_clear_target(
        conn,
        now_iso="now",
        slug="s",
        event_date="2026-07-03",
        target_bucket="32",
    ) == 1
    assert copy_acceptance_seed_old(
        conn,
        now_iso="now",
        slug="s",
        event_date="2026-07-03",
        old_bucket="31",
    ) == 1

    target = conn.execute(
        "SELECT state, position_size, avg_price, last_action FROM edge_trades WHERE outcome = '32'"
    ).fetchone()
    old = conn.execute(
        "SELECT state, position_size, avg_price, last_action FROM edge_trades WHERE outcome = '31'"
    ).fetchone()
    assert target == ("exit", 0, 0.44, "copy_acceptance_clear_target")
    assert old == ("entry", 8.0, 0.25, "copy_acceptance_seed_old")


def test_runtime_upsert_does_not_blank_existing_family_codes():
    conn = sqlite3.connect(":memory:")
    _create_table(conn)

    base = {
        "slug": "s",
        "event_date": "2026-07-03",
        "outcome": "32",
        "side": "YES",
        "state": "entry",
        "exit_reason_family_codes": "target_realization",
        "position_size": 2.0,
        "avg_price": 0.41,
        "updated_at": "t1",
    }
    assert upsert_edge_trade_runtime_row(conn, base) == 1
    assert upsert_edge_trade_runtime_row(
        conn,
        {
            **base,
            "state": "exit",
            "exit_reason_family_codes": "",
            "position_size": 0.0,
            "updated_at": "t2",
        },
    ) == 1

    row = conn.execute(
        """
        SELECT state, exit_reason_family_codes, position_size, avg_price, updated_at
        FROM edge_trades
        WHERE slug = 's' AND event_date = '2026-07-03' AND outcome = '32' AND side = 'YES'
        """
    ).fetchone()
    assert row == ("exit", "target_realization", 0.0, 0.41, "t2")


def test_reconcile_helpers_keep_their_where_guards():
    conn = sqlite3.connect(":memory:")
    _create_table(conn)
    insert_edge_trade_row(
        conn,
        {
            "slug": "s",
            "event_date": "2026-07-03",
            "outcome": "32",
            "side": "YES",
            "state": "entry_pending",
            "last_order_id": "",
            "last_error": "pending_timeout",
            "position_size": 0,
        },
    )
    insert_edge_trade_row(
        conn,
        {
            "slug": "s",
            "event_date": "2026-07-03",
            "outcome": "31",
            "side": "YES",
            "state": "exit",
            "last_order_id": "",
            "last_error": "pending_timeout",
            "position_size": 0,
        },
    )

    assert reconcile_pending_entry_order_id(
        conn,
        slug="s",
        event_date="2026-07-03",
        outcome="32",
        side="YES",
        order_id="order-1",
        status="submitted",
        now_iso="now",
    ) == 1
    assert reconcile_exit_no_position_blockers(conn, now_iso="now") == 1

    rows = conn.execute(
        "SELECT outcome, state, last_order_id, last_error, last_status FROM edge_trades ORDER BY outcome"
    ).fetchall()
    assert rows == [
        ("31", "exit", "", "", "no_position"),
        ("32", "entry_pending", "order-1", "", "submitted"),
    ]


def test_clear_balance_skip_after_no_position_sets_nulls_under_size_guard():
    conn = sqlite3.connect(":memory:")
    _create_table(conn)
    insert_edge_trade_row(
        conn,
        {
            "slug": "s",
            "event_date": "2026-07-03",
            "outcome": "32",
            "side": "YES",
            "last_error": "no_balance",
            "last_status": "blocked",
            "exit_skip_reason": "no_balance",
            "exit_skip_detail": "raw",
            "exit_skip_at": "old",
            "position_size": 0,
        },
    )

    assert clear_balance_skip_after_no_position(
        conn,
        slug="s",
        event_date="2026-07-03",
        outcome="32",
        side="YES",
        max_position_size=0.01,
        now_iso="now",
    ) == 1

    row = conn.execute(
        "SELECT last_error, last_status, exit_skip_reason, exit_skip_detail, exit_skip_at, updated_at FROM edge_trades"
    ).fetchone()
    assert row == (None, None, None, None, None, "now")
