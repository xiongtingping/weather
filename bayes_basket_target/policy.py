"""Global-prior to station-policy resolution.

The resolver is read-only and fail-closed.  It never trains, borrows a
neighbor station, or changes the probability center/direction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .contracts import (
    ContractError,
    STAGES,
    _finite,
    _sha,
    _text,
    _timestamp,
    content_sha256,
    timestamp_in_window,
    timestamp_lte,
    timestamp_lt,
)


POLICY_FAMILIES = ("target", "reserve", "cadence")
POLICY_AUTHORITY_SCOPES = ("FORMAL_PROMOTED", "PRODUCTION_NO_LIVE_CANDIDATE")


def _mapping(name: str, value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{name}_mapping_required")
    return value


def _boolean(name: str, raw: Mapping[str, Any], *, default: bool) -> bool:
    """Accept a default only when the field is absent; reject null/string booleans."""

    if name not in raw:
        return default
    value = raw[name]
    if not isinstance(value, bool):
        raise ContractError(f"{name}_invalid")
    return value


def _generation(raw: Mapping[str, Any]) -> int:
    value = raw.get("generation", 0)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError("policy_activation_generation_invalid")
    if value < 1:
        raise ContractError("policy_activation_generation_invalid")
    return value


def _optional_id(name: str, value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ContractError(f"{name}_invalid")
    value = value.strip()
    return value or None


def _float_mapping(name: str, value: Any) -> dict[str, float]:
    raw = {} if value is None else value
    if not isinstance(raw, Mapping):
        raise ContractError(f"{name}_mapping_required")
    return {str(key): _finite(name, item) for key, item in raw.items()}


def _content_sha(name: str, supplied: Any, identity: Mapping[str, Any]) -> str:
    value = _sha(name, supplied)
    if value != content_sha256(identity):
        raise ContractError(f"{name}_mismatch")
    return value


def _coerce_prior(value: Any) -> "GlobalBasketPriorArtifact":
    if isinstance(value, GlobalBasketPriorArtifact):
        return value.validate()
    if isinstance(value, Mapping):
        return GlobalBasketPriorArtifact.from_mapping(value)
    raise ContractError("global_prior_artifact_invalid")


def _coerce_artifact(value: Any) -> "PolicyArtifact":
    if isinstance(value, PolicyArtifact):
        return value.validate()
    if isinstance(value, Mapping):
        return PolicyArtifact.from_mapping(value)
    raise ContractError("policy_artifact_invalid")


def _scope_match(
    *,
    artifact_stage: str,
    artifact_season: str,
    artifact_maturity: str,
    artifact_liquidity: str,
    artifact_strategy: str,
    stage: str,
    season_bin: str,
    maturity_bin: str,
    liquidity_bin: str,
    strategy_version: str,
) -> bool:
    return (
        artifact_stage,
        artifact_season,
        artifact_maturity,
        artifact_liquidity,
        artifact_strategy,
    ) == (
        stage,
        season_bin,
        maturity_bin,
        liquidity_bin,
        strategy_version,
    )


@dataclass(frozen=True)
class GlobalBasketPriorArtifact:
    global_prior_id: str
    strategy_version: str
    family_parameters: Mapping[str, Mapping[str, float]]
    default_policy: Mapping[str, Any]
    valid_from_utc: str
    valid_until_utc: str
    content_sha256: str
    promoted: bool = True

    def content_identity(self) -> dict[str, Any]:
        return {
            "schema": "global_basket_prior_artifact_v1",
            "global_prior_id": self.global_prior_id,
            "strategy_version": self.strategy_version,
            "family_parameters": {
                family: dict(self.family_parameters[family]) for family in POLICY_FAMILIES
            },
            "default_policy": dict(self.default_policy),
            "valid_from_utc": self.valid_from_utc,
            "valid_until_utc": self.valid_until_utc,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.content_identity(),
            "promoted": self.promoted,
            "content_sha256": self.content_sha256,
        }

    def validate(self) -> "GlobalBasketPriorArtifact":
        return type(self).from_mapping(self.to_dict())

    @classmethod
    def create(cls, **kwargs: Any) -> "GlobalBasketPriorArtifact":
        raw = dict(kwargs)
        raw.setdefault("promoted", True)
        normalized = cls._normalize(raw)
        raw["content_sha256"] = content_sha256({
            "schema": "global_basket_prior_artifact_v1",
            **{key: normalized[key] for key in normalized if key != "promoted"},
        })
        return cls.from_mapping(raw)

    @classmethod
    def _normalize(cls, raw: Mapping[str, Any]) -> dict[str, Any]:
        raw = _mapping("global_prior", raw)
        families_raw = raw.get("family_parameters")
        if not isinstance(families_raw, Mapping) or set(families_raw) != set(POLICY_FAMILIES):
            raise ContractError("global_prior_family_set_invalid")
        families = {family: _float_mapping("global_prior_parameter", families_raw[family]) for family in POLICY_FAMILIES}
        default_policy = dict(_mapping("global_prior_default_policy", raw.get("default_policy") if raw.get("default_policy") is not None else {}))
        promoted = _boolean("promoted", raw, default=True)
        normalized = {
            "global_prior_id": _text("global_prior_id", raw.get("global_prior_id")),
            "strategy_version": _text("strategy_version", raw.get("strategy_version")),
            "family_parameters": families,
            "default_policy": default_policy,
            "valid_from_utc": _timestamp("valid_from_utc", raw.get("valid_from_utc")),
            "valid_until_utc": _timestamp("valid_until_utc", raw.get("valid_until_utc")),
            "promoted": promoted,
        }
        if not timestamp_lt(normalized["valid_from_utc"], normalized["valid_until_utc"]):
            raise ContractError("global_prior_valid_window_invalid")
        return normalized

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "GlobalBasketPriorArtifact":
        normalized = cls._normalize(raw)
        supplied = _content_sha(
            "content_sha256",
            raw.get("content_sha256"),
            {"schema": "global_basket_prior_artifact_v1", **{
                key: value for key, value in normalized.items() if key != "promoted"
            }},
        )
        return cls(content_sha256=supplied, **{key: normalized[key] for key in normalized if key != "promoted"}, promoted=normalized["promoted"])


@dataclass(frozen=True)
class PolicyArtifact:
    artifact_id: str
    family: str
    station_id: str
    stage: str
    season_bin: str
    maturity_bin: str
    liquidity_bin: str
    strategy_version: str
    theta_local: Mapping[str, float]
    effective_sample_n: float
    kappa: float
    trained_through_utc: str
    valid_from_utc: str
    valid_until_utc: str
    content_sha256: str
    promoted: bool = True

    def content_identity(self) -> dict[str, Any]:
        return {
            "schema": "station_policy_artifact_v1",
            "artifact_id": self.artifact_id,
            "family": self.family,
            "station_id": self.station_id,
            "stage": self.stage,
            "season_bin": self.season_bin,
            "maturity_bin": self.maturity_bin,
            "liquidity_bin": self.liquidity_bin,
            "strategy_version": self.strategy_version,
            "theta_local": dict(self.theta_local),
            "effective_sample_n": self.effective_sample_n,
            "kappa": self.kappa,
            "trained_through_utc": self.trained_through_utc,
            "valid_from_utc": self.valid_from_utc,
            "valid_until_utc": self.valid_until_utc,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.content_identity(),
            "promoted": self.promoted,
            "content_sha256": self.content_sha256,
        }

    def validate(self) -> "PolicyArtifact":
        return type(self).from_mapping(self.to_dict())

    @classmethod
    def create(cls, **kwargs: Any) -> "PolicyArtifact":
        raw = dict(kwargs)
        raw.setdefault("promoted", True)
        normalized = cls._normalize(raw)
        raw["content_sha256"] = content_sha256({
            "schema": "station_policy_artifact_v1",
            **{key: value for key, value in normalized.items() if key != "promoted"},
        })
        return cls.from_mapping(raw)

    @classmethod
    def _normalize(cls, raw: Mapping[str, Any]) -> dict[str, Any]:
        raw = _mapping("policy_artifact", raw)
        family = _text("policy_family", raw.get("family"))
        if family not in POLICY_FAMILIES:
            raise ContractError("policy_family_invalid")
        stage = _text("policy_stage", raw.get("stage"))
        if stage not in STAGES:
            raise ContractError("policy_stage_invalid")
        normalized = {
            "artifact_id": _text("policy_artifact_id", raw.get("artifact_id")),
            "family": family,
            "station_id": _text("policy_station_id", raw.get("station_id")),
            "stage": stage,
            "season_bin": _text("policy_season_bin", raw.get("season_bin")),
            "maturity_bin": _text("policy_maturity_bin", raw.get("maturity_bin")),
            "liquidity_bin": _text("policy_liquidity_bin", raw.get("liquidity_bin")),
            "strategy_version": _text("policy_strategy_version", raw.get("strategy_version")),
            "theta_local": _float_mapping("policy_theta_local", raw.get("theta_local")),
            "effective_sample_n": _finite("policy_effective_sample_n", raw.get("effective_sample_n", 0.0)),
            "kappa": _finite("policy_kappa", raw.get("kappa", 0.0)),
            "trained_through_utc": _timestamp("policy_trained_through_utc", raw.get("trained_through_utc")),
            "valid_from_utc": _timestamp("policy_valid_from_utc", raw.get("valid_from_utc")),
            "valid_until_utc": _timestamp("policy_valid_until_utc", raw.get("valid_until_utc")),
            "promoted": _boolean("promoted", raw, default=True),
        }
        if normalized["effective_sample_n"] < 0 or normalized["kappa"] < 0:
            raise ContractError("policy_sample_or_kappa_negative")
        if not timestamp_lt(normalized["valid_from_utc"], normalized["valid_until_utc"]):
            raise ContractError("policy_artifact_valid_window_invalid")
        return normalized

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "PolicyArtifact":
        normalized = cls._normalize(raw)
        supplied = _content_sha(
            "policy_content_sha256",
            raw.get("content_sha256"),
            {"schema": "station_policy_artifact_v1", **{
                key: value for key, value in normalized.items() if key != "promoted"
            }},
        )
        return cls(content_sha256=supplied, **{key: normalized[key] for key in normalized if key != "promoted"}, promoted=normalized["promoted"])


@dataclass(frozen=True)
class StationBasketPolicyActivation:
    activation_id: str
    station_id: str
    stage: str
    season_bin: str
    maturity_bin: str
    liquidity_bin: str
    strategy_version: str
    global_prior_id: str
    target_artifact_id: str | None
    reserve_artifact_id: str | None
    cadence_artifact_id: str | None
    fallback_families: tuple[str, ...]
    generation: int
    valid_from_utc: str
    valid_until_utc: str
    activated_at_utc: str
    content_sha256: str
    promoted: bool = True

    def content_identity(self) -> dict[str, Any]:
        return {
            "schema": "station_basket_policy_activation_v1",
            "activation_id": self.activation_id,
            "station_id": self.station_id,
            "stage": self.stage,
            "season_bin": self.season_bin,
            "maturity_bin": self.maturity_bin,
            "liquidity_bin": self.liquidity_bin,
            "strategy_version": self.strategy_version,
            "global_prior_id": self.global_prior_id,
            "target_artifact_id": self.target_artifact_id,
            "reserve_artifact_id": self.reserve_artifact_id,
            "cadence_artifact_id": self.cadence_artifact_id,
            "fallback_families": list(self.fallback_families),
            "generation": self.generation,
            "valid_from_utc": self.valid_from_utc,
            "valid_until_utc": self.valid_until_utc,
            "activated_at_utc": self.activated_at_utc,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.content_identity(),
            "promoted": self.promoted,
            "content_sha256": self.content_sha256,
        }

    def validate(self) -> "StationBasketPolicyActivation":
        return type(self).from_mapping(self.to_dict())

    @classmethod
    def create(cls, **kwargs: Any) -> "StationBasketPolicyActivation":
        raw = dict(kwargs)
        raw.setdefault("promoted", True)
        normalized = cls._normalize(raw)
        raw["content_sha256"] = content_sha256({
            "schema": "station_basket_policy_activation_v1",
            **{key: value for key, value in normalized.items() if key != "promoted"},
        })
        return cls.from_mapping(raw)

    @classmethod
    def _normalize(cls, raw: Mapping[str, Any]) -> dict[str, Any]:
        raw = _mapping("policy_activation", raw)
        stage = _text("policy_activation_stage", raw.get("stage"))
        if stage not in STAGES:
            raise ContractError("policy_activation_stage_invalid")
        fallback_raw = raw["fallback_families"] if "fallback_families" in raw else []
        if not isinstance(fallback_raw, Sequence) or isinstance(fallback_raw, (str, bytes, bytearray)):
            raise ContractError("policy_fallback_family_invalid")
        fallback = tuple(_text("policy_fallback_family", item) for item in fallback_raw)
        if len(set(fallback)) != len(fallback) or any(item not in POLICY_FAMILIES for item in fallback):
            raise ContractError("policy_fallback_family_invalid")
        normalized = {
            "activation_id": _text("policy_activation_id", raw.get("activation_id")),
            "station_id": _text("policy_activation_station_id", raw.get("station_id")),
            "stage": stage,
            "season_bin": _text("policy_activation_season_bin", raw.get("season_bin")),
            "maturity_bin": _text("policy_activation_maturity_bin", raw.get("maturity_bin")),
            "liquidity_bin": _text("policy_activation_liquidity_bin", raw.get("liquidity_bin")),
            "strategy_version": _text("policy_activation_strategy_version", raw.get("strategy_version")),
            "global_prior_id": _text("policy_activation_global_prior_id", raw.get("global_prior_id")),
            "target_artifact_id": _optional_id("target_artifact_id", raw.get("target_artifact_id")),
            "reserve_artifact_id": _optional_id("reserve_artifact_id", raw.get("reserve_artifact_id")),
            "cadence_artifact_id": _optional_id("cadence_artifact_id", raw.get("cadence_artifact_id")),
            "fallback_families": fallback,
            "generation": _generation(raw),
            "valid_from_utc": _timestamp("policy_activation_valid_from_utc", raw.get("valid_from_utc")),
            "valid_until_utc": _timestamp("policy_activation_valid_until_utc", raw.get("valid_until_utc")),
            "activated_at_utc": _timestamp("policy_activation_activated_at_utc", raw.get("activated_at_utc")),
            "promoted": _boolean("promoted", raw, default=True),
        }
        if not timestamp_lt(normalized["valid_from_utc"], normalized["valid_until_utc"]):
            raise ContractError("policy_activation_valid_window_invalid")
        return normalized

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "StationBasketPolicyActivation":
        normalized = cls._normalize(raw)
        supplied = _content_sha(
            "policy_activation_content_sha256",
            raw.get("content_sha256"),
            {"schema": "station_basket_policy_activation_v1", **{
                key: value for key, value in normalized.items() if key != "promoted"
            }},
        )
        return cls(content_sha256=supplied, **{key: normalized[key] for key in normalized if key != "promoted"}, promoted=normalized["promoted"])


@dataclass(frozen=True)
class ResolvedStationBasketPolicy:
    station_id: str
    stage: str
    season_bin: str
    maturity_bin: str
    liquidity_bin: str
    strategy_version: str
    activation_id: str
    global_prior_id: str
    target_artifact_id: str
    reserve_artifact_id: str
    cadence_artifact_id: str
    fallback_reason: str
    target_theta: Mapping[str, float]
    reserve_theta: Mapping[str, float]
    cadence_theta: Mapping[str, float]
    default_policy: Mapping[str, Any]
    policy_sha256: str
    authority_scope: str = "FORMAL_PROMOTED"

    def to_adaptive_policy_overrides(self, base: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Map learned target parameters into the existing pure evaluator.

        The reserve and cadence families remain explicit in the returned
        envelope; only fields already understood by AdaptiveBasketPolicy are
        mapped into its target-allocation controls.
        """

        result = dict(self.default_policy)
        result.update(dict(base or {}))
        target_keys = {
            "total_budget_usdc", "base_coverage",
            "entropy_coverage_gain", "maximum_coverage",
            "direction_skew_deadband", "direction_entropy_max", "direction_min_side_mass",
            "center_lead_min_probability",
            "edge_max_ask",
            "edge_min_probability", "peripheral_model_reliability_min",
            "peripheral_min_coverage_value",
            "max_disagreement_expansion_layers",
            "maximum_support_buckets",
            "dispersion_c_per_expansion_layer", "clip_min_notional_usdc",
            "clip_max_notional_usdc", "price_tick_size", "maximum_limit_price",
            "minimum_expected_edge_per_share", "minimum_expected_pnl_after_usdc", "fee_rate",
            "maker_queue_cost_per_share", "taker_slippage_buffer_per_share", "protected_floor_usdc",
            "max_outside_loss_usdc",
            "basket_downside_lambda_per_usdc",
            "basket_directional_coverage_lambda_usdc",
            "basket_concentration_lambda_usdc",
            "disagreement_concentration_gain_per_layer",
            "maximum_bucket_budget_fraction",
            "basket_optimizer_max_iterations",
            "basket_optimizer_tolerance",
        }
        result.update({key: value for key, value in self.target_theta.items() if key in target_keys})
        result["policy_id"] = result.get("policy_id") or "bayes_basket_target_station_policy_v1"
        return result

