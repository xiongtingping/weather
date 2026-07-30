from __future__ import annotations

import pytest

from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.settlement_evidence_packet import (
    NA_ACTUAL_FILL_EVIDENCE,
    SCHEMA,
    build_attribution_terms,
    build_settlement_version_evidence_packet,
    validate_attribution_terms,
    validate_settlement_version_evidence_packet,
)


SHA = "a" * 64


def _binding() -> dict[str, object]:
    core = {
        "schema": "bayes_basket_target_settlement_version_label_binding_v1",
        "fact_id": "d2fact_1",
        "fact_content_sha256": SHA,
        "station_id": "RKPK",
        "event_date": "2026-07-29",
        "settlement_timezone": "Asia/Karachi",
        "settlement_closed_at_utc": "2026-07-30T00:00:00Z",
        "truth_id": "truth-1",
        "truth_revision_no": 1,
        "truth_sha256": "b" * 64,
        "probability_envelope_id": "p1-1",
        "posterior_sha256": "c" * 64,
        "ws_snapshot_id": "ws-1",
        "quote_sha256": "d" * 64,
        "inventory_snapshot_id": "inventory-1",
        "inventory_sha256": "e" * 64,
        "order_state_snapshot_id": "order-1",
        "order_state_sha256": "f" * 64,
        "cycle_id": "cycle-1",
        "decision_id": "decision-1",
        "decision_sha256": "0" * 64,
        "plan_sha256": "1" * 64,
        "policy_version_vector_sha256": "2" * 64,
    }
    return {**core, "content_sha256": content_sha256(core)}


def _terms(binding: dict[str, object]) -> dict[str, object]:
    return build_attribution_terms(
        fact_id=str(binding["fact_id"]),
        fact_content_sha256=str(binding["fact_content_sha256"]),
        truth_sha256=str(binding["truth_sha256"]),
        winning_bucket="38°C",
        policy_version_vector_sha256=str(binding["policy_version_vector_sha256"]),
        coverage_usdc=7.0,
        overlap_usdc=0.5,
        edge_usdc=0.25,
        execution_cost_usdc=3.0,
        failure_cost_usdc=0.25,
    )


def test_packet_is_content_addressed_and_marks_actual_fill_na() -> None:
    binding = _binding()
    terms = _terms(binding)
    packet = build_settlement_version_evidence_packet(
        settlement_version_binding=binding,
        attribution_terms=terms,
    )

    assert packet["schema"] == SCHEMA
    assert packet["actual_fill_evidence"]["status"] == NA_ACTUAL_FILL_EVIDENCE
    assert packet["production_boundary"][
        "production_no_live_blocked_by_forward_oos"
    ] is False
    packet_sha = packet["content_sha256"]
    assert content_sha256({k: v for k, v in packet.items() if k != "content_sha256"}) == packet_sha
    validated, error = validate_settlement_version_evidence_packet(packet)
    assert error is None
    assert validated is not None


def test_terms_require_hash_and_close_exactly() -> None:
    binding = _binding()
    terms = _terms(binding)
    validated, error = validate_attribution_terms(
        terms,
        expected_fact_id="d2fact_1",
        expected_fact_content_sha256=SHA,
        expected_truth_sha256="b" * 64,
        expected_policy_version_vector_sha256="2" * 64,
    )
    assert error is None
    assert validated is not None
    drifted = dict(terms)
    drifted["edge_usdc"] = 99.0
    _, error = validate_attribution_terms(drifted)
    assert error == "settled_attribution_content_drift"


def test_forward_oos_cannot_block_no_live_packet() -> None:
    binding = _binding()
    with pytest.raises(ContractError, match="no_live_boundary"):
        build_settlement_version_evidence_packet(
            settlement_version_binding=binding,
            attribution_terms=_terms(binding),
            production_no_live_blocked_by_forward_oos=True,
        )


def test_no_live_packet_cannot_smuggle_zero_fill_evidence() -> None:
    binding = _binding()
    packet = build_settlement_version_evidence_packet(
        settlement_version_binding=binding,
        attribution_terms=_terms(binding),
    )
    mutated = dict(packet)
    mutated["actual_fill_evidence"] = {
        "status": NA_ACTUAL_FILL_EVIDENCE,
        "fill_count": 0,
    }
    mutated["content_sha256"] = content_sha256(
        {key: value for key, value in mutated.items() if key != "content_sha256"}
    )
    _, error = validate_settlement_version_evidence_packet(mutated)
    assert error == "settlement_evidence_actual_fill_fabricated"
