import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import scan_weather_market as swm


class TestExitSweep(unittest.TestCase):
    def setUp(self) -> None:
        self._auto_trade_globals_before = {
            "AUTO_TRADE_ENABLED": swm.AUTO_TRADE_ENABLED,
            "AUTO_TRADE_DRY_RUN": swm.AUTO_TRADE_DRY_RUN,
            "AUTO_TRADE_SYNC_TRADES": swm.AUTO_TRADE_SYNC_TRADES,
        }

    def tearDown(self) -> None:
        for key, value in self._auto_trade_globals_before.items():
            setattr(swm, key, value)

    def test_exit_quote_refresh_has_time_budget_and_no_default_token_remap(self) -> None:
        text = Path("scan_weather_market.py").read_text(encoding="utf-8")
        self.assertIn("PM_AUTO_TRADE_EXIT_REFRESH_TIME_BUDGET_SEC", text)
        self.assertIn("PM_AUTO_TRADE_EXIT_REFRESH_TIMEOUT_SEC", text)
        self.assertIn("PM_AUTO_TRADE_EXIT_REFRESH_MAX_PER_EVENT", text)
        self.assertIn("PM_AUTO_TRADE_EXIT_REFRESH_ALLOW_TOKEN_REMAP", text)
        self.assertIn('"time_budget_sec": float(exit_refresh_time_budget_sec)', text)
        self.assertIn('"timeout_sec": float(exit_refresh_timeout_sec)', text)
        self.assertIn('"allow_token_remap": bool(exit_refresh_allow_token_remap)', text)
        self.assertIn("max_wall_time_sec=float(orderbook_timeout) if has_time_budget else None", text)
        self.assertIn("time_budget_reached", text)
        self.assertIn("PM_AUTO_TRADE_REFRESH_TICK_WRITE_BUSY_TIMEOUT_MS", text)
        self.assertIn("if (not has_time_budget) and _should_run_market_tick_cleanup(now_ts):", text)
        self.assertIn("PM_AUTO_TRADE_MARKET_TICK_SIGNATURE_BUSY_TIMEOUT_MS", text)
        self.assertIn("PM_AUTO_TRADE_MARKET_TICK_SIGNATURE_LOOKBACK_ROWS", text)
        self.assertIn("This function is only a coalescing", text)
        self.assertIn('"PM_LCB_GLOBAL_OB_REFRESH_ALLOW_TOKEN_REMAP",\n        False,', text)
        self.assertIn("apply_global_rate_limit=False", text)
        self.assertIn("max_wall_time_sec=float(lcb_global_ob_refresh_timeout_sec)", text)
        self.assertIn("max_wall_time_sec=float(price_timeout) if has_time_budget else None", text)
        self.assertIn("def _clob_book_wait_global_slot(\n    scope: str = \"clob_book\",\n    *,\n    max_wait_sec: Optional[float] = None,", text)
        self.assertNotIn("ROW_NUMBER() OVER (\\n                    PARTITION BY outcome, side", text)

    def test_budgeted_market_refresh_uses_short_db_write_path(self) -> None:
        text = Path("scan_weather_market.py").read_text(encoding="utf-8")
        self.assertIn("budgeted_market_refresh = bool(", text)
        self.assertIn("PM_MARKET_REFRESH_SKIP_EVENT_RUN_MERGE_WHEN_BUDGETED", text)
        self.assertIn("market_refresh_write_busy_timeout_ms = min(int(market_refresh_write_busy_timeout_ms), 50)", text)
        self.assertIn("max_lock_retries=int(market_refresh_write_lock_retries)", text)
        self.assertIn("if not (budgeted_market_refresh and skip_event_run_merge_when_budgeted):", text)
        self.assertIn("PM_MARKET_REFRESH_TICK_WRITE_WALL_SEC", text)
        self.assertIn("max_wall_time_sec=ticks_wall", text)
        self.assertIn("max_wall_time_sec=snapshot_remaining", text)
        self.assertIn("save_market_ticks budget_exhausted", text)
        self.assertIn("save_market_snapshot budget_exhausted", text)

    def _build_exit_only_fact_db(self, tmpdir: str) -> Path:
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
            CREATE TABLE edge_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT,
                event_date TEXT,
                outcome TEXT,
                side TEXT,
                state TEXT,
                position_size REAL
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
            INSERT INTO edge_trades(slug, event_date, outcome, side, state, position_size)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "highest-temperature-in-paris-on-april-3-2026",
                "2026-04-03",
                "13°C",
                "NO",
                "entry",
                7.6,
            ),
        )
        fact_payload = {
            "slug": "highest-temperature-in-paris-on-april-3-2026",
            "title": "Highest temperature in Paris on April 3?",
            "event_url": "https://polymarket.com/event/highest-temperature-in-paris-on-april-3-2026",
            "distribution": [
                {
                    "outcome": "13°C",
                    "best_bid_no": 0.21,
                    "best_ask_no": 0.22,
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
                "2026-04-03",
                "highest-temperature-in-paris-on-april-3-2026",
                "2026-04-03",
                "2026-04-03T13:02:08+00:00",
                "test",
                json.dumps(fact_payload, ensure_ascii=False),
                "2026-04-03T13:02:08+00:00",
            ),
        )
        fact_id = cur.lastrowid
        cur.execute(
            """
            INSERT INTO event_fact_latest(slug, event_date, fact_id, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                "highest-temperature-in-paris-on-april-3-2026",
                "2026-04-03",
                fact_id,
                "2026-04-03T13:02:08+00:00",
            ),
        )
        conn.commit()
        conn.close()
        return db_path

    def test_auto_trade_job_touch_returns_none_on_locked(self) -> None:
        with mock.patch.object(
            swm,
            "_open_auto_trade_job_bookkeeping_db",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            with mock.patch("scan_weather_market.time.sleep", return_value=None):
                self.assertIsNone(swm._auto_trade_job_touch(123, run_id=456))

    def test_exit_only_open_position_falls_back_to_facts_inline(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = self._build_exit_only_fact_db(tmpdir)

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
                direct_job_context=False,
            ):
                captured.append(
                    {
                        "run_date": run_date,
                        "event_date": event_date,
                        "payload": payload,
                        "exit_only": exit_only,
                        "run_id": run_id,
                        "positions_snapshot": positions_snapshot,
                        "positions_snapshot_error": positions_snapshot_error,
                        "settled_reconcile_context": settled_reconcile_context,
                        "direct_job_context": direct_job_context,
                    }
                )

            with mock.patch.object(swm, "DB_PATH", db_path):
                with mock.patch("scan_weather_market.multiprocessing.get_context", side_effect=AssertionError("should not spawn worker")):
                    with mock.patch.object(swm, "execute_auto_trades", side_effect=_fake_execute):
                        result = swm.run_auto_trades_for_date("2026-04-03", exit_only=True, run_id=123)

            self.assertTrue(result.get("ok"))
            self.assertEqual(result.get("processed"), 1)
            self.assertEqual(result.get("skipped"), 0)
            self.assertEqual(len(captured), 1)
            self.assertTrue(captured[0]["exit_only"])
            self.assertEqual(captured[0]["event_date"], "2026-04-03")
            self.assertEqual(captured[0]["payload"]["slug"], "highest-temperature-in-paris-on-april-3-2026")
            self.assertEqual(captured[0]["payload"]["rows"][0]["outcome"], "13°C")

    def test_exit_only_job_payload_filters_open_position_fallbacks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = self._build_exit_only_fact_db(tmpdir)
            now_iso = "2026-04-03T13:05:00+00:00"
            other_slug = "highest-temperature-in-lyon-on-april-3-2026"
            other_payload = {
                "slug": other_slug,
                "title": "Highest temperature in Lyon on April 3?",
                "event_url": f"https://polymarket.com/event/{other_slug}",
                "distribution": [
                    {
                        "outcome": "14°C",
                        "best_bid_no": 0.31,
                        "best_ask_no": 0.32,
                    }
                ],
            }
            with sqlite3.connect(db_path) as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO edge_trades(slug, event_date, outcome, side, state, position_size)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (other_slug, "2026-04-03", "14°C", "NO", "entry", 3.0),
                )
                cur.execute(
                    """
                    INSERT INTO event_fact_history(
                        run_date, slug, event_date, fact_as_of, composer_version, fact_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "2026-04-03",
                        other_slug,
                        "2026-04-03",
                        now_iso,
                        "test",
                        json.dumps(other_payload, ensure_ascii=False),
                        now_iso,
                    ),
                )
                fact_id = cur.lastrowid
                cur.execute(
                    """
                    INSERT INTO event_fact_latest(slug, event_date, fact_id, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (other_slug, "2026-04-03", fact_id, now_iso),
                )

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
                direct_job_context=False,
            ):
                captured.append(
                    {
                        "run_date": run_date,
                        "event_date": event_date,
                        "payload": payload,
                        "exit_only": exit_only,
                        "direct_job_context": direct_job_context,
                    }
                )

            target_slug = "highest-temperature-in-paris-on-april-3-2026"
            with mock.patch.object(swm, "DB_PATH", db_path):
                with mock.patch.object(swm, "execute_auto_trades", side_effect=_fake_execute):
                    result = swm.run_auto_trades_for_date(
                        "2026-04-03",
                        exit_only=True,
                        run_id=123,
                        job_payload={
                            "include_event_keys": [
                                {"slug": target_slug, "event_date": "2026-04-03"}
                            ]
                        },
                    )

            self.assertTrue(result.get("ok"))
            self.assertEqual(result.get("processed"), 1)
            self.assertEqual(result.get("job_filtered_events"), 1)
            self.assertEqual(len(captured), 1)
            self.assertEqual(captured[0]["payload"]["slug"], target_slug)

    def test_exit_only_marks_run_failed_when_decision_persistence_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = self._build_exit_only_fact_db(tmpdir)
            with mock.patch.object(swm, "DB_PATH", db_path):
                with mock.patch.object(
                    swm,
                    "execute_auto_trades",
                    side_effect=RuntimeError("decision_persistence_failed:1 [13°C NO entry]"),
                ):
                    result = swm.run_auto_trades_for_date("2026-04-03", exit_only=True, run_id=123)

            self.assertFalse(result.get("ok"))
            self.assertEqual(result.get("processed"), 0)
            self.assertEqual(result.get("skipped"), 1)
            self.assertEqual(result.get("error"), "decision_persistence_failed:1")
            self.assertEqual(len(result.get("decision_persistence_errors") or []), 1)

    def test_exit_only_keeps_best_effort_for_generic_event_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = self._build_exit_only_fact_db(tmpdir)
            with mock.patch.object(swm, "DB_PATH", db_path):
                with mock.patch.object(
                    swm,
                    "execute_auto_trades",
                    side_effect=RuntimeError("generic_worker_failed"),
                ):
                    result = swm.run_auto_trades_for_date("2026-04-03", exit_only=True, run_id=123)

            self.assertTrue(result.get("ok"))
            self.assertEqual(result.get("processed"), 0)
            self.assertEqual(result.get("skipped"), 1)
            self.assertIsNone(result.get("error"))

    def test_execute_auto_trades_exit_only_skips_q_fallback_payload_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            with mock.patch.object(swm, "DB_PATH", db_path):
                swm.init_db()
                result = {
                    "slug": "highest-temperature-in-test-city-on-april-3-2026",
                    "title": "Highest temperature in Test City on April 3?",
                    "event_url": "https://example.com/test-city",
                    "rows": [
                        {
                            "outcome": "10°C",
                            "best_bid": 0.20,
                            "best_ask": 0.21,
                            "best_bid_no": 0.79,
                            "best_ask_no": 0.80,
                        }
                    ],
                }
                with mock.patch.object(
                    swm,
                    "_load_latest_payload_rows",
                    side_effect=AssertionError("exit_only should not load q fallback payload rows"),
                ):
                    with mock.patch.dict(
                        "os.environ",
                        {
                            "PM_AUTO_TRADE_ENABLED": "1",
                            "PM_AUTO_TRADE_DRY_RUN": "1",
                            "PM_AUTO_TRADE_SYNC_OPEN_ORDERS": "0",
                        },
                        clear=False,
                    ):
                        swm.execute_auto_trades(
                            "2026-04-03",
                            "2026-04-03",
                            result,
                            exit_only=True,
                            positions_snapshot={},
                            positions_snapshot_error="",
                            settled_reconcile_context={"done": True},
                        )

    def test_execute_auto_trades_exit_only_skips_entry_confirm_for_live_position(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            with mock.patch.object(swm, "DB_PATH", db_path):
                swm.init_db()
                with sqlite3.connect(db_path) as conn:
                    cur = conn.cursor()
                    cur.execute(
                        """
                        INSERT INTO edge_trades (
                            slug, event_date, outcome, side, state, position_size, avg_price,
                            last_action, last_action_at, last_status, last_error,
                            last_order_id, last_action_order_type, entry_at, exit_triggered_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "highest-temperature-in-paris-on-april-4-2026",
                            "2026-04-04",
                            "16°C",
                            "NO",
                            "entry",
                            5.837917,
                            0.729999,
                            "pos_sync",
                            "2026-04-03T16:25:23+00:00",
                            "pos_sync",
                            "",
                            "0xfd26e775176ed96a054e65b2bea0e23870641734d256c1aa6882205ba59e6a71",
                            "FAK",
                            "2026-04-03T14:13:26+00:00",
                            "2026-04-03T15:00:00+00:00",
                        ),
                    )
                    conn.commit()
                result = {
                    "slug": "highest-temperature-in-paris-on-april-4-2026",
                    "title": "Highest temperature in Paris on April 4?",
                    "event_url": "https://example.com/paris",
                    "rows": [
                        {
                            "outcome": "16°C",
                            "best_bid_no": 0.74,
                            "best_ask_no": 0.75,
                            "best_bid": 0.25,
                            "best_ask": 0.26,
                            "no_token": "tok_paris_no",
                            "yes_token": "tok_paris_yes",
                        }
                    ],
                }
                with mock.patch.object(
                    swm,
                    "_run_node_get_order",
                    side_effect=AssertionError("exit_only live position should skip entry confirm"),
                ):
                    with mock.patch.dict(
                        "os.environ",
                        {
                            "PM_AUTO_TRADE_ENABLED": "1",
                            "PM_AUTO_TRADE_DRY_RUN": "1",
                            "PM_AUTO_TRADE_SYNC_OPEN_ORDERS": "0",
                            "PM_POSITIONS_SYNC": "0",
                        },
                        clear=False,
                    ):
                        swm.execute_auto_trades(
                            "2026-04-04",
                            "2026-04-04",
                            result,
                            exit_only=True,
                            positions_snapshot={},
                            positions_snapshot_error="",
                            settled_reconcile_context={"done": True},
                        )

    def test_run_node_order_market_sell_uses_fast_direct_env(self) -> None:
        captured = {}

        def _fake_subprocess(cmd, env=None, timeout=None, retries_on_errno24=0):
            captured["cmd"] = list(cmd)
            captured["env"] = dict(env or {})
            captured["timeout"] = timeout
            return mock.Mock(returncode=0, stdout='{"orderID":"oid-fast-exit","status":"submitted"}\n', stderr="")

        with mock.patch.dict("os.environ", {}, clear=True):
            with mock.patch.object(swm, "ensure_system_proxy_settings", return_value=None):
                with mock.patch.object(swm, "AUTO_TRADE_DRY_RUN", False):
                    with mock.patch.object(swm, "_resolve_node_bin", return_value="node"):
                        with mock.patch.object(swm, "_run_node_subprocess", side_effect=_fake_subprocess):
                            ok, payload = swm._run_node_order(
                                "place-order",
                                "tok_exit",
                                "SELL",
                                price=0.56,
                                size=3.25,
                                order_type="FAK",
                            )

        self.assertTrue(ok)
        self.assertEqual(payload.get("orderID"), "oid-fast-exit")
        self.assertEqual(captured["env"].get("PM_CLOB_FORCE_DIRECT"), "1")
        self.assertEqual(captured["env"].get("PM_CLOB_DIRECT_RETRIES"), "1")
        self.assertEqual(captured["env"].get("PM_CLOB_RETRY_MAX"), "1")
        self.assertEqual(captured["env"].get("PM_CLOB_RETRY_BASE_MS"), "0")
        self.assertEqual(captured["env"].get("PM_CLOB_RETRY_MAX_MS"), "0")
        self.assertEqual(captured["env"].get("PM_ALLOW_MARKET_ORDERS"), "1")
        self.assertTrue(int(captured["env"].get("PM_CLOB_TIMEOUT_MS", "0")) >= 3000)

    def test_direct_target_market_sell_uses_exit_timeout_budget(self) -> None:
        captured = {}

        def _fake_subprocess(cmd, env=None, timeout=None, retries_on_errno24=0):
            captured["cmd"] = list(cmd)
            captured["env"] = dict(env or {})
            captured["timeout"] = timeout
            return mock.Mock(returncode=0, stdout='{"orderID":"oid-direct-exit","status":"submitted"}\n', stderr="")

        with mock.patch.dict(
            "os.environ",
            {
                "PM_AUTO_TRADE_DIRECT_JOB_CONTEXT": "1",
                "PM_AUTO_TRADE_DIRECT_EXIT_ORDER_TIMEOUT_SEC": "7",
            },
            clear=True,
        ):
            with mock.patch.object(swm, "ensure_system_proxy_settings", return_value=None):
                with mock.patch.object(swm, "AUTO_TRADE_DRY_RUN", False):
                    with mock.patch.object(swm, "_resolve_node_bin", return_value="node"):
                        with mock.patch.object(swm, "_run_node_subprocess", side_effect=_fake_subprocess):
                            ok, payload = swm._run_node_order(
                                "place-order",
                                "tok_exit",
                                "SELL",
                                price=0.56,
                                size=3.25,
                                order_type="FAK",
                            )

        self.assertTrue(ok)
        self.assertEqual(payload.get("orderID"), "oid-direct-exit")
        self.assertTrue(payload.get("direct_target_order_context"))
        self.assertGreaterEqual(float(captured["timeout"]), 7.0)
        self.assertEqual(captured["env"].get("PM_AUTO_TRADE_DIRECT_EXIT_ORDER_CONTEXT"), "1")
        clob_timeout_ms = int(captured["env"].get("PM_CLOB_TIMEOUT_MS", "0"))
        self.assertGreater(clob_timeout_ms, 0)
        self.assertLess(clob_timeout_ms, int(float(captured["timeout"]) * 1000))
        self.assertGreater(
            int(captured["env"].get("PM_CLOB_PLACE_ORDER_HARD_TIMEOUT_MS", "0")),
            clob_timeout_ms,
        )

    def test_direct_target_market_sell_default_timeout_is_short(self) -> None:
        captured = {}

        def _fake_subprocess(cmd, env=None, timeout=None, retries_on_errno24=0):
            captured["cmd"] = list(cmd)
            captured["env"] = dict(env or {})
            captured["timeout"] = timeout
            return mock.Mock(returncode=0, stdout='{"orderID":"oid-direct-exit","status":"submitted"}\n', stderr="")

        with mock.patch.dict("os.environ", {"PM_AUTO_TRADE_DIRECT_JOB_CONTEXT": "1"}, clear=True):
            with mock.patch.object(swm, "ensure_system_proxy_settings", return_value=None):
                with mock.patch.object(swm, "AUTO_TRADE_DRY_RUN", False):
                    with mock.patch.object(swm, "_resolve_node_bin", return_value="node"):
                        with mock.patch.object(swm, "_run_node_subprocess", side_effect=_fake_subprocess):
                            ok, payload = swm._run_node_order(
                                "place-order",
                                "tok_exit",
                                "SELL",
                                price=0.56,
                                size=3.25,
                                order_type="FAK",
                            )

        self.assertTrue(ok)
        self.assertEqual(payload.get("orderID"), "oid-direct-exit")
        self.assertEqual(float(captured["timeout"]), 15.0)
        self.assertEqual(captured["env"].get("PM_AUTO_TRADE_DIRECT_EXIT_ORDER_CONTEXT"), "1")

    def test_run_node_get_order_uses_proxy_fallback_env(self) -> None:
        captured = {}

        def _fake_subprocess(cmd, env=None, timeout=None, retries_on_errno24=0):
            captured["cmd"] = list(cmd)
            captured["env"] = dict(env or {})
            captured["timeout"] = timeout
            return mock.Mock(
                returncode=0,
                stdout='{"ok":true,"order":{"orderID":"oid1","status":"live"}}\n',
                stderr="",
            )

        with mock.patch.dict("os.environ", {}, clear=True):
            with mock.patch.object(swm, "ensure_system_proxy_settings", return_value=None):
                with mock.patch.object(swm, "AUTO_TRADE_DRY_RUN", False):
                    with mock.patch.object(swm, "_resolve_node_bin", return_value="node"):
                        with mock.patch.object(swm, "_run_node_subprocess", side_effect=_fake_subprocess):
                            ok, payload = swm._run_node_get_order("oid1")

        self.assertTrue(ok)
        self.assertEqual(payload.get("order", {}).get("orderID"), "oid1")
        self.assertNotEqual(captured["env"].get("PM_CLOB_FORCE_DIRECT"), "1")
        self.assertEqual(captured["env"].get("PM_CLOB_FAST_ALLOW_PROXY_FALLBACK"), "1")
        self.assertEqual(captured["env"].get("PM_CLOB_DIRECT_RETRIES"), "1")
        self.assertEqual(captured["env"].get("PM_CLOB_RETRY_MAX"), "2")
        self.assertEqual(captured["env"].get("PM_CLOB_RETRY_BASE_MS"), "0")
        self.assertEqual(captured["env"].get("PM_CLOB_RETRY_MAX_MS"), "0")
        self.assertTrue(int(captured["env"].get("PM_CLOB_TIMEOUT_MS", "0")) >= 3000)
        self.assertTrue(int(captured["env"].get("PM_CLOB_GET_ORDER_HARD_TIMEOUT_MS", "0")) >= 4000)
        self.assertLessEqual(float(captured["timeout"]), 12.0)

    def test_direct_target_get_order_uses_short_timeout(self) -> None:
        captured = {}

        def _fake_subprocess(cmd, env=None, timeout=None, retries_on_errno24=0):
            captured["cmd"] = list(cmd)
            captured["env"] = dict(env or {})
            captured["timeout"] = timeout
            return mock.Mock(
                returncode=0,
                stdout='{"ok":true,"order":{"orderID":"oid-direct","status":"live"}}\n',
                stderr="",
            )

        with mock.patch.dict(
            "os.environ",
            {
                "PM_AUTO_TRADE_DIRECT_JOB_CONTEXT": "1",
                "PM_AUTO_TRADE_DIRECT_GET_ORDER_TIMEOUT_SEC": "3.5",
            },
            clear=True,
        ):
            with mock.patch.object(swm, "ensure_system_proxy_settings", return_value=None):
                with mock.patch.object(swm, "AUTO_TRADE_DRY_RUN", False):
                    with mock.patch.object(swm, "_resolve_node_bin", return_value="node"):
                        with mock.patch.object(swm, "_run_node_subprocess", side_effect=_fake_subprocess):
                            ok, payload = swm._run_node_get_order("oid-direct")

        self.assertTrue(ok)
        self.assertEqual(payload.get("order", {}).get("orderID"), "oid-direct")
        self.assertLessEqual(float(captured["timeout"]), 3.5)
        self.assertEqual(captured["env"].get("PM_CLOB_FORCE_DIRECT"), "1")
        self.assertLessEqual(
            int(captured["env"].get("PM_CLOB_GET_ORDER_HARD_TIMEOUT_MS", "0")),
            3500,
        )

    def test_run_node_subprocess_timeout_terminates_process_group(self) -> None:
        captured = {}
        kills = []

        class FakeProc:
            pid = 98765

            def __init__(self) -> None:
                self.returncode = None
                self.communicate_calls = 0

            def communicate(self, timeout=None):
                self.communicate_calls += 1
                if self.communicate_calls <= 2:
                    raise swm.subprocess.TimeoutExpired(["node", "script"], timeout)
                self.returncode = -9
                return "", ""

        def _fake_popen(cmd, **kwargs):
            captured["cmd"] = list(cmd)
            captured["kwargs"] = dict(kwargs)
            return FakeProc()

        with mock.patch.object(swm.subprocess, "Popen", side_effect=_fake_popen):
            with mock.patch.object(swm.os, "getpgid", return_value=4242):
                with mock.patch.object(
                    swm.os,
                    "killpg",
                    side_effect=lambda pgid, sig: kills.append((pgid, sig)),
                ):
                    with self.assertRaises(swm.subprocess.TimeoutExpired):
                        swm._run_node_subprocess(
                            ["node", "script"],
                            env={},
                            timeout=0.01,
                            retries_on_errno24=0,
                        )

        self.assertTrue(captured["kwargs"].get("start_new_session"))
        self.assertIn((4242, swm.signal.SIGTERM), kills)
        self.assertIn((4242, swm.signal.SIGKILL), kills)
        self.assertNotIn(4242, swm._NODE_SUBPROCESS_ACTIVE_PGIDS)

    def test_event_day_is_past_uses_fallback_event_date(self) -> None:
        now_dt = datetime(2026, 4, 4, 0, 0, tzinfo=timezone.utc)
        self.assertTrue(swm._event_day_is_past("", "2026-04-03", now_dt=now_dt))
        self.assertFalse(swm._event_day_is_past("", "2026-04-04", now_dt=now_dt))

    def test_should_preserve_exit_retry_block_only_for_live_exit_context(self) -> None:
        self.assertFalse(
            swm._should_preserve_exit_retry_block(
                prev_last_action="pos_sync",
                prev_exit_live_like=False,
            )
        )
        self.assertTrue(
            swm._should_preserve_exit_retry_block(
                prev_last_action="exit",
                prev_exit_live_like=False,
            )
        )
        self.assertTrue(
            swm._should_preserve_exit_retry_block(
                prev_last_action="pos_sync",
                prev_exit_live_like=True,
            )
        )

    def test_hard_zero_old_bucket_does_not_defer_for_target_switch_entry_priority(self) -> None:
        row = {
            "q_bayes_physical_likelihood_hard_zero": True,
            "q_bayes_physical_likelihood_hard_reasons": ["observed_today_max_above_bucket"],
            "q_bayes_posterior": 0.0,
            "target_adjustment_force_exit": True,
        }

        should_defer = swm._should_defer_target_switch_exit_for_entry_priority(
            exit_only=False,
            target_switch_expected_entry_keys={("25°C", "YES")},
            outcome_label="24°C or below",
            side_label="YES",
            row_ref=row,
            exit_codes=["target_realization_switch_buy_exit_old"],
        )

        self.assertFalse(should_defer)
        self.assertEqual(
            row["target_switch_entry_priority_override"],
            "hard_zero_old_bucket_exit",
        )

    def test_non_hard_zero_old_bucket_exits_immediately_for_target_switch(self) -> None:
        row = {"target_adjustment_force_exit": True}

        should_defer = swm._should_defer_target_switch_exit_for_entry_priority(
            exit_only=False,
            target_switch_expected_entry_keys={("25°C", "YES")},
            outcome_label="24°C or below",
            side_label="YES",
            row_ref=row,
            exit_codes=["target_realization_switch_buy_exit_old"],
        )

        self.assertFalse(should_defer)
        self.assertEqual(row["target_switch_entry_priority_override"], "old_bucket_exit_immediate")

    def test_old_no_bucket_exits_immediately_for_target_switch(self) -> None:
        row = {"target_adjustment_force_exit": True}

        should_defer = swm._should_defer_target_switch_exit_for_entry_priority(
            exit_only=False,
            target_switch_expected_entry_keys={("29°C", "YES"), ("28°C", "YES")},
            outcome_label="28°C",
            side_label="NO",
            row_ref=row,
            exit_codes=["target_realization_switch_buy_exit_old"],
        )

        self.assertFalse(should_defer)
        self.assertEqual(row["target_switch_entry_priority_override"], "old_bucket_exit_immediate")

    def test_target_realization_hard_zero_exit_requires_target_exit_code_when_codes_present(self) -> None:
        row = {
            "q_bayes_physical_likelihood_hard_zero": True,
            "q_bayes_physical_likelihood_hard_reasons": ["observed_today_max_above_bucket"],
            "q_bayes_posterior": 0.0,
        }

        blocked_meta = swm._target_realization_hard_zero_exit_meta(
            row,
            "YES",
            ["stop_loss"],
        )
        allowed_meta = swm._target_realization_hard_zero_exit_meta(
            row,
            "YES",
            ["target_realization_switch_buy_exit_old"],
        )

        self.assertFalse(blocked_meta["target_realization_hard_zero_exit"])
        self.assertTrue(allowed_meta["target_realization_hard_zero_exit"])
        self.assertTrue(allowed_meta["observed_today_max_above_bucket"])

    def test_hard_zero_price_floor_rearms_without_incrementing_retry_count(self) -> None:
        hard_zero_plan = swm._marketable_exit_retry_price_floor_plan(
            prev_exit_retry_count=3,
            hard_zero_price_floor_rearm=True,
        )
        normal_plan = swm._marketable_exit_retry_price_floor_plan(
            prev_exit_retry_count=3,
            hard_zero_price_floor_rearm=False,
        )

        self.assertEqual(
            hard_zero_plan["last_status"],
            "target_realization_hard_zero_dead_market",
        )
        self.assertEqual(hard_zero_plan["last_error"], "")
        self.assertEqual(hard_zero_plan["exit_retry_count"], 0)
        self.assertEqual(
            hard_zero_plan["reason_codes"],
            ["target_realization_hard_zero_dead_market"],
        )
        self.assertEqual(normal_plan["last_status"], "exit_retry_price_floor")
        self.assertEqual(normal_plan["last_error"], "exit_retry_price_floor")
        self.assertEqual(normal_plan["exit_retry_count"], 4)

    def test_hard_zero_dead_market_detects_ask_only_quote_as_unexitable(self) -> None:
        row = {
            "q_bayes_physical_likelihood_hard_zero": True,
            "q_bayes_physical_likelihood_hard_reasons": ["observed_today_max_above_bucket"],
            "q_bayes_posterior": 0.0,
        }

        meta = swm._hard_zero_dead_market_exit_meta(
            row,
            "YES",
            ["target_realization_switch_buy_exit_old"],
            best_bid=0.0,
            best_ask=0.001,
            bid_depth=0.0,
        )

        self.assertTrue(meta["exit_dead_market"])
        self.assertTrue(meta["exit_dead_market_ask_only"])
        self.assertEqual(
            meta["exit_dead_market_reason"],
            "hard_zero_old_bucket_no_executable_yes_bid",
        )

    def test_hard_zero_dead_market_guard_can_ignore_risk_exit_codes(self) -> None:
        row = {
            "q_bayes_physical_likelihood_hard_zero": True,
            "q_bayes_physical_likelihood_hard_reasons": ["observed_today_max_above_bucket"],
            "q_bayes_posterior": 0.0,
        }

        risk_code_meta = swm._hard_zero_dead_market_exit_meta(
            row,
            "YES",
            ["stop_loss", "trail_stop"],
            best_bid=0.0,
            best_ask=0.001,
            bid_depth=0.0,
        )
        final_guard_meta = swm._hard_zero_dead_market_exit_meta(
            row,
            "YES",
            None,
            best_bid=0.0,
            best_ask=0.001,
            bid_depth=0.0,
        )

        self.assertFalse(risk_code_meta["exit_dead_market"])
        self.assertTrue(final_guard_meta["exit_dead_market"])
        self.assertEqual(
            final_guard_meta["exit_dead_market_reason"],
            "hard_zero_old_bucket_no_executable_yes_bid",
        )

    def test_hard_zero_dead_market_allows_real_executable_bid(self) -> None:
        row = {
            "q_bayes_physical_likelihood_hard_zero": True,
            "q_bayes_physical_likelihood_hard_reasons": ["observed_today_max_above_bucket"],
            "q_bayes_posterior": 0.0,
        }

        meta = swm._hard_zero_dead_market_exit_meta(
            row,
            "YES",
            ["target_realization_switch_buy_exit_old"],
            best_bid=0.001,
            best_ask=0.002,
            bid_depth=20.0,
        )

        self.assertFalse(meta["exit_dead_market"])

    def test_is_marketable_no_match_error_true_for_exchange_message(self) -> None:
        self.assertTrue(
            swm._is_marketable_no_match_error(
                {
                    "error": "exec_nonzero",
                    "stderr": "no orders found to match with FAK order. FAK orders are partially filled or killed if no match is found.",
                }
            )
        )

    def test_is_marketable_no_match_error_false_for_other_errors(self) -> None:
        self.assertFalse(
            swm._is_marketable_no_match_error(
                {
                    "error": "exec_nonzero",
                    "stderr": "request failed with status=404 orderbook does not exist",
                }
            )
        )


if __name__ == "__main__":
    unittest.main()
