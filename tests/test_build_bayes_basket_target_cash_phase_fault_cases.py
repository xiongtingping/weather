from __future__ import annotations

from bayes_basket_target.cash_phase_oos import REQUIRED_FAULT_CASES
from bayes_basket_target.contracts import content_sha256
from scripts.build_bayes_basket_target_cash_phase_fault_cases import (
    EVIDENCE_CLASS,
    SCHEMA,
    build_fault_cases,
)


def test_cash_phase_fault_cases_cover_exact_required_set() -> None:
    artifact = build_fault_cases()
    cases = artifact["fault_cases"]

    assert artifact["schema"] == SCHEMA
    assert artifact["status"] == "PASS"
    assert artifact["fault_case_count"] == 6
    assert {row["name"] for row in cases} == REQUIRED_FAULT_CASES
    assert all(row["passed"] is True for row in cases)
    assert all(row["evidence_owner"] == "bayes_basket_target" for row in cases)
    assert all(row["evidence_class"] == EVIDENCE_CLASS for row in cases)
    assert all(row["market_oos_evidence"] is False for row in cases)
    assert all(row["heavy_phase_observation"] is False for row in cases)
    assert all(len(row["evidence_sha256"]) == 64 for row in cases)


def test_cash_phase_fault_cases_are_deterministic_and_non_promoting() -> None:
    first = build_fault_cases()
    second = build_fault_cases()

    assert first == second
    assert first["formal_promotion_allowed"] is False
    assert first["live_activation_allowed"] is False
    assert first["writes_production_db"] is False
    assert first["calls_order_gateway"] is False
    assert first["orders_submitted"] == 0
    assert first["content_sha256"] == content_sha256({
        key: value for key, value in first.items() if key != "content_sha256"
    })
