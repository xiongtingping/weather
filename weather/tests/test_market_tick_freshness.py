import sqlite3
import unittest

from scan_weather_market import _load_market_tick_latest_by_key


class MarketTickFreshnessTests(unittest.TestCase):
    def test_market_tokens_updated_at_fills_freshness_when_ticks_missing(self) -> None:
        conn = sqlite3.connect(":memory:")
        cur = conn.cursor()
        cur.executescript(
            """
            CREATE TABLE event_market_ticks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT,
                event_date TEXT,
                outcome TEXT,
                side TEXT,
                created_at TEXT
            );
            CREATE TABLE market_tokens (
                slug TEXT NOT NULL,
                event_date TEXT NOT NULL,
                outcome TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        cur.execute(
            """
            INSERT INTO event_market_ticks (slug, event_date, outcome, side, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "test-slug",
                "2026-04-02",
                "11°C",
                "NO",
                "2026-04-02T10:00:00+00:00",
            ),
        )
        cur.execute(
            """
            INSERT INTO market_tokens (slug, event_date, outcome, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                "test-slug",
                "2026-04-02",
                "11°C",
                "2026-04-02T10:05:00+00:00",
            ),
        )

        latest = _load_market_tick_latest_by_key(conn, "test-slug", "2026-04-02")

        self.assertIn(("11°C", "YES"), latest)
        self.assertIn(("11°C", "NO"), latest)
        self.assertEqual(latest[("11°C", "YES")].isoformat(), "2026-04-02T10:05:00+00:00")
        self.assertEqual(latest[("11°C", "NO")].isoformat(), "2026-04-02T10:05:00+00:00")


if __name__ == "__main__":
    unittest.main()
