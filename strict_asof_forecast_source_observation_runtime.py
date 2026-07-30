"""Pure Tier-A forecast-capture to SourceObservation proposal adapter.

This adapter deliberately stops before any database write.  It is the only
allowed bridge from a future strict-as-of capture into a prospective P1
forecast observation proposal; historical Tier B/C evidence cannot cross it.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

from source_observation_runtime import build_source_observation
from strict_asof_forecast_capture_runtime import TIER_A
from weak_likelihood_source_set_runtime import SOURCE_UNIVERSE


SCHEMA_VERSION = "strict_asof_forecast_source_observation_proposal_v1"

# ``source_id`` in the joint weak model is a logical prediction source, while
# provider/transport identity remains immutable provenance.  Do not admit the
# retired Open-Meteo/model-display identifiers here: only the independent TAF,
# WU and TWC capture identities are normalized at this boundary.
_WEAK_SOURCE_BY_PROVIDER_SOURCE = {
    # Official NWP captures use provider-facing peak identities at the raw
    # transport boundary; the model contract consumes the canonical logical
    # source IDs.  Keep this mapping explicit so an official model capture is
    # normalized without accepting an arbitrary caller-supplied relabel.
    "ecmwf_model_peak": "ecmwf",
    "gfs_model_peak": "gfs",
    "icon_model_peak": "icon",
    "zuuu_taf_forecast_amsc": "taf",
    "wu_forecast_high": "wu",
    "wu_forecast_peak": "wu",
    "weather_com_forecast_peak": "twc",
    "twc_forecast_peak": "twc",
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _weak_source_id(provider_source_id: str, declared: Any) -> str:
    explicit = _text(declared)
    mapped = _WEAK_SOURCE_BY_PROVIDER_SOURCE.get(provider_source_id, provider_source_id)
    # A producer may repeat the canonical identity explicitly, but it may not
    # relabel a known transport source into another weak family.
    return mapped if not explicit else explicit if explicit == mapped else ""


def _forecast_value_semantics(forecast: Mapping[str, Any], peak: float) -> tuple[str, str]:
    explicit = _text(forecast.get("forecast_peak_valid_time") or forecast.get("valid_time"))
    if explicit:
        return explicit, "peak_point"
    candidates: list[str] = []
    for point in forecast.get("hourly_curve") or []:
        row = _mapping(point)
        value = _finite(row.get("temperature_c"))
        valid = _text(row.get("valid_time_utc") or row.get("valid_time") or row.get("valid_time_local"))
        if value is not None and valid and abs(value - peak) <= 1e-12:
            candidates.append(valid)
    if candidates:
        return sorted(candidates)[0], "peak_point"
    kind = _text(forecast.get("forecast_value_kind"))
    record_valid_time = _text(forecast.get("forecast_record_valid_time") or forecast.get("forecast_valid_local"))
    if kind == "daily_max_event_window" and record_valid_time:
        return record_valid_time, kind
    return "", ""


def build_tier_a_forecast_source_observation_proposal(
    *,
    capture: Mapping[str, Any],
    normalized_forecast: Mapping[str, Any],
    station_id: str,
    raw_ref: str,
) -> dict[str, Any]:
    """Return one append-ready forecast SourceObservation proposal or block.

    No fallback substitutes event-date midnight, provider-declared availability,
    historical retrieval time, or a derived-only body for a missing Tier-A
    valid time/transport/raw identity.
    """

    evidence = _mapping(capture)
    forecast = _mapping(normalized_forecast)
    reasons: list[str] = []
    if evidence.get("strict_training_eligible") is not True or _text(evidence.get("evidence_tier")) != TIER_A:
        reasons.append("forecast_source_observation_capture_not_tier_a")
    provider_source_id = _text(forecast.get("source_id") or evidence.get("source_id"))
    source_id = _weak_source_id(provider_source_id, forecast.get("weak_source_id"))
    event_date = _text(forecast.get("target_event_date_local") or evidence.get("event_date"))[:10]
    peak = _finite(forecast.get("forecast_peak_c"))
    valid_time, value_kind = _forecast_value_semantics(forecast, peak) if peak is not None else ("", "")
    first_seen = _text(evidence.get("first_system_seen_at"))
    raw_sha256 = _text(evidence.get("raw_response_sha256"))
    snapshot_id = _text(evidence.get("forecast_snapshot_id"))
    if not provider_source_id or not source_id or not event_date or not station_id:
        reasons.append("forecast_source_observation_identity_missing")
    if source_id not in SOURCE_UNIVERSE:
        reasons.append("forecast_source_observation_weak_source_id_not_in_unique_universe")
    if _text(evidence.get("source_id")) != provider_source_id or _text(evidence.get("event_date")) != event_date:
        reasons.append("forecast_source_observation_capture_forecast_identity_mismatch")
    if peak is None:
        reasons.append("forecast_source_observation_peak_missing_or_invalid")
    if not valid_time:
        reasons.append("forecast_source_observation_peak_or_daily_window_valid_time_missing")
    if not first_seen:
        reasons.append("forecast_source_observation_first_system_seen_missing")
    if not raw_sha256 or not snapshot_id or not _text(raw_ref):
        reasons.append("forecast_source_observation_raw_snapshot_identity_missing")
    if _text(forecast.get("forecast_snapshot_id")) != snapshot_id:
        reasons.append("forecast_source_observation_snapshot_not_bound_to_capture")
    if reasons:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "block_reasons": sorted(set(reasons)),
            "read_only": True,
        }
    observation = build_source_observation(
        source_id=source_id,
        station_id=_text(station_id),
        event_date=event_date,
        valid_time=valid_time,
        valid_time_local=valid_time,
        fetched_at=first_seen,
        parsed_at=first_seen,
        response_completed_at=first_seen,
        forecast_peak_c=peak,
        raw_ref=_text(raw_ref),
        raw_sha256_override=raw_sha256,
        parser_version=SCHEMA_VERSION,
        quality_flags=(
            "strict_asof_tier_a",
            "observed_transport",
            "response_headers_observed",
            "raw_provider_response_bound",
            *(() if value_kind == "peak_point" else ("daily_max_event_window",)),
        ),
        payload={
            "strict_asof_capture_id": _text(evidence.get("forecast_capture_id")),
            "provider_source_id": provider_source_id,
            "weak_source_id": source_id,
            "evidence_tier": TIER_A,
            "first_system_seen_at": first_seen,
            "available_at": first_seen,
            "forecast_snapshot_id": snapshot_id,
            "raw_response_sha256": raw_sha256,
            "forecast_value_kind": value_kind,
            "forecast_peak_valid_time": valid_time if value_kind == "peak_point" else "",
            "forecast_record_valid_time": valid_time if value_kind == "daily_max_event_window" else "",
            "trajectory_eligible": value_kind == "peak_point",
            "capture_time_provenance": "observed_transport",
            "response_headers_observed": True,
            "future_training_proposal_only": True,
        },
    ).as_dict()
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "proposal_ready",
        "source_observation": observation,
        "boundary": {
            "tier_a_only": True,
            "append_only_p1_proposal_not_p1_write": True,
            "writes_archive_db": False,
            "creates_likelihood": False,
            "creates_posterior": False,
            "places_orders": False,
        },
        "read_only": True,
    }


__all__ = ["SCHEMA_VERSION", "build_tier_a_forecast_source_observation_proposal"]
