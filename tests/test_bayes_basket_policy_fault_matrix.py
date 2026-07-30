from __future__ import annotations

from dataclasses import replace

import pytest

from bayes_basket_target import (
    ContractError,
    GlobalBasketPriorArtifact,
    PolicyArtifact,
    StationBasketPolicyActivation,
    resolve_station_policy,
)


DECISION = "2026-07-23T00:00:00Z"
VALID_FROM = "2026-07-01T00:00:00Z"
VALID_UNTIL = "2026-08-01T00:00:00Z"


def _prior(**overrides: object) -> GlobalBasketPriorArtifact:
    value = {
        "global_prior_id": "prior-fault-v1",
        "strategy_version": "target-v1",
        "family_parameters": {"target": {}, "reserve": {"price_margin": 0.0}, "cadence": {}},
        "default_policy": {"total_budget_usdc": 12.0},
        "valid_from_utc": VALID_FROM,
        "valid_until_utc": VALID_UNTIL,
        "promoted": True,
    }
    value.update(overrides)
    return GlobalBasketPriorArtifact.create(**value)


def _artifacts(**overrides: object) -> list[PolicyArtifact]:
    rows: list[PolicyArtifact] = []
    for family in ("target", "reserve", "cadence"):
        value = {
            "artifact_id": f"fault-policy-{family}",
            "family": family,
            "station_id": "ZUUU",
            "stage": "P0-M",
            "season_bin": "summer",
            "maturity_bin": "d1",
            "liquidity_bin": "liquid",
            "strategy_version": "target-v1",
            "theta_local": {"price_margin": 0.0} if family == "reserve" else {},
            "effective_sample_n": 10.0,
            "kappa": 10.0,
            "trained_through_utc": "2026-07-22T00:00:00Z",
            "valid_from_utc": VALID_FROM,
            "valid_until_utc": VALID_UNTIL,
            "promoted": True,
        }
        value.update(overrides)
        rows.append(PolicyArtifact.create(**value))
    return rows


def _activation(**overrides: object) -> StationBasketPolicyActivation:
    value = {
        "activation_id": "fault-activation-v1",
        "station_id": "ZUUU",
        "stage": "P0-M",
        "season_bin": "summer",
        "maturity_bin": "d1",
        "liquidity_bin": "liquid",
        "strategy_version": "target-v1",
        "global_prior_id": "prior-fault-v1",
        "target_artifact_id": "fault-policy-target",
        "reserve_artifact_id": "fault-policy-reserve",
        "cadence_artifact_id": "fault-policy-cadence",
        "fallback_families": [],
        "generation": 1,
        "valid_from_utc": VALID_FROM,
        "valid_until_utc": VALID_UNTIL,
        "activated_at_utc": "2026-06-30T00:00:00Z",
        "promoted": True,
    }
    value.update(overrides)
    return StationBasketPolicyActivation.create(**value)


def _resolve(
    *,
    activation: StationBasketPolicyActivation | None = None,
    activations: list[StationBasketPolicyActivation] | None = None,
    prior: GlobalBasketPriorArtifact | None = None,
    artifacts: list[PolicyArtifact] | None = None,
    station_id: str = "ZUUU",
    required_authority_scope: str = "FORMAL_PROMOTED",
) -> object:
    return resolve_station_policy(
        activations=activations or [activation or _activation()],
        global_priors={"prior-fault-v1": prior or _prior()},
        artifacts=artifacts or _artifacts(),
        station_id=station_id,
        stage="P0-M",
        season_bin="summer",
        maturity_bin="d1",
        liquidity_bin="liquid",
        strategy_version="target-v1",
        decision_at_utc=DECISION,
        required_authority_scope=required_authority_scope,
    )


@pytest.mark.parametrize(
    ("factory", "field", "error"),
    [
        (_prior, "content_sha256", "content_sha256_mismatch"),
        (lambda **kw: _artifacts(**kw)[0], "content_sha256", "policy_content_sha256_mismatch"),
        (_activation, "content_sha256", "policy_activation_content_sha256_mismatch"),
    ],
)
def test_content_sha_is_recomputed_not_shape_only(factory, field, error) -> None:
    value = factory()
    raw = value.to_dict()
    raw[field] = "a" * 64
    with pytest.raises(ContractError, match=error):
        type(value).from_mapping(raw)


