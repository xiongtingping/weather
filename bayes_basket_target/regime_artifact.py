"""Content-addressed, already-materialized regime SSOT.

The regime artifact is deliberately a *decision aid*, not a second
probability model.  It contains only derived diagnostics and the bounded
concentration control that was produced by an offline/materialization step.
Raw weather fields are rejected at this boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from .contracts import ContractError, content_sha256


REGIME_ARTIFACT_SCHEMA = "bayes_basket_regime_artifact_v1"
REGIME_ARTIFACT_VERSION = "v1"
REGIMES = frozenset({"NEUTRAL", "UP", "DOWN"})
_FORBIDDEN = frozenset({"taf", "nwp", "metar", "speci", "temperature", "weather", "raw"})
_KEYS = frozenset(
    {
        "schema", "artifact_version", "artifact_id", "station_id", "event_date",
        "stage", "checkpoint_id", "settlement_timezone", "as_of_utc",
        "valid_from_utc", "valid_until_utc", "probability_sha256",
        "regime", "center_stable", "center_lead_margin",
        "stability_threshold", "side_mass_up", "side_mass_down",
        "direction_mass_margin", "direction_threshold",
        "model_disagreement", "model_extreme_low_bucket", "model_extreme_high_bucket",
        "concentration_multiplier", "content_sha256",
    }
)


def _text(name: str, value: Any) -> str:
    value = str(value or "").strip()
    if not value:
        raise ContractError(f"regime_{name}_missing")
    return value


def _timestamp(name: str, value: Any) -> str:
    value = _text(name, value)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"regime_{name}_invalid_timestamp") from exc
    if parsed.tzinfo is None:
        raise ContractError(f"regime_{name}_timezone_required")
    return value


def _sha(name: str, value: Any) -> str:
    result = _text(name, value).lower()
    if (
        len(result) != 64
        or any(character not in "0123456789abcdef" for character in result)
    ):
        raise ContractError(f"regime_{name}_invalid")
    return result


def _number(name: str, value: Any, *, lower: float = 0.0, upper: float = 1.0) -> float:
    if isinstance(value, bool):
        raise ContractError(f"regime_{name}_invalid")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"regime_{name}_invalid") from exc
    if result != result or result in {float("inf"), float("-inf")} or not lower <= result <= upper:
        raise ContractError(f"regime_{name}_invalid")
    return result


def _contains_forbidden_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).strip().lower() in _FORBIDDEN:
                return True
            if _contains_forbidden_key(child):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_contains_forbidden_key(child) for child in value)
    return False


@dataclass(frozen=True)
class RegimeArtifact:
    artifact_version: str
    artifact_id: str
    station_id: str
    event_date: str
    stage: str
    checkpoint_id: str
    settlement_timezone: str
    as_of_utc: str
    valid_from_utc: str
    valid_until_utc: str
    probability_sha256: str
    regime: str
    center_stable: bool
    center_lead_margin: float
    stability_threshold: float
    side_mass_up: float
    side_mass_down: float
    direction_mass_margin: float
    direction_threshold: float
    model_disagreement: float
    model_extreme_low_bucket: str
    model_extreme_high_bucket: str
    concentration_multiplier: float
    content_sha256: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "RegimeArtifact":
        if not isinstance(raw, Mapping):
            raise ContractError("regime_artifact_invalid")
        if set(raw) - _KEYS:
            raise ContractError("regime_artifact_unknown_field")
        if _contains_forbidden_key(raw):
            raise ContractError("regime_raw_weather_field_forbidden")
        if raw.get("schema") != REGIME_ARTIFACT_SCHEMA:
            raise ContractError("regime_artifact_schema_invalid")
        if raw.get("artifact_version") != REGIME_ARTIFACT_VERSION:
            raise ContractError("regime_artifact_version_invalid")
        required = _KEYS - {"schema", "content_sha256"}
        if any(key not in raw for key in required):
            raise ContractError("regime_artifact_field_missing")
        stage = _text("stage", raw["stage"])
        if stage not in {"P0", "P0-M", "P1"}:
            raise ContractError("regime_stage_invalid")
        regime = _text("regime", raw["regime"]).upper()
        if regime not in REGIMES:
            raise ContractError("regime_value_invalid")
        if not isinstance(raw["center_stable"], bool):
            raise ContractError("regime_center_stable_invalid")
        valid_from = _timestamp("valid_from_utc", raw["valid_from_utc"])
        valid_until = _timestamp("valid_until_utc", raw["valid_until_utc"])
        as_of = _timestamp("as_of_utc", raw["as_of_utc"])
        try:
            if datetime.fromisoformat(valid_from.replace("Z", "+00:00")) >= datetime.fromisoformat(valid_until.replace("Z", "+00:00")):
                raise ContractError("regime_validity_window_invalid")
            if not datetime.fromisoformat(valid_from.replace("Z", "+00:00")) <= datetime.fromisoformat(as_of.replace("Z", "+00:00")) < datetime.fromisoformat(valid_until.replace("Z", "+00:00")):
                raise ContractError("regime_as_of_outside_validity")
        except ContractError:
            raise
        body = dict(raw)
        body.pop("content_sha256", None)
        expected = content_sha256(body)
        if str(raw.get("content_sha256") or "").lower() != expected:
            raise ContractError("regime_content_hash_mismatch")
        probability_sha = _sha(
            "probability_sha256",
            raw["probability_sha256"],
        )
        center_margin = _number(
            "center_lead_margin",
            raw["center_lead_margin"],
        )
        stability_threshold = _number(
            "stability_threshold",
            raw["stability_threshold"],
        )
        side_up = _number("side_mass_up", raw["side_mass_up"])
        side_down = _number("side_mass_down", raw["side_mass_down"])
        direction_margin = _number(
            "direction_mass_margin",
            raw["direction_mass_margin"],
            lower=-1.0,
            upper=1.0,
        )
        direction_threshold = _number(
            "direction_threshold",
            raw["direction_threshold"],
        )
        stable = bool(raw["center_stable"])
        if stable != (center_margin >= stability_threshold):
            raise ContractError("regime_center_stability_inconsistent")
        if abs(direction_margin - (side_up - side_down)) > 1e-9:
            raise ContractError("regime_direction_mass_decomposition_mismatch")
        expected_regime = (
            "NEUTRAL"
            if not stable or abs(direction_margin) < direction_threshold
            else "UP"
            if direction_margin > 0.0
            else "DOWN"
        )
        if regime != expected_regime:
            raise ContractError("regime_probability_direction_inconsistent")
        return cls(
            artifact_version=raw["artifact_version"], artifact_id=_text("artifact_id", raw["artifact_id"]),
            station_id=_text("station_id", raw["station_id"]), event_date=_text("event_date", raw["event_date"]),
            stage=stage, checkpoint_id=_text("checkpoint_id", raw["checkpoint_id"]),
            settlement_timezone=_text("settlement_timezone", raw["settlement_timezone"]), as_of_utc=as_of,
            valid_from_utc=valid_from, valid_until_utc=valid_until,
            probability_sha256=probability_sha,
            regime=regime,
            center_stable=stable,
            center_lead_margin=center_margin,
            stability_threshold=stability_threshold,
            side_mass_up=side_up,
            side_mass_down=side_down,
            direction_mass_margin=direction_margin,
            direction_threshold=direction_threshold,
            model_disagreement=_number("model_disagreement", raw["model_disagreement"], upper=float("inf")),
            model_extreme_low_bucket=_text("model_extreme_low_bucket", raw["model_extreme_low_bucket"]),
            model_extreme_high_bucket=_text("model_extreme_high_bucket", raw["model_extreme_high_bucket"]),
            concentration_multiplier=_number("concentration_multiplier", raw["concentration_multiplier"], lower=0.05),
            content_sha256=expected,
        )

    def to_dict(self) -> dict[str, Any]:
        body = {
            "schema": REGIME_ARTIFACT_SCHEMA, "artifact_version": self.artifact_version,
            "artifact_id": self.artifact_id, "station_id": self.station_id, "event_date": self.event_date,
            "stage": self.stage, "checkpoint_id": self.checkpoint_id, "settlement_timezone": self.settlement_timezone,
            "as_of_utc": self.as_of_utc, "valid_from_utc": self.valid_from_utc, "valid_until_utc": self.valid_until_utc,
            "probability_sha256": self.probability_sha256,
            "regime": self.regime, "center_stable": self.center_stable,
            "center_lead_margin": self.center_lead_margin,
            "stability_threshold": self.stability_threshold,
            "side_mass_up": self.side_mass_up, "side_mass_down": self.side_mass_down, "model_disagreement": self.model_disagreement,
            "direction_mass_margin": self.direction_mass_margin,
            "direction_threshold": self.direction_threshold,
            "model_extreme_low_bucket": self.model_extreme_low_bucket, "model_extreme_high_bucket": self.model_extreme_high_bucket,
            "concentration_multiplier": self.concentration_multiplier,
        }
        body["content_sha256"] = content_sha256(body)
        return body


def resolve_regime_artifact(
    artifact: RegimeArtifact | Mapping[str, Any], *, station_id: str, event_date: str,
    stage: str, checkpoint_id: str, settlement_timezone: str,
    probability_sha256: str, evaluation_at_utc: str,
) -> RegimeArtifact:
    resolved = artifact if isinstance(artifact, RegimeArtifact) else RegimeArtifact.from_mapping(artifact)
    if (resolved.station_id, resolved.event_date, resolved.stage, resolved.checkpoint_id, resolved.settlement_timezone) != (station_id, event_date, stage, checkpoint_id, settlement_timezone):
        raise ContractError("regime_exact_scope_mismatch")
    if resolved.probability_sha256 != _sha(
        "probability_sha256",
        probability_sha256,
    ):
        raise ContractError("regime_probability_version_mismatch")
    at = datetime.fromisoformat(_timestamp("evaluation_at_utc", evaluation_at_utc).replace("Z", "+00:00"))
    start = datetime.fromisoformat(resolved.valid_from_utc.replace("Z", "+00:00"))
    end = datetime.fromisoformat(resolved.valid_until_utc.replace("Z", "+00:00"))
    if not start <= at < end:
        raise ContractError("regime_artifact_expired")
    return resolved


__all__ = ["REGIME_ARTIFACT_SCHEMA", "REGIME_ARTIFACT_VERSION", "RegimeArtifact", "resolve_regime_artifact"]
