import json
import sqlite3
from datetime import datetime, timedelta, timezone


def _mk_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE edge_trade_fills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT,
            event_date TEXT,
            outcome TEXT,
            outcome_side TEXT,
            token_id TEXT,
            trade_id TEXT,
            order_id TEXT,
            trade_side TEXT,
            price REAL,
            size REAL,
            fee REAL,
            trade_time TEXT,
            raw_json TEXT,
            created_at TEXT
        )
        """
    )
    cur.execute(
        """
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
            created_at TEXT,
            q_raw REAL,
            q_calibrated REAL,
            q_edge REAL,
            order_type TEXT,
            order_status TEXT,
            order_checked_at TEXT,
            strategy TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE mirror_follow_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mirror_order_id TEXT,
            created_at TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE edge_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            last_order_id TEXT
        )
        """
    )
    conn.commit()
    return conn


def test_orphan_fill_stats_classification_and_writeback():
    from ui.server import compute_orphan_fill_stats

    conn = _mk_conn()
    cur = conn.cursor()
    now = datetime(2026, 2, 24, 12, 0, 0, tzinfo=timezone.utc)
    ts = (now - timedelta(days=1)).isoformat()

    # 1) Non-orphan: fill order_id exists in auto_orders.
    cur.execute(
        "INSERT INTO auto_orders(order_id, created_at) VALUES (?, ?)",
        ("auto1", ts),
    )
    cur.execute(
        """
        INSERT INTO edge_trade_fills(trade_id, order_id, trade_time, created_at, raw_json, slug, event_date, outcome, outcome_side, trade_side, price, size, fee)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("t_auto", "auto1", ts, ts, json.dumps({"trader_side": "TAKER"}), "s", "2026-02-24", "X", "YES", "BUY", 0.1, 10.0, 0.0),
    )

    # 2) Synthetic clears.
    cur.execute(
        """
        INSERT INTO edge_trade_fills(trade_id, order_id, trade_time, created_at, raw_json)
        VALUES (?, ?, ?, ?, ?)
        """,
        ("residual_clear:abc", "", ts, ts, ""),
    )
    cur.execute(
        """
        INSERT INTO edge_trade_fills(trade_id, order_id, trade_time, created_at, raw_json)
        VALUES (?, ?, ?, ?, ?)
        """,
        ("manual_clear:abc", "", ts, ts, ""),
    )

    # 3) Mirror follow orphan (order_id present but not in auto_orders).
    cur.execute("INSERT INTO mirror_follow_trades(mirror_order_id, created_at) VALUES (?, ?)", ("mm1", ts))
    cur.execute(
        "INSERT INTO edge_trade_fills(trade_id, order_id, trade_time, created_at, raw_json) VALUES (?, ?, ?, ?, ?)",
        ("t_mm", "mm1", ts, ts, json.dumps({"trader_side": "TAKER"})),
    )

    # 4) Edge trades last_order_id orphan.
    cur.execute("INSERT INTO edge_trades(last_order_id) VALUES (?)", ("e1",))
    cur.execute(
        "INSERT INTO edge_trade_fills(trade_id, order_id, trade_time, created_at, raw_json) VALUES (?, ?, ?, ?, ?)",
        ("t_e1", "e1", ts, ts, json.dumps({"trader_side": "MAKER"})),
    )

    # 5) Trader side based.
    cur.execute(
        "INSERT INTO edge_trade_fills(trade_id, order_id, trade_time, created_at, raw_json) VALUES (?, ?, ?, ?, ?)",
        ("t_taker", "o_taker", ts, ts, json.dumps({"trader_side": "TAKER"})),
    )
    cur.execute(
        "INSERT INTO edge_trade_fills(trade_id, order_id, trade_time, created_at, raw_json) VALUES (?, ?, ?, ?, ?)",
        ("t_maker", "o_maker", ts, ts, json.dumps({"trader_side": "MAKER"})),
    )
    conn.commit()

    payload = compute_orphan_fill_stats(conn, days=60, now_dt=now, max_rows=1000, writeback=True)
    assert payload["ok"] is True
    assert payload["fills_in_window"] >= 7
    # orphan excludes the one tied to auto_orders
    assert payload["orphan_fill_count"] == 6
    by_class = payload["by_class"]
    assert by_class["synthetic_residual_clear"] == 1
    assert by_class["synthetic_manual_clear"] == 1
    assert by_class["mirror_follow_missing_auto_orders"] == 1
    assert by_class["edge_trades_last_order_id_missing_auto_orders"] == 1
    assert by_class["orphan_order_taker_no_auto_order"] == 1
    assert by_class["orphan_order_maker_no_auto_order"] == 1

    # writeback table present with 6 rows
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM orphan_fill_classifications")
    assert int(cur.fetchone()[0] or 0) == 6

