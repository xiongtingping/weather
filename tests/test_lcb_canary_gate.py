import unittest

import scan_weather_market as swm


class LcbCanaryGateTests(unittest.TestCase):
    def test_untradeable_extreme_tail_yes_blocked(self) -> None:
        candidate = {
            "side": "YES",
            "strategy": "lcb_global",
            "q_entry": 0.015,
            "ask": 0.01,
        }

        blocked, reason, meta = swm._candidate_is_extreme_tail_ask_untradeable(
            candidate,
            side="YES",
            row_ref={},
        )

        self.assertTrue(blocked)
        self.assertEqual(reason, "extreme_tail_yes_low_prob_low_ask")
        self.assertTrue(meta["edge_tail_untradeable"])

    def test_untradeable_extreme_tail_no_blocked(self) -> None:
        candidate = {
            "side": "NO",
            "strategy": "lcb_global",
            "q_entry": 0.995,
            "ask": 0.99,
        }

        blocked, reason, meta = swm._candidate_is_extreme_tail_ask_untradeable(
            candidate,
            side="NO",
            row_ref={},
        )

        self.assertTrue(blocked)
        self.assertEqual(reason, "extreme_tail_no_high_prob_high_ask")
        self.assertTrue(meta["edge_tail_untradeable"])

    def test_extreme_tail_untradeable_disabled(self) -> None:
        candidate = {
            "side": "YES",
            "strategy": "lcb_global",
            "q_entry": 0.01,
            "ask": 0.01,
        }

        import os

        old = os.environ.get("PM_AUTO_TRADE_EXTREME_TAIL_UNTRADEABLE")
        os.environ["PM_AUTO_TRADE_EXTREME_TAIL_UNTRADEABLE"] = "0"
        try:
            blocked, reason, _meta = swm._candidate_is_extreme_tail_ask_untradeable(
                candidate,
                side="YES",
                row_ref={},
            )
            self.assertFalse(blocked)
            self.assertEqual(reason, "")
        finally:
            if old is None:
                os.environ.pop("PM_AUTO_TRADE_EXTREME_TAIL_UNTRADEABLE", None)
            else:
                os.environ["PM_AUTO_TRADE_EXTREME_TAIL_UNTRADEABLE"] = old

    def test_positive_ev_extreme_tail_gets_stale_tick_refresh(self) -> None:
        candidate = {
            "strategy": "lcb_global",
            "q_entry": 0.07,
            "ask": 0.045,
            "edge_tradeability_blocked": True,
            "edge_tradeability_pass": False,
            "edge_tradeability_profit_value": 0.025,
            "weather_strategy_positive_ev": True,
            "weather_strategy_ev_gap": 0.014,
        }

        skip, reason, meta = swm._candidate_should_skip_extreme_tail_tick_refresh(
            candidate,
            strategy_name="lcb_global",
        )

        self.assertFalse(skip)
        self.assertEqual(reason, "")
        self.assertEqual(meta, {})

    def test_small_positive_ev_extreme_tail_gets_stale_tick_refresh(self) -> None:
        candidate = {
            "strategy": "lcb_global",
            "q_entry": 0.02,
            "ask": 0.01,
            "edge_tradeability_blocked": True,
            "edge_tradeability_pass": False,
            "edge_tradeability_profit_value": 0.001,
            "weather_strategy_positive_ev": True,
            "weather_strategy_ev_gap": 0.001,
        }

        skip, reason, meta = swm._candidate_should_skip_extreme_tail_tick_refresh(
            candidate,
            strategy_name="lcb_global",
        )

        self.assertFalse(skip)
        self.assertEqual(reason, "")
        self.assertEqual(meta, {})

    def test_non_positive_ev_extreme_tail_still_skips_refresh(self) -> None:
        candidate = {
            "strategy": "lcb_global",
            "q_entry": 0.02,
            "ask": 0.01,
            "edge_tradeability_blocked": True,
            "edge_tradeability_pass": False,
            "edge_tradeability_profit_value": -0.001,
            "weather_strategy_positive_ev": False,
            "weather_strategy_ev_gap": -0.001,
        }

        skip, reason, meta = swm._candidate_should_skip_extreme_tail_tick_refresh(
            candidate,
            strategy_name="lcb_global",
        )

        self.assertTrue(skip)
        self.assertEqual(reason, "extreme_tail_tradeability_blocked")
        self.assertAlmostEqual(float(meta["positive_ev_refresh_min"]), 0.0, places=9)

    def test_yes_canary_uses_positive_ev_without_price_cap(self) -> None:
        candidate = {
            "strategy": "lcb_global",
            "side": "YES",
            "price": 0.62,
            "exec_edge_entry": 0.012,
            "edge_tradeability_profit_value": 0.006,
            "edge_tradeability_blocked": True,
            "edge_tradeability_reasons": "edge_lcb_below_min",
            "size": 10.0,
        }

        result = swm._apply_lcb_global_lcb_canary_override(
            candidate,
            mode="small_live",
            size_ratio=0.04,
            yes_price_max=None,
            yes_exec_edge_min=0.0,
            no_price_min=0.50,
            no_price_max=0.95,
            no_exec_edge_min=0.01,
            profit_floor=0.0,
        )

        self.assertTrue(result["overridden"])
        self.assertFalse(candidate["edge_tradeability_blocked"])
        self.assertTrue(candidate["edge_tradeability_pass"])
        self.assertEqual(candidate["edge_tradeability_override_source"], "lcb_canary")
        self.assertIsNone(candidate["edge_tradeability_pass_before_override"])
        self.assertTrue(candidate["edge_tradeability_blocked_before_override"])
        self.assertEqual(candidate["edge_tradeability_lcb_canary_fail_reason"], "pass")
        self.assertNotIn("edge_tradeability_lcb_canary_yes_price_max", candidate)
        self.assertAlmostEqual(candidate["size"], 0.4, places=9)

    def test_yes_canary_requires_positive_profit_when_floor_is_zero(self) -> None:
        candidate = {
            "strategy": "lcb_global",
            "side": "YES",
            "price": 0.02,
            "exec_edge_entry": 0.012,
            "edge_tradeability_profit_value": -0.001,
            "edge_tradeability_blocked": True,
            "edge_tradeability_reasons": "signal_lcb_below_min",
            "size": 10.0,
        }

        result = swm._apply_lcb_global_lcb_canary_override(
            candidate,
            mode="small_live",
            size_ratio=0.04,
            yes_price_max=None,
            yes_exec_edge_min=0.0,
            no_price_min=0.50,
            no_price_max=0.95,
            no_exec_edge_min=0.01,
            profit_floor=0.0,
        )

        self.assertFalse(result["overridden"])
        self.assertTrue(candidate["edge_tradeability_blocked"])
        self.assertEqual(
            candidate["edge_tradeability_lcb_canary_fail_reason"],
            "profit_not_positive",
        )

    def test_no_canary_price_bounds_are_optional_when_positive_ev(self) -> None:
        candidate = {
            "strategy": "lcb_global",
            "side": "NO",
            "price": 0.98,
            "exec_edge_entry": 0.012,
            "edge_tradeability_profit_value": 0.006,
            "edge_tradeability_blocked": True,
            "edge_tradeability_reasons": "edge_lcb_below_min",
            "size": 10.0,
        }

        result = swm._apply_lcb_global_lcb_canary_override(
            candidate,
            mode="small_live",
            size_ratio=0.04,
            yes_price_max=None,
            yes_exec_edge_min=0.0,
            no_price_min=None,
            no_price_max=None,
            no_exec_edge_min=0.01,
            profit_floor=0.0,
        )

        self.assertTrue(result["overridden"])
        self.assertEqual(candidate["edge_tradeability_lcb_canary_fail_reason"], "pass")
        self.assertNotIn("edge_tradeability_lcb_canary_no_price_min", candidate)
        self.assertNotIn("edge_tradeability_lcb_canary_no_price_max", candidate)

    def test_no_canary_explicit_price_bound_still_records_failure(self) -> None:
        candidate = {
            "strategy": "lcb_global",
            "side": "NO",
            "price": 0.98,
            "exec_edge_entry": 0.012,
            "edge_tradeability_profit_value": 0.006,
            "edge_tradeability_blocked": True,
            "edge_tradeability_reasons": "edge_lcb_below_min",
            "size": 10.0,
        }

        result = swm._apply_lcb_global_lcb_canary_override(
            candidate,
            mode="small_live",
            size_ratio=0.04,
            yes_price_max=None,
            yes_exec_edge_min=0.0,
            no_price_min=None,
            no_price_max=0.95,
            no_exec_edge_min=0.01,
            profit_floor=0.0,
        )

        self.assertFalse(result["overridden"])
        self.assertEqual(
            candidate["edge_tradeability_lcb_canary_fail_reason"],
            "no_price_above_max",
        )


if __name__ == "__main__":
    unittest.main()