def resolve_station_policy(
    *,
    activations: Sequence[StationBasketPolicyActivation | Mapping[str, Any]],
    global_priors: Mapping[str, GlobalBasketPriorArtifact] | Sequence[GlobalBasketPriorArtifact],
    artifacts: Mapping[str, PolicyArtifact] | Sequence[PolicyArtifact],
    station_id: str,
    stage: str,
    season_bin: str,
    maturity_bin: str,
    liquidity_bin: str,
    strategy_version: str,
    decision_at_utc: str,
    required_authority_scope: str = "FORMAL_PROMOTED",
) -> ResolvedStationBasketPolicy:
    """Resolve one explicit policy authority.

    Formal callers only see promoted rows.  The separate no-live candidate
    scope exists solely to collect self-owned forward evidence before
    promotion and never grants live or gateway authority.
    """

    authority_scope = str(required_authority_scope or "").strip().upper()
    if authority_scope not in POLICY_AUTHORITY_SCOPES:
        raise ContractError("station_policy_authority_scope_invalid")

    def authority_allows(promoted: bool) -> bool:
        return bool(promoted) if authority_scope == "FORMAL_PROMOTED" else True

    rows = [
        item.validate() if isinstance(item, StationBasketPolicyActivation)
        else StationBasketPolicyActivation.from_mapping(item)
        for item in activations
    ]
    eligible = [
        row for row in rows
        if authority_allows(row.promoted) and row.station_id == station_id
        and _scope_match(
            artifact_stage=row.stage,
            artifact_season=row.season_bin,
            artifact_maturity=row.maturity_bin,
            artifact_liquidity=row.liquidity_bin,
            artifact_strategy=row.strategy_version,
            stage=stage,
            season_bin=season_bin,
            maturity_bin=maturity_bin,
            liquidity_bin=liquidity_bin,
            strategy_version=strategy_version,
        )
        and timestamp_lte(row.activated_at_utc, decision_at_utc)
        and timestamp_in_window(decision_at_utc, row.valid_from_utc, row.valid_until_utc)
    ]
    if not eligible:
        raise ContractError("FAIL_CLOSED_NO_STATION_POLICY")
    generation = max(row.generation for row in eligible)
    winners = [row for row in eligible if row.generation == generation]
    if len(winners) != 1:
        raise ContractError("ambiguous_station_policy_activation")
    activation = winners[0]

    if isinstance(global_priors, Mapping):
        prior_raw = global_priors.get(activation.global_prior_id)
        prior = _coerce_prior(prior_raw) if prior_raw is not None else None
    else:
        prior_rows = [_coerce_prior(item) for item in global_priors]
        prior = next((item for item in prior_rows if item.global_prior_id == activation.global_prior_id), None)
    if prior is None or not authority_allows(prior.promoted) or not timestamp_in_window(decision_at_utc, prior.valid_from_utc, prior.valid_until_utc):
        raise ContractError("FAIL_CLOSED_NO_GLOBAL_BASKET_PRIOR")
    if prior.strategy_version != strategy_version:
        raise ContractError("global_prior_strategy_mismatch")

    if isinstance(artifacts, Mapping):
        artifact_map = {str(key): _coerce_artifact(value) for key, value in artifacts.items()}
    else:
        artifact_map = {
            item.artifact_id: item
            for item in (_coerce_artifact(value) for value in artifacts)
        }
    ids = {
        "target": activation.target_artifact_id,
        "reserve": activation.reserve_artifact_id,
        "cadence": activation.cadence_artifact_id,
    }
    missing_declared_fallbacks = {
        family for family, artifact_id in {
            "target": activation.target_artifact_id,
            "reserve": activation.reserve_artifact_id,
            "cadence": activation.cadence_artifact_id,
        }.items() if not artifact_id
    }
    if missing_declared_fallbacks != set(activation.fallback_families):
        raise ContractError("FAIL_CLOSED_NO_STATION_POLICY")

    resolved: dict[str, Mapping[str, float]] = {}
    resolved_ids: dict[str, str] = {}
    fallback_families: list[str] = []
    for family in POLICY_FAMILIES:
        artifact = artifact_map.get(ids[family] or "")
        if artifact is not None:
            exact = (
                authority_allows(artifact.promoted)
                and artifact.family == family
                and artifact.station_id == station_id
                and _scope_match(
                    artifact_stage=artifact.stage,
                    artifact_season=artifact.season_bin,
                    artifact_maturity=artifact.maturity_bin,
                    artifact_liquidity=artifact.liquidity_bin,
                    artifact_strategy=artifact.strategy_version,
                    stage=stage,
                    season_bin=season_bin,
                    maturity_bin=maturity_bin,
                    liquidity_bin=liquidity_bin,
                    strategy_version=strategy_version,
                )
                and timestamp_lte(artifact.trained_through_utc, decision_at_utc)
                and timestamp_in_window(decision_at_utc, artifact.valid_from_utc, artifact.valid_until_utc)
            )
            if not exact:
                raise ContractError("FAIL_CLOSED_NO_STATION_POLICY")
            rho = artifact.effective_sample_n / (artifact.effective_sample_n + artifact.kappa) if artifact.effective_sample_n + artifact.kappa > 0 else 0.0
            local = dict(artifact.theta_local)
            global_theta = prior.family_parameters[family]
            keys = set(local) | set(global_theta)
            resolved[family] = {key: rho * float(local.get(key, global_theta.get(key, 0.0))) + (1.0 - rho) * float(global_theta.get(key, 0.0)) for key in keys}
            resolved_ids[family] = artifact.artifact_id
            continue
        if family not in activation.fallback_families:
            raise ContractError("FAIL_CLOSED_NO_STATION_POLICY")
        resolved[family] = dict(prior.family_parameters[family])
        resolved_ids[family] = f"GLOBAL_FALLBACK:{prior.global_prior_id}:{family}"
        fallback_families.append(family)

    core = {
        "station_id": station_id,
        "stage": stage,
        "season_bin": season_bin,
        "maturity_bin": maturity_bin,
        "liquidity_bin": liquidity_bin,
        "strategy_version": strategy_version,
        "activation_id": activation.activation_id,
        "global_prior_id": prior.global_prior_id,
        "resolved_ids": resolved_ids,
        "fallback_families": fallback_families,
        "target_theta": resolved["target"],
        "reserve_theta": resolved["reserve"],
        "cadence_theta": resolved["cadence"],
        "authority_scope": authority_scope,
    }
    return ResolvedStationBasketPolicy(
        station_id=station_id,
        stage=stage,
        season_bin=season_bin,
        maturity_bin=maturity_bin,
        liquidity_bin=liquidity_bin,
        strategy_version=strategy_version,
        activation_id=activation.activation_id,
        global_prior_id=prior.global_prior_id,
        target_artifact_id=resolved_ids["target"],
        reserve_artifact_id=resolved_ids["reserve"],
        cadence_artifact_id=resolved_ids["cadence"],
        fallback_reason=";".join(fallback_families) if fallback_families else "exact_station_artifacts",
        target_theta=resolved["target"],
        reserve_theta=resolved["reserve"],
        cadence_theta=resolved["cadence"],
        default_policy=prior.default_policy,
        policy_sha256=content_sha256(core),
        authority_scope=authority_scope,
    )


__all__ = [
    "GlobalBasketPriorArtifact",
    "POLICY_AUTHORITY_SCOPES",
    "POLICY_FAMILIES",
    "PolicyArtifact",
    "ResolvedStationBasketPolicy",
    "StationBasketPolicyActivation",
    "resolve_station_policy",
]
