"""Candidate bundle boundary for the three station-policy artifact families.

The bundle is deliberately separate from the resolver's active inputs.  It
records the station target/reserve/cadence artifacts and their promotion
decision, but defaults every candidate to ``promoted=False``.  This prevents a
successful offline fit from becoming an execution authority without an
explicit, evidence-backed activation.
"""

from __future__ import annotations

from typing import Any, Mapping

from .contracts import ContractError, _finite, _text, content_sha256
from .policy import (
    POLICY_FAMILIES,
    GlobalBasketPriorArtifact,
    PolicyArtifact,
    StationBasketPolicyActivation,
)


SCHEMA = "station_policy_candidate_bundle_v1"
PROMOTION_DECISION_SCHEMA = "station_policy_promotion_decision_v2"
FORBIDDEN_TARGET_KEYS = frozenset(
    {
        "center_bucket",
        "direction",
        "direction_sign",
        "hard_lower_bound",
        "equal_share",
        "primary_secondary_ratio",
    }
)


def _assert_target_boundary(theta_local: Mapping[str, Any]) -> None:
    forbidden = sorted(set(theta_local) & FORBIDDEN_TARGET_KEYS)
    if forbidden:
        raise ContractError("station_target_probability_boundary_violation")


def _check_mapping(name: str, value: Any) -> dict[str, bool]:
    if not isinstance(value, Mapping) or not value:
        raise ContractError(f"{name}_mapping_required")
    result: dict[str, bool] = {}
    for key, item in value.items():
        if not isinstance(item, bool):
            raise ContractError(f"{name}_boolean_required")
        result[_text(f"{name}_key", key)] = item
    return result


def _lineage(name: str, value: Mapping[str, Any] | None) -> dict[str, Any]:
    if value is None or not isinstance(value, Mapping) or not value:
        raise ContractError(f"{name}_missing")
    result = {str(key): item for key, item in value.items()}
    if any(not str(key).strip() for key in result):
        raise ContractError(f"{name}_key_invalid")
    return result


def _promotion_decision_sha(identity: Mapping[str, Any]) -> str:
    return content_sha256(identity)


def build_station_policy_candidate(
    *,
    candidate_id: str,
    artifact_id: str,
    family: str,
    station_id: str,
    stage: str,
    season_bin: str,
    maturity_bin: str,
    liquidity_bin: str,
    strategy_version: str,
    theta_local: Mapping[str, Any],
    effective_sample_n: float,
    kappa: float,
    trained_through_utc: str,
    valid_from_utc: str,
    valid_until_utc: str,
    oos_checks: Mapping[str, bool],
    lineage: Mapping[str, Any] | None = None,
    gate_version: str = "station_policy_promotion_gate_v2",
    promotion_requested: bool = False,
) -> dict[str, Any]:
    """Create one content-addressed candidate and an explicit promotion record."""

    if family not in POLICY_FAMILIES:
        raise ContractError("station_policy_candidate_family_invalid")
    if family == "target":
        _assert_target_boundary(theta_local)
    checks = _check_mapping("oos_checks", oos_checks)
    evidence_lineage = _lineage(
        "candidate_lineage",
        lineage or {"lineage_status": "local_candidate_fixture"},
    )
    gate = _text("gate_version", gate_version)
    if isinstance(promotion_requested, bool) is False:
        raise ContractError("promotion_requested_invalid")
    if promotion_requested:
        raise ContractError(
            "station_policy_candidate_direct_promotion_forbidden"
        )
    sample_n = _finite("effective_sample_n", effective_sample_n)
    shrink_kappa = _finite("kappa", kappa)
    if sample_n <= 0.0 or shrink_kappa < 0.0:
        raise ContractError("station_policy_candidate_sample_invalid")

    # Candidate construction never grants authority.  Formal promotion is a
    # separate scorecard-bound transition; free-form diagnostic booleans
    # remain useful in the candidate receipt but cannot set ``promoted``.
    promoted = False
    artifact = PolicyArtifact.create(
        artifact_id=artifact_id,
        family=family,
        station_id=station_id,
        stage=stage,
        season_bin=season_bin,
        maturity_bin=maturity_bin,
        liquidity_bin=liquidity_bin,
        strategy_version=strategy_version,
        theta_local={str(key): _finite("theta_local", value) for key, value in theta_local.items()},
        effective_sample_n=sample_n,
        kappa=shrink_kappa,
        trained_through_utc=trained_through_utc,
        valid_from_utc=valid_from_utc,
        valid_until_utc=valid_until_utc,
        promoted=promoted,
    )
    decision_identity = {
        "schema": PROMOTION_DECISION_SCHEMA,
        "candidate_id": _text("candidate_id", candidate_id),
        "artifact_kind": "STATION_POLICY",
        "artifact_id": artifact.artifact_id,
        "artifact_content_sha256": artifact.content_sha256,
        "artifact_schema": "station_policy_artifact_v1",
        "family": family,
        "station_id": station_id,
        "scope": {
            "stage": stage,
            "season_bin": season_bin,
            "maturity_bin": maturity_bin,
            "liquidity_bin": liquidity_bin,
            "strategy_version": strategy_version,
        },
        "effective_sample_n": sample_n,
        "kappa": shrink_kappa,
        "rho": sample_n / (sample_n + shrink_kappa) if sample_n + shrink_kappa else 0.0,
        "trained_through_utc": artifact.trained_through_utc,
        "valid_from_utc": artifact.valid_from_utc,
        "valid_until_utc": artifact.valid_until_utc,
        "oos_checks": checks,
        "lineage": evidence_lineage,
        "gate_version": gate,
        "promotion_requested": promotion_requested,
        "promoted": promoted,
        "decision": "PROMOTED" if promoted else "REJECTED",
        "reason_codes": ["candidate_only_default"],
        "production_authority": False,
        "live_order_authorized": False,
    }
    decision = {
        **decision_identity,
        "content_sha256": content_sha256(decision_identity),
    }
    return {"artifact": artifact, "promotion_decision": decision}


