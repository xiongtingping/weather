import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import scan_weather_market as swm


class TestEventRunQHydration(unittest.TestCase):
    def test_payload_row_q_fields_for_side_yes_preserves_q_quote(self) -> None:
        out = swm._payload_row_q_fields_for_side(
            {
                "q_entry": 0.61,
                "q_round": 0.62,
                "q_calibrated": 0.63,
                "q_quote": 0.58,
                "q_quote_blend_alpha": 0.75,
                "q_quote_source": "bundle_segmented_platt",
                "q_edge": 0.64,
            },
            "YES",
        )

        self.assertAlmostEqual(out["q_entry"], 0.61)
        self.assertAlmostEqual(out["q_raw"], 0.62)
        self.assertAlmostEqual(out["q_calibrated"], 0.63)
        self.assertAlmostEqual(out["q_quote"], 0.58)
        self.assertAlmostEqual(out["q_quote_blend_alpha"], 0.75)
        self.assertEqual(out["q_quote_source"], "bundle_segmented_platt")
        self.assertAlmostEqual(out["q_edge"], 0.64)

    def test_payload_row_q_fields_for_side_no_complements_yes_q_quote(self) -> None:
        out = swm._payload_row_q_fields_for_side(
            {
                "q_entry": 0.25,
                "q_round": 0.24,
                "q_calibrated": 0.23,
                "q_quote": 0.21,
                "q_quote_blend_alpha": 0.8,
            },
            "NO",
        )

        self.assertAlmostEqual(out["q_entry"], 0.75)
        self.assertAlmostEqual(out["q_raw"], 0.76)
        self.assertAlmostEqual(out["q_calibrated"], 0.77)
        self.assertAlmostEqual(out["q_quote"], 0.79)
        self.assertAlmostEqual(out["q_quote_blend_alpha"], 0.8)
        self.assertEqual(out["q_quote_source"], "complement_yes_q_quote")
        self.assertAlmostEqual(out["q_edge"], 0.77)

    def test_market_refresh_skips_qless_event_run_without_history(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
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
        cur.execute(
            """
            CREATE TABLE event_fact_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date TEXT NOT NULL,
                slug TEXT NOT NULL,
                event_date TEXT NOT NULL,
                fact_as_of TEXT NOT NULL,
                composer_version TEXT NOT NULL,
                fact_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )

        swm._save_event_run_from_market_refresh(
            conn,
            "2026-04-03",
            "2026-04-03",
            "test-slug",
            [{"outcome": "10C", "best_bid": 0.40, "best_ask": 0.42, "spread": 0.02}],
            "2026-04-03T11:05:00+00:00",
            event_url="https://polymarket.com/event/test-slug",
            title="Test Market",
        )

        saved = cur.execute("SELECT COUNT(*) FROM event_runs").fetchone()[0]
        conn.close()
        self.assertEqual(saved, 0)

    def test_market_refresh_seeds_event_run_with_historical_q_rows(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
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
        cur.execute(
            """
            CREATE TABLE event_fact_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date TEXT NOT NULL,
                slug TEXT NOT NULL,
                event_date TEXT NOT NULL,
                fact_as_of TEXT NOT NULL,
                composer_version TEXT NOT NULL,
                fact_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        fact_payload = {
            "slug": "test-slug",
            "distribution": [
                {
                    "outcome": "10C",
                    "q_entry": 0.61,
                    "q_round": 0.62,
                    "q_calibrated": 0.63,
                    "q_quote": 0.58,
                }
            ],
        }
        cur.execute(
            """
            INSERT INTO event_fact_history(
                run_date, slug, event_date, fact_as_of, composer_version, fact_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "2026-04-02",
                "test-slug",
                "2026-04-03",
                "2026-04-02T11:00:00+00:00",
                "test",
                json.dumps(fact_payload, ensure_ascii=True),
                "2026-04-02T11:00:00+00:00",
            ),
        )

        swm._save_event_run_from_market_refresh(
            conn,
            "2026-04-03",
            "2026-04-03",
            "test-slug",
            [{"outcome": "10C", "best_bid": 0.40, "best_ask": 0.42, "spread": 0.02}],
            "2026-04-03T11:05:00+00:00",
            event_url="https://polymarket.com/event/test-slug",
            title="Test Market",
        )

        saved = cur.execute(
            "SELECT payload_json FROM event_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        payload = json.loads(saved["payload_json"])
        row = payload["rows"][0]
        conn.close()

        self.assertAlmostEqual(row["best_bid"], 0.40)
        self.assertAlmostEqual(row["best_ask"], 0.42)
        self.assertAlmostEqual(row["spread"], 0.02)
        self.assertAlmostEqual(row["q_entry"], 0.61)
        self.assertAlmostEqual(row["q_round"], 0.62)
        self.assertAlmostEqual(row["q_calibrated"], 0.63)
        self.assertAlmostEqual(row["q_quote"], 0.58)

    def test_market_refresh_restores_full_outcome_coverage_from_history(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
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
        cur.execute(
            """
            CREATE TABLE event_fact_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date TEXT NOT NULL,
                slug TEXT NOT NULL,
                event_date TEXT NOT NULL,
                fact_as_of TEXT NOT NULL,
                composer_version TEXT NOT NULL,
                fact_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            INSERT INTO event_runs(run_date, event_date, slug, event_url, title, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "2026-04-03",
                "2026-04-03",
                "test-slug",
                "https://polymarket.com/event/test-slug",
                "Test Market",
                json.dumps(
                    {
                        "slug": "test-slug",
                        "rows": [
                            {
                                "outcome": "10C",
                                "best_bid": 0.40,
                                "best_ask": 0.42,
                                "spread": 0.02,
                            }
                        ],
                    },
                    ensure_ascii=True,
                ),
                "2026-04-03T11:00:00+00:00",
            ),
        )
        cur.execute(
            """
            INSERT INTO event_fact_history(
                run_date, slug, event_date, fact_as_of, composer_version, fact_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "2026-04-02",
                "test-slug",
                "2026-04-03",
                "2026-04-02T11:00:00+00:00",
                "test",
                json.dumps(
                    {
                        "slug": "test-slug",
                        "distribution": [
                            {
                                "outcome": "10C",
                                "q_entry": 0.61,
                                "q_round": 0.62,
                                "q_calibrated": 0.63,
                                "q_quote": 0.58,
                            },
                            {
                                "outcome": "11C",
                                "q_entry": 0.20,
                                "q_round": 0.21,
                                "q_calibrated": 0.22,
                                "q_quote": 0.24,
                            },
                        ],
                    },
                    ensure_ascii=True,
                ),
                "2026-04-02T11:00:00+00:00",
            ),
        )

        swm._save_event_run_from_market_refresh(
            conn,
            "2026-04-03",
            "2026-04-03",
            "test-slug",
            [{"outcome": "10C", "best_bid": 0.41, "best_ask": 0.43, "spread": 0.02}],
            "2026-04-03T11:05:00+00:00",
            event_url="https://polymarket.com/event/test-slug",
            title="Test Market",
        )

        saved = cur.execute(
            "SELECT payload_json FROM event_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        payload = json.loads(saved["payload_json"])
        rows = payload["rows"]
        by_outcome = {row["outcome"]: row for row in rows}
        conn.close()

        self.assertEqual(sorted(by_outcome), ["10C", "11C"])
        self.assertAlmostEqual(by_outcome["10C"]["best_bid"], 0.41)
        self.assertAlmostEqual(by_outcome["10C"]["q_calibrated"], 0.63)
        self.assertAlmostEqual(by_outcome["11C"]["q_calibrated"], 0.22)

    def test_run_auto_trades_hydrates_qless_event_run_from_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE event_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_date TEXT,
                    event_date TEXT,
                    slug TEXT,
                    event_url TEXT,
                    title TEXT,
                    payload_json TEXT,
                    created_at TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE event_fact_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_date TEXT NOT NULL,
                    slug TEXT NOT NULL,
                    event_date TEXT NOT NULL,
                    fact_as_of TEXT NOT NULL,
                    composer_version TEXT NOT NULL,
                    fact_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            cur.execute(
                """
                INSERT INTO event_runs(run_date, event_date, slug, event_url, title, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "2026-04-03",
                    "2026-04-04",
                    "test-slug",
                    "https://polymarket.com/event/test-slug",
                    "Test Market",
                    json.dumps(
                        {
                            "slug": "test-slug",
                            "rows": [
                                {
                                    "outcome": "10C",
                                    "best_bid": 0.40,
                                    "best_ask": 0.42,
                                    "spread": 0.02,
                                }
                            ],
                        },
                        ensure_ascii=True,
                    ),
                    "2026-04-03T12:00:00+00:00",
                ),
            )
            cur.execute(
                """
                INSERT INTO event_fact_history(
                    run_date, slug, event_date, fact_as_of, composer_version, fact_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "2026-04-02",
                    "test-slug",
                    "2026-04-04",
                    "2026-04-02T11:00:00+00:00",
                    "test",
                    json.dumps(
                        {
                            "slug": "test-slug",
                            "distribution": [
                                {
                                    "outcome": "10C",
                                    "q_entry": 0.61,
                                    "q_round": 0.62,
                                    "q_calibrated": 0.63,
                                    "q_quote": 0.58,
                                }
                            ],
                        },
                        ensure_ascii=True,
                    ),
                    "2026-04-02T11:00:00+00:00",
                ),
            )
            conn.commit()
            conn.close()

            captured = []

            def _fake_execute(
                run_date: str,
                event_date: str,
                payload: dict,
                *,
                exit_only: bool = False,
                run_id=None,
                positions_snapshot=None,
                positions_snapshot_error="",
                settled_reconcile_context=None,
            ) -> None:
                captured.append((run_date, event_date, payload))

            with mock.patch.object(swm, "DB_PATH", db_path):
                with mock.patch.dict("os.environ", {"PM_AUTO_TRADE_EVENT_TIMEOUT_SEC": "0"}, clear=False):
                    with mock.patch.object(swm, "execute_auto_trades", side_effect=_fake_execute):
                        result = swm.run_auto_trades_for_date("2026-04-03", exit_only=False, run_id=7)

            self.assertTrue(result.get("ok"))
            self.assertEqual(result.get("processed"), 1)
            self.assertEqual(result.get("skipped"), 0)
            self.assertEqual(len(captured), 1)
            payload = captured[0][2]
            row = payload["rows"][0]
            self.assertAlmostEqual(row["best_bid"], 0.40)
            self.assertAlmostEqual(row["best_ask"], 0.42)
            self.assertAlmostEqual(row["spread"], 0.02)
            self.assertAlmostEqual(row["q_entry"], 0.61)
            self.assertAlmostEqual(row["q_round"], 0.62)
            self.assertAlmostEqual(row["q_calibrated"], 0.63)
            self.assertAlmostEqual(row["q_quote"], 0.58)


if __name__ == "__main__":
    unittest.main()
