import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import scan_weather_market as swm


class TestPriorityQMissing(unittest.TestCase):
    def test_priority_plan_has_no_chengdu_fast_path(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "scan_weather_market.py").read_text()

        self.assertNotIn("_load_chengdu_zuuu_priority_fast", source)
        self.assertNotIn("scope_fast_path", source)
        self.assertIn("_priority_event_runs_scope_clause", source)
        self.assertIn("WITH latest AS (\n                  SELECT slug, MAX(id) AS max_id", source)
        self.assertNotIn(
            "SELECT slug, title, event_end_utc, resolution_source, created_at\n"
            "                FROM event_runs\n"
            "                WHERE slug IN",
            source,
        )

    def test_priority_event_stub_uses_latest_event_run_per_slug(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            db_path = tmp_path / "archive.db"
            data_dir = tmp_path / "data"
            reports_dir = data_dir / "reports"
            reports_dir.mkdir(parents=True, exist_ok=True)
            slug = "highest-temperature-in-chengdu-on-june-18-2026"
            gap_report = reports_dir / "recommended_pilot_gap_scan_plan_latest.json"
            gap_report.write_text(json.dumps({"ok": False, "priority_slugs": [slug]}), encoding="utf-8")
            with (
                mock.patch.object(swm, "DB_PATH", db_path),
                mock.patch.object(swm, "DATA_DIR", data_dir),
            ):
                swm.init_db()
                run_date = "2026-06-18"
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS weather_market_discovery (
                        slug TEXT NOT NULL,
                        event_date TEXT NOT NULL,
                        title TEXT,
                        market_open_at TEXT,
                        first_seen_at TEXT NOT NULL,
                        first_tokenized_at TEXT,
                        token_count INTEGER NOT NULL DEFAULT 0,
                        status TEXT,
                        details_json TEXT,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (slug, event_date)
                    )
                    """
                )
                cur.execute(
                    """
                    INSERT INTO weather_market_discovery(
                        slug, event_date, title, first_seen_at, token_count, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        slug,
                        run_date,
                        "Discovery title",
                        "2026-06-18T00:00:00+00:00",
                        3,
                        "2026-06-18T00:00:00+00:00",
                    ),
                )
                for idx in range(30):
                    cur.execute(
                        """
                        INSERT INTO event_runs(
                            run_date, event_date, slug, title, resolution_source, suggestions_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run_date,
                            run_date,
                            slug,
                            f"Old title {idx}",
                            "https://old.example.com",
                            "[]",
                            "2099-01-01T00:00:00+00:00",
                        ),
                    )
                cur.execute(
                    """
                    INSERT INTO event_runs(
                        run_date, event_date, slug, title, resolution_source, event_end_utc,
                        suggestions_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_date,
                        run_date,
                        slug,
                        "Latest title",
                        "https://latest.example.com",
                        "2026-06-18T16:00:00+00:00",
                        "[]",
                        "2026-06-18T01:00:00+00:00",
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO market_tokens(
                        slug, event_date, outcome, yes_token, no_token,
                        market_active, market_closed, market_archived, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (slug, run_date, "30°C", "yes", "no", 1, 0, 0, "2026-06-18T01:00:00+00:00"),
                )
                conn.commit()
                conn.close()

                with mock.patch.dict(
                    "os.environ",
                    {
                        "PM_SCAN_PRIORITY_ENABLED": "1",
                        "PM_SCAN_PRIORITY_INCLUDE_RECOMMENDED_PILOT_GAPS": "1",
                        "PM_SCAN_PRIORITY_INCLUDE_ENTRY_OK": "0",
                        "PM_SCAN_PRIORITY_INCLUDE_Q_MISSING": "0",
                        "PM_SCAN_PRIORITY_INCLUDE_MARKET_TICK_STALE": "0",
                        "PM_SCAN_PRIORITY_INCLUDE_NEW_MARKETS": "0",
                        "PM_SCAN_PRIORITY_INCLUDE_FUTURE_MODEL_MISSING": "0",
                        "PM_SCAN_PRIORITY_INCLUDE_SETTLEMENT_PENDING": "0",
                        "PM_RECOMMENDED_PILOT_GAP_SCAN_PLAN_REPORT": str(gap_report),
                        "PM_SCAN_PRIORITY_TOPN": "3",
                        "PM_SCAN_PRIORITY_LOOKAHEAD_DAYS": "0",
                    },
                    clear=False,
                ):
                    plan = swm._load_priority_scan_plan(run_date)

            stub = dict(plan.get("event_stub_by_slug") or {}).get(slug)
            self.assertIsInstance(stub, dict)
            self.assertEqual(stub.get("title"), "Latest title")
            self.assertEqual(stub.get("resolutionSource"), "https://latest.example.com")

    def test_priority_plan_includes_stale_closed_likelihood_for_settlement_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            with mock.patch.object(swm, "DB_PATH", db_path):
                swm.init_db()
                run_date = "2026-06-18"
                now = datetime.now(timezone.utc).replace(microsecond=0)
                stale_refreshed = (now - timedelta(minutes=45)).isoformat()
                fresh_refreshed = (now - timedelta(minutes=1)).isoformat()
                event_end_text = (now - timedelta(hours=1)).isoformat()
                stale_slug = "highest-temperature-in-chengdu-on-june-18-2026"
                fresh_slug = "highest-temperature-in-chengdu-on-june-18-2026-fresh"
                conn = sqlite3.connect(db_path)
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS weather_market_discovery (
                        slug TEXT NOT NULL,
                        event_date TEXT NOT NULL,
                        title TEXT,
                        market_open_at TEXT,
                        first_seen_at TEXT NOT NULL,
                        first_tokenized_at TEXT,
                        token_count INTEGER NOT NULL DEFAULT 0,
                        status TEXT,
                        details_json TEXT,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (slug, event_date)
                    )
                    """
                )
                for slug, refreshed_at in (
                    (stale_slug, stale_refreshed),
                    (fresh_slug, fresh_refreshed),
                ):
                    conn.execute(
                        """
                        INSERT INTO weather_market_discovery(
                            slug, event_date, title, first_seen_at, token_count, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            slug,
                            run_date,
                            "Highest temperature in Chengdu on June 18?",
                            now.isoformat(),
                            3,
                            now.isoformat(),
                        ),
                    )
                    conn.execute(
                        """
                        INSERT INTO event_runs(
                            run_date, event_date, slug, title, resolution_source,
                            market_var, market_unit, lon, tz, market_closed,
                            event_end_utc, settlement_actual, payload_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run_date,
                            run_date,
                            slug,
                            "Highest temperature in Chengdu on June 18?",
                            "https://www.wunderground.com/history/daily/cn/chengdu/ZUUU",
                            "temperature_2m_max",
                            "C",
                            103.945966,
                            "Asia/Shanghai",
                            1,
                            event_end_text,
                            None,
                            json.dumps(
                                {
                                    "weather_likelihood_refreshed_at": refreshed_at,
                                    "rows": [
                                        {
                                            "outcome": "30°C",
                                            "q_bayes_posterior": 0.5,
                                            "weather_likelihood_refreshed_at": refreshed_at,
                                        }
                                    ],
                                }
                            ),
                            now.isoformat(),
                        ),
                    )
                conn.commit()
                conn.close()

                with mock.patch.dict(
                    "os.environ",
                    {
                        "PM_ARCHIVE_WRITE_SCOPE": "chengdu_zuuu",
                        "PM_SCAN_PRIORITY_ENABLED": "1",
                        "PM_SCAN_PRIORITY_LOOKAHEAD_DAYS": "0",
                        "PM_SCAN_PRIORITY_INCLUDE_LIKELIHOOD_STALE": "1",
                        "PM_SCAN_PRIORITY_LIKELIHOOD_STALE_TOPN": "10",
                        "PM_SCAN_PRIORITY_INCLUDE_SETTLEMENT_PENDING": "1",
                        "PM_SCAN_PRIORITY_SETTLEMENT_PENDING_TOPN": "10",
                        "PM_SCAN_PRIORITY_SETTLEMENT_LIKELIHOOD_STALE_SEC": "900",
                    },
                    clear=False,
                ):
                    plan = swm._load_priority_scan_plan(run_date, max_events_per_run=2)

        likelihood_stale_slugs = list(plan.get("likelihood_stale_slugs") or [])
        self.assertIn(stale_slug, likelihood_stale_slugs)
        self.assertNotIn(fresh_slug, likelihood_stale_slugs)
        self.assertIn(stale_slug, list(plan.get("fixed_slugs") or []))

    def test_priority_likelihood_stale_uses_likelihood_refresh_time_not_event_run_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            with mock.patch.object(swm, "DB_PATH", db_path):
                swm.init_db()
                now = datetime.now(timezone.utc).replace(microsecond=0)
                run_date = now.date().isoformat()
                stale_slug = "highest-temperature-in-stale-likelihood-city-on-test-date"
                fresh_slug = "highest-temperature-in-fresh-likelihood-city-on-test-date"
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                for slug, refreshed_at in (
                    (stale_slug, (now - timedelta(minutes=45)).isoformat()),
                    (fresh_slug, (now - timedelta(minutes=1)).isoformat()),
                ):
                    cur.execute(
                        """
                        INSERT INTO event_runs(
                            run_date, event_date, slug, title, resolution_source,
                            market_var, market_unit, lon, tz, market_closed,
                            payload_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run_date,
                            run_date,
                            slug,
                            "Highest temperature in Test City?",
                            "https://www.wunderground.com/history/daily/cn/chengdu/ZUUU",
                            "temperature_2m_max",
                            "C",
                            103.945966,
                            "Asia/Shanghai",
                            0,
                            json.dumps(
                                {
                                    "weather_likelihood_refreshed_at": refreshed_at,
                                    "observed_nowcast": {"refreshed_at": refreshed_at},
                                    "rows": [
                                        {
                                            "outcome": "30°C",
                                            "q_bayes_posterior": 0.5,
                                            "weather_likelihood_refreshed_at": refreshed_at,
                                        }
                                    ],
                                }
                            ),
                            now.isoformat(),
                        ),
                    )
                    cur.execute(
                        """
                        INSERT INTO market_tokens(
                            slug, event_date, outcome, yes_token, no_token,
                            market_active, market_closed, market_archived, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (slug, run_date, "30°C", "yes", "no", 1, 0, 0, now.isoformat()),
                    )
                conn.commit()
                conn.close()

                with mock.patch.dict(
                    "os.environ",
                    {
                        "PM_SCAN_PRIORITY_ENABLED": "1",
                        "PM_SCAN_PRIORITY_INCLUDE_LIKELIHOOD_STALE": "1",
                        "PM_SCAN_PRIORITY_LIKELIHOOD_STALE_TOPN": "10",
                        "PM_SCAN_PRIORITY_INCLUDE_ENTRY_OK": "0",
                        "PM_SCAN_PRIORITY_INCLUDE_Q_MISSING": "0",
                        "PM_SCAN_PRIORITY_INCLUDE_MARKET_TICK_STALE": "0",
                        "PM_SCAN_PRIORITY_INCLUDE_NEW_MARKETS": "0",
                        "PM_SCAN_PRIORITY_INCLUDE_FUTURE_MODEL_MISSING": "0",
                        "PM_SCAN_PRIORITY_INCLUDE_SETTLEMENT_PENDING": "0",
                        "PM_SCAN_PRIORITY_TOPN": "10",
                        "PM_SCAN_PRIORITY_LOOKAHEAD_DAYS": "0",
                        "PM_BAYES_LIKELIHOOD_REFRESH_START_HOUR": "0",
                        "PM_BAYES_LIKELIHOOD_REFRESH_PEAK_HOUR": "12",
                        "PM_BAYES_LIKELIHOOD_REFRESH_END_HOUR": "23",
                        "PM_BAYES_LIKELIHOOD_REFRESH_PRE_PEAK_MIN": "10",
                        "PM_BAYES_LIKELIHOOD_REFRESH_PEAK_MIN": "10",
                        "PM_BAYES_LIKELIHOOD_REFRESH_POST_PEAK_MIN": "10",
                    },
                    clear=False,
                ):
                    plan = swm._load_priority_scan_plan(run_date)

            likelihood_stale_slugs = list(plan.get("likelihood_stale_slugs") or [])
            self.assertIn(stale_slug, likelihood_stale_slugs)
            self.assertNotIn(fresh_slug, likelihood_stale_slugs)
            self.assertIn(stale_slug, list(plan.get("fixed_slugs") or []))

    def test_priority_plan_includes_recommended_pilot_gap_slugs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            db_path = tmp_path / "archive.db"
            data_dir = tmp_path / "data"
            reports_dir = data_dir / "reports"
            reports_dir.mkdir(parents=True, exist_ok=True)
            gap_slug = "highest-temperature-in-buenos-aires-on-june-1-2026"
            gap_report = reports_dir / "recommended_pilot_gap_scan_plan_latest.json"
            gap_report.write_text(
                json.dumps(
                    {
                        "ok": False,
                        "priority_slugs": [gap_slug],
                        "plan": [
                            {
                                "city": "buenos-aires",
                                "open_market_slugs": [gap_slug],
                                "scan_directives": [
                                    "scan_open_tail_no_reverse_edge_candidates"
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch.object(swm, "DB_PATH", db_path),
                mock.patch.object(swm, "DATA_DIR", data_dir),
            ):
                swm.init_db()
                now_text = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
                run_date = "2026-06-01"
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO event_runs(
                        run_date, event_date, slug, title, resolution_source, suggestions_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_date,
                        run_date,
                        gap_slug,
                        "Highest temperature in Buenos Aires on June 1?",
                        "https://www.wunderground.com/history/daily/ar/ezeiza/SAEZ",
                        "[]",
                        now_text,
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO market_tokens(
                        slug, event_date, outcome, yes_token, no_token,
                        market_active, market_closed, market_archived, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (gap_slug, run_date, "18C", "yes", "no", 1, 0, 0, now_text),
                )
                conn.commit()
                conn.close()

                with mock.patch.dict(
                    "os.environ",
                    {
                        "PM_SCAN_PRIORITY_ENABLED": "1",
                        "PM_SCAN_PRIORITY_INCLUDE_RECOMMENDED_PILOT_GAPS": "1",
                        "PM_SCAN_PRIORITY_INCLUDE_ENTRY_OK": "0",
                        "PM_SCAN_PRIORITY_INCLUDE_Q_MISSING": "0",
                        "PM_SCAN_PRIORITY_INCLUDE_MARKET_TICK_STALE": "0",
                        "PM_SCAN_PRIORITY_INCLUDE_NEW_MARKETS": "0",
                        "PM_SCAN_PRIORITY_INCLUDE_FUTURE_MODEL_MISSING": "0",
                        "PM_SCAN_PRIORITY_INCLUDE_SETTLEMENT_PENDING": "0",
                        "PM_RECOMMENDED_PILOT_GAP_SCAN_PLAN_REPORT": str(gap_report),
                        "PM_SCAN_PRIORITY_TOPN": "3",
                        "PM_SCAN_PRIORITY_LOOKAHEAD_DAYS": "0",
                    },
                    clear=False,
                ):
                    plan = swm._load_priority_scan_plan(run_date)

            self.assertEqual(list(plan.get("recommended_pilot_gap_slugs") or []), [gap_slug])
            self.assertIn(gap_slug, list(plan.get("candidate_slugs") or []))
            self.assertIn(gap_slug, list(plan.get("ordered_slugs") or []))
            self.assertIn(gap_slug, dict(plan.get("event_stub_by_slug") or {}))

    def test_priority_plan_rotates_recommended_pilot_gap_slugs_when_capped(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            db_path = tmp_path / "archive.db"
            data_dir = tmp_path / "data"
            reports_dir = data_dir / "reports"
            reports_dir.mkdir(parents=True, exist_ok=True)
            slugs = [
                "highest-temperature-in-ankara-on-june-1-2026",
                "highest-temperature-in-chongqing-on-june-1-2026",
                "highest-temperature-in-buenos-aires-on-june-1-2026",
                "highest-temperature-in-atlanta-on-june-1-2026",
            ]
            gap_report = reports_dir / "recommended_pilot_gap_scan_plan_latest.json"
            gap_report.write_text(
                json.dumps({"ok": False, "priority_slugs": slugs}),
                encoding="utf-8",
            )
            with (
                mock.patch.object(swm, "DB_PATH", db_path),
                mock.patch.object(swm, "DATA_DIR", data_dir),
            ):
                swm.init_db()
                now_text = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                for slug in slugs:
                    cur.execute(
                        """
                        INSERT INTO event_runs(
                            run_date, event_date, slug, title, resolution_source, suggestions_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "2026-06-01",
                            "2026-06-01",
                            slug,
                            slug,
                            "https://example.com",
                            "[]",
                            now_text,
                        ),
                    )
                    cur.execute(
                        """
                        INSERT INTO market_tokens(
                            slug, event_date, outcome, yes_token, no_token,
                            market_active, market_closed, market_archived, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (slug, "2026-06-01", "30C", f"yes-{slug}", f"no-{slug}", 1, 0, 0, now_text),
                    )
                conn.commit()
                conn.close()

                env = {
                    "PM_SCAN_PRIORITY_ENABLED": "1",
                    "PM_SCAN_PRIORITY_INCLUDE_RECOMMENDED_PILOT_GAPS": "1",
                    "PM_SCAN_PRIORITY_RECOMMENDED_PILOT_GAP_TOPN": "2",
                    "PM_SCAN_PRIORITY_INCLUDE_ENTRY_OK": "0",
                    "PM_SCAN_PRIORITY_INCLUDE_Q_MISSING": "0",
                    "PM_SCAN_PRIORITY_INCLUDE_MARKET_TICK_STALE": "0",
                    "PM_SCAN_PRIORITY_INCLUDE_NEW_MARKETS": "0",
                    "PM_SCAN_PRIORITY_INCLUDE_FUTURE_MODEL_MISSING": "0",
                    "PM_SCAN_PRIORITY_INCLUDE_SETTLEMENT_PENDING": "0",
                    "PM_RECOMMENDED_PILOT_GAP_SCAN_PLAN_REPORT": str(gap_report),
                    "PM_SCAN_PRIORITY_TOPN": "3",
                    "PM_SCAN_PRIORITY_LOOKAHEAD_DAYS": "0",
                }
                with mock.patch.dict("os.environ", env, clear=False):
                    first = swm._load_priority_scan_plan("2026-06-01")
                    second = swm._load_priority_scan_plan("2026-06-01")

            self.assertEqual(first.get("recommended_pilot_gap_slugs"), slugs[:2])
            self.assertEqual(second.get("recommended_pilot_gap_slugs"), slugs[2:])

    def test_priority_plan_prunes_stuck_no_quote_slug_when_last_success_precedes_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            with mock.patch.object(swm, "DB_PATH", db_path):
                swm.init_db()
                now = datetime.now(timezone.utc).replace(microsecond=0)
                run_date = now.date().isoformat()
                slug = "highest-temperature-in-stuck-city-on-test-date"
                success_text = (now - timedelta(minutes=120)).isoformat()
                fail_1 = (now - timedelta(minutes=20)).isoformat()
                fail_2 = (now - timedelta(minutes=10)).isoformat()
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
                        slug,
                        run_date,
                        "Stuck City",
                        "10C",
                        "NO",
                        "entry",
                        "blocked",
                        0,
                        '["market_tick_stale"]',
                        '["tick stale"]',
                        "{}",
                        fail_2,
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO event_market_ticks(
                        run_date, event_date, slug, outcome, side, best_bid, best_ask, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_date,
                        run_date,
                        slug,
                        "10C",
                        "NO",
                        0.21,
                        0.22,
                        success_text,
                    ),
                )
                for created_at in (fail_1, fail_2):
                    cur.execute(
                        """
                        INSERT INTO scan_skips(slug, reason, created_at)
                        VALUES (?, ?, ?)
                        """,
                        (
                            slug,
                            "market_refresh_failed no_orderbook_quotes: synthetic test",
                            created_at,
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
                        "PM_SCAN_PRIORITY_MARKET_TICK_STALE_WINDOW_MIN": "180",
                        "PM_SCAN_PRIORITY_NO_QUOTE_STUCK_WINDOW_MIN": "180",
                        "PM_SCAN_PRIORITY_NO_QUOTE_STUCK_MIN_FAILS": "2",
                        "PM_SCAN_PRIORITY_TOPN": "10",
                        "PM_SCAN_PRIORITY_LOOKAHEAD_DAYS": "0",
                    },
                    clear=False,
                ):
                    plan = swm._load_priority_scan_plan(run_date)

            self.assertIn(slug, list(plan.get("market_tick_stale_slugs") or []))
            self.assertIn(slug, list(plan.get("fixed_pruned_no_quote_slugs") or []))
            self.assertNotIn(slug, list(plan.get("fixed_slugs") or []))

    def test_priority_plan_keeps_no_quote_slug_when_success_arrives_after_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            with mock.patch.object(swm, "DB_PATH", db_path):
                swm.init_db()
                now = datetime.now(timezone.utc).replace(microsecond=0)
                run_date = now.date().isoformat()
                slug = "highest-temperature-in-recovered-city-on-test-date"
                fail_1 = (now - timedelta(minutes=20)).isoformat()
                fail_2 = (now - timedelta(minutes=10)).isoformat()
                success_text = (now - timedelta(minutes=5)).isoformat()
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
                        slug,
                        run_date,
                        "Recovered City",
                        "11C",
                        "YES",
                        "entry",
                        "blocked",
                        0,
                        '["market_tick_stale"]',
                        '["tick stale"]',
                        "{}",
                        success_text,
                    ),
                )
                for created_at in (fail_1, fail_2):
                    cur.execute(
                        """
                        INSERT INTO scan_skips(slug, reason, created_at)
                        VALUES (?, ?, ?)
                        """,
                        (
                            slug,
                            "market_refresh_failed no_orderbook_quotes: synthetic test",
                            created_at,
                        ),
                    )
                cur.execute(
                    """
                    INSERT INTO event_market_ticks(
                        run_date, event_date, slug, outcome, side, best_bid, best_ask, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_date,
                        run_date,
                        slug,
                        "11C",
                        "YES",
                        0.34,
                        0.35,
                        success_text,
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
                        "PM_SCAN_PRIORITY_MARKET_TICK_STALE_WINDOW_MIN": "180",
                        "PM_SCAN_PRIORITY_NO_QUOTE_STUCK_WINDOW_MIN": "180",
                        "PM_SCAN_PRIORITY_NO_QUOTE_STUCK_MIN_FAILS": "2",
                        "PM_SCAN_PRIORITY_TOPN": "10",
                        "PM_SCAN_PRIORITY_LOOKAHEAD_DAYS": "0",
                    },
                    clear=False,
                ):
                    plan = swm._load_priority_scan_plan(run_date)

            self.assertIn(slug, list(plan.get("market_tick_stale_slugs") or []))
            self.assertNotIn(slug, list(plan.get("fixed_pruned_no_quote_slugs") or []))
            self.assertIn(slug, list(plan.get("fixed_slugs") or []))

    def test_priority_plan_keeps_protected_position_and_open_order_when_no_quote_stuck(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            with mock.patch.object(swm, "DB_PATH", db_path):
                swm.init_db()
                now = datetime.now(timezone.utc).replace(microsecond=0)
                run_date = now.date().isoformat()
                position_slug = "highest-temperature-in-position-city-on-test-date"
                open_order_slug = "highest-temperature-in-open-order-city-on-test-date"
                success_text = (now - timedelta(minutes=120)).isoformat()
                fail_1 = (now - timedelta(minutes=20)).isoformat()
                fail_2 = (now - timedelta(minutes=10)).isoformat()
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO edge_trades(
                        slug, event_date, outcome, side, state, position_size, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        position_slug,
                        run_date,
                        "12C",
                        "NO",
                        "entry",
                        1.25,
                        now.isoformat(),
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO edge_trades(
                        slug, event_date, outcome, side, state, last_status, position_size, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        open_order_slug,
                        run_date,
                        "13C",
                        "YES",
                        "entry_pending",
                        "open_order_pending",
                        0.0,
                        now.isoformat(),
                    ),
                )
                for slug, outcome, side in (
                    (position_slug, "12C", "NO"),
                    (open_order_slug, "13C", "YES"),
                ):
                    cur.execute(
                        """
                        INSERT INTO event_market_ticks(
                            run_date, event_date, slug, outcome, side, best_bid, best_ask, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run_date,
                            run_date,
                            slug,
                            outcome,
                            side,
                            0.25,
                            0.26,
                            success_text,
                        ),
                    )
                    for created_at in (fail_1, fail_2):
                        cur.execute(
                            """
                            INSERT INTO scan_skips(slug, reason, created_at)
                            VALUES (?, ?, ?)
                            """,
                            (
                                slug,
                                "market_refresh_failed no_orderbook_quotes: synthetic test",
                                created_at,
                            ),
                        )
                conn.commit()
                conn.close()

                with mock.patch.dict(
                    "os.environ",
                    {
                        "PM_SCAN_PRIORITY_ENABLED": "1",
                        "PM_SCAN_PRIORITY_INCLUDE_OPEN_ORDERS": "1",
                        "PM_SCAN_PRIORITY_NO_QUOTE_STUCK_WINDOW_MIN": "180",
                        "PM_SCAN_PRIORITY_NO_QUOTE_STUCK_MIN_FAILS": "2",
                        "PM_SCAN_PRIORITY_TOPN": "10",
                        "PM_SCAN_PRIORITY_LOOKAHEAD_DAYS": "0",
                    },
                    clear=False,
                ):
                    plan = swm._load_priority_scan_plan(run_date)

            fixed_slugs = list(plan.get("fixed_slugs") or [])
            pruned_slugs = list(plan.get("fixed_pruned_no_quote_slugs") or [])
            self.assertIn(position_slug, list(plan.get("position_slugs") or []))
            self.assertIn(open_order_slug, list(plan.get("open_order_slugs") or []))
            self.assertIn(position_slug, fixed_slugs)
            self.assertIn(open_order_slug, fixed_slugs)
            self.assertNotIn(position_slug, pruned_slugs)
            self.assertNotIn(open_order_slug, pruned_slugs)

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

    def test_recent_market_tick_stale_decision_outranks_global_stale_tail_when_capped(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            with mock.patch.object(swm, "DB_PATH", db_path):
                swm.init_db()
                now = datetime.now(timezone.utc)
                now_text = now.isoformat()
                decision_stale_text = (now - timedelta(hours=2)).isoformat()
                global_stale_text = (now - timedelta(hours=6)).isoformat()
                run_date = now.date().isoformat()
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                for slug, tick_created_at in [
                    ("highest-temperature-in-decision-stale-city-on-test-date", decision_stale_text),
                    ("highest-temperature-in-global-stale-city-on-test-date", global_stale_text),
                ]:
                    cur.execute(
                        """
                        INSERT INTO event_market_ticks(
                            run_date, event_date, slug, outcome, side,
                            best_bid, best_ask, mid, spread,
                            bid_depth_topN, ask_depth_topN, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run_date,
                            run_date,
                            slug,
                            "10C",
                            "YES",
                            0.39,
                            0.40,
                            0.395,
                            0.01,
                            100.0,
                            100.0,
                            tick_created_at,
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
                        1,
                        run_date,
                        "highest-temperature-in-decision-stale-city-on-test-date",
                        run_date,
                        "Decision Stale City",
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
                conn.commit()
                conn.close()

                with mock.patch.dict(
                    "os.environ",
                    {
                        "PM_SCAN_PRIORITY_ENABLED": "1",
                        "PM_SCAN_PRIORITY_INCLUDE_ENTRY_OK": "0",
                        "PM_SCAN_PRIORITY_INCLUDE_MARKET_TICK_STALE": "1",
                        "PM_SCAN_PRIORITY_MARKET_TICK_STALE_TOPN": "1",
                        "PM_SCAN_PRIORITY_MARKET_TICK_STALE_MIN_AGE_SEC": "180",
                        "PM_SCAN_PRIORITY_MARKET_TICK_STALE_WINDOW_MIN": "30",
                        "PM_SCAN_PRIORITY_TOPN": "10",
                        "PM_SCAN_PRIORITY_LOOKAHEAD_DAYS": "0",
                    },
                    clear=False,
                ):
                    plan = swm._load_priority_scan_plan(run_date)

            tick_stale_slugs = list(plan.get("market_tick_stale_slugs") or [])
            self.assertEqual(
                tick_stale_slugs,
                ["highest-temperature-in-decision-stale-city-on-test-date"],
            )

    def test_latest_completed_run_market_tick_stale_outranks_older_window_hits(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            with mock.patch.object(swm, "DB_PATH", db_path):
                swm.init_db()
                now = datetime.now(timezone.utc)
                run_date = now.date().isoformat()
                older_run_started = (now - timedelta(minutes=10)).isoformat()
                older_run_ended = (now - timedelta(minutes=9)).isoformat()
                latest_run_started = (now - timedelta(minutes=2)).isoformat()
                latest_run_ended = (now - timedelta(minutes=1)).isoformat()
                tick_text = (now - timedelta(hours=2)).isoformat()
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                cur.executemany(
                    """
                    INSERT INTO auto_trade_runs(id, run_date, started_at, ended_at, ok, processed, skipped, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (1, run_date, older_run_started, older_run_ended, 1, 0, 0, older_run_started),
                        (2, run_date, latest_run_started, latest_run_ended, 1, 0, 0, latest_run_started),
                    ],
                )
                for slug in (
                    "highest-temperature-in-old-window-city-on-test-date",
                    "highest-temperature-in-latest-run-city-on-test-date",
                ):
                    cur.execute(
                        """
                        INSERT INTO event_market_ticks(
                            run_date, event_date, slug, outcome, side,
                            best_bid, best_ask, mid, spread,
                            bid_depth_topN, ask_depth_topN, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run_date,
                            run_date,
                            slug,
                            "10C",
                            "YES",
                            0.39,
                            0.40,
                            0.395,
                            0.01,
                            100.0,
                            100.0,
                            tick_text,
                        ),
                    )
                for idx in range(5):
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
                            "highest-temperature-in-old-window-city-on-test-date",
                            run_date,
                            "Old Window City",
                            f"10C-{idx}",
                            "YES",
                            "entry",
                            "blocked",
                            0,
                            '["market_tick_stale"]',
                            '["tick stale"]',
                            "{}",
                            older_run_started,
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
                        "highest-temperature-in-latest-run-city-on-test-date",
                        run_date,
                        "Latest Run City",
                        "10C",
                        "YES",
                        "entry",
                        "blocked",
                        0,
                        '["market_tick_stale"]',
                        '["tick stale"]',
                        "{}",
                        latest_run_started,
                    ),
                )
                conn.commit()
                conn.close()

                with mock.patch.dict(
                    "os.environ",
                    {
                        "PM_SCAN_PRIORITY_ENABLED": "1",
                        "PM_SCAN_PRIORITY_INCLUDE_ENTRY_OK": "0",
                        "PM_SCAN_PRIORITY_INCLUDE_MARKET_TICK_STALE": "1",
                        "PM_SCAN_PRIORITY_MARKET_TICK_STALE_TOPN": "1",
                        "PM_SCAN_PRIORITY_MARKET_TICK_STALE_MIN_AGE_SEC": "180",
                        "PM_SCAN_PRIORITY_MARKET_TICK_STALE_WINDOW_MIN": "30",
                        "PM_SCAN_PRIORITY_TOPN": "10",
                        "PM_SCAN_PRIORITY_LOOKAHEAD_DAYS": "0",
                    },
                    clear=False,
                ):
                    plan = swm._load_priority_scan_plan(run_date)

            tick_stale_slugs = list(plan.get("market_tick_stale_slugs") or [])
            self.assertEqual(
                tick_stale_slugs,
                ["highest-temperature-in-latest-run-city-on-test-date"],
            )

    def test_market_tick_stale_priority_orders_tradeable_profit_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            with mock.patch.object(swm, "DB_PATH", db_path):
                swm.init_db()
                now = datetime.now(timezone.utc)
                run_date = now.date().isoformat()
                event_date = run_date
                latest_run_started = (now - timedelta(minutes=2)).isoformat()
                latest_run_ended = (now - timedelta(minutes=1)).isoformat()
                tick_text = (now - timedelta(hours=2)).isoformat()
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO auto_trade_runs(id, run_date, started_at, ended_at, ok, processed, skipped, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (1, run_date, latest_run_started, latest_run_ended, 1, 0, 0, latest_run_started),
                )
                cases = [
                    (
                        "highest-temperature-in-low-profit-city-on-test-date",
                        5,
                        {"edge_tradeability_pass": True, "edge_tradeability_profit_value": 0.01},
                    ),
                    (
                        "highest-temperature-in-high-profit-city-on-test-date",
                        1,
                        {"edge_tradeability_pass": True, "edge_tradeability_profit_value": 0.12},
                    ),
                    (
                        "highest-temperature-in-blocked-profit-city-on-test-date",
                        1,
                        {"edge_tradeability_pass": False, "edge_tradeability_profit_value": 0.50},
                    ),
                ]
                for slug, hits, details in cases:
                    cur.execute(
                        """
                        INSERT INTO event_market_ticks(
                            run_date, event_date, slug, outcome, side,
                            best_bid, best_ask, mid, spread,
                            bid_depth_topN, ask_depth_topN, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run_date,
                            event_date,
                            slug,
                            "10C",
                            "YES",
                            0.39,
                            0.40,
                            0.395,
                            0.01,
                            100.0,
                            100.0,
                            tick_text,
                        ),
                    )
                    for idx in range(hits):
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
                                slug,
                                event_date,
                                slug,
                                f"10C-{idx}",
                                "YES",
                                "entry",
                                "blocked",
                                0,
                                '["market_tick_stale"]',
                                '["tick stale"]',
                                json.dumps(details),
                                latest_run_started,
                            ),
                        )
                conn.commit()
                conn.close()

                with mock.patch.dict(
                    "os.environ",
                    {
                        "PM_SCAN_PRIORITY_ENABLED": "1",
                        "PM_SCAN_PRIORITY_INCLUDE_ENTRY_OK": "0",
                        "PM_SCAN_PRIORITY_INCLUDE_MARKET_TICK_STALE": "1",
                        "PM_SCAN_PRIORITY_MARKET_TICK_STALE_TOPN": "2",
                        "PM_SCAN_PRIORITY_MARKET_TICK_STALE_MIN_AGE_SEC": "180",
                        "PM_SCAN_PRIORITY_MARKET_TICK_STALE_WINDOW_MIN": "30",
                        "PM_SCAN_PRIORITY_TOPN": "10",
                        "PM_SCAN_PRIORITY_LOOKAHEAD_DAYS": "0",
                    },
                    clear=False,
                ):
                    plan = swm._load_priority_scan_plan(run_date)

            tick_stale_slugs = list(plan.get("market_tick_stale_slugs") or [])
            self.assertEqual(
                tick_stale_slugs,
                [
                    "highest-temperature-in-high-profit-city-on-test-date",
                    "highest-temperature-in-low-profit-city-on-test-date",
                ],
            )

    def test_latest_market_tick_stale_blocker_outranks_entry_ok_stale_when_capped(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            with mock.patch.object(swm, "DB_PATH", db_path):
                swm.init_db()
                now = datetime.now(timezone.utc)
                run_date = now.date().isoformat()
                event_date = run_date
                stale_tick_text = (now - timedelta(hours=2)).isoformat()
                latest_run_started = (now - timedelta(minutes=2)).isoformat()
                latest_run_ended = (now - timedelta(minutes=1)).isoformat()
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO auto_trade_runs(id, run_date, started_at, ended_at, ok, processed, skipped, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (1, run_date, latest_run_started, latest_run_ended, 1, 0, 0, latest_run_started),
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
                for slug in (
                    "highest-temperature-in-entry-ok-city-on-test-date",
                    "highest-temperature-in-latest-blocker-city-on-test-date",
                ):
                    cur.execute(
                        """
                        INSERT INTO event_market_ticks(
                            run_date, event_date, slug, outcome, side,
                            best_bid, best_ask, mid, spread,
                            bid_depth_topN, ask_depth_topN, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run_date,
                            event_date,
                            slug,
                            "10C",
                            "YES",
                            0.39,
                            0.40,
                            0.395,
                            0.01,
                            100.0,
                            100.0,
                            stale_tick_text,
                        ),
                    )
                fact_json = {
                    "run_date": run_date,
                    "event_date": event_date,
                    "market_top_label": "25C+",
                    "auto_trade_ssot": {
                        "main_candidate": {
                            "outcome": "24C",
                            "side": "NO",
                            "entry_ok": True,
                            "edge_score": 15.0,
                            "ask": 0.62,
                            "order_price": 0.62,
                        },
                        "main_entry_ok": True,
                    },
                }
                cur.execute(
                    """
                    INSERT INTO event_fact_history(
                        run_date, slug, event_date, fact_as_of,
                        composer_version, best_side, best_outcome, best_edge_score, fact_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_date,
                        "highest-temperature-in-entry-ok-city-on-test-date",
                        event_date,
                        latest_run_started,
                        "test",
                        "NO",
                        "24C",
                        15.0,
                        json.dumps(fact_json),
                        latest_run_started,
                    ),
                )
                fact_id = cur.lastrowid
                cur.execute(
                    """
                    INSERT INTO event_fact_latest(slug, event_date, fact_id, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        "highest-temperature-in-entry-ok-city-on-test-date",
                        event_date,
                        fact_id,
                        latest_run_started,
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
                        1,
                        run_date,
                        "highest-temperature-in-latest-blocker-city-on-test-date",
                        event_date,
                        "Latest Blocker City",
                        "10C",
                        "YES",
                        "entry",
                        "blocked",
                        0,
                        '["market_tick_stale"]',
                        '["tick stale"]',
                        "{}",
                        latest_run_started,
                    ),
                )
                conn.commit()
                conn.close()

                with mock.patch.dict(
                    "os.environ",
                    {
                        "PM_SCAN_PRIORITY_ENABLED": "1",
                        "PM_SCAN_PRIORITY_INCLUDE_ENTRY_OK": "1",
                        "PM_SCAN_PRIORITY_INCLUDE_MARKET_TICK_STALE": "1",
                        "PM_SCAN_PRIORITY_MARKET_TICK_STALE_TOPN": "1",
                        "PM_SCAN_PRIORITY_MARKET_TICK_STALE_MIN_AGE_SEC": "180",
                        "PM_SCAN_PRIORITY_ENTRY_OK_TOPN": "10",
                        "PM_SCAN_PRIORITY_TOPN": "10",
                        "PM_SCAN_PRIORITY_LOOKAHEAD_DAYS": "0",
                    },
                    clear=False,
                ):
                    plan = swm._load_priority_scan_plan(run_date)

            tick_stale_slugs = list(plan.get("market_tick_stale_slugs") or [])
            self.assertEqual(
                tick_stale_slugs,
                ["highest-temperature-in-latest-blocker-city-on-test-date"],
            )

    def test_market_refresh_fast_mode_prefers_recent_decision_run_blockers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            with mock.patch.object(swm, "DB_PATH", db_path):
                swm.init_db()
                now = datetime.now(timezone.utc)
                run_date = now.date().isoformat()
                older_started = (now - timedelta(minutes=20)).isoformat()
                older_ended = (now - timedelta(minutes=19)).isoformat()
                recent_text = (now - timedelta(seconds=20)).isoformat()
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO auto_trade_runs(id, run_date, started_at, ended_at, ok, processed, skipped, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (1, run_date, older_started, older_ended, 1, 0, 0, older_started),
                )
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
                        "highest-temperature-in-old-completed-run-on-test-date",
                        run_date,
                        "Old Completed Run",
                        "10C",
                        "YES",
                        "entry",
                        "blocked",
                        0,
                        '["market_tick_stale"]',
                        '["tick stale"]',
                        "{}",
                        older_started,
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
                        "highest-temperature-in-recent-live-run-on-test-date",
                        run_date,
                        "Recent Live Run",
                        "11C",
                        "NO",
                        "entry",
                        "blocked",
                        0,
                        '["market_tick_stale"]',
                        '["tick stale"]',
                        "{}",
                        recent_text,
                    ),
                )
                conn.commit()
                conn.close()

                with mock.patch.dict(
                    "os.environ",
                    {
                        "PM_SCAN_PRIORITY_ENABLED": "1",
                        "PM_SCAN_PRIORITY_INCLUDE_ENTRY_OK": "0",
                        "PM_SCAN_PRIORITY_INCLUDE_Q_MISSING": "0",
                        "PM_SCAN_PRIORITY_INCLUDE_MARKET_TICK_STALE": "1",
                        "PM_SCAN_PRIORITY_MARKET_TICK_STALE_TOPN": "1",
                        "PM_SCAN_PRIORITY_TOPN": "1",
                        "PM_SCAN_PRIORITY_LOOKAHEAD_DAYS": "0",
                    },
                    clear=False,
                ):
                    plan = swm._load_priority_scan_plan(
                        run_date,
                        args=SimpleNamespace(market_refresh_only=True),
                        max_events_per_run=1,
                    )

            self.assertEqual(
                list(plan.get("market_tick_stale_slugs") or []),
                ["highest-temperature-in-recent-live-run-on-test-date"],
            )

    def test_market_refresh_fast_mode_skips_nonfixed_when_stale_budget_is_full(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            with mock.patch.object(swm, "DB_PATH", db_path):
                swm.init_db()
                now = datetime.now(timezone.utc)
                run_date = now.date().isoformat()
                now_text = now.isoformat()
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                for slug in (
                    "highest-temperature-in-stale-a-on-test-date",
                    "highest-temperature-in-stale-b-on-test-date",
                ):
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
                            slug,
                            run_date,
                            slug,
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
                        3,
                        run_date,
                        "highest-temperature-in-q-missing-on-test-date",
                        run_date,
                        "Q Missing City",
                        "12C",
                        "YES",
                        "entry",
                        "blocked",
                        0,
                        '["q_missing"]',
                        '["q missing"]',
                        "{}",
                        now_text,
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO event_runs(run_date, event_date, slug, title, suggestions_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_date,
                        run_date,
                        "highest-temperature-in-candidate-on-test-date",
                        "Candidate City",
                        json.dumps([{"edge_score": 12.0}]),
                        now_text,
                    ),
                )
                conn.commit()
                conn.close()

                with mock.patch.dict(
                    "os.environ",
                    {
                        "PM_SCAN_PRIORITY_ENABLED": "1",
                        "PM_SCAN_PRIORITY_INCLUDE_ENTRY_OK": "0",
                        "PM_SCAN_PRIORITY_INCLUDE_Q_MISSING": "1",
                        "PM_SCAN_PRIORITY_INCLUDE_MARKET_TICK_STALE": "1",
                        "PM_SCAN_PRIORITY_MARKET_TICK_STALE_TOPN": "2",
                        "PM_SCAN_PRIORITY_Q_MISSING_TOPN": "2",
                        "PM_SCAN_PRIORITY_TOPN": "2",
                        "PM_SCAN_PRIORITY_LOOKAHEAD_DAYS": "0",
                    },
                    clear=False,
                ):
                    plan = swm._load_priority_scan_plan(
                        run_date,
                        args=SimpleNamespace(market_refresh_only=True),
                        max_events_per_run=2,
                    )

            self.assertEqual(
                list(plan.get("market_tick_stale_slugs") or []),
                [
                    "highest-temperature-in-stale-a-on-test-date",
                    "highest-temperature-in-stale-b-on-test-date",
                ],
            )
            self.assertEqual(list(plan.get("candidate_slugs") or []), [])
            self.assertEqual(list(plan.get("q_missing_slugs") or []), [])

    def test_priority_plan_promotes_stale_entry_ok_tick_into_fixed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            with mock.patch.object(swm, "DB_PATH", db_path):
                swm.init_db()
                now = datetime.now(timezone.utc)
                now_text = now.isoformat()
                stale_tick_text = (now - timedelta(hours=2)).isoformat()
                fresh_tick_text = (now - timedelta(seconds=30)).isoformat()
                run_date = now.date().isoformat()
                event_date = (now.date() + timedelta(days=1)).isoformat()
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
                for slug, created_at, tick_created_at in [
                    (
                        "highest-temperature-in-stale-entry-ok-city-on-test-date",
                        now_text,
                        stale_tick_text,
                    ),
                    (
                        "highest-temperature-in-fresh-entry-ok-city-on-test-date",
                        now_text,
                        fresh_tick_text,
                    ),
                ]:
                    fact_json = {
                        "run_date": run_date,
                        "event_date": event_date,
                        "market_top_label": "25C+",
                        "auto_trade_ssot": {
                            "main_candidate": {
                                "outcome": "24C",
                                "side": "NO",
                                "entry_ok": True,
                                "edge_score": 15.0,
                                "ask": 0.62,
                                "order_price": 0.62,
                            },
                            "main_entry_ok": True,
                        },
                    }
                    cur.execute(
                        """
                        INSERT INTO event_fact_history(
                            run_date, slug, event_date, fact_as_of,
                            composer_version, best_side, best_outcome, best_edge_score, fact_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run_date,
                            slug,
                            event_date,
                            created_at,
                            "test",
                            "NO",
                            "24C",
                            15.0,
                            json.dumps(fact_json),
                            created_at,
                        ),
                    )
                    fact_id = cur.lastrowid
                    cur.execute(
                        """
                        INSERT INTO event_fact_latest(slug, event_date, fact_id, updated_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (slug, event_date, fact_id, created_at),
                    )
                    cur.execute(
                        """
                        INSERT INTO event_market_ticks(
                            run_date, event_date, slug, outcome, side,
                            best_bid, best_ask, mid, spread,
                            bid_depth_topN, ask_depth_topN, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run_date,
                            event_date,
                            slug,
                            "24C",
                            "NO",
                            0.35,
                            0.36,
                            0.355,
                            0.01,
                            100.0,
                            100.0,
                            tick_created_at,
                        ),
                    )
                conn.commit()
                conn.close()

                with mock.patch.dict(
                    "os.environ",
                    {
                        "PM_SCAN_PRIORITY_ENABLED": "1",
                        "PM_SCAN_PRIORITY_INCLUDE_ENTRY_OK": "1",
                        "PM_SCAN_PRIORITY_INCLUDE_MARKET_TICK_STALE": "1",
                        "PM_SCAN_PRIORITY_MARKET_TICK_STALE_TOPN": "3",
                        "PM_SCAN_PRIORITY_MARKET_TICK_STALE_MIN_AGE_SEC": "180",
                        "PM_SCAN_PRIORITY_ENTRY_OK_TOPN": "10",
                        "PM_SCAN_PRIORITY_TOPN": "10",
                        "PM_SCAN_PRIORITY_LOOKAHEAD_DAYS": "1",
                    },
                    clear=False,
                ):
                    plan = swm._load_priority_scan_plan(run_date)

            tick_stale_slugs = list(plan.get("market_tick_stale_slugs") or [])
            fixed_slugs = list(plan.get("fixed_slugs") or [])
            self.assertIn("highest-temperature-in-stale-entry-ok-city-on-test-date", tick_stale_slugs)
            self.assertIn("highest-temperature-in-stale-entry-ok-city-on-test-date", fixed_slugs)
            self.assertNotIn("highest-temperature-in-fresh-entry-ok-city-on-test-date", tick_stale_slugs)

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
                    "PM_MARKET_REFRESH_COMPOSE_SCORE": "0",
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

    def test_priority_plan_keeps_fact_tail_when_fixed_saturated(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            with mock.patch.object(swm, "DB_PATH", db_path):
                swm.init_db()
                now = datetime.now(timezone.utc).replace(microsecond=0)
                run_date = now.date().isoformat()
                previous_date = (now.date() - timedelta(days=1)).isoformat()
                fact_slug = "highest-temperature-in-fact-tail-city-on-test-date"
                pending_slug = "highest-temperature-in-pending-saturated-city-on-test-date"
                stale_fact_as_of = (now - timedelta(hours=2)).isoformat()
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                cur.execute(
                    """
                    CREATE TABLE event_fact_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_date TEXT,
                        event_date TEXT,
                        slug TEXT,
                        fact_as_of TEXT,
                        created_at TEXT
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE event_fact_latest (
                        run_date TEXT,
                        event_date TEXT,
                        slug TEXT,
                        fact_id INTEGER,
                        updated_at TEXT,
                        PRIMARY KEY (run_date, event_date, slug)
                    )
                    """
                )
                cur.execute(
                    """
                    INSERT INTO event_fact_history(
                        run_date, event_date, slug, fact_as_of, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (run_date, run_date, fact_slug, stale_fact_as_of, stale_fact_as_of),
                )
                fact_id = int(cur.lastrowid)
                cur.execute(
                    """
                    INSERT INTO event_fact_latest(run_date, event_date, slug, fact_id, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (run_date, run_date, fact_slug, fact_id, stale_fact_as_of),
                )
                cur.execute(
                    """
                    INSERT INTO event_runs(
                        run_date, event_date, slug, title, event_end_utc,
                        settlement_actual, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_date,
                        previous_date,
                        pending_slug,
                        "Pending Saturated City",
                        (now - timedelta(hours=6)).isoformat(),
                        None,
                        (now - timedelta(hours=7)).isoformat(),
                    ),
                )
                conn.commit()
                conn.close()

                args = SimpleNamespace(market_refresh_only=True)
                with mock.patch.dict(
                    "os.environ",
                    {
                        "PM_ARCHIVE_WRITE_SCOPE": "",
                        "PM_SCAN_PRIORITY_ENABLED": "1",
                        "PM_SCAN_PRIORITY_INCLUDE_FACT_TAIL": "1",
                        "PM_SCAN_PRIORITY_FACT_TAIL_TOPN": "10",
                        "PM_SCAN_PRIORITY_FACT_TAIL_MIN_AGE_SEC": "60",
                        "PM_SCAN_PRIORITY_INCLUDE_SETTLEMENT_PENDING": "1",
                        "PM_SCAN_PRIORITY_SETTLEMENT_PENDING_TOPN": "10",
                        "PM_SCAN_PRIORITY_SETTLEMENT_PENDING_LOOKBACK_DAYS": "7",
                        "PM_SCAN_PRIORITY_LOOKAHEAD_DAYS": "0",
                    },
                    clear=False,
                ):
                    plan = swm._load_priority_scan_plan(
                        run_date,
                        args=args,
                        max_events_per_run=1,
                    )

            self.assertIn(fact_slug, list(plan.get("fact_tail_slugs") or []))
            self.assertIn(fact_slug, list(plan.get("tail_slugs") or []))
            self.assertIn(fact_slug, list(plan.get("fixed_slugs") or []))
            self.assertIn(fact_slug, list(plan.get("cap_protected_slugs") or []))
            self.assertIn(pending_slug, list(plan.get("fixed_slugs") or []))

    def test_model_refresh_cap_protects_current_scope_slug_from_fixed_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            db_path = tmp_path / "archive.db"
            data_dir = tmp_path / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            current_slug = "highest-temperature-in-chengdu-on-june-18-2026"
            old_slug_a = "highest-temperature-in-chengdu-on-june-17-2026"
            old_slug_b = "highest-temperature-in-chengdu-on-june-13-2026"
            run_date = "2026-06-18"
            priority_plan = {
                "enabled": True,
                "ordered_slugs": [current_slug, old_slug_a, old_slug_b],
                "fixed_slugs": [current_slug, old_slug_a, old_slug_b],
                "candidate_slugs": [],
                "settlement_pending_slugs": [old_slug_a, old_slug_b],
                "cap_protected_slugs": [current_slug],
                "fixed_pruned_no_quote_slugs": [],
                "position_slugs": [],
                "open_order_slugs": [],
                "q_missing_slugs": [],
                "entry_ok_slugs": [],
                "event_stub_by_slug": {},
                "others_per_run": 0,
                "candidates_per_run": 0,
            }
            fake_events = [
                {"slug": current_slug, "title": "Highest temperature in Chengdu on June 18?", "endDate": "2026-06-18T23:59:00Z"},
                {"slug": old_slug_a, "title": "Highest temperature in Chengdu on June 17?", "endDate": "2026-06-17T23:59:00Z"},
                {"slug": old_slug_b, "title": "Highest temperature in Chengdu on June 13?", "endDate": "2026-06-13T23:59:00Z"},
            ]
            (data_dir / "model_scan.fixed_offset").write_text("1", encoding="utf-8")

            def fake_analyze(event_url, event_date, lat, lon, tz, args, event_hint=None):
                slug = str(event_url).rstrip("/").split("/")[-1]
                return {
                    "slug": slug,
                    "title": (event_hint or {}).get("title") or slug,
                    "market_var": "temperature_2m_max",
                    "market_unit": "C",
                    "resolution_source": "https://www.wunderground.com/history/daily/cn/chengdu/ZUUU",
                    "rows": [{"outcome": "30C", "q_entry": 0.5, "q_calibrated": 0.5}],
                    "models": ["test-model"],
                }

            args = [
                "scan_weather_market.py",
                "--scan-weather-today",
                "--model-refresh-only",
                "--no-auto-trade",
                "--no-auto-diagnose",
                "--days-ahead",
                "0",
                "--tz",
                "America/New_York",
            ]
            with (
                mock.patch.object(swm, "DB_PATH", db_path),
                mock.patch.object(swm, "DATA_DIR", data_dir),
                mock.patch("scan_weather_market.today_date_str", return_value=run_date),
                mock.patch("scan_weather_market.fetch_weather_events", return_value=(fake_events, [])),
                mock.patch("scan_weather_market._load_priority_scan_plan", return_value=priority_plan),
                mock.patch("scan_weather_market.fetch_gamma_event_by_slug", return_value=None),
                mock.patch("scan_weather_market.resolve_location_for_event", return_value=(30.66, 104.06, "Asia/Shanghai", "Chengdu")),
                mock.patch("scan_weather_market.analyze_event", side_effect=fake_analyze) as analyze_mock,
                mock.patch(
                    "scan_weather_market.refresh_settlement_actual_for_pending_event",
                    return_value=(True, "settlement_actual_unavailable", None),
                ) as settlement_mock,
                mock.patch("scan_weather_market.save_model_snapshot"),
                mock.patch("scan_weather_market._save_event_run_from_market_refresh"),
                mock.patch("scan_weather_market.to_jsonable_result", return_value={}),
                mock.patch("scan_weather_market.compose_score_snapshot_with_market_bootstrap", return_value=(True, None)),
                mock.patch.dict(
                    "os.environ",
                    {
                        "PM_ARCHIVE_WRITE_SCOPE": "",
                        "PM_SCAN_PRIORITY_ENABLED": "1",
                        "PM_SCAN_FETCH_EVENT_DETAIL": "0",
                        "PM_SCAN_RULES_FIRST": "0",
                        "PM_SCAN_MAX_EVENTS_PER_RUN": "2",
                        "PM_SCAN_MAX_MATCHED_PER_RUN": "2",
                        "PM_SCAN_PRIORITY_ROTATE_FIXED_WHEN_CAPPED": "1",
                    },
                    clear=False,
                ),
                mock.patch("sys.argv", args),
            ):
                swm.main()

            called_slugs = [
                str(call.args[0]).rstrip("/").split("/")[-1]
                for call in analyze_mock.call_args_list
            ]
            self.assertIn(current_slug, called_slugs)
            settlement_slugs = [call.args[2] for call in settlement_mock.call_args_list]
            self.assertEqual(settlement_slugs, [old_slug_b])

    def test_model_refresh_cap_prefers_future_missing_over_old_protected_tick(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            db_path = tmp_path / "archive.db"
            data_dir = tmp_path / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            run_date = "2026-06-22"
            old_slug = "highest-temperature-in-chengdu-on-june-22-2026"
            future_slug = "highest-temperature-in-chengdu-on-june-24-2026"
            priority_plan = {
                "enabled": True,
                "ordered_slugs": [old_slug, future_slug],
                "fixed_slugs": [old_slug, future_slug],
                "candidate_slugs": [],
                "settlement_pending_slugs": [],
                "cap_protected_slugs": [future_slug, old_slug],
                "fact_tail_slugs": [],
                "fixed_pruned_no_quote_slugs": [],
                "position_slugs": [old_slug],
                "open_order_slugs": [],
                "q_missing_slugs": [],
                "entry_ok_slugs": [],
                "event_stub_by_slug": {},
                "others_per_run": 0,
                "candidates_per_run": 0,
            }
            fake_events = [
                {
                    "slug": old_slug,
                    "title": "Highest temperature in Chengdu on June 22?",
                    "endDate": "2026-06-22T23:59:00Z",
                },
                {
                    "slug": future_slug,
                    "title": "Highest temperature in Chengdu on June 24?",
                    "endDate": "2026-06-24T23:59:00Z",
                },
            ]

            def fake_analyze(event_url, event_date, lat, lon, tz, args, event_hint=None):
                slug = str(event_url).rstrip("/").split("/")[-1]
                return {
                    "slug": slug,
                    "title": (event_hint or {}).get("title") or slug,
                    "market_var": "temperature_2m_max",
                    "market_unit": "C",
                    "resolution_source": "https://www.wunderground.com/history/daily/cn/chengdu/ZUUU",
                    "rows": [{"outcome": "30C", "q_entry": 0.5, "q_calibrated": 0.5}],
                    "models": ["test-model"],
                }

            args = [
                "scan_weather_market.py",
                "--scan-weather-today",
                "--model-refresh-only",
                "--no-auto-trade",
                "--no-auto-diagnose",
                "--days-ahead",
                "2",
                "--tz",
                "America/New_York",
            ]
            with (
                mock.patch.object(swm, "DB_PATH", db_path),
                mock.patch.object(swm, "DATA_DIR", data_dir),
                mock.patch("scan_weather_market.today_date_str", return_value=run_date),
                mock.patch("scan_weather_market.fetch_weather_events", return_value=(fake_events, [])),
                mock.patch("scan_weather_market._load_priority_scan_plan", return_value=priority_plan),
                mock.patch("scan_weather_market.fetch_gamma_event_by_slug", return_value=None),
                mock.patch(
                    "scan_weather_market.resolve_location_for_event",
                    return_value=(30.66, 104.06, "Asia/Shanghai", "Chengdu"),
                ),
                mock.patch("scan_weather_market.analyze_event", side_effect=fake_analyze) as analyze_mock,
                mock.patch("scan_weather_market.save_model_snapshot"),
                mock.patch("scan_weather_market._save_event_run_from_market_refresh"),
                mock.patch("scan_weather_market.to_jsonable_result", return_value={}),
                mock.patch("scan_weather_market.compose_score_snapshot_with_market_bootstrap", return_value=(True, None)),
                mock.patch.dict(
                    "os.environ",
                    {
                        "PM_ARCHIVE_WRITE_SCOPE": "",
                        "PM_SCAN_PRIORITY_ENABLED": "1",
                        "PM_SCAN_FETCH_EVENT_DETAIL": "0",
                        "PM_SCAN_RULES_FIRST": "0",
                        "PM_SCAN_MAX_EVENTS_PER_RUN": "1",
                        "PM_SCAN_MAX_MATCHED_PER_RUN": "1",
                        "PM_SCAN_PRIORITY_ROTATE_FIXED_WHEN_CAPPED": "1",
                    },
                    clear=False,
                ),
                mock.patch("sys.argv", args),
            ):
                swm.main()

            called_slugs = [
                str(call.args[0]).rstrip("/").split("/")[-1]
                for call in analyze_mock.call_args_list
            ]
            self.assertEqual(called_slugs, [future_slug])

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
                            "ask": 0.95,
                            "order_price": 0.95,
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

    def test_priority_plan_keeps_non_peak_entry_ok_fact(self) -> None:
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
                            "outcome": "24C",
                            "side": "NO",
                            "entry_ok": True,
                            "edge_score": 15.0,
                            "ask": 0.62,
                            "order_price": 0.62,
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
                        "highest-temperature-in-non-peak-city-on-test-date",
                        event_date,
                        now_text,
                        "test",
                        "NO",
                        "24C",
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
                        "highest-temperature-in-non-peak-city-on-test-date",
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
                        "PM_LCB_GLOBAL_PLAIN_NO_PEAK_EXCLUDE_MIN_PRICE": "0.70",
                    },
                    clear=False,
                ):
                    plan = swm._load_priority_scan_plan(run_date)

            entry_ok_slugs = list(plan.get("entry_ok_slugs") or [])
            self.assertIn("highest-temperature-in-non-peak-city-on-test-date", entry_ok_slugs)

    def test_priority_plan_promotes_settlement_pending_event_after_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            with mock.patch.object(swm, "DB_PATH", db_path):
                swm.init_db()
                now = datetime.now(timezone.utc).replace(microsecond=0)
                run_date = now.date().isoformat()
                previous_date = (now.date() - timedelta(days=1)).isoformat()
                pending_end = (now - timedelta(hours=6)).isoformat()
                pending_created = (now - timedelta(hours=8)).isoformat()
                future_end = (now + timedelta(hours=2)).isoformat()
                now_text = now.isoformat()
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                rows = [
                    (
                        run_date,
                        previous_date,
                        "highest-temperature-in-settlement-pending-city-on-test-date",
                        "Settlement Pending City",
                        pending_end,
                        None,
                        pending_created,
                    ),
                    (
                        run_date,
                        previous_date,
                        "highest-temperature-in-settled-city-on-test-date",
                        "Settled City",
                        pending_end,
                        72.0,
                        pending_created,
                    ),
                    (
                        run_date,
                        run_date,
                        "highest-temperature-in-future-end-city-on-test-date",
                        "Future End City",
                        future_end,
                        None,
                        now_text,
                    ),
                ]
                cur.executemany(
                    """
                    INSERT INTO event_runs(
                        run_date, event_date, slug, title, event_end_utc,
                        settlement_actual, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
                conn.commit()
                conn.close()

                with mock.patch.dict(
                    "os.environ",
                    {
                        "PM_SCAN_PRIORITY_ENABLED": "1",
                        "PM_SCAN_PRIORITY_INCLUDE_SETTLEMENT_PENDING": "1",
                        "PM_SCAN_PRIORITY_SETTLEMENT_PENDING_TOPN": "10",
                        "PM_SCAN_PRIORITY_SETTLEMENT_PENDING_LOOKBACK_DAYS": "7",
                        "PM_SCAN_PRIORITY_LOOKAHEAD_DAYS": "0",
                        "PM_SCAN_PRIORITY_TOPN": "10",
                    },
                    clear=False,
                ):
                    plan = swm._load_priority_scan_plan(run_date)

            pending_slug = "highest-temperature-in-settlement-pending-city-on-test-date"
            self.assertEqual(list(plan.get("settlement_pending_slugs") or []), [pending_slug])
            self.assertIn(pending_slug, list(plan.get("fixed_slugs") or []))
            self.assertIn(pending_slug, list(plan.get("ordered_slugs") or []))
            stub = dict((plan.get("event_stub_by_slug") or {}).get(pending_slug) or {})
            self.assertEqual(stub.get("endDate"), pending_end)
            self.assertNotIn(
                "highest-temperature-in-settled-city-on-test-date",
                list(plan.get("settlement_pending_slugs") or []),
            )
            self.assertNotIn(
                "highest-temperature-in-future-end-city-on-test-date",
                list(plan.get("settlement_pending_slugs") or []),
            )

    def test_priority_plan_does_not_reopen_settlement_pending_after_any_settled_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            with mock.patch.object(swm, "DB_PATH", db_path):
                swm.init_db()
                now = datetime.now(timezone.utc).replace(microsecond=0)
                run_date = now.date().isoformat()
                previous_date = (now.date() - timedelta(days=1)).isoformat()
                event_end = (now - timedelta(hours=6)).isoformat()
                settled_created = (now - timedelta(hours=5)).isoformat()
                latest_created = (now - timedelta(hours=4)).isoformat()
                slug = "highest-temperature-in-already-settled-latest-empty-city-on-test-date"
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                cur.executemany(
                    """
                    INSERT INTO event_runs(
                        run_date, event_date, slug, title, event_end_utc,
                        settlement_actual, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (run_date, previous_date, slug, "Already Settled City", event_end, 70.0, settled_created),
                        (run_date, previous_date, slug, "Already Settled City", event_end, None, latest_created),
                    ],
                )
                conn.commit()
                conn.close()

                with mock.patch.dict(
                    "os.environ",
                    {
                        "PM_SCAN_PRIORITY_ENABLED": "1",
                        "PM_SCAN_PRIORITY_INCLUDE_SETTLEMENT_PENDING": "1",
                        "PM_SCAN_PRIORITY_SETTLEMENT_PENDING_TOPN": "10",
                        "PM_SCAN_PRIORITY_SETTLEMENT_PENDING_LOOKBACK_DAYS": "7",
                        "PM_SCAN_PRIORITY_LOOKAHEAD_DAYS": "0",
                        "PM_SCAN_PRIORITY_TOPN": "10",
                    },
                    clear=False,
                ):
                    plan = swm._load_priority_scan_plan(run_date)

            self.assertNotIn(slug, list(plan.get("settlement_pending_slugs") or []))
            self.assertNotIn(slug, list(plan.get("fixed_slugs") or []))

    def test_priority_plan_promotes_past_settlement_pending_without_event_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            with mock.patch.object(swm, "DB_PATH", db_path):
                swm.init_db()
                now = datetime.now(timezone.utc).replace(microsecond=0)
                run_date = now.date().isoformat()
                previous_date = (now.date() - timedelta(days=1)).isoformat()
                slug = "highest-temperature-in-missing-end-city-on-test-date"
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO event_runs(
                        run_date, event_date, slug, title, event_end_utc,
                        settlement_actual, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_date,
                        previous_date,
                        slug,
                        "Highest temperature in Missing End City on Test Date?",
                        "",
                        None,
                        (now - timedelta(hours=6)).isoformat(),
                    ),
                )
                conn.commit()
                conn.close()

                with mock.patch.dict(
                    "os.environ",
                    {
                        "PM_SCAN_PRIORITY_ENABLED": "1",
                        "PM_SCAN_PRIORITY_INCLUDE_SETTLEMENT_PENDING": "1",
                        "PM_SCAN_PRIORITY_SETTLEMENT_PENDING_TOPN": "10",
                        "PM_SCAN_PRIORITY_SETTLEMENT_PENDING_LOOKBACK_DAYS": "7",
                        "PM_SCAN_PRIORITY_LOOKAHEAD_DAYS": "0",
                        "PM_SCAN_PRIORITY_TOPN": "10",
                    },
                    clear=False,
                ):
                    plan = swm._load_priority_scan_plan(run_date)

            self.assertIn(slug, list(plan.get("settlement_pending_slugs") or []))
            self.assertIn(slug, list(plan.get("fixed_slugs") or []))
            stub = dict((plan.get("event_stub_by_slug") or {}).get(slug) or {})
            self.assertEqual(stub.get("endDate"), "")

    def test_priority_plan_orders_settlement_pending_before_new_market(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            with mock.patch.object(swm, "DB_PATH", db_path):
                swm.init_db()
                now = datetime.now(timezone.utc).replace(microsecond=0)
                run_date = now.date().isoformat()
                pending_date = (now.date() - timedelta(days=1)).isoformat()
                future_date = (now.date() + timedelta(days=1)).isoformat()
                pending_slug = "highest-temperature-in-pending-before-new-city-on-test-date"
                new_slug = "highest-temperature-in-new-market-city-on-test-date"
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO event_runs(
                        run_date, event_date, slug, title, event_end_utc,
                        settlement_actual, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_date,
                        pending_date,
                        pending_slug,
                        "Pending Before New City",
                        (now - timedelta(hours=4)).isoformat(),
                        None,
                        (now - timedelta(hours=6)).isoformat(),
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO market_tokens(
                        event_date, slug, outcome, yes_token, no_token,
                        market_active, market_closed, market_archived, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (future_date, new_slug, "10C", "yes-new", "no-new", 1, 0, 0, now.isoformat()),
                )
                cur.execute(
                    """
                    INSERT INTO market_tokens(
                        event_date, slug, outcome, yes_token, no_token,
                        market_active, market_closed, market_archived, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (future_date, new_slug, "11C", "yes-new-2", "no-new-2", 1, 0, 0, now.isoformat()),
                )
                cur.execute(
                    """
                    CREATE TABLE weather_market_discovery (
                        event_date TEXT,
                        slug TEXT,
                        title TEXT,
                        token_count INTEGER,
                        first_seen_at TEXT,
                        first_tokenized_at TEXT,
                        updated_at TEXT
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE event_fact_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_date TEXT,
                        slug TEXT,
                        event_date TEXT,
                        fact_as_of TEXT,
                        created_at TEXT
                    )
                    """
                )
                cur.execute(
                    """
                    INSERT INTO weather_market_discovery(
                        event_date, slug, title, token_count, first_seen_at, first_tokenized_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (future_date, new_slug, "New Market City", 2, now.isoformat(), now.isoformat(), now.isoformat()),
                )
                conn.commit()
                conn.close()

                with mock.patch.dict(
                    "os.environ",
                    {
                        "PM_SCAN_PRIORITY_ENABLED": "1",
                        "PM_SCAN_PRIORITY_INCLUDE_SETTLEMENT_PENDING": "1",
                        "PM_SCAN_PRIORITY_SETTLEMENT_PENDING_TOPN": "10",
                        "PM_SCAN_PRIORITY_SETTLEMENT_PENDING_LOOKBACK_DAYS": "7",
                        "PM_SCAN_PRIORITY_INCLUDE_NEW_MARKETS": "1",
                        "PM_SCAN_PRIORITY_NEW_MARKET_TOPN": "10",
                        "PM_SCAN_PRIORITY_LOOKAHEAD_DAYS": "1",
                        "PM_SCAN_PRIORITY_TOPN": "10",
                    },
                    clear=False,
                ):
                    plan = swm._load_priority_scan_plan(run_date)

            fixed_slugs = list(plan.get("fixed_slugs") or [])
            self.assertIn(pending_slug, fixed_slugs)
            self.assertIn(new_slug, fixed_slugs)
            self.assertLess(fixed_slugs.index(pending_slug), fixed_slugs.index(new_slug))

    def test_chengdu_scope_future_model_missing_not_starved_by_non_chengdu_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            with mock.patch.object(swm, "DB_PATH", db_path):
                swm.init_db()
                run_date = "2026-06-22"
                now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS weather_market_discovery (
                        slug TEXT NOT NULL,
                        event_date TEXT NOT NULL,
                        title TEXT,
                        market_open_at TEXT,
                        first_seen_at TEXT NOT NULL,
                        first_tokenized_at TEXT,
                        token_count INTEGER NOT NULL DEFAULT 0,
                        status TEXT,
                        details_json TEXT,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (slug, event_date)
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS event_fact_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_date TEXT,
                        slug TEXT,
                        event_date TEXT,
                        fact_as_of TEXT,
                        created_at TEXT
                    )
                    """
                )
                candidates = [
                    ("lowest-temperature-in-london-on-june-23-2026", "2026-06-23", "Lowest temperature in London on June 23?"),
                    ("lowest-temperature-in-paris-on-june-23-2026", "2026-06-23", "Lowest temperature in Paris on June 23?"),
                    ("lowest-temperature-in-seoul-on-june-23-2026", "2026-06-23", "Lowest temperature in Seoul on June 23?"),
                    ("highest-temperature-in-chengdu-on-june-24-2026", "2026-06-24", "Highest temperature in Chengdu on June 24?"),
                ]
                for idx, (slug, event_date, title) in enumerate(candidates):
                    ts = (now + timedelta(minutes=idx)).isoformat()
                    cur.execute(
                        """
                        INSERT INTO weather_market_discovery(
                            slug, event_date, title, first_seen_at, first_tokenized_at,
                            token_count, status, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (slug, event_date, title, ts, ts, 2, "tokenized", ts),
                    )
                    for outcome in ("27°C", "28°C"):
                        cur.execute(
                            """
                            INSERT INTO market_tokens(
                                event_date, slug, outcome, yes_token, no_token,
                                market_active, market_closed, market_archived, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                event_date,
                                slug,
                                outcome,
                                f"yes-{slug}-{outcome}",
                                f"no-{slug}-{outcome}",
                                1,
                                0,
                                0,
                                ts,
                            ),
                        )
                conn.commit()
                conn.close()

                with mock.patch.dict(
                    "os.environ",
                    {
                        "PM_ARCHIVE_WRITE_SCOPE": "chengdu_zuuu",
                        "PM_SCAN_PRIORITY_ENABLED": "1",
                        "PM_SCAN_PRIORITY_INCLUDE_ENTRY_OK": "0",
                        "PM_SCAN_PRIORITY_INCLUDE_Q_MISSING": "0",
                        "PM_SCAN_PRIORITY_INCLUDE_MARKET_TICK_STALE": "0",
                        "PM_SCAN_PRIORITY_INCLUDE_NEW_MARKETS": "0",
                        "PM_SCAN_PRIORITY_INCLUDE_FUTURE_MODEL_MISSING": "1",
                        "PM_SCAN_PRIORITY_INCLUDE_SETTLEMENT_PENDING": "0",
                        "PM_SCAN_PRIORITY_INCLUDE_RECOMMENDED_PILOT_GAPS": "0",
                        "PM_SCAN_PRIORITY_INCLUDE_LIKELIHOOD_STALE": "0",
                        "PM_SCAN_PRIORITY_TOPN": "3",
                        "PM_SCAN_PRIORITY_FUTURE_MODEL_MISSING_TOPN": "3",
                        "PM_SCAN_PRIORITY_LOOKAHEAD_DAYS": "2",
                    },
                    clear=False,
                ):
                    plan = swm._load_priority_scan_plan(run_date)

            self.assertEqual(
                plan.get("future_model_missing_slugs"),
                ["highest-temperature-in-chengdu-on-june-24-2026"],
            )
            self.assertIn(
                "highest-temperature-in-chengdu-on-june-24-2026",
                plan.get("fixed_slugs") or [],
            )
            self.assertIn(
                "highest-temperature-in-chengdu-on-june-24-2026",
                plan.get("cap_protected_slugs") or [],
            )

    def test_refresh_settlement_pending_appends_actual_from_existing_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            with mock.patch.object(swm, "DB_PATH", db_path):
                swm.init_db()
                now = datetime.now(timezone.utc).replace(microsecond=0)
                run_date = now.date().isoformat()
                event_date = (now.date() - timedelta(days=1)).isoformat()
                slug = "highest-temperature-in-direct-settlement-city-on-test-date"
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO event_runs(
                        run_date, event_date, slug, title, resolution_source,
                        station_id, market_var, market_unit, lat, lon, tz,
                        event_end_utc, settlement_actual, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_date,
                        event_date,
                        slug,
                        "Highest temperature in Direct Settlement City?",
                        "https://www.wunderground.com/history/daily/us/test/KAAA",
                        "KAAA",
                        "temperature_2m_max",
                        "C",
                        40.0,
                        -73.0,
                        "UTC",
                        (now - timedelta(hours=4)).isoformat(),
                        None,
                        '{"rows":[]}',
                        (now - timedelta(hours=6)).isoformat(),
                    ),
                )
                conn.commit()
                conn.close()

                with mock.patch("scan_weather_market.fetch_settlement_actual", return_value=23.5) as fetch_mock:
                    ok, reason, actual = swm.refresh_settlement_actual_for_pending_event(
                        run_date,
                        event_date,
                        slug,
                        "Highest temperature in Direct Settlement City?",
                        {"endDate": (now - timedelta(hours=4)).isoformat()},
                    )

                self.assertTrue(ok)
                self.assertEqual(reason, "settlement_actual_refreshed")
                self.assertEqual(actual, 23.5)
                fetch_mock.assert_called_once()
                args, kwargs = fetch_mock.call_args
                self.assertEqual(args[:4], (
                    "https://www.wunderground.com/history/daily/us/test/KAAA",
                    event_date,
                    "temperature_2m_max",
                    "C",
                ))
                self.assertEqual(kwargs.get("station_id"), "KAAA")

                conn = sqlite3.connect(db_path)
                rows = conn.execute(
                    """
                    SELECT settlement_actual, market_closed, created_at
                    FROM event_runs
                    WHERE slug = ? AND event_date = ?
                    ORDER BY id ASC
                    """,
                    (slug, event_date),
                ).fetchall()
                conn.close()

            self.assertEqual(len(rows), 2)
            self.assertIsNone(rows[0][0])
            self.assertEqual(rows[1][0], 23.5)
            self.assertEqual(rows[1][1], 1)

    def test_model_refresh_settlement_pending_fetches_detail_when_stub_rules_missing(self) -> None:
        run_date = "2026-05-06"
        event_date = "2026-05-05"
        slug = "highest-temperature-in-detail-fetch-city-on-may-5-2026"
        priority_plan = {
            "enabled": True,
            "ordered_slugs": [slug],
            "fixed_slugs": [slug],
            "candidate_slugs": [],
            "settlement_pending_slugs": [slug],
            "fixed_pruned_no_quote_slugs": [],
            "position_slugs": [],
            "open_order_slugs": [],
            "q_missing_slugs": [],
            "entry_ok_slugs": [],
            "event_stub_by_slug": {
                slug: {
                    "slug": slug,
                    "title": "Highest temperature in Detail Fetch City on May 5?",
                    "endDate": "",
                    "description": "",
                    "resolutionSource": "",
                }
            },
            "others_per_run": 0,
            "candidates_per_run": 0,
        }
        detail = {
            "slug": slug,
            "title": "Highest temperature in Detail Fetch City on May 5?",
            "endDate": "2026-05-05T23:59:00Z",
            "description": "Settles according to the linked weather station.",
            "resolutionSource": "https://www.wunderground.com/history/daily/us/test/KDDD",
        }
        args = [
            "scan_weather_market.py",
            "--scan-weather-today",
            "--model-refresh-only",
            "--no-auto-trade",
            "--no-auto-diagnose",
            "--days-ahead",
            "0",
            "--tz",
            "America/New_York",
        ]
        with (
            mock.patch("scan_weather_market.today_date_str", return_value=run_date),
            mock.patch(
                "scan_weather_market.fetch_weather_events",
                return_value=(
                    [
                        {
                            "slug": "highest-temperature-in-other-city-on-may-6-2026",
                            "title": "Highest temperature in Other City on May 6?",
                            "endDate": "2026-05-06T23:59:00Z",
                        }
                    ],
                    [],
                ),
            ),
            mock.patch("scan_weather_market._load_priority_scan_plan", return_value=priority_plan),
            mock.patch("scan_weather_market.fetch_gamma_event_by_slug", return_value=detail) as detail_mock,
            mock.patch(
                "scan_weather_market.refresh_settlement_actual_for_pending_event",
                return_value=(True, "settlement_actual_refreshed", 21.5),
            ) as refresh_mock,
            mock.patch.dict(
                "os.environ",
                {
                    "PM_SCAN_PRIORITY_ENABLED": "1",
                    "PM_SCAN_FETCH_EVENT_DETAIL": "0",
                    "PM_SCAN_RULES_FIRST": "1",
                    "PM_SCAN_MAX_EVENTS_PER_RUN": "1",
                    "PM_SCAN_MAX_MATCHED_PER_RUN": "1",
                },
                clear=False,
            ),
            mock.patch("sys.argv", args),
        ):
            swm.main()

        detail_mock.assert_called_once_with(slug)
        refresh_mock.assert_called_once()
        call_args = refresh_mock.call_args.args
        self.assertEqual(call_args[:4], (
            run_date,
            event_date,
            slug,
            "Highest temperature in Detail Fetch City on May 5?",
        ))
        self.assertEqual(call_args[4].get("endDate"), "2026-05-05T23:59:00Z")
        self.assertEqual(
            call_args[4].get("resolutionSource"),
            "https://www.wunderground.com/history/daily/us/test/KDDD",
        )


if __name__ == "__main__":
    unittest.main()
