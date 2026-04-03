import unittest

import scan_weather_market as swm


class TestLcbComboIsolation(unittest.TestCase):
    def test_selected_lcb_q_combo_role_ignores_unselected_role(self) -> None:
        candidate = {
            "lcb_q_combo_role": "yes_top",
            "lcb_q_combo_selected": False,
            "lcb_q_combo_selected_theoretical": True,
        }
        self.assertEqual(swm._selected_lcb_q_combo_role(candidate), "")

    def test_selected_lcb_q_combo_role_normalizes_selected_aliases(self) -> None:
        candidate_yes = {
            "lcb_q_combo_role": "yes_top",
            "lcb_q_combo_selected": True,
        }
        candidate_no = {
            "lcb_q_combo_role": "no_far",
            "lcb_q_combo_selected": True,
        }

        self.assertEqual(
            swm._selected_lcb_q_combo_role(candidate_yes),
            swm.LCB_Q_COMBO_ROLE_YES_PEAK,
        )
        self.assertEqual(
            swm._selected_lcb_q_combo_role(candidate_no),
            swm.LCB_Q_COMBO_ROLE_NO_REMOTE,
        )

    def test_candidate_lcb_combo_role_hint_uses_hint_when_combo_enabled(self) -> None:
        candidate = {
            "strategy": "lcb_global",
            "lcb_q_combo_role_hint": "no_far",
        }
        self.assertEqual(
            swm._candidate_lcb_combo_role_hint(candidate, combo_enabled=True),
            swm.LCB_Q_COMBO_ROLE_NO_REMOTE,
        )
        self.assertFalse(
            swm._candidate_requires_plain_lcb_path_gate(candidate, combo_enabled=True)
        )

    def test_candidate_lcb_combo_role_hint_ignores_hint_when_combo_disabled(self) -> None:
        candidate = {
            "strategy": "lcb_global",
            "lcb_q_combo_role_hint": "no_far",
        }
        self.assertEqual(
            swm._candidate_lcb_combo_role_hint(candidate, combo_enabled=False),
            "",
        )
        self.assertTrue(
            swm._candidate_requires_plain_lcb_path_gate(candidate, combo_enabled=False)
        )

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
