from __future__ import annotations

import copy

import pytest

from bayes_basket_target import ContractError, content_sha256
from bayes_basket_target.station_policy_candidate import assert_candidate_bundle_boundary
from scripts.audit_station_policy_resolver import build_audit
from scripts.build_station_policy_candidate_bundle import build_bundle


def test_candidate_bundle_has_three_promotion_decision_layers_and_lineage() -> None:
    bundle = build_bundle()
    assert_candidate_bundle_boundary(bundle)
    assert bundle["global_prior_promotion_decision"]["artifact_kind"] == "GLOBAL_PRIOR"
    assert len(bundle["promotion_decisions"]) == bundle["artifact_count"]
    assert len(bundle["activation_promotion_decisions"]) == bundle["activation_count"]
    assert all(item["promoted"] is False for item in bundle["promotion_decisions"])
    assert all(item["promoted"] is False for item in bundle["activation_promotion_decisions"])
    assert bundle["lineage"]["source_formula_sha256"] == bundle["source_formula_sha256"]
    assert bundle["promotion_permitted"] is False


def test_promotion_decision_lineage_tamper_is_rejected() -> None:
    bundle = build_bundle()
    tampered = copy.deepcopy(bundle)
    tampered["promotion_decisions"][0]["artifact_content_sha256"] = "0" * 64
    tampered["bundle_sha256"] = content_sha256(
        {key: value for key, value in tampered.items() if key != "bundle_sha256"}
    )
    with pytest.raises(ContractError, match="lineage_mismatch"):
        assert_candidate_bundle_boundary(tampered)


def test_resolver_fault_matrix_is_all_pass_and_no_live() -> None:
    receipt = build_audit()
    assert receipt["all_cases_passed"] is True
    assert receipt["case_count"] == 9
    assert receipt["candidate_bundle_remains_unpromoted"] is True
    assert receipt["production_mutation"] is False
    assert receipt["orders_submitted"] == 0