def build_global_prior_promotion_decision(
    *,
    prior: Any,
    oos_checks: Mapping[str, bool],
    lineage: Mapping[str, Any],
    promotion_requested: bool = False,
    gate_version: str = "station_policy_promotion_gate_v2",
) -> dict[str, Any]:
    """Build the explicit, non-authorizing decision for a global prior."""

    if not isinstance(prior, GlobalBasketPriorArtifact):
        raise ContractError("global_prior_promotion_target_invalid")
    checks = _check_mapping("global_prior_oos_checks", oos_checks)
    evidence_lineage = _lineage("global_prior_lineage", lineage)
    gate = _text("gate_version", gate_version)
    if not isinstance(promotion_requested, bool):
        raise ContractError("promotion_requested_invalid")
    if promotion_requested:
        raise ContractError("global_prior_direct_promotion_forbidden")
    promoted = False
    identity = {
        "schema": PROMOTION_DECISION_SCHEMA,
        "decision_id": f"promotion-global-prior-{prior.global_prior_id}",
        "artifact_kind": "GLOBAL_PRIOR",
        "artifact_id": prior.global_prior_id,
        "artifact_content_sha256": prior.content_sha256,
        "artifact_schema": "global_basket_prior_artifact_v1",
        "scope": {"strategy_version": prior.strategy_version},
        "valid_from_utc": prior.valid_from_utc,
        "valid_until_utc": prior.valid_until_utc,
        "oos_checks": checks,
        "lineage": evidence_lineage,
        "gate_version": gate,
        "promotion_requested": promotion_requested,
        "promoted": promoted,
        "decision": "PROMOTED" if promoted else "REJECTED",
        "production_authority": False,
        "live_order_authorized": False,
        "reason_codes": ["candidate_only_default"],
    }
    return {**identity, "content_sha256": _promotion_decision_sha(identity)}


