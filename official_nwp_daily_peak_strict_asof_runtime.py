"""Adapt a complete official-NWP daily curve into one Tier-A P1 proposal.

An official daily maximum is derived from multiple direct GRIB point receipts,
not from one synthetic provider response.  This module keeps that distinction:
the aggregate hash remains bound to every raw receipt and point snapshot, and
the revision becomes usable only at the latest observed receipt completion.
It creates a proposal only; the caller separately owns any append transaction.
"""

from __future__ import annotations

from datetime import datetime
import math
from typing import Any, Mapping, Sequence

from source_observation_runtime import build_source_observation
from weak_likelihood_source_set_runtime import SOURCE_UNIVERSE


SCHEMA_VERSION = "official_nwp_daily_peak_strict_asof_proposal_v1"
PARSER_VERSION = SCHEMA_VERSION
TIER_A = "A_live_observed_transport"
_OFFICIAL_NWP_SOURCES = frozenset({"ecmwf", "icon", "gfs", "jma"})


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    return list(value) if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)) else []


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
    return number if math.isfinite(number) else None


def build_official_nwp_daily_peak_tier_a_proposal(
    *,
    revision: Mapping[str, Any],
    station_id: str,
    revision_ref: str,
) -> dict[str, Any]:
    """Return one append-ready P1 proposal, or fail closed with reasons.

    ``revision_ref`` must point to an immutable persisted daily-curve revision;
    the raw object references and hashes inside that revision remain the
    provider-response lineage rather than being replaced by derived JSON.
    """

    value = _mapping(revision)
    reasons: list[str] = []
    source_id = _text(value.get("source_id"))
    event_date = _text(value.get("event_date"))[:10]
    available_at = _text(value.get("available_at"))
    peak_valid_time = _text(value.get("forecast_peak_valid_time"))
    peak = _finite(value.get("forecast_peak_c"))
    raw_sha256 = _text(value.get("raw_sha256"))
    raw_sha256s = [_text(item) for item in _sequence(value.get("raw_sha256s"))]
    raw_payload_sha256s = [_sequence(item) for item in _sequence(value.get("raw_payload_sha256s"))]
    raw_refs = [_text(item) for item in _sequence(value.get("raw_object_refs"))]
    snapshot_sha256s = [_text(item) for item in _sequence(value.get("snapshot_sha256s"))]
    expected = [_text(item) for item in _sequence(value.get("expected_valid_times_utc"))]
    curve = [_mapping(item) for item in _sequence(value.get("hourly_curve"))]

    if value.get("schema") != "official_nwp_daily_peak_forecast_revision_v1" or value.get("ok") is not True or value.get("state") != "available":
        reasons.append("official_nwp_daily_peak_revision_not_available")
    if source_id not in _OFFICIAL_NWP_SOURCES or source_id not in SOURCE_UNIVERSE:
        reasons.append("official_nwp_daily_peak_source_invalid")
    if not station_id or not event_date or peak is None or not peak_valid_time or _time(available_at) is None:
        reasons.append("official_nwp_daily_peak_identity_or_value_missing")
    if value.get("strict_asof_tier_a") is not True or value.get("response_headers_observed") is not True:
        reasons.append("official_nwp_daily_peak_not_all_points_tier_a")
    if not raw_sha256 or not raw_sha256s or not raw_payload_sha256s or not raw_refs or not snapshot_sha256s:
        reasons.append("official_nwp_daily_peak_raw_lineage_missing")
    if len(expected) < 2 or len(curve) != len(expected) or len(raw_sha256s) != len(expected) or len(raw_payload_sha256s) != len(expected) or any(not row or any(not _text(item) for item in row) for row in raw_payload_sha256s) or len(raw_refs) != len(expected) or len(snapshot_sha256s) != len(expected):
        reasons.append("official_nwp_daily_peak_complete_curve_lineage_invalid")
    if not _text(revision_ref):
        reasons.append("official_nwp_daily_peak_revision_ref_missing")
    curve_valid_times = [_text(point.get("valid_time_utc")) for point in curve]
    curve_temperatures = [_finite(point.get("temperature_c")) for point in curve]
    if curve_valid_times != expected or any(item is None for item in curve_temperatures):
        reasons.append("official_nwp_daily_peak_hourly_curve_invalid")
    elif peak is not None and peak_valid_time in curve_valid_times:
        peak_at_time = curve_temperatures[curve_valid_times.index(peak_valid_time)]
        if peak_at_time is None or abs(peak_at_time - peak) > 1e-12 or abs(max(item for item in curve_temperatures if item is not None) - peak) > 1e-12:
            reasons.append("official_nwp_daily_peak_peak_curve_binding_invalid")
    else:
        reasons.append("official_nwp_daily_peak_peak_time_not_in_curve")
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
        valid_time=peak_valid_time,
        valid_time_local=peak_valid_time,
        fetched_at=available_at,
        parsed_at=available_at,
        response_completed_at=available_at,
        forecast_peak_c=peak,
        raw_ref=_text(revision_ref),
        raw_sha256_override=raw_sha256,
        parser_version=PARSER_VERSION,
        quality_flags=(
            "strict_asof_tier_a",
            "observed_transport",
            "response_headers_observed",
            "raw_provider_response_bound",
            "official_nwp_complete_daily_curve",
        ),
        payload={
            "provider_source_id": source_id,
            "weak_source_id": source_id,
            "evidence_tier": TIER_A,
            "first_system_seen_at": available_at,
            "available_at": available_at,
            "forecast_snapshot_id": raw_sha256,
            "raw_response_sha256": raw_sha256,
            "forecast_value_kind": "peak_point",
            "forecast_peak_valid_time": peak_valid_time,
            "forecast_record_valid_time": "",
            "trajectory_eligible": True,
            "capture_time_provenance": "observed_transport",
            "response_headers_observed": True,
            "future_training_proposal_only": True,
            "official_nwp_daily_curve_complete": True,
            "official_nwp_raw_sha256s": raw_sha256s,
            "official_nwp_raw_payload_sha256s": raw_payload_sha256s,
            "official_nwp_raw_object_refs": raw_refs,
            "official_nwp_snapshot_sha256s": snapshot_sha256s,
            "official_nwp_run_at_utc": _text(value.get("run_at_utc")),
            "physical_regime": _mapping(value.get("physical_regime")),
            "forecast_weather_regime": _text(value.get("forecast_weather_regime")) or "unknown_forecast_regime",
            "forecast_weather_regime_origin": _text(value.get("forecast_weather_regime_origin")),
            "basin_regime": _text(value.get("basin_regime")) or "unknown",
            "basin_regime_confidence": _finite(value.get("basin_regime_confidence")),
            "basin_regime_source": _text(value.get("basin_regime_source")),
            "temperature_curve_weather_type_inference_forbidden": True,
        },
    ).as_dict()
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "proposal_ready",
        "source_observation": observation,
        "boundary": {
            "tier_a_only": True,
            "complete_official_curve_required": True,
            "append_only_p1_proposal_not_p1_write": True,
            "writes_archive_db": False,
            "creates_posterior": False,
            "places_orders": False,
        },
        "read_only": True,
    }


__all__ = ["PARSER_VERSION", "SCHEMA_VERSION", "build_official_nwp_daily_peak_tier_a_proposal"]
