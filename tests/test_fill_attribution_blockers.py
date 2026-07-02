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
            trade_id TEXT,
            order_id TEXT,
            slug TEXT,
            event_date TEXT,
            outcome TEXT,
            outcome_side TEXT,
            trade_side TEXT,
            trade_time TEXT,
            created_at TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE trade_fill_attributions (
            trade_id TEXT PRIMARY KEY,
            linked INTEGER NOT NULL DEFAULT 0,
            reason TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE auto_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT,
            created_at TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE orphan_fill_classifications (
            trade_id TEXT PRIMARY KEY,
            classification TEXT
        )
        """
    )
    conn.commit()
    return conn


def _insert_fill(conn, trade_id, order_id, trade_time, *, linked=0, reason="no_auto_order"):
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO edge_trade_fills(
            trade_id, order_id, slug, event_date, outcome, outcome_side,
            trade_side, trade_time, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            trade_id,
            order_id,
            "highest-temperature-in-test",
            "2026-02-24",
            "30°C",
            "YES",
            "BUY",
            trade_time,
            trade_time,
        ),
    )
    cur.execute(
        """
        INSERT INTO trade_fill_attributions(trade_id, linked, reason)
        VALUES (?, ?, ?)
        """,
        (trade_id, int(linked), reason),
    )
    conn.commit()


def test_fill_attribution_blockers_ignore_pre_auto_order_history():
    from ui.server import collect_fill_attribution_blockers

    conn = _mk_conn()
    now = datetime.now(timezone.utc)
    first_auto = now - timedelta(hours=12)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO auto_orders(order_id, created_at) VALUES (?, ?)",
        ("auto_1", first_auto.isoformat(timespec="seconds")),
    )
    _insert_fill(
        conn,
        "legacy_gap",
        "external_order",
        (now - timedelta(hours=24)).isoformat(timespec="seconds"),
        linked=0,
        reason="no_auto_order",
    )
    _insert_fill(
        conn,
        "linked_fill",
        "auto_1",
        (now - timedelta(hours=1)).isoformat(timespec="seconds"),
        linked=1,
        reason="linked",
    )

    payload = collect_fill_attribution_blockers(conn, window_hours=72)

    assert payload["raw_fills_with_order"] == 2
    assert payload["raw_unlinked"] == 1
    assert payload["ignored_unlinked"] == 1
    assert payload["fills_with_order"] == 1
    assert payload["linked"] == 1
    assert payload["unlinked"] == 0
    assert payload["link_ratio"] == 1.0


def test_fill_attribution_blockers_keep_post_auto_order_gap():
    from ui.server import collect_fill_attribution_blockers

    conn = _mk_conn()
    now = datetime.now(timezone.utc)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO auto_orders(order_id, created_at) VALUES (?, ?)",
        ("auto_1", (now - timedelta(hours=48)).isoformat(timespec="seconds")),
    )
    _insert_fill(
        conn,
        "real_gap",
        "unmatched_order",
        (now - timedelta(hours=1)).isoformat(timespec="seconds"),
        linked=0,
        reason="no_auto_order",
    )

    payload = collect_fill_attribution_blockers(conn, window_hours=72)

    assert payload["raw_fills_with_order"] == 1
    assert payload["ignored_unlinked"] == 0
    assert payload["fills_with_order"] == 1
    assert payload["linked"] == 0
    assert payload["unlinked"] == 1


def test_fill_attribution_blockers_ignore_orphan_classified_gap():
    from ui.server import collect_fill_attribution_blockers

    conn = _mk_conn()
    now = datetime.now(timezone.utc)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO auto_orders(order_id, created_at) VALUES (?, ?)",
        ("auto_1", (now - timedelta(hours=48)).isoformat(timespec="seconds")),
    )
    _insert_fill(
        conn,
        "orphan_gap",
        "external_order",
        (now - timedelta(hours=1)).isoformat(timespec="seconds"),
        linked=0,
        reason="no_auto_order",
    )
    cur.execute(
        """
        INSERT INTO orphan_fill_classifications(trade_id, classification)
        VALUES (?, ?)
        """,
        ("orphan_gap", "orphan_order_taker_no_auto_order"),
    )
    conn.commit()

    payload = collect_fill_attribution_blockers(conn, window_hours=72)

    assert payload["raw_fills_with_order"] == 1
    assert payload["raw_unlinked"] == 1
    assert payload["ignored_unlinked"] == 1
    assert payload["fills_with_order"] == 0
    assert payload["unlinked"] == 0
