import sqlite3

from ui.server import _compose_fact_from_snapshots, query_latest_event_run_for_run_date


def test_compose_fact_from_snapshots_returns_none_when_snapshot_times_mismatch() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE model_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            event_date TEXT,
            slug TEXT,
            payload_json TEXT,
            meta_json TEXT,
            created_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE market_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            event_date TEXT,
            slug TEXT,
            payload_json TEXT,
            meta_json TEXT,
            created_at TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO model_snapshots(run_date, event_date, slug, payload_json, meta_json, created_at)
        VALUES ('2026-04-15', '2026-04-15', 'slug-a', '{"rows":[{"outcome":"35°C","q_round":0.55,"q_raw":0.55,"q_calibrated":0.56}]}', '{}', '2026-04-15T15:00:00+00:00')
        """
    )
    conn.execute(
        """
        INSERT INTO market_snapshots(run_date, event_date, slug, payload_json, meta_json, created_at)
        VALUES ('2026-04-14', '2026-04-15', 'slug-a', '{"rows":[{"outcome":"35°C","best_bid":0.50,"best_ask":0.51}]}', '{}', '2026-04-15T02:00:00+00:00')
        """
    )

    composed = _compose_fact_from_snapshots(
        conn,
        "2026-04-15",
        "slug-a",
        "2026-04-15",
        {},
        {},
        {},
        {},
        platt=None,
    )

    assert composed is None


def test_query_latest_event_run_for_run_date_uses_exact_run_date() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE event_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            event_date TEXT,
            slug TEXT,
            created_at TEXT,
            payload_json TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO event_runs(run_date, event_date, slug, created_at, payload_json)
        VALUES
        ('2026-04-14', '2026-04-15', 'slug-a', '2026-04-15T02:00:00+00:00', '{}'),
        ('2026-04-15', '2026-04-15', 'slug-a', '2026-04-15T15:00:00+00:00', '{}')
        """
    )

    row = query_latest_event_run_for_run_date(conn, "slug-a", "2026-04-15", "2026-04-15")

    assert row is not None
    assert row["run_date"] == "2026-04-15"
    assert row["created_at"] == "2026-04-15T15:00:00+00:00"
