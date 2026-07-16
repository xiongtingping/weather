from __future__ import annotations

import sqlite3

from target_realization_execution_lock_writer import (
    active_target_realization_execution_locks,
    ensure_target_realization_execution_lock_schema,
    has_active_target_realization_execution_lock,
    upsert_target_realization_execution_lock,
)


def test_execution_lock_is_durable_and_merge_fill_preserves_source_order() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_target_realization_execution_lock_schema(conn)
    upsert_target_realization_execution_lock(
        conn,
        slug="chengdu-july-11",
        event_date="2026-07-11",
        outcome="33°C",
        side="YES",
        lock_type="top_yes_profit_lock",
        state="active",
        source_order_id="sell-1",
        plan_id="plan-1",
        locked_at="2026-07-11T06:00:00+00:00",
        updated_at="2026-07-11T06:00:00+00:00",
        details={"matched_size": 10},
    )
    upsert_target_realization_execution_lock(
        conn,
        slug="chengdu-july-11",
        event_date="2026-07-11",
        outcome="33°C",
        side="YES",
        lock_type="top_yes_profit_lock",
        state="active",
        locked_at="2026-07-11T06:00:01+00:00",
        updated_at="2026-07-11T06:00:01+00:00",
    )
    conn.commit()

    rows = active_target_realization_execution_locks(
        conn,
        slug="chengdu-july-11",
        event_date="2026-07-11",
    )
    assert len(rows) == 1
    assert rows[0]["source_order_id"] == "sell-1"
    assert rows[0]["details"]["matched_size"] == 10
    assert (
        has_active_target_realization_execution_lock(
            conn,
            slug="chengdu-july-11",
            event_date="2026-07-11",
            outcome="33°C",
            side="YES",
        )
        == "top_yes_profit_lock"
    )
