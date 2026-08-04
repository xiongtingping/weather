from __future__ import annotations

from copy import deepcopy

import pytest

from bayes_basket_target.canonical_activation_current import (
    ACTIVATION_CURRENT_AMBIGUOUS,
    BLOCKED,
    CURRENT,
    NO_LIVE_AUTHORITY_SCOPE,
    SCHEMA,
    build_canonical_activation_current_index,
    verify_canonical_activation_current_index,
)
from bayes_basket_target.contracts import ContractError, content_sha256


def _activation(
    *,
    activation_id: str = "activation-katl-d1-g1",
    station_id: str = "KATL",
    model_family: str = "global_deterministic",
    lead_bucket: str = "d1",
    run_cycle: str = "PREVIOUS_DAY1",
    generation: int = 1,
    authority_scope: str = NO_LIVE_AUTHORITY_SCOPE,
    oos_role: str = "historical_strict_oos",
) -> dict:
    identity = {
        "activation_id": activation_id,
        "station_id": station_id,
        "model_family": model_family,
        "lead_bucket": lead_bucket,
        "run_cycle": run_cycle,
        "candidate_models": ["ECMWF", "GFS"],
        "selected_models": ["ECMWF"],
        "selected_weights": {"ECMWF": 1.0},
        "available_model_set_hash": "a" * 64,
        "selection_evidence_sha256": "b" * 64,
        "artifact_content_sha256": "c" * 64,
        "activation_generation": generation,
        "authority_scope": authority_scope,
        "oos_role": oos_role,
    }
    return {**identity, "activation_content_sha256": content_sha256(identity)}


def test_unique_scopes_build_current_content_addressed_no_live_index() -> None:
    katl = _activation()
    zuuu = _activation(
        activation_id="activation-zuuu-d0-g1",
        station_id="ZUUU",
        lead_bucket="d0",
        run_cycle="SAME_DAY",
    )

    result = build_canonical_activation_current_index([zuuu, katl])

    assert result["schema"] == SCHEMA
    assert result["authority_scope"] == NO_LIVE_AUTHORITY_SCOPE
    assert result["state"] == CURRENT
    assert result["entry_count"] == 2
    assert [entry["scope"]["station_id"] for entry in result["entries"]] == [
        "KATL",
        "ZUUU",
    ]
    assert result["entries"][0]["current_activation"] == {
        "activation_id": katl["activation_id"],
        "activation_generation": 1,
        "activation_content_sha256": katl["activation_content_sha256"],
    }
    assert result["content_sha256"] == content_sha256(
        {key: value for key, value in result.items() if key != "content_sha256"}
    )
    assert build_canonical_activation_current_index([katl, zuuu]) == result


def test_multiple_generations_are_blocked_without_selecting_a_maximum() -> None:
    g1 = _activation(generation=1)
    g2 = _activation(activation_id="activation-katl-d1-g2", generation=2)

    result = build_canonical_activation_current_index([g1, g2])

    assert result["state"] == BLOCKED
    entry = result["entries"][0]
    assert entry["state"] == BLOCKED
    assert entry["reason"] == ACTIVATION_CURRENT_AMBIGUOUS
    assert "current_activation" not in entry
    assert entry["activation_candidates"] == [
        {
            "activation_id": g1["activation_id"],
            "activation_generation": 1,
            "activation_content_sha256": g1["activation_content_sha256"],
        },
        {
            "activation_id": g2["activation_id"],
            "activation_generation": 2,
            "activation_content_sha256": g2["activation_content_sha256"],
        },
    ]


def test_duplicate_scope_with_the_same_generation_is_also_blocked() -> None:
    first = _activation(activation_id="activation-katl-d1-a", generation=3)
    second = _activation(activation_id="activation-katl-d1-b", generation=3)

    result = build_canonical_activation_current_index([first, second])

    assert result["state"] == BLOCKED
    entry = result["entries"][0]
    assert entry["reason"] == ACTIVATION_CURRENT_AMBIGUOUS
    assert [item["activation_id"] for item in entry["activation_candidates"]] == [
        "activation-katl-d1-a",
        "activation-katl-d1-b",
    ]
    assert "current_activation" not in entry


def test_generation_regression_is_rejected_in_input_order() -> None:
    g2 = _activation(activation_id="activation-katl-d1-g2", generation=2)
    g1 = _activation(generation=1)

    with pytest.raises(
        ContractError, match="canonical_activation_current_generation_regression"
    ):
        build_canonical_activation_current_index([g2, g1])


def test_invalid_activation_hash_and_unknown_field_fail_closed() -> None:
    hash_tampered = _activation()
    hash_tampered["selection_evidence_sha256"] = "d" * 64
    with pytest.raises(
        ContractError, match="canonical_activation_content_sha256_mismatch"
    ):
        build_canonical_activation_current_index([hash_tampered])

    unknown_field = _activation()
    unknown_field["target_local_date"] = "2026-08-04"
    with pytest.raises(ContractError, match="canonical_activation_fact_unknown_field"):
        build_canonical_activation_current_index([unknown_field])


def test_formal_activation_cannot_enter_no_live_current_index() -> None:
    formal = _activation(authority_scope="FORMAL_NATIVE_ELIGIBLE")

    with pytest.raises(
        ContractError, match="canonical_activation_current_authority_scope_invalid"
    ):
        build_canonical_activation_current_index([formal])


def test_index_hash_tampering_is_rejected_and_rebuild_is_deterministic() -> None:
    activation = _activation()
    result = build_canonical_activation_current_index([activation])
    assert verify_canonical_activation_current_index(result) == result

    tampered = deepcopy(result)
    tampered["entries"][0]["current_activation"]["activation_id"] = "forged"
    with pytest.raises(
        ContractError, match="canonical_activation_current_content_sha256_mismatch"
    ):
        verify_canonical_activation_current_index(tampered)

    assert build_canonical_activation_current_index([activation]) == result
