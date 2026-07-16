import sqlite3

from production_reported_x_canonical_event_drain_runtime import (
    _monotonic_started_at,
)


def test_replay_started_at_never_precedes_latest_checkpoint() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE posterior_checkpoints(target_identity TEXT,state TEXT,replay_generation INTEGER,committed_at TEXT)"
    )
    conn.execute(
        "INSERT INTO posterior_checkpoints VALUES(?,?,?,?)",
        ("target-1", "committed", 2, "2026-07-15T02:00:00Z"),
    )
    assert _monotonic_started_at(
        conn,
        target="target-1",
        clock_value="2026-07-15T01:59:00Z",
    ) == "2026-07-15T02:00:00.000Z"
    assert _monotonic_started_at(
        conn,
        target="target-1",
        clock_value="2026-07-15T02:01:00Z",
    ) == "2026-07-15T02:01:00.000Z"
