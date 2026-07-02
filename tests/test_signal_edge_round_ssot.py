import math
import unittest


class TestSignalEdgeRoundSSOT(unittest.TestCase):
    def test_signal_edge_round_uses_q_raw_not_calibrated(self) -> None:
        from ui.server import _compute_edge_fields_in_place

        row = {
            "q_raw": 0.20,
            "q_calibrated": 0.80,
            "q_entry": 0.80,
            "best_ask": 0.10,
            "best_ask_no": 0.90,
            "spread": 0.02,
            "spread_no": 0.02,
            "structure_factor": 0.5,
            "time_trend": 0.0,
            "time_freshness": 1.0,
            "time_factor_yes": 0.5,
            "time_factor_no": 0.5,
        }
        _compute_edge_fields_in_place(row, time_structure_min=0.1)
        self.assertTrue(math.isfinite(row.get("signal_edge_round")))
        self.assertAlmostEqual(float(row.get("signal_edge_round")), 0.10, places=9)
        self.assertAlmostEqual(
            float(row.get("target_execution_margin_entry_yes")),
            float(row.get("exec_edge_entry_yes")),
            places=9,
        )
        self.assertEqual(row.get("target_realization_alias_mode"), "legacy_read_alias")


if __name__ == "__main__":
    unittest.main()
