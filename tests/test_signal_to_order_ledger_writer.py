import json
import sqlite3

from signal_to_order_ledger_writer import (
    SIGNAL_TO_ORDER_TIMING_COLUMNS,
    build_signal_to_order_timing_row,
    signal_to_order_timing_schema_status,
    upsert_signal_to_order_timing,
)


def _create_signal_to_order_table(conn: sqlite3.Connection) -> None:
    columns = []
    for column in SIGNAL_TO_ORDER_TIMING_COLUMNS:
        suffix = " PRIMARY KEY" if column == "signal_id" else ""
        columns.append(f"{column} TEXT{suffix}")
    conn.execute(f"CREATE TABLE signal_to_order_timing ({', '.join(columns)})")


def test_signal_to_order_writer_noops_when_schema_missing():
    conn = sqlite3.connect(":memory:")

    result = upsert_signal_to_order_timing(
        conn,
        {
            "signal_id": "sig_1234567890abcdef",
            "signal_to_order_timing": {
                "signal_id": "sig_1234567890abcdef",
                "awc_hard_source_signal_at": "2026-07-03T09:05:38Z",
            },
        },
        created_at="2026-07-03T09:05:38Z",
    )

    assert result["ok"] is True
    assert result["status"] == "schema_missing_noop"
    assert result["writes_archive_db"] is False
    assert result["schema_status"]["exists"] is False


def test_signal_to_order_writer_upserts_and_preserves_existing_non_empty_fields():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _create_signal_to_order_table(conn)

    assert signal_to_order_timing_schema_status(conn)["ok"] is True
    first = upsert_signal_to_order_timing(
        conn,
        {
            "signal_id": "sig_1234567890abcdef",
            "decision_id": "1",
            "awc_hard_source_signal_at": "2026-07-03T09:05:38Z",
            "signal_to_order_timing": {
                "signal_id": "sig_1234567890abcdef",
                "awc_hard_source_signal_at": "2026-07-03T09:05:38Z",
                "decision_written_at": "2026-07-03T09:05:38Z",
                "terminal_state": "decision_written",
            },
        },
        created_at="2026-07-03T09:05:38Z",
    )
    second = upsert_signal_to_order_timing(
        conn,
        {
            "signal_id": "sig_1234567890abcdef",
            "order_submitted_at": "2026-07-03T09:05:40Z",
            "signal_to_order_slo_violation_reason": "",
            "signal_to_order_timing": {
                "signal_id": "sig_1234567890abcdef",
                "awc_hard_source_signal_at": "2026-07-03T09:05:38Z",
                "order_submitted_at": "2026-07-03T09:05:40Z",
                "awc_signal_to_order_submit_ms": 2000.0,
                "terminal_state": "submitted",
            },
        },
        created_at="2026-07-03T09:05:40Z",
    )

    assert first["status"] == "upserted"
    assert second["status"] == "upserted"
    row = conn.execute("SELECT * FROM signal_to_order_timing").fetchone()
    assert row["decision_id"] == "1"
    assert row["awc_hard_source_signal_at"] == "2026-07-03T09:05:38Z"
    assert row["order_submitted_at"] == "2026-07-03T09:05:40Z"
    timing = json.loads(row["timing_json"])
    assert timing["terminal_state"] == "submitted"
    assert timing["decision_written_at"] == "2026-07-03T09:05:38Z"
    assert timing["order_submitted_at"] == "2026-07-03T09:05:40Z"


def test_signal_to_order_writer_does_not_downgrade_submitted_terminal():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _create_signal_to_order_table(conn)

    submitted = upsert_signal_to_order_timing(
        conn,
        {
            "signal_id": "sig_1234567890abcdef",
            "decision_id": "42",
            "awc_hard_source_signal_at": "2026-07-03T09:05:38Z",
            "order_submitted_at": "2026-07-03T09:05:40Z",
            "signal_to_order_timing": {
                "signal_id": "sig_1234567890abcdef",
                "awc_hard_source_signal_at": "2026-07-03T09:05:38Z",
                "order_submitted_at": "2026-07-03T09:05:40Z",
                "terminal_state": "submitted",
            },
        },
        created_at="2026-07-03T09:05:40Z",
    )
    skipped = upsert_signal_to_order_timing(
        conn,
        {
            "signal_id": "sig_1234567890abcdef",
            "signal_to_order_slo_violation_reason": "hard_source_direct_recent_unchanged_decision",
            "signal_to_order_timing": {
                "signal_id": "sig_1234567890abcdef",
                "awc_hard_source_signal_at": "2026-07-03T09:05:38Z",
                "terminal_state": "no_action",
                "terminal_reason": "hard_source_direct_recent_unchanged_decision",
                "signal_to_order_slo_violation_reason": "hard_source_direct_recent_unchanged_decision",
            },
        },
        created_at="2026-07-03T09:05:45Z",
    )

    assert submitted["status"] == "upserted"
    assert skipped["status"] == "preserved_existing_terminal"
    assert skipped["writes_archive_db"] is False
    row = conn.execute("SELECT * FROM signal_to_order_timing").fetchone()
    timing = json.loads(row["timing_json"])
    assert timing["terminal_state"] == "submitted"
    assert row["order_submitted_at"] == "2026-07-03T09:05:40Z"


def test_signal_to_order_writer_preserves_first_exchange_post_t5():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _create_signal_to_order_table(conn)

    for t5 in ("2026-07-03T09:05:40Z", "2026-07-03T09:05:41Z"):
        result = upsert_signal_to_order_timing(
            conn,
            {
                "signal_id": "sig_immutable_t5",
                "order_submitted_at": t5,
                "signal_to_order_timing": {
                    "signal_id": "sig_immutable_t5",
                    "terminal_state": "submitted",
                    "order_submitted_at": t5,
                },
            },
        )
        assert result["ok"] is True

    row = conn.execute(
        "SELECT order_submitted_at, timing_json FROM signal_to_order_timing WHERE signal_id = ?",
        ("sig_immutable_t5",),
    ).fetchone()
    assert row["order_submitted_at"] == "2026-07-03T09:05:40Z"
    assert json.loads(row["timing_json"])["order_submitted_at"] == "2026-07-03T09:05:40Z"


def test_signal_to_order_row_maps_known_timing_aliases():
    row = build_signal_to_order_timing_row(
        {
            "signal_id": "sig_1234567890abcdef",
            "signal_to_order_timing": {
                "signal_id": "sig_1234567890abcdef",
                "awc_hard_source_signal_at": "2026-07-03T09:05:38Z",
                "fact_written_at": "2026-07-03T09:05:38.100Z",
                "decision_written_at": "2026-07-03T09:05:38.200Z",
                "job_enqueued_at": "2026-07-03T09:05:38.300Z",
            },
        },
        created_at="2026-07-03T09:05:38Z",
    )

    assert row[0] == "sig_1234567890abcdef"
    assert row[3] == "2026-07-03T09:05:38Z"
    assert row[4] == "2026-07-03T09:05:38.100Z"
    assert row[6] == "2026-07-03T09:05:38.200Z"
    assert row[8] == "2026-07-03T09:05:38.300Z"
