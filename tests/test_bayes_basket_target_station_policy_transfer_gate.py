from __future__ import annotations

from scripts.audit_station_policy_transfer_gate import build_gate


def test_m8_12_gate_separates_upstream_evidence_from_family_promotion() -> None:
    receipt = build_gate()
    assert receipt["status"] == "BLOCKED_EVIDENCE"
    assert receipt["candidate_station_count"] == 45
    assert receipt["strict_asof"]["station_days"] is None
    assert receipt["strict_asof"]["window_count"] == 0
    assert receipt["pooled_transfer"]["stations"] == 8
    assert receipt["promotion_permitted"] is False
    assert receipt["orders_submitted"] == 0
    assert receipt["checks"]["strict_asof_input_pass"] is False
    assert receipt["checks"]["family_specific_target_gate"] is False
    assert receipt["families"]["target"]["cross_season"]["status"] == "NOT_EVALUATED"
    assert "3_7_15_30_policy_family_join_missing" in receipt["blockers"]
