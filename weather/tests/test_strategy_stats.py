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
        main = stats["main"]
        yes_top = stats["yes_top"]
        hard_no = stats["hard_bound_no"]

        # main: net BUY = 0.4 + 0.1/10 = 0.41, net SELL = 0.6 - 0.1/10 = 0.59 -> pnl=0.18*10=1.8
        self.assertAlmostEqual(float(main["total_pnl"] or 0.0), 1.8, places=6)
        self.assertAlmostEqual(float(main["total_cost"] or 0.0), 4.1, places=6)
        self.assertEqual(int(main["closed_trade_count"] or 0), 1)
        self.assertEqual(int(main["win_count"] or 0), 1)
        # Filled orders are counted by explicit order.strategy; legacy strategy="exit" is tracked under system bucket "exit".
        self.assertEqual(int(main["filled_order_count"] or 0), 1)
        self.assertAlmostEqual(float(main["total_fees"] or 0.0), 0.2, places=6)

        # yes_top: settlement pnl = (1 - 0.2)*5 = 4.0
        self.assertAlmostEqual(float(yes_top["total_pnl"] or 0.0), 4.0, places=6)
        self.assertAlmostEqual(float(yes_top["total_cost"] or 0.0), 1.0, places=6)
        self.assertEqual(int(yes_top["closed_trade_count"] or 0), 1)
        self.assertEqual(int(yes_top["win_count"] or 0), 1)
        self.assertAlmostEqual(float(yes_top["total_pnl_settlement"] or 0.0), 4.0, places=6)

        # hard_bound_no: settlement pnl = (0 - 0.7)*4 = -2.8
        self.assertAlmostEqual(float(hard_no["total_pnl"] or 0.0), -2.8, places=6)
        self.assertAlmostEqual(float(hard_no["total_cost"] or 0.0), 2.8, places=6)
        self.assertEqual(int(hard_no["closed_trade_count"] or 0), 1)
        self.assertEqual(int(hard_no["loss_count"] or 0), 1)

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
        self.assertNotIn("unknown", stats)
        self.assertEqual(int(stats["main"]["buy_fill_count"] or 0), 0)


if __name__ == "__main__":
    unittest.main()
