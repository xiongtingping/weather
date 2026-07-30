from __future__ import annotations

import copy

import pytest

from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.policy import (
    GlobalBasketPriorArtifact,
    PolicyArtifact,
    StationBasketPolicyActivation,
)
from bayes_basket_target.station_policy_candidate import (
    assert_candidate_bundle_boundary,
    build_activation_promotion_decision,
    build_global_prior_promotion_decision,
    build_station_policy_candidate,
)


def _candidate(**overrides):
    values = {
        "candidate_id": "candidate-target-1",
        "artifact_id": "artifact-target-1",
        "family": "target",
        "station_id": "ZUUU",
        "stage": "P0",
        "season_bin": "summer",
        "maturity_bin": "d1",
        "liquidity_bin": "liquid",
        "strategy_version": "target-v1",
        "theta_local": {"edge_weight": 0.2},
        "effective_sample_n": 12.0,
        "kappa": 8.0,
        "trained_through_utc": "2026-07-17T00:00:00Z",
        "valid_from_utc": "2026-07-23T00:00:00Z",
        "valid_until_utc": "2026-08-23T00:00:00Z",
        "oos_checks": {"direction_preserved": True, "support_closed": True},
    }
    values.update(overrides)
    return build_station_policy_candidate(**values)


def test_candidate_defaults_to_unpromoted_and_records_rho_and_lineage() -> None:
    candidate = _candidate()
    artifact = candidate["artifact"]
    decision = candidate["promotion_decision"]
    assert artifact.promoted is False
    assert decision["promoted"] is False
    assert decision["rho"] == pytest.approx(0.6)
    assert decision["content_sha256"]
    assert "candidate_only_default" in decision["reason_codes"]
    assert artifact.to_dict()["content_sha256"]


def test_candidate_builder_cannot_promote_from_free_form_oos_booleans() -> None:
    with pytest.raises(
        ContractError,
        match="station_policy_candidate_direct_promotion_forbidden",
    ):
        _candidate(promotion_requested=True)


def test_prior_and_activation_builders_cannot_directly_promote() -> None:
    from scripts.build_station_policy_candidate_bundle import build_bundle

    bundle = build_bundle()
    prior = GlobalBasketPriorArtifact.from_mapping(bundle["global_prior"])
    with pytest.raises(
        ContractError,
        match="global_prior_direct_promotion_forbidden",
    ):
        build_global_prior_promotion_decision(
            prior=prior,
            oos_checks={"anything": True},
            lineage={"source": "test"},
            promotion_requested=True,
        )

    activation = StationBasketPolicyActivation.from_mapping(
        bundle["activations"][0]
    )
    artifact_shas = {
        str(row["artifact_id"]): str(row["content_sha256"])
        for row in bundle["artifacts"]
    }
    with pytest.raises(
        ContractError,
        match="station_activation_direct_promotion_forbidden",
    ):
        build_activation_promotion_decision(
            activation=activation,
            global_prior_content_sha256=prior.content_sha256,
            artifact_content_sha256_by_family={
                "target": artifact_shas[activation.target_artifact_id],
                "reserve": artifact_shas[activation.reserve_artifact_id],
                "cadence": artifact_shas[activation.cadence_artifact_id],
            },
            oos_checks={"anything": True},
            lineage={"source": "test"},
            promotion_requested=True,
        )


def test_target_cannot_learn_probability_center_or_direction() -> None:
    with pytest.raises(ContractError, match="probability_boundary_violation"):
        _candidate(theta_local={"center_bucket": 38.0})
    with pytest.raises(ContractError, match="probability_boundary_violation"):
        _candidate(theta_local={"direction_sign": -1.0})


def test_reserve_and_cadence_have_explicit_non_authority_boundaries() -> None:
    reserve = _candidate(
        artifact_id="artifact-reserve-1",
        candidate_id="candidate-reserve-1",
        family="reserve",
        theta_local={"price_margin": 0.02},
        oos_checks={"math_cap_min_preserved": True, "b14_breakthroughs_zero": True},
    )
    cadence = _candidate(
        artifact_id="artifact-cadence-1",
        candidate_id="candidate-cadence-1",
        family="cadence",
        theta_local={"gate_seconds_intercept": 600.0},
        oos_checks={"opens_time_gate_only": True, "b13_b14_bypass": False},
    )
    assert reserve["artifact"].family == "reserve"
    assert cadence["artifact"].family == "cadence"
    assert reserve["promotion_decision"]["live_order_authorized"] is False
    assert cadence["promotion_decision"]["production_authority"] is False


def test_bundle_boundary_rejects_promoted_artifacts_and_nonzero_orders() -> None:
    from scripts.build_station_policy_candidate_bundle import build_bundle

    bundle = build_bundle()
    assert_candidate_bundle_boundary(bundle)

    promoted_bundle = copy.deepcopy(bundle)
    promoted = copy.deepcopy(promoted_bundle["artifacts"][0])
    promoted["promoted"] = True
    promoted["content_sha256"] = PolicyArtifact.create(**{
        **{key: value for key, value in promoted.items() if key != "content_sha256"},
    }).content_sha256
    promoted_bundle["artifacts"][0] = promoted
    promoted_bundle["bundle_sha256"] = content_sha256({
        key: value for key, value in promoted_bundle.items() if key != "bundle_sha256"
    })
    with pytest.raises(ContractError, match="promoted_artifact_forbidden"):
        assert_candidate_bundle_boundary(promoted_bundle)

    nonzero = copy.deepcopy(bundle)
    nonzero["orders_submitted"] = 1
    nonzero["bundle_sha256"] = content_sha256({
        key: value for key, value in nonzero.items() if key != "bundle_sha256"
    })
    with pytest.raises(ContractError, match="orders_forbidden"):
        assert_candidate_bundle_boundary(nonzero)
