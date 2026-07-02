import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import scan_weather_market as swm


class TestTradeSyncMaps(unittest.TestCase):
    def test_sqlite_execute_with_retry_retries_locked_error(self) -> None:
        calls = []
        sleeps = []

        def _execute(sql: str, params: tuple) -> str:
            calls.append((sql, params))
            if len(calls) < 3:
                raise sqlite3.OperationalError("database is locked")
            return "ok"

        with mock.patch.object(swm.time, "sleep", side_effect=lambda sec: sleeps.append(sec)):
            result = swm._sqlite_execute_with_retry(
                _execute,
                "SELECT 1",
                (1,),
                retry_count=3,
                retry_sleep_sec=0.2,
            )

        self.assertEqual(result, "ok")
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, [0.2, 0.4])

    def test_sqlite_execute_with_retry_supports_cursor_style_attempts_signature(self) -> None:
        class FakeCursor:
            def __init__(self) -> None:
                self.calls = 0

            def execute(self, sql: str, params: tuple) -> str:
                self.calls += 1
                if self.calls < 3:
                    raise sqlite3.OperationalError("database is locked")
                return f"{sql}:{params[0]}"

        cursor = FakeCursor()
        sleeps = []
        with mock.patch.object(swm.time, "sleep", side_effect=lambda sec: sleeps.append(sec)):
            result = swm._sqlite_execute_with_retry(
                cursor,
                "SELECT ?",
                (7,),
                attempts=3,
                sleep_base_sec=0.1,
                sleep_cap_sec=0.15,
            )

        self.assertEqual(result, "SELECT ?:7")
        self.assertEqual(cursor.calls, 3)
        self.assertEqual(sleeps, [0.1, 0.15])

    def test_sqlite_execute_with_retry_does_not_swallow_non_lock_error(self) -> None:
        def _execute(_sql: str, _params: tuple) -> None:
            raise sqlite3.OperationalError("no such table: missing")

        with self.assertRaises(sqlite3.OperationalError):
            swm._sqlite_execute_with_retry(
                _execute,
                "SELECT 1",
                (),
                retry_count=3,
                retry_sleep_sec=0.0,
            )

    def test_resolve_trade_sync_mapping_uses_embedded_weather_fields(self) -> None:
        token_map = {}
        trade = {
            "asset_id": "asset-old-1",
            "eventSlug": "highest-temperature-in-toronto-on-april-9-2026",
            "bucket_outcome": "17C",
            "outcome_side": "NO",
        }

        mapping = swm._resolve_trade_sync_mapping(token_map, "asset-old-1", trade)

        self.assertEqual(
            mapping,
            {
                "slug": "highest-temperature-in-toronto-on-april-9-2026",
                "event_date": "2026-04-09",
                "outcome": "17C",
                "side": "NO",
            },
        )
        self.assertIn("asset-old-1", token_map)
        self.assertEqual(token_map["asset-old-1"]["event_date"], "2026-04-09")

    def test_recent_after_hint_falls_back_to_auto_orders_when_fills_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            with mock.patch.object(swm, "DB_PATH", db_path):
                swm.init_db()
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                created_at = "2026-04-14T10:00:00+00:00"
                cur.execute(
                    """
                    INSERT INTO auto_orders(
                        order_id, slug, event_date, outcome, side, action, price, size, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "order-1",
                        "highest-temperature-in-test-city-on-april-14-2026",
                        "2026-04-14",
                        "20C",
                        "YES",
                        "entry",
                        0.41,
                        1.0,
                        created_at,
                    ),
                )
                conn.commit()
                conn.close()

                hint = swm._trade_sync_recent_after_hint(
                    {
                        "token-yes": {
                            "slug": "highest-temperature-in-test-city-on-april-14-2026",
                            "event_date": "2026-04-14",
                            "outcome": "20C",
                            "side": "YES",
                        }
                    },
                    3600.0,
                )

            self.assertEqual(hint, "2026-04-14T09:00:00+00:00")

    def test_recent_after_hint_falls_back_to_edge_trades_when_orders_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            with mock.patch.object(swm, "DB_PATH", db_path):
                swm.init_db()
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                updated_at = "2026-04-14T11:30:00+00:00"
                cur.execute(
                    """
                    INSERT INTO edge_trades(
                        slug, event_date, outcome, side, state, position_size, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "highest-temperature-in-test-city-on-april-14-2026",
                        "2026-04-14",
                        "20C",
                        "NO",
                        "entry",
                        1.0,
                        updated_at,
                    ),
                )
                conn.commit()
                conn.close()

                hint = swm._trade_sync_recent_after_hint(
                    {
                        "token-no": {
                            "slug": "highest-temperature-in-test-city-on-april-14-2026",
                            "event_date": "2026-04-14",
                            "outcome": "20C",
                            "side": "NO",
                        }
                    },
                    1800.0,
                )

            self.assertEqual(hint, "2026-04-14T11:00:00+00:00")

    def test_build_trade_sync_maps_includes_market_tokens_without_event_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            with mock.patch.object(swm, "DB_PATH", db_path):
                swm.init_db()
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                now_text = datetime.now(timezone.utc).isoformat(timespec="seconds")
                cur.execute(
                    """
                    INSERT INTO market_tokens(
                        slug, event_date, outcome, yes_token, no_token, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "highest-temperature-in-test-city-on-april-14-2026",
                        "2026-04-14",
                        "20C",
                        "yes-token-1",
                        "no-token-1",
                        now_text,
                    ),
                )
                conn.commit()

                token_map, mark_map, event_count = swm.build_trade_sync_maps(conn, "2026-04-14")
                conn.close()

            self.assertIn("yes-token-1", token_map)
            self.assertIn("no-token-1", token_map)
            self.assertEqual(token_map["yes-token-1"]["side"], "YES")
            self.assertEqual(token_map["no-token-1"]["side"], "NO")
            self.assertEqual(mark_map, {})
            self.assertEqual(event_count, 0)

    def test_build_trade_sync_maps_can_limit_cached_market_tokens_to_run_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            with mock.patch.object(swm, "DB_PATH", db_path):
                swm.init_db()
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                now_text = datetime.now(timezone.utc).isoformat(timespec="seconds")
                cur.executemany(
                    """
                    INSERT INTO market_tokens(
                        slug, event_date, outcome, yes_token, no_token, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            "highest-temperature-in-test-city-on-april-14-2026",
                            "2026-04-14",
                            "20C",
                            "yes-token-current",
                            "no-token-current",
                            now_text,
                        ),
                        (
                            "highest-temperature-in-old-city-on-april-13-2026",
                            "2026-04-13",
                            "19C",
                            "yes-token-old",
                            "no-token-old",
                            now_text,
                        ),
                    ],
                )
                conn.commit()

                token_map, _mark_map, event_count = swm.build_trade_sync_maps(
                    conn,
                    "2026-04-14",
                    include_all_market_tokens=False,
                )
                conn.close()

            self.assertIn("yes-token-current", token_map)
            self.assertIn("no-token-current", token_map)
            self.assertNotIn("yes-token-old", token_map)
            self.assertNotIn("no-token-old", token_map)
            self.assertEqual(event_count, 0)

    def test_build_trade_sync_maps_overlays_marks_from_event_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            with mock.patch.object(swm, "DB_PATH", db_path):
                swm.init_db()
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                now = datetime.now(timezone.utc)
                now_text = now.isoformat(timespec="seconds")
                cur.execute(
                    """
                    INSERT INTO market_tokens(
                        slug, event_date, outcome, yes_token, no_token, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "highest-temperature-in-test-city-on-april-14-2026",
                        "2026-04-14",
                        "20C",
                        "yes-token-1",
                        "no-token-1",
                        now_text,
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO event_runs(
                        run_date, event_date, slug, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        "2026-04-14",
                        "2026-04-14",
                        "highest-temperature-in-test-city-on-april-14-2026",
                        (
                            '{"rows":[{"outcome":"20C","yes_token":"yes-token-1","no_token":"no-token-1",'
                            '"best_bid":0.44,"best_ask":0.46,"best_bid_no":0.53,"best_ask_no":0.55}]}'
                        ),
                        (now + timedelta(minutes=1)).isoformat(timespec="seconds"),
                    ),
                )
                conn.commit()

                token_map, mark_map, event_count = swm.build_trade_sync_maps(conn, "2026-04-14")
                conn.close()

            self.assertIn("yes-token-1", token_map)
            self.assertIn(("highest-temperature-in-test-city-on-april-14-2026", "2026-04-14", "20C", "YES"), mark_map)
            self.assertIn(("highest-temperature-in-test-city-on-april-14-2026", "2026-04-14", "20C", "NO"), mark_map)
            self.assertAlmostEqual(
                mark_map[("highest-temperature-in-test-city-on-april-14-2026", "2026-04-14", "20C", "YES")],
                0.44,
            )
            self.assertAlmostEqual(
                mark_map[("highest-temperature-in-test-city-on-april-14-2026", "2026-04-14", "20C", "NO")],
                0.53,
            )
            self.assertEqual(event_count, 1)

    def test_sync_trade_fills_accepts_data_api_rows_without_current_token_map_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            with mock.patch.object(swm, "DB_PATH", db_path):
                swm.init_db()
                conn = sqlite3.connect(db_path)
                trade_token_map = {
                    "current-token": {
                        "slug": "highest-temperature-in-test-city-on-april-14-2026",
                        "event_date": "2026-04-14",
                        "outcome": "20C",
                        "side": "YES",
                    }
                }
                data_api_payload = {
                    "trades": [
                        {
                            "tradeID": "data-api-trade-1",
                            "asset_id": "asset-old-1",
                            "side": "BUY",
                            "price": 0.42,
                            "size": 3.0,
                            "timestamp": "2026-04-09T12:00:00+00:00",
                            "eventSlug": "highest-temperature-in-toronto-on-april-9-2026",
                            "bucket_outcome": "17C",
                            "outcome_side": "NO",
                            "_source": "data_api_trades",
                        }
                    ],
                    "complete": True,
                }
                env = {
                    "POLY_API_KEY": "k",
                    "POLY_API_SECRET": "s",
                    "POLY_API_PASSPHRASE": "p",
                    "PRIVATE_KEY": "pk",
                }
                with (
                    mock.patch.object(swm, "TRADE_SYNC_DATA_API_ENABLED", True),
                    mock.patch.object(swm, "_fetch_trade_sync_data_api_trades", return_value=(True, data_api_payload)),
                    mock.patch.object(swm, "_run_node_trades", return_value=(False, {"error": "should_not_run"})),
                    mock.patch.dict(swm.os.environ, env, clear=False),
                ):
                    ok = swm.sync_trade_fills(
                        trade_token_map,
                        {},
                        lightweight=True,
                        respect_circuit=False,
                        update_status=False,
                        allow_backfill=False,
                        allow_infer=False,
                        allow_residual_clear=False,
                    )
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT slug, event_date, outcome, outcome_side, token_id, trade_id, trade_side, price, size
                    FROM edge_trade_fills
                    """
                )
                rows = cur.fetchall()
                conn.close()

            self.assertTrue(ok)
            self.assertEqual(len(rows), 1)
            self.assertEqual(
                rows[0],
                (
                    "highest-temperature-in-toronto-on-april-9-2026",
                    "2026-04-09",
                    "17C",
                    "NO",
                    "asset-old-1",
                    "data-api-trade-1",
                    "BUY",
                    0.42,
                    3.0,
                ),
            )
            self.assertIn("asset-old-1", trade_token_map)

    def test_sync_trade_fills_links_data_api_fill_without_order_id_via_auto_order_time_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            with mock.patch.object(swm, "DB_PATH", db_path):
                swm.init_db()
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO auto_orders(
                        order_id, slug, event_date, outcome, side, action, price, size, strategy, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "order-auto-1",
                        "highest-temperature-in-toronto-on-april-9-2026",
                        "2026-04-09",
                        "17C",
                        "NO",
                        "entry",
                        0.81,
                        6.64,
                        "lcb_global",
                        "2026-04-09T13:20:00+00:00",
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
                        101,
                        "2026-04-09",
                        "highest-temperature-in-toronto-on-april-9-2026",
                        "2026-04-09",
                        "Toronto 17C",
                        "17C",
                        "NO",
                        "entry",
                        "entry_submitted",
                        1,
                        '["ok"]',
                        '["ok"]',
                        '{"order_id":"order-auto-1","order_price":0.81,"order_size":6.64,"strategy":"lcb_global"}',
                        "2026-04-09T13:20:05+00:00",
                    ),
                )
                conn.commit()
                trade_token_map = {
                    "current-token": {
                        "slug": "highest-temperature-in-test-city-on-april-14-2026",
                        "event_date": "2026-04-14",
                        "outcome": "20C",
                        "side": "YES",
                    }
                }
                data_api_payload = {
                    "trades": [
                        {
                            "tradeID": "data-api-trade-link-1",
                            "asset_id": "asset-old-2",
                            "side": "BUY",
                            "price": 0.81,
                            "size": 6.64,
                            "timestamp": "2026-04-09T13:20:23+00:00",
                            "eventSlug": "highest-temperature-in-toronto-on-april-9-2026",
                            "bucket_outcome": "17C",
                            "outcome_side": "NO",
                            "_source": "data_api_trades",
                        }
                    ],
                    "complete": True,
                }
                env = {
                    "POLY_API_KEY": "k",
                    "POLY_API_SECRET": "s",
                    "POLY_API_PASSPHRASE": "p",
                    "PRIVATE_KEY": "pk",
                }
                with (
                    mock.patch.object(swm, "TRADE_SYNC_DATA_API_ENABLED", True),
                    mock.patch.object(swm, "_fetch_trade_sync_data_api_trades", return_value=(True, data_api_payload)),
                    mock.patch.object(swm, "_run_node_trades", return_value=(False, {"error": "should_not_run"})),
                    mock.patch.dict(swm.os.environ, env, clear=False),
                ):
                    ok = swm.sync_trade_fills(
                        trade_token_map,
                        {},
                        lightweight=True,
                        respect_circuit=False,
                        update_status=False,
                        allow_backfill=False,
                        allow_infer=False,
                        allow_residual_clear=False,
                    )
                cur.execute(
                    """
                    SELECT order_id, auto_order_action, auto_order_strategy, decision_id, decision_run_id, linked, reason
                    FROM trade_fill_attributions
                    WHERE trade_id = 'data-api-trade-link-1'
                    """
                )
                attribution = cur.fetchone()
                conn.close()

            self.assertTrue(ok)
            self.assertEqual(
                attribution,
                ("order-auto-1", "entry", "lcb_global", 1, 101, 1, "linked"),
            )

    def test_sync_trade_fills_dedupes_same_fill_when_trade_id_format_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            with mock.patch.object(swm, "DB_PATH", db_path):
                swm.init_db()
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO edge_trade_fills(
                        slug, event_date, outcome, outcome_side, token_id,
                        trade_id, order_id, trade_side, price, size, fee,
                        trade_time, raw_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "highest-temperature-in-toronto-on-april-9-2026",
                        "2026-04-09",
                        "17C",
                        "NO",
                        "asset-old-2",
                        "fallback-old-trade-id",
                        None,
                        "BUY",
                        0.81,
                        6.64,
                        0.0,
                        "2026-04-09T13:20:20+00:00",
                        "{}",
                        "2026-04-09T13:20:25+00:00",
                    ),
                )
                conn.commit()
                trade_token_map = {
                    "current-token": {
                        "slug": "highest-temperature-in-test-city-on-april-14-2026",
                        "event_date": "2026-04-14",
                        "outcome": "20C",
                        "side": "YES",
                    }
                }
                data_api_payload = {
                    "trades": [
                        {
                            "tradeID": "uuid-replayed-trade-id",
                            "orderID": "order-auto-1",
                            "asset_id": "asset-old-2",
                            "side": "BUY",
                            "price": 0.81,
                            "size": 6.64,
                            "timestamp": "2026-04-09T13:20:23+00:00",
                            "eventSlug": "highest-temperature-in-toronto-on-april-9-2026",
                            "bucket_outcome": "17C",
                            "outcome_side": "NO",
                            "_source": "data_api_trades",
                        }
                    ],
                    "complete": True,
                }
                env = {
                    "POLY_API_KEY": "k",
                    "POLY_API_SECRET": "s",
                    "POLY_API_PASSPHRASE": "p",
                    "PRIVATE_KEY": "pk",
                }
                with (
                    mock.patch.object(swm, "TRADE_SYNC_DATA_API_ENABLED", True),
                    mock.patch.object(swm, "_fetch_trade_sync_data_api_trades", return_value=(True, data_api_payload)),
                    mock.patch.object(swm, "_run_node_trades", return_value=(False, {"error": "should_not_run"})),
                    mock.patch.dict(swm.os.environ, env, clear=False),
                ):
                    ok = swm.sync_trade_fills(
                        trade_token_map,
                        {},
                        lightweight=True,
                        respect_circuit=False,
                        update_status=False,
                        allow_backfill=False,
                        allow_infer=False,
                        allow_residual_clear=False,
                    )
                cur.execute(
                    """
                    SELECT trade_id, order_id, price, size
                    FROM edge_trade_fills
                    ORDER BY id
                    """
                )
                rows = cur.fetchall()
                conn.close()

            self.assertTrue(ok)
            self.assertEqual(rows, [("fallback-old-trade-id", "order-auto-1", 0.81, 6.64)])

    def test_sync_trade_fills_without_trades_does_not_materialize_empty_edge_trade_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "archive.db"
            with mock.patch.object(swm, "DB_PATH", db_path):
                swm.init_db()
                conn = sqlite3.connect(db_path)
                trade_token_map = {
                    "token-yes": {
                        "slug": "highest-temperature-in-test-city-on-april-14-2026",
                        "event_date": "2026-04-14",
                        "outcome": "20C",
                        "side": "YES",
                    },
                    "token-no": {
                        "slug": "highest-temperature-in-test-city-on-april-14-2026",
                        "event_date": "2026-04-14",
                        "outcome": "20C",
                        "side": "NO",
                    },
                }
                data_api_payload = {"trades": [], "complete": False}
                env = {
                    "POLY_API_KEY": "k",
                    "POLY_API_SECRET": "s",
                    "POLY_API_PASSPHRASE": "p",
                    "PRIVATE_KEY": "pk",
                }
                with (
                    mock.patch.object(swm, "TRADE_SYNC_DATA_API_ENABLED", True),
                    mock.patch.object(swm, "_fetch_trade_sync_data_api_trades", return_value=(True, data_api_payload)),
                    mock.patch.object(swm, "_run_node_trades", return_value=(False, {"error": "should_not_run"})),
                    mock.patch.dict(swm.os.environ, env, clear=False),
                ):
                    ok = swm.sync_trade_fills(
                        trade_token_map,
                        {},
                        lightweight=True,
                        respect_circuit=False,
                        update_status=False,
                        allow_backfill=False,
                        allow_infer=False,
                        allow_residual_clear=False,
                    )
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) FROM edge_trade_fills")
                fill_count = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM edge_trades")
                edge_trade_count = cur.fetchone()[0]
                conn.close()

            self.assertTrue(ok)
            self.assertEqual(fill_count, 0)
            self.assertEqual(edge_trade_count, 0)

    def test_fetch_trade_sync_data_api_uses_direct_and_returns_soft_error(self) -> None:
        observed_kwargs = {}

        def _raise_ssl(*_args, **kwargs):
            observed_kwargs.update(kwargs)
            raise swm.requests.exceptions.SSLError("ssl eof")

        with mock.patch.dict(swm.os.environ, {"POLY_DATA_API_USER": "0xabc"}, clear=False):
            with mock.patch.object(swm, "requests_get", side_effect=_raise_ssl) as get_mock:
                ok, payload = swm._fetch_trade_sync_data_api_trades(
                    recent_after_iso="2026-04-21T00:00:00+00:00",
                    lightweight=True,
                )

        self.assertFalse(ok)
        self.assertEqual(payload.get("error"), "data_api_request_failed")
        self.assertIn("ssl eof", str(payload.get("error_detail") or ""))
        self.assertTrue(get_mock.called)
        self.assertTrue(observed_kwargs.get("force_direct"))
        self.assertEqual(
            observed_kwargs.get("timeout"),
            int(max(1, round(float(swm.TRADE_SYNC_DATA_API_TIMEOUT_SEC)))),
        )


if __name__ == "__main__":
    unittest.main()
