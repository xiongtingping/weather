import json
import inspect
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import scan_weather_market as swm


class TestAutoTradeRotation(unittest.TestCase):
    def test_direct_job_post_end_allows_open_market_snapshot_only(self) -> None:
        payload = {
            "event_end_utc": "2000-01-01T00:00:00Z",
            "market_closed": 0,
            "rows": [
                {
                    "outcome": "32\u00b0C",
                    "market_active": True,
                    "market_closed": False,
                    "market_archived": False,
                    "market_accepting_orders": True,
                    "market_enable_orderbook": True,
                }
            ],
        }

        self.assertTrue(swm._payload_blocks_new_entry(payload))
        self.assertFalse(
            swm._payload_blocks_new_entry(
                payload,
                allow_post_end_if_open_market=True,
            )
        )

        closed_payload = dict(payload)
        closed_payload["rows"] = [dict(payload["rows"][0], market_closed=True)]
        self.assertTrue(
            swm._payload_blocks_new_entry(
                closed_payload,
                allow_post_end_if_open_market=True,
            )
        )

    def test_direct_job_post_end_allows_executable_target_snapshot(self) -> None:
        payload = {
            "event_end_utc": "2000-01-01T00:00:00Z",
            "market_closed": 1,
            "posterior_target_direct": {"target_bucket": "32\u00b0C"},
            "entry_expected_targets": [
                {
                    "outcome": "32\u00b0C",
                    "side": "YES",
                    "entry_ok": True,
                    "posterior_target_direct": True,
                    "target_realization_leg_role": "posterior_target_direct_target",
                    "token_id": "token-32-yes",
                    "best_ask": 0.26,
                }
            ],
            "rows": [{"outcome": "32\u00b0C", "q_entry": 1.0}],
        }

        self.assertTrue(swm._payload_blocks_new_entry(payload))
        self.assertFalse(
            swm._payload_blocks_new_entry(
                payload,
                allow_post_end_if_open_market=True,
                allow_post_end_if_direct_target=True,
            )
        )

    def test_rising_ask_fast_path_does_not_hide_normal_entry_path(self) -> None:
        source = inspect.getsource(swm.execute_auto_trades)
        rising_idx = source.index("if entry_market_on_rising_ask and edge_score_market_ok:")
        normal_idx = source.index("if order_price >= 1:", rising_idx)
        rising_line = source[rising_idx:].splitlines()[0]
        normal_line = source[normal_idx:].splitlines()[0]

        self.assertEqual(
            len(rising_line) - len(rising_line.lstrip()),
            len(normal_line) - len(normal_line.lstrip()),
        )
        self.assertNotIn(
            "\n                        continue\n                        if order_price >= 1:",
            source[rising_idx:normal_idx + len("if order_price >= 1:")],
        )

    def test_execute_auto_trades_initializes_event_date_key_before_expected_targets(self) -> None:
        source = inspect.getsource(swm.execute_auto_trades)
        event_date_key_idx = source.index("event_date_key = event_date.split")
        expected_targets_idx = source.index(
            'raw_expected_targets = result.get("entry_expected_targets")'
        )

        self.assertLess(event_date_key_idx, expected_targets_idx)

    def test_direct_target_primary_entry_skips_old_leg_order_confirm_first(self) -> None:
        source = inspect.getsource(swm.execute_auto_trades)

        self.assertIn(
            "def _direct_target_primary_entry_pending_before_old_leg",
            source,
        )
        self.assertIn(
            '"PM_TARGET_REALIZATION_DIRECT_SKIP_OLD_CONFIRM_BEFORE_PRIMARY"',
            source,
        )
        self.assertIn(
            "and not entry_confirm_skip_direct_primary_pending",
            source[
                source.index('prev_state == "entry"')
                : source.index('prev_state in {"entry", "entry_pending", "entry_failed"}')
            ],
        )
        self.assertIn(
            "and not entry_confirm_skip_direct_primary_pending",
            source[
                source.index('prev_last_action == "exit"')
                : source.index('_event_stage_end("exit_open_order_probe"')
            ],
        )

    def test_posterior_target_direct_primary_entry_waives_retry_cooldown(self) -> None:
        source = inspect.getsource(swm.execute_auto_trades)
        waiver_start = source.index("def _maybe_waive_entry_retry_block")
        waiver_end = source.index("def _parse_depth_block_since", waiver_start)
        waiver_block = source[waiver_start:waiver_end]

        self.assertIn(
            '"PM_POSTERIOR_TARGET_DIRECT_WAIVE_RETRY_ON_PRIMARY_ENTRY"',
            waiver_block,
        )
        self.assertIn("_is_posterior_target_direct_buy_new_target", waiver_block)
        self.assertIn("posterior_target_direct_primary_entry_missing", waiver_block)
        self.assertLess(
            waiver_block.index("_is_posterior_target_direct_buy_new_target"),
            waiver_block.index("_is_marketable_buy_amount_error"),
        )

    def test_posterior_target_direct_does_not_waive_previous_place_order_failure(self) -> None:
        source = inspect.getsource(swm.execute_auto_trades)
        waiver_start = source.index("def _maybe_waive_entry_retry_block")
        waiver_end = source.index("def _parse_depth_block_since", waiver_start)
        waiver_block = source[waiver_start:waiver_end]

        self.assertIn("previous_real_order_failure", waiver_block)
        self.assertIn("place-order", waiver_block)
        self.assertIn("missing_order_id", waiver_block)
        self.assertIn(
            "posterior_target_direct_previous_place_order_failure",
            waiver_block,
        )
        self.assertLess(
            waiver_block.index("if previous_real_order_failure:"),
            waiver_block.index("posterior_target_direct_primary_entry_missing"),
        )

    def test_ssot_candidate_preserves_posterior_target_direct_provenance(self) -> None:
        source = inspect.getsource(swm.execute_auto_trades)
        merge_start = source.index("direct_expected_ref = expected_entry_targets_by_key.get")
        merge_end = source.index("candidate_map[ssot_side] = ssot_candidate", merge_start)
        merge_block = source[merge_start:merge_end]

        self.assertIn("_is_posterior_target_direct_buy_new_target", merge_block)
        self.assertIn('ssot_candidate["posterior_target_direct"] = True', merge_block)
        self.assertIn('ssot_candidate["strategy_source"] = "posterior_target_direct"', merge_block)
        self.assertIn('"posterior_target_direct_target"', merge_block)

    def test_ssot_fallback_targets_preserve_tradeability_and_rules_fields(self) -> None:
        targets = swm._build_fallback_targets_from_ssot(
            {
                "main_candidate": {
                    "strategy": "lcb_global",
                    "outcome": "29C or higher",
                    "side": "NO",
                    "entry_ok": True,
                    "edge_score": 12.0,
                    "exec_edge_entry": 0.42,
                    "edge_tradeability_pass": True,
                    "edge_signal_lcb": 0.36,
                    "edge_path_p_reach_lcb": 0.80,
                    "edge_tradeability_profit_value": 0.18,
                    "rules_contract_ok": True,
                    "station_id": "MMMX",
                    "source_usable_for_settlement": True,
                    "weather_strategy_rules_ok": True,
                    "weather_strategy_source_usable_for_settlement": True,
                }
            }
        )

        self.assertEqual(len(targets), 1)
        target = targets[0]
        self.assertEqual(target["strategy"], "lcb_global")
        self.assertTrue(target["edge_tradeability_pass"])
        self.assertAlmostEqual(float(target["edge_signal_lcb"]), 0.36, places=9)
        self.assertTrue(target["rules_contract_ok"])
        self.assertEqual(target["station_id"], "MMMX")
        self.assertTrue(target["source_usable_for_settlement"])

    def test_entry_expected_target_merge_fill_preserves_primary_target_reason(self) -> None:
        merged = swm._merge_entry_expected_targets(
            [
                {
                    "outcome": "25°C",
                    "side": "YES",
                    "strategy": "lcb_global",
                    "entry_ok": True,
                    "entry_reasons": ["target_realization_switch"],
                    "ask": None,
                }
            ],
            [
                {
                    "outcome": "25°C",
                    "side": "YES",
                    "strategy": "main",
                    "entry_ok": False,
                    "entry_reasons": ["fallback_ssot"],
                    "ask": 0.42,
                }
            ],
        )

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["strategy"], "lcb_global")
        self.assertTrue(merged[0]["entry_ok"])
        self.assertAlmostEqual(float(merged[0]["ask"]), 0.42)
        self.assertEqual(
            merged[0]["entry_reasons"],
            ["target_realization_switch", "fallback_ssot"],
        )

    def test_hard_zero_old_bucket_exit_is_not_deferred_for_entry_priority(self) -> None:
        row = {
            "q_bayes_posterior": 0.0,
            "q_posterior_observed_constraints_hard_zero": True,
            "q_posterior_observed_constraints_hard_zero_reasons": [
                "observed_today_max_above_bucket",
                "hard_source_lower_bound_constraint",
            ],
            "ssot_target_bucket": "31°C",
            "target_bucket": "31°C",
            "second_target_bucket": "32°C",
            "target_posterior": 0.58,
            "second_target_posterior": 0.31,
            "target_realization_path_p_reach_yes": 0.74,
            "target_realization_reliability": 0.66,
            "target_realization_path_reliability_state": "medium",
            "target_realization_trade_follow_allowed": True,
            "target_realization_observation_age_minutes": 3.5,
            "target_realization_ui_risk_explanation_text": (
                "目标 31°C posterior 58.0%，路径可兑现 74.0%。"
            ),
            "target_realization_hard_source_state": {
                "available": True,
                "observed_max_c": 31.1,
                "source": "zuuu_metar_raw_awc",
            },
        }

        should_defer = swm._should_defer_target_switch_exit_for_entry_priority(
            exit_only=False,
            target_switch_expected_entry_keys={("31°C", "YES")},
            outcome_label="30°C",
            side_label="YES",
            row_ref=row,
            exit_codes=["target_realization_switch_buy_exit_old"],
        )

        self.assertFalse(should_defer)
        self.assertEqual(row["target_switch_entry_priority_override"], "hard_zero_old_bucket_exit")
        meta = swm._target_realization_hard_zero_exit_meta(
            row,
            "YES",
            ["target_realization_switch_buy_exit_old"],
        )
        self.assertTrue(meta["target_realization_hard_zero_exit"])
        context = meta["target_realization_hard_zero_exit_context"]
        self.assertEqual(context["ssot_target_bucket"], "31°C")
        self.assertEqual(context["second_target_bucket"], "32°C")
        self.assertAlmostEqual(context["target_posterior"], 0.58)
        self.assertAlmostEqual(context["second_target_posterior"], 0.31)
        self.assertAlmostEqual(context["target_realization_path_p_reach_yes"], 0.74)
        self.assertEqual(context["target_realization_path_reliability_state"], "medium")
        self.assertAlmostEqual(context["target_realization_observation_age_minutes"], 3.5)
        self.assertIn("路径可兑现", context["target_realization_ui_risk_explanation_text"])
        self.assertEqual(
            context["target_realization_hard_source_state"]["source"],
            "zuuu_metar_raw_awc",
        )

    def test_post_peak_near_settlement_guard_blocks_chengdu_near_100_yes(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {
                "PM_AUTO_TRADE_BLOCK_POST_PEAK_NEAR_CERTAIN_ENTRY": "1",
                "PM_AUTO_TRADE_POST_PEAK_NEAR_CERTAIN_PRICE_MIN": "0.99",
            },
            clear=False,
        ):
            blocked, detail = swm._entry_post_peak_near_settlement_price_block_context(
                slug="highest-temperature-in-chengdu-on-june-17-2026",
                title="Highest temperature in Chengdu on June 17?",
                market_var="temperature_2m_max",
                event_date="2026-06-17",
                row_ref={
                    "outcome": "25°C",
                    "best_ask": 0.999,
                    "tz": "Asia/Shanghai",
                },
                candidate_ref={
                    "strategy": swm.POSTERIOR_SETTLEMENT_FAMILY,
                    "local_peak_hour": 16.0,
                    "price": 0.999,
                },
                side="YES",
                order_price=0.999,
                market_price=0.999,
                order_size=9.4,
                order_type="FAK",
                submit_source="entry_main",
                now_dt=datetime(2026, 6, 17, 8, 30, tzinfo=timezone.utc),
            )

        self.assertTrue(blocked)
        self.assertEqual(
            detail["post_peak_near_settlement_price_guard_reason"],
            swm.POST_PEAK_NEAR_SETTLEMENT_ENTRY_REASON,
        )
        self.assertEqual(detail["post_peak_near_settlement_price_timezone"], "Asia/Shanghai")
        self.assertAlmostEqual(float(detail["entry_effective_price"]), 0.999, places=9)
        self.assertGreaterEqual(
            float(detail["post_peak_near_settlement_price_hours_after_peak"]),
            0.0,
        )
        self.assertTrue(detail["post_peak_near_settlement_price_execution_guard_only"])
        self.assertTrue(detail["target_selection_inputs_unchanged"])

    def test_post_peak_near_settlement_guard_bypasses_posterior_target_direct(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {
                "PM_AUTO_TRADE_BLOCK_POST_PEAK_NEAR_CERTAIN_ENTRY": "1",
                "PM_AUTO_TRADE_POST_PEAK_NEAR_CERTAIN_PRICE_MIN": "0.99",
            },
            clear=False,
        ):
            blocked, detail = swm._entry_post_peak_near_settlement_price_block_context(
                slug="highest-temperature-in-chengdu-on-june-17-2026",
                title="Highest temperature in Chengdu on June 17?",
                market_var="temperature_2m_max",
                event_date="2026-06-17",
                row_ref={
                    "outcome": "30°C or higher",
                    "best_ask": 0.999,
                    "tz": "Asia/Shanghai",
                },
                candidate_ref={
                    "strategy": swm.TARGET_REALIZATION_ENTRY_STRATEGY,
                    "strategy_family": swm.POSTERIOR_SETTLEMENT_FAMILY,
                    "strategy_source": "posterior_target_direct",
                    "posterior_target_direct": True,
                    "target_adjustment_decision_version": "posterior_target_direct_v1",
                    "local_peak_hour": 16.0,
                    "price": 0.999,
                },
                side="YES",
                order_price=0.999,
                market_price=0.999,
                order_size=9.4,
                order_type="FAK",
                submit_source="entry_main",
                now_dt=datetime(2026, 6, 17, 8, 30, tzinfo=timezone.utc),
            )

        self.assertFalse(blocked)
        self.assertEqual(
            detail["post_peak_near_settlement_price_guard_reason"],
            "posterior_target_direct_bypass",
        )
        self.assertTrue(
            detail["post_peak_near_settlement_price_bypassed_for_posterior_target_direct"]
        )
        self.assertTrue(detail["target_selection_inputs_unchanged"])

    def test_post_peak_near_settlement_guard_can_enforce_posterior_target_direct(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {
                "PM_AUTO_TRADE_BLOCK_POST_PEAK_NEAR_CERTAIN_ENTRY": "1",
                "PM_AUTO_TRADE_POST_PEAK_NEAR_CERTAIN_PRICE_MIN": "0.99",
                "PM_POSTERIOR_TARGET_DIRECT_BYPASS_POST_PEAK_PRICE_GUARD": "0",
            },
            clear=False,
        ):
            blocked, detail = swm._entry_post_peak_near_settlement_price_block_context(
                slug="highest-temperature-in-chengdu-on-june-17-2026",
                title="Highest temperature in Chengdu on June 17?",
                market_var="temperature_2m_max",
                event_date="2026-06-17",
                row_ref={"outcome": "30°C or higher", "best_ask": 0.999, "tz": "Asia/Shanghai"},
                candidate_ref={
                    "strategy": swm.TARGET_REALIZATION_ENTRY_STRATEGY,
                    "strategy_family": swm.POSTERIOR_SETTLEMENT_FAMILY,
                    "strategy_source": "posterior_target_direct",
                    "posterior_target_direct": True,
                    "local_peak_hour": 16.0,
                    "price": 0.999,
                },
                side="YES",
                order_price=0.999,
                market_price=0.999,
                order_size=9.4,
                order_type="FAK",
                submit_source="entry_main",
                now_dt=datetime(2026, 6, 17, 8, 30, tzinfo=timezone.utc),
            )

        self.assertTrue(blocked)
        self.assertEqual(
            detail["post_peak_near_settlement_price_guard_reason"],
            swm.POST_PEAK_NEAR_SETTLEMENT_ENTRY_REASON,
        )

    def test_post_peak_near_settlement_guard_allows_below_threshold(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {
                "PM_AUTO_TRADE_BLOCK_POST_PEAK_NEAR_CERTAIN_ENTRY": "1",
                "PM_AUTO_TRADE_POST_PEAK_NEAR_CERTAIN_PRICE_MIN": "0.99",
            },
            clear=False,
        ):
            blocked, detail = swm._entry_post_peak_near_settlement_price_block_context(
                slug="highest-temperature-in-chengdu-on-june-17-2026",
                title="Highest temperature in Chengdu on June 17?",
                market_var="temperature_2m_max",
                event_date="2026-06-17",
                row_ref={"best_ask": 0.98, "tz": "Asia/Shanghai"},
                candidate_ref={"local_peak_hour": 16.0, "price": 0.98},
                side="YES",
                order_price=0.98,
                market_price=0.98,
                submit_source="entry_main",
                now_dt=datetime(2026, 6, 17, 8, 30, tzinfo=timezone.utc),
            )

        self.assertFalse(blocked)
        self.assertEqual(
            detail["post_peak_near_settlement_price_guard_reason"],
            "entry_price_below_threshold",
        )

    def test_post_peak_near_settlement_reason_stays_hard_in_realizable_mode(self) -> None:
        hard_codes, soft_codes = swm._normalize_entry_block_reason_codes(
            [swm.POST_PEAK_NEAR_SETTLEMENT_ENTRY_REASON],
            entry_only_realizable=True,
        )

        self.assertEqual(hard_codes, [swm.POST_PEAK_NEAR_SETTLEMENT_ENTRY_REASON])
        self.assertEqual(soft_codes, [])

    def test_post_peak_near_settlement_details_survive_entry_compaction(self) -> None:
        details = {
            "entry_effective_price": 0.999,
            "entry_effective_price_source": "order_price",
            "entry_order_submit_source": "entry_main",
            "price_for_order": 0.999,
            "market_price": 0.999,
            "order_amount": 9.35,
            "post_peak_near_settlement_price_guard_enabled": True,
            "post_peak_near_settlement_price_guard_reason": swm.POST_PEAK_NEAR_SETTLEMENT_ENTRY_REASON,
            "post_peak_near_settlement_price_threshold": 0.99,
            "post_peak_near_settlement_price_timezone": "Asia/Shanghai",
            "post_peak_near_settlement_price_local_time": "2026-06-17T17:30:00+08:00",
            "post_peak_near_settlement_price_peak_hour_local": 16.0,
            "post_peak_near_settlement_price_hours_after_peak": 1.5,
            "post_peak_near_settlement_price_execution_guard_only": True,
            "target_selection_inputs_unchanged": True,
            "q_bayes_model_evidence": {"ecmwf": "x" * 400, "gfs": "y" * 400},
            "weather_strategy_debug_blob": "z" * 400,
        }

        with mock.patch.dict(
            "os.environ",
            {
                "PM_DECISION_DETAILS_COMPACT": "1",
                "PM_DECISION_DETAILS_COMPACT_MAX_BYTES": "512",
            },
            clear=False,
        ):
            compacted = swm._compact_decision_details(
                details,
                action="entry",
                will_order=False,
            )

        self.assertEqual(compacted["entry_effective_price"], 0.999)
        self.assertEqual(
            compacted["post_peak_near_settlement_price_guard_reason"],
            swm.POST_PEAK_NEAR_SETTLEMENT_ENTRY_REASON,
        )
        self.assertEqual(
            compacted["post_peak_near_settlement_price_local_time"],
            "2026-06-17T17:30:00+08:00",
        )
        self.assertEqual(
            compacted["post_peak_near_settlement_price_peak_hour_local"],
            16.0,
        )
        self.assertTrue(compacted["post_peak_near_settlement_price_execution_guard_only"])
        self.assertTrue(compacted["target_selection_inputs_unchanged"])

    def test_fact_rows_rotate_when_events_per_job_max_is_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "archive.db"
            data_dir = root / "data"
            data_dir.mkdir(parents=True, exist_ok=True)

            with mock.patch.object(swm, "DB_PATH", db_path), mock.patch.object(swm, "DATA_DIR", data_dir):
                swm.init_db()
                run_date = "2026-04-14"
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS event_fact_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_date TEXT NOT NULL,
                        slug TEXT NOT NULL,
                        event_date TEXT NOT NULL,
                        fact_as_of TEXT NOT NULL,
                        best_edge_score REAL,
                        composer_version TEXT NOT NULL,
                        fact_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                for idx, slug in enumerate(["event-a", "event-b", "event-c"], start=1):
                    fact_json = json.dumps(
                        {
                            "slug": slug,
                            "rows": [{"outcome": f"{idx}C", "q_entry": 0.5, "q_calibrated": 0.5}],
                        },
                        ensure_ascii=True,
                    )
                    cur.execute(
                        """
                        INSERT INTO event_fact_history(
                            run_date, slug, event_date, fact_as_of, best_edge_score, composer_version, fact_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run_date,
                            slug,
                            run_date,
                            f"{run_date}T0{idx}:00:00+00:00",
                            float(10 - idx),
                            "test",
                            fact_json,
                            f"{run_date}T0{idx}:00:00+00:00",
                        ),
                    )
                conn.commit()
                conn.close()

                seen_calls = []

                def _capture_execute(run_date_key, event_date_key, payload, **kwargs):
                    seen_calls.append((str(payload.get("slug") or ""), event_date_key))

                with mock.patch.dict(
                    "os.environ",
                    {
                        "PM_AUTO_TRADE_EVENTS_PER_JOB_MAX": "2",
                        "PM_AUTO_TRADE_EVENT_TIMEOUT_SEC": "0",
                        "PM_AUTO_TRADE_EVENT_BACKOFF_SEC": "0",
                    },
                    clear=False,
                ), mock.patch.object(swm, "execute_auto_trades", side_effect=_capture_execute):
                    swm.run_auto_trades_for_date(run_date)
                    first_run = list(seen_calls)
                    seen_calls.clear()
                    swm.run_auto_trades_for_date(run_date)
                    second_run = list(seen_calls)

            self.assertEqual(len(first_run), 2)
            self.assertEqual(len(second_run), 2)
            self.assertNotEqual(first_run, second_run)
            self.assertEqual({slug for slug, _ in first_run + second_run}, {"event-a", "event-b", "event-c"})

    def test_job_payload_include_event_keys_filters_auto_trade_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "archive.db"
            data_dir = root / "data"
            data_dir.mkdir(parents=True, exist_ok=True)

            with mock.patch.object(swm, "DB_PATH", db_path), mock.patch.object(swm, "DATA_DIR", data_dir):
                swm.init_db()
                run_date = "2026-04-14"
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS event_fact_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_date TEXT NOT NULL,
                        slug TEXT NOT NULL,
                        event_date TEXT NOT NULL,
                        fact_as_of TEXT NOT NULL,
                        best_edge_score REAL,
                        composer_version TEXT NOT NULL,
                        fact_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                for idx, slug in enumerate(["event-a", "event-b", "event-c"], start=1):
                    fact_json = json.dumps(
                        {
                            "slug": slug,
                            "event_date": run_date,
                            "rows": [{"outcome": f"{idx}C", "q_entry": 0.5, "q_calibrated": 0.5}],
                        },
                        ensure_ascii=True,
                    )
                    cur.execute(
                        """
                        INSERT INTO event_fact_history(
                            run_date, slug, event_date, fact_as_of, best_edge_score, composer_version, fact_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run_date,
                            slug,
                            run_date,
                            f"{run_date}T0{idx}:00:00+00:00",
                            float(10 - idx),
                            "test",
                            fact_json,
                            f"{run_date}T0{idx}:00:00+00:00",
                        ),
                    )
                conn.commit()
                conn.close()

                seen_calls = []

                def _capture_execute(run_date_key, event_date_key, payload, **kwargs):
                    seen_calls.append((str(payload.get("slug") or ""), event_date_key))

                with mock.patch.dict(
                    "os.environ",
                    {
                        "PM_AUTO_TRADE_EVENTS_PER_JOB_MAX": "0",
                        "PM_AUTO_TRADE_EVENT_TIMEOUT_SEC": "0",
                        "PM_AUTO_TRADE_EVENT_BACKOFF_SEC": "0",
                    },
                    clear=False,
                ), mock.patch.object(swm, "execute_auto_trades", side_effect=_capture_execute):
                    result = swm.run_auto_trades_for_date(
                        run_date,
                        job_payload={
                            "include_event_keys": [
                                {"slug": "event-b", "event_date": run_date},
                            ]
                        },
                    )

            self.assertEqual(seen_calls, [("event-b", run_date)])
            self.assertEqual(result.get("processed"), 1)
            self.assertEqual(result.get("job_payload_filter"), {"include_slugs": 0, "include_event_keys": 1})

    def test_job_payload_include_event_keys_filter_runs_before_rotation_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "archive.db"
            data_dir = root / "data"
            data_dir.mkdir(parents=True, exist_ok=True)

            with mock.patch.object(swm, "DB_PATH", db_path), mock.patch.object(swm, "DATA_DIR", data_dir):
                swm.init_db()
                run_date = "2026-04-14"
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS event_fact_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_date TEXT NOT NULL,
                        slug TEXT NOT NULL,
                        event_date TEXT NOT NULL,
                        fact_as_of TEXT NOT NULL,
                        best_edge_score REAL,
                        composer_version TEXT NOT NULL,
                        fact_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                for idx, slug in enumerate(["event-a", "event-b", "event-c"], start=1):
                    fact_json = json.dumps(
                        {
                            "slug": slug,
                            "event_date": run_date,
                            "rows": [{"outcome": f"{idx}C", "q_entry": 0.5, "q_calibrated": 0.5}],
                        },
                        ensure_ascii=True,
                    )
                    cur.execute(
                        """
                        INSERT INTO event_fact_history(
                            run_date, slug, event_date, fact_as_of, best_edge_score, composer_version, fact_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run_date,
                            slug,
                            run_date,
                            f"{run_date}T0{idx}:00:00+00:00",
                            0.0,
                            "test",
                            fact_json,
                            f"{run_date}T0{idx}:00:00+00:00",
                        ),
                    )
                conn.commit()
                conn.close()

                seen_calls = []

                def _capture_execute(run_date_key, event_date_key, payload, **kwargs):
                    seen_calls.append((str(payload.get("slug") or ""), event_date_key))

                with mock.patch.dict(
                    "os.environ",
                    {
                        "PM_AUTO_TRADE_EVENTS_PER_JOB_MAX": "1",
                        "PM_AUTO_TRADE_EVENT_TIMEOUT_SEC": "0",
                        "PM_AUTO_TRADE_EVENT_BACKOFF_SEC": "0",
                    },
                    clear=False,
                ), mock.patch.object(swm, "execute_auto_trades", side_effect=_capture_execute):
                    result = swm.run_auto_trades_for_date(
                        run_date,
                        job_payload={
                            "include_event_keys": [
                                {"slug": "event-b", "event_date": run_date},
                            ]
                        },
                    )

            self.assertTrue(result.get("ok"))
            self.assertEqual(seen_calls, [("event-b", run_date)])
            self.assertEqual(result.get("processed"), 1)
            self.assertEqual(result.get("job_payload_filter"), {"include_slugs": 0, "include_event_keys": 1})

    def test_job_payload_include_event_keys_empty_run_fails_instead_of_succeeding(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "archive.db"
            data_dir = root / "data"
            data_dir.mkdir(parents=True, exist_ok=True)

            with mock.patch.object(swm, "DB_PATH", db_path), mock.patch.object(swm, "DATA_DIR", data_dir):
                swm.init_db()
                run_date = "2026-04-14"
                seen_calls = []

                def _capture_execute(run_date_key, event_date_key, payload, **kwargs):
                    seen_calls.append((str(payload.get("slug") or ""), event_date_key))

                with mock.patch.dict(
                    "os.environ",
                    {
                        "PM_AUTO_TRADE_EVENTS_PER_JOB_MAX": "1",
                        "PM_AUTO_TRADE_EVENT_TIMEOUT_SEC": "0",
                        "PM_AUTO_TRADE_EVENT_BACKOFF_SEC": "0",
                    },
                    clear=False,
                ), mock.patch.object(swm, "execute_auto_trades", side_effect=_capture_execute):
                    result = swm.run_auto_trades_for_date(
                        run_date,
                        job_payload={
                            "include_event_keys": [
                                {"slug": "missing-event", "event_date": run_date},
                            ]
                        },
                    )

            self.assertFalse(result.get("ok"))
            self.assertEqual(result.get("processed"), 0)
            self.assertEqual(result.get("error"), "job_payload_include_event_keys_not_processed")
            self.assertEqual(seen_calls, [])

    def test_job_payload_entry_expected_targets_are_merged_into_fact_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "archive.db"
            data_dir = root / "data"
            data_dir.mkdir(parents=True, exist_ok=True)

            with mock.patch.object(swm, "DB_PATH", db_path), mock.patch.object(swm, "DATA_DIR", data_dir):
                swm.init_db()
                run_date = "2026-06-17"
                slug = "highest-temperature-in-chengdu-on-june-17-2026"
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS event_fact_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_date TEXT NOT NULL,
                        slug TEXT NOT NULL,
                        event_date TEXT NOT NULL,
                        fact_as_of TEXT NOT NULL,
                        best_edge_score REAL,
                        composer_version TEXT NOT NULL,
                        fact_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                fact_json = json.dumps(
                    {
                        "slug": slug,
                        "event_date": run_date,
                        "rows": [
                            {
                                "outcome": "25°C",
                                "yes_token": "yes-25",
                                "q_entry": 0.99,
                                "q_calibrated": 0.99,
                            }
                        ],
                        "auto_trade_ssot": {
                            "main_candidate": {
                                "strategy": "lcb_global",
                                "outcome": "26°C",
                                "side": "YES",
                                "entry_ok": True,
                                "entry_reasons": ["fallback_ssot"],
                            }
                        },
                    },
                    ensure_ascii=True,
                )
                cur.execute(
                    """
                    INSERT INTO event_fact_history(
                        run_date, slug, event_date, fact_as_of, best_edge_score, composer_version, fact_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_date,
                        slug,
                        run_date,
                        f"{run_date}T08:00:00+00:00",
                        10.0,
                        "test",
                        fact_json,
                        f"{run_date}T08:00:00+00:00",
                    ),
                )
                conn.commit()
                conn.close()

                seen_payloads = []

                def _capture_execute(_run_date_key, _event_date_key, payload, **_kwargs):
                    seen_payloads.append(payload)

                with mock.patch.dict(
                    "os.environ",
                    {
                        "PM_AUTO_TRADE_EVENTS_PER_JOB_MAX": "0",
                        "PM_AUTO_TRADE_EVENT_TIMEOUT_SEC": "0",
                        "PM_AUTO_TRADE_EVENT_BACKOFF_SEC": "0",
                    },
                    clear=False,
                ), mock.patch.object(swm, "execute_auto_trades", side_effect=_capture_execute):
                    result = swm.run_auto_trades_for_date(
                        run_date,
                        job_payload={
                            "include_event_keys": [{"slug": slug, "event_date": run_date}],
                            "entry_expected_targets": [
                                {
                                    "outcome": "25°C",
                                    "side": "YES",
                                    "strategy": "lcb_global",
                                    "entry_ok": True,
                                    "entry_reasons": ["target_realization_switch"],
                                }
                            ],
                        },
                    )

            self.assertEqual(result.get("processed"), 1)
            self.assertEqual(len(seen_payloads), 1)
            targets = seen_payloads[0].get("entry_expected_targets") or []
            target_keys = {(item.get("outcome"), item.get("side")) for item in targets}
            self.assertIn(("25°C", "YES"), target_keys)
            self.assertIn(("26°C", "YES"), target_keys)
            target_25 = next(item for item in targets if item.get("outcome") == "25°C")
            self.assertIn("target_realization_switch", target_25.get("entry_reasons") or [])

    def test_direct_job_payload_uses_inline_snapshot_fast_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "archive.db"
            data_dir = root / "data"
            data_dir.mkdir(parents=True, exist_ok=True)

            run_date = "2026-06-30"
            slug = "highest-temperature-in-chengdu-on-june-30-2026"
            target = {
                "slug": slug,
                "event_date": run_date,
                "outcome": "30°C or higher",
                "side": "YES",
                "strategy": "target_realization_binary_settlement",
                "entry_ok": True,
                "entry_reasons": ["posterior_target_direct", "target_realization_order_intent"],
                "posterior_target_direct": True,
                "token_id": "yes-token-30",
                "ask": 0.42,
            }
            event_payload = {
                "slug": slug,
                "event_date": run_date,
                "posterior_target_direct_snapshot": True,
                "posterior_target_direct": {
                    "job_enqueued_at": "2026-06-29T17:35:08+00:00",
                    "target_bucket": "30°C or higher",
                },
                "entry_expected_targets": [dict(target)],
                "rows": [
                    {
                        "outcome": "30°C or higher",
                        "yes_token": "yes-token-30",
                        "best_ask": 0.42,
                        "q_entry": 0.58,
                        "q_calibrated": 0.58,
                    }
                ],
            }
            job_payload = {
                "include_event_keys": [{"slug": slug, "event_date": run_date}],
                "entry_expected_targets": [dict(target)],
                "posterior_target_direct": {
                    "job_enqueued_at": "2026-06-29T17:35:08+00:00",
                    "target_bucket": "30°C or higher",
                },
                "event_payloads": [
                    {"slug": slug, "event_date": run_date, "payload": event_payload}
                ],
            }

            seen_payloads = []
            seen_kwargs = []

            def _capture_execute(_run_date_key, event_date_key, payload, **_kwargs):
                seen_payloads.append((event_date_key, payload))
                seen_kwargs.append(dict(_kwargs))
                self.assertEqual(os.environ.get("PM_AUTO_TRADE_ENTRY_PREFETCH_MARKET_TICKS"), "0")
                self.assertEqual(os.environ.get("PM_AUTO_TRADE_ENTRY_REFRESH_ON_STALE"), "0")
                self.assertEqual(os.environ.get("PM_AUTO_TRADE_SYNC_OPEN_ORDERS"), "0")

            with mock.patch.object(swm, "DB_PATH", db_path), mock.patch.object(swm, "DATA_DIR", data_dir):
                swm.init_db()
                with mock.patch.dict(
                    "os.environ",
                    {
                        "PM_AUTO_TRADE_ENABLED": "1",
                        "PM_AUTO_TRADE_EVENT_TIMEOUT_SEC": "60",
                        "PM_AUTO_TRADE_EVENT_BACKOFF_SEC": "0",
                        "PM_AUTO_TRADE_DIRECT_JOB_INLINE": "1",
                    },
                    clear=False,
                ), mock.patch.object(
                    swm,
                    "_auto_trade_load_fact_rows",
                    side_effect=AssertionError("direct job must not load full fact rows"),
                ), mock.patch.object(
                    swm,
                    "_refresh_payload_rows_with_latest_market_snapshot",
                    side_effect=AssertionError("direct job must not refresh snapshot rows by default"),
                ), mock.patch.object(swm, "execute_auto_trades", side_effect=_capture_execute):
                    result = swm.run_auto_trades_for_date(run_date, job_payload=job_payload)

            self.assertTrue(result.get("ok"))
            self.assertTrue(result.get("direct_job_fast_path"))
            self.assertTrue(result.get("direct_job_inline"))
            self.assertEqual(result.get("processed"), 1)
            self.assertEqual(result.get("job_payload_filter"), {"include_slugs": 0, "include_event_keys": 1})
            self.assertEqual(len(seen_payloads), 1)
            self.assertEqual(len(seen_kwargs), 1)
            self.assertTrue(seen_kwargs[0].get("direct_job_context"))
            self.assertEqual(seen_kwargs[0].get("settled_reconcile_context"), {"done": True, "source": "direct_job_fast_path"})
            self.assertEqual(seen_payloads[0][0], run_date)
            payload_seen = seen_payloads[0][1]
            targets_seen = payload_seen.get("entry_expected_targets") or []
            self.assertEqual((targets_seen[0].get("outcome"), targets_seen[0].get("side")), ("30°C or higher", "YES"))

    def test_direct_job_primary_entry_fast_slice_defers_old_and_remote_legs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "archive.db"
            data_dir = root / "data"
            data_dir.mkdir(parents=True, exist_ok=True)

            run_date = "2026-07-01"
            slug = "highest-temperature-in-chengdu-on-july-1-2026"
            primary = {
                "slug": slug,
                "event_date": run_date,
                "outcome": "32°C",
                "side": "YES",
                "strategy": "target_realization_binary_settlement",
                "strategy_source": "posterior_target_direct",
                "target_realization_leg_role": "posterior_target_direct_target",
                "entry_ok": True,
                "entry_reasons": ["posterior_target_direct", "target_realization_order_intent"],
                "posterior_target_direct": True,
                "target_bucket": "32°C",
                "token_id": "yes-token-32",
            }
            old_yes = {
                **primary,
                "outcome": "31°C",
                "target_realization_leg_role": "old_bucket_reverse",
                "token_id": "yes-token-31",
            }
            remote_no = {
                **primary,
                "outcome": "29°C",
                "side": "NO",
                "target_realization_leg_role": "remote_no",
                "token_id": "no-token-29",
            }
            event_payload = {
                "slug": slug,
                "event_date": run_date,
                "posterior_target_direct_snapshot": True,
                "posterior_target_direct": {
                    "target_bucket": "32°C",
                    "primary_only_fast_path_allowed": False,
                },
                "entry_expected_targets": [dict(primary), dict(old_yes), dict(remote_no)],
                "rows": [
                    {"outcome": "31°C", "yes_token": "yes-token-31"},
                    {"outcome": "32°C", "yes_token": "yes-token-32"},
                    {"outcome": "29°C", "no_token": "no-token-29"},
                ],
                "distribution": [
                    {"outcome": "31°C", "yes_token": "yes-token-31"},
                    {"outcome": "32°C", "yes_token": "yes-token-32"},
                    {"outcome": "29°C", "no_token": "no-token-29"},
                ],
            }
            job_payload = {
                "include_event_keys": [{"slug": slug, "event_date": run_date}],
                "entry_expected_targets": [dict(primary), dict(old_yes), dict(remote_no)],
                "posterior_target_direct": {
                    "target_bucket": "32°C",
                    "primary_only_fast_path_allowed": False,
                },
                "event_payloads": [{"slug": slug, "event_date": run_date, "payload": event_payload}],
            }

            seen_payloads = []

            def _capture_execute(_run_date_key, _event_date_key, payload, **_kwargs):
                seen_payloads.append(payload)

            with mock.patch.object(swm, "DB_PATH", db_path), mock.patch.object(swm, "DATA_DIR", data_dir):
                swm.init_db()
                with mock.patch.dict(
                    "os.environ",
                    {
                        "PM_AUTO_TRADE_ENABLED": "1",
                        "PM_AUTO_TRADE_EVENT_TIMEOUT_SEC": "60",
                        "PM_AUTO_TRADE_EVENT_BACKOFF_SEC": "0",
                        "PM_AUTO_TRADE_DIRECT_JOB_INLINE": "1",
                    },
                    clear=False,
                ), mock.patch.object(swm, "execute_auto_trades", side_effect=_capture_execute):
                    result = swm.run_auto_trades_for_date(run_date, job_payload=job_payload)

            self.assertTrue(result.get("ok"))
            self.assertTrue(result.get("direct_target_primary_entry_fast_slice"))
            self.assertGreaterEqual(int(result.get("direct_target_deferred_target_count") or 0), 2)
            self.assertEqual(len(seen_payloads), 1)
            targets_seen = seen_payloads[0].get("entry_expected_targets") or []
            self.assertEqual([(item.get("outcome"), item.get("side")) for item in targets_seen], [("32°C", "YES")])
            self.assertEqual([row.get("outcome") for row in seen_payloads[0].get("rows") or []], ["32°C"])
            self.assertEqual([row.get("outcome") for row in seen_payloads[0].get("distribution") or []], ["32°C"])

    def test_direct_job_primary_only_targets_still_slice_full_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "archive.db"
            data_dir = root / "data"
            data_dir.mkdir(parents=True, exist_ok=True)

            run_date = "2026-07-01"
            slug = "highest-temperature-in-chengdu-on-july-1-2026"
            primary = {
                "slug": slug,
                "event_date": run_date,
                "outcome": "32°C",
                "side": "YES",
                "strategy": "target_realization_binary_settlement",
                "strategy_source": "posterior_target_direct",
                "target_realization_leg_role": "posterior_target_direct_target",
                "entry_ok": True,
                "entry_reasons": ["posterior_target_direct", "target_realization_order_intent"],
                "posterior_target_direct": True,
                "target_bucket": "32°C",
                "token_id": "yes-token-32",
            }
            event_payload = {
                "slug": slug,
                "event_date": run_date,
                "posterior_target_direct_snapshot": True,
                "posterior_target_direct": {"target_bucket": "32°C"},
                "entry_expected_targets": [dict(primary)],
                "rows": [
                    {"outcome": "31°C", "yes_token": "yes-token-31"},
                    {"outcome": "32°C", "yes_token": "yes-token-32"},
                    {"outcome": "33°C", "yes_token": "yes-token-33"},
                ],
                "distribution": [
                    {"outcome": "31°C", "yes_token": "yes-token-31"},
                    {"outcome": "32°C", "yes_token": "yes-token-32"},
                    {"outcome": "33°C", "yes_token": "yes-token-33"},
                ],
            }
            job_payload = {
                "include_event_keys": [{"slug": slug, "event_date": run_date}],
                "entry_expected_targets": [dict(primary)],
                "posterior_target_direct": {"target_bucket": "32°C"},
                "event_payloads": [{"slug": slug, "event_date": run_date, "payload": event_payload}],
            }

            seen_payloads = []

            def _capture_execute(_run_date_key, _event_date_key, payload, **_kwargs):
                seen_payloads.append(payload)

            with mock.patch.object(swm, "DB_PATH", db_path), mock.patch.object(swm, "DATA_DIR", data_dir):
                swm.init_db()
                with mock.patch.dict(
                    "os.environ",
                    {
                        "PM_AUTO_TRADE_ENABLED": "1",
                        "PM_AUTO_TRADE_EVENT_TIMEOUT_SEC": "60",
                        "PM_AUTO_TRADE_EVENT_BACKOFF_SEC": "0",
                        "PM_AUTO_TRADE_DIRECT_JOB_INLINE": "1",
                    },
                    clear=False,
                ), mock.patch.object(swm, "execute_auto_trades", side_effect=_capture_execute):
                    result = swm.run_auto_trades_for_date(run_date, job_payload=job_payload)

            self.assertTrue(result.get("ok"))
            self.assertTrue(result.get("direct_target_primary_entry_fast_slice"))
            self.assertEqual(int(result.get("direct_target_deferred_target_count") or 0), 0)
            self.assertEqual(len(seen_payloads), 1)
            self.assertEqual([row.get("outcome") for row in seen_payloads[0].get("rows") or []], ["32°C"])
            self.assertEqual([row.get("outcome") for row in seen_payloads[0].get("distribution") or []], ["32°C"])

    def test_direct_job_disabled_writes_no_submit_without_full_execute(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "archive.db"
            data_dir = root / "data"
            data_dir.mkdir(parents=True, exist_ok=True)

            run_date = "2026-06-30"
            slug = "highest-temperature-in-chengdu-on-june-30-2026"
            target = {
                "slug": slug,
                "event_date": run_date,
                "outcome": "30°C or higher",
                "side": "YES",
                "strategy": "target_realization_binary_settlement",
                "entry_ok": True,
                "entry_reasons": ["posterior_target_direct"],
                "posterior_target_direct": True,
                "token_id": "yes-token-30",
            }
            event_payload = {
                "slug": slug,
                "event_date": run_date,
                "title": "Highest temperature in Chengdu on June 30?",
                "posterior_target_direct_snapshot": True,
                "posterior_target_direct": {"target_bucket": "30°C or higher"},
                "entry_expected_targets": [dict(target)],
                "rows": [{"outcome": "30°C or higher", "yes_token": "yes-token-30"}],
            }
            job_payload = {
                "include_event_keys": [{"slug": slug, "event_date": run_date}],
                "entry_expected_targets": [dict(target)],
                "posterior_target_direct": {"target_bucket": "30°C or higher"},
                "event_payloads": [{"slug": slug, "event_date": run_date, "payload": event_payload}],
            }
            recorded = []
            claim_env = {}

            def _record_disabled(**kwargs):
                recorded.append(kwargs)
                return 1

            with mock.patch.object(swm, "DB_PATH", db_path), mock.patch.object(swm, "DATA_DIR", data_dir):
                swm.init_db()
                with mock.patch.dict(
                    "os.environ",
                    {
                        "PM_AUTO_TRADE_ENABLED": "0",
                        "PM_AUTO_TRADE_DRY_RUN": "0",
                        "PM_AUTO_TRADE_DIRECT_JOB_INLINE": "1",
                    },
                    clear=False,
                ), mock.patch.object(
                    swm,
                    "_auto_trade_load_fact_rows",
                    side_effect=AssertionError("direct job must not load full fact rows"),
                ), mock.patch.object(
                    swm,
                    "execute_auto_trades",
                    side_effect=AssertionError("disabled direct job must not run full executor"),
                ), mock.patch.object(
                    swm,
                    "_record_auto_trade_fallback_decisions",
                    side_effect=_record_disabled,
                ):
                    result = swm.run_auto_trades_for_date(run_date, job_payload=job_payload)

            self.assertTrue(result.get("ok"))
            self.assertTrue(result.get("direct_job_fast_path"))
            self.assertEqual(result.get("processed"), 1)
            self.assertEqual(len(recorded), 1)
            self.assertEqual(recorded[0]["reason_code"], "auto_trade_disabled_direct_target_no_submit")
            self.assertEqual(recorded[0]["targets"][0]["outcome"], "30°C or higher")

    def test_worker_direct_job_bypasses_slow_locks_when_auto_trade_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "archive.db"
            data_dir = root / "data"
            data_dir.mkdir(parents=True, exist_ok=True)

            run_date = swm.today_date_str("America/New_York")
            slug = "highest-temperature-in-chengdu-on-june-30-2026"
            target = {
                "slug": slug,
                "event_date": run_date,
                "outcome": "30°C or higher",
                "side": "YES",
                "strategy": "target_realization_binary_settlement",
                "entry_ok": True,
                "entry_reasons": ["posterior_target_direct"],
                "posterior_target_direct": True,
                "token_id": "yes-token-30",
            }
            event_payload = {
                "slug": slug,
                "event_date": run_date,
                "title": "Highest temperature in Chengdu on June 30?",
                "posterior_target_direct_snapshot": True,
                "posterior_target_direct": {"target_bucket": "30°C or higher"},
                "entry_expected_targets": [dict(target)],
            }
            job_payload = {
                "include_event_keys": [{"slug": slug, "event_date": run_date}],
                "entry_expected_targets": [dict(target)],
                "posterior_target_direct": {"target_bucket": "30°C or higher"},
                "event_payloads": [{"slug": slug, "event_date": run_date, "payload": event_payload}],
            }
            recorded = []
            claim_env = {}

            def _record_disabled(**kwargs):
                recorded.append(kwargs)
                return 1

            with mock.patch.object(swm, "DB_PATH", db_path), mock.patch.object(swm, "DATA_DIR", data_dir):
                swm.init_db()
                enqueue_result = swm.enqueue_auto_trade_job(
                    run_date,
                    source="posterior_target_direct",
                    priority=15000,
                    payload=job_payload,
                    max_retry=0,
                    force_enqueue=True,
                )
                self.assertTrue(enqueue_result.get("ok"))
                self.assertTrue(enqueue_result.get("enqueued"))
                original_claim_one = swm._auto_trade_job_claim_one

                def _claim_one_capture_env(**_kwargs):
                    claim_env.update(
                        {
                            "lease": os.environ.get("PM_AUTO_TRADE_JOB_LEASE_SEC"),
                            "stuck": os.environ.get("PM_AUTO_TRADE_JOB_STUCK_SEC"),
                            "orphan": os.environ.get("PM_AUTO_TRADE_JOB_ORPHAN_GRACE_SEC"),
                            "backoff": os.environ.get("PM_AUTO_TRADE_JOB_BACKOFF_SEC"),
                            "connect": os.environ.get("PM_DB_CONNECT_TIMEOUT_SEC"),
                            "busy": os.environ.get("PM_DB_BUSY_TIMEOUT_MS"),
                            "skip_cleanup": os.environ.get("PM_AUTO_TRADE_JOB_SKIP_CLEANUP_ON_CLAIM"),
                            "decision_attempts": os.environ.get("PM_AUTO_TRADE_DECISION_WRITE_RETRY_ATTEMPTS"),
                            "decision_sleep": os.environ.get("PM_AUTO_TRADE_DECISION_WRITE_RETRY_SLEEP_SEC"),
                        }
                    )
                    return original_claim_one()

                with mock.patch.dict(
                    "os.environ",
                    {
                        "PM_AUTO_TRADE_ENABLED": "0",
                        "PM_AUTO_TRADE_DRY_RUN": "0",
                        "PM_AUTO_TRADE_DIRECT_JOB_INLINE": "1",
                    },
                    clear=False,
                ), mock.patch.object(
                    swm,
                    "_wait_for_facts_lock",
                    side_effect=AssertionError("direct job must not wait for facts lock"),
                ), mock.patch.object(
                    swm,
                    "_wait_for_db_writer_lock",
                    side_effect=AssertionError("direct job must not wait for db writer lock"),
                ), mock.patch.object(
                    swm,
                    "_auto_trade_runtime_lock_acquire",
                    side_effect=AssertionError("disabled direct job must not take runtime lock"),
                ), mock.patch.object(
                    swm,
                    "_auto_trade_job_claim_one",
                    side_effect=_claim_one_capture_env,
                ), mock.patch.object(
                    swm,
                    "run_auto_trades_for_date",
                    side_effect=AssertionError("disabled direct worker must not run full trade flow"),
                ), mock.patch.object(
                    swm,
                    "_record_auto_trade_fallback_decisions",
                    side_effect=_record_disabled,
                ):
                    result = swm.run_auto_trade_job_worker_once()

                with sqlite3.connect(db_path) as conn:
                    row = conn.execute(
                        "SELECT state, run_id FROM auto_trade_jobs WHERE id = ?",
                        (int((enqueue_result.get("job") or {}).get("id") or 0),),
                    ).fetchone()

            self.assertTrue(result.get("ok"))
            self.assertTrue(result.get("picked"))
            self.assertTrue(result.get("result", {}).get("direct_target_disabled_fast_path"))
            self.assertEqual(result.get("result", {}).get("processed"), 1)
            self.assertEqual(len(recorded), 1)
            self.assertEqual(recorded[0]["reason_code"], "auto_trade_disabled_direct_target_no_submit")
            self.assertEqual(claim_env.get("lease"), "30")
            self.assertEqual(claim_env.get("stuck"), "30")
            self.assertEqual(claim_env.get("orphan"), "15")
            self.assertEqual(claim_env.get("backoff"), "1.000")
            self.assertEqual(claim_env.get("connect"), "0.250")
            self.assertEqual(claim_env.get("busy"), "250")
            self.assertEqual(claim_env.get("skip_cleanup"), "1")
            self.assertEqual(claim_env.get("decision_attempts"), "1")
            self.assertEqual(claim_env.get("decision_sleep"), "0.000")
            self.assertIsNotNone(row)
            self.assertEqual(row[0], "success")
            self.assertIsNotNone(row[1])

    def test_worker_direct_job_disabled_fast_path_ignores_dry_run_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "archive.db"
            data_dir = root / "data"
            data_dir.mkdir(parents=True, exist_ok=True)

            run_date = swm.today_date_str("America/New_York")
            slug = "highest-temperature-in-chengdu-on-june-30-2026"
            target = {
                "slug": slug,
                "event_date": run_date,
                "outcome": "30°C or higher",
                "side": "YES",
                "strategy": "target_realization_binary_settlement",
                "entry_ok": True,
                "entry_reasons": ["posterior_target_direct"],
                "posterior_target_direct": True,
                "token_id": "yes-token-30",
            }
            job_payload = {
                "include_event_keys": [{"slug": slug, "event_date": run_date}],
                "entry_expected_targets": [dict(target)],
                "posterior_target_direct": {"target_bucket": "30°C or higher"},
                "event_payloads": [
                    {
                        "slug": slug,
                        "event_date": run_date,
                        "payload": {
                            "slug": slug,
                            "event_date": run_date,
                            "posterior_target_direct_snapshot": True,
                            "posterior_target_direct": {"target_bucket": "30°C or higher"},
                            "entry_expected_targets": [dict(target)],
                        },
                    }
                ],
            }
            recorded = []

            def _record_disabled(**kwargs):
                recorded.append(kwargs)
                return 1

            with mock.patch.object(swm, "DB_PATH", db_path), mock.patch.object(swm, "DATA_DIR", data_dir):
                swm.init_db()
                enqueue_result = swm.enqueue_auto_trade_job(
                    run_date,
                    source="posterior_target_direct",
                    priority=15000,
                    payload=job_payload,
                    max_retry=0,
                    force_enqueue=True,
                )
                with mock.patch.dict(
                    "os.environ",
                    {
                        "PM_AUTO_TRADE_ENABLED": "0",
                        "PM_AUTO_TRADE_DIRECT_JOB_INLINE": "1",
                    },
                    clear=False,
                ), mock.patch.object(
                    swm,
                    "_auto_trade_runtime_lock_acquire",
                    side_effect=AssertionError("disabled direct job must not take runtime lock"),
                ), mock.patch.object(
                    swm,
                    "run_auto_trades_for_date",
                    side_effect=AssertionError("disabled direct worker must not run full trade flow"),
                ), mock.patch.object(
                    swm,
                    "_record_auto_trade_fallback_decisions",
                    side_effect=_record_disabled,
                ):
                    os.environ.pop("PM_AUTO_TRADE_DRY_RUN", None)
                    result = swm.run_auto_trade_job_worker_once()

            self.assertTrue(enqueue_result.get("enqueued"))
            self.assertTrue(result.get("ok"))
            self.assertTrue(result.get("result", {}).get("direct_target_disabled_fast_path"))
            self.assertEqual(recorded[0]["reason_code"], "auto_trade_disabled_direct_target_no_submit")

    def test_worker_enabled_direct_job_passes_inline_payload_without_slow_locks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "archive.db"
            data_dir = root / "data"
            data_dir.mkdir(parents=True, exist_ok=True)

            run_date = swm.today_date_str("America/New_York")
            slug = "highest-temperature-in-chengdu-on-june-30-2026"
            target = {
                "slug": slug,
                "event_date": run_date,
                "outcome": "30°C or higher",
                "side": "YES",
                "strategy": "target_realization_binary_settlement",
                "entry_ok": True,
                "entry_reasons": ["posterior_target_direct", "target_realization_order_intent"],
                "posterior_target_direct": True,
                "token_id": "yes-token-30",
                "ask": 0.42,
            }
            event_payload = {
                "slug": slug,
                "event_date": run_date,
                "title": "Highest temperature in Chengdu on June 30?",
                "posterior_target_direct_snapshot": True,
                "posterior_target_direct": {"target_bucket": "30°C or higher"},
                "entry_expected_targets": [dict(target)],
                "rows": [{"outcome": "30°C or higher", "yes_token": "yes-token-30"}],
            }
            job_payload = {
                "include_event_keys": [{"slug": slug, "event_date": run_date}],
                "entry_expected_targets": [dict(target)],
                "posterior_target_direct": {"target_bucket": "30°C or higher"},
                "event_payloads": [{"slug": slug, "event_date": run_date, "payload": event_payload}],
            }
            seen = []
            claim_env = {}
            original_claim_one = swm._auto_trade_job_claim_one

            def _claim_one_capture_env(*args, **kwargs):
                claim_env.update(
                    {
                        "skip_cleanup": os.environ.get("PM_AUTO_TRADE_JOB_SKIP_CLEANUP_ON_CLAIM"),
                        "latest_first": os.environ.get("PM_AUTO_TRADE_DIRECT_JOB_CLAIM_LATEST_FIRST"),
                    }
                )
                return original_claim_one(*args, **kwargs)

            def _capture_run(run_date_arg, **kwargs):
                seen.append((run_date_arg, kwargs))
                payload_seen = kwargs.get("job_payload")
                self.assertTrue(swm._auto_trade_payload_is_direct_target_job(payload_seen))
                return {"ok": True, "processed": 1, "skipped": 0}

            with mock.patch.object(swm, "DB_PATH", db_path), mock.patch.object(swm, "DATA_DIR", data_dir):
                swm.init_db()
                enqueue_result = swm.enqueue_auto_trade_job(
                    run_date,
                    source="posterior_target_direct",
                    priority=15000,
                    payload=job_payload,
                    max_retry=0,
                    force_enqueue=True,
                )
                with mock.patch.dict(
                    "os.environ",
                    {
                        "PM_AUTO_TRADE_ENABLED": "1",
                        "PM_AUTO_TRADE_DRY_RUN": "1",
                        "PM_AUTO_TRADE_DIRECT_JOB_INLINE": "1",
                    },
                    clear=False,
                ), mock.patch.object(
                    swm,
                    "_wait_for_facts_lock",
                    side_effect=AssertionError("direct job must not wait for facts lock"),
                ), mock.patch.object(
                    swm,
                    "_wait_for_db_writer_lock",
                    side_effect=AssertionError("direct job must not wait for db writer lock"),
                ), mock.patch.object(
                    swm,
                    "_auto_trade_job_claim_one",
                    side_effect=_claim_one_capture_env,
                ), mock.patch.object(
                    swm,
                    "run_auto_trades_for_date",
                    side_effect=_capture_run,
                ):
                    result = swm.run_auto_trade_job_worker_once()

            self.assertTrue(enqueue_result.get("enqueued"))
            self.assertTrue(result.get("ok"))
            self.assertTrue(result.get("picked"))
            self.assertEqual(result.get("result", {}).get("processed"), 1)
            self.assertEqual(len(seen), 1)
            self.assertEqual(claim_env.get("skip_cleanup"), "1")
            self.assertEqual(claim_env.get("latest_first"), "1")

    def test_worker_direct_job_runtime_lock_busy_keeps_retry_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "archive.db"
            data_dir = root / "data"
            data_dir.mkdir(parents=True, exist_ok=True)

            run_date = swm.today_date_str("America/New_York")
            slug = "highest-temperature-in-chengdu-on-june-30-2026"
            target = {
                "slug": slug,
                "event_date": run_date,
                "outcome": "30°C or higher",
                "side": "YES",
                "strategy": "target_realization_binary_settlement",
                "entry_ok": True,
                "entry_reasons": ["posterior_target_direct", "target_realization_order_intent"],
                "posterior_target_direct": True,
                "token_id": "yes-token-30",
                "ask": 0.42,
            }
            job_payload = {
                "include_event_keys": [{"slug": slug, "event_date": run_date}],
                "entry_expected_targets": [dict(target)],
                "posterior_target_direct": {"target_bucket": "30°C or higher"},
                "event_payloads": [
                    {
                        "slug": slug,
                        "event_date": run_date,
                        "payload": {
                            "slug": slug,
                            "event_date": run_date,
                            "posterior_target_direct_snapshot": True,
                            "posterior_target_direct": {"target_bucket": "30°C or higher"},
                            "entry_expected_targets": [dict(target)],
                        },
                    }
                ],
            }

            with mock.patch.object(swm, "DB_PATH", db_path), mock.patch.object(swm, "DATA_DIR", data_dir):
                swm.init_db()
                enqueue_result = swm.enqueue_auto_trade_job(
                    run_date,
                    source="posterior_target_direct",
                    priority=15000,
                    payload=job_payload,
                    max_retry=1,
                    force_enqueue=True,
                )
                with mock.patch.dict(
                    "os.environ",
                    {
                        "PM_AUTO_TRADE_ENABLED": "1",
                        "PM_AUTO_TRADE_DRY_RUN": "1",
                        "PM_AUTO_TRADE_DIRECT_RUNTIME_LOCK_WAIT_SEC": "0",
                    },
                    clear=False,
                ), mock.patch.object(
                    swm,
                    "_wait_for_facts_lock",
                    side_effect=AssertionError("direct job must not wait for facts lock"),
                ), mock.patch.object(
                    swm,
                    "_wait_for_db_writer_lock",
                    side_effect=AssertionError("direct job must not wait for db writer lock"),
                ), mock.patch.object(
                    swm,
                    "_auto_trade_runtime_lock_acquire",
                    return_value=False,
                ), mock.patch.object(
                    swm,
                    "run_auto_trades_for_date",
                    side_effect=AssertionError("runtime-lock-busy direct job must not run full trade flow"),
                ):
                    result = swm.run_auto_trade_job_worker_once()

                with sqlite3.connect(db_path) as conn:
                    row = conn.execute(
                        """
                        SELECT state, retry_count, max_retry, last_error, next_retry_at
                        FROM auto_trade_jobs
                        WHERE id = ?
                        """,
                        (int((enqueue_result.get("job") or {}).get("id") or 0),),
                    ).fetchone()

        self.assertFalse(result.get("ok"))
        self.assertTrue(result.get("picked"))
        self.assertTrue(result.get("retrying"))
        self.assertTrue(result.get("result", {}).get("direct_target_runtime_lock_fast_retry"))
        self.assertEqual(result.get("error"), "runtime_lock_busy")
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "retrying")
        self.assertEqual(row[1], 0)
        self.assertEqual(row[2], 1)
        self.assertEqual(row[3], "runtime_lock_busy")
        self.assertTrue(row[4])

    def test_worker_direct_job_claims_newest_direct_job_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "archive.db"
            data_dir = root / "data"
            data_dir.mkdir(parents=True, exist_ok=True)

            run_date = swm.today_date_str("America/New_York")
            slug = "highest-temperature-in-chengdu-on-june-30-2026"

            def _payload(outcome: str) -> dict:
                target = {
                    "slug": slug,
                    "event_date": run_date,
                    "outcome": outcome,
                    "side": "YES",
                    "strategy": "target_realization_binary_settlement",
                    "entry_ok": True,
                    "entry_reasons": ["posterior_target_direct"],
                    "posterior_target_direct": True,
                    "token_id": f"yes-token-{outcome}",
                }
                event_payload = {
                    "slug": slug,
                    "event_date": run_date,
                    "title": "Highest temperature in Chengdu on June 30?",
                    "posterior_target_direct_snapshot": True,
                    "posterior_target_direct": {"target_bucket": outcome},
                    "entry_expected_targets": [dict(target)],
                }
                return {
                    "include_event_keys": [{"slug": slug, "event_date": run_date}],
                    "entry_expected_targets": [dict(target)],
                    "posterior_target_direct": {"target_bucket": outcome},
                    "event_payloads": [
                        {"slug": slug, "event_date": run_date, "payload": event_payload}
                    ],
                }

            recorded = []

            def _record_disabled(**kwargs):
                recorded.append(kwargs)
                return 1

            with mock.patch.object(swm, "DB_PATH", db_path), mock.patch.object(swm, "DATA_DIR", data_dir):
                swm.init_db()
                older = swm.enqueue_auto_trade_job(
                    run_date,
                    source="posterior_target_direct",
                    priority=15000,
                    payload=_payload("29°C"),
                    max_retry=0,
                    force_enqueue=True,
                )
                newer = swm.enqueue_auto_trade_job(
                    run_date,
                    source="posterior_target_direct",
                    priority=15000,
                    payload=_payload("30°C or higher"),
                    max_retry=0,
                    force_enqueue=True,
                )
                with mock.patch.dict(
                    "os.environ",
                    {
                        "PM_AUTO_TRADE_ENABLED": "0",
                        "PM_AUTO_TRADE_DRY_RUN": "0",
                        "PM_AUTO_TRADE_DIRECT_JOB_INLINE": "1",
                    },
                    clear=False,
                ), mock.patch.object(
                    swm,
                    "_wait_for_facts_lock",
                    side_effect=AssertionError("direct job must not wait for facts lock"),
                ), mock.patch.object(
                    swm,
                    "_wait_for_db_writer_lock",
                    side_effect=AssertionError("direct job must not wait for db writer lock"),
                ), mock.patch.object(
                    swm,
                    "_auto_trade_runtime_lock_acquire",
                    side_effect=AssertionError("disabled direct job must not take runtime lock"),
                ), mock.patch.object(
                    swm,
                    "run_auto_trades_for_date",
                    side_effect=AssertionError("disabled direct worker must not run full trade flow"),
                ), mock.patch.object(
                    swm,
                    "_record_auto_trade_fallback_decisions",
                    side_effect=_record_disabled,
                ):
                    result = swm.run_auto_trade_job_worker_once()

                with sqlite3.connect(db_path) as conn:
                    older_state = conn.execute(
                        "SELECT state FROM auto_trade_jobs WHERE id = ?",
                        (int((older.get("job") or {}).get("id") or 0),),
                    ).fetchone()[0]
                    newer_state = conn.execute(
                        "SELECT state FROM auto_trade_jobs WHERE id = ?",
                        (int((newer.get("job") or {}).get("id") or 0),),
                    ).fetchone()[0]

            self.assertTrue(result.get("ok"))
            self.assertTrue(result.get("picked"))
            self.assertEqual(newer_state, "success")
            self.assertEqual(older_state, "queued")
            self.assertEqual(recorded[0]["targets"][0]["outcome"], "30°C or higher")

    def test_entry_1m_live_enqueue_remains_available_outside_peak_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "archive.db"
            data_dir = root / "data"
            data_dir.mkdir(parents=True, exist_ok=True)

            payload = {
                "include_event_keys": [
                    {
                        "slug": "highest-temperature-in-chengdu-on-june-19-2026",
                        "event_date": "2026-06-19",
                    }
                ]
            }
            with mock.patch.object(swm, "DB_PATH", db_path), mock.patch.object(swm, "DATA_DIR", data_dir):
                swm.init_db()
                with mock.patch.dict(
                    "os.environ",
                    {
                        "PM_AUTO_TRADE_DRY_RUN": "0",
                        "PM_AUTO_TRADE_ENTRY_PEAK_WINDOW_ENABLED": "1",
                        "PM_AUTO_TRADE_ENTRY_LOCAL_DATE_OVERRIDE": "2026-06-19",
                        "PM_AUTO_TRADE_ENTRY_LOCAL_HOUR_OVERRIDE": "14.9",
                    },
                    clear=False,
                ):
                    result = swm.enqueue_auto_trade_job(
                        "2026-06-18",
                        source="entry_1m_strategy_batch",
                        payload=payload,
                    )
                conn = sqlite3.connect(db_path)
                job_count = conn.execute("SELECT COUNT(*) FROM auto_trade_jobs").fetchone()[0]

            self.assertTrue(result.get("ok"))
            self.assertTrue(result.get("enqueued"))
            self.assertEqual(job_count, 1)

    def test_fallback_decision_writer_uses_module_level_source_normalizer(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "archive.db"
            data_dir = root / "data"
            data_dir.mkdir(parents=True, exist_ok=True)

            with mock.patch.object(swm, "DB_PATH", db_path), mock.patch.object(swm, "DATA_DIR", data_dir):
                swm.init_db()
                inserted = swm._record_auto_trade_fallback_decisions(
                    run_id=1,
                    run_date="2026-04-14",
                    slug="highest-temperature-in-test-on-april-14-2026",
                    event_date="2026-04-14",
                    title="Highest temperature in Test?",
                    targets=[
                        {"outcome": "30C", "side": "YES", "strategy": "lcb_global"},
                    ],
                    reason_code="event_worker_failed",
                    reason_text="worker failed",
                    details={"strategy": "lcb_global"},
                    entry_only_realizable_mode=True,
                )
                conn = sqlite3.connect(db_path)
                try:
                    row = conn.execute(
                        "SELECT details_json FROM auto_trade_decisions LIMIT 1"
                    ).fetchone()
                finally:
                    conn.close()

            self.assertEqual(inserted, 1)
            self.assertIsNotNone(row)
            payload = json.loads(row[0])
            self.assertEqual(payload["source_fit_na_reason"], "source_fit_unavailable")
            self.assertEqual(payload["entry_usable_edge_na_reason"], "edge_tradeability_missing")
            self.assertEqual(payload["entry_usable_edge_base_source"], "edge_tradeability_missing")
            self.assertEqual(payload["entry_strategy_family"], "path_probability_follow")

    def test_fallback_decision_writer_preserves_target_tradeability_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "archive.db"
            data_dir = root / "data"
            data_dir.mkdir(parents=True, exist_ok=True)

            with mock.patch.object(swm, "DB_PATH", db_path), mock.patch.object(swm, "DATA_DIR", data_dir):
                swm.init_db()
                inserted = swm._record_auto_trade_fallback_decisions(
                    run_id=1,
                    run_date="2026-04-14",
                    slug="highest-temperature-in-test-on-april-14-2026",
                    event_date="2026-04-14",
                    title="Highest temperature in Test?",
                    targets=[
                        {
                            "outcome": "30C",
                            "side": "YES",
                            "strategy": "lcb_global",
                            "edge_tradeability_profit_value": 0.14,
                            "ask": 0.40,
                        },
                    ],
                    reason_code="event_worker_failed",
                    reason_text="worker failed",
                    details={},
                    entry_only_realizable_mode=True,
                )
                conn = sqlite3.connect(db_path)
                try:
                    row = conn.execute(
                        "SELECT details_json FROM auto_trade_decisions LIMIT 1"
                    ).fetchone()
                finally:
                    conn.close()

        self.assertEqual(inserted, 1)
        self.assertIsNotNone(row)
        payload = json.loads(row[0])
        self.assertAlmostEqual(payload["entry_usable_edge_base"], 0.14)
        self.assertGreater(payload["entry_usable_edge"], 0)
        self.assertEqual(payload["entry_usable_edge_base_source"], "edge_tradeability_profit_value")

    def test_direct_job_active_primary_entry_fast_confirms_without_full_execute(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "archive.db"
            data_dir = root / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            run_date = "2026-07-02"
            slug = "highest-temperature-in-chengdu-on-july-2-2026"
            event_date = "2026-07-02"
            target = {
                "slug": slug,
                "event_date": event_date,
                "outcome": "31\u00b0C",
                "side": "YES",
                "strategy": "target_realization_binary_settlement",
                "strategy_source": "posterior_target_direct",
                "entry_ok": True,
                "posterior_target_direct": True,
                "target_bucket": "31\u00b0C",
                "posterior_target_bucket": "31\u00b0C",
                "target_realization_leg_role": "posterior_target_direct_target",
                "target_realization_execution_action": "BUY",
                "target_realization_plan_action": "add",
                "token_id": "yes31-token",
                "ask": 0.36,
                "q_bayes_posterior": 0.56,
                "q_temperature_bucket_posterior": 0.56,
            }
            event_payload = {
                "slug": slug,
                "event_date": event_date,
                "title": "Highest temperature in Chengdu on July 2?",
                "posterior_target_direct_snapshot": True,
                "posterior_target_direct": {
                    "source": "posterior_target_direct",
                    "target_bucket": "31\u00b0C",
                    "target_posterior": 0.56,
                    "job_enqueued_at": "2026-07-02T01:00:00+00:00",
                    "primary_only_fast_path_allowed": False,
                },
                "entry_expected_targets": [dict(target)],
                "rows": [{"outcome": "31\u00b0C", "q_bayes_posterior": 0.56}],
            }
            job_payload = {
                "include_event_keys": [{"slug": slug, "event_date": event_date}],
                "posterior_target_direct": dict(event_payload["posterior_target_direct"]),
                "entry_expected_targets": [dict(target)],
                "event_payloads": [
                    {
                        "slug": slug,
                        "event_date": event_date,
                        "source": "posterior_target_direct",
                        "payload": event_payload,
                    }
                ],
            }

            with mock.patch.object(swm, "DB_PATH", db_path), mock.patch.object(swm, "DATA_DIR", data_dir):
                swm.init_db()
                conn = sqlite3.connect(db_path)
                try:
                    now = "2026-07-02T01:00:00+00:00"
                    conn.execute(
                        """
                        INSERT INTO edge_trades(
                            slug, event_date, outcome, side, state,
                            last_order_id, last_action, last_status, last_action_at,
                            position_size, avg_price, updated_at
                        ) VALUES (?, ?, ?, 'YES', 'entry', ?, 'fill_sync', 'confirmed', ?, ?, ?, ?)
                        """,
                        (slug, event_date, "31\u00b0C", "active-order-31", now, 12.5, 0.34, now),
                    )
                    conn.execute(
                        """
                        INSERT INTO auto_trade_jobs(
                            run_date, source, state, priority, payload_json,
                            lease_until, retry_count, max_retry, next_retry_at, last_error,
                            started_at, ended_at, run_id, created_at, updated_at
                        ) VALUES (?, 'posterior_target_direct', 'queued', 15000, ?, '', 0, 0, ?, '', '', '', NULL, ?, ?)
                        """,
                        (
                            run_date,
                            json.dumps(job_payload, ensure_ascii=True, separators=(",", ":")),
                            now,
                            now,
                            now,
                        ),
                    )
                    conn.commit()
                finally:
                    conn.close()

                with mock.patch.dict(
                    os.environ,
                    {
                        "PM_AUTO_TRADE_ENABLED": "1",
                        "PM_AUTO_TRADE_DRY_RUN": "0",
                        "PM_AUTO_TRADE_DIRECT_ACTIVE_ENTRY_FAST_CONFIRM": "1",
                    },
                    clear=False,
                ), mock.patch.object(swm, "today_date_str", return_value=run_date), mock.patch.object(
                    swm,
                    "execute_auto_trades",
                    side_effect=AssertionError("full execute should not run for active direct target"),
                ) as execute_mock:
                    result = swm.run_auto_trade_job_worker_once()

                conn = sqlite3.connect(db_path)
                conn.row_factory = sqlite3.Row
                try:
                    job_state = conn.execute("SELECT state FROM auto_trade_jobs LIMIT 1").fetchone()[0]
                    decision = conn.execute(
                        """
                        SELECT reason_codes_json, details_json
                        FROM auto_trade_decisions
                        WHERE slug = ? AND event_date = ? AND outcome = '31°C' AND side = 'YES'
                        ORDER BY id DESC
                        LIMIT 1
                        """,
                        (slug, event_date),
                    ).fetchone()
                finally:
                    conn.close()

            self.assertTrue(result["ok"])
            self.assertTrue(result["picked"])
            self.assertTrue(result["result"]["direct_target_active_entry_fast_confirm"])
            self.assertEqual(result["result"]["processed"], 1)
            self.assertEqual(job_state, "success")
            execute_mock.assert_not_called()
            self.assertIsNotNone(decision)
            reason_codes = json.loads(decision["reason_codes_json"] or "[]")
            details = json.loads(decision["details_json"] or "{}")
            self.assertIn("order_already_live", reason_codes)
            self.assertTrue(details["active_entry_fast_confirm"])
            self.assertTrue(details["target_entry_already_active"])
            self.assertFalse(details["primary_target_entry_needed"])
            self.assertEqual(details["last_order_id"], "active-order-31")

    def test_clob_order_service_pending_reconciler_confirms_idempotent_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "archive.db"
            data_dir = root / "data"
            data_dir.mkdir(parents=True, exist_ok=True)
            slug = "highest-temperature-in-chengdu-on-july-2-2026"
            event_date = "2026-07-02"
            now = "2026-07-02T01:00:00+00:00"
            idem_key = "clobsvc:test-pending-31"

            with mock.patch.object(swm, "DB_PATH", db_path), mock.patch.object(swm, "DATA_DIR", data_dir):
                swm.init_db()
                conn = sqlite3.connect(db_path)
                try:
                    conn.execute(
                        """
                        INSERT INTO edge_trades(
                            slug, event_date, outcome, side, state,
                            last_order_id, last_action, last_status, last_action_at,
                            entry_price, entry_size, position_size, updated_at
                        ) VALUES (?, ?, '31°C', 'YES', 'entry_pending', '', 'entry',
                                  'clob_order_service_submit_pending', ?, 0.73, 13.6986, 0, ?)
                        """,
                        (slug, event_date, now, now),
                    )
                    conn.execute(
                        """
                        INSERT INTO auto_trade_decisions(
                            run_id, run_date, slug, event_date, title,
                            outcome, side, action, decided_state, will_order,
                            reason_codes_json, reasons_json, details_json, created_at
                        ) VALUES (NULL, ?, ?, ?, ?, '31°C', 'YES', 'entry', 'entry_pending', 1, ?, '[]', ?, ?)
                        """,
                        (
                            event_date,
                            slug,
                            event_date,
                            "Highest temperature in Chengdu on July 2?",
                            json.dumps(["order_submit_pending_confirmation"]),
                            json.dumps(
                                {
                                    "idempotency_key": idem_key,
                                    "order_price": 0.73,
                                    "order_size": 13.6986,
                                    "order_type": "GTC",
                                    "strategy": "posterior_settlement",
                                },
                                ensure_ascii=False,
                            ),
                            now,
                        ),
                    )
                    conn.commit()
                finally:
                    conn.close()

                with mock.patch.dict(os.environ, {"PM_ARCHIVE_WRITE_SCOPE": "chengdu_zuuu"}, clear=False), mock.patch.object(
                    swm,
                    "_clob_order_service_enabled",
                    return_value=True,
                ), mock.patch.object(
                    swm,
                    "_clob_order_service_idempotency_status_once",
                    return_value={
                        "ok": True,
                        "orderID": "0xrecovered",
                        "status": "matched",
                        "idempotency_key": idem_key,
                        "clob_order_service": True,
                    },
                ):
                    result = swm.reconcile_clob_order_service_pending_orders(
                        db_path,
                        slug=slug,
                        event_date=event_date,
                    )

                conn = sqlite3.connect(db_path)
                conn.row_factory = sqlite3.Row
                try:
                    trade = conn.execute(
                        """
                        SELECT last_order_id, last_status, last_error
                        FROM edge_trades
                        WHERE slug = ? AND event_date = ? AND outcome = '31°C' AND side = 'YES'
                        """,
                        (slug, event_date),
                    ).fetchone()
                    order = conn.execute(
                        "SELECT order_id, order_status, price, size FROM auto_orders WHERE order_id = '0xrecovered'"
                    ).fetchone()
                    decision = conn.execute(
                        """
                        SELECT reason_codes_json, details_json
                        FROM auto_trade_decisions
                        WHERE slug = ? AND event_date = ? AND outcome = '31°C' AND side = 'YES'
                        ORDER BY id DESC
                        LIMIT 1
                        """,
                        (slug, event_date),
                    ).fetchone()
                finally:
                    conn.close()

        self.assertTrue(result["ok"])
        self.assertEqual(result["checked"], 1)
        self.assertEqual(result["recovered"], 1)
        self.assertEqual(result["decisions_written"], 1)
        self.assertIsNotNone(trade)
        self.assertEqual(trade["last_order_id"], "0xrecovered")
        self.assertEqual(trade["last_status"], "matched")
        self.assertEqual(trade["last_error"], "")
        self.assertIsNotNone(order)
        self.assertEqual(order["order_status"], "matched")
        self.assertAlmostEqual(order["price"], 0.73)
        self.assertAlmostEqual(order["size"], 13.6986)
        self.assertIsNotNone(decision)
        reason_codes = json.loads(decision["reason_codes_json"] or "[]")
        details = json.loads(decision["details_json"] or "{}")
        self.assertIn("order_already_live", reason_codes)
        self.assertTrue(details["clob_order_service_pending_reconciled"])
        self.assertEqual(details["clob_order_service_reconcile"], "order_id_recovered")
        self.assertEqual(details["last_order_id"], "0xrecovered")

    def test_closed_and_ended_fact_rows_are_filtered_before_rotation_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "archive.db"
            data_dir = root / "data"
            data_dir.mkdir(parents=True, exist_ok=True)

            with mock.patch.object(swm, "DB_PATH", db_path), mock.patch.object(swm, "DATA_DIR", data_dir):
                swm.init_db()
                run_date = "2026-04-14"
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS event_fact_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_date TEXT NOT NULL,
                        slug TEXT NOT NULL,
                        event_date TEXT NOT NULL,
                        fact_as_of TEXT NOT NULL,
                        best_edge_score REAL,
                        composer_version TEXT NOT NULL,
                        fact_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                rows = [
                    (
                        "closed-event",
                        100.0,
                        {
                            "slug": "closed-event",
                            "market_closed": True,
                            "event_end_utc": "2000-01-01T12:00:00Z",
                            "rows": [{"outcome": "1C", "q_entry": 0.5, "q_calibrated": 0.5}],
                        },
                    ),
                    (
                        "ended-event",
                        90.0,
                        {
                            "slug": "ended-event",
                            "market_closed": False,
                            "event_end_utc": "2000-01-01T12:00:00Z",
                            "rows": [{"outcome": "2C", "q_entry": 0.5, "q_calibrated": 0.5}],
                        },
                    ),
                    (
                        "active-event-a",
                        80.0,
                        {
                            "slug": "active-event-a",
                            "market_closed": False,
                            "event_end_utc": "2999-01-01T12:00:00Z",
                            "rows": [{"outcome": "3C", "q_entry": 0.5, "q_calibrated": 0.5}],
                        },
                    ),
                    (
                        "active-event-b",
                        70.0,
                        {
                            "slug": "active-event-b",
                            "market_closed": False,
                            "event_end_utc": "2999-01-01T12:00:00Z",
                            "rows": [{"outcome": "4C", "q_entry": 0.5, "q_calibrated": 0.5}],
                        },
                    ),
                ]
                for idx, (slug, score, payload) in enumerate(rows, start=1):
                    cur.execute(
                        """
                        INSERT INTO event_fact_history(
                            run_date, slug, event_date, fact_as_of, best_edge_score, composer_version, fact_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run_date,
                            slug,
                            run_date,
                            f"{run_date}T0{idx}:00:00+00:00",
                            score,
                            "test",
                            json.dumps(payload, ensure_ascii=True),
                            f"{run_date}T0{idx}:00:00+00:00",
                        ),
                    )
                conn.commit()
                conn.close()

                seen_calls = []

                def _capture_execute(run_date_key, event_date_key, payload, **kwargs):
                    seen_calls.append((str(payload.get("slug") or ""), event_date_key))

                with mock.patch.dict(
                    "os.environ",
                    {
                        "PM_AUTO_TRADE_EVENTS_PER_JOB_MAX": "2",
                        "PM_AUTO_TRADE_EVENT_TIMEOUT_SEC": "0",
                        "PM_AUTO_TRADE_EVENT_BACKOFF_SEC": "0",
                    },
                    clear=False,
                ), mock.patch.object(swm, "execute_auto_trades", side_effect=_capture_execute):
                    swm.run_auto_trades_for_date(run_date)

            self.assertEqual(seen_calls, [("active-event-a", run_date), ("active-event-b", run_date)])

    def test_entry_ok_fact_rows_are_selected_before_blind_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "archive.db"
            data_dir = root / "data"
            data_dir.mkdir(parents=True, exist_ok=True)

            with mock.patch.object(swm, "DB_PATH", db_path), mock.patch.object(swm, "DATA_DIR", data_dir):
                swm.init_db()
                run_date = "2026-04-14"
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS event_fact_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_date TEXT NOT NULL,
                        slug TEXT NOT NULL,
                        event_date TEXT NOT NULL,
                        fact_as_of TEXT NOT NULL,
                        best_edge_score REAL,
                        composer_version TEXT NOT NULL,
                        fact_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                rows = [
                    (
                        "high-score-a",
                        100.0,
                        {
                            "slug": "high-score-a",
                            "rows": [{"outcome": "20C", "q_entry": 0.5, "q_calibrated": 0.5}],
                        },
                    ),
                    (
                        "high-score-b",
                        90.0,
                        {
                            "slug": "high-score-b",
                            "rows": [{"outcome": "21C", "q_entry": 0.5, "q_calibrated": 0.5}],
                        },
                    ),
                    (
                        "low-score-entry-ok",
                        1.0,
                        {
                            "slug": "low-score-entry-ok",
                            "rows": [{"outcome": "22C", "q_entry": 0.5, "q_calibrated": 0.5}],
                            "auto_trade_ssot": {
                                "main_candidate": {
                                    "outcome": "22C",
                                    "side": "YES",
                                    "entry_ok": True,
                                    "edge_tradeability_pass": True,
                                    "exec_edge_entry": 0.12,
                                    "edge_realizable_score": 0.12,
                                }
                            },
                        },
                    ),
                ]
                for idx, (slug, score, payload) in enumerate(rows, start=1):
                    cur.execute(
                        """
                        INSERT INTO event_fact_history(
                            run_date, slug, event_date, fact_as_of, best_edge_score, composer_version, fact_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run_date,
                            slug,
                            run_date,
                            f"{run_date}T0{idx}:00:00+00:00",
                            score,
                            "test",
                            json.dumps(payload, ensure_ascii=True),
                            f"{run_date}T0{idx}:00:00+00:00",
                        ),
                    )
                conn.commit()
                conn.close()

                seen_calls = []

                def _capture_execute(run_date_key, event_date_key, payload, **kwargs):
                    seen_calls.append((str(payload.get("slug") or ""), event_date_key))

                with mock.patch.dict(
                    "os.environ",
                    {
                        "PM_AUTO_TRADE_EVENTS_PER_JOB_MAX": "2",
                        "PM_AUTO_TRADE_EVENT_TIMEOUT_SEC": "0",
                        "PM_AUTO_TRADE_EVENT_BACKOFF_SEC": "0",
                    },
                    clear=False,
                ), mock.patch.object(swm, "execute_auto_trades", side_effect=_capture_execute):
                    result = swm.run_auto_trades_for_date(run_date)

            self.assertEqual(len(seen_calls), 2)
            self.assertIn(("low-score-entry-ok", run_date), seen_calls)
            self.assertEqual(result.get("entry_priority_events"), 1)

    def test_recent_timeout_fact_rows_are_filtered_before_rotation_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "archive.db"
            data_dir = root / "data"
            data_dir.mkdir(parents=True, exist_ok=True)

            with mock.patch.object(swm, "DB_PATH", db_path), mock.patch.object(swm, "DATA_DIR", data_dir):
                swm.init_db()
                run_date = "2026-04-14"
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS event_fact_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_date TEXT NOT NULL,
                        slug TEXT NOT NULL,
                        event_date TEXT NOT NULL,
                        fact_as_of TEXT NOT NULL,
                        best_edge_score REAL,
                        composer_version TEXT NOT NULL,
                        fact_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                for idx, (slug, score) in enumerate(
                    [
                        ("timeout-event", 100.0),
                        ("active-event-a", 90.0),
                        ("active-event-b", 80.0),
                    ],
                    start=1,
                ):
                    cur.execute(
                        """
                        INSERT INTO event_fact_history(
                            run_date, slug, event_date, fact_as_of, best_edge_score, composer_version, fact_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run_date,
                            slug,
                            run_date,
                            f"{run_date}T0{idx}:00:00+00:00",
                            score,
                            "test",
                            json.dumps(
                                {
                                    "slug": slug,
                                    "rows": [{"outcome": f"{idx}C", "q_entry": 0.5, "q_calibrated": 0.5}],
                                },
                                ensure_ascii=True,
                            ),
                            f"{run_date}T0{idx}:00:00+00:00",
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
                        "timeout-event",
                        run_date,
                        "Timeout Event",
                        "1C",
                        "YES",
                        "entry",
                        "error_timeout",
                        0,
                        '["event_worker_timeout"]',
                        '["timeout"]',
                        "{}",
                        datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    ),
                )
                conn.commit()
                conn.close()

                seen_calls = []

                def _capture_execute(run_date_key, event_date_key, payload, **kwargs):
                    seen_calls.append((str(payload.get("slug") or ""), event_date_key))

                with mock.patch.dict(
                    "os.environ",
                    {
                        "PM_AUTO_TRADE_EVENTS_PER_JOB_MAX": "2",
                        "PM_AUTO_TRADE_EVENT_TIMEOUT_SEC": "0",
                        "PM_AUTO_TRADE_EVENT_BACKOFF_SEC": "0",
                        "PM_AUTO_TRADE_EVENT_TIMEOUT_BACKOFF_SEC": "900",
                        "PM_AUTO_TRADE_EVENT_TIMEOUT_BACKOFF_MIN_HITS": "1",
                    },
                    clear=False,
                ), mock.patch.object(swm, "execute_auto_trades", side_effect=_capture_execute):
                    result = swm.run_auto_trades_for_date(run_date)

            self.assertEqual(seen_calls, [("active-event-a", run_date), ("active-event-b", run_date)])
            self.assertEqual(result.get("pruned_recent_timeout_events"), 1)

    def test_closed_fact_row_with_active_position_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "archive.db"
            data_dir = root / "data"
            data_dir.mkdir(parents=True, exist_ok=True)

            with mock.patch.object(swm, "DB_PATH", db_path), mock.patch.object(swm, "DATA_DIR", data_dir):
                swm.init_db()
                run_date = "2026-04-14"
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS event_fact_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_date TEXT NOT NULL,
                        slug TEXT NOT NULL,
                        event_date TEXT NOT NULL,
                        fact_as_of TEXT NOT NULL,
                        best_edge_score REAL,
                        composer_version TEXT NOT NULL,
                        fact_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                cur.execute(
                    """
                    INSERT INTO event_fact_history(
                        run_date, slug, event_date, fact_as_of, best_edge_score, composer_version, fact_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_date,
                        "closed-but-active",
                        run_date,
                        f"{run_date}T01:00:00+00:00",
                        100.0,
                        "test",
                        json.dumps(
                            {
                                "slug": "closed-but-active",
                                "market_closed": True,
                                "event_end_utc": "2000-01-01T12:00:00Z",
                                "rows": [{"outcome": "1C", "q_entry": 0.5, "q_calibrated": 0.5}],
                            },
                            ensure_ascii=True,
                        ),
                        f"{run_date}T01:00:00+00:00",
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO edge_trades(
                        slug, event_date, outcome, side, state, position_size, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "closed-but-active",
                        run_date,
                        "1C",
                        "YES",
                        "entry",
                        1.0,
                        f"{run_date}T02:00:00+00:00",
                    ),
                )
                conn.commit()
                conn.close()

                seen_calls = []

                def _capture_execute(run_date_key, event_date_key, payload, **kwargs):
                    seen_calls.append((str(payload.get("slug") or ""), event_date_key))

                with mock.patch.dict(
                    "os.environ",
                    {
                        "PM_AUTO_TRADE_EVENTS_PER_JOB_MAX": "1",
                        "PM_AUTO_TRADE_EVENT_TIMEOUT_SEC": "0",
                        "PM_AUTO_TRADE_EVENT_BACKOFF_SEC": "0",
                    },
                    clear=False,
                ), mock.patch.object(swm, "execute_auto_trades", side_effect=_capture_execute):
                    swm.run_auto_trades_for_date(run_date)

            self.assertEqual(seen_calls, [("closed-but-active", run_date)])


if __name__ == "__main__":
    unittest.main()
