import sqlite3
import tempfile
import time
import unittest

from scan_weather_market import (
    _fill_rows_with_latest_ticks,
    _load_market_tick_latest_by_key,
    _load_market_tick_latest_signature_by_key,
    _tick_signature_changed,
)
from datetime import datetime, timezone


class MarketTickFreshnessTest(unittest.TestCase):
    def test_market_token_update_does_not_count_as_tick_freshness(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute(
                """
                CREATE TABLE event_market_ticks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    slug TEXT,
                    event_date TEXT,
                    outcome TEXT,
                    side TEXT,
                    created_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE market_tokens (
                    slug TEXT,
                    event_date TEXT,
                    outcome TEXT,
                    updated_at TEXT
                )
                """
            )
            conn.execute(
                """
                INSERT INTO market_tokens(slug, event_date, outcome, updated_at)
                VALUES ('s', '2026-05-06', '82-83°F', '2026-05-05T08:30:00+00:00')
                """
            )

            latest = _load_market_tick_latest_by_key(conn, "s", "2026-05-06")

            self.assertNotIn(("82-83°F", "YES"), latest)
            self.assertNotIn(("82-83°F", "NO"), latest)
        finally:
            conn.close()

    def test_event_market_tick_sets_tick_freshness(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute(
                """
                CREATE TABLE event_market_ticks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    slug TEXT,
                    event_date TEXT,
                    outcome TEXT,
                    side TEXT,
                    created_at TEXT
                )
                """
            )
            conn.execute(
                """
                INSERT INTO event_market_ticks(slug, event_date, outcome, side, created_at)
                VALUES ('s', '2026-05-06', '82-83°F', 'YES', '2026-05-05T03:44:29+00:00')
                """
            )

            latest = _load_market_tick_latest_by_key(conn, "s", "2026-05-06")

            self.assertIn(("82-83°F", "YES"), latest)
            self.assertNotIn(("82-83°F", "NO"), latest)
            self.assertEqual(
                latest[("82-83°F", "YES")].isoformat(),
                "2026-05-05T03:44:29+00:00",
            )
        finally:
            conn.close()

    def test_latest_tick_overrides_gamma_price_fallback(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute(
                """
                CREATE TABLE event_market_ticks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    slug TEXT,
                    event_date TEXT,
                    outcome TEXT,
                    side TEXT,
                    best_bid REAL,
                    best_ask REAL,
                    bid_depth_topN REAL,
                    ask_depth_topN REAL,
                    created_at TEXT
                )
                """
            )
            conn.execute(
                """
                INSERT INTO event_market_ticks(
                    slug, event_date, outcome, side,
                    best_bid, best_ask, bid_depth_topN, ask_depth_topN, created_at
                ) VALUES (
                    's', '2026-05-06', '82-83°F', 'YES',
                    0.002, 0.009, 12.0, 34.0, '2026-05-05T09:13:17+00:00'
                )
                """
            )
            rows = [
                {
                    "outcome": "82-83°F",
                    "best_bid": 0.0175,
                    "best_ask": 0.0175,
                    "spread": 0.0,
                    "bid_depth_topN": 0.0,
                    "ask_depth_topN": 0.0,
                    "ask_missing_reason_yes": "gamma_price_fallback",
                }
            ]

            _fill_rows_with_latest_ticks(rows, conn, "s", "2026-05-06")

            self.assertEqual(rows[0]["best_bid"], 0.002)
            self.assertEqual(rows[0]["best_ask"], 0.009)
            self.assertAlmostEqual(rows[0]["spread"], 0.007)
            self.assertEqual(rows[0]["bid_depth_topN"], 12.0)
            self.assertEqual(rows[0]["ask_depth_topN"], 34.0)
            self.assertIsNone(rows[0]["ask_missing_reason_yes"])
            self.assertTrue(rows[0]["market_tick_override_yes"])
        finally:
            conn.close()

    def test_latest_tick_overrides_non_fallback_price_by_default(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute(
                """
                CREATE TABLE event_market_ticks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    slug TEXT,
                    event_date TEXT,
                    outcome TEXT,
                    side TEXT,
                    best_bid REAL,
                    best_ask REAL,
                    bid_depth_topN REAL,
                    ask_depth_topN REAL,
                    created_at TEXT
                )
                """
            )
            conn.execute(
                """
                INSERT INTO event_market_ticks(
                    slug, event_date, outcome, side,
                    best_bid, best_ask, bid_depth_topN, ask_depth_topN, created_at
                ) VALUES (
                    's', '2026-05-06', '82-83°F', 'YES',
                    0.002, 0.009, 12.0, 34.0, '2026-05-05T09:13:17+00:00'
                )
                """
            )
            rows = [
                {
                    "outcome": "82-83°F",
                    "best_bid": 0.0175,
                    "best_ask": 0.0175,
                    "spread": 0.0,
                    "ask_missing_reason_yes": "",
                }
            ]

            _fill_rows_with_latest_ticks(rows, conn, "s", "2026-05-06")

            self.assertEqual(rows[0]["best_bid"], 0.002)
            self.assertEqual(rows[0]["best_ask"], 0.009)
            self.assertAlmostEqual(rows[0]["spread"], 0.007)
            self.assertTrue(rows[0]["market_tick_override_yes"])
            self.assertTrue(rows[0]["market_tick_quote_overrode_existing_yes"])
        finally:
            conn.close()

    def test_latest_tick_can_preserve_existing_price_when_override_disabled(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute(
                """
                CREATE TABLE event_market_ticks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    slug TEXT,
                    event_date TEXT,
                    outcome TEXT,
                    side TEXT,
                    best_bid REAL,
                    best_ask REAL,
                    bid_depth_topN REAL,
                    ask_depth_topN REAL,
                    created_at TEXT
                )
                """
            )
            conn.execute(
                """
                INSERT INTO event_market_ticks(
                    slug, event_date, outcome, side,
                    best_bid, best_ask, bid_depth_topN, ask_depth_topN, created_at
                ) VALUES (
                    's', '2026-05-06', '82-83°F', 'YES',
                    0.002, 0.009, 12.0, 34.0, '2026-05-05T09:13:17+00:00'
                )
                """
            )
            rows = [
                {
                    "outcome": "82-83°F",
                    "best_bid": 0.0175,
                    "best_ask": 0.0175,
                    "spread": 0.0,
                    "ask_missing_reason_yes": "",
                }
            ]

            _fill_rows_with_latest_ticks(
                rows,
                conn,
                "s",
                "2026-05-06",
                override_existing=False,
            )

            self.assertEqual(rows[0]["best_bid"], 0.0175)
            self.assertEqual(rows[0]["best_ask"], 0.0175)
            self.assertEqual(rows[0]["bid_depth_topN"], 12.0)
            self.assertEqual(rows[0]["ask_depth_topN"], 34.0)
            self.assertTrue(rows[0]["market_tick_override_yes"])
            self.assertNotIn("market_tick_quote_overrode_existing_yes", rows[0])
        finally:
            conn.close()

    def test_tick_signature_detects_unchanged(self):
        self.assertFalse(
            _tick_signature_changed(
                (0.1, 0.2, 0.01, 1.0, 2.0),
                (0.10000001, 0.19999999, 0.01000001, 1.00000001, 2.00000002),
                tolerance=1e-3,
            )
        )
        self.assertTrue(
            _tick_signature_changed(
                (0.1, 0.2, 0.01, 1.0, 2.0),
                (0.11, 0.2, 0.01, 1.0, 2.0),
                tolerance=1e-3,
            )
        )

    def test_load_latest_market_tick_signature_by_key(self):
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute(
                """
                CREATE TABLE event_market_ticks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_date TEXT,
                    event_date TEXT,
                    slug TEXT,
                    outcome TEXT,
                    side TEXT,
                    best_bid REAL,
                    best_ask REAL,
                    mid REAL,
                    spread REAL,
                    bid_depth_topN REAL,
                    ask_depth_topN REAL,
                    source TEXT,
                    source_detail TEXT,
                    created_at TEXT
                )
                """
            )
            conn.execute(
                """
                INSERT INTO event_market_ticks (
                    run_date, event_date, slug, outcome, side,
                    best_bid, best_ask, mid, spread, bid_depth_topN, ask_depth_topN,
                    source, source_detail, created_at
                ) VALUES (
                    '2026-05-30', '2026-05-30', 's', '80-81°F', 'YES',
                    0.1, 0.2, 0.15, 0.01, 1.0, 2.0,
                    'clob_rest_refresh', 'market_refresh', '2026-05-30T12:00:01+00:00'
                )
                """
            )
            conn.execute(
                """
                INSERT INTO event_market_ticks (
                    run_date, event_date, slug, outcome, side,
                    best_bid, best_ask, mid, spread, bid_depth_topN, ask_depth_topN,
                    source, source_detail, created_at
                ) VALUES (
                    '2026-05-30', '2026-05-30', 's', '80-81°F', 'YES',
                    0.11, 0.21, 0.16, 0.011, 1.1, 2.1,
                    'clob_rest_refresh', 'market_refresh', '2026-05-30T12:00:03+00:00'
                )
                """
            )
            sigs = _load_market_tick_latest_signature_by_key(
                conn,
                "s",
                "2026-05-30",
                source="clob_rest_refresh",
                source_detail="market_refresh",
            )
            self.assertEqual(len(sigs), 1)
            values = sigs[("80-81°F", "YES")]
            self.assertEqual(values[0], 0.11)
            self.assertEqual(values[1], 0.21)
            self.assertEqual(values[2], 0.011)
            self.assertEqual(values[3], 1.1)
            self.assertEqual(values[4], 2.1)
            self.assertIsInstance(values[5], datetime)
            self.assertEqual(values[5], datetime(2026, 5, 30, 12, 0, 3, tzinfo=timezone.utc))
        finally:
            conn.close()

    def test_load_latest_market_tick_signature_returns_fast_when_db_locked(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = f"{tmpdir}/ticks.db"
            setup = sqlite3.connect(db_path)
            setup.execute(
                """
                CREATE TABLE event_market_ticks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_date TEXT,
                    event_date TEXT,
                    slug TEXT,
                    outcome TEXT,
                    side TEXT,
                    best_bid REAL,
                    best_ask REAL,
                    mid REAL,
                    spread REAL,
                    bid_depth_topN REAL,
                    ask_depth_topN REAL,
                    source TEXT,
                    source_detail TEXT,
                    created_at TEXT
                )
                """
            )
            setup.commit()
            setup.close()

            writer = sqlite3.connect(db_path, timeout=5.0)
            reader = sqlite3.connect(db_path, timeout=5.0)
            try:
                writer.execute("BEGIN EXCLUSIVE")
                writer.execute(
                    """
                    INSERT INTO event_market_ticks(
                        run_date, event_date, slug, outcome, side,
                        source, source_detail, created_at
                    ) VALUES (
                        '2026-05-30', '2026-05-30', 's', '80-81°F', 'YES',
                        'clob_rest_refresh', 'market_refresh', '2026-05-30T12:00:03+00:00'
                    )
                    """
                )
                started = time.perf_counter()
                sigs = _load_market_tick_latest_signature_by_key(
                    reader,
                    "s",
                    "2026-05-30",
                    source="clob_rest_refresh",
                    source_detail="market_refresh",
                )
                elapsed = time.perf_counter() - started
                self.assertEqual(sigs, {})
                self.assertLess(elapsed, 1.0)
            finally:
                writer.rollback()
                writer.close()
                reader.close()


if __name__ == "__main__":
    unittest.main()
