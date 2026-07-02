import unittest

import scan_weather_market as swm


class EntryRealizableSoftGateTests(unittest.TestCase):
    def test_missing_realizable_is_soft_when_tradeability_exists(self) -> None:
        candidate = {
            "edge_tradeability_blocked": True,
            "edge_tradeability_reasons": "signal_lcb_below_min",
            "edge_signal_lcb": 0.12,
            "edge_path_p_reach_lcb": 0.25,
            "edge_tradeability_profit_value": -0.001,
            "edge_tradeability_profit_min": 0.0005,
        }
        blocked = swm._entry_tradeability_required_block_detail(
            candidate,
            min_edge_realizable=0.0,
        )
        self.assertIsNone(blocked)
        self.assertTrue(candidate["edge_realizable_soft_blocked"])
        self.assertEqual(candidate["edge_realizable_soft_block_reason"], "tradeability_available")

    def test_nonpositive_realizable_is_soft_diagnostic(self) -> None:
        candidate = {
            "edge_realizable": 0.0,
            "edge_tradeability_blocked": True,
            "edge_tradeability_reasons": "signal_lcb_below_min",
            "edge_tradeability_profit_value": -0.001,
            "edge_tradeability_profit_min": 0.0005,
        }
        blocked = swm._entry_tradeability_required_block_detail(
            candidate,
            min_edge_realizable=0.0,
        )
        self.assertIsNone(blocked)
        self.assertTrue(candidate["edge_realizable_soft_blocked"])
        self.assertEqual(candidate["edge_realizable_soft_block_reason"], "entry_only_realizable")

    def test_tradeability_missing_blocks_when_tradeability_snapshot_missing(self) -> None:
        blocked = swm._entry_tradeability_required_block_detail(
            {},
            min_edge_realizable=0.0,
        )
        self.assertIsNotNone(blocked)
        reason_code, reason_text, details = blocked
        self.assertEqual(reason_code, "edge_tradeability_missing")
        self.assertEqual(reason_text, "tradeability快照缺失，无法构造入场alpha硬门槛")
        self.assertFalse(details["edge_tradeability_available"])
        self.assertTrue(details["edge_tradeability_missing"])

    def test_posterior_settlement_bypasses_tradeability_snapshot_requirement(self) -> None:
        candidate = {
            "strategy_family": "posterior_settlement",
            "strategy_lane": "yes_realized_target",
            "posterior_settlement_entry_allowed": True,
            "target_bucket": "33°C",
            "target_posterior": 0.44,
            "target_realization_reliability": 0.72,
        }

        blocked = swm._entry_tradeability_required_block_detail(
            candidate,
            min_edge_realizable=0.0,
        )

        self.assertIsNone(blocked)
        self.assertEqual(candidate["entry_alpha_gate_source"], "posterior_settlement")
        self.assertTrue(candidate["edge_tradeability_diagnostic_only"])
        self.assertTrue(candidate["edge_realizable_diagnostic_only"])

    def test_trade_ev_helpers_do_not_fallback_to_edge_realizable(self) -> None:
        candidate = {
            "size": 10,
            "edge_realizable": 0.50,
            "edge_realizable_v2": 0.40,
            "exec_edge_entry": 0.30,
        }

        trade_ev, per_share, source = swm._candidate_trade_ev_usdc(candidate)
        plain_ev, plain_per_share, plain_source = swm._candidate_plain_lcb_min_total_edge_usdc(
            candidate
        )

        self.assertIsNone(trade_ev)
        self.assertIsNone(per_share)
        self.assertEqual(source, "edge_missing")
        self.assertIsNone(plain_ev)
        self.assertIsNone(plain_per_share)
        self.assertEqual(plain_source, "edge_missing")

    def test_trade_ev_helpers_use_edge_tradeability(self) -> None:
        candidate = {
            "size": 10,
            "edge_tradeability_profit_value": 0.02,
            "edge_realizable": 0.50,
        }

        trade_ev, per_share, source = swm._candidate_trade_ev_usdc(candidate)
        plain_ev, plain_per_share, plain_source = swm._candidate_plain_lcb_min_total_edge_usdc(
            candidate
        )

        self.assertAlmostEqual(trade_ev, 0.20)
        self.assertAlmostEqual(per_share, 0.02)
        self.assertEqual(source, "edge_tradeability_profit_value")
        self.assertAlmostEqual(plain_ev, 0.20)
        self.assertAlmostEqual(plain_per_share, 0.02)
        self.assertEqual(plain_source, "edge_tradeability_profit_value")


if __name__ == "__main__":
    unittest.main()
