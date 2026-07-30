from __future__ import annotations

import pytest

from bayes_basket_target.contracts import ContractError
from bayes_basket_target.promotion_boundary import build_promotion_boundary_decision
from scripts.bayes_basket_target_promotion_boundary_audit import build_receipt


def test_failed_research_gate_only_blocks_candidate() -> None:
    result = build_promotion_boundary_decision(
        active_artifact={
            "artifact_id": "active",
            "content_sha256": "a" * 64,
            "state": "ACTIVE",
            "research_gate_dependency": False,
        },
        research_gate={"checks": {"oos": False}, "promotion_requested": True},
        candidate_artifact={"artifact_id": "candidate"},
    )
    assert result["active_runtime_allowed"] is True
    assert result["candidate_promotion_allowed"] is False
    assert result["active_runtime_reads_research_gate"] is False


def test_active_artifact_cannot_declare_research_gate_dependency() -> None:
    with pytest.raises(ContractError, match="reads_research_gate"):
        build_promotion_boundary_decision(
            active_artifact={
                "artifact_id": "active",
                "content_sha256": "a" * 64,
                "state": "ACTIVE",
                "research_gate_dependency": True,
            },
            research_gate={"checks": {"oos": True}, "promotion_requested": True},
            candidate_artifact={"artifact_id": "candidate"},
        )


def test_runtime_boundary_audit_passes() -> None:
    receipt = build_receipt()
    assert receipt["promotion_boundary_verified"] is True
