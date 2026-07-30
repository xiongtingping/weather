"""Immutable, family-specific station-policy OOS artifacts.

This module is deliberately offline-only.  It does not know about SQLite,
the resident worker, probability materialization, or an order gateway.  The
three policy families share a scope contract, but each receives its own
artifact, metrics, version and content hash.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .contracts import ContractError, _finite, _sha, _text, _timestamp, content_sha256, timestamp_lte, timestamp_lt


POLICY_FAMILIES = ("target", "reserve", "cadence")
WINDOW_DAYS = (3, 7, 15, 30)
ARTIFACT_SCHEMAS = {
    "target": "station_target_policy_artifact_v2",
    "reserve": "station_reserve_price_artifact_v2",
    "cadence": "station_cadence_artifact_v2",
}
ARTIFACT_SCHEMA = "station_policy_oos_artifact_v1"
PROMOTION_SCHEMA = "station_policy_oos_promotion_decision_v1"


def _mapping(name: str, value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{name}_mapping_required")
    return value


def _bool(name: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise ContractError(f"{name}_must_be_boolean")
    return value


def _nonnegative(name: str, value: Any) -> float:
    result = _finite(name, value)
    if result < 0:
        raise ContractError(f"{name}_must_be_nonnegative")
    return result


def _sha_or_empty(name: str, value: Any) -> str | None:
    if value is None:
        return None
    return _sha(name, value)


def _valid_timezone(name: str, value: Any) -> str:
    text = _text(name, value)
    try:
        ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ContractError(f"{name}_invalid") from exc
    return text


@dataclass(frozen=True)
class StationPolicyScope:
    station_id: str
    settlement_timezone: str
    season_bin: str
    lead_bucket: str
    issuance_cycle: str
    stage: str
    maturity_bin: str
    liquidity_bin: str
    strategy_version: str
    train_start_utc: str
    train_end_utc: str
    test_start_utc: str
    test_end_utc: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "station_id": self.station_id,
            "settlement_timezone": self.settlement_timezone,
            "season_bin": self.season_bin,
            "lead_bucket": self.lead_bucket,
            "issuance_cycle": self.issuance_cycle,
            "stage": self.stage,
            "maturity_bin": self.maturity_bin,
            "liquidity_bin": self.liquidity_bin,
            "strategy_version": self.strategy_version,
            "train_start_utc": self.train_start_utc,
            "train_end_utc": self.train_end_utc,
            "test_start_utc": self.test_start_utc,
            "test_end_utc": self.test_end_utc,
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "StationPolicyScope":
        raw = _mapping("station_policy_scope", raw)
        normalized = cls(
            station_id=_text("scope_station_id", raw.get("station_id")),
            settlement_timezone=_valid_timezone("scope_settlement_timezone", raw.get("settlement_timezone")),
            season_bin=_text("scope_season_bin", raw.get("season_bin")),
            lead_bucket=_text("scope_lead_bucket", raw.get("lead_bucket")),
            issuance_cycle=_text("scope_issuance_cycle", raw.get("issuance_cycle")),
            stage=_text("scope_stage", raw.get("stage")),
            maturity_bin=_text("scope_maturity_bin", raw.get("maturity_bin")),
            liquidity_bin=_text("scope_liquidity_bin", raw.get("liquidity_bin")),
            strategy_version=_text("scope_strategy_version", raw.get("strategy_version")),
            train_start_utc=_timestamp("scope_train_start_utc", raw.get("train_start_utc")),
            train_end_utc=_timestamp("scope_train_end_utc", raw.get("train_end_utc")),
            test_start_utc=_timestamp("scope_test_start_utc", raw.get("test_start_utc")),
            test_end_utc=_timestamp("scope_test_end_utc", raw.get("test_end_utc")),
        )
        if not timestamp_lt(normalized.train_start_utc, normalized.train_end_utc):
            raise ContractError("station_policy_train_window_invalid")
        if not timestamp_lt(normalized.test_start_utc, normalized.test_end_utc):
            raise ContractError("station_policy_test_window_invalid")
        # A shared boundary is safe: training is strictly before train_end,
        # while test is at/after test_start.  Equality therefore creates no
        # overlap and is the canonical cutover representation.
        if not timestamp_lte(normalized.train_end_utc, normalized.test_start_utc):
            raise ContractError("station_policy_train_test_overlap")
        return normalized


@dataclass(frozen=True)
class StationPolicyArtifact:
    artifact_id: str
    artifact_version: str
    family: str
    scope: StationPolicyScope
    theta_local: Mapping[str, float]
    theta_post: Mapping[str, float]
    rho: float
    n_eff: float
    sample_n: int
    metrics: Mapping[str, Any]
    source_evidence_sha256: str
    parent_artifact_sha256: str | None
    parameter_delta_max: float
    content_sha256: str
    promoted: bool = False

    @property
    def schema(self) -> str:
        return ARTIFACT_SCHEMAS[self.family]

    def content_identity(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "artifact_id": self.artifact_id,
            "artifact_version": self.artifact_version,
            "family": self.family,
            "scope": self.scope.to_dict(),
            "theta_local": dict(sorted(self.theta_local.items())),
            "theta_post": dict(sorted(self.theta_post.items())),
            "rho": self.rho,
            "n_eff": self.n_eff,
            "sample_n": self.sample_n,
            "metrics": self.metrics,
            "source_evidence_sha256": self.source_evidence_sha256,
            "parent_artifact_sha256": self.parent_artifact_sha256,
            "parameter_delta_max": self.parameter_delta_max,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.content_identity(), "promoted": self.promoted, "content_sha256": self.content_sha256}

    def validate(self) -> "StationPolicyArtifact":
        return type(self).from_mapping(self.to_dict())

    @classmethod
    def create(cls, **kwargs: Any) -> "StationPolicyArtifact":
        raw = dict(kwargs)
        raw.setdefault("promoted", False)
        normalized = cls._normalize(raw)
        raw["content_sha256"] = content_sha256({key: value for key, value in normalized.items() if key != "promoted"})
        return cls.from_mapping(raw)

    @classmethod
    def _normalize(cls, raw: Mapping[str, Any]) -> dict[str, Any]:
        raw = _mapping("station_policy_artifact", raw)
        family = _text("station_policy_family", raw.get("family")).lower()
        if family not in POLICY_FAMILIES:
            raise ContractError("station_policy_family_invalid")
        scope_raw = raw.get("scope")
        scope = scope_raw if isinstance(scope_raw, StationPolicyScope) else StationPolicyScope.from_mapping(scope_raw)
        local_raw = _mapping("station_policy_theta_local", raw.get("theta_local"))
        post_raw = _mapping("station_policy_theta_post", raw.get("theta_post"))
        local = {str(key): _finite("station_policy_theta_local_value", value) for key, value in local_raw.items()}
        post = {str(key): _finite("station_policy_theta_post_value", value) for key, value in post_raw.items()}
        if not local or not post or set(local) != set(post):
            raise ContractError("station_policy_theta_key_set_invalid")
        n_eff = _nonnegative("station_policy_n_eff", raw.get("n_eff"))
        sample_n_raw = raw.get("sample_n")
        if isinstance(sample_n_raw, bool):
            raise ContractError("station_policy_sample_n_invalid")
        try:
            sample_n = int(sample_n_raw)
        except (TypeError, ValueError) as exc:
            raise ContractError("station_policy_sample_n_invalid") from exc
        if sample_n < 1 or n_eff <= 0 or n_eff > sample_n + 1e-9:
            raise ContractError("station_policy_sample_n_invalid")
        rho = _finite("station_policy_rho", raw.get("rho"))
        if not 0.0 <= rho <= 1.0:
            raise ContractError("station_policy_rho_invalid")
        delta = _nonnegative("station_policy_parameter_delta_max", raw.get("parameter_delta_max", 0.0))
        metrics = dict(_mapping("station_policy_metrics", raw.get("metrics")))
        source_sha = _sha("station_policy_source_evidence_sha256", raw.get("source_evidence_sha256"))
        parent_sha = _sha_or_empty("station_policy_parent_artifact_sha256", raw.get("parent_artifact_sha256"))
        return {
            "artifact_id": _text("station_policy_artifact_id", raw.get("artifact_id")),
            "artifact_version": _text("station_policy_artifact_version", raw.get("artifact_version")),
            "family": family,
            "scope": scope,
            "theta_local": local,
            "theta_post": post,
            "rho": rho,
            "n_eff": n_eff,
            "sample_n": sample_n,
            "metrics": metrics,
            "source_evidence_sha256": source_sha,
            "parent_artifact_sha256": parent_sha,
            "parameter_delta_max": delta,
            "promoted": _bool("station_policy_promoted", raw.get("promoted", False)),
        }

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "StationPolicyArtifact":
        normalized = cls._normalize(raw)
        supplied = _sha("station_policy_content_sha256", raw.get("content_sha256"))
        expected = content_sha256({key: value for key, value in normalized.items() if key != "promoted"})
        if supplied != expected:
            raise ContractError("station_policy_content_sha256_mismatch")
        return cls(content_sha256=supplied, **normalized)


def build_promotion_decision(
    *,
    artifact: StationPolicyArtifact,
    evidence: Mapping[str, Any],
    promotion_requested: bool = False,
    decided_at_utc: str,
    parameter_change_limit: float,
) -> dict[str, Any]:
    """Create an immutable family-specific promotion decision.

    Eligibility is intentionally distinct from ``promoted``.  A research
    receipt may prove that a candidate is promotion-grade while the caller
    still leaves the artifact inactive (the default for this target lane).
    """

    if not isinstance(promotion_requested, bool):
        raise ContractError("station_policy_promotion_requested_invalid")
    if not isinstance(evidence, Mapping) or not evidence:
        raise ContractError("station_policy_promotion_evidence_missing")
    if parameter_change_limit < 0:
        raise ContractError("station_policy_parameter_change_limit_invalid")
    eligible = evidence.get("promotion_eligible") is True
    delta_ok = float(artifact.parameter_delta_max) <= float(parameter_change_limit) + 1e-12
    if not delta_ok:
        eligible = False
    promoted = bool(promotion_requested and eligible)
    identity = {
        "schema": PROMOTION_SCHEMA,
        "decision_id": f"promotion-{artifact.artifact_id}-{artifact.content_sha256[:16]}",
        "artifact_id": artifact.artifact_id,
        "artifact_schema": artifact.schema,
        "artifact_content_sha256": artifact.content_sha256,
        "family": artifact.family,
        "station_id": artifact.scope.station_id,
        "scope": artifact.scope.to_dict(),
        "evidence_sha256": content_sha256(evidence),
        "evidence": dict(evidence),
        "parameter_change_limit": float(parameter_change_limit),
        "parameter_change_within_limit": delta_ok,
        "promotion_requested": promotion_requested,
        "promotion_eligible": eligible,
        "promoted": promoted,
        "decision": "PROMOTED" if promoted else "REJECTED",
        "reason_codes": [] if eligible else ["oos_gate_failed"],
        "decided_at_utc": _timestamp("station_policy_decided_at_utc", decided_at_utc),
        "production_no_live": True,
        "live_order_authorized": False,
        "writes_production_db": False,
        "calls_order_gateway": False,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


__all__ = [
    "ARTIFACT_SCHEMA",
    "ARTIFACT_SCHEMAS",
    "POLICY_FAMILIES",
    "PROMOTION_SCHEMA",
    "StationPolicyArtifact",
    "StationPolicyScope",
    "WINDOW_DAYS",
    "build_promotion_decision",
]
