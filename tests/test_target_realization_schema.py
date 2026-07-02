import unittest

from target_realization_schema import (
    SCHEMA_VERSION,
    apply_target_realization_aliases,
    canonical_field_for,
    copy_canonical_to_legacy,
    legacy_field_for,
    missing_canonical_aliases,
)


class TargetRealizationSchemaTests(unittest.TestCase):
    def test_aliases_legacy_edge_fields_to_target_realization_fields(self) -> None:
        row = {
            "edge_signal_lcb": 0.12,
            "edge_path_p_reach_lcb": 0.44,
            "edge_tradeability_profit_value": 0.03,
            "edge_tradeability_reasons": "signal_lcb_below_min",
            "exec_edge_entry": 0.08,
            "q_edge": 0.61,
        }
        out = apply_target_realization_aliases(row)
        self.assertAlmostEqual(float(out["target_realization_signal_lcb"]), 0.12, places=9)
        self.assertAlmostEqual(float(out["target_realization_path_lcb"]), 0.44, places=9)
        self.assertAlmostEqual(float(out["target_realization_tradeability_lcb"]), 0.03, places=9)
        self.assertEqual(out["target_realization_tradeability_reasons"], "signal_lcb_below_min")
        self.assertAlmostEqual(float(out["target_execution_margin_entry"]), 0.08, places=9)
        self.assertAlmostEqual(float(out["target_probability_delta"]), 0.61, places=9)
        self.assertEqual(out["target_realization_schema_version"], SCHEMA_VERSION)
        self.assertEqual(out["target_realization_alias_mode"], "legacy_read_alias")

    def test_aliases_preserve_existing_canonical_values(self) -> None:
        row = {
            "edge_signal_lcb": 0.12,
            "target_realization_signal_lcb": 0.30,
        }
        out = apply_target_realization_aliases(row)
        self.assertAlmostEqual(float(out["target_realization_signal_lcb"]), 0.30, places=9)

    def test_aliases_support_yes_no_suffixes(self) -> None:
        row = {
            "edge_tradeability_pass_yes": True,
            "edge_tradeability_pass_no": False,
            "exec_edge_entry_yes": 0.04,
            "exec_edge_entry_no": 0.02,
        }
        out = apply_target_realization_aliases(row)
        self.assertTrue(out["target_realization_tradeability_pass_yes"])
        self.assertFalse(out["target_realization_tradeability_pass_no"])
        self.assertAlmostEqual(float(out["target_execution_margin_entry_yes"]), 0.04, places=9)
        self.assertAlmostEqual(float(out["target_execution_margin_entry_no"]), 0.02, places=9)

    def test_reverse_alias_is_available_for_future_dual_write(self) -> None:
        row = {
            "target_realization_signal_lcb": 0.09,
            "target_realization_execution_lcb": 0.05,
        }
        out = copy_canonical_to_legacy(row)
        self.assertAlmostEqual(float(out["edge_signal_lcb"]), 0.09, places=9)
        self.assertAlmostEqual(float(out["edge_tradeability_edge_lcb"]), 0.05, places=9)

    def test_field_lookup_and_missing_report(self) -> None:
        self.assertEqual(legacy_field_for("target_realization_signal_lcb"), "edge_signal_lcb")
        self.assertEqual(canonical_field_for("edge_signal_lcb"), "target_realization_signal_lcb")
        missing = missing_canonical_aliases({"edge_signal_lcb_yes": 0.2}, suffix="_yes")
        self.assertEqual(missing, {"target_realization_signal_lcb_yes": "edge_signal_lcb_yes"})


if __name__ == "__main__":
    unittest.main()
