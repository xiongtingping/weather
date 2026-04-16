import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import scan_weather_market as swm


class TestPriorityQMissing(unittest.TestCase):
    def test_priority_plan_includes_recent_market_tick_stale_entry_blockers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            with mock.patch.object(swm, "DB_PATH", db_path):
                swm.init_db()
                now = datetime.now(timezone.utc)
                now_text = now.isoformat()
                stale_text = (now - timedelta(hours=2)).isoformat()
                run_date = now.date().isoformat()
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO auto_trade_decisions(
                        run_id, run_date, slug, event_date, title, outcome, side, action,
                        decided_state, will_order, reason_codes_json, reasons_json, details_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        1,
                        run_date,
                        "highest-temperature-in-stale-city-on-test-date",
                        run_date,
                        "Stale City",
                        "10C",
                        "YES",
                        "entry",
                        "blocked",
                        0,
                        '["market_tick_stale"]',
                        '["tick stale"]',
                        "{}",
                        now_text,
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO auto_trade_decisions(
                        run_id, run_date, slug, event_date, title, outcome, side, action,
                        decided_state, will_order, reason_codes_json, reasons_json, details_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        2,
                        run_date,
                        "highest-temperature-in-old-stale-city-on-test-date",
                        run_date,
                        "Old Stale City",
                        "11C",
                        "NO",
                        "entry",
                        "blocked",
                        0,
                        '["market_tick_stale"]',
                        '["tick stale"]',
                        "{}",
                        stale_text,
                    ),
                )
                conn.commit()
                conn.close()

                with mock.patch.dict(
                    "os.environ",
                    {
                        "PM_SCAN_PRIORITY_ENABLED": "1",
                        "PM_SCAN_PRIORITY_INCLUDE_MARKET_TICK_STALE": "1",
                        "PM_SCAN_PRIORITY_MARKET_TICK_STALE_TOPN": "10",
                        "PM_SCAN_PRIORITY_MARKET_TICK_STALE_WINDOW_MIN": "30",
                        "PM_SCAN_PRIORITY_TOPN": "10",
                        "PM_SCAN_PRIORITY_LOOKAHEAD_DAYS": "0",
                    },
                    clear=False,
                ):
                    plan = swm._load_priority_scan_plan(run_date)

            tick_stale_slugs = list(plan.get("market_tick_stale_slugs") or [])
            self.assertIn("highest-temperature-in-stale-city-on-test-date", tick_stale_slugs)
            self.assertNotIn("highest-temperature-in-old-stale-city-on-test-date", tick_stale_slugs)

    def test_priority_plan_includes_recent_market_without_model_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            with mock.patch.object(swm, "DB_PATH", db_path):
                swm.init_db()
                now_text = datetime.now(timezone.utc).isoformat()
                run_date = datetime.now(timezone.utc).date().isoformat()
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO market_snapshots(run_date, event_date, slug, payload_json, meta_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_date,
                        run_date,
                        "highest-temperature-in-test-city-on-test-date",
                        '{"slug":"highest-temperature-in-test-city-on-test-date","rows":[{"outcome":"10C","best_bid":0.4,"best_ask":0.42}]}',
                        '{"slug":"highest-temperature-in-test-city-on-test-date","title":"Test City"}',
                        now_text,
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO market_snapshots(run_date, event_date, slug, payload_json, meta_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_date,
                        run_date,
                        "highest-temperature-in-modeled-city-on-test-date",
                        '{"slug":"highest-temperature-in-modeled-city-on-test-date","rows":[{"outcome":"11C","best_bid":0.3,"best_ask":0.31}]}',
                        '{"slug":"highest-temperature-in-modeled-city-on-test-date","title":"Modeled City"}',
                        now_text,
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO model_snapshots(run_date, event_date, slug, payload_json, meta_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_date,
                        run_date,
                        "highest-temperature-in-modeled-city-on-test-date",
                        '{"slug":"highest-temperature-in-modeled-city-on-test-date","rows":[{"outcome":"11C","q_entry":0.4,"q_calibrated":0.41}]}',
                        '{"slug":"highest-temperature-in-modeled-city-on-test-date","title":"Modeled City"}',
                        now_text,
                    ),
                )
                conn.commit()
                conn.close()

                with mock.patch.dict(
                    "os.environ",
                    {
                        "PM_SCAN_PRIORITY_ENABLED": "1",
                        "PM_SCAN_PRIORITY_INCLUDE_Q_MISSING": "1",
                        "PM_SCAN_PRIORITY_Q_MISSING_TOPN": "10",
                        "PM_SCAN_PRIORITY_TOPN": "10",
                        "PM_SCAN_PRIORITY_LOOKAHEAD_DAYS": "0",
                    },
                    clear=False,
                ):
                    plan = swm._load_priority_scan_plan(run_date)

            q_missing_slugs = list(plan.get("q_missing_slugs") or [])
            self.assertIn("highest-temperature-in-test-city-on-test-date", q_missing_slugs)
            self.assertNotIn("highest-temperature-in-modeled-city-on-test-date", q_missing_slugs)

    def test_market_tick_stale_priority_survives_tail_reservation_when_capped(self) -> None:
        run_date = "2026-04-14"
        priority_plan = {
            "enabled": True,
            "ordered_slugs": [
                "stale-a",
                "stale-b",
                "tail-x",
                "tail-y",
                "candidate-z",
            ],
            "fixed_slugs": ["stale-a", "stale-b", "tail-x", "tail-y"],
            "candidate_slugs": ["candidate-z"],
            "market_tick_stale_slugs": ["stale-a", "stale-b"],
            "tail_slugs": ["tail-y", "tail-x"],
            "fixed_pruned_no_quote_slugs": [],
            "position_slugs": [],
            "open_order_slugs": [],
            "q_missing_slugs": [],
            "entry_ok_slugs": [],
            "event_stub_by_slug": {
                "stale-a": {"slug": "stale-a", "title": "stale-a", "endDate": run_date, "description": "", "resolutionSource": ""},
                "stale-b": {"slug": "stale-b", "title": "stale-b", "endDate": run_date, "description": "", "resolutionSource": ""},
                "tail-x": {"slug": "tail-x", "title": "tail-x", "endDate": run_date, "description": "", "resolutionSource": ""},
                "tail-y": {"slug": "tail-y", "title": "tail-y", "endDate": run_date, "description": "", "resolutionSource": ""},
                "candidate-z": {"slug": "candidate-z", "title": "candidate-z", "endDate": run_date, "description": "", "resolutionSource": ""},
            },
            "others_per_run": 0,
            "candidates_per_run": 1,
        }
        fake_events = [
            {"slug": "stale-a", "title": "stale-a", "endDate": run_date},
            {"slug": "stale-b", "title": "stale-b", "endDate": run_date},
            {"slug": "tail-x", "title": "tail-x", "endDate": run_date},
            {"slug": "tail-y", "title": "tail-y", "endDate": run_date},
            {"slug": "candidate-z", "title": "candidate-z", "endDate": run_date},
        ]
        args = [
            "scan_weather_market.py",
            "--scan-weather-today",
            "--market-refresh-only",
            "--days-ahead",
            "0",
            "--tz",
            "America/New_York",
        ]
        with (
            mock.patch("scan_weather_market.fetch_weather_events", return_value=(fake_events, [])),
            mock.patch("scan_weather_market._load_priority_scan_plan", return_value=priority_plan),
            mock.patch(
                "scan_weather_market.refresh_market_ticks_for_event",
                return_value=(True, None, 0, [], "2026-04-14T00:00:00+00:00"),
            ) as refresh_mock,
            mock.patch("scan_weather_market.save_market_snapshot"),
            mock.patch.dict(
                "os.environ",
                {
                    "PM_SCAN_PRIORITY_ENABLED": "1",
                    "PM_SCAN_MAX_EVENTS_PER_RUN": "4",
                    "PM_SCAN_MAX_MATCHED_PER_RUN": "4",
                    "PM_SCAN_PRIORITY_RESERVE_NONFIXED_SLOT": "1",
                    "PM_SCAN_PRIORITY_RESERVE_NONFIXED_SLOTS": "1",
                    "PM_SCAN_PRIORITY_RESERVE_TAIL_SLOT": "1",
                    "PM_SCAN_PRIORITY_RESERVE_TAIL_PREPEND": "1",
                },
                clear=False,
            ),
            mock.patch("sys.argv", args),
        ):
            swm.main()

        called_slugs = [
            call.args[0]
            for call in refresh_mock.call_args_list
        ]
        self.assertIn("stale-a", called_slugs)
        self.assertIn("stale-b", called_slugs)

    def test_priority_plan_includes_recent_market_with_stale_model_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            with mock.patch.object(swm, "DB_PATH", db_path):
                swm.init_db()
                now = datetime.now(timezone.utc)
                now_text = now.isoformat()
                stale_text = (now - timedelta(hours=2)).isoformat()
                fresh_text = (now - timedelta(minutes=5)).isoformat()
                run_date = now.date().isoformat()
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO market_snapshots(run_date, event_date, slug, payload_json, meta_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_date,
                        run_date,
                        "highest-temperature-in-stale-model-city-on-test-date",
                        '{"slug":"highest-temperature-in-stale-model-city-on-test-date","rows":[{"outcome":"10C","best_bid":0.4,"best_ask":0.42}]}',
                        '{"slug":"highest-temperature-in-stale-model-city-on-test-date","title":"Stale Model City"}',
                        now_text,
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO model_snapshots(run_date, event_date, slug, payload_json, meta_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_date,
                        run_date,
                        "highest-temperature-in-stale-model-city-on-test-date",
                        '{"slug":"highest-temperature-in-stale-model-city-on-test-date","rows":[{"outcome":"10C","q_entry":0.4,"q_calibrated":0.41}]}',
                        '{"slug":"highest-temperature-in-stale-model-city-on-test-date","title":"Stale Model City"}',
                        stale_text,
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO market_snapshots(run_date, event_date, slug, payload_json, meta_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_date,
                        run_date,
                        "highest-temperature-in-fresh-model-city-on-test-date",
                        '{"slug":"highest-temperature-in-fresh-model-city-on-test-date","rows":[{"outcome":"11C","best_bid":0.3,"best_ask":0.31}]}',
                        '{"slug":"highest-temperature-in-fresh-model-city-on-test-date","title":"Fresh Model City"}',
                        now_text,
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO model_snapshots(run_date, event_date, slug, payload_json, meta_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_date,
                        run_date,
                        "highest-temperature-in-fresh-model-city-on-test-date",
                        '{"slug":"highest-temperature-in-fresh-model-city-on-test-date","rows":[{"outcome":"11C","q_entry":0.4,"q_calibrated":0.41}]}',
                        '{"slug":"highest-temperature-in-fresh-model-city-on-test-date","title":"Fresh Model City"}',
                        fresh_text,
                    ),
                )
                conn.commit()
                conn.close()

                with mock.patch.dict(
                    "os.environ",
                    {
                        "PM_SCAN_PRIORITY_ENABLED": "1",
                        "PM_SCAN_PRIORITY_INCLUDE_Q_MISSING": "1",
                        "PM_SCAN_PRIORITY_Q_MISSING_TOPN": "10",
                        "PM_SCAN_PRIORITY_Q_MISSING_WINDOW_MIN": "30",
                        "PM_SCAN_PRIORITY_Q_MISSING_MODEL_LAG_MIN": "30",
                        "PM_SCAN_PRIORITY_TOPN": "10",
                        "PM_SCAN_PRIORITY_LOOKAHEAD_DAYS": "0",
                    },
                    clear=False,
                ):
                    plan = swm._load_priority_scan_plan(run_date)

            q_missing_slugs = list(plan.get("q_missing_slugs") or [])
            self.assertIn("highest-temperature-in-stale-model-city-on-test-date", q_missing_slugs)
            self.assertNotIn("highest-temperature-in-fresh-model-city-on-test-date", q_missing_slugs)

    def test_priority_plan_skips_stale_plain_lcb_peak_entry_ok_fact(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            with mock.patch.object(swm, "DB_PATH", db_path):
                swm.init_db()
                now = datetime.now(timezone.utc)
                now_text = now.isoformat()
                run_date = now.date().isoformat()
                event_date = (now.date() + timedelta(days=1)).isoformat()
                fact_json = {
                    "run_date": run_date,
                    "event_date": event_date,
                    "market_top_label": "25C+",
                    "auto_trade_ssot": {
                        "main_candidate": {
                            "outcome": "25C+",
                            "side": "NO",
                            "entry_ok": True,
                            "edge_score": 15.0,
                        },
                        "main_entry_ok": True,
                    },
                }
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                cur.execute(
                    """
                    CREATE TABLE event_fact_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_date TEXT NOT NULL,
                        slug TEXT NOT NULL,
                        event_date TEXT NOT NULL,
                        fact_as_of TEXT NOT NULL,
                        composer_version TEXT NOT NULL,
                        best_side TEXT,
                        best_outcome TEXT,
                        best_edge_score REAL,
                        fact_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE event_fact_latest (
                        slug TEXT NOT NULL,
                        event_date TEXT NOT NULL,
                        fact_id INTEGER NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (slug, event_date)
                    )
                    """
                )
                cur.execute(
                    """
                    INSERT INTO event_fact_history(
                        run_date, slug, event_date, fact_as_of,
                        composer_version, best_side, best_outcome, best_edge_score, fact_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_date,
                        "highest-temperature-in-peak-city-on-test-date",
                        event_date,
                        now_text,
                        "test",
                        "NO",
                        "25C+",
                        15.0,
                        json.dumps(fact_json),
                        now_text,
                    ),
                )
                fact_id = cur.lastrowid
                cur.execute(
                    """
                    INSERT INTO event_fact_latest(slug, event_date, fact_id, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        "highest-temperature-in-peak-city-on-test-date",
                        event_date,
                        fact_id,
                        now_text,
                    ),
                )
                conn.commit()
                conn.close()

                with mock.patch.dict(
                    "os.environ",
                    {
                        "PM_SCAN_PRIORITY_ENABLED": "1",
                        "PM_SCAN_PRIORITY_INCLUDE_ENTRY_OK": "1",
                        "PM_SCAN_PRIORITY_ENTRY_OK_TOPN": "10",
                        "PM_SCAN_PRIORITY_TOPN": "10",
                        "PM_SCAN_PRIORITY_LOOKAHEAD_DAYS": "1",
                        "PM_LCB_GLOBAL_ENABLED": "1",
                        "PM_LCB_GLOBAL_PLAIN_NO_PEAK_EXCLUDE": "1",
                    },
                    clear=False,
                ):
                    plan = swm._load_priority_scan_plan(run_date)

            entry_ok_slugs = list(plan.get("entry_ok_slugs") or [])
            self.assertNotIn("highest-temperature-in-peak-city-on-test-date", entry_ok_slugs)


if __name__ == "__main__":
    unittest.main()
