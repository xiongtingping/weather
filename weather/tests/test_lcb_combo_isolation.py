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


if __name__ == "__main__":
    unittest.main()
