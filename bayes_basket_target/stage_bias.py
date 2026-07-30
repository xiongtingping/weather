"""Probability-only stage direction contract; market and weather fields are absent."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .basket_kernel.contracts import Direction, TargetBasketPolicy
from .basket_kernel.support import (
    _normalized_entropy,
    expected_direction_from_probability_vector,
    probability_center_diagnostics,
)
from .contracts import ContractError, ProbabilityEnvelope, content_sha256


@dataclass(frozen=True)
class StageProbabilityBiasContract:
    stage: str
    checkpoint_id: str
    center_bucket: str
    runner_up_bucket: str | None
    center_lead_probability: float
    center_stable: bool
    lower_side_mass: float
    upper_side_mass: float
    direction_skew: float
    normalized_entropy: float
    direction_side_mass: float
    direction: str
    evidence_fresh: bool

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "StageProbabilityBiasContract":
        allowed = {
            "schema",
            "stage",
            "checkpoint_id",
            "center_bucket",
            "runner_up_bucket",
            "center_lead_probability",
            "center_stable",
            "lower_side_mass",
            "upper_side_mass",
            "direction_skew",
            "normalized_entropy",
            "direction_side_mass",
            "direction",
            "evidence_fresh",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise ContractError(
                "stage_probability_bias_unknown_fields:"
                + ",".join(sorted(str(item) for item in unknown))
            )
        if raw.get("schema") not in {None, "stage_probability_bias_contract_v1"}:
            raise ContractError("stage_probability_bias_schema_invalid")
        direction = str(raw.get("direction") or "").strip().upper()
        if direction not in {item.value for item in Direction}:
            raise ContractError("stage_probability_bias_direction_invalid")
        return cls(
            stage=str(raw.get("stage") or "").strip(),
            checkpoint_id=str(raw.get("checkpoint_id") or "").strip(),
            center_bucket=str(raw.get("center_bucket") or "").strip(),
            runner_up_bucket=(
                str(raw.get("runner_up_bucket")).strip()
                if raw.get("runner_up_bucket") is not None
                else None
            ),
            center_lead_probability=float(raw.get("center_lead_probability")),
            center_stable=raw.get("center_stable") is True,
            lower_side_mass=float(raw.get("lower_side_mass")),
            upper_side_mass=float(raw.get("upper_side_mass")),
            direction_skew=float(raw.get("direction_skew")),
            normalized_entropy=float(raw.get("normalized_entropy")),
            direction_side_mass=float(raw.get("direction_side_mass")),
            direction=direction,
            evidence_fresh=raw.get("evidence_fresh") is True,
        )

    @classmethod
    def materialize(
        cls,
        envelope: ProbabilityEnvelope,
        policy: TargetBasketPolicy,
        *,
        at_utc: str,
    ) -> "StageProbabilityBiasContract":
        probabilities = envelope.probabilities
        ordinals = envelope.bucket_ordinals
        center, runner, lead, stable = probability_center_diagnostics(
            probabilities,
            ordinals,
            policy,
        )
        center_ordinal = ordinals[center]
        lower = sum(
            probability
            for bucket, probability in probabilities.items()
            if ordinals[bucket] < center_ordinal
        )
        upper = sum(
            probability
            for bucket, probability in probabilities.items()
            if ordinals[bucket] > center_ordinal
        )
        fresh = envelope.is_fresh(at_utc)
        direction = expected_direction_from_probability_vector(
            probabilities,
            ordinals,
            policy,
            evidence_fresh=fresh,
        )
        side_mass = upper if direction is Direction.UP else lower if direction is Direction.DOWN else 0.0
        return cls(
            stage=envelope.stage,
            checkpoint_id=envelope.checkpoint_id,
            center_bucket=center,
            runner_up_bucket=runner,
            center_lead_probability=lead,
            center_stable=stable,
            lower_side_mass=lower,
            upper_side_mass=upper,
            direction_skew=upper - lower,
            normalized_entropy=_normalized_entropy(probabilities.values()),
            direction_side_mass=side_mass,
            direction=direction.value,
            evidence_fresh=fresh,
        )

    @property
    def content_sha256(self) -> str:
        return content_sha256(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "stage_probability_bias_contract_v1",
            "stage": self.stage,
            "checkpoint_id": self.checkpoint_id,
            "center_bucket": self.center_bucket,
            "runner_up_bucket": self.runner_up_bucket,
            "center_lead_probability": self.center_lead_probability,
            "center_stable": self.center_stable,
            "lower_side_mass": self.lower_side_mass,
            "upper_side_mass": self.upper_side_mass,
            "direction_skew": self.direction_skew,
            "normalized_entropy": self.normalized_entropy,
            "direction_side_mass": self.direction_side_mass,
            "direction": self.direction,
            "evidence_fresh": self.evidence_fresh,
        }


__all__ = ["StageProbabilityBiasContract"]
