"""Forecast-only background context owned by one weak evidence snapshot.

The context is auditable input provenance, not an independent likelihood and
not an active transition bias.  Promotion into a transition artifact requires
separate calibration and evidence that it does not duplicate hourly forcing.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = "forecast_background_context_v1"
REGIMES = (
    "clear_dry",
    "cloudy_stable",
    "wet_precipitation",
    "convection_or_showers",
    "fog_low_cloud",
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def build_forecast_background_context(
    *,
    forecast_evidence_snapshot_id: str,
    decision_as_of: str,
    source_revisions: Iterable[Mapping[str, Any]],
    source_expert_trace: Iterable[Mapping[str, Any]],
    missing_source_ids: Iterable[str] = (),
) -> dict[str, Any]:
    revisions = {
        (_text(row.get("source_id")), _text(row.get("source_revision_id"))): dict(row)
        for row in source_revisions
        if isinstance(row, Mapping)
    }
    raw_rows: list[dict[str, Any]] = []
    total_weight = 0.0
    known_weight = 0.0
    regime_weights = {regime: 0.0 for regime in REGIMES}
    for raw in source_expert_trace:
        if not isinstance(raw, Mapping):
            continue
        row = dict(raw)
        identity = (_text(row.get("source_id")), _text(row.get("source_revision_id")))
        revision = revisions.get(identity, {})
        try:
            weight = float(row.get("information_weight") or 0.0)
        except (TypeError, ValueError):
            weight = 0.0
        if not math.isfinite(weight) or weight < 0.0:
            raise ValueError("forecast_background_information_weight_invalid")
        regime = _text(revision.get("forecast_weather_regime")).lower()
        recognized = regime in REGIMES
        total_weight += weight
        if recognized:
            regime_weights[regime] += weight
            known_weight += weight
        raw_rows.append(
            {
                "source_id": identity[0],
                "source_revision_id": identity[1],
                "forecast_background_regime": regime if recognized else "unknown",
                "information_weight": weight,
                "recognized_regime": recognized,
            }
        )
    denominator = total_weight if total_weight > 0.0 else 1.0
    vector = {regime: regime_weights[regime] / denominator for regime in REGIMES}
    unknown_probability = max(0.0, min(1.0, 1.0 - sum(vector.values())))
    contributions = [
        {**row, "normalized_information_contribution": row["information_weight"] / denominator}
        for row in raw_rows
    ]
    core = {
        "schema": SCHEMA_VERSION,
        "forecast_evidence_snapshot_id": _text(forecast_evidence_snapshot_id),
        "effective_at": _text(decision_as_of),
        "regime_probability_vector": vector,
        "unknown_probability": unknown_probability,
        "state": "available" if known_weight > 0.0 else "unknown_neutral",
        "source_contributions": contributions,
        "missing_source_ids": sorted({_text(value) for value in missing_source_ids if _text(value)}),
        "forecast_only": True,
        "physical_weather_regime_consumed": False,
        "transition_control_active": False,
        "changes_probability": False,
        "changes_source_weights": False,
        "requires_new_calibrated_transition_artifact_for_activation": True,
    }
    if not core["forecast_evidence_snapshot_id"] or not core["effective_at"]:
        raise ValueError("forecast_background_context_identity_missing")
    return {**core, "forecast_background_context_sha256": _sha(core)}


def validate_forecast_background_context(value: Mapping[str, Any]) -> dict[str, Any]:
    context = dict(_mapping(value))
    if _text(context.get("schema")) != SCHEMA_VERSION:
        raise ValueError("forecast_background_context_schema_invalid")
    vector = _mapping(context.get("regime_probability_vector"))
    if set(vector) != set(REGIMES):
        raise ValueError("forecast_background_context_vector_keys_invalid")
    probabilities = [float(vector[regime]) for regime in REGIMES]
    unknown = float(context.get("unknown_probability"))
    if any(not math.isfinite(item) or item < 0.0 or item > 1.0 for item in [*probabilities, unknown]):
        raise ValueError("forecast_background_context_probability_invalid")
    if abs(sum(probabilities) + unknown - 1.0) > 1e-9:
        raise ValueError("forecast_background_context_not_normalized")
    if (
        context.get("forecast_only") is not True
        or context.get("physical_weather_regime_consumed") is not False
        or context.get("transition_control_active") is not False
        or context.get("changes_probability") is not False
    ):
        raise ValueError("forecast_background_context_boundary_invalid")
    expected = _sha({key: child for key, child in context.items() if key != "forecast_background_context_sha256"})
    if expected != _text(context.get("forecast_background_context_sha256")):
        raise ValueError("forecast_background_context_sha_mismatch")
    return context


__all__ = [
    "REGIMES",
    "SCHEMA_VERSION",
    "build_forecast_background_context",
    "validate_forecast_background_context",
]
