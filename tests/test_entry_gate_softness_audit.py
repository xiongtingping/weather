import unittest

from scripts.entry_gate_softness_audit import (
    classify_reason_code,
    classify_reason_group,
    parse_reason_codes,
    treatment_for_category,
)


class EntryGateSoftnessAuditTest(unittest.TestCase):
    def test_classifies_contract_and_market_safety_as_hard(self):
        self.assertEqual(classify_reason_code("market_closed"), "hard_safety_or_contract")
        self.assertEqual(
            classify_reason_code("weather_rules_contract_incomplete"),
            "hard_safety_or_contract",
        )

    def test_classifies_tick_and_liquidity_reasons_as_soft_penalty_candidates(self):
        self.assertEqual(
            classify_reason_code("market_tick_stale"),
            "soft_penalty_candidate",
        )
        self.assertEqual(
            classify_reason_code("depth_below_min"),
            "soft_penalty_candidate",
        )

    def test_classifies_new_soft_candidates(self):
        self.assertEqual(
            classify_reason_code("ask_above_cap"),
            "soft_penalty_candidate",
        )
        self.assertEqual(
            classify_reason_code("ask_above_cap_soft"),
            "soft_penalty_candidate",
        )
        self.assertEqual(
            classify_reason_code("spread_too_wide_soft"),
            "soft_penalty_candidate",
        )
        self.assertEqual(
            classify_reason_code("depth_soft_violation"),
            "soft_penalty_candidate",
        )
        self.assertEqual(
            classify_reason_code("market_spread_missing"),
            "soft_penalty_candidate",
        )
        self.assertEqual(
            classify_reason_code("market_one_sided_thin_book"),
            "soft_penalty_candidate",
        )
        self.assertEqual(
            classify_reason_code("market_ask_missing_no_bid"),
            "soft_penalty_candidate",
        )
        self.assertEqual(
            classify_reason_code("outcome_volume_missing"),
            "soft_penalty_candidate",
        )
        self.assertEqual(
            classify_reason_code("q_missing_soft"),
            "soft_penalty_candidate",
        )

    def test_classifies_q_missing_as_alpha_gate(self):
        self.assertEqual(
            classify_reason_code("q_missing"),
            "alpha_gate_current",
        )

    def test_alpha_lcb_reasons_remain_current_alpha_gate(self):
        self.assertEqual(classify_reason_code("edge_lcb_below_min"), "alpha_gate_current")
        self.assertEqual(
            classify_reason_code("edge_tradeability_blocked"),
            "alpha_gate_current",
        )

    def test_churn_and_position_reasons_are_separate_from_strategy(self):
        self.assertEqual(
            classify_reason_code("plain_lcb_reentry_cooldown"),
            "churn_or_position_risk",
        )
        self.assertEqual(
            classify_reason_code("other_side_active"),
            "churn_or_position_risk",
        )

    def test_group_ignores_entry_gate_wrapper_and_uses_companion_reason(self):
        self.assertEqual(
            classify_reason_group(["entry_gate_blocked", "market_tick_stale"]),
            "soft_penalty_candidate",
        )
        self.assertEqual(
            classify_reason_group(["entry_gate_blocked", "edge_lcb_below_min"]),
            "alpha_gate_current",
        )

    def test_will_order_overrides_group_category(self):
        self.assertEqual(
            classify_reason_group(["entry_gate_blocked", "edge_lcb_below_min"], will_order=True),
            "live_order_intent",
        )

    def test_treatment_names_make_softening_explicit(self):
        self.assertEqual(
            treatment_for_category("soft_penalty_candidate"),
            "convert_to_entry_usable_edge_penalty",
        )

    def test_parse_reason_codes_rejects_non_list_json(self):
        self.assertEqual(parse_reason_codes('{"code":"market_closed"}'), [])
        self.assertEqual(parse_reason_codes('["market_closed", null, ""]'), ["market_closed"])


if __name__ == "__main__":
    unittest.main()
