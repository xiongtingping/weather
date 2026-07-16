import sqlite3
from pathlib import Path

from scripts import fact_materialization_temp_drill as drill


def _create_source_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    drill.server.ensure_fact_tables(conn)
    conn.executescript(
        """
        CREATE TABLE model_snapshots (
            id INTEGER PRIMARY KEY,
            run_date TEXT,
            event_date TEXT,
            slug TEXT,
            payload_json TEXT,
            meta_json TEXT,
            created_at TEXT
        );
        CREATE TABLE market_snapshots (
            id INTEGER PRIMARY KEY,
            run_date TEXT,
            event_date TEXT,
            slug TEXT,
            payload_json TEXT,
            meta_json TEXT,
            created_at TEXT
        );
        CREATE TABLE event_runs (
            id INTEGER PRIMARY KEY,
            run_date TEXT,
            event_date TEXT,
            slug TEXT,
            payload_json TEXT,
            created_at TEXT
        );
        CREATE TABLE weather_market_discovery (
            slug TEXT,
            event_date TEXT,
            title TEXT,
            market_open_at TEXT,
            first_seen_at TEXT,
            first_tokenized_at TEXT,
            token_count INTEGER,
            status TEXT,
            details_json TEXT,
            updated_at TEXT
        );
        CREATE TABLE market_tokens (
            slug TEXT,
            event_date TEXT,
            outcome TEXT,
            yes_token TEXT,
            no_token TEXT,
            outcome_volume REAL,
            liquidity REAL,
            market_active INTEGER,
            market_closed INTEGER,
            market_archived INTEGER,
            market_accepting_orders INTEGER,
            market_enable_orderbook INTEGER,
            updated_at TEXT
        );
        """
    )
    slug = "highest-temperature-in-chengdu-on-july-6-2026"
    conn.execute(
        """
        INSERT INTO weather_market_discovery(
            slug, event_date, title, market_open_at, first_seen_at, first_tokenized_at,
            token_count, status, details_json, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            slug,
            "2026-07-06",
            "Highest temperature in Chengdu on July 6?",
            "2026-07-05T01:10:00+00:00",
            "2026-07-05T01:11:00+00:00",
            "2026-07-05T01:12:00+00:00",
            2,
            "tokenized",
            "{}",
            "2026-07-05T01:16:49+00:00",
        ),
    )
    for outcome in ("30°C", "31°C"):
        conn.execute(
            """
            INSERT INTO market_tokens(
                slug, event_date, outcome, yes_token, no_token, outcome_volume, liquidity,
                market_active, market_closed, market_archived, market_accepting_orders,
                market_enable_orderbook, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                slug,
                "2026-07-06",
                outcome,
                f"yes-{outcome}",
                f"no-{outcome}",
                10.0,
                20.0,
                1,
                0,
                0,
                1,
                1,
                "2026-07-05T01:16:50+00:00",
            ),
        )
    conn.commit()
    conn.close()


def test_fact_materialization_temp_drill_uses_formal_writer_without_production_write(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_db = tmp_path / "archive.db"
    _create_source_db(source_db)
    old_db_path = drill.server.DB_PATH
    monkeypatch.setattr(drill.server, "_today_et_key", lambda: "2026-07-05")

    result = drill.run_drill(
        db_path=source_db,
        start_date="2026-07-06",
        end_date="2026-07-06",
        max_items_output=5,
    )

    assert result["ok"] is True
    assert result["read_only_production"] is True
    assert result["guards"]["writes_production_archive_db"] is False
    assert result["guards"]["writes_temp_archive_db"] is True
    assert result["created_count"] == 1
    assert result["temp_db_retained"] is False
    assert drill.server.DB_PATH == old_db_path
    row = result["latest_rows"][0]
    assert row["resolver_source"] == "weather_market_discovery"
    assert row["formal_fact_chain"] is True
    assert row["posterior_ssot_schema"] == "bayes_posterior_ssot_fact_placeholder_v1"
    assert row["posterior_ssot_state"] == "not_materialized"
    assert row["main_entry_ok"] is False

    with sqlite3.connect(source_db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM event_fact_history").fetchone()[0]
    assert count == 0
