import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from scan_weather_market import (
    _candidate_is_plain_lcb_pinned_tail_no,
    _candidate_is_plain_lcb_zero_prob_yes,
    _load_event_market_context,
    _effective_market_refresh_event_budget_sec,
    compose_score_snapshot,
    compose_score_snapshot_with_market_bootstrap,
    _dump_reason_list_or_blank,
    _effective_stop_loss_edge_confirm_enabled,
    _effective_stop_loss_edge_confirm_fail_open_on_missing,
    _freshest_exit_quote_for_side,
    _load_market_tick_latest_by_key,
    _resolve_exit_submission_price,
    refresh_market_ticks_for_event,
)


class MarketTickFreshnessTests(unittest.TestCase):
    def test_market_tokens_updated_at_fills_freshness_when_ticks_missing(self) -> None:
        conn = sqlite3.connect(":memory:")
        cur = conn.cursor()
        cur.executescript(
            """
            CREATE TABLE event_market_ticks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT,
                event_date TEXT,
                outcome TEXT,
                side TEXT,
                created_at TEXT
            );
            CREATE TABLE market_tokens (
                slug TEXT NOT NULL,
                event_date TEXT NOT NULL,
                outcome TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        cur.execute(
            """
            INSERT INTO event_market_ticks (slug, event_date, outcome, side, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "test-slug",
                "2026-04-02",
                "11°C",
                "NO",
                "2026-04-02T10:00:00+00:00",
            ),
        )
        cur.execute(
            """
            INSERT INTO market_tokens (slug, event_date, outcome, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                "test-slug",
                "2026-04-02",
                "11°C",
                "2026-04-02T10:05:00+00:00",
            ),
        )

        latest = _load_market_tick_latest_by_key(conn, "test-slug", "2026-04-02")

        self.assertIn(("11°C", "YES"), latest)
        self.assertIn(("11°C", "NO"), latest)
        self.assertEqual(latest[("11°C", "YES")].isoformat(), "2026-04-02T10:05:00+00:00")
        self.assertEqual(latest[("11°C", "NO")].isoformat(), "2026-04-02T10:05:00+00:00")

    def test_freshest_exit_quote_prefers_latest_tick_over_row(self) -> None:
        conn = sqlite3.connect(":memory:")
        cur = conn.cursor()
        cur.executescript(
            """
            CREATE TABLE event_market_ticks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT,
                event_date TEXT,
                outcome TEXT,
                side TEXT,
                best_bid REAL,
                best_ask REAL,
                bid_depth_topN REAL,
                ask_depth_topN REAL,
                created_at TEXT
            );
            """
        )
        cur.execute(
            """
            INSERT INTO event_market_ticks (
                slug, event_date, outcome, side, best_bid, best_ask, bid_depth_topN, ask_depth_topN, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "test-slug",
                "2026-04-02",
                "11°C",
                "NO",
                0.002,
                0.004,
                2000.0,
                900.0,
                "2026-04-02T10:05:00+00:00",
            ),
        )
        row = {
            "best_bid_no": 0.27,
            "best_ask_no": 0.31,
            "bid_depth_topN_no": 10.0,
            "ask_depth_topN_no": 20.0,
        }
        best_bid, best_ask, bid_depth, ask_depth = _freshest_exit_quote_for_side(
            conn,
            "test-slug",
            "2026-04-02",
            "11°C",
            "NO",
            row,
        )
        self.assertAlmostEqual(best_bid, 0.002)
        self.assertAlmostEqual(best_ask, 0.004)
        self.assertAlmostEqual(bid_depth, 2000.0)
        self.assertAlmostEqual(ask_depth, 900.0)

    def test_resolve_exit_submission_price_uses_one_sided_ask_floor(self) -> None:
        row = {
            "best_bid_no": 0.0,
            "best_ask_no": 0.001,
        }
        exit_price, used_ask, reason, best_bid, best_ask = _resolve_exit_submission_price(
            None,
            0.0,
            0.001,
            side="NO",
            row_ref=row,
            allow_ask_fallback=False,
        )
        self.assertAlmostEqual(exit_price, 0.001)
        self.assertTrue(used_ask)
        self.assertEqual(reason, "one_sided_ask_only")
        self.assertAlmostEqual(best_bid, 0.0)
        self.assertAlmostEqual(best_ask, 0.001)

    def test_resolve_exit_submission_price_can_use_complement_quote(self) -> None:
        row = {
            "best_bid": 0.999,
            "best_ask": 1.0,
        }
        exit_price, used_ask, reason, best_bid, best_ask = _resolve_exit_submission_price(
            None,
            None,
            None,
            side="NO",
            row_ref=row,
            allow_ask_fallback=False,
        )
        self.assertAlmostEqual(exit_price, 0.001)
        self.assertTrue(used_ask)
        self.assertEqual(reason, "one_sided_ask_only_complement")
        self.assertAlmostEqual(best_bid, 0.0)
        self.assertAlmostEqual(best_ask, 0.001)

    def test_effective_stop_loss_edge_confirm_kept_for_plain_lcb(self) -> None:
        self.assertTrue(
            _effective_stop_loss_edge_confirm_enabled(
                True,
                lcb_exit_context_active=True,
                lcb_ignore_edge_confirm=True,
            )
        )

    def test_effective_stop_loss_edge_confirm_disabled_for_lcb_stop_loss_only(self) -> None:
        self.assertFalse(
            _effective_stop_loss_edge_confirm_enabled(
                True,
                lcb_exit_context_active=True,
                lcb_ignore_edge_confirm=True,
                lcb_stop_loss_only=True,
            )
        )

    def test_effective_stop_loss_edge_confirm_kept_for_non_lcb(self) -> None:
        self.assertTrue(
            _effective_stop_loss_edge_confirm_enabled(
                True,
                lcb_exit_context_active=False,
                lcb_ignore_edge_confirm=True,
            )
        )

    def test_effective_stop_loss_fail_open_disabled_for_plain_lcb(self) -> None:
        self.assertFalse(
            _effective_stop_loss_edge_confirm_fail_open_on_missing(
                True,
                lcb_exit_context_active=True,
                lcb_stop_loss_only=False,
            )
        )

    def test_effective_stop_loss_fail_open_kept_for_lcb_stop_loss_only(self) -> None:
        self.assertTrue(
            _effective_stop_loss_edge_confirm_fail_open_on_missing(
                True,
                lcb_exit_context_active=True,
                lcb_stop_loss_only=True,
            )
        )

    def test_market_refresh_cache_only_bootstraps_tokens_when_cache_empty(self) -> None:
        conn = sqlite3.connect(":memory:")
        cur = conn.cursor()
        cur.executescript(
            """
            CREATE TABLE market_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date TEXT,
                event_date TEXT,
                slug TEXT,
                payload_json TEXT,
                meta_json TEXT,
                created_at TEXT
            );
            CREATE TABLE market_tokens (
                slug TEXT NOT NULL,
                event_date TEXT NOT NULL,
                outcome TEXT NOT NULL,
                yes_token TEXT,
                no_token TEXT,
                outcome_volume REAL,
                liquidity REAL,
                market_active INTEGER,
                market_closed INTEGER,
                market_archived INTEGER,
                market_accepting_orders INTEGER,
                market_enable_orderbook INTEGER,
                updated_at TEXT,
                PRIMARY KEY (slug, event_date, outcome)
            );
            """
        )
        args = SimpleNamespace(
            market_refresh_only=True,
            market_refresh_max_outcomes=0,
            market_refresh_time_budget_sec=0,
            market_refresh_clob_timeout_sec=1,
            market_refresh_clob_retries=1,
            depth_levels=5,
            tz="America/New_York",
        )
        outcome_tokens = [
            {
                "label": "10C",
                "yes_token": "yes-10c",
                "no_token": "no-10c",
                "outcome_volume": 123.0,
                "liquidity": 456.0,
                "market_active": True,
                "market_closed": False,
                "market_archived": False,
                "market_accepting_orders": True,
                "market_enable_orderbook": True,
            }
        ]
        fake_book = {"bids": [{"price": "0.40", "size": "10"}], "asks": [{"price": "0.42", "size": "12"}]}
        with (
            patch("scan_weather_market.init_db"),
            patch("scan_weather_market.open_db", return_value=conn),
            patch("scan_weather_market.fetch_gamma_event_by_slug", return_value={"id": "evt"}),
            patch("scan_weather_market.parse_outcomes_and_tokens_from_event", return_value=outcome_tokens),
            patch("scan_weather_market.fetch_orderbook", return_value=fake_book),
            patch("scan_weather_market._save_event_run_from_market_refresh"),
            patch("scan_weather_market.save_market_ticks"),
            patch(
                "scan_weather_market.best_bid_ask_and_depth",
                return_value=(0.40, 0.42, 0.02, 10.0, 12.0, 10.0, 12.0),
            ),
        ):
            ok, err, count, rows, _created_at = refresh_market_ticks_for_event(
                "test-slug",
                "2026-04-14",
                args,
                None,
            )

        self.assertTrue(ok)
        self.assertIsNone(err)
        self.assertEqual(count, 1)
        self.assertEqual(len(rows or []), 1)
        saved = cur.execute(
            """
            SELECT yes_token, no_token
            FROM market_tokens
            WHERE slug = ? AND event_date = ? AND outcome = ?
            """,
            ("test-slug", "2026-04-14", "10C"),
        ).fetchone()
        conn.close()
        self.assertEqual(saved, ("yes-10c", "no-10c"))

    def test_market_refresh_budget_stops_before_second_side_fetch(self) -> None:
        conn = sqlite3.connect(":memory:")
        args = SimpleNamespace(
            market_refresh_only=True,
            market_refresh_max_outcomes=0,
            market_refresh_time_budget_sec=1,
            market_refresh_clob_timeout_sec=1,
            market_refresh_clob_retries=1,
            depth_levels=5,
            tz="America/New_York",
        )
        outcome_tokens = [
            {
                "label": "10C",
                "yes_token": "yes-10c",
                "no_token": "no-10c",
                "market_active": True,
                "market_closed": False,
                "market_archived": False,
                "market_accepting_orders": True,
                "market_enable_orderbook": True,
            },
            {
                "label": "11C",
                "yes_token": "yes-11c",
                "no_token": "no-11c",
                "market_active": True,
                "market_closed": False,
                "market_archived": False,
                "market_accepting_orders": True,
                "market_enable_orderbook": True,
            },
        ]
        fetch_calls = []

        def _fake_monotonic() -> float:
            _fake_monotonic.calls += 1
            if _fake_monotonic.calls <= 2:
                return 0.0
            return 1.2

        _fake_monotonic.calls = 0

        def _raise_no_book(token_id, *args, **kwargs):
            fetch_calls.append(str(token_id))
            raise RuntimeError("orderbook_unavailable status=404")

        with (
            patch.dict(
                "os.environ",
                {
                    "PM_SCAN_MARKET_REFRESH_ALLOW_TOKEN_REMAP": "0",
                    "PM_SCAN_MARKET_REFRESH_ALLOW_GAMMA_PRICE_FALLBACK": "0",
                    "PM_SCAN_MARKET_REFRESH_ALLOW_LAST_TICK_FALLBACK": "0",
                },
                clear=False,
            ),
            patch("scan_weather_market.init_db"),
            patch("scan_weather_market.open_db", return_value=conn),
            patch("scan_weather_market._load_cached_market_tokens", return_value=outcome_tokens),
            patch("scan_weather_market.fetch_orderbook", side_effect=_raise_no_book),
            patch("scan_weather_market.save_market_ticks"),
            patch("scan_weather_market.save_market_snapshot"),
            patch("scan_weather_market._save_event_run_from_market_refresh"),
            patch("scan_weather_market.time.monotonic", side_effect=_fake_monotonic),
        ):
            ok, err, count, rows, _created_at = refresh_market_ticks_for_event(
                "test-slug",
                "2026-04-14",
                args,
                None,
            )

        conn.close()
        self.assertFalse(ok)
        self.assertTrue(str(err or "").startswith("no_orderbook_quotes:"))
        self.assertEqual(count, 0)
        self.assertIsNone(rows)
        self.assertEqual(fetch_calls, ["yes-10c"])

    def test_market_refresh_fast_mode_disables_heavy_fallbacks(self) -> None:
        conn = sqlite3.connect(":memory:")
        args = SimpleNamespace(
            market_refresh_only=True,
            market_refresh_max_outcomes=1,
            market_refresh_time_budget_sec=10,
            market_refresh_clob_timeout_sec=1,
            market_refresh_clob_retries=1,
            depth_levels=5,
            tz="America/New_York",
        )
        outcome_tokens = [
            {
                "label": "10C",
                "yes_token": "yes-10c",
                "no_token": "no-10c",
                "market_active": True,
                "market_closed": False,
                "market_archived": False,
                "market_accepting_orders": True,
                "market_enable_orderbook": True,
            }
        ]
        fetch_kwargs = []

        def _fake_fetch_orderbook(token_id, *args, **kwargs):
            fetch_kwargs.append(dict(kwargs))
            return {"bids": [{"price": "0.40", "size": "10"}], "asks": [{"price": "0.42", "size": "12"}]}

        with (
            patch.dict("os.environ", {}, clear=True),
            patch("scan_weather_market.init_db"),
            patch("scan_weather_market.open_db", return_value=conn),
            patch("scan_weather_market._load_cached_market_tokens", return_value=outcome_tokens),
            patch("scan_weather_market.fetch_orderbook", side_effect=_fake_fetch_orderbook),
            patch("scan_weather_market.save_market_ticks"),
            patch("scan_weather_market.save_market_snapshot"),
            patch("scan_weather_market._save_event_run_from_market_refresh"),
            patch(
                "scan_weather_market.best_bid_ask_and_depth",
                return_value=(0.40, 0.42, 0.02, 10.0, 12.0, 10.0, 12.0),
            ),
        ):
            ok, err, count, rows, _created_at = refresh_market_ticks_for_event(
                "test-slug",
                "2026-04-14",
                args,
                None,
            )

        conn.close()
        self.assertTrue(ok)
        self.assertIsNone(err)
        self.assertEqual(count, 1)
        self.assertEqual(len(rows or []), 1)
        self.assertGreaterEqual(len(fetch_kwargs), 1)
        self.assertFalse(fetch_kwargs[0]["price_augment_enabled"])
        self.assertFalse(fetch_kwargs[0]["apply_global_rate_limit"])

    def test_market_refresh_rolls_back_when_market_snapshot_save_fails(self) -> None:
        conn = sqlite3.connect(":memory:")
        cur = conn.cursor()
        cur.executescript(
            """
            CREATE TABLE event_market_ticks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date TEXT,
                event_date TEXT,
                slug TEXT,
                outcome TEXT,
                side TEXT,
                best_bid REAL,
                best_ask REAL,
                mid REAL,
                spread REAL,
                bid_depth_topN REAL,
                ask_depth_topN REAL,
                created_at TEXT
            );
            """
        )
        args = SimpleNamespace(
            market_refresh_only=True,
            market_refresh_max_outcomes=1,
            market_refresh_time_budget_sec=10,
            market_refresh_clob_timeout_sec=1,
            market_refresh_clob_retries=1,
            depth_levels=5,
            tz="America/New_York",
        )
        outcome_tokens = [
            {
                "label": "10C",
                "yes_token": "yes-10c",
                "no_token": "no-10c",
                "market_active": True,
                "market_closed": False,
                "market_archived": False,
                "market_accepting_orders": True,
                "market_enable_orderbook": True,
            }
        ]
        fake_book = {"bids": [{"price": "0.40", "size": "10"}], "asks": [{"price": "0.42", "size": "12"}]}

        with (
            patch("scan_weather_market.init_db"),
            patch("scan_weather_market.open_db", return_value=conn),
            patch("scan_weather_market._load_cached_market_tokens", return_value=outcome_tokens),
            patch("scan_weather_market.fetch_orderbook", return_value=fake_book),
            patch("scan_weather_market.save_market_snapshot", return_value=False),
            patch(
                "scan_weather_market.best_bid_ask_and_depth",
                return_value=(0.40, 0.42, 0.02, 10.0, 12.0, 10.0, 12.0),
            ),
        ):
            ok, err, count, rows, _created_at = refresh_market_ticks_for_event(
                "test-slug",
                "2026-04-14",
                args,
                None,
            )

        self.assertFalse(ok)
        self.assertEqual(err, "db_write_incomplete")
        self.assertEqual(count, 0)
        self.assertIsNone(rows)
        persisted = cur.execute("SELECT COUNT(*) FROM event_market_ticks").fetchone()[0]
        conn.close()
        self.assertEqual(persisted, 0)

    def test_effective_market_refresh_event_budget_respects_run_deadline(self) -> None:
        args = SimpleNamespace(
            market_refresh_only=True,
            market_refresh_time_budget_sec=8,
        )
        budget = _effective_market_refresh_event_budget_sec(
            args,
            run_deadline_monotonic=15.0,
            now_monotonic=10.0,
        )
        self.assertEqual(budget, 5.0)

    def test_effective_market_refresh_event_budget_keeps_event_budget_when_smaller(self) -> None:
        args = SimpleNamespace(
            market_refresh_only=True,
            market_refresh_time_budget_sec=3,
        )
        budget = _effective_market_refresh_event_budget_sec(
            args,
            run_deadline_monotonic=20.0,
            now_monotonic=10.0,
        )
        self.assertEqual(budget, 3.0)

    def test_compose_score_snapshot_allows_model_only_fallback_with_market_rows(self) -> None:
        conn = sqlite3.connect(":memory:")
        cur = conn.cursor()
        cur.executescript(
            """
            CREATE TABLE model_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date TEXT,
                event_date TEXT,
                slug TEXT,
                payload_json TEXT,
                meta_json TEXT,
                created_at TEXT
            );
            CREATE TABLE market_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date TEXT,
                event_date TEXT,
                slug TEXT,
                payload_json TEXT,
                meta_json TEXT,
                created_at TEXT
            );
            CREATE TABLE event_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date TEXT,
                event_date TEXT,
                slug TEXT,
                payload_json TEXT,
                created_at TEXT
            );
            """
        )
        cur.execute(
            """
            INSERT INTO model_snapshots (run_date, event_date, slug, payload_json, meta_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "2026-04-13",
                "2026-04-14",
                "test-slug",
                '{"rows":[{"outcome":"10C","q_raw":0.62,"q_entry":0.61,"q_calibrated":0.63}]}',
                '{"event_url":"https://polymarket.com/event/test-slug","title":"Test Market"}',
                "2026-04-13T14:00:00+00:00",
            ),
        )
        cur.execute(
            """
            INSERT INTO event_runs (run_date, event_date, slug, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "2026-04-13",
                "2026-04-14",
                "test-slug",
                '{"rows":[{"outcome":"10C","best_bid":0.40,"best_ask":0.42,"spread":0.02,"bid_depth_topN":10.0,"ask_depth_topN":12.0}]}',
                "2026-04-13T13:58:00+00:00",
            ),
        )
        archive_calls = []

        def _capture_archive(run_date, event_date, result, suggestions, meta):
            archive_calls.append((run_date, event_date, result, suggestions, meta))

        with (
            patch("scan_weather_market.init_db"),
            patch("scan_weather_market.open_db", return_value=conn),
            patch("scan_weather_market.archive_run", side_effect=_capture_archive),
        ):
            ok, err = compose_score_snapshot(
                "2026-04-13",
                "2026-04-14",
                "test-slug",
                SimpleNamespace(),
                meta_hint=None,
            )

        self.assertTrue(ok)
        self.assertIsNone(err)
        self.assertEqual(len(archive_calls), 1)
        result = archive_calls[0][2]
        row = result["rows"][0]
        self.assertEqual(result["score_composer"]["source"], "model_only_snapshot_fallback")
        self.assertAlmostEqual(row["q_calibrated"], 0.63)
        self.assertAlmostEqual(row["best_ask"], 0.42)
        self.assertAlmostEqual(row["signal_edge_round"], 0.21)

    def test_compose_score_snapshot_with_market_bootstrap_retries_missing_snapshot(self) -> None:
        conn = sqlite3.connect(":memory:")
        compose_side_effects = [(False, "missing_snapshot"), (True, None)]
        with (
            patch("scan_weather_market.init_db"),
            patch("scan_weather_market.open_db", return_value=conn),
            patch("scan_weather_market.compose_score_snapshot", side_effect=compose_side_effects) as compose_mock,
            patch(
                "scan_weather_market.refresh_market_ticks_for_event",
                return_value=(
                    True,
                    None,
                    1,
                    [{"outcome": "10C", "best_bid": 0.40, "best_ask": 0.42}],
                    "2026-04-13T14:20:00+00:00",
                ),
            ) as refresh_mock,
            patch("scan_weather_market.save_market_snapshot") as save_market_snapshot_mock,
        ):
            ok, err = compose_score_snapshot_with_market_bootstrap(
                "2026-04-13",
                "2026-04-14",
                "test-slug",
                SimpleNamespace(),
                meta_hint={"event_url": "https://polymarket.com/event/test-slug", "title": "Test Market"},
            )
        conn.close()

        self.assertTrue(ok)
        self.assertIsNone(err)
        self.assertEqual(compose_mock.call_count, 2)
        refresh_mock.assert_called_once()
        save_market_snapshot_mock.assert_called_once()

    def test_load_event_market_context_falls_back_to_cached_tokens_and_hint(self) -> None:
        conn = sqlite3.connect(":memory:")
        cur = conn.cursor()
        cur.executescript(
            """
            CREATE TABLE market_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date TEXT,
                event_date TEXT,
                slug TEXT,
                payload_json TEXT,
                meta_json TEXT,
                created_at TEXT
            );
            CREATE TABLE market_tokens (
                slug TEXT NOT NULL,
                event_date TEXT NOT NULL,
                outcome TEXT NOT NULL,
                yes_token TEXT,
                no_token TEXT,
                outcome_volume REAL,
                liquidity REAL,
                market_active INTEGER,
                market_closed INTEGER,
                market_archived INTEGER,
                market_accepting_orders INTEGER,
                market_enable_orderbook INTEGER,
                updated_at TEXT,
                PRIMARY KEY (slug, event_date, outcome)
            );
            """
        )
        cur.execute(
            """
            INSERT INTO market_tokens (
                slug, event_date, outcome, yes_token, no_token, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "test-slug",
                "2026-04-14",
                "10C",
                "yes-10c",
                "no-10c",
                "2026-04-13T14:20:00+00:00",
            ),
        )
        with (
            patch("scan_weather_market.init_db"),
            patch("scan_weather_market.open_db", return_value=conn),
            patch("scan_weather_market.fetch_gamma_event_by_slug", side_effect=RuntimeError("gamma_ssl")),
            patch("scan_weather_market.fetch_gamma_market_by_slug", side_effect=RuntimeError("market_ssl")),
        ):
            (
                outcome_tokens,
                title,
                resolution_source,
                description,
                _event_ref,
                source,
                errors,
            ) = _load_event_market_context(
                "test-slug",
                "2026-04-14",
                event_hint={
                    "title": "Test Market",
                    "resolutionSource": "Resolve via station",
                    "description": "Test description",
                },
            )
        conn.close()

        self.assertEqual(source, "cached_tokens")
        self.assertEqual(title, "Test Market")
        self.assertEqual(resolution_source, "Resolve via station")
        self.assertEqual(description, "Test description")
        self.assertEqual(len(outcome_tokens), 1)
        self.assertEqual(outcome_tokens[0]["label"], "10C")
        self.assertTrue(any("event_fetch=gamma_ssl" in item for item in errors))
        self.assertTrue(any("market_fetch=market_ssl" in item for item in errors))

    def test_plain_lcb_pinned_tail_no_q_only_prefilter_hits_high_confidence_missing_quote(self) -> None:
        candidate = {
            "strategy": "lcb_global",
            "side": "NO",
            "q_entry": 0.999,
            "q_round": 0.999,
            "q_raw": 0.999,
            "ask": None,
            "order_price": None,
        }
        self.assertTrue(_candidate_is_plain_lcb_pinned_tail_no(candidate))

    def test_plain_lcb_pinned_tail_no_q_only_prefilter_skips_non_extreme_missing_quote(self) -> None:
        candidate = {
            "strategy": "lcb_global",
            "side": "NO",
            "q_entry": 0.97,
            "q_round": 0.97,
            "q_raw": 0.97,
            "ask": None,
            "order_price": None,
        }
        self.assertFalse(_candidate_is_plain_lcb_pinned_tail_no(candidate))

    def test_plain_lcb_pinned_tail_no_prefilter_hits_bid_only_high_confidence(self) -> None:
        candidate = {
            "strategy": "lcb_global",
            "side": "NO",
            "q_entry": 1.0,
            "q_round": 1.0,
            "q_raw": 1.0,
            "ask": None,
            "bid": 0.998,
            "order_price": 0.9985,
        }
        self.assertTrue(_candidate_is_plain_lcb_pinned_tail_no(candidate))

    def test_plain_lcb_zero_prob_yes_prefilter_hits(self) -> None:
        candidate = {
            "strategy": "lcb_global",
            "side": "YES",
            "q_entry": 0.0,
            "q_round": 0.0,
            "q_raw": 0.0,
        }
        self.assertTrue(_candidate_is_plain_lcb_zero_prob_yes(candidate))

    def test_plain_lcb_zero_prob_yes_prefilter_skips_non_zero(self) -> None:
        candidate = {
            "strategy": "lcb_global",
            "side": "YES",
            "q_entry": 0.02,
            "q_round": 0.02,
            "q_raw": 0.02,
        }
        self.assertFalse(_candidate_is_plain_lcb_zero_prob_yes(candidate))

    def test_dump_reason_list_or_blank_clears_empty(self) -> None:
        self.assertEqual(_dump_reason_list_or_blank([]), "")
        self.assertEqual(_dump_reason_list_or_blank(None), "")

    def test_dump_reason_list_or_blank_keeps_values(self) -> None:
        self.assertEqual(
            _dump_reason_list_or_blank(["stop_loss", "edge_min"]),
            '["stop_loss", "edge_min"]',
        )


if __name__ == "__main__":
    unittest.main()
