import json
import sqlite3
from datetime import datetime, timezone

import scan_weather_market as swm


def test_market_refresh_weather_merge_stale_reason_only_blocks_d0() -> None:
    now = datetime(2026, 6, 13, 8, 2, tzinfo=timezone.utc)
    stale_payload = {"weather_likelihood_refreshed_at": "2026-06-13T06:44:11+00:00"}
    fresh_payload = {"weather_likelihood_refreshed_at": "2026-06-13T07:55:00+00:00"}

    stale_reason = swm._market_refresh_weather_merge_stale_reason(
        stale_payload,
        "2026-06-13",
        now=now,
        today_key="2026-06-13",
    )
    assert "weather_likelihood_stale" in stale_reason

    assert (
        swm._market_refresh_weather_merge_stale_reason(
            fresh_payload,
            "2026-06-13",
            now=now,
            today_key="2026-06-13",
        )
        == ""
    )
    assert (
        swm._market_refresh_weather_merge_stale_reason(
            stale_payload,
            "2026-06-14",
            now=now,
            today_key="2026-06-13",
        )
        == ""
    )


def test_save_event_run_from_market_refresh_rewrites_market_snapshot_provenance() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE event_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            event_date TEXT,
            slug TEXT,
            event_url TEXT,
            title TEXT,
            lat REAL,
            lon REAL,
            tz TEXT,
            payload_json TEXT,
            suggestions_json TEXT,
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
        INSERT INTO event_runs(
            run_date, event_date, slug, event_url, title, lat, lon, tz,
            payload_json, suggestions_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "2026-04-20",
            "2026-04-22",
            "slug-a",
            "https://polymarket.com/event/slug-a",
            "slug-a",
            None,
            None,
            "UTC",
            json.dumps(
                {
                    "slug": "slug-a",
                    "rows": [
                        {
                            "outcome": "35°C",
                            "q_entry_no": 0.61,
                            "best_ask_no": 0.974,
                            "entry_price_no": 0.974,
                        }
                    ],
                    "score_composer": {
                        "model_snapshot_id": 7,
                        "market_snapshot_id": 111,
                        "model_snapshot_at": "2026-04-20T00:00:00+00:00",
                        "market_snapshot_at": "2026-04-20T00:00:00+00:00",
                    },
                }
            ),
            "[]",
            "2026-04-20T00:00:00+00:00",
        ),
    )
    conn.execute(
        """
        INSERT INTO market_snapshots(run_date, event_date, slug, payload_json, meta_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            "2026-04-21",
            "2026-04-22",
            "slug-a",
            json.dumps(
                {
                    "slug": "slug-a",
                    "rows": [
                        {
                            "outcome": "35°C",
                            "best_bid_no": 0.96,
                            "best_ask_no": 0.98,
                        }
                    ],
                }
            ),
            "{}",
            "2026-04-21T05:23:28+00:00",
        ),
    )

    swm._save_event_run_from_market_refresh(
        conn,
        run_date="2026-04-21",
        event_date="2026-04-22",
        slug="slug-a",
        rows=[{"outcome": "35°C", "best_bid_no": 0.96, "best_ask_no": 0.98}],
        created_at="2026-04-21T05:23:28+00:00",
        event_url="https://polymarket.com/event/slug-a",
        title="slug-a",
    )

    latest = conn.execute(
        """
        SELECT payload_json
        FROM event_runs
        WHERE slug = 'slug-a' AND event_date = '2026-04-22'
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()

    assert latest is not None
    payload = json.loads(latest["payload_json"])
    assert payload["market_updated_at"] == "2026-04-21T05:23:28+00:00"
    assert payload["score_composer"]["model_snapshot_id"] == 7
    assert payload["score_composer"]["market_snapshot_id"] == 1
    assert payload["score_composer"]["market_snapshot_at"] == "2026-04-21T05:23:28+00:00"
    assert payload["score_composer"]["source"] == "market_refresh_event_run_merge"
    assert payload["rows"][0]["best_ask_no"] == 0.98
    assert payload["rows"][0]["entry_price_no"] == 0.98


def test_refresh_payload_rows_with_latest_market_snapshot_updates_no_entry_price() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
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
        INSERT INTO market_snapshots(run_date, event_date, slug, payload_json, meta_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            "2026-04-21",
            "2026-04-22",
            "slug-a",
            json.dumps(
                {
                    "slug": "slug-a",
                    "rows": [
                        {
                            "outcome": "62-63°F",
                            "best_bid_no": 0.96,
                            "best_ask_no": 0.98,
                        }
                    ],
                }
            ),
            "{}",
            "2026-04-21T05:23:28+00:00",
        ),
    )

    rows_out, market_updated_at = swm._refresh_payload_rows_with_latest_market_snapshot(
        conn,
        "slug-a",
        "2026-04-22",
        [
            {
                "outcome": "62-63°F",
                "q_entry_no": 0.8959788366083569,
                "best_bid_no": 0.97,
                "best_ask_no": 0.974,
                "entry_price_no": 0.974,
                "signal_edge_min_no": -0.07802116339164314,
            }
        ],
    )

    assert market_updated_at == "2026-04-21T05:23:28+00:00"
    assert rows_out[0]["best_bid_no"] == 0.96
    assert rows_out[0]["best_ask_no"] == 0.98
    assert rows_out[0]["entry_price_no"] == 0.98
    assert abs(rows_out[0]["signal_edge_min_no"] - (0.8959788366083569 - 0.98)) < 1e-9
