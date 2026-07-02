import sqlite3
import unittest


class TestPositionsTruthScope(unittest.TestCase):
    def _patch_server_flags(self, srv):
        original = {
            "POSITIONS_SYNC_ENABLED": srv.POSITIONS_SYNC_ENABLED,
            "POSITIONS_API_URL": srv.POSITIONS_API_URL,
            "ACCOUNT_REQUESTS_DISABLED": srv.ACCOUNT_REQUESTS_DISABLED,
            "POSITIONS_CACHE": dict(srv.POSITIONS_CACHE),
            "_fetch_json": srv._fetch_json,
            "_positions_api_url": srv._positions_api_url,
            "fetch_positions_cache": srv.fetch_positions_cache,
        }
        return original

    def _restore_server_flags(self, srv, original):
        srv.POSITIONS_SYNC_ENABLED = original["POSITIONS_SYNC_ENABLED"]
        srv.POSITIONS_API_URL = original["POSITIONS_API_URL"]
        srv.ACCOUNT_REQUESTS_DISABLED = original["ACCOUNT_REQUESTS_DISABLED"]
        srv.POSITIONS_CACHE.clear()
        srv.POSITIONS_CACHE.update(original["POSITIONS_CACHE"])
        srv._fetch_json = original["_fetch_json"]
        srv._positions_api_url = original["_positions_api_url"]
        srv.fetch_positions_cache = original["fetch_positions_cache"]

    def test_fetch_positions_cache_maps_asset_and_metadata(self) -> None:
        from ui import server as srv

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        srv.ensure_fact_tables(conn)
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO event_fact_history (
              slug, event_date, run_date, fact_as_of, composer_version, fact_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "miami-temp-2026-03-02",
                "2026-03-02",
                "2026-03-02",
                "2026-03-02T00:00:00Z",
                "test",
                '{"distribution":[{"outcome":"30°C","yes_token":"tok_asset","no_token":"tok_no"}]}',
                "2026-03-02T00:00:00Z",
            ),
        )
        fact_id = int(cur.lastrowid)
        cur.execute(
            """
            INSERT INTO event_fact_latest (slug, event_date, fact_id, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            ("miami-temp-2026-03-02", "2026-03-02", fact_id, "2026-03-02T00:00:00Z"),
        )
        conn.commit()

        original = self._patch_server_flags(srv)
        try:
            srv.POSITIONS_SYNC_ENABLED = True
            srv.POSITIONS_API_URL = "https://example.test/positions"
            srv.ACCOUNT_REQUESTS_DISABLED = False
            srv.POSITIONS_CACHE.clear()
            srv.POSITIONS_CACHE.update({"ts": 0.0, "updated_at": "", "error": "", "positions": {}})
            srv._positions_api_url = lambda: "https://example.test/positions"
            srv._fetch_json = lambda _url, headers=None, timeout=20: {
                "positions": [
                    {
                        "asset": "tok_asset",
                        "size": "2.5",
                        "avgPrice": "0.44",
                        "redeemable": True,
                        "endDate": "2026-03-02T12:00:00Z",
                        "eventSlug": "miami-temp-2026-03-02",
                    }
                ]
            }
            mapped, err, _updated_at = srv.fetch_positions_cache(conn, force=True)
        finally:
            self._restore_server_flags(srv, original)

        self.assertEqual(err, "")
        key = ("miami-temp-2026-03-02", "2026-03-02", "30°C", "YES")
        self.assertIn(key, mapped)
        row = mapped[key]
        self.assertAlmostEqual(float(row["position_size"] or 0.0), 2.5, places=6)
        self.assertAlmostEqual(float(row["avg_price"] or 0.0), 0.44, places=6)
        self.assertEqual(bool(row.get("redeemable")), True)
        self.assertEqual(str(row.get("end_date") or ""), "2026-03-02")
        self.assertEqual(str(row.get("event_slug") or ""), "miami-temp-2026-03-02")

    def test_daily_stats_dual_scope_uses_positions_truth(self) -> None:
        from ui import server as srv

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.executescript(
            """
            CREATE TABLE auto_orders (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              order_id TEXT,
              slug TEXT,
              event_date TEXT,
              outcome TEXT,
              side TEXT,
              strategy TEXT,
              action TEXT,
              created_at TEXT
            );
            CREATE TABLE edge_trade_fills (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              trade_id TEXT,
              order_id TEXT,
              slug TEXT,
              event_date TEXT,
              outcome TEXT,
              outcome_side TEXT,
              trade_side TEXT,
              price REAL,
              size REAL,
              trade_time TEXT,
              created_at TEXT
            );
            CREATE TABLE edge_trades (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              slug TEXT,
              event_date TEXT,
              outcome TEXT,
              side TEXT,
              state TEXT,
              position_size REAL,
              avg_price REAL,
              entry_price REAL,
              mark_price REAL,
              entry_at TEXT,
              updated_at TEXT,
              last_order_id TEXT,
              last_status TEXT,
              last_action TEXT
            );
            """
        )
        rows = [
            ("o1", "evt-trading", "2099-01-01", "30°C", "YES", "main", "entry", "2099-01-01T00:00:00Z"),
            ("o2", "evt-redeem", "2099-01-02", "31°C", "NO", "main", "entry", "2099-01-02T00:00:00Z"),
            ("o3", "evt-stale", "2099-01-03", "32°C", "YES", "main", "entry", "2099-01-03T00:00:00Z"),
        ]
        cur.executemany(
            """
            INSERT INTO auto_orders (order_id, slug, event_date, outcome, side, strategy, action, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        cur.executemany(
            """
            INSERT INTO edge_trades (
              slug, event_date, outcome, side, state, position_size, avg_price, entry_price, mark_price,
              entry_at, updated_at, last_order_id, last_status, last_action
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("evt-trading", "2099-01-01", "30°C", "YES", "entry", 9.0, 0.4, 0.4, 0.45, "2099-01-01T00:00:00Z", "2099-01-01T01:00:00Z", "o1", "entry_open", "entry"),
                ("evt-redeem", "2099-01-02", "31°C", "NO", "entry", 7.0, 0.5, 0.5, 0.52, "2099-01-02T00:00:00Z", "2099-01-02T01:00:00Z", "o2", "entry_open", "entry"),
                ("evt-stale", "2099-01-03", "32°C", "YES", "entry", 6.0, 0.6, 0.6, 0.62, "2099-01-03T00:00:00Z", "2099-01-03T01:00:00Z", "o3", "entry_open", "entry"),
            ],
        )
        conn.commit()

        original = self._patch_server_flags(srv)
        try:
            srv.POSITIONS_SYNC_ENABLED = True
            srv.POSITIONS_API_URL = "https://example.test/positions"
            srv.ACCOUNT_REQUESTS_DISABLED = False
            srv.POSITIONS_CACHE.clear()
            srv.POSITIONS_CACHE.update({"ts": 0.0, "updated_at": "2099-01-03T12:00:00Z", "error": "", "positions": {}})

            def _stub_fetch_positions_cache(_conn, force=False):
                return (
                    {
                        ("evt-trading", "2099-01-01", "30°C", "YES"): {
                            "position_size": 3.0,
                            "avg_price": 0.41,
                            "redeemable": False,
                            "updated_at": "2099-01-03T10:00:00Z",
                        },
                        ("evt-redeem", "2099-01-02", "31°C", "NO"): {
                            "position_size": 2.0,
                            "avg_price": 0.5,
                            "redeemable": True,
                            "updated_at": "2099-01-03T11:00:00Z",
                        },
                    },
                    "",
                    "2099-01-03T12:00:00Z",
                )

            srv.fetch_positions_cache = _stub_fetch_positions_cache
            stats = srv.compute_auto_trade_daily_stats(conn)
        finally:
            self._restore_server_flags(srv, original)

        self.assertEqual(int(stats.get("open_position_count") or 0), 1)
        self.assertEqual(int(stats.get("open_position_redeemable_count") or 0), 1)
        self.assertEqual(int(stats.get("open_position_total_count") or 0), 2)
        self.assertEqual(int(stats.get("open_position_count_all") or 0), 2)
        self.assertEqual(str(stats.get("position_truth_source") or ""), "positions_api_funder")
        redeemable_events = stats.get("open_position_redeemable_events")
        self.assertIsInstance(redeemable_events, list)
        self.assertEqual(len(redeemable_events), 1)
        self.assertEqual(str(redeemable_events[0].get("slug") or ""), "evt-redeem")


if __name__ == "__main__":
    unittest.main()
