import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import scan_weather_market as swm


class TestDbLocking(unittest.TestCase):
    def setUp(self) -> None:
        self._auto_trade_globals_before = {
            "AUTO_TRADE_ENABLED": swm.AUTO_TRADE_ENABLED,
            "AUTO_TRADE_DRY_RUN": swm.AUTO_TRADE_DRY_RUN,
            "AUTO_TRADE_SYNC_TRADES": swm.AUTO_TRADE_SYNC_TRADES,
        }

    def tearDown(self) -> None:
        for key, value in self._auto_trade_globals_before.items():
            setattr(swm, key, value)

    def test_market_refresh_defaults_use_resilient_db_busy_timeout(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "scripts" / "market_refresh_1m.sh").read_text()

        self.assertIn("PM_MARKET_REFRESH_DB_BUSY_TIMEOUT_MS:-5000", script)
        self.assertIn("PM_MARKET_REFRESH_DB_CONNECT_TIMEOUT_SEC:-5", script)
        self.assertIn("DB_BUSY=${PM_DB_BUSY_TIMEOUT_MS:-na}ms", script)
        self.assertIn("PM_MARKET_REFRESH_MAX_EVENTS_PER_RUN:=12", script)
        self.assertIn("PM_MARKET_REFRESH_AUTO_TRADE_HOT_EVENTS_CAP:=3", script)
        self.assertIn("PM_MARKET_REFRESH_BURST_HOT_EVENTS_CAP:=3", script)
        self.assertIn("PM_MARKET_REFRESH_PRIORITY_MARKET_TICK_STALE_TOPN:=${PM_MARKET_REFRESH_MAX_EVENTS_PER_RUN}", script)
        self.assertIn('export PM_SCAN_PRIORITY_MARKET_TICK_STALE_TOPN="${PM_MARKET_REFRESH_PRIORITY_MARKET_TICK_STALE_TOPN}"', script)
        self.assertIn("P_STALE=${PM_SCAN_PRIORITY_MARKET_TICK_STALE_TOPN:-na}", script)
        self.assertIn("PM_SCAN_MARKET_REFRESH_CLOB_BOOK_TIMEOUT_SEC:=1", script)
        self.assertIn("PM_SCAN_MARKET_REFRESH_CLOB_BOOK_RETRIES:=1", script)
        self.assertIn('PM_SCAN_MARKET_REFRESH_QUOTE_PREFETCH_WORKERS:-6', script)
        self.assertIn('PM_MARKET_REFRESH_TICK_WRITE_DB_CONNECT_TIMEOUT_SEC:-0.25', script)
        self.assertIn('PM_MARKET_REFRESH_TICK_WRITE_DB_BUSY_TIMEOUT_MS:-250', script)
        self.assertIn('PM_MARKET_REFRESH_TICK_WRITE_LOCK_RETRIES:-0', script)
        self.assertIn("PM_MARKET_REFRESH_DB_LOCK_ENABLED:=1", script)
        self.assertIn("Coordinate the two heavy archive writers by default", script)
        self.assertIn("PM_ARCHIVE_WRITE_SCOPE:=chengdu_zuuu", script)
        self.assertIn("PM_MARKET_REFRESH_HEALTH_SQL_TIMEOUT_SEC", script)
        self.assertIn("highest-temperature-in-chengdu-", script)
        self.assertIn("JOIN active a", script)
        self.assertIn("CLOB_RETRIES=${PM_SCAN_MARKET_REFRESH_CLOB_BOOK_RETRIES:-na}", script)

    def test_model_scan_auto_trade_defaults_avoid_repeated_timeout_loops(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "scripts" / "model_scan.sh").read_text()

        self.assertIn("PM_MODEL_SCAN_AUTO_TRADE_AUTO_DIAG:-0", script)
        self.assertIn("PM_MODEL_SCAN_AUTO_TRADE_RETRY_MAX:-0", script)
        self.assertIn("PM_MODEL_SCAN_AUTO_TRADE_TIMEOUT_SEC:-360", script)
        self.assertIn("PM_MODEL_SCAN_FACT_STALE_BYPASS_DURING_AUTO_TRADE:=1", script)
        self.assertIn("PM_MODEL_SCAN_WEATHER_LIKELIHOOD_STALE_BYPASS_SEC", script)
        self.assertIn("date_inputs = (event_date,) if event_date else (run_date,)", script)
        self.assertIn("PM_MODEL_SCAN_WU_TWC_OBSERVED_STALE_GUARD", script)
        self.assertIn("wu_has_observation_ref", script)
        self.assertIn('wu_available in {"1", "true", "yes", "on"}', script)
        self.assertIn("wu_twc_observed_intraday_fetched_at", script)
        self.assertIn("PM_MODEL_SCAN_DYNAMIC_WEATHER_LIKELIHOOD_STALE_SEC:-1", script)
        self.assertIn("_temperature_peak_likelihood_refresh_interval_sec", script)
        self.assertIn("PM_MARKET_SCAN_COMPOSE_MARKET_BOOTSTRAP", script)
        self.assertIn("PM_MODEL_SCAN_WEATHER_STALE_MAX_EVENTS_PER_RUN:-1", script)
        self.assertIn("_model_scan_event_days_ahead_from_run_date", script)
        self.assertIn("PM_MODEL_SCAN_WEATHER_STALE_DEFAULT_DAYS_AHEAD", script)
        self.assertIn("PM_MODEL_SCAN_WEATHER_STALE_DAYS_AHEAD:-${PM_MODEL_SCAN_WEATHER_STALE_DEFAULT_DAYS_AHEAD}", script)
        self.assertIn("PM_MODEL_SCAN_WEATHER_STALE_NO_UPDATE_MODELS", script)
        self.assertIn("PM_MODEL_SCAN_WEATHER_STALE_PRIORITY_ENABLED:-0", script)
        self.assertIn("weather likelihood stale, run current-event cached-observation refresh", script)
        self.assertIn("priority=${PM_SCAN_PRIORITY_ENABLED}", script)
        self.assertIn("export PM_SCAN_NO_UPDATE_MODELS=1", script)
        self.assertIn("export PM_SCAN_PRIORITY_ENABLED=0", script)
        self.assertIn("PM_MODEL_SCAN_DB_LOCK_ENABLED:=1", script)
        self.assertIn("--refresh-awc-facts --awc-date", script)
        self.assertIn("PM_MODEL_SCAN_AWC_FACT_REFRESH_PRE_LANE_ENABLED:-1", script)
        self.assertIn("PM_MODEL_SCAN_AWC_FACT_REFRESH_MIN_INTERVAL_SEC:-300", script)
        self.assertIn("awc_fact_refresh cooldown", script)
        self.assertIn("data/awc_fact_refresh.lock", script)
        self.assertIn('_model_scan_run_awc_fact_refresh "pre_lane"', script)
        self.assertIn("awc_fact_refresh pre-lane complete; continue regular scan", script)
        self.assertIn("MODEL_SCAN_AWC_FACT_REFRESH_DONE=1", script)
        self.assertLess(
            script.index('_model_scan_run_awc_fact_refresh "pre_lane"'),
            script.index('weather_scan_lane_acquire "model_scan"'),
        )
        self.assertIn("awc_fact_refresh locked-lane complete; continue regular scan", script)
        self.assertIn("PM_MODEL_SCAN_AWC_FACT_REFRESH_SKIP_SCAN:-0", script)
        self.assertIn("PM_MODEL_SCAN_AWC_FACT_REFRESH_SKIP_HOT_SCHEDULE:-1", script)
        self.assertIn("awc_fact_refresh ok but hot schedule requires heavyweight scan", script)
        self.assertIn('export PM_SCAN_AUTO_DIAG="${PM_SCAN_AUTO_DIAG:-0}"', script)
        self.assertIn("PM_MODEL_SCAN_YIELD_TO_MARKET_REFRESH_WHEN_TICK_STALE:=1", script)
        self.assertIn("market_refresh running, yield while ticks stale/unknown", script)

    def test_model_scan_materializes_stale_facts_before_auto_trade_yield(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "scripts" / "model_scan.sh").read_text()

        self.assertIn("PM_MODEL_SCAN_FACTS_MATERIALIZE_ON_YIELD:-1", script)
        self.assertIn("model_scan.facts_yield_materialize_ts", script)
        self.assertIn("facts_materialize_on_yield start", script)
        self.assertIn('_model_scan_materialize_stale_facts "auto_trade_worker_process"', script)
        self.assertIn("--materialize-facts --facts-date", script)

    def test_open_db_autocommit_uses_isolation_level_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            data_dir = Path(tmpdir)
            with mock.patch.object(swm, "DB_PATH", db_path), mock.patch.object(swm, "DATA_DIR", data_dir):
                conn = swm.open_db(autocommit=True)
                try:
                    self.assertIsNone(conn.isolation_level)
                finally:
                    conn.close()

    def test_open_db_context_manager_closes_connection(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            data_dir = Path(tmpdir)
            with mock.patch.object(swm, "DB_PATH", db_path), mock.patch.object(swm, "DATA_DIR", data_dir):
                with swm.open_db() as conn:
                    conn.execute("CREATE TABLE sample(id INTEGER PRIMARY KEY)")

                with self.assertRaises(sqlite3.ProgrammingError):
                    conn.execute("SELECT 1")

    def test_init_db_skips_repeated_schema_bootstrap_for_same_db_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            data_dir = Path(tmpdir)
            real_connect = sqlite3.connect
            connect_calls = 0

            def _counting_connect(*args, **kwargs):
                nonlocal connect_calls
                connect_calls += 1
                return real_connect(*args, **kwargs)

            with mock.patch.object(swm, "DB_PATH", db_path), mock.patch.object(swm, "DATA_DIR", data_dir):
                with mock.patch("scan_weather_market.sqlite3.connect", side_effect=_counting_connect):
                    swm.init_db()
                    swm.init_db()

            self.assertEqual(connect_calls, 1)

    def test_init_db_creates_priority_refresh_indexes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            data_dir = Path(tmpdir)
            with mock.patch.object(swm, "DB_PATH", db_path), mock.patch.object(swm, "DATA_DIR", data_dir):
                swm.init_db()
                conn = sqlite3.connect(db_path)
                try:
                    index_names = set()
                    for table_name in (
                        "auto_trade_decisions",
                        "event_market_ticks",
                        "event_runs",
                        "scan_skips",
                    ):
                        index_names.update(row[1] for row in conn.execute(f"PRAGMA index_list({table_name})"))
                finally:
                    conn.close()

            self.assertIn("idx_auto_trade_decisions_action_event_created", index_names)
            self.assertIn("idx_event_market_ticks_event_slug_created", index_names)
            self.assertIn("idx_event_market_ticks_slug_created", index_names)
            self.assertIn("idx_event_runs_slug_created_at", index_names)
            self.assertIn("idx_scan_skips_created_reason_slug", index_names)

    def test_execute_auto_trades_opens_autocommit_db_connection(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            data_dir = Path(tmpdir)
            with mock.patch.object(swm, "DB_PATH", db_path), mock.patch.object(swm, "DATA_DIR", data_dir):
                swm.init_db()
                captured_kwargs = []
                real_open_db = swm.open_db

                def _capture_open_db(*args, **kwargs):
                    captured_kwargs.append(dict(kwargs))
                    return real_open_db(*args, **kwargs)

                result = {
                    "slug": "test-slug",
                    "title": "Test Event",
                    "event_url": "https://example.com/test",
                    "rows": [
                        {
                            "outcome": "39°C",
                            "best_bid": 0.39,
                            "best_ask": 0.40,
                            "spread": 0.01,
                            "best_bid_no": 0.59,
                            "best_ask_no": 0.60,
                            "spread_no": 0.01,
                            "yes_token": "tok_yes",
                            "no_token": "tok_no",
                            "q_entry": 0.50,
                            "q_round": 0.50,
                            "q_calibrated": 0.50,
                            "q_quote": 0.50,
                            "q_edge": 0.50,
                            "q_entry_no": 0.50,
                            "q_round_no": 0.50,
                            "q_calibrated_no": 0.50,
                            "q_quote_no": 0.50,
                            "q_edge_no": 0.50,
                            "exec_edge_entry_yes": 0.02,
                            "exec_edge_exit_yes": 0.02,
                            "exec_edge_score_yes": 2.0,
                            "exec_edge_entry_no": -0.1,
                            "exec_edge_exit_no": -0.1,
                            "exec_edge_score_no": 0.0,
                            "ask_depth_topN": 100.0,
                            "ask_depth_topN_no": 100.0,
                        }
                    ],
                }

                env = {
                    "PM_AUTO_TRADE_ENABLED": "1",
                    "PM_AUTO_TRADE_DRY_RUN": "1",
                    "PM_AUTO_TRADE_SYNC_TRADES": "0",
                    "PM_AUTO_TRADE_SYNC_OPEN_ORDERS": "0",
                    "PM_POSITIONS_SYNC": "0",
                    "PM_AUTO_TRADE_ENTRY_OPEN_DELAY_MAX_SEC": "0",
                    "PM_AUTO_TRADE_ENTRY_ONLY_REALIZABLE": "1",
                    "PM_AUTO_TRADE_MARKET_TICK_REQUIRE_PRESENT": "0",
                    "PM_AUTO_TRADE_MARKET_TICK_STALE_SEC": "0",
                    "PM_EDGE_REALIZABLE_V2_ENABLED": "0",
                    "PM_EDGE_REALIZABLE_V2_GATE_ENTRY": "0",
                    "PM_ORDER_USDC": "10",
                    "PM_AUTO_TRADE_ENTRY_STALE_SEC": "0",
                    "PM_AUTO_TRADE_EVENT_TIMEOUT_SEC": "0",
                    "PM_AUTO_TRADE_EVENT_BACKOFF_SEC": "0",
                }
                with mock.patch.object(swm, "init_db", return_value=None):
                    with mock.patch.object(swm, "open_db", side_effect=_capture_open_db):
                        with mock.patch.dict(os.environ, env, clear=False):
                            swm.execute_auto_trades(
                                "2026-04-16",
                                "2026-04-16",
                                result,
                                exit_only=False,
                                positions_snapshot={},
                                positions_snapshot_error="",
                                settled_reconcile_context={"done": True},
                            )

                self.assertTrue(captured_kwargs)
                self.assertTrue(any(kwargs.get("autocommit") is True for kwargs in captured_kwargs))

    def test_open_db_accepts_timeout_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            data_dir = Path(tmpdir)
            with mock.patch.object(swm, "DB_PATH", db_path), mock.patch.object(swm, "DATA_DIR", data_dir):
                captured = {}
                real_open = swm._open_db_connection

                def _capture(**kwargs):
                    captured.update(kwargs)
                    return real_open(**kwargs)

                with mock.patch("scan_weather_market._open_db_connection", side_effect=_capture):
                    conn = swm.open_db(connect_timeout_sec=0.25, busy_timeout_ms=250)
                    try:
                        self.assertAlmostEqual(captured.get("connect_timeout_sec"), 0.25)
                        self.assertEqual(captured.get("busy_timeout_ms"), 250)
                    finally:
                        conn.close()


if __name__ == "__main__":
    unittest.main()