def build_activation_promotion_decision(
    *,
    activation: Any,
    global_prior_content_sha256: str,
    artifact_content_sha256_by_family: Mapping[str, str],
    oos_checks: Mapping[str, bool],
    lineage: Mapping[str, Any],
    promotion_requested: bool = False,
    gate_version: str = "station_policy_promotion_gate_v2",
) -> dict[str, Any]:
    """Build the explicit binding decision for one station activation."""

    if not isinstance(activation, StationBasketPolicyActivation):
        raise ContractError("activation_promotion_target_invalid")
    if set(artifact_content_sha256_by_family) != set(POLICY_FAMILIES):
        raise ContractError("activation_promotion_artifact_lineage_invalid")
    for family, value in artifact_content_sha256_by_family.items():
        if not isinstance(value, str) or len(value) != 64:
            raise ContractError(f"activation_{family}_content_sha256_invalid")
    prior_sha = _text("global_prior_content_sha256", global_prior_content_sha256)
    checks = _check_mapping("activation_oos_checks", oos_checks)
    evidence_lineage = _lineage("activation_lineage", lineage)
    gate = _text("gate_version", gate_version)
    if not isinstance(promotion_requested, bool):
        raise ContractError("promotion_requested_invalid")
    if promotion_requested:
        raise ContractError("station_activation_direct_promotion_forbidden")
    promoted = False
    identity = {
        "schema": PROMOTION_DECISION_SCHEMA,
        "decision_id": f"promotion-station-activation-{activation.activation_id}",
        "artifact_kind": "STATION_ACTIVATION",
        "artifact_id": activation.activation_id,
        "artifact_content_sha256": activation.content_sha256,
        "artifact_schema": "station_basket_policy_activation_v1",
        "scope": {
            "station_id": activation.station_id,
            "stage": activation.stage,
            "season_bin": activation.season_bin,
            "maturity_bin": activation.maturity_bin,
            "liquidity_bin": activation.liquidity_bin,
            "strategy_version": activation.strategy_version,
            "generation": activation.generation,
        },
        "global_prior_id": activation.global_prior_id,
        "global_prior_content_sha256": prior_sha,
        "artifact_content_sha256_by_family": dict(artifact_content_sha256_by_family),
        "valid_from_utc": activation.valid_from_utc,
        "valid_until_utc": activation.valid_until_utc,
        "activated_at_utc": activation.activated_at_utc,
        "oos_checks": checks,
        "lineage": evidence_lineage,
        "gate_version": gate,
        "promotion_requested": promotion_requested,
        "promoted": promoted,
        "decision": "PROMOTED" if promoted else "REJECTED",
        "production_authority": False,
        "live_order_authorized": False,
        "reason_codes": ["candidate_only_default"],
    }
    return {**identity, "content_sha256": _promotion_decision_sha(identity)}


def assert_candidate_bundle_boundary(bundle: Mapping[str, Any]) -> None:
    """Reject a bundle that can be mistaken for a live activation."""

    if not isinstance(bundle, Mapping):
        raise ContractError("station_policy_bundle_mapping_required")
    if bundle.get("schema") != SCHEMA:
        raise ContractError("station_policy_bundle_schema_invalid")
    bundle_sha = bundle.get("bundle_sha256")
    if not isinstance(bundle_sha, str) or bundle_sha != content_sha256(
        {key: value for key, value in bundle.items() if key != "bundle_sha256"}
    ):
        raise ContractError("station_policy_bundle_content_sha256_mismatch")
    if bundle.get("live_order_authorized") is not False:
        raise ContractError("station_policy_bundle_live_authority_forbidden")
    if bundle.get("production_mutation") is not False:
        raise ContractError("station_policy_bundle_production_mutation_forbidden")
    if bundle.get("promotion_permitted") is not False:
        raise ContractError("station_policy_bundle_promotion_permitted")
    if bundle.get("writes_production_db") is not False:
        raise ContractError("station_policy_bundle_production_write_forbidden")
    if int(bundle.get("orders_submitted", -1)) != 0:
        raise ContractError("station_policy_bundle_orders_forbidden")
    if not isinstance(bundle.get("artifacts"), list) or not bundle["artifacts"]:
        raise ContractError("station_policy_bundle_artifacts_missing")
    if int(bundle.get("artifact_count", -1)) != len(bundle["artifacts"]):
        raise ContractError("station_policy_bundle_artifact_count_mismatch")
    prior_raw = bundle.get("global_prior")
    if not isinstance(prior_raw, Mapping):
        raise ContractError("station_policy_bundle_global_prior_missing")
    prior = GlobalBasketPriorArtifact.from_mapping(prior_raw)
    if prior.promoted:
        raise ContractError("station_policy_bundle_promoted_global_prior_forbidden")
    prior_decision = bundle.get("global_prior_promotion_decision")
    _assert_promotion_decision(
        prior_decision,
        artifact_kind="GLOBAL_PRIOR",
        artifact_id=prior.global_prior_id,
        artifact_sha=prior.content_sha256,
        promoted=False,
    )
    for raw in bundle["artifacts"]:
        artifact = PolicyArtifact.from_mapping(raw)
        if artifact.promoted:
            raise ContractError("station_policy_bundle_promoted_artifact_forbidden")
    decisions = bundle.get("promotion_decisions")
    if not isinstance(decisions, list) or len(decisions) != len(bundle["artifacts"]):
        raise ContractError("station_policy_bundle_decisions_mismatch")
    by_id = {str(raw.get("artifact_id")): raw for raw in bundle["artifacts"]}
    for decision in decisions:
        artifact_id = str(decision.get("artifact_id") or "") if isinstance(decision, Mapping) else ""
        raw = by_id.get(artifact_id)
        if raw is None:
            raise ContractError("station_policy_bundle_decision_artifact_missing")
        artifact = PolicyArtifact.from_mapping(raw)
        _assert_promotion_decision(
            decision,
            artifact_kind="STATION_POLICY",
            artifact_id=artifact.artifact_id,
            artifact_sha=artifact.content_sha256,
            promoted=False,
        )
    activations = bundle.get("activations")
    activation_decisions = bundle.get("activation_promotion_decisions")
    if not isinstance(activations, list) or not activations:
        raise ContractError("station_policy_bundle_activations_missing")
    if int(bundle.get("activation_count", -1)) != len(activations):
        raise ContractError("station_policy_bundle_activation_count_mismatch")
    if not isinstance(activation_decisions, list) or len(activation_decisions) != len(activations):
        raise ContractError("station_policy_bundle_activation_decisions_mismatch")
    artifact_shas = {artifact.artifact_id: artifact.content_sha256 for artifact in (
        PolicyArtifact.from_mapping(raw) for raw in bundle["artifacts"]
    )}
    for raw, decision in zip(activations, activation_decisions):
        activation = StationBasketPolicyActivation.from_mapping(raw)
        if activation.promoted:
            raise ContractError("station_policy_bundle_promoted_activation_forbidden")
        expected_family_shas = {
            "target": artifact_shas.get(activation.target_artifact_id or ""),
            "reserve": artifact_shas.get(activation.reserve_artifact_id or ""),
            "cadence": artifact_shas.get(activation.cadence_artifact_id or ""),
        }
        if any(value is None for value in expected_family_shas.values()):
            raise ContractError("station_policy_bundle_activation_artifact_missing")
        _assert_promotion_decision(
            decision,
            artifact_kind="STATION_ACTIVATION",
            artifact_id=activation.activation_id,
            artifact_sha=activation.content_sha256,
            promoted=False,
            expected_fields={
                "global_prior_id": activation.global_prior_id,
                "global_prior_content_sha256": prior.content_sha256,
                "artifact_content_sha256_by_family": expected_family_shas,
            },
        )