@pytest.mark.parametrize(
    ("factory", "field"),
    [(_prior, "promoted"), (lambda **kw: _artifacts(**kw)[0], "promoted"), (_activation, "promoted")],
)
def test_promoted_requires_a_real_boolean(factory, field) -> None:
    value = factory()
    raw = value.to_dict()
    raw[field] = "false"
    with pytest.raises(ContractError, match="promoted_invalid"):
        type(value).from_mapping(raw)


def test_expired_artifact_and_scope_mismatch_fail_closed() -> None:
    expired = _artifacts(valid_until_utc="2026-07-22T00:00:00Z")
    with pytest.raises(ContractError, match="FAIL_CLOSED_NO_STATION_POLICY"):
        _resolve(artifacts=expired)

    wrong_scope = _artifacts(station_id="ZBAA")
    with pytest.raises(ContractError, match="FAIL_CLOSED_NO_STATION_POLICY"):
        _resolve(artifacts=wrong_scope)


def test_same_generation_activation_conflict_is_not_latest_wins() -> None:
    first = _activation()
    second = StationBasketPolicyActivation.create(
        **{**first.to_dict(), "activation_id": "fault-activation-conflict"}
    )
    with pytest.raises(ContractError, match="ambiguous_station_policy_activation"):
        _resolve(activations=[first, second])


def test_missing_fallback_family_and_unactivated_fallback_fail_closed() -> None:
    missing_declaration = _activation(target_artifact_id=None, fallback_families=[])
    with pytest.raises(ContractError, match="FAIL_CLOSED_NO_STATION_POLICY"):
        _resolve(activation=missing_declaration)

    extra_declaration = _activation(fallback_families=["target"])
    with pytest.raises(ContractError, match="FAIL_CLOSED_NO_STATION_POLICY"):
        _resolve(activation=extra_declaration)


def test_no_neighbor_station_or_fuzzy_latest_borrow_is_allowed() -> None:
    with pytest.raises(ContractError, match="FAIL_CLOSED_NO_STATION_POLICY"):
        _resolve(station_id="ZBAA")

    newer_wrong_scope = _activation(
        activation_id="fault-activation-wrong-scope",
        generation=99,
        season_bin="winter",
    )
    with pytest.raises(ContractError, match="FAIL_CLOSED_NO_STATION_POLICY"):
        _resolve(activations=[newer_wrong_scope])


def test_directly_constructed_tampered_artifacts_are_revalidated() -> None:
    prior = _prior()
    tampered = replace(prior, default_policy={"total_budget_usdc": 999.0})
    with pytest.raises(ContractError, match="content_sha256_mismatch"):
        _resolve(prior=tampered)


def test_unpromoted_candidate_fails_closed_in_formal_scope() -> None:
    with pytest.raises(ContractError, match="FAIL_CLOSED_NO_STATION_POLICY"):
        _resolve(
            activation=_activation(promoted=False),
            prior=_prior(promoted=False),
            artifacts=_artifacts(promoted=False),
        )


def test_unpromoted_candidate_resolves_only_in_explicit_no_live_scope() -> None:
    resolved = _resolve(
        activation=_activation(promoted=False),
        prior=_prior(promoted=False),
        artifacts=_artifacts(promoted=False),
        required_authority_scope="PRODUCTION_NO_LIVE_CANDIDATE",
    )
    assert resolved.authority_scope == "PRODUCTION_NO_LIVE_CANDIDATE"
    assert resolved.activation_id == "fault-activation-v1"


@pytest.mark.parametrize(
    "authority_scope",
    ["", "PRODUCTION_NO_LIVE_ONLY", "LIVE", "formal_promoted_typo"],
)
def test_unknown_policy_authority_scope_is_rejected(authority_scope: str) -> None:
    with pytest.raises(ContractError, match="station_policy_authority_scope_invalid"):
        _resolve(required_authority_scope=authority_scope)
