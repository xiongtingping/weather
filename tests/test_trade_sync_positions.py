import json
import sqlite3
import unittest

import scan_weather_market as swm


class TestTradeSyncPositions(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_assume_zero = swm.POSITIONS_ASSUME_ZERO
        swm.POSITIONS_ASSUME_ZERO = True

    def tearDown(self) -> None:
        swm.POSITIONS_ASSUME_ZERO = self._orig_assume_zero

    def _make_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        cur = conn.cursor()
        cur.executescript(
            """
            CREATE TABLE edge_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT,
                event_date TEXT,
                outcome TEXT,
                side TEXT,
                state TEXT,
                position_size REAL,
                avg_price REAL,
                last_action TEXT,
                last_action_at TEXT,
                last_status TEXT,
                last_error TEXT,
                updated_at TEXT
            );
            CREATE TABLE market_tokens (
                slug TEXT NOT NULL,
                event_date TEXT NOT NULL,
                outcome TEXT NOT NULL,
                yes_token TEXT,
                no_token TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (slug, event_date, outcome)
            );
            CREATE TABLE event_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT,
                event_date TEXT,
                payload_json TEXT,
                created_at TEXT
            );
            """
        )
        conn.commit()
        return conn

    def test_sync_positions_updates_local_active_rows_only(self) -> None:
        conn = self._make_conn()
        cur = conn.cursor()
        cur.executemany(
            """
            INSERT INTO edge_trades (
                slug, event_date, outcome, side, state, position_size, avg_price,
                last_action, last_action_at, last_status, last_error, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "evt-local",
                    "2099-01-01",
                    "30°C",
                    "YES",
                    "entry",
                    1.0,
                    0.40,
                    "",
                    "",
                    "",
                    "",
                    "",
                ),
                (
                    "evt-missing",
                    "2099-01-02",
                    "31°C",
                    "NO",
                    "entry",
                    2.0,
                    0.55,
                    "",
                    "",
                    "",
                    "",
                    "",
                ),
            ],
        )
        cur.executemany(
            """
            INSERT INTO market_tokens (slug, event_date, outcome, yes_token, no_token, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                ("evt-local", "2099-01-01", "30°C", "tok_local_yes", "tok_local_no", "2099-01-01T00:00:00Z"),
                ("evt-missing", "2099-01-02", "31°C", "tok_missing_yes", "tok_missing_no", "2099-01-02T00:00:00Z"),
            ],
        )
        conn.commit()

        updated = swm.sync_positions_to_db(
            conn,
            {
                "tok_local_yes": {"size": 3.0, "avg_price": 0.44},
                "tok_unrelated": {"size": 9.0, "avg_price": 0.91},
            },
        )
        self.assertGreaterEqual(updated, 2)

        cur.execute(
            """
            SELECT slug, position_size, avg_price, state, last_action, last_status
            FROM edge_trades
            ORDER BY slug
            """
        )
        rows = {str(row["slug"]): dict(row) for row in cur.fetchall()}
        self.assertAlmostEqual(float(rows["evt-local"]["position_size"] or 0.0), 3.0, places=6)
        self.assertAlmostEqual(float(rows["evt-local"]["avg_price"] or 0.0), 0.44, places=6)
        self.assertEqual(str(rows["evt-local"]["state"] or ""), "entry")
        self.assertEqual(str(rows["evt-local"]["last_action"] or ""), "pos_sync")
        self.assertEqual(str(rows["evt-local"]["last_status"] or ""), "pos_sync")

        self.assertAlmostEqual(float(rows["evt-missing"]["position_size"] or 0.0), 0.0, places=6)
        self.assertEqual(str(rows["evt-missing"]["state"] or ""), "exit")
        self.assertEqual(str(rows["evt-missing"]["last_action"] or ""), "pos_sync_clear")
        self.assertEqual(str(rows["evt-missing"]["last_status"] or ""), "no_position")

    def test_sync_positions_falls_back_to_latest_event_payload(self) -> None:
        conn = self._make_conn()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO edge_trades (
                slug, event_date, outcome, side, state, position_size, avg_price,
                last_action, last_action_at, last_status, last_error, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "evt-fallback",
                "2099-01-03",
                "32°C",
                "YES",
                "entry",
                1.0,
                0.25,
                "",
                "",
                "",
                "",
                "",
            ),
        )
        payload = json.dumps(
            {
                "rows": [
                    {
                        "outcome": "32°C",
                        "yes_token": "tok_payload_yes",
                        "no_token": "tok_payload_no",
                    }
                ]
            }
        )
        cur.execute(
            """
            INSERT INTO event_runs (slug, event_date, payload_json, created_at)
            VALUES (?, ?, ?, ?)
            """,
            ("evt-fallback", "2099-01-03", payload, "2099-01-03T00:00:00Z"),
        )
        conn.commit()

        updated = swm.sync_positions_to_db(
            conn,
            {"tok_payload_yes": {"size": 2.5, "avg_price": 0.33}},
        )
        self.assertGreaterEqual(updated, 1)

        cur.execute(
            """
            SELECT position_size, avg_price, state, last_action, last_status
            FROM edge_trades
            WHERE slug = 'evt-fallback'
            """
        )
        row = cur.fetchone()
        self.assertIsNotNone(row)
        self.assertAlmostEqual(float(row["position_size"] or 0.0), 2.5, places=6)
        self.assertAlmostEqual(float(row["avg_price"] or 0.0), 0.33, places=6)
        self.assertEqual(str(row["state"] or ""), "entry")
        self.assertEqual(str(row["last_action"] or ""), "pos_sync")
        self.assertEqual(str(row["last_status"] or ""), "pos_sync")

    def test_reconcile_clears_past_confirmed_no_position_entry(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        cur = conn.cursor()
        cur.executescript(
            """
            CREATE TABLE edge_trade_fills (
                slug TEXT,
                event_date TEXT,
                outcome TEXT,
                outcome_side TEXT,
                trade_side TEXT,
                size REAL,
                trade_time TEXT
            );
            CREATE TABLE edge_trades (
                slug TEXT,
                event_date TEXT,
                outcome TEXT,
                side TEXT,
                state TEXT,
                position_size REAL,
                last_order_id TEXT,
                last_action TEXT,
                last_action_at TEXT,
                last_status TEXT,
                last_error TEXT,
                exit_skip_reason TEXT,
                exit_skip_detail TEXT,
                exit_skip_at TEXT,
                updated_at TEXT
            );
            """
        )
        cur.execute(
            """
            INSERT INTO edge_trades (
                slug, event_date, outcome, side, state, position_size, last_order_id,
                last_action, last_status, last_error, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "past-event",
                "2000-01-01",
                "24°C",
                "YES",
                "entry",
                None,
                "0xabc",
                "confirm",
                "confirmed",
                "",
                "2000-01-01T12:00:00+00:00",
            ),
        )
        conn.commit()

        updated = swm.reconcile_edge_trades_positions_from_fills(conn)
        self.assertGreaterEqual(updated, 1)

        cur.execute(
            """
            SELECT state, position_size, last_order_id, last_action, last_status, last_error
            FROM edge_trades
            WHERE slug = 'past-event'
            """
        )
        row = cur.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(str(row["state"] or ""), "exit")
        self.assertAlmostEqual(float(row["position_size"] or 0.0), 0.0, places=6)
        self.assertEqual(str(row["last_order_id"] or ""), "")
        self.assertEqual(str(row["last_action"] or ""), "reconcile_no_position")
        self.assertEqual(str(row["last_status"] or ""), "no_position")
        self.assertEqual(str(row["last_error"] or ""), "")


if __name__ == "__main__":
    unittest.main()
