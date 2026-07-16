import os
import json
import subprocess
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import scan_weather_market as swm
from scan_weather_market import (
    _TARGET_REALIZATION_FACT_PLAN_CACHE,
    _align_target_realization_plan_with_path_distribution,
    _apply_kelly_size_in_place,
    _apply_temperature_path_trade_follow_gate_to_plan,
    _auto_order_record_price,
    _bayes_bimodal_size_scale,
    _entry_order_policy_block_reason,
    _load_target_realization_fact_plan,
    _estimate_buy_collateral_required_usdc,
    _max_buy_size_for_collateral_usdc,
    _merge_target_realization_probability_fields_from_ssot_leg,
    _resolve_order_size,
    _run_node_order,
    _same_outcome_active_entry_block_from_state,
)


class EntryBudgetSizingTests(unittest.TestCase):
    def test_resolve_order_size_scales_notional_by_budget_ratio(self) -> None:
        with patch.dict(
            os.environ,
            {
                "PM_ORDER_USDC": "5",
                "PM_AUTO_TRADE_ENTRY_BUDGET_RATIO": "0.70",
                "PM_AUTO_TRADE_ENTRY_BUDGET_MIN_USDC": "1",
                "PM_MAX_ORDER_USDC": "",
                "PM_MAX_ORDER_SIZE": "",
                "PM_MIN_ORDER_SIZE": "0",
                "PM_ENFORCE_EXCHANGE_MIN_ORDER_SIZE": "0",
            },
            clear=False,
        ):
            size = _resolve_order_size(0.5, None, None)
        self.assertAlmostEqual(float(size), 7.0, places=4)

    def test_resolve_order_size_honors_1u_budget_floor(self) -> None:
        with patch.dict(
            os.environ,
            {
                "PM_ORDER_USDC": "1",
                "PM_AUTO_TRADE_ENTRY_BUDGET_RATIO": "0.70",
                "PM_AUTO_TRADE_ENTRY_BUDGET_MIN_USDC": "1",
                "PM_MAX_ORDER_USDC": "",
                "PM_MAX_ORDER_SIZE": "",
                "PM_MIN_ORDER_SIZE": "0",
                "PM_ENFORCE_EXCHANGE_MIN_ORDER_SIZE": "0",
            },
            clear=False,
        ):
            size = _resolve_order_size(0.25, None, None)
        self.assertAlmostEqual(float(size), 4.0, places=4)

    def test_resolve_order_size_defaults_to_10u_cap_when_max_missing(self) -> None:
        with patch.dict(
            os.environ,
            {
                "PM_ORDER_USDC": "50",
                "PM_AUTO_TRADE_ENTRY_BUDGET_RATIO": "1",
                "PM_AUTO_TRADE_ENTRY_BUDGET_MIN_USDC": "1",
                "PM_MAX_ORDER_USDC": "",
                "PM_MAX_ORDER_SIZE": "",
                "PM_MIN_ORDER_SIZE": "0",
                "PM_ENFORCE_EXCHANGE_MIN_ORDER_SIZE": "0",
            },
            clear=False,
        ):
            size = _resolve_order_size(0.5, None, None)
        self.assertAlmostEqual(float(size), 20.0, places=4)

    def test_run_node_order_caps_buy_limit_notional_at_10u(self) -> None:
        with patch.dict(
            os.environ,
            {
                "PM_MAX_ORDER_USDC": "",
                "PM_AUTO_TRADE_ENTRY_BUDGET_RATIO": "1",
            },
            clear=False,
        ):
            ok, payload = _run_node_order(
                "place-order",
                "dryrun-token",
                "BUY",
                price=0.50,
                size=100,
                order_type="GTC",
            )
        self.assertTrue(ok)
        self.assertTrue(bool(payload.get("dry_run")))
        self.assertAlmostEqual(float(payload.get("order_notional_cap_usdc")), 10.0, places=9)
        self.assertAlmostEqual(float(payload.get("order_size_after_cap")), 20.0, places=9)
        self.assertIn("--size", payload.get("cmd"))
        self.assertIn("20.000000", payload.get("cmd"))

    def test_run_node_order_caps_buy_market_amount_and_size_at_10u(self) -> None:
        with patch.dict(
            os.environ,
            {
                "PM_MAX_ORDER_USDC": "",
                "PM_AUTO_TRADE_ENTRY_BUDGET_RATIO": "1",
            },
            clear=False,
        ):
            ok, payload = _run_node_order(
                "place-order",
                "dryrun-token",
                "BUY",
                size=100,
                amount=25,
                order_type="FAK",
            )
        self.assertTrue(ok)
        self.assertTrue(bool(payload.get("dry_run")))
        self.assertAlmostEqual(float(payload.get("order_notional_cap_usdc")), 10.0, places=9)
        self.assertAlmostEqual(float(payload.get("order_amount_after_cap")), 10.0, places=9)
        self.assertAlmostEqual(float(payload.get("order_size_after_cap")), 10.0, places=9)
        self.assertIn("--amount", payload.get("cmd"))
        self.assertIn("10.000000", payload.get("cmd"))

    def test_live_order_requires_durable_clob_service(self) -> None:
        with patch.dict(
            os.environ,
            {
                "PM_AUTO_TRADE_DRY_RUN": "0",
                "PM_CLOB_ORDER_SERVICE_ENABLED": "0",
                "PM_CLOB_ALLOW_LEGACY_NODE_SUBMIT": "0",
            },
            clear=False,
        ), patch("scan_weather_market._run_node_subprocess") as node_submit:
            ok, payload = _run_node_order(
                "place-order",
                "live-token",
                "BUY",
                price=0.50,
                size=2,
                order_type="GTC",
            )

        self.assertFalse(ok)
        self.assertEqual(payload["error"], "durable_clob_order_service_required")
        node_submit.assert_not_called()

    def test_direct_target_buy_order_forces_single_direct_attempt_timeout(self) -> None:
        with patch.dict(os.environ, {"PM_CLOB_ALLOW_LEGACY_NODE_SUBMIT": "1"}, clear=True), \
             patch.object(swm, "AUTO_TRADE_DRY_RUN", False), \
             patch.object(swm, "_run_node_subprocess") as node_submit:
            ok, payload = swm._run_node_order("place-order", "token", "BUY", price=0.42, size=2.0, order_type="GTC")
        self.assertFalse(ok)
        self.assertEqual(payload.get("error"), "durable_clob_order_service_required")
        node_submit.assert_not_called()


    def test_direct_target_buy_order_default_timeout_covers_node_cold_start(self) -> None:
        with patch.dict(os.environ, {"PM_CLOB_ALLOW_LEGACY_NODE_SUBMIT": "1"}, clear=True), \
             patch.object(swm, "AUTO_TRADE_DRY_RUN", False), \
             patch.object(swm, "_run_node_subprocess") as node_submit:
            ok, payload = swm._run_node_order("place-order", "token", "BUY", price=0.42, size=2.0, order_type="GTC")
        self.assertFalse(ok)
        self.assertEqual(payload.get("error"), "durable_clob_order_service_required")
        node_submit.assert_not_called()


    def test_direct_target_buy_order_env_timeout_skips_parent_watchdog_floor(self) -> None:
        with patch.dict(os.environ, {"PM_CLOB_ALLOW_LEGACY_NODE_SUBMIT": "1", "PM_AUTO_TRADE_DIRECT_ORDER_TIMEOUT_SEC": "4.5"}, clear=True), \
             patch.object(swm, "AUTO_TRADE_DRY_RUN", False), \
             patch.object(swm, "_run_node_subprocess") as node_submit:
            ok, payload = swm._run_node_order("place-order", "token", "BUY", price=0.42, size=2.0, order_type="GTC")
        self.assertFalse(ok)
        self.assertEqual(payload.get("error"), "durable_clob_order_service_required")
        node_submit.assert_not_called()


    def test_direct_target_service_read_timeout_defers_confirmation(self) -> None:
        def _unexpected_timeout_confirm(**_kwargs):
            raise AssertionError("direct target service read timeout should not enter long confirm loop")

        with patch.dict(
            os.environ,
            {
                "PM_CLOB_ORDER_SERVICE_ENABLED": "1",
                "PM_CLOB_ORDER_SERVICE_URL": "http://127.0.0.1:8765",
                "PM_CLOB_ORDER_SERVICE_REQUEST_TIMEOUT_SEC": "2.8",
                "PM_CLOB_ORDER_SERVICE_DIRECT_TIMEOUT_DEFER_CONFIRM": "1",
                "PM_AUTO_TRADE_DIRECT_JOB_CONTEXT": "1",
                "PM_AUTO_TRADE_DIRECT_ORDER_TIMEOUT_SEC": "3.8",
                "PM_MAX_ORDER_USDC": "10",
                "PM_AUTO_TRADE_ENTRY_BUDGET_RATIO": "1",
            },
            clear=True,
        ):
            with patch.object(swm, "ensure_system_proxy_settings", return_value=None):
                with patch.object(swm, "AUTO_TRADE_DRY_RUN", False):
                    with patch.object(swm, "_resolve_node_bin", return_value="node"):
                        with patch.object(
                            swm.requests,
                            "post",
                            side_effect=swm.requests.exceptions.ReadTimeout("local service slow"),
                        ):
                            with patch.object(
                                swm,
                                "_clob_order_service_timeout_confirm",
                                side_effect=_unexpected_timeout_confirm,
                            ):
                                ok, payload = swm._run_node_order(
                                    "place-order",
                                    "direct-buy-token",
                                    "BUY",
                                    price=0.42,
                                    size=2.0,
                                    order_type="GTC",
                                )

        self.assertFalse(ok)
        self.assertEqual(payload.get("error"), "clob_order_service_timeout")
        self.assertTrue(payload.get("timeout_expired"))
        self.assertTrue(payload.get("clob_order_service_timeout_confirm_deferred"))
        self.assertTrue(payload.get("order_submit_pending_confirmation"))
        self.assertTrue(payload.get("reconcile_required"))
        self.assertNotIn("clob_order_service_fallback_allowed", payload)
        self.assertNotIn("clob_order_service_fallback_blocked_reason", payload)
        self.assertEqual(payload.get("cmd"), ["clob_order_service", "place-order"])
        self.assertIn("order_submit_ms", payload)

    def test_direct_target_proxy_fallback_overrides_global_force_direct(self) -> None:
        with patch.dict(os.environ, {"PM_CLOB_ALLOW_LEGACY_NODE_SUBMIT": "1", "PM_AUTO_TRADE_DIRECT_PROXY_FALLBACK_ENABLED": "1"}, clear=True), \
             patch.object(swm, "AUTO_TRADE_DRY_RUN", False), \
             patch.object(swm, "_run_node_subprocess") as node_submit:
            ok, payload = swm._run_node_order("place-order", "token", "BUY", price=0.42, size=2.0, order_type="GTC")
        self.assertFalse(ok)
        self.assertEqual(payload.get("error"), "durable_clob_order_service_required")
        node_submit.assert_not_called()


    def test_exec_failed_payload_preserves_timeout_stderr(self) -> None:
        exc = subprocess.TimeoutExpired(
            ["node", "scripts/polymarket_order.mjs", "place-order"],
            2.75,
            output="",
            stderr="[CLOB Client] place-order parent hard timeout after 1500ms",
        )

        payload = swm._exec_failed_payload(exc)

        self.assertIn("exec_failed", payload["error"])
        self.assertIn("parent hard timeout", payload["stderr"])

    def test_exec_failed_payload_marks_empty_outer_timeout_as_diagnostic(self) -> None:
        exc = subprocess.TimeoutExpired(
            ["node", "scripts/polymarket_order.mjs", "place-order"],
            3.0,
            output="",
            stderr="",
        )

        payload = swm._exec_failed_payload(exc)

        self.assertTrue(payload["timeout_expired"])
        self.assertEqual(payload["timeout_sec"], 3.0)
        self.assertEqual(payload["stdout"], "")
        self.assertEqual(payload["stderr"], "")
        self.assertTrue(payload["subprocess_timeout_output_missing"])
        self.assertEqual(payload["error_detail"], "subprocess_timeout")

    def test_same_outcome_active_entry_blocks_existing_opposite_side_position(self) -> None:
        block = _same_outcome_active_entry_block_from_state(
            outcome_label="33°C",
            side_label="YES",
            prev_states={
                ("33°C", "NO"): {
                    "state": "entry",
                    "position_size": 6.0,
                    "last_order_id": "order-1",
                }
            },
            rows=[],
            open_orders_by_token={},
            entry_submit_guard_runtime=set(),
            entry_submit_guard_meta={},
        )
        self.assertIsInstance(block, dict)
        self.assertEqual(block.get("active_count"), 1)
        self.assertEqual(block.get("active_sources")[0].get("side"), "NO")

    def test_same_outcome_active_entry_allows_same_side_position_topup(self) -> None:
        block = _same_outcome_active_entry_block_from_state(
            outcome_label="33°C",
            side_label="YES",
            prev_states={
                ("33°C", "YES"): {
                    "state": "entry",
                    "position_size": 6.0,
                    "last_order_id": "filled-entry",
                }
            },
            rows=[],
            open_orders_by_token={},
            entry_submit_guard_runtime=set(),
            entry_submit_guard_meta={},
            allow_same_side_position=True,
        )
        self.assertIsNone(block)

    def test_same_outcome_active_entry_blocks_buy_open_order_from_either_token(self) -> None:
        block = _same_outcome_active_entry_block_from_state(
            outcome_label="34°C",
            side_label="YES",
            prev_states={},
            rows=[{"outcome": "34°C", "yes_token": "yes-token", "no_token": "no-token"}],
            open_orders_by_token={
                "no-token": [
                    {
                        "side": "BUY",
                        "order_id": "open-no",
                        "price": "0.33",
                        "size": "5",
                    }
                ]
            },
            entry_submit_guard_runtime=set(),
            entry_submit_guard_meta={},
        )
        self.assertIsInstance(block, dict)
        self.assertEqual(block.get("active_sources")[0].get("source"), "open_orders")
        self.assertEqual(block.get("active_sources")[0].get("side"), "NO")

    def test_same_outcome_active_entry_blocks_same_run_submission(self) -> None:
        block = _same_outcome_active_entry_block_from_state(
            outcome_label="35°C",
            side_label="NO",
            prev_states={},
            rows=[],
            open_orders_by_token={},
            entry_submit_guard_runtime={("35°C", "YES")},
            entry_submit_guard_meta={("35°C", "YES"): {"source": "entry_main"}},
        )
        self.assertIsInstance(block, dict)
        self.assertEqual(block.get("active_sources")[0].get("source"), "run_submit_guard")
        self.assertEqual(block.get("active_sources")[0].get("side"), "YES")

    def test_kelly_bankroll_path_scales_budget_before_final_size(self) -> None:
        candidate = {
            "price": 0.5,
            "q_calibrated": 0.8,
        }
        with patch.dict(
            os.environ,
            {
                "PM_KELLY_ENABLED": "1",
                "PM_BANKROLL_USDC": "100",
                "PM_ORDER_USDC": "1",
                "PM_KELLY_FRACTION": "0.25",
                "PM_KELLY_MIN_NOTIONAL_USDC": "1",
                "PM_KELLY_MAX_NOTIONAL_USDC": "",
                "PM_MAX_ORDER_USDC": "",
                "PM_MIN_ORDER_SIZE": "0",
                "PM_MAX_ORDER_SIZE": "",
                "PM_AUTO_TRADE_ENTRY_BUDGET_RATIO": "0.70",
                "PM_AUTO_TRADE_ENTRY_BUDGET_MIN_USDC": "1",
            },
            clear=False,
        ):
            _apply_kelly_size_in_place(candidate, allow_apply=True)
        self.assertTrue(bool(candidate.get("kelly_applied")))
        self.assertAlmostEqual(float(candidate.get("kelly_target_notional")), 7.0, places=4)
        self.assertAlmostEqual(float(candidate.get("size")), 14.0, places=4)

    def test_kelly_uses_posterior_settlement_candidate_prob_before_legacy_q(self) -> None:
        candidate = {
            "side": "YES",
            "price": 0.2,
            "size": 10,
            "strategy_family": "posterior_settlement",
            "outcome": "32°C",
            "target_bucket": "32°C",
            "posterior_settlement_candidate_bucket_prob": 0.5,
            "q_entry": 0.05,
            "q_raw": 0.95,
        }
        with patch.dict(
            os.environ,
            {
                "PM_KELLY_ENABLED": "1",
                "PM_BANKROLL_USDC": "",
                "PM_ORDER_USDC": "",
                "PM_KELLY_FRACTION": "1",
                "PM_KELLY_REF_FRACTION": "0.5",
                "PM_KELLY_MIN_SCALE": "0",
                "PM_KELLY_MAX_SCALE": "10",
                "PM_KELLY_MIN_NOTIONAL_USDC": "",
                "PM_KELLY_MAX_NOTIONAL_USDC": "",
                "PM_MAX_ORDER_USDC": "",
                "PM_MIN_ORDER_SIZE": "0",
                "PM_MAX_ORDER_SIZE": "",
                "PM_KELLY_BLOCK_NONPOSITIVE": "1",
            },
            clear=False,
        ):
            _apply_kelly_size_in_place(candidate, allow_apply=True)
        self.assertTrue(bool(candidate.get("kelly_applied")))
        self.assertEqual(candidate.get("kelly_q_source"), "posterior_settlement_candidate_bucket_prob")
        self.assertAlmostEqual(float(candidate.get("kelly_q_prob")), 0.5, places=9)
        self.assertEqual(candidate.get("kelly_fraction_basis"), "target_realization_probability")
        self.assertTrue(bool(candidate.get("kelly_price_edge_ignored")))
        self.assertAlmostEqual(float(candidate.get("kelly_target_notional")), 2.0, places=9)
        self.assertAlmostEqual(float(candidate.get("size")), 10.0, places=9)

    def test_kelly_remote_no_uses_inverse_posterior_bucket_probability(self) -> None:
        candidate = {
            "side": "NO",
            "price": 0.2,
            "size": 10,
            "strategy_family": "posterior_settlement",
            "outcome": "35°C",
            "target_bucket": "32°C",
            "posterior_settlement_candidate_bucket_prob": 0.1,
            "q_entry": 0.1,
            "q_raw": 0.1,
        }
        with patch.dict(
            os.environ,
            {
                "PM_KELLY_ENABLED": "1",
                "PM_BANKROLL_USDC": "",
                "PM_ORDER_USDC": "",
                "PM_KELLY_FRACTION": "1",
                "PM_KELLY_REF_FRACTION": "0.875",
                "PM_KELLY_MIN_SCALE": "0",
                "PM_KELLY_MAX_SCALE": "10",
                "PM_KELLY_MIN_NOTIONAL_USDC": "",
                "PM_KELLY_MAX_NOTIONAL_USDC": "",
                "PM_MAX_ORDER_USDC": "",
                "PM_MIN_ORDER_SIZE": "0",
                "PM_MAX_ORDER_SIZE": "",
                "PM_KELLY_BLOCK_NONPOSITIVE": "1",
            },
            clear=False,
        ):
            _apply_kelly_size_in_place(candidate, allow_apply=True)
        self.assertTrue(bool(candidate.get("kelly_applied")))
        self.assertEqual(candidate.get("kelly_q_source"), "1-posterior_settlement_candidate_bucket_prob")
        self.assertAlmostEqual(float(candidate.get("kelly_q_prob")), 0.9, places=9)
        self.assertEqual(candidate.get("kelly_fraction_basis"), "target_realization_probability")
        self.assertAlmostEqual(float(candidate.get("kelly_target_notional")), 2.057142857, places=9)
        self.assertAlmostEqual(float(candidate.get("size")), 10.2857, places=4)

    def test_kelly_posterior_no_fallback_uses_side_specific_bayes_probability(self) -> None:
        candidate = {
            "side": "NO",
            "price": 0.2,
            "size": 10,
            "strategy_family": "posterior_settlement",
            "outcome": "35°C",
            "target_bucket": "32°C",
            "q_bayes_posterior": 0.8,
            "q_entry": 0.1,
        }
        with patch.dict(
            os.environ,
            {
                "PM_KELLY_ENABLED": "1",
                "PM_BANKROLL_USDC": "",
                "PM_ORDER_USDC": "",
                "PM_KELLY_FRACTION": "1",
                "PM_KELLY_REF_FRACTION": "0.75",
                "PM_KELLY_MIN_SCALE": "0",
                "PM_KELLY_MAX_SCALE": "10",
                "PM_KELLY_MIN_NOTIONAL_USDC": "",
                "PM_KELLY_MAX_NOTIONAL_USDC": "",
                "PM_MAX_ORDER_USDC": "",
                "PM_MIN_ORDER_SIZE": "0",
                "PM_MAX_ORDER_SIZE": "",
                "PM_KELLY_BLOCK_NONPOSITIVE": "1",
            },
            clear=False,
        ):
            _apply_kelly_size_in_place(candidate, allow_apply=True)
        self.assertTrue(bool(candidate.get("kelly_applied")))
        self.assertEqual(candidate.get("kelly_q_source"), "q_bayes_posterior")
        self.assertAlmostEqual(float(candidate.get("kelly_q_prob")), 0.8, places=9)
        self.assertEqual(candidate.get("kelly_fraction_basis"), "target_realization_probability")
        self.assertAlmostEqual(float(candidate.get("kelly_target_notional")), 2.133333333, places=9)

    def test_kelly_allocates_today_yes_from_near_three_market_pool(self) -> None:
        candidate = {
            "side": "YES",
            "price": 0.25,
            "size": 10,
            "strategy_family": "posterior_settlement",
            "days_to_event": 0,
            "outcome": "32°C",
            "target_bucket": "32°C",
            "posterior_settlement_candidate_bucket_prob": 0.5,
            "target_realization_reliability": 0.8,
        }
        with patch.dict(
            os.environ,
            {
                "PM_KELLY_ENABLED": "1",
                "PM_BANKROLL_USDC": "900",
                "PM_KELLY_RESERVE_RATIO": "0.20",
                "PM_KELLY_RESERVE_USDC": "",
                "PM_KELLY_MARKET_TODAY_RATIO": "0.50",
                "PM_KELLY_MARKET_TOMORROW_RATIO": "0.30",
                "PM_KELLY_MARKET_DAY_AFTER_RATIO": "0.20",
                "PM_KELLY_YES_RATIO": "0.70",
                "PM_KELLY_NO_RATIO": "0.30",
                "PM_KELLY_FRACTION": "0.10",
                "PM_KELLY_MIN_NOTIONAL_USDC": "",
                "PM_KELLY_MAX_NOTIONAL_USDC": "",
                "PM_MAX_ORDER_USDC": "",
                "PM_MIN_ORDER_SIZE": "0",
                "PM_MAX_ORDER_SIZE": "",
            },
            clear=False,
        ):
            _apply_kelly_size_in_place(candidate, allow_apply=True)
        self.assertTrue(bool(candidate.get("kelly_applied")))
        self.assertEqual(candidate.get("kelly_sizing_source"), "near_three_market_allocation")
        self.assertEqual(candidate.get("kelly_market_day"), "today")
        self.assertAlmostEqual(float(candidate.get("kelly_available_usdc")), 720.0, places=9)
        self.assertAlmostEqual(float(candidate.get("kelly_market_budget_usdc")), 360.0, places=9)
        self.assertAlmostEqual(float(candidate.get("kelly_side_budget_usdc")), 252.0, places=9)
        self.assertAlmostEqual(float(candidate.get("kelly_target_notional")), 10.0, places=9)
        self.assertAlmostEqual(float(candidate.get("size")), 40.0, places=9)

    def test_kelly_allocates_tomorrow_no_from_near_three_market_pool(self) -> None:
        candidate = {
            "side": "NO",
            "price": 0.2,
            "size": 10,
            "strategy_family": "posterior_settlement",
            "days_to_event": 1,
            "outcome": "35°C",
            "target_bucket": "32°C",
            "posterior_settlement_candidate_bucket_prob": 0.1,
            "posterior_settlement_size_scale": 0.6,
        }
        with patch.dict(
            os.environ,
            {
                "PM_KELLY_ENABLED": "1",
                "PM_BANKROLL_USDC": "900",
                "PM_KELLY_RESERVE_RATIO": "0.20",
                "PM_KELLY_RESERVE_USDC": "",
                "PM_KELLY_MARKET_TODAY_RATIO": "0.50",
                "PM_KELLY_MARKET_TOMORROW_RATIO": "0.30",
                "PM_KELLY_MARKET_DAY_AFTER_RATIO": "0.20",
                "PM_KELLY_YES_RATIO": "0.70",
                "PM_KELLY_NO_RATIO": "0.30",
                "PM_KELLY_FRACTION": "0.10",
                "PM_KELLY_MIN_NOTIONAL_USDC": "",
                "PM_KELLY_MAX_NOTIONAL_USDC": "",
                "PM_MAX_ORDER_USDC": "",
                "PM_MIN_ORDER_SIZE": "0",
                "PM_MAX_ORDER_SIZE": "",
            },
            clear=False,
        ):
            _apply_kelly_size_in_place(candidate, allow_apply=True)
        self.assertTrue(bool(candidate.get("kelly_applied")))
        self.assertEqual(candidate.get("kelly_sizing_source"), "near_three_market_allocation")
        self.assertEqual(candidate.get("kelly_market_day"), "tomorrow")
        self.assertAlmostEqual(float(candidate.get("kelly_market_budget_usdc")), 216.0, places=9)
        self.assertAlmostEqual(float(candidate.get("kelly_side_budget_usdc")), 64.8, places=9)
        self.assertAlmostEqual(float(candidate.get("kelly_target_notional")), 3.4992, places=9)
        self.assertAlmostEqual(float(candidate.get("size")), 17.496, places=9)

    def test_target_realization_default_yes_pool_uses_full_pool_coefficient(self) -> None:
        candidate = {
            "side": "YES",
            "price": 0.5,
            "size": 2,
            "strategy_family": "posterior_settlement",
            "days_to_event": 0,
            "outcome": "33°C",
            "target_bucket": "33°C",
            "posterior_settlement_candidate_bucket_prob": 0.5,
            "target_realization_reliability": 1.0,
        }
        with patch.dict(
            os.environ,
            {
                "PM_KELLY_ENABLED": "1",
                "PM_TARGET_REALIZATION_BANKROLL_USDC": "900",
                "PM_KELLY_MIN_NOTIONAL_USDC": "",
                "PM_KELLY_MAX_NOTIONAL_USDC": "",
                "PM_MAX_ORDER_USDC": "",
                "PM_MIN_ORDER_SIZE": "0",
                "PM_MAX_ORDER_SIZE": "",
            },
            clear=True,
        ):
            _apply_kelly_size_in_place(candidate, allow_apply=True)
        self.assertTrue(bool(candidate.get("kelly_applied")))
        self.assertTrue(bool(candidate.get("target_realization_sizing_active")))
        self.assertEqual(candidate.get("target_realization_capital_plan_version"), "target_realization_capital_plan_v2")
        self.assertEqual(candidate.get("kelly_sizing_source"), "near_three_market_allocation")
        self.assertAlmostEqual(float(candidate.get("kelly_available_usdc")), 720.0, places=9)
        self.assertAlmostEqual(float(candidate.get("kelly_market_budget_usdc")), 432.0, places=9)
        self.assertAlmostEqual(float(candidate.get("kelly_side_ratio")), 0.20, places=9)
        self.assertAlmostEqual(float(candidate.get("kelly_side_budget_usdc")), 86.4, places=9)
        self.assertAlmostEqual(float(candidate.get("kelly_fraction_cfg")), 0.1, places=9)
        self.assertAlmostEqual(float(candidate.get("kelly_target_notional")), 4.32, places=9)
        self.assertAlmostEqual(float(candidate.get("size")), 8.64, places=9)

    def test_target_realization_default_remote_no_uses_defense_pool(self) -> None:
        candidate = {
            "side": "NO",
            "price": 0.5,
            "size": 2,
            "strategy_family": "posterior_settlement",
            "days_to_event": 0,
            "outcome": "36°C",
            "target_bucket": "33°C",
            "posterior_settlement_candidate_bucket_prob": 0.25,
            "target_realization_reliability": 1.0,
        }
        with patch.dict(
            os.environ,
            {
                "PM_KELLY_ENABLED": "1",
                "PM_TARGET_REALIZATION_BANKROLL_USDC": "900",
                "PM_KELLY_MIN_NOTIONAL_USDC": "",
                "PM_KELLY_MAX_NOTIONAL_USDC": "",
                "PM_MAX_ORDER_USDC": "",
                "PM_MIN_ORDER_SIZE": "0",
                "PM_MAX_ORDER_SIZE": "",
            },
            clear=True,
        ):
            _apply_kelly_size_in_place(candidate, allow_apply=True)
        self.assertTrue(bool(candidate.get("kelly_applied")))
        self.assertEqual(candidate.get("kelly_q_source"), "1-posterior_settlement_candidate_bucket_prob")
        self.assertAlmostEqual(float(candidate.get("kelly_q_prob")), 0.75, places=9)
        self.assertAlmostEqual(float(candidate.get("kelly_side_ratio")), 0.80, places=9)
        self.assertAlmostEqual(float(candidate.get("kelly_side_budget_usdc")), 345.6, places=9)
        self.assertAlmostEqual(float(candidate.get("kelly_fraction_cfg")), 0.1, places=9)
        self.assertAlmostEqual(float(candidate.get("kelly_target_notional")), 10.0, places=9)
        self.assertAlmostEqual(float(candidate.get("size")), 20.0, places=9)

    def test_kelly_uses_ui_snapshot_ssot_target_notional_when_present(self) -> None:
        candidate = {
            "side": "YES",
            "price": 0.25,
            "size": 10,
            "strategy_family": "posterior_settlement",
            "days_to_event": 0,
            "outcome": "32°C",
            "target_bucket": "32°C",
            "posterior_settlement_candidate_bucket_prob": 0.45,
            "target_realization_reliability": 0.9,
            "target_realization_plan_notional": 12.5,
            "target_realization_plan_notional_source": "ui_snapshot_ssot",
            "kelly_sizing_source": "ui_snapshot_ssot_capital_plan",
        }
        with patch.dict(
            os.environ,
            {
                "PM_KELLY_ENABLED": "1",
                "PM_TARGET_REALIZATION_BANKROLL_USDC": "900",
                "PM_KELLY_MIN_NOTIONAL_USDC": "",
                "PM_KELLY_MAX_NOTIONAL_USDC": "",
                "PM_MAX_ORDER_USDC": "",
                "PM_MIN_ORDER_SIZE": "0",
                "PM_MAX_ORDER_SIZE": "",
            },
            clear=True,
        ):
            _apply_kelly_size_in_place(candidate, allow_apply=True)
        self.assertTrue(bool(candidate.get("kelly_applied")))
        self.assertEqual(candidate.get("kelly_sizing_source"), "ui_snapshot_ssot_capital_plan")
        self.assertAlmostEqual(float(candidate.get("kelly_target_notional")), 10.0, places=9)
        self.assertAlmostEqual(float(candidate.get("size")), 40.0, places=9)
        self.assertEqual(candidate.get("order_size_source"), "ui_snapshot_ssot_capital_plan")

    def test_load_target_realization_ui_plan_prefers_frozen_snapshot_file(self) -> None:
        _TARGET_REALIZATION_FACT_PLAN_CACHE.clear()
        with patch.dict(os.environ, {"PM_TARGET_REALIZATION_SSOT_SNAPSHOT_FILE": "/tmp/retired.json"}, clear=False):
            legs, plan = _load_target_realization_fact_plan("chengdu-test", "2026-06-17")
        self.assertEqual(legs, {})
        self.assertEqual(plan, {})


    def test_align_plan_reads_full_temperature_path_bucket_distribution(self) -> None:
        plan = {
            "ok": True,
            "target_bucket": "25°C or below",
            "target_posterior": 0.26,
            "second_target_bucket": "26°C",
            "second_target_posterior": 0.20,
            "legs": [
                {
                    "role": "target_yes",
                    "bucket": "25°C or below",
                    "side": "YES",
                    "posterior": 0.26,
                    "target_realization_path_probability": 0.30,
                },
                {
                    "role": "second_target_yes",
                    "bucket": "26°C",
                    "side": "YES",
                    "posterior": 0.20,
                    "target_realization_path_probability": 0.10,
                },
            ],
        }
        payload = {
            "temperature_path_distribution": {
                "available": True,
                "target_bucket": "25°C or below",
                "second_target_bucket": "26°C",
                "bucket_distribution": [
                    {
                        "bucket": "25°C or below",
                        "posterior": 0.42,
                        "path_probability": 0.7688,
                        "path_probability_source": "hard_observed_max_not_exceeded_boundary",
                    },
                    {
                        "bucket": "26°C",
                        "posterior": 0.28,
                        "path_probability": 0.0813,
                        "path_probability_source": "soft_forecast_error_band",
                    },
                    {
                        "bucket": "27°C",
                        "posterior": 0.11,
                        "path_probability": 0.01,
                        "path_probability_source": "soft_forecast_error_band",
                    },
                ],
                "trade_follow": {"allowed": True, "reason": "posterior_path_reliability_and_freshness_pass"},
            }
        }

        aligned = _align_target_realization_plan_with_path_distribution(dict(plan), payload)
        legs = {(leg["bucket"], leg["side"]): leg for leg in aligned["legs"]}

        self.assertAlmostEqual(float(aligned["target_posterior"]), 0.42, places=9)
        self.assertAlmostEqual(float(aligned["second_target_posterior"]), 0.28, places=9)
        self.assertAlmostEqual(
            float(legs[("25°C or below", "YES")]["target_realization_path_probability"]),
            0.7688,
            places=9,
        )
        self.assertAlmostEqual(
            float(legs[("26°C", "YES")]["target_realization_path_probability"]),
            0.0813,
            places=9,
        )
        self.assertEqual(
            legs[("25°C or below", "YES")]["target_realization_path_probability_source"],
            "hard_observed_max_not_exceeded_boundary",
        )

    def test_load_target_realization_ui_plan_aligns_stale_plan_to_path_distribution(self) -> None:
        _TARGET_REALIZATION_FACT_PLAN_CACHE.clear()
        with patch.dict(os.environ, {"PM_TARGET_REALIZATION_SSOT_SNAPSHOT_FILE": "/tmp/retired.json"}, clear=False):
            legs, plan = _load_target_realization_fact_plan("chengdu-test", "2026-06-21")
        self.assertEqual(legs, {})
        self.assertEqual(plan, {})


    def test_align_stale_plan_clears_old_path_probability_when_path_distribution_missing(self) -> None:
        plan = {
            "ok": True,
            "target_bucket": "30°C",
            "target_posterior": 0.20,
            "second_target_bucket": "31°C",
            "second_target_posterior": 0.18,
            "legs": [
                {
                    "role": "target_yes",
                    "bucket": "30°C",
                    "side": "YES",
                    "posterior": 0.20,
                    "target_realization_path_probability": 0.56,
                    "target_realization_reliability": 0.56,
                },
                {
                    "role": "second_target_yes",
                    "bucket": "31°C",
                    "side": "YES",
                    "posterior": 0.18,
                    "target_realization_path_probability": 0.22,
                    "target_realization_reliability": 0.22,
                },
            ],
            "ui_risk_explanation": {
                "target_realization_probability": 0.56,
                "second_target_realization_probability": 0.22,
            },
        }
        payload = {
            "temperature_path_distribution": {
                "target_bucket": "30°C",
                "target_bucket_posterior": 0.24598122644339906,
                "target_bucket_path_probability": None,
                "second_target_bucket": "31°C",
                "second_target_bucket_posterior": 0.19461077408328195,
                "second_target_bucket_path_probability": None,
                "trade_follow": {"allowed": False, "reason": "blocked_path_reliability_low"},
            }
        }

        aligned = _align_target_realization_plan_with_path_distribution(dict(plan), payload)
        legs = {(leg["bucket"], leg["side"]): leg for leg in aligned["legs"]}

        self.assertAlmostEqual(float(aligned["target_posterior"]), 0.24598122644339906, places=9)
        self.assertAlmostEqual(float(aligned["second_target_posterior"]), 0.19461077408328195, places=9)
        self.assertIsNone(legs[("30°C", "YES")]["target_realization_path_probability"])
        self.assertIsNone(legs[("31°C", "YES")]["target_realization_path_probability"])
        self.assertEqual(legs[("30°C", "YES")]["target_realization_reliability"], 0.0)
        self.assertEqual(legs[("31°C", "YES")]["target_realization_reliability"], 0.0)
        self.assertEqual(
            legs[("30°C", "YES")]["target_realization_reliability_source"],
            "temperature_path_distribution_path_probability_missing",
        )
        self.assertIsNone(aligned["ui_risk_explanation"]["target_realization_probability"])
        self.assertIsNone(aligned["ui_risk_explanation"]["second_target_realization_probability"])
        self.assertTrue(aligned["temperature_path_trade_follow_block_yes"])

    def test_ssot_leg_keeps_path_probability_separate_from_reliability(self) -> None:
        details = _merge_target_realization_probability_fields_from_ssot_leg(
            {},
            {
                "target_realization_reliability": 0.66,
                "target_realization_reliability_source": "path_reliability_score",
                "target_realization_path_probability": 0.74,
                "target_realization_path_probability_source": "temperature_path_distribution",
            },
        )

        self.assertAlmostEqual(float(details["target_realization_reliability"]), 0.66, places=9)
        self.assertAlmostEqual(float(details["kelly_realization_strength"]), 0.66, places=9)
        self.assertEqual(details["target_realization_reliability_source"], "path_reliability_score")
        self.assertAlmostEqual(float(details["target_realization_path_probability"]), 0.74, places=9)
        self.assertAlmostEqual(float(details["target_realization_path_p_reach_yes"]), 0.74, places=9)
        self.assertEqual(
            details["target_realization_path_probability_source"],
            "temperature_path_distribution",
        )

    def test_temperature_path_gate_sanitizes_stale_yes_add_plan(self) -> None:
        plan = {
            "ok": True,
            "version": "target_realization_capital_plan_v2",
            "target_bucket": "25°C or below",
            "second_target_bucket": "26°C",
            "legs": [
                {
                    "role": "target_yes",
                    "bucket": "25°C or below",
                    "side": "YES",
                    "action": "add",
                    "target_notional": 12.0,
                    "current_notional": 0.0,
                    "price": 0.53,
                },
                {
                    "role": "remote_no",
                    "bucket": "28°C",
                    "side": "NO",
                    "action": "add",
                    "target_notional": 80.0,
                    "price": 0.95,
                },
            ],
        }
        payload = {
            "temperature_path_distribution": {
                "trade_follow": {
                    "allowed": False,
                    "reason": "blocked_path_reliability_low",
                }
            }
        }

        sanitized = _apply_temperature_path_trade_follow_gate_to_plan(plan, payload)
        legs = {
            (leg["bucket"], leg["side"]): leg
            for leg in sanitized["legs"]
        }

        yes_leg = legs[("25°C or below", "YES")]
        no_leg = legs[("28°C", "NO")]
        self.assertFalse(sanitized["temperature_path_trade_follow_allowed"])
        self.assertTrue(sanitized["temperature_path_trade_follow_block_yes"])
        self.assertEqual(yes_leg["action"], "hold")
        self.assertEqual(yes_leg["block_reason"], "temperature_path_trade_follow_not_allowed")
        self.assertFalse(yes_leg["target_realization_trade_follow_allowed"])
        self.assertEqual(yes_leg["target_realization_trade_follow_reason"], "blocked_path_reliability_low")
        self.assertFalse(yes_leg["temperature_path_trade_follow_allowed"])
        self.assertTrue(yes_leg["temperature_path_trade_follow_block_yes"])
        self.assertEqual(yes_leg["temperature_path_trade_follow_gate_source"], "temperature_path_distribution.trade_follow")
        self.assertEqual(float(yes_leg["target_notional"]), 0.0)
        self.assertEqual(no_leg["action"], "add")

        blocked_candidate = {
            "strategy_family": "posterior_settlement",
            "entry_strategy_family": "posterior_settlement",
            "target_probability_field": "posterior_ssot_fact.target_probability",
            "posterior_settlement_entry_allowed": False,
            "posterior_settlement_entry_block_reason": "temperature_path_trade_follow_not_allowed",
        }
        self.assertEqual(
            _entry_order_policy_block_reason(blocked_candidate),
            "temperature_path_trade_follow_not_allowed",
        )

    def test_load_target_realization_ui_plan_applies_temperature_path_gate(self) -> None:
        _TARGET_REALIZATION_FACT_PLAN_CACHE.clear()
        with patch.dict(os.environ, {"PM_TARGET_REALIZATION_SSOT_SNAPSHOT_FILE": "/tmp/retired.json"}, clear=False):
            legs, plan = _load_target_realization_fact_plan("chengdu-test", "2026-06-21")
        self.assertEqual(legs, {})
        self.assertEqual(plan, {})


    def test_local_fact_plan_without_path_distribution_uses_capital_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "archive.db")
            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE event_fact_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_date TEXT,
                    slug TEXT,
                    event_date TEXT,
                    fact_as_of TEXT,
                    composer_version TEXT,
                    created_at TEXT,
                    fact_json TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE event_fact_latest (
                    slug TEXT,
                    event_date TEXT,
                    fact_id INTEGER,
                    updated_at TEXT
                )
                """
            )
            stale_payload = {
                "slug": "chengdu-test",
                "event_date": "2026-06-21",
                "posterior_ssot_fact": {
                    "state": "available",
                    "target_bucket": "26°C",
                },
                "target_realization_capital_plan": {
                    "ok": True,
                    "version": "target_realization_capital_plan_v2",
                    "target_bucket": "26°C",
                    "legs": [
                        {
                            "role": "target_yes",
                            "bucket": "26°C",
                            "side": "YES",
                            "action": "add",
                            "target_notional": 12.03,
                        }
                    ],
                },
            }
            cur = conn.execute(
                """
                INSERT INTO event_fact_history(run_date, slug, event_date, fact_as_of, composer_version, created_at, fact_json)
                VALUES('2026-06-20', 'chengdu-test', '2026-06-21', '2026-06-20T12:00:00+00:00', 'test', '2026-06-20T12:00:00+00:00', ?)
                """,
                (json.dumps(stale_payload),),
            )
            fact_id = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO event_fact_latest(slug, event_date, fact_id, updated_at) VALUES('chengdu-test', '2026-06-21', ?, '2026-06-20T12:00:00+00:00')",
                (fact_id,),
            )
            conn.commit()
            conn.close()

            _TARGET_REALIZATION_FACT_PLAN_CACHE.clear()
            with patch("scan_weather_market.DB_PATH", Path(db_path)), patch.dict(
                os.environ,
                {
                    "PM_TARGET_REALIZATION_SSOT_LOCAL_FACT_ENABLED": "1",
                    "PM_TARGET_REALIZATION_SSOT_URL": "",
                    "PM_TARGET_REALIZATION_SSOT_SNAPSHOT_FILE": "",
                },
                clear=False,
            ):
                legs, plan = _load_target_realization_fact_plan("chengdu-test", "2026-06-21")

            self.assertEqual(plan["ssot_load_source"], "event_fact_latest_local")
            self.assertEqual(plan["target_bucket"], "26°C")
            self.assertAlmostEqual(float(legs[("26°C", "YES")]["target_notional"]), 12.03, places=9)
            self.assertNotIn("target_realization_path_distribution_aligned", plan)

    def test_live_plan_loader_ignores_snapshot_file_and_ui_http(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
            json.dump({"items": [{"slug": "chengdu-test", "event_date": "2026-06-21"}]}, fh)
            path = fh.name
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        _TARGET_REALIZATION_FACT_PLAN_CACHE.clear()
        with patch.dict(
            os.environ,
            {
                "PM_AUTO_TRADE_DRY_RUN": "0",
                "PM_TARGET_REALIZATION_SSOT_SNAPSHOT_FILE": path,
                "PM_TARGET_REALIZATION_SSOT_URL": "http://127.0.0.1:9/must-not-be-used",
                "PM_TARGET_REALIZATION_SSOT_LOCAL_FACT_ENABLED": "1",
            },
            clear=False,
        ), patch(
            "scan_weather_market._load_target_realization_local_fact_plan",
            return_value=None,
        ) as local_loader, patch("scan_weather_market.requests.get") as http_get:
            legs, plan = _load_target_realization_fact_plan("chengdu-test", "2026-06-21")

        self.assertEqual(legs, {})
        self.assertEqual(plan, {})
        local_loader.assert_called_once_with("chengdu-test", "2026-06-21")
        http_get.assert_not_called()

    def test_local_fact_plan_uses_persisted_capital_plan_without_scanner_recompute(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "archive.db")
            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE event_fact_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_date TEXT,
                    slug TEXT,
                    event_date TEXT,
                    fact_as_of TEXT,
                    composer_version TEXT,
                    created_at TEXT,
                    fact_json TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE event_fact_latest (
                    slug TEXT,
                    event_date TEXT,
                    fact_id INTEGER,
                    updated_at TEXT
                )
                """
            )
            payload = {
                "slug": "chengdu-test",
                "event_date": "2026-06-21",
                "posterior_ssot_fact": {
                    "state": "available",
                    "target_bucket": "25°C or below",
                },
                "temperature_path_distribution": {
                    "trade_follow": {
                        "allowed": True,
                        "reason": "posterior_path_reliability_and_freshness_pass",
                    },
                    "target_bucket": "25°C or below",
                    "target_bucket_posterior": 0.42016291281991425,
                    "target_bucket_path_probability": 0.5,
                    "second_target_bucket": "26°C",
                    "second_target_bucket_posterior": 0.2817846528381569,
                    "second_target_bucket_path_probability": 0.2119,
                    "freshness": {"source_stale": False, "observation_age_minutes": 29.2},
                    "reliability": {"state": "high", "label": "High", "score": 0.8115},
                    "risk_explanation": (
                        "目标 25°C or below posterior 42.02%，路径可兑现 50.0%。"
                        " 第二目标 26°C posterior 28.18%，路径可兑现 21.19%。"
                    ),
                },
                "target_realization_capital_plan": {
                    "ok": True,
                    "version": "target_realization_capital_plan_v2",
                    "target_bucket": "25°C or below",
                    "target_posterior": 0.26122337983261507,
                    "second_target_bucket": "26°C",
                    "second_target_posterior": 0.2017124436814672,
                    "legs": [
                        {
                            "role": "target_yes",
                            "bucket": "25°C or below",
                            "side": "YES",
                            "posterior": 0.26122337983261507,
                            "target_realization_path_probability": 0.3,
                            "target_realization_reliability": 0.3,
                            "target_notional": 12.03,
                        },
                        {
                            "role": "second_target_yes",
                            "bucket": "26°C",
                            "side": "YES",
                            "posterior": 0.2017124436814672,
                            "target_realization_path_probability": 0.0765,
                            "target_realization_reliability": 0.0765,
                            "target_notional": 4.2,
                        },
                    ],
                },
            }
            cur = conn.execute(
                """
                INSERT INTO event_fact_history(run_date, slug, event_date, fact_as_of, composer_version, created_at, fact_json)
                VALUES('2026-06-21', 'chengdu-test', '2026-06-21', '2026-06-21T02:30:00+00:00', 'test', '2026-06-21T02:30:00+00:00', ?)
                """,
                (json.dumps(payload, ensure_ascii=False),),
            )
            fact_id = int(cur.lastrowid)
            conn.execute(
                "INSERT INTO event_fact_latest(slug, event_date, fact_id, updated_at) VALUES('chengdu-test', '2026-06-21', ?, '2026-06-21T02:30:00+00:00')",
                (fact_id,),
            )
            conn.commit()
            conn.close()

            _TARGET_REALIZATION_FACT_PLAN_CACHE.clear()
            with patch("scan_weather_market.DB_PATH", Path(db_path)), patch.dict(
                os.environ,
                {
                    "PM_TARGET_REALIZATION_SSOT_LOCAL_FACT_ENABLED": "1",
                    "PM_TARGET_REALIZATION_SSOT_URL": "",
                    "PM_TARGET_REALIZATION_SSOT_SNAPSHOT_FILE": "",
                },
                clear=False,
            ):
                legs, plan = _load_target_realization_fact_plan("chengdu-test", "2026-06-21")

            self.assertEqual(plan["ssot_load_source"], "event_fact_latest_local")
            self.assertNotIn("target_realization_path_distribution_aligned", plan)
            self.assertAlmostEqual(float(plan["target_posterior"]), 0.26122337983261507, places=9)
            self.assertAlmostEqual(float(plan["second_target_posterior"]), 0.2017124436814672, places=9)
            self.assertAlmostEqual(
                float(legs[("25°C or below", "YES")]["target_realization_path_probability"]),
                0.3,
                places=9,
            )
            self.assertAlmostEqual(
                float(legs[("26°C", "YES")]["target_realization_path_probability"]),
                0.0765,
                places=9,
            )

    def test_buy_collateral_requirement_includes_weather_fee(self) -> None:
        required = _estimate_buy_collateral_required_usdc(
            price=0.005,
            size=200,
            order_type="GTC",
            fee_rate=0.05,
        )
        self.assertAlmostEqual(float(required), 1.05, places=6)

    def test_max_buy_size_respects_available_balance_and_fee(self) -> None:
        size = _max_buy_size_for_collateral_usdc(
            available_usdc=2.0,
            reserve_usdc=0.05,
            price=0.01,
            fee_rate=0.05,
        )
        self.assertAlmostEqual(float(size), 185.7142, places=4)

    def test_auto_order_record_price_matches_clob_min_tick(self) -> None:
        self.assertAlmostEqual(float(_auto_order_record_price(0.0005)), 0.001, places=6)

    def test_bayes_bimodal_size_scale_is_retired_and_diagnostic_only(self) -> None:
        self.assertAlmostEqual(
            _bayes_bimodal_size_scale(
                {"q_bayes_bimodal_risk": True, "q_bayes_bimodal_size_scale": 0.42}
            ),
            1.0,
            places=9,
        )
        self.assertAlmostEqual(
            _bayes_bimodal_size_scale(
                {"q_bayes_bimodal_risk": False, "q_bayes_bimodal_size_scale": 0.42}
            ),
            1.0,
            places=9,
        )
        self.assertAlmostEqual(_bayes_bimodal_size_scale({"q_bayes_bimodal_risk": True, "q_bayes_bimodal_size_scale": 2.0}), 1.0, places=9)
        with patch.dict(os.environ, {"PM_BAYES_BIMODAL_SIZE_SCALE_ENABLED": "0"}, clear=False):
            self.assertAlmostEqual(
                _bayes_bimodal_size_scale(
                    {"q_bayes_bimodal_risk": True, "q_bayes_bimodal_size_scale": 0.42}
                ),
                1.0,
                places=9,
            )


if __name__ == "__main__":
    unittest.main()