def _assert_promotion_decision(
    value: Any,
    *,
    artifact_kind: str,
    artifact_id: str,
    artifact_sha: str,
    promoted: bool,
    expected_fields: Mapping[str, Any] | None = None,
) -> None:
    if not isinstance(value, Mapping):
        raise ContractError("station_policy_bundle_promotion_decision_missing")
    identity = {key: item for key, item in value.items() if key != "content_sha256"}
    if value.get("schema") != PROMOTION_DECISION_SCHEMA:
        raise ContractError("station_policy_bundle_promotion_decision_schema_invalid")
    if value.get("artifact_kind") != artifact_kind or value.get("artifact_id") != artifact_id:
        raise ContractError("station_policy_bundle_promotion_decision_target_mismatch")
    if value.get("artifact_content_sha256") != artifact_sha:
        raise ContractError("station_policy_bundle_promotion_decision_lineage_mismatch")
    if expected_fields:
        for key, expected in expected_fields.items():
            if value.get(key) != expected:
                raise ContractError("station_policy_bundle_promotion_decision_lineage_mismatch")
    if value.get("promoted") is not promoted or value.get("decision") != ("PROMOTED" if promoted else "REJECTED"):
        raise ContractError("station_policy_bundle_promotion_decision_state_invalid")
    if not isinstance(value.get("promotion_requested"), bool):
        raise ContractError("station_policy_bundle_promotion_requested_invalid")
    if value.get("production_authority") is not False or value.get("live_order_authorized") is not False:
        raise ContractError("station_policy_bundle_promotion_decision_authority_invalid")
    supplied = value.get("content_sha256")
    if not isinstance(supplied, str) or supplied != _promotion_decision_sha(identity):
        raise ContractError("station_policy_bundle_promotion_decision_content_sha256_mismatch")


__all__ = [
    "FORBIDDEN_TARGET_KEYS",
    "PROMOTION_DECISION_SCHEMA",
    "SCHEMA",
    "assert_candidate_bundle_boundary",
    "build_activation_promotion_decision",
    "build_global_prior_promotion_decision",
    "build_station_policy_candidate",
]
