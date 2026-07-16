"""Assemble one strict station-day strong ``physical_x`` training sample.

All inputs are already-frozen evidence: a P2 physical-day canonical event and
its predecessor sequence, a closed final X, a complete/as-of P0 context, and
forecast revisions visible at the event's actual T1.  This module makes no
database reads and never creates a posterior or a strategy signal.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Sequence

from temperature_trajectory_feature_runtime import (
    build_empty_pre_event_temperature_trajectory_feature_state,
    build_temperature_trajectory_feature_state,
)
from trajectory_label_sample_runtime import build_temperature_incremental_bayes_training_sample


SCHEMA_VERSION = "station_day_strong_physical_training_assembly_v1"
FEATURE_BUILDER_VERSION = "station_day_strong_physical_features_v1"
LABEL_BUILDER_VERSION = "amsc_final_physical_x_label_v1"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def _blocked(*reasons: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "ok": False,
        "state": "blocked",
        "block_reasons": sorted(set(reason for reason in reasons if reason)),
        "read_only": True,
        "shadow_only": True,
        "places_orders": False,
    }


def build_station_day_strong_physical_training_sample(
    *,
    canonical_observation_event: Mapping[str, Any],
    canonical_predecessors: Sequence[Mapping[str, Any]],
    coverage_manifest: Mapping[str, Any],
    final_physical_x_label: Mapping[str, Any],
    full_physical_p0_prior_context: Mapping[str, Any],
    forecast_evidence: Mapping[str, Any],
    source_reliability_pre_state: Mapping[str, Any],
    temperature_quantization_c: float = 1.0,
) -> dict[str, Any]:
    """Create a B-class physical-X sample without requiring S/B/bridge."""

    event = _mapping(canonical_observation_event)
    p0 = _mapping(full_physical_p0_prior_context)
    forecast = _mapping(forecast_evidence)
    label = _mapping(final_physical_x_label)
    target = _text(event.get("target_identity"))
    day = _text(event.get("event_date") or event.get("valid_time"))[:10]
    station = _text(event.get("station_id")).upper()
    cutoff = _text(event.get("first_actionable_seen_at"))
    issues: list[str] = []
    if not target or not day or not station or _time(cutoff) is None:
        issues.append("station_day_strong_sample_canonical_identity_or_t1_invalid")
    if p0.get("schema") != "full_physical_p0_prior_context_v1" or p0.get("ok") is not True or p0.get("state") != "available_shadow_only":
        issues.append("station_day_strong_sample_full_physical_p0_unavailable")
    if not _text(p0.get("prior_context_id")):
        issues.append("station_day_strong_sample_prior_context_id_missing")
    if _text(forecast.get("forecast_evidence_snapshot_id")) == "":
        issues.append("station_day_strong_sample_forecast_snapshot_id_missing")
    revisions = forecast.get("source_revisions") if isinstance(forecast.get("source_revisions"), list) else []
    if not revisions:
        issues.append("station_day_strong_sample_forecast_revisions_missing")
    high = _finite(label.get("final_physical_high_c"))
    label_available = _time(label.get("label_available_at"))
    if high is None or _text(label.get("label_finality")) != "final" or label_available is None:
        issues.append("station_day_strong_sample_final_physical_x_label_invalid")
    elif label.get("strict_asof_training_eligible") is not True:
        issues.append("station_day_strong_sample_final_physical_x_not_strict_asof_eligible")
    elif _time(cutoff) is not None and label_available <= _time(cutoff):
        issues.append("station_day_strong_sample_final_x_not_post_cutoff")
    if issues:
        return _blocked(*issues)

    predecessors = [dict(row) for row in canonical_predecessors if isinstance(row, Mapping)]
    predecessor_ids = [_text(row.get("canonical_observation_event_id")) for row in predecessors]
    if any(not value for value in predecessor_ids) or len(predecessor_ids) != len(set(predecessor_ids)):
        return _blocked("station_day_strong_sample_predecessor_identity_invalid")
    sequence = [*predecessor_ids, _text(event.get("canonical_observation_event_id"))]
    if not sequence[-1]:
        return _blocked("station_day_strong_sample_current_event_id_missing")
    if predecessors:
        trajectory = build_temperature_trajectory_feature_state(
            canonical_events=predecessors,
            decision_as_of=cutoff,
            temperature_quantization_c=temperature_quantization_c,
            temporal_role="pre_event",
        )
    else:
        trajectory = build_empty_pre_event_temperature_trajectory_feature_state(
            target_identity=target,
            station_id=station,
            decision_as_of=cutoff,
            temperature_quantization_c=temperature_quantization_c,
        )
    if trajectory.get("ok") is not True:
        return _blocked(*list(trajectory.get("block_reasons") or ["station_day_strong_sample_pre_event_trajectory_unavailable"]))

    parsed = _mapping(event.get("parsed_observation"))
    current_temperature = _finite(parsed.get("temperature_c") if "temperature_c" in parsed else event.get("temperature_c"))
    previous_temperature = None
    if predecessors:
        previous = _mapping(predecessors[-1])
        previous_parsed = _mapping(previous.get("parsed_observation"))
        previous_temperature = _finite(previous_parsed.get("temperature_c") if "temperature_c" in previous_parsed else previous.get("temperature_c"))
    forecast_by_source = {
        _text(row.get("source_id")): _finite(row.get("forecast_peak_c"))
        for row in revisions if isinstance(row, Mapping) and _text(row.get("source_id"))
    }
    features = {
        "canonical_new_observation": dict(parsed),
        "observation_innovation": {
            "current_minus_previous_c": current_temperature - previous_temperature if current_temperature is not None and previous_temperature is not None else None,
        },
        "trajectory_path_memory": dict(trajectory),
        "forecast_evidence": {source: {"forecast_peak_c": value} for source, value in forecast_by_source.items()},
        "forecast_observation_residuals": {
            source: {"current_minus_forecast_peak_c": current_temperature - value if current_temperature is not None and value is not None else None}
            for source, value in forecast_by_source.items()
        },
        "prior_features": {
            "prior_context_id": _text(p0.get("prior_context_id")),
            "basin_context": _text(p0.get("basin_context")),
        },
        "observed_max_lower_bound_c": trajectory.get("observed_max_lower_bound_c"),
        "time_context": {"valid_time": _text(event.get("valid_time")), "decision_as_of": cutoff},
        "quality_missingness_quantization": {
            "temperature_quantization_c": temperature_quantization_c,
            "source_reliability_pre_state": dict(source_reliability_pre_state),
        },
    }
    physical_label = {
        "final_physical_high_c": high,
        "label_available_at": _text(label.get("label_available_at")),
        "label_finality": "final",
        "strict_asof_training_eligible": True,
        "label_provenance": dict(_mapping(label.get("label_provenance"))),
    }
    sample = build_temperature_incremental_bayes_training_sample(
        target_identity=target,
        event_date=day,
        canonical_observation_event=event,
        previous_canonical_event_id=predecessor_ids[-1] if predecessor_ids else "",
        canonical_sequence_event_ids=sequence,
        prior_context_id=_text(p0.get("prior_context_id")),
        forecast_evidence_snapshot_id=_text(forecast.get("forecast_evidence_snapshot_id")),
        source_revisions=[dict(row) for row in revisions if isinstance(row, Mapping)],
        coverage_manifest=coverage_manifest,
        feature_payload=features,
        resolution_label=physical_label,
        feature_builder_version=FEATURE_BUILDER_VERSION,
        label_builder_version=LABEL_BUILDER_VERSION,
    )
    return {
        "schema": SCHEMA_VERSION,
        "ok": sample.get("physical_auxiliary_eligible") is True,
        "state": "available_physical_x" if sample.get("physical_auxiliary_eligible") is True else "blocked",
        "training_sample": sample,
        "pre_event_trajectory": trajectory,
        "boundary": {
            "training_scope": "physical_x",
            "market_finality_not_required": True,
            "settlement_s_or_bucket_not_required": True,
            "final_x_is_label_not_feature": True,
            "writes_posterior": False,
            "places_orders": False,
        },
    }


__all__ = ["SCHEMA_VERSION", "build_station_day_strong_physical_training_sample"]
