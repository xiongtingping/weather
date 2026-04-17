import unittest

import scan_weather_market as swm


class TestLcbComboIsolation(unittest.TestCase):
    def test_candidate_requires_plain_lcb_path_gate_for_lcb_global(self) -> None:
        candidate = {
            "strategy": "lcb_global",
        }
        self.assertTrue(swm._candidate_requires_plain_lcb_path_gate(candidate))

    def test_candidate_requires_plain_lcb_path_gate_skips_non_lcb(self) -> None:
        candidate = {
            "strategy": "main",
        }
        self.assertFalse(swm._candidate_requires_plain_lcb_path_gate(candidate))

    def test_tradeability_profit_value_prefers_new_field_and_falls_back_legacy(self) -> None:
        self.assertAlmostEqual(
            float(
                swm._candidate_tradeability_profit_value(
                    {"edge_tradeability_profit_value": 0.0123}
                )
            ),
            0.0123,
            places=9,
        )
        self.assertAlmostEqual(
            float(
                swm._candidate_tradeability_profit_value(
                    {"edge_tradeability_edge_lcb": 0.0456}
                )
            ),
            0.0456,
            places=9,
        )
        self.assertAlmostEqual(
            float(
                swm._candidate_tradeability_profit_min(
                    {"edge_tradeability_edge_lcb_min": 0.0789}
                )
            ),
            0.0789,
            places=9,
        )


if __name__ == "__main__":
    unittest.main()
