import unittest

from edge_signal_runtime import enrich_signal_observability


class EdgeSignalRuntimeTests(unittest.TestCase):
    def test_enrich_signal_observability_backfills_missing_signal_fields(self) -> None:
        details = {
            "q_round": 0.4570940170940171,
            "q_calibrated": 0.42707611202345735,
            "q_edge": 0.4570940170940171,
            "q_round_model_spread": 0.13538461538461538,
        }
        out = enrich_signal_observability(details)
        self.assertAlmostEqual(float(out["q_round_model_spread"]), 0.13538461538461538, places=12)
        self.assertAlmostEqual(float(out["edge_signal_alpha"]), 0.42386324786324786, places=12)
        self.assertAlmostEqual(float(out["edge_signal_sigma"]), 0.061, places=12)
        self.assertAlmostEqual(float(out["edge_signal_lcb"]), 0.3238232478632479, places=12)
        self.assertAlmostEqual(float(out["edge_signal_confidence"]), 0.8257142857142857, places=12)
        self.assertAlmostEqual(float(out["edge_signal_components_q_edge"]), 0.4570940170940171, places=12)
        self.assertAlmostEqual(float(out["edge_signal_components_model_spread"]), 0.13538461538461538, places=12)
        self.assertAlmostEqual(float(out["edge_signal_components_consensus_adjustment"]), 0.0, places=12)

    def test_enrich_signal_observability_preserves_existing_lcb_and_aliases_model_spread(self) -> None:
        details = {
            "q_edge": 0.42,
            "model_spread": 0.10,
            "edge_signal_lcb": 0.12,
        }
        out = enrich_signal_observability(details)
        self.assertAlmostEqual(float(out["q_round_model_spread"]), 0.10, places=12)
        self.assertAlmostEqual(float(out["edge_signal_lcb"]), 0.12, places=12)
        self.assertIn("edge_signal_alpha", out)
        self.assertIn("edge_signal_sigma", out)
        self.assertIn("edge_signal_components_q_edge", out)


if __name__ == "__main__":
    unittest.main()
