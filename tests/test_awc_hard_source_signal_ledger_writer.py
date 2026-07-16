import sqlite3

from awc_hard_source_signal_ledger_writer import (
    AWC_HARD_SOURCE_SIGNAL_COLUMNS,
    awc_hard_source_signals_schema_status,
    upsert_awc_hard_source_signal,
)


def _create_awc_signal_table(conn: sqlite3.Connection) -> None:
    columns = []
    for column in AWC_HARD_SOURCE_SIGNAL_COLUMNS:
        suffix = " PRIMARY KEY" if column == "signal_id" else ""
        columns.append(f"{column} TEXT{suffix}")
    conn.execute(f"CREATE TABLE awc_hard_source_signals ({', '.join(columns)})")


def test_awc_hard_source_signal_writer_noops_when_schema_missing():
    conn = sqlite3.connect(":memory:")

    result = upsert_awc_hard_source_signal(
        conn,
        {
            "signal_id": "sig_1234567890abcdef",
            "awc_hard_source_signal_at": "2026-07-03T09:05:38Z",
            "acceptance_state": "accepted",
            "terminal_state": "decision_written",
        },
        created_at="2026-07-03T09:05:38Z",
    )

    assert result["ok"] is True
    assert result["status"] == "schema_missing_noop"
    assert result["writes_archive_db"] is False
    assert result["schema_status"]["exists"] is False


def test_awc_hard_source_signal_writer_upserts_and_advances_terminal():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _create_awc_signal_table(conn)

    assert awc_hard_source_signals_schema_status(conn)["ok"] is True
    first = upsert_awc_hard_source_signal(
        conn,
        {
            "signal_id": "sig_1234567890abcdef",
            "source_event_id": "srcevt_1",
            "slug": "highest-temperature-in-chengdu-on-july-3-2026",
            "event_date": "2026-07-03",
            "awc_valid_time": "2026-07-03T09:00:00Z",
            "awc_hard_source_signal_at": "2026-07-03T09:05:38Z",
            "acceptance_state": "accepted",
            "terminal_state": "decision_written",
            "state_reason": "awaiting_job_enqueue_or_execution",
        },
        created_at="2026-07-03T09:05:38Z",
    )
    second = upsert_awc_hard_source_signal(
        conn,
        {
            "signal_id": "sig_1234567890abcdef",
            "signal_to_order_timing": {
                "signal_id": "sig_1234567890abcdef",
                "awc_hard_source_signal_at": "2026-07-03T09:05:38Z",
                "terminal_state": "job_enqueued",
                "terminal_reason": "awaiting_order_submit",
            },
        },
        created_at="2026-07-03T09:05:39Z",
    )

    assert first["status"] == "upserted"
    assert second["status"] == "upserted"
    row = conn.execute("SELECT * FROM awc_hard_source_signals").fetchone()
    assert row["source_event_id"] == "srcevt_1"
    assert row["slug"] == "highest-temperature-in-chengdu-on-july-3-2026"
    assert row["event_date"] == "2026-07-03"
    assert row["awc_valid_time"] == "2026-07-03T09:00:00Z"
    assert row["awc_hard_source_signal_at"] == "2026-07-03T09:05:38Z"
    assert row["acceptance_state"] == "accepted"
    assert row["terminal_state"] == "job_enqueued"
    assert row["state_reason"] == "awaiting_order_submit"


def test_awc_hard_source_signal_writer_does_not_downgrade_submitted():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _create_awc_signal_table(conn)

    submitted = upsert_awc_hard_source_signal(
        conn,
        {
            "signal_id": "sig_1234567890abcdef",
            "awc_hard_source_signal_at": "2026-07-03T09:05:38Z",
            "acceptance_state": "accepted",
            "terminal_state": "submitted",
            "state_reason": "order_submitted",
        },
        created_at="2026-07-03T09:05:40Z",
    )
    skipped = upsert_awc_hard_source_signal(
        conn,
        {
            "signal_id": "sig_1234567890abcdef",
            "terminal_state": "skipped",
            "state_reason": "hard_source_direct_enqueue_disabled",
        },
        created_at="2026-07-03T09:05:45Z",
    )

    assert submitted["status"] == "upserted"
    assert skipped["status"] == "preserved_existing_terminal"
    assert skipped["writes_archive_db"] is False
    row = conn.execute("SELECT * FROM awc_hard_source_signals").fetchone()
    assert row["terminal_state"] == "submitted"
    assert row["state_reason"] == "order_submitted"
