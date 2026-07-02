import multiprocessing
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from edge_exec_runtime import estimate_tradeable_edge_floor
from edge_policy_runtime import evaluate_tradeability, load_edge_policy_from_env
import scan_weather_market as swm


class EdgeTradeabilityRuntimeTests(unittest.TestCase):
    def test_load_edge_policy_from_env_defaults_to_soft_gates(self) -> None:
        with patch.dict(
            os.environ,
            {},
            clear=True,
        ):
            policy = load_edge_policy_from_env()
            self.assertFalse(bool(policy.get("require_exec_audit")))
            self.assertFalse(bool(policy.get("require_v2")))

    def test_load_edge_policy_from_env_allows_explicit_overrides(self) -> None:
        with patch.dict(
            os.environ,
            {
                "PM_EDGE_POLICY_REQUIRE_EXEC_AUDIT": "1",
                "PM_EDGE_POLICY_REQUIRE_V2": "1",
            },
            clear=True,
        ):
            policy = load_edge_policy_from_env()
            self.assertTrue(bool(policy.get("require_exec_audit")))
            self.assertTrue(bool(policy.get("require_v2")))

    def test_v2_semantics_are_passthrough(self) -> None:
        out = estimate_tradeable_edge_floor(
            edge_raw=0.0042,
            p_reach=0.03,
            p_reach_lcb=0.01,
            p_reach_ucb=0.06,
            spread=0.04,
            fee_cost=0.0,
            hold_cost=0.001,
            slippage_buffer=0.003,
            edge_semantics="realized_v2",
        )
        self.assertAlmostEqual(float(out.get("edge_expected_tradeable")), 0.0042, places=9)
        self.assertAlmostEqual(float(out.get("edge_lcb_tradeable")), 0.0042, places=9)
        self.assertEqual(out.get("edge_floor_mode"), "realized_v2_passthrough")
        self.assertFalse(bool(out.get("path_gate_enforced")))
        self.assertAlmostEqual(float(out.get("edge_miss_cost")), 0.0, places=9)

    def test_raw_semantics_keep_path_floor(self) -> None:
        out = estimate_tradeable_edge_floor(
            edge_raw=0.02,
            p_reach=0.5,
            p_reach_lcb=0.4,
            p_reach_ucb=0.6,
            spread=0.02,
            fee_cost=0.001,
            hold_cost=0.002,
            slippage_buffer=0.003,
            edge_semantics="raw_exec",
        )
        self.assertEqual(out.get("edge_floor_mode"), "raw_exec_path_floor")
        self.assertTrue(bool(out.get("path_gate_enforced")))
        self.assertLess(float(out.get("edge_lcb_tradeable")), float(out.get("edge_expected_tradeable")))

    def test_default_slippage_does_not_count_half_spread_twice(self) -> None:
        out = estimate_tradeable_edge_floor(
            edge_raw=0.02,
            p_reach=0.5,
            p_reach_lcb=0.4,
            p_reach_ucb=0.6,
            spread=0.02,
        )
        self.assertAlmostEqual(float(out.get("edge_slippage_buffer")), 0.003, places=9)
        self.assertAlmostEqual(float(out.get("edge_miss_cost")), 0.013, places=9)

    def test_policy_can_skip_path_gate_for_v2_inputs(self) -> None:
        out = evaluate_tradeability(
            policy={
                "enabled": True,
                "mode": "enforced",
                "signal_lcb_min": 0.0,
                "path_lcb_min": 0.1,
                "edge_lcb_min": 0.0,
                "require_exec_audit": True,
                "require_v2": True,
            },
            key="case",
            signal_lcb=0.05,
            path_lcb=0.01,
            edge_lcb=0.002,
            v2_value=0.002,
            exec_audit_gate_pass=True,
            enforce_path_gate=False,
        )
        self.assertTrue(bool(out.get("pass")))
        self.assertFalse(bool(out.get("blocked")))
        self.assertFalse(bool(out.get("path_gate_enforced")))
        self.assertNotIn("path_lcb_below_min", out.get("reasons") or [])

    def test_entry_realizable_only_gate_allows_positive_realizable(self) -> None:
        blocked = swm._entry_tradeability_required_block_detail(
            {
                "edge_realizable": 0.12,
                "edge_tradeability_blocked": True,
                "edge_tradeability_soft_blocked": True,
                "edge_tradeability_hard_block_disabled": True,
                "edge_tradeability_soft_block_reason": "entry_only_realizable",
                "edge_tradeability_reasons": "signal_lcb_below_min",
                "edge_signal_lcb": 0.03,
                "edge_path_p_reach_lcb": 0.70,
                "edge_tradeability_profit_value": 0.10,
                "edge_tradeability_profit_min": 0.0005,
            },
            min_edge_realizable=0.0,
        )
        self.assertIsNone(blocked)

    def test_entry_realizable_only_gate_blocks_nonpositive_realizable(self) -> None:
        candidate = {
            "edge_realizable": 0.0,
            "edge_tradeability_blocked": True,
            "edge_tradeability_soft_blocked": True,
            "edge_tradeability_hard_block_disabled": True,
            "edge_tradeability_soft_block_reason": "entry_only_realizable",
            "edge_tradeability_reasons": "signal_lcb_below_min",
        }
        blocked = swm._entry_tradeability_required_block_detail(
            candidate,
            min_edge_realizable=0.0,
        )
        self.assertIsNone(blocked)
        self.assertTrue(candidate["edge_realizable_soft_blocked"])
        self.assertEqual(candidate["edge_realizable_soft_block_reason"], "entry_only_realizable")
        self.assertIn("已降级为软诊断", candidate["edge_realizable_soft_block_detail"])

    def test_signal_soft_band_shadow_keeps_block_but_marks_candidate(self) -> None:
        candidate = {
            "strategy": "lcb_global",
            "side": "NO",
            "size": 10.0,
            "edge_tradeability_blocked": True,
            "edge_tradeability_reasons": "signal_lcb_below_min",
            "edge_signal_lcb": 0.29,
            "edge_path_p_reach_lcb": 0.42,
            "edge_tradeability_path_lcb_min": 0.10,
            "edge_tradeability_profit_value": 0.015,
            "edge_tradeability_profit_min": 0.0,
        }
        meta = swm._apply_signal_lcb_soft_band_override(
            candidate,
            band_min=0.25,
            band_max=0.35,
            mode="shadow",
            size_ratio=0.35,
        )
        self.assertIsInstance(meta, dict)
        self.assertFalse(bool(meta.get("overridden")))
        self.assertTrue(candidate["edge_tradeability_blocked"])
        self.assertTrue(candidate["edge_tradeability_signal_soft_band_matched"])
        self.assertTrue(candidate["edge_tradeability_signal_soft_band_eligible"])
        self.assertTrue(candidate["edge_tradeability_signal_soft_band_side_allowed"])
        self.assertEqual(candidate["edge_tradeability_signal_soft_band_side_mode"], "no_only")
        self.assertTrue(candidate["edge_tradeability_shadow_block"])
        self.assertTrue(candidate["edge_tradeability_signal_soft_band_shadow"])
        self.assertEqual(candidate["edge_tradeability_signal_soft_band_mode"], "shadow")
        self.assertAlmostEqual(float(candidate["size"]), 10.0, places=9)

    def test_signal_soft_band_small_live_scales_size_and_clears_block(self) -> None:
        candidate = {
            "strategy": "lcb_global",
            "side": "NO",
            "size": 10.0,
            "edge_tradeability_blocked": True,
            "edge_tradeability_reasons": "signal_lcb_below_min",
            "edge_signal_lcb": 0.31,
            "edge_path_p_reach_lcb": 0.44,
            "edge_tradeability_path_lcb_min": 0.10,
            "edge_tradeability_profit_value": 0.02,
            "edge_tradeability_profit_min": 0.0,
        }
        meta = swm._apply_signal_lcb_soft_band_override(
            candidate,
            band_min=0.25,
            band_max=0.35,
            mode="small_live",
            size_ratio=0.35,
        )
        self.assertIsInstance(meta, dict)
        self.assertTrue(bool(meta.get("overridden")))
        self.assertFalse(candidate["edge_tradeability_blocked"])
        self.assertTrue(candidate["edge_tradeability_signal_soft_band_matched"])
        self.assertTrue(candidate["edge_tradeability_signal_soft_band_eligible"])
        self.assertTrue(candidate["edge_tradeability_signal_soft_band_side_allowed"])
        self.assertTrue(candidate["edge_tradeability_signal_soft_band_override"])
        self.assertAlmostEqual(float(candidate["edge_tradeability_signal_soft_band_size_before"]), 10.0, places=9)
        self.assertAlmostEqual(float(candidate["size"]), 3.5, places=9)

    def test_signal_soft_band_no_only_blocks_yes_side_but_keeps_observability(self) -> None:
        candidate = {
            "strategy": "lcb_global",
            "side": "YES",
            "size": 10.0,
            "edge_tradeability_blocked": True,
            "edge_tradeability_reasons": "signal_lcb_below_min",
            "edge_signal_lcb": 0.29,
            "edge_path_p_reach_lcb": 0.42,
            "edge_tradeability_path_lcb_min": 0.10,
            "edge_tradeability_profit_value": 0.015,
            "edge_tradeability_profit_min": 0.0,
        }
        meta = swm._apply_signal_lcb_soft_band_override(
            candidate,
            band_min=0.25,
            band_max=0.35,
            mode="small_live",
            size_ratio=0.35,
        )
        self.assertIsInstance(meta, dict)
        self.assertFalse(bool(meta.get("overridden")))
        self.assertTrue(bool(meta.get("side_filtered")))
        self.assertTrue(candidate["edge_tradeability_blocked"])
        self.assertTrue(candidate["edge_tradeability_signal_soft_band_matched"])
        self.assertFalse(candidate["edge_tradeability_signal_soft_band_eligible"])
        self.assertFalse(candidate["edge_tradeability_signal_soft_band_side_allowed"])
        self.assertEqual(candidate["edge_tradeability_signal_soft_band_side_mode"], "no_only")
        self.assertAlmostEqual(float(candidate["size"]), 10.0, places=9)

    def test_signal_soft_band_secondary_tier_small_live_scales_no_side(self) -> None:
        candidate = {
            "strategy": "lcb_global",
            "side": "NO",
            "size": 10.0,
            "edge_tradeability_blocked": True,
            "edge_tradeability_reasons": "signal_lcb_below_min",
            "edge_signal_lcb": 0.22,
            "edge_path_p_reach_lcb": 0.44,
            "edge_tradeability_path_lcb_min": 0.10,
            "edge_tradeability_profit_value": 0.02,
            "edge_tradeability_profit_min": 0.0,
        }
        meta = swm._apply_signal_lcb_soft_band_tiers(
            candidate,
            tiers=[
                {
                    "tier": "primary",
                    "mode": "small_live",
                    "band_min": 0.25,
                    "band_max": 0.35,
                    "size_ratio": 0.35,
                    "side_mode": "no_only",
                },
                {
                    "tier": "secondary",
                    "mode": "small_live",
                    "band_min": 0.20,
                    "band_max": 0.25,
                    "size_ratio": 0.20,
                    "side_mode": "no_only",
                },
            ],
        )
        self.assertIsInstance(meta, dict)
        self.assertEqual(meta.get("tier"), "secondary")
        self.assertTrue(bool(meta.get("overridden")))
        self.assertEqual(candidate["edge_tradeability_signal_soft_band_tier"], "secondary")
        self.assertFalse(candidate["edge_tradeability_blocked"])
        self.assertAlmostEqual(float(candidate["size"]), 2.0, places=9)

    def test_signal_soft_band_secondary_tier_yes_side_is_filtered(self) -> None:
        candidate = {
            "strategy": "lcb_global",
            "side": "YES",
            "size": 10.0,
            "edge_tradeability_blocked": True,
            "edge_tradeability_reasons": "signal_lcb_below_min",
            "edge_signal_lcb": 0.22,
            "edge_path_p_reach_lcb": 0.44,
            "edge_tradeability_path_lcb_min": 0.10,
            "edge_tradeability_profit_value": 0.02,
            "edge_tradeability_profit_min": 0.0,
        }
        meta = swm._apply_signal_lcb_soft_band_tiers(
            candidate,
            tiers=[
                {
                    "tier": "primary",
                    "mode": "small_live",
                    "band_min": 0.25,
                    "band_max": 0.35,
                    "size_ratio": 0.35,
                    "side_mode": "no_only",
                },
                {
                    "tier": "secondary",
                    "mode": "small_live",
                    "band_min": 0.20,
                    "band_max": 0.25,
                    "size_ratio": 0.20,
                    "side_mode": "no_only",
                },
            ],
        )
        self.assertIsInstance(meta, dict)
        self.assertEqual(meta.get("tier"), "secondary")
        self.assertTrue(bool(meta.get("side_filtered")))
        self.assertEqual(candidate["edge_tradeability_signal_soft_band_tier"], "secondary")
        self.assertTrue(candidate["edge_tradeability_signal_soft_band_matched"])
        self.assertFalse(candidate["edge_tradeability_signal_soft_band_eligible"])
        self.assertTrue(candidate["edge_tradeability_blocked"])
        self.assertAlmostEqual(float(candidate["size"]), 10.0, places=9)

    def test_signal_soft_band_tertiary_tier_small_live_scales_no_side(self) -> None:
        candidate = {
            "strategy": "lcb_global",
            "side": "NO",
            "size": 10.0,
            "edge_tradeability_blocked": True,
            "edge_tradeability_reasons": "signal_lcb_below_min",
            "edge_signal_lcb": 0.17,
            "edge_path_p_reach_lcb": 0.44,
            "edge_tradeability_path_lcb_min": 0.10,
            "edge_tradeability_profit_value": 0.02,
            "edge_tradeability_profit_min": 0.0,
        }
        meta = swm._apply_signal_lcb_soft_band_tiers(
            candidate,
            tiers=[
                {
                    "tier": "primary",
                    "mode": "small_live",
                    "band_min": 0.25,
                    "band_max": 0.35,
                    "size_ratio": 0.35,
                    "side_mode": "no_only",
                },
                {
                    "tier": "secondary",
                    "mode": "small_live",
                    "band_min": 0.20,
                    "band_max": 0.25,
                    "size_ratio": 0.20,
                    "side_mode": "no_only",
                },
                {
                    "tier": "tertiary",
                    "mode": "small_live",
                    "band_min": 0.15,
                    "band_max": 0.20,
                    "size_ratio": 0.10,
                    "side_mode": "no_only",
                },
            ],
        )
        self.assertIsInstance(meta, dict)
        self.assertEqual(meta.get("tier"), "tertiary")
        self.assertTrue(bool(meta.get("overridden")))
        self.assertEqual(candidate["edge_tradeability_signal_soft_band_tier"], "tertiary")
        self.assertFalse(candidate["edge_tradeability_blocked"])
        self.assertAlmostEqual(float(candidate["size"]), 1.0, places=9)

    def test_signal_soft_band_tertiary_tier_yes_side_is_filtered(self) -> None:
        candidate = {
            "strategy": "lcb_global",
            "side": "YES",
            "size": 10.0,
            "edge_tradeability_blocked": True,
            "edge_tradeability_reasons": "signal_lcb_below_min",
            "edge_signal_lcb": 0.17,
            "edge_path_p_reach_lcb": 0.44,
            "edge_tradeability_path_lcb_min": 0.10,
            "edge_tradeability_profit_value": 0.02,
            "edge_tradeability_profit_min": 0.0,
        }
        meta = swm._apply_signal_lcb_soft_band_tiers(
            candidate,
            tiers=[
                {
                    "tier": "primary",
                    "mode": "small_live",
                    "band_min": 0.25,
                    "band_max": 0.35,
                    "size_ratio": 0.35,
                    "side_mode": "no_only",
                },
                {
                    "tier": "secondary",
                    "mode": "small_live",
                    "band_min": 0.20,
                    "band_max": 0.25,
                    "size_ratio": 0.20,
                    "side_mode": "no_only",
                },
                {
                    "tier": "tertiary",
                    "mode": "small_live",
                    "band_min": 0.15,
                    "band_max": 0.20,
                    "size_ratio": 0.10,
                    "side_mode": "no_only",
                },
            ],
        )
        self.assertIsInstance(meta, dict)
        self.assertEqual(meta.get("tier"), "tertiary")
        self.assertTrue(bool(meta.get("side_filtered")))
        self.assertEqual(candidate["edge_tradeability_signal_soft_band_tier"], "tertiary")
        self.assertTrue(candidate["edge_tradeability_signal_soft_band_matched"])
        self.assertFalse(candidate["edge_tradeability_signal_soft_band_eligible"])
        self.assertTrue(candidate["edge_tradeability_blocked"])
        self.assertAlmostEqual(float(candidate["size"]), 10.0, places=9)

    def test_tradeability_deferral_flags_mark_requested_paths(self) -> None:
        candidate = {}
        meta = swm._mark_tradeability_deferral_flags(
            candidate,
            enable_near_miss=True,
            enable_signal_soft_band=True,
        )
        self.assertTrue(bool(meta.get("deferred")))
        self.assertTrue(bool(meta.get("defer_to_near_miss")))
        self.assertTrue(bool(meta.get("defer_to_signal_soft_band")))
        self.assertTrue(candidate["edge_tradeability_deferred_to_near_miss"])
        self.assertTrue(candidate["edge_tradeability_deferred_to_signal_soft_band"])

    def test_consensus_yes_top_override_requires_nonnegative_tradeability_ev(self) -> None:
        candidate = {
            "strategy": "lcb_global",
            "side": "YES",
            "price": 0.40,
            "size": 10.0,
            "lcb_consensus_yes_top_matched": True,
            "lcb_consensus_yes_top_eligible": True,
            "edge_tradeability_blocked": True,
            "edge_tradeability_reasons": "edge_lcb_below_min",
            "exec_edge_entry": 0.03,
            "edge_tradeability_profit_value": -0.001,
        }

        meta = swm._apply_lcb_global_consensus_yes_top_override(
            candidate,
            mode="small_live",
            size_ratio=0.04,
            price_max=None,
            exec_edge_min=0.0,
            profit_floor=0.0,
        )

        self.assertIsInstance(meta, dict)
        self.assertFalse(bool(meta.get("overridden")))
        self.assertEqual(meta.get("fail_reason"), "profit_not_positive")
        self.assertTrue(candidate["edge_tradeability_blocked"])

    def test_plain_lcb_peak_no_soft_override_scales_lcb_pass(self) -> None:
        candidate = {
            "strategy": "lcb_global",
            "side": "NO",
            "size": 10.0,
            "edge_signal_lcb": 0.40,
            "edge_tradeability_signal_lcb_min": 0.0,
            "edge_path_p_reach_lcb": 0.80,
            "edge_tradeability_path_lcb_min": 0.0,
            "edge_tradeability_profit_value": 0.025,
            "edge_tradeability_profit_min": -0.001,
            "plain_lcb_peak_no_filtered": True,
        }
        meta = swm._apply_plain_lcb_peak_no_soft_override(
            candidate,
            size_ratio=0.35,
            min_entry_size=2.0,
        )
        self.assertTrue(bool(meta.get("overridden")))
        self.assertTrue(candidate["plain_lcb_peak_no_soft_eligible"])
        self.assertTrue(candidate["plain_lcb_peak_no_soft_override"])
        self.assertFalse(candidate["plain_lcb_peak_no_filtered"])
        self.assertAlmostEqual(float(candidate["plain_lcb_peak_no_soft_size_before"]), 10.0, places=9)
        self.assertAlmostEqual(float(candidate["size"]), 3.5, places=9)

    def test_plain_lcb_peak_no_soft_override_keeps_block_when_lcb_fails(self) -> None:
        candidate = {
            "strategy": "lcb_global",
            "side": "NO",
            "size": 10.0,
            "edge_signal_lcb": -0.01,
            "edge_tradeability_signal_lcb_min": 0.0,
            "edge_path_p_reach_lcb": 0.80,
            "edge_tradeability_path_lcb_min": 0.0,
            "edge_tradeability_profit_value": 0.025,
            "edge_tradeability_profit_min": -0.001,
            "plain_lcb_peak_no_filtered": True,
        }
        meta = swm._apply_plain_lcb_peak_no_soft_override(
            candidate,
            size_ratio=0.35,
            min_entry_size=2.0,
        )
        self.assertFalse(bool(meta.get("overridden")))
        self.assertFalse(candidate["plain_lcb_peak_no_soft_eligible"])
        self.assertIn("signal_lcb_below_min", candidate["plain_lcb_peak_no_soft_reasons"])
        self.assertTrue(candidate["plain_lcb_peak_no_filtered"])
        self.assertAlmostEqual(float(candidate["size"]), 10.0, places=9)

    def test_signal_near_zero_small_live_requires_positive_edge(self) -> None:
        candidate = {
            "strategy": "lcb_global",
            "side": "YES",
            "size": 10.0,
            "edge_tradeability_blocked": True,
            "edge_tradeability_reasons": "signal_lcb_below_min",
            "edge_signal_lcb": -0.012,
            "edge_path_p_reach_lcb": 0.45,
            "edge_tradeability_profit_value": 0.018,
        }
        meta = swm._apply_signal_lcb_near_zero_override(
            candidate,
            mode="small_live",
            band_min=-0.025,
            band_max=0.0,
            path_min=0.35,
            edge_min=0.005,
            size_ratio=0.20,
        )
        self.assertTrue(bool(meta.get("overridden")))
        self.assertFalse(candidate["edge_tradeability_blocked"])
        self.assertTrue(candidate["edge_tradeability_signal_near_zero_override"])
        self.assertEqual(candidate["edge_tradeability_signal_near_zero_tier"], "primary")
        self.assertAlmostEqual(float(candidate["edge_tradeability_signal_near_zero_size_before"]), 10.0, places=9)
        self.assertAlmostEqual(float(candidate["size"]), 2.0, places=9)

    def test_signal_near_zero_deep_tier_requires_stronger_edge(self) -> None:
        candidate = {
            "strategy": "lcb_global",
            "side": "YES",
            "size": 10.0,
            "edge_tradeability_blocked": True,
            "edge_tradeability_reasons": "signal_lcb_below_min",
            "edge_signal_lcb": -0.06,
            "edge_path_p_reach_lcb": 0.45,
            "edge_tradeability_profit_value": 0.03,
        }
        meta = swm._apply_signal_lcb_near_zero_override(
            candidate,
            mode="small_live",
            band_min=-0.075,
            band_max=-0.025,
            path_min=0.40,
            edge_min=0.020,
            size_ratio=0.12,
            min_entry_size=2.0,
            tier="deep",
        )
        self.assertTrue(bool(meta.get("overridden")))
        self.assertEqual(candidate["edge_tradeability_signal_near_zero_tier"], "deep")
        self.assertFalse(candidate["edge_tradeability_blocked"])
        self.assertAlmostEqual(float(candidate["size"]), 2.0, places=9)

    def test_signal_near_zero_small_live_keeps_block_when_edge_low(self) -> None:
        candidate = {
            "strategy": "lcb_global",
            "side": "YES",
            "size": 10.0,
            "edge_tradeability_blocked": True,
            "edge_tradeability_reasons": "signal_lcb_below_min",
            "edge_signal_lcb": -0.012,
            "edge_path_p_reach_lcb": 0.45,
            "edge_tradeability_profit_value": 0.001,
        }
        meta = swm._apply_signal_lcb_near_zero_override(
            candidate,
            mode="small_live",
            band_min=-0.025,
            band_max=0.0,
            path_min=0.35,
            edge_min=0.005,
            size_ratio=0.20,
        )
        self.assertIsNone(meta)
        self.assertTrue(candidate["edge_tradeability_blocked"])
        self.assertAlmostEqual(float(candidate["size"]), 10.0, places=9)

    def test_deferred_sort_missing_candidate_is_deprioritized(self) -> None:
        self.assertEqual(
            swm._candidate_sort_score_value(
                {
                    "edge_score": 88.0,
                    "edge_realizable_sort_missing_deferred": True,
                }
            ),
            -1e9,
        )
        self.assertAlmostEqual(
            swm._candidate_sort_score_value(
                {
                    "edge_score": 88.0,
                }
            ),
            88.0,
            places=9,
        )

    def test_auto_trade_event_worker_uses_resilient_decision_write_defaults(self) -> None:
        keys = [
            "PM_DB_BUSY_TIMEOUT_MS",
            "PM_AUTO_TRADE_DECISION_WRITE_RETRY_ATTEMPTS",
            "PM_AUTO_TRADE_DECISION_WRITE_RETRY_SLEEP_SEC",
            "PM_AUTO_TRADE_EVENT_WORKER_DB_BUSY_TIMEOUT_MS",
            "PM_AUTO_TRADE_EVENT_WORKER_DECISION_WRITE_RETRY_ATTEMPTS",
            "PM_AUTO_TRADE_EVENT_WORKER_DECISION_WRITE_RETRY_SLEEP_SEC",
            "PM_AUTO_TRADE_EVENT_WORKER_ALLOW_DIRECT_FALLBACK",
            "PM_AUTO_TRADE_EVENT_WORKER_PREFETCH_MARKET_TICKS",
            "PM_SCAN_FORCE_DIRECT",
            "PM_SCAN_PROXY_DUAL_MODE",
            "PM_CLOB_FORCE_DIRECT",
            "PM_CLOB_FORCE_PROXY",
            "PM_CLOB_DIRECT_FIRST",
            "PM_CLOB_DIRECT_RETRIES",
            "PM_CLOB_RETRY_MAX",
        ]
        saved_env = {key: os.environ.get(key) for key in keys}
        observed = {}
        queue = multiprocessing.Queue()
        try:
            for key in keys:
                os.environ.pop(key, None)

            def _fake_execute_auto_trades(*args, **kwargs) -> None:
                observed["busy_timeout_ms"] = os.environ.get("PM_DB_BUSY_TIMEOUT_MS")
                observed["retry_attempts"] = os.environ.get(
                    "PM_AUTO_TRADE_DECISION_WRITE_RETRY_ATTEMPTS"
                )
                observed["retry_sleep_sec"] = os.environ.get(
                    "PM_AUTO_TRADE_DECISION_WRITE_RETRY_SLEEP_SEC"
                )
                observed["entry_prefetch_market_ticks"] = os.environ.get(
                    "PM_AUTO_TRADE_ENTRY_PREFETCH_MARKET_TICKS"
                )
                observed["scan_force_direct"] = os.environ.get("PM_SCAN_FORCE_DIRECT")
                observed["scan_proxy_dual"] = os.environ.get("PM_SCAN_PROXY_DUAL_MODE")
                observed["clob_direct_first"] = os.environ.get("PM_CLOB_DIRECT_FIRST")
                observed["clob_direct_retries"] = os.environ.get("PM_CLOB_DIRECT_RETRIES")
                observed["clob_retry_max"] = os.environ.get("PM_CLOB_RETRY_MAX")

            with patch.object(swm, "execute_auto_trades", side_effect=_fake_execute_auto_trades):
                swm._auto_trade_event_worker(
                    "2026-04-18",
                    "2026-04-18",
                    {},
                    False,
                    None,
                    queue,
                    event_timeout_sec=1.0,
                )

            result = queue.get(timeout=1.0)
            self.assertTrue(result.get("ok"))
            self.assertEqual(observed.get("busy_timeout_ms"), "5000")
            self.assertEqual(observed.get("retry_attempts"), "5")
            self.assertEqual(observed.get("retry_sleep_sec"), "0.300")
            self.assertEqual(observed.get("entry_prefetch_market_ticks"), "1")
            self.assertIsNone(observed.get("scan_force_direct"))
            self.assertIsNone(observed.get("scan_proxy_dual"))
            self.assertIsNone(observed.get("clob_direct_first"))
            self.assertIsNone(observed.get("clob_direct_retries"))
            self.assertIsNone(observed.get("clob_retry_max"))
        finally:
            try:
                queue.close()
            except Exception:
                pass
            for key, value in saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_auto_trade_event_worker_caps_stale_refresh_budget(self) -> None:
        keys = [
            "PM_AUTO_TRADE_EVENT_WORKER_REFRESH_ON_STALE",
            "PM_AUTO_TRADE_EVENT_WORKER_REFRESH_TIMEOUT_SEC",
            "PM_AUTO_TRADE_EVENT_WORKER_REFRESH_RETRIES",
            "PM_AUTO_TRADE_EVENT_WORKER_REFRESH_MAX_PER_EVENT",
            "PM_AUTO_TRADE_EVENT_WORKER_REFRESH_MAX_TOTAL_ATTEMPTS",
            "PM_AUTO_TRADE_ENTRY_REFRESH_TIMEOUT_SEC",
            "PM_AUTO_TRADE_ENTRY_FOLLOW_REFRESH_TIMEOUT_SEC",
            "PM_AUTO_TRADE_ENTRY_REFRESH_RETRIES",
            "PM_AUTO_TRADE_ENTRY_FOLLOW_REFRESH_RETRIES",
            "PM_AUTO_TRADE_ENTRY_REFRESH_MAX_PER_EVENT",
            "PM_AUTO_TRADE_ENTRY_FOLLOW_REFRESH_MAX_PER_EVENT",
            "PM_AUTO_TRADE_ENTRY_REFRESH_MAX_TOTAL_ATTEMPTS",
            "PM_AUTO_TRADE_ENTRY_FOLLOW_REFRESH_MAX_TOTAL_ATTEMPTS",
            "PM_AUTO_TRADE_EXEC_EDGE_ORDERBOOK_TIMEOUT_SEC",
            "PM_AUTO_TRADE_EXEC_EDGE_ORDERBOOK_RETRIES",
            "PM_LCB_GLOBAL_ENABLED",
            "PM_LCB_GLOBAL_OB_REFRESH_TIMEOUT_SEC",
            "PM_LCB_GLOBAL_OB_REFRESH_MAX",
            "PM_AUTO_TRADE_EVENT_WORKER_CLOB_PRICE_AUGMENT_ENABLED",
            "PM_CLOB_PRICE_AUGMENT_ENABLED",
            "PM_CLOB_BOOK_DIRECT_FALLBACK",
        ]
        saved_env = {key: os.environ.get(key) for key in keys}
        observed = {}
        queue = multiprocessing.Queue()
        try:
            for key in keys:
                os.environ.pop(key, None)
            os.environ["PM_AUTO_TRADE_EVENT_WORKER_REFRESH_ON_STALE"] = "1"
            os.environ["PM_AUTO_TRADE_ENTRY_REFRESH_TIMEOUT_SEC"] = "10"
            os.environ["PM_AUTO_TRADE_EXEC_EDGE_ORDERBOOK_TIMEOUT_SEC"] = "9"
            os.environ["PM_AUTO_TRADE_EXEC_EDGE_ORDERBOOK_RETRIES"] = "2"
            os.environ["PM_LCB_GLOBAL_ENABLED"] = "1"
            os.environ["PM_LCB_GLOBAL_OB_REFRESH_TIMEOUT_SEC"] = "9"
            os.environ["PM_LCB_GLOBAL_OB_REFRESH_MAX"] = "99"

            def _fake_execute_auto_trades(*args, **kwargs) -> None:
                observed["entry_timeout"] = os.environ.get(
                    "PM_AUTO_TRADE_ENTRY_REFRESH_TIMEOUT_SEC"
                )
                observed["follow_timeout"] = os.environ.get(
                    "PM_AUTO_TRADE_ENTRY_FOLLOW_REFRESH_TIMEOUT_SEC"
                )
                observed["entry_retries"] = os.environ.get(
                    "PM_AUTO_TRADE_ENTRY_REFRESH_RETRIES"
                )
                observed["entry_max_per_event"] = os.environ.get(
                    "PM_AUTO_TRADE_ENTRY_REFRESH_MAX_PER_EVENT"
                )
                observed["entry_max_total"] = os.environ.get(
                    "PM_AUTO_TRADE_ENTRY_REFRESH_MAX_TOTAL_ATTEMPTS"
                )
                observed["exec_edge_ob_timeout"] = os.environ.get(
                    "PM_AUTO_TRADE_EXEC_EDGE_ORDERBOOK_TIMEOUT_SEC"
                )
                observed["exec_edge_ob_retries"] = os.environ.get(
                    "PM_AUTO_TRADE_EXEC_EDGE_ORDERBOOK_RETRIES"
                )
                observed["lcb_ob_timeout"] = os.environ.get(
                    "PM_LCB_GLOBAL_OB_REFRESH_TIMEOUT_SEC"
                )
                observed["lcb_ob_max"] = os.environ.get(
                    "PM_LCB_GLOBAL_OB_REFRESH_MAX"
                )
                observed["lcb_enabled"] = os.environ.get("PM_LCB_GLOBAL_ENABLED")
                observed["price_augment"] = os.environ.get(
                    "PM_CLOB_PRICE_AUGMENT_ENABLED"
                )
                observed["book_direct_fallback"] = os.environ.get(
                    "PM_CLOB_BOOK_DIRECT_FALLBACK"
                )

            with patch.object(swm, "execute_auto_trades", side_effect=_fake_execute_auto_trades):
                swm._auto_trade_event_worker(
                    "2026-04-18",
                    "2026-04-18",
                    {},
                    False,
                    None,
                    queue,
                    event_timeout_sec=1.0,
                )

            result = queue.get(timeout=1.0)
            self.assertTrue(result.get("ok"))
            self.assertEqual(observed.get("entry_timeout"), "1.200")
            self.assertEqual(observed.get("follow_timeout"), "1.200")
            self.assertEqual(observed.get("entry_retries"), "1")
            self.assertEqual(observed.get("entry_max_per_event"), "1")
            self.assertEqual(observed.get("entry_max_total"), "2")
            self.assertEqual(observed.get("exec_edge_ob_timeout"), "1.200")
            self.assertEqual(observed.get("exec_edge_ob_retries"), "1")
            self.assertEqual(observed.get("lcb_ob_timeout"), "1.200")
            self.assertEqual(observed.get("lcb_ob_max"), "0")
            self.assertEqual(observed.get("lcb_enabled"), "0")
            self.assertEqual(observed.get("price_augment"), "0")
            self.assertEqual(observed.get("book_direct_fallback"), "0")
        finally:
            try:
                queue.close()
            except Exception:
                pass
            for key, value in saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_trade_engine_loop_enables_bounded_worker_stale_refresh(self) -> None:
        script = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "trade_engine_loop.sh"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'PM_AUTO_TRADE_EVENT_WORKER_REFRESH_ON_STALE="${PM_AUTO_TRADE_EVENT_WORKER_REFRESH_ON_STALE:-1}"',
            script,
        )
        self.assertIn(
            'PM_AUTO_TRADE_EVENT_WORKER_ALLOW_TOKEN_REMAP="${PM_AUTO_TRADE_EVENT_WORKER_ALLOW_TOKEN_REMAP:-1}"',
            script,
        )
        self.assertIn(
            'PM_AUTO_TRADE_EVENT_WORKER_PREFETCH_MARKET_TICKS="${PM_AUTO_TRADE_EVENT_WORKER_PREFETCH_MARKET_TICKS:-1}"',
            script,
        )
        self.assertIn('PM_AUTO_TRADE_EXEC_EDGE_ORDERBOOK_TIMEOUT_SEC="${PM_AUTO_TRADE_EXEC_EDGE_ORDERBOOK_TIMEOUT_SEC:-1.2}"', script)
        self.assertIn("PM_LCB_GLOBAL_ENABLED=0", script)
        self.assertIn("PM_LCB_GLOBAL_OB_REFRESH_MAX=0", script)
        self.assertIn('PM_AUTO_TRADE_EVENT_WORKER_CLOB_PRICE_AUGMENT_ENABLED="${PM_AUTO_TRADE_EVENT_WORKER_CLOB_PRICE_AUGMENT_ENABLED:-0}"', script)
        self.assertIn("PM_AUTO_TRADE_EVENT_TIMEOUT_CAP_SEC", script)
        self.assertIn('PM_AUTO_TRADE_EVENT_TIMEOUT_SEC="${PM_AUTO_TRADE_EVENT_TIMEOUT_CAP_SEC}"', script)

    def test_auto_trade_worker_enables_bounded_worker_stale_refresh(self) -> None:
        script = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "auto_trade_worker.sh"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'PM_AUTO_TRADE_EVENT_WORKER_REFRESH_ON_STALE="${PM_AUTO_TRADE_EVENT_WORKER_REFRESH_ON_STALE:-1}"',
            script,
        )
        self.assertIn(
            'PM_AUTO_TRADE_EVENT_WORKER_ALLOW_TOKEN_REMAP="${PM_AUTO_TRADE_EVENT_WORKER_ALLOW_TOKEN_REMAP:-1}"',
            script,
        )
        self.assertIn(
            'PM_AUTO_TRADE_EVENT_WORKER_PREFETCH_MARKET_TICKS="${PM_AUTO_TRADE_EVENT_WORKER_PREFETCH_MARKET_TICKS:-1}"',
            script,
        )
        self.assertIn('PM_AUTO_TRADE_EXEC_EDGE_ORDERBOOK_TIMEOUT_SEC="${PM_AUTO_TRADE_EXEC_EDGE_ORDERBOOK_TIMEOUT_SEC:-1.2}"', script)
        self.assertIn("PM_LCB_GLOBAL_ENABLED=0", script)
        self.assertIn("PM_LCB_GLOBAL_OB_REFRESH_MAX=0", script)
        self.assertIn('PM_AUTO_TRADE_EVENT_WORKER_CLOB_PRICE_AUGMENT_ENABLED="${PM_AUTO_TRADE_EVENT_WORKER_CLOB_PRICE_AUGMENT_ENABLED:-0}"', script)
        self.assertIn("PM_AUTO_TRADE_EVENT_TIMEOUT_CAP_SEC", script)
        self.assertIn('PM_AUTO_TRADE_EVENT_TIMEOUT_SEC="${PM_AUTO_TRADE_EVENT_TIMEOUT_CAP_SEC}"', script)

    def test_auto_trade_entry_uses_strategy_batch_by_default(self) -> None:
        script = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "auto_trade_entry_1m.sh"
        ).read_text(encoding="utf-8")
        self.assertIn('STRATEGY_BATCH_ENABLED="${PM_AUTO_TRADE_ENTRY_STRATEGY_BATCH_ENABLED:-1}"', script)
        self.assertIn('"$ROOT/scripts/weather_strategy_batch.py"', script)
        self.assertIn('STRATEGY_BATCH_DAYS_AHEAD="${PM_AUTO_TRADE_ENTRY_STRATEGY_BATCH_DAYS_AHEAD:-2}"', script)
        self.assertIn("STRATEGY_BATCH_EVENT_DATES", script)
        self.assertIn("priority_tail_no_reverse_edge,paper_trade_candidate,strict_edge_candidate_review", script)
        self.assertIn('STRATEGY_BATCH_RESOURCE_FOCUS_POLICY="${PM_RESOURCE_FOCUS_POLICY:-warn}"', script)
        self.assertIn('STRATEGY_BATCH_MIN_ENTRY_EDGE="${PM_WEATHER_STRATEGY_BATCH_MIN_ENTRY_EDGE:-0.10}"', script)
        self.assertIn('STRATEGY_BATCH_OBSERVATION_TARGET_FOCUS="${PM_AUTO_TRADE_ENTRY_OBSERVATION_TARGET_FOCUS:-1}"', script)
        self.assertIn("PM_PAPER_OBSERVATION_TARGET_JSON", script)
        self.assertIn("--resource-focus-policy", script)
        self.assertIn("--min-entry-edge", script)
        self.assertIn("--observation-target-package", script)
        self.assertIn("--observation-target-focus", script)
        self.assertIn("--enqueue", script)
        self.assertIn("--compact", script)
        self.assertIn("PM_AUTO_TRADE_ENTRY_STRATEGY_BATCH_FALLBACK_FULL", script)

    def test_scan_scripts_detect_strategy_batch_enqueue_activity(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for rel in (
            "scripts/market_refresh_1m.sh",
            "scripts/model_scan.sh",
            "scripts/q_calibrated_writeback_hourly.sh",
            "scripts/feedback_loop_hourly.sh",
        ):
            script = (root / rel).read_text(encoding="utf-8")
            self.assertIn("weather_strategy_batch.py .*--enqueue", script)

    def test_auto_trade_event_worker_mp_start_method_defaults_to_spawn(self) -> None:
        saved = os.environ.get("PM_AUTO_TRADE_EVENT_WORKER_MP_START_METHOD")
        try:
            os.environ.pop("PM_AUTO_TRADE_EVENT_WORKER_MP_START_METHOD", None)
            self.assertEqual(swm._auto_trade_event_worker_mp_start_method(), "spawn")
            os.environ["PM_AUTO_TRADE_EVENT_WORKER_MP_START_METHOD"] = "spawn"
            self.assertEqual(swm._auto_trade_event_worker_mp_start_method(), "spawn")
        finally:
            if saved is None:
                os.environ.pop("PM_AUTO_TRADE_EVENT_WORKER_MP_START_METHOD", None)
            else:
                os.environ["PM_AUTO_TRADE_EVENT_WORKER_MP_START_METHOD"] = saved

    def test_limit_runtime_shortfall_prefers_topn_when_total_zero(self) -> None:
        self.assertAlmostEqual(swm._effective_depth_total(0.0, 125.87), 125.87, places=9)
        self.assertAlmostEqual(swm._effective_depth_total(12.0, 125.87), 12.0, places=9)


if __name__ == "__main__":
    unittest.main()
