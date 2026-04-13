import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import scan_weather_market as swm


class TestExitSweep(unittest.TestCase):
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

    def test_run_node_get_order_uses_fast_direct_env(self) -> None:
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
        self.assertEqual(captured["env"].get("PM_CLOB_FORCE_DIRECT"), "1")
        self.assertEqual(captured["env"].get("PM_CLOB_DIRECT_RETRIES"), "1")
        self.assertEqual(captured["env"].get("PM_CLOB_RETRY_MAX"), "1")
        self.assertEqual(captured["env"].get("PM_CLOB_RETRY_BASE_MS"), "0")
        self.assertEqual(captured["env"].get("PM_CLOB_RETRY_MAX_MS"), "0")
        self.assertTrue(int(captured["env"].get("PM_CLOB_TIMEOUT_MS", "0")) >= 3000)
        self.assertTrue(int(captured["env"].get("PM_CLOB_GET_ORDER_HARD_TIMEOUT_MS", "0")) >= 4000)

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
