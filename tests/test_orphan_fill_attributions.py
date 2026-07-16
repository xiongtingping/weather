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
            created_at TEXT
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
    cur.execute(
        """
        CREATE TABLE auto_trade_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            slug TEXT,
            event_date TEXT,
            outcome TEXT,
            side TEXT,
            action TEXT,
            will_order INTEGER,
            reason_codes_json TEXT,
            details_json TEXT,
            created_at TEXT
        )
        """
    )
    conn.commit()
    return conn


def test_orphan_fill_infers_from_auto_trade_decisions_order_id_writeback():
    from ui.server import compute_orphan_fill_stats

    conn = _mk_conn()
    cur = conn.cursor()
    now = datetime(2026, 2, 24, 12, 0, 0, tzinfo=timezone.utc)
    ts = (now - timedelta(hours=1)).isoformat()

    # Orphan taker fill: order_id not in auto_orders.
    cur.execute(
        """
        INSERT INTO edge_trade_fills(
            trade_id, order_id, slug, event_date, outcome, outcome_side, trade_side,
            price, size, fee, trade_time, created_at, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "t1",
            "ord_1",
            "s",
            "2026-02-24",
            "X",
            "YES",
            "BUY",
            0.12,
            10.0,
            0.0,
            ts,
            ts,
            json.dumps({"trader_side": "TAKER"}),
        ),
    )

    # Decision row with matching details_json.order_id.
    cur.execute(
        """
        INSERT INTO auto_trade_decisions(
            run_date, slug, event_date, outcome, side, action, will_order,
            reason_codes_json, details_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "2026-02-24",
            "s",
            "2026-02-24",
            "X",
            "YES",
            "entry",
            1,
            json.dumps(["order_submitted"]),
            json.dumps({"order_id": "ord_1", "strategy": "yes_top"}),
            ts,
        ),
    )
    conn.commit()

    payload = compute_orphan_fill_stats(conn, days=2, now_dt=now, max_rows=10, writeback=True)
    assert payload["ok"] is True
    assert payload["orphan_fill_count"] == 1
    assert payload["by_class"]["orphan_order_taker_no_auto_order"] == 1
    assert payload["by_inferred_source"]["auto_trade_decisions"] == 1

    row = payload["rows"][0]
    assert row["inferred_source"] == "auto_trade_decisions"
    assert row["inferred_strategy"] == "yes_top"
    assert float(row["inferred_confidence"] or 0.0) >= 0.8

    cur.execute("SELECT inferred_source, inferred_strategy FROM orphan_order_attributions WHERE order_id = ?", ("ord_1",))
    got = cur.fetchone()
    assert got is not None
    assert str(got[0] or "") == "auto_trade_decisions"
    assert str(got[1] or "") == "yes_top"
