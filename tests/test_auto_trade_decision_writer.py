import json
import sqlite3

from auto_trade_decision_writer import (
    fill_missing_auto_trade_decision_run_id,
    insert_auto_trade_decision,
    insert_auto_trade_decision_rows,
)


def _create_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE auto_trade_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER,
            run_date TEXT,
            slug TEXT,
            event_date TEXT,
            title TEXT,
            outcome TEXT,
            side TEXT,
            action TEXT,
            decided_state TEXT,
            will_order INTEGER,
            reason_codes_json TEXT,
            reasons_json TEXT,
            details_json TEXT,
            created_at TEXT
        )
        """
    )


def _create_table_with_identity(conn: sqlite3.Connection) -> None:
    _create_table(conn)
    conn.execute("ALTER TABLE auto_trade_decisions ADD COLUMN correlation_id TEXT")
    conn.execute("ALTER TABLE auto_trade_decisions ADD COLUMN fact_snapshot_id TEXT")
    conn.execute("ALTER TABLE auto_trade_decisions ADD COLUMN posterior_run_id TEXT")
    conn.execute("ALTER TABLE auto_trade_decisions ADD COLUMN decision_id TEXT")
    conn.execute("ALTER TABLE auto_trade_decisions ADD COLUMN signal_id TEXT")
    conn.execute("ALTER TABLE auto_trade_decisions ADD COLUMN order_intent_id TEXT")


def test_insert_auto_trade_decision_and_fill_missing_run_id():
    conn = sqlite3.connect(":memory:")
    _create_table(conn)

    decision_id = insert_auto_trade_decision(
        conn,
        run_date="2026-07-03",
        slug="highest-temperature-in-chengdu-on-july-3",
        event_date="2026-07-03",
        title="Highest temperature in Chengdu on July 3?",
        outcome="30",
        side="YES",
        action="entry",
        decided_state="blocked",
        will_order=False,
        reason_codes=["posterior_below_threshold"],
        reasons=["posterior below threshold"],
        details={"phase": "D0_late"},
        created_at="2026-07-03T15:59:00Z",
    )

    assert decision_id == 1
    assert fill_missing_auto_trade_decision_run_id(conn, decision_id=decision_id, run_id=42) is True
    row = conn.execute(
        "SELECT run_id, will_order, reason_codes_json, details_json FROM auto_trade_decisions WHERE id = ?",
        (decision_id,),
    ).fetchone()
    assert row[0] == 42
    assert row[1] == 0
    assert json.loads(row[2]) == ["posterior_below_threshold"]
    assert json.loads(row[3])["phase"] == "D0_late"


def test_insert_auto_trade_decision_rows_preserves_existing_json_text():
    conn = sqlite3.connect(":memory:")
    _create_table(conn)

    count = insert_auto_trade_decision_rows(
        conn,
        [
            (
                None,
                "2026-07-03",
                "slug-a",
                "2026-07-03",
                "title-a",
                "31",
                "YES",
                "entry",
                "will_order",
                1,
                '["order_submitted"]',
                '["submitted"]',
                '{"order_id":"abc"}',
                "2026-07-03T16:05:30Z",
            )
        ],
    )

    assert count == 1
    row = conn.execute("SELECT will_order, details_json FROM auto_trade_decisions").fetchone()
    assert row == (1, '{"order_id":"abc"}')


def test_insert_auto_trade_decision_merge_fills_identity_columns():
    conn = sqlite3.connect(":memory:")
    _create_table_with_identity(conn)

    decision_row_id = insert_auto_trade_decision(
        conn,
        run_date="2026-07-07",
        slug="highest-temperature-in-chengdu-on-july-7-2026",
        event_date="2026-07-07",
        title="Highest temperature in Chengdu on July 7?",
        outcome="37",
        side="YES",
        action="entry",
        decided_state="blocked",
        will_order=False,
        reason_codes=["posterior_target_direct_no_order"],
        reasons=["no order"],
        details={
            "posterior_run_id": "post_1234567890abcdef",
            "fact_snapshot_id": "fact_1234567890abcdef",
            "order_intent_id": "oint_1234567890abcdef",
        },
        created_at="2026-07-07T11:48:17Z",
    )

    row = conn.execute(
        """
        SELECT decision_id, correlation_id, fact_snapshot_id, posterior_run_id, order_intent_id
        FROM auto_trade_decisions
        WHERE id = ?
        """,
        (decision_row_id,),
    ).fetchone()
    assert row[0].startswith("dec_")
    assert row[1].startswith("corr_")
    assert row[2] == "fact_1234567890abcdef"
    assert row[3] == "post_1234567890abcdef"
    assert row[4] == "oint_1234567890abcdef"
