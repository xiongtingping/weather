import sqlite3

from auto_trade_job_writer import (
    claim_auto_trade_job_running,
    delete_terminal_auto_trade_jobs_before,
    insert_auto_trade_job,
    mark_superseded_terminal_jobs_with_newer_active,
    touch_running_auto_trade_job,
    write_auto_trade_job_final_state,
)


def _create_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE auto_trade_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT NOT NULL,
            source TEXT,
            state TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 0,
            payload_json TEXT,
            lease_until TEXT,
            retry_count INTEGER NOT NULL DEFAULT 0,
            max_retry INTEGER NOT NULL DEFAULT 3,
            next_retry_at TEXT,
            last_error TEXT,
            started_at TEXT,
            ended_at TEXT,
            run_id INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )


def _create_table_with_identity(conn: sqlite3.Connection) -> None:
    _create_table(conn)
    conn.execute("ALTER TABLE auto_trade_jobs ADD COLUMN correlation_id TEXT")
    conn.execute("ALTER TABLE auto_trade_jobs ADD COLUMN decision_id TEXT")
    conn.execute("ALTER TABLE auto_trade_jobs ADD COLUMN signal_id TEXT")
    conn.execute("ALTER TABLE auto_trade_jobs ADD COLUMN order_intent_id TEXT")


def test_auto_trade_job_writer_lifecycle():
    conn = sqlite3.connect(":memory:")
    _create_table(conn)

    job_id = insert_auto_trade_job(
        conn,
        run_date="2026-07-03",
        source="posterior_target_direct",
        priority=10,
        payload={"target": "32"},
        max_retry=2,
        next_retry_at="2026-07-03T16:05:00Z",
        created_at="2026-07-03T16:05:00Z",
        updated_at="2026-07-03T16:05:00Z",
    )
    assert job_id == 1

    assert claim_auto_trade_job_running(
        conn,
        job_id=job_id,
        lease_until="2026-07-03T16:06:00Z",
        now_iso="2026-07-03T16:05:30Z",
    ) == 1
    assert touch_running_auto_trade_job(
        conn,
        job_id=job_id,
        lease_until="2026-07-03T16:06:30Z",
        now_iso="2026-07-03T16:05:45Z",
        run_id=99,
    ) == 1
    assert write_auto_trade_job_final_state(
        conn,
        job_id=job_id,
        state="success",
        ended_at="2026-07-03T16:05:55Z",
        run_id=99,
    ) == 1

    row = conn.execute(
        "SELECT state, run_id, lease_until, last_error FROM auto_trade_jobs WHERE id = ?",
        (job_id,),
    ).fetchone()
    assert row == ("success", 99, "", "")


def test_auto_trade_job_writer_cleanup_helpers():
    conn = sqlite3.connect(":memory:")
    _create_table(conn)
    conn.executemany(
        """
        INSERT INTO auto_trade_jobs(
            run_date, source, state, priority, payload_json, lease_until,
            retry_count, max_retry, next_retry_at, last_error, started_at,
            ended_at, run_id, created_at, updated_at
        )
        VALUES (?, 'test', ?, 0, '', '', 0, 1, '', '', '', '', NULL, ?, ?)
        """,
        [
            ("2026-07-03", "failed", "2026-07-01T00:00:00Z", "2026-07-01T00:00:00Z"),
            ("2026-07-03", "queued", "2026-07-03T16:00:00Z", "2026-07-03T16:00:00Z"),
            ("2026-07-02", "success", "2026-07-01T00:00:00Z", "2026-07-01T00:00:00Z"),
        ],
    )

    assert mark_superseded_terminal_jobs_with_newer_active(
        conn,
        now_iso="2026-07-03T16:01:00Z",
    ) == 1
    assert delete_terminal_auto_trade_jobs_before(
        conn,
        cutoff_iso="2026-07-02T00:00:00Z",
    ) == 1
    rows = conn.execute("SELECT state FROM auto_trade_jobs ORDER BY id").fetchall()
    assert rows == [("superseded",), ("queued",)]


def test_insert_auto_trade_job_merge_fills_identity_columns():
    conn = sqlite3.connect(":memory:")
    _create_table_with_identity(conn)

    job_id = insert_auto_trade_job(
        conn,
        run_date="2026-07-07",
        source="posterior_target_direct",
        priority=10,
        payload={
            "decision_id": "dec_1234567890abcdef",
            "signal_id": "sig_1234567890abcdef",
            "signal_to_order_timing": {
                "order_intent_id": "oint_1234567890abcdef",
            },
        },
        max_retry=2,
        next_retry_at="2026-07-07T11:48:17Z",
        created_at="2026-07-07T11:48:17Z",
        updated_at="2026-07-07T11:48:17Z",
    )

    row = conn.execute(
        """
        SELECT correlation_id, decision_id, signal_id, order_intent_id
        FROM auto_trade_jobs
        WHERE id = ?
        """,
        (job_id,),
    ).fetchone()
    assert row[0].startswith("corr_")
    assert row[1] == "dec_1234567890abcdef"
    assert row[2] == "sig_1234567890abcdef"
    assert row[3] == "oint_1234567890abcdef"
