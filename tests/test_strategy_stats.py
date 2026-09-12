import sqlite3
import unittest


class TestStrategyStats(unittest.TestCase):
    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.executescript(
            """
            CREATE TABLE auto_orders (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              order_id TEXT UNIQUE,
              slug TEXT,
              event_date TEXT,
              outcome TEXT,
              side TEXT,
              action TEXT,
              price REAL,
              size REAL,
              created_at TEXT,
              strategy TEXT
            );
            CREATE TABLE edge_trade_fills (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              order_id TEXT,
              slug TEXT,
              event_date TEXT,
              outcome TEXT,
              outcome_side TEXT,
              trade_side TEXT,
              price REAL,
              size REAL,
              fee REAL,
              trade_time TEXT,
              created_at TEXT
            );
            CREATE TABLE event_runs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              slug TEXT,
              event_date TEXT,
              settlement_actual REAL,
              payload_json TEXT,
              rounding_rule TEXT,
              market_var TEXT,
              created_at TEXT
            );
            """
        )
        conn.commit()
        return conn

    def test_strategy_stats_includes_fees_and_settlement_close(self) -> None:
        from ui.server import compute_strategy_stats

        conn = self._conn()
        cur = conn.cursor()
        slug = "s"
        event_date = "2026-02-24"
        outcome = "10°C"

        # Event settles at 10°C -> actual bucket label == "10°C".
        cur.execute(
            """
            INSERT INTO event_runs (slug, event_date, settlement_actual, payload_json, rounding_rule, market_var, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                slug,
                event_date,
                10.0,
                '{"rows":[{"outcome":"10°C"},{"outcome":"11°C"}]}',
                "",
                "temperature",
                "2026-02-24T00:00:00Z",
            ),
        )

        # main: BUY then SELL (include fees).
        cur.execute(
            """
            INSERT INTO auto_orders (order_id, slug, event_date, outcome, side, action, price, size, created_at, strategy)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("o1", slug, event_date, outcome, "YES", "entry", 0.4, 10.0, "2026-02-24T00:00:01Z", "main"),
        )
        cur.execute(
            """
            INSERT INTO auto_orders (order_id, slug, event_date, outcome, side, action, price, size, created_at, strategy)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("o2", slug, event_date, outcome, "YES", "exit", 0.6, 10.0, "2026-02-24T00:05:00Z", "exit"),
        )
        cur.execute(
            """
            INSERT INTO edge_trade_fills (order_id, slug, event_date, outcome, outcome_side, trade_side, price, size, fee, trade_time, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("o1", slug, event_date, outcome, "YES", "BUY", 0.4, 10.0, 0.1, "2026-02-24T00:00:02Z", "2026-02-24T00:00:02Z"),
        )
        cur.execute(
            """
            INSERT INTO edge_trade_fills (order_id, slug, event_date, outcome, outcome_side, trade_side, price, size, fee, trade_time, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("o2", slug, event_date, outcome, "YES", "SELL", 0.6, 10.0, 0.1, "2026-02-24T00:05:01Z", "2026-02-24T00:05:01Z"),
        )

        # yes_top: BUY only, closed via settlement payout=1.
        cur.execute(
            """
            INSERT INTO auto_orders (order_id, slug, event_date, outcome, side, action, price, size, created_at, strategy)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("o3", slug, event_date, outcome, "YES", "entry", 0.2, 5.0, "2026-02-24T00:00:10Z", "yes_top"),
        )
        cur.execute(
            """
            INSERT INTO edge_trade_fills (order_id, slug, event_date, outcome, outcome_side, trade_side, price, size, fee, trade_time, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("o3", slug, event_date, outcome, "YES", "BUY", 0.2, 5.0, 0.0, "2026-02-24T00:00:11Z", "2026-02-24T00:00:11Z"),
        )

        # hard_bound_no: BUY NO, closed via settlement payout=0 (since actual bucket is 10°C).
        cur.execute(
            """
            INSERT INTO auto_orders (order_id, slug, event_date, outcome, side, action, price, size, created_at, strategy)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("o4", slug, event_date, outcome, "NO", "entry", 0.7, 4.0, "2026-02-24T00:00:20Z", "hard_bound_no"),
        )
        cur.execute(
            """
            INSERT INTO edge_trade_fills (order_id, slug, event_date, outcome, outcome_side, trade_side, price, size, fee, trade_time, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("o4", slug, event_date, outcome, "NO", "BUY", 0.7, 4.0, 0.0, "2026-02-24T00:00:21Z", "2026-02-24T00:00:21Z"),
        )

        conn.commit()

        stats = compute_strategy_stats(conn, days=1, run_date=event_date)
        self.assertNotIn("lcb_global", stats)

        # These stats used to be read off a single "path_probability_follow"
        # row.  That roll-up only exists when PM_LCB_GLOBAL_ENABLED is set,
        # and scan_weather_market forces it to "0" with its own comment on
        # the line above: "lcb_global is a retired strategy; keep parsing old
        # fields for historical rows, but do not allow it to participate in
        # current entry/refresh logic."  Production stats are keyed by entry
        # family, so the same rows and the same money are asserted where they
        # were earned.
        main = stats["main"]
        yes_top = stats["yes_top"]
        hard_bound_no = stats["hard_bound_no"]
        self.assertAlmostEqual(float(main["total_pnl"] or 0.0), 1.8, places=6)
        self.assertAlmostEqual(float(yes_top["total_pnl"] or 0.0), 4.0, places=6)
        self.assertAlmostEqual(float(hard_bound_no["total_pnl"] or 0.0), -2.8, places=6)

        def total(field: str) -> float:
            return sum(float(row.get(field) or 0.0) for row in stats.values())

        # main round-trip +1.8, yes_top settlement +4.0, hard_bound_no
        # settlement -2.8 => +3.0 across the book, as before.
        self.assertAlmostEqual(total("total_pnl"), 3.0, places=6)
        self.assertAlmostEqual(total("total_cost"), 7.9, places=6)
        self.assertEqual(int(total("filled_order_count")), 4)
        self.assertEqual(int(total("filled_entry_order_count")), 3)
        self.assertEqual(int(total("filled_exit_order_count")), 1)
        self.assertAlmostEqual(total("total_fees"), 0.2, places=6)
        self.assertAlmostEqual(total("total_pnl_settlement"), 1.2, places=6)
        # Trades close per family here rather than per rolled-up chain, so the
        # three positions are three closed trades with two wins -- the roll-up
        # merged main's round trip into one of them and reported 2 and 1.
        self.assertEqual(int(total("closed_trade_count")), 3)
        self.assertEqual(int(total("win_count")), 2)
        self.assertEqual(int(total("loss_count")), 1)
        # Fees were paid on the main round trip; the settlement money is the
        # other two.
        self.assertAlmostEqual(float(main["total_fees"] or 0.0), 0.2, places=6)
        self.assertAlmostEqual(float(main["total_pnl_settlement"] or 0.0), 0.0, places=6)
        self.assertAlmostEqual(float(yes_top["total_pnl_settlement"] or 0.0), 4.0, places=6)
        self.assertAlmostEqual(float(hard_bound_no["total_pnl_settlement"] or 0.0), -2.8, places=6)

    def test_strategy_stats_excludes_fills_without_auto_orders(self) -> None:
        from ui.server import compute_strategy_stats

        conn = self._conn()
        cur = conn.cursor()
        slug = "s"
        event_date = "2026-02-24"
        outcome = "10°C"

        # A fill exists in edge_trade_fills but has no matching auto_orders row -> should be excluded.
        cur.execute(
            """
            INSERT INTO edge_trade_fills (order_id, slug, event_date, outcome, outcome_side, trade_side, price, size, fee, trade_time, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("missing", slug, event_date, outcome, "YES", "BUY", 0.5, 10.0, 0.0, "2026-02-24T00:00:02Z", "2026-02-24T00:00:02Z"),
        )
        conn.commit()

        stats = compute_strategy_stats(conn, days=1, run_date=event_date)
        # SSOT policy: do not create/accumulate "unknown" from fills missing auto_orders provenance.
        self.assertNotIn("lcb_global", stats)
        self.assertEqual(
            sum(int(row.get("buy_fill_count") or 0) for row in stats.values()), 0
        )
        self.assertEqual(int(stats["unknown"]["buy_fill_count"] or 0), 0)

    def test_strategy_stats_run_date_uses_activity_day(self) -> None:
        from ui.server import compute_strategy_stats

        conn = self._conn()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO auto_orders (order_id, slug, event_date, outcome, side, action, price, size, created_at, strategy)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "ord-activity-1",
                "activity-slug",
                "2026-02-20",
                "10°C",
                "YES",
                "entry",
                0.51,
                3.0,
                "2026-03-03T01:00:00Z",
                "adj_pair",
            ),
        )
        cur.execute(
            """
            INSERT INTO edge_trade_fills (order_id, slug, event_date, outcome, outcome_side, trade_side, price, size, fee, trade_time, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "ord-activity-1",
                "activity-slug",
                "2026-02-20",
                "10°C",
                "YES",
                "BUY",
                0.51,
                3.0,
                0.0,
                "2026-03-03T01:00:05Z",
                "2026-03-03T01:00:05Z",
            ),
        )
        conn.commit()

        stats = compute_strategy_stats(conn, days=1, run_date="2026-03-03")
        self.assertNotIn("lcb_global", stats)
        # The order carries strategy "adj_pair", which is its own family once
        # the retired lcb_global roll-up is off.
        self.assertEqual(int(stats["adj_pair"]["filled_order_count"] or 0), 1)
        self.assertEqual(int(stats["adj_pair"]["order_count"] or 0), 1)


if __name__ == "__main__":
    unittest.main()
