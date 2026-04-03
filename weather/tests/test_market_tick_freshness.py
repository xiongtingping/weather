import sqlite3
import unittest

from scan_weather_market import (
    _dump_reason_list_or_blank,
    _effective_stop_loss_edge_confirm_enabled,
    _freshest_exit_quote_for_side,
    _load_market_tick_latest_by_key,
    _resolve_exit_submission_price,
)


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

    def test_freshest_exit_quote_prefers_latest_tick_over_row(self) -> None:
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
                best_bid REAL,
                best_ask REAL,
                bid_depth_topN REAL,
                ask_depth_topN REAL,
                created_at TEXT
            );
            """
        )
        cur.execute(
            """
            INSERT INTO event_market_ticks (
                slug, event_date, outcome, side, best_bid, best_ask, bid_depth_topN, ask_depth_topN, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "test-slug",
                "2026-04-02",
                "11°C",
                "NO",
                0.002,
                0.004,
                2000.0,
                900.0,
                "2026-04-02T10:05:00+00:00",
            ),
        )
        row = {
            "best_bid_no": 0.27,
            "best_ask_no": 0.31,
            "bid_depth_topN_no": 10.0,
            "ask_depth_topN_no": 20.0,
        }
        best_bid, best_ask, bid_depth, ask_depth = _freshest_exit_quote_for_side(
            conn,
            "test-slug",
            "2026-04-02",
            "11°C",
            "NO",
            row,
        )
        self.assertAlmostEqual(best_bid, 0.002)
        self.assertAlmostEqual(best_ask, 0.004)
        self.assertAlmostEqual(bid_depth, 2000.0)
        self.assertAlmostEqual(ask_depth, 900.0)

    def test_resolve_exit_submission_price_uses_one_sided_ask_floor(self) -> None:
        row = {
            "best_bid_no": 0.0,
            "best_ask_no": 0.001,
        }
        exit_price, used_ask, reason, best_bid, best_ask = _resolve_exit_submission_price(
            None,
            0.0,
            0.001,
            side="NO",
            row_ref=row,
            allow_ask_fallback=False,
        )
        self.assertAlmostEqual(exit_price, 0.001)
        self.assertTrue(used_ask)
        self.assertEqual(reason, "one_sided_ask_only")
        self.assertAlmostEqual(best_bid, 0.0)
        self.assertAlmostEqual(best_ask, 0.001)

    def test_resolve_exit_submission_price_can_use_complement_quote(self) -> None:
        row = {
            "best_bid": 0.999,
            "best_ask": 1.0,
        }
        exit_price, used_ask, reason, best_bid, best_ask = _resolve_exit_submission_price(
            None,
            None,
            None,
            side="NO",
            row_ref=row,
            allow_ask_fallback=False,
        )
        self.assertAlmostEqual(exit_price, 0.001)
        self.assertTrue(used_ask)
        self.assertEqual(reason, "one_sided_ask_only_complement")
        self.assertAlmostEqual(best_bid, 0.0)
        self.assertAlmostEqual(best_ask, 0.001)

    def test_effective_stop_loss_edge_confirm_disabled_for_plain_lcb(self) -> None:
        self.assertFalse(
            _effective_stop_loss_edge_confirm_enabled(
                True,
                lcb_exit_context_active=True,
                lcb_ignore_edge_confirm=True,
            )
        )

    def test_effective_stop_loss_edge_confirm_kept_for_non_lcb(self) -> None:
        self.assertTrue(
            _effective_stop_loss_edge_confirm_enabled(
                True,
                lcb_exit_context_active=False,
                lcb_ignore_edge_confirm=True,
            )
        )

    def test_dump_reason_list_or_blank_clears_empty(self) -> None:
        self.assertEqual(_dump_reason_list_or_blank([]), "")
        self.assertEqual(_dump_reason_list_or_blank(None), "")

    def test_dump_reason_list_or_blank_keeps_values(self) -> None:
        self.assertEqual(
            _dump_reason_list_or_blank(["stop_loss", "edge_min"]),
            '["stop_loss", "edge_min"]',
        )


if __name__ == "__main__":
    unittest.main()
