import unittest

from edge_exec_runtime import estimate_tradeable_edge_floor
from edge_policy_runtime import evaluate_tradeability


class EdgeTradeabilityRuntimeTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
