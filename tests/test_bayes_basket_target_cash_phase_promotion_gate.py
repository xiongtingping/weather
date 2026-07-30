from __future__ import annotations

from scripts.audit_cash_phase_promotion_gate import build_gate


def test_m8_13_separates_source_replay_from_own_forward_promotion() -> None:
    receipt = build_gate()
    assert receipt["status"] == "BLOCKED_EVIDENCE"
    assert receipt["checks"]["source_behavior_validation_pass"] is True
    assert receipt["checks"]["own_zero_order_production_no_live_mapping_pass"] is True
    assert receipt["checks"]["own_forward_price_coverage_scorecard"] is False
    assert receipt["checks"]["own_forward_pnl_attribution_scorecard"] is False
    assert receipt["promotion_permitted"] is False
    assert receipt["orders_submitted"] == 0
