from resolution_bridge_artifact_runtime import validate_resolution_bridge_artifact
from resolution_bridge_operational_baseline_runtime import (
    build_resolution_bridge_operational_baseline_candidate,
)
from resolution_bridge_promotion_runtime import (
    approval_receipt_sha256,
    compile_resolution_bridge_promotion,
)


def _candidate(pairs=()):
    return build_resolution_bridge_operational_baseline_candidate(
        authority_contract_sha256="a" * 64,
        operational_projection_authority_contract_sha256="c" * 64,
        observed_pairs=pairs,
    )


def _receipt(candidate):
    core = {
        "schema": "resolution_bridge_promotion_approval_v1",
        "state": "accepted",
        "candidate_artifact_sha256": candidate["artifact_sha256"],
        "operational_baseline_review_accepted": True,
        "probabilistic_residual_support_review_accepted": True,
        "hard_exclusion_disabled_review_accepted": True,
        "sample_or_oos_live_gate": False,
        "approval_id": "ADR-XSB-OPERATIONAL-BASELINE-0001",
        "reviewer": "sol-adversarial-review",
        "approved_at": "2026-07-16T09:45:00Z",
    }
    return {**core, "approval_receipt_sha256": approval_receipt_sha256(core)}


def test_zero_sample_operational_baseline_is_valid_and_retains_probabilistic_uncertainty() -> None:
    candidate = _candidate()
    assert candidate["kernel_kind"] == "same_station_sample_free_operational_residual_prior_v1"
    assert candidate["coverage"]["accepted_pair_count"] == 0
    assert candidate["coverage"]["required_for_runtime"] is False
    assert set(candidate["global_residual_pmf"]) == {str(value) for value in range(-20, 21)}
    assert candidate["global_residual_pmf"]["0"] > candidate["global_residual_pmf"]["1"]
    assert candidate["global_residual_pmf"]["-20"] > 0.0
    assert candidate["global_residual_pmf"]["20"] > 0.0
    assert candidate["operational_baseline"]["soft_tail_mass_present"] is True
    assert candidate["operational_baseline"]["soft_projection_exact_zero_forbidden_without_hard_certificate"] is True
    assert abs(sum(candidate["global_residual_pmf"].values()) - 1.0) < 1e-12


def test_observed_pairs_cannot_refine_operational_baseline_outside_calibration_pipeline() -> None:
    import pytest

    with pytest.raises(ValueError, match="must_not_consume_observed_pairs"):
        _candidate(
            [{
                "event_date": "2026-07-14",
                "final_physical_high_c": 38.0,
                "settlement_scalar_c": 38.0,
                "observed_x_s_pair_sha256": "b" * 64,
            }]
        )


def test_reviewed_operational_baseline_compiles_production_bridge_without_sample_gate() -> None:
    candidate = _candidate()
    result = compile_resolution_bridge_promotion(
        candidate_artifact=candidate,
        approval_receipt=_receipt(candidate),
        artifact_path="config/artifacts/resolution_bridge/operational_baseline.json",
    )
    assert result["ok"] is True
    artifact = validate_resolution_bridge_artifact(
        result["production_artifact"], require_production=True
    )
    assert artifact["promotion_basis"] == "approved_sample_nonblocking_operational_baseline"
    assert result["production_activation"]["authorizes_clob"] is False
    assert result["production_activation"]["places_orders"] is False


def test_operational_baseline_cannot_promote_without_probabilistic_support_and_no_disable_review() -> None:
    candidate = _candidate()
    receipt = _receipt(candidate)
    receipt["hard_exclusion_disabled_review_accepted"] = False
    receipt["approval_receipt_sha256"] = approval_receipt_sha256(
        {key: value for key, value in receipt.items() if key != "approval_receipt_sha256"}
    )
    result = compile_resolution_bridge_promotion(
        candidate_artifact=candidate,
        approval_receipt=receipt,
        artifact_path="config/artifacts/resolution_bridge/operational_baseline.json",
    )
    assert result["ok"] is False
    assert "bridge_operational_baseline_review_not_accepted" in result["block_reasons"]
