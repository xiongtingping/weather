"""Evidence-tier gate for strict-as-of forecast training captures.

An immutable response file alone is not proof that the system could have seen
it at a historical cutoff.  This module keeps live observed transport captures
separate from historical provider replay and from unverifiable derived files.
It is a pure classifier: no archive, posterior, model artifact, or order is
ever written here.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping

from external_forecast_snapshot_runtime import validate_external_forecast_raw_envelope


SCHEMA_VERSION = "strict_asof_forecast_capture_v1"
MANIFEST_SCHEMA_VERSION = "strict_asof_forecast_capture_manifest_v1"
TIER_A = "A_live_observed_transport"
TIER_B = "B_historical_declared_availability"
TIER_C = "C_unverifiable_or_incomplete"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _capture_id(value: Mapping[str, Any]) -> str:
    return "fcap_" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()[:24]


def classify_strict_asof_forecast_capture(
    *,
    raw_envelope: Mapping[str, Any],
    normalized_forecast: Mapping[str, Any],
) -> dict[str, Any]:
    """Classify one forecast revision by causal evidence quality.

    Tier A is the only tier allowed to contribute a strict-as-of training
    event.  Tier B retains declared provider timing for historical coverage
    analysis but cannot be promoted to system-first-seen evidence.
    """

    envelope = _mapping(raw_envelope)
    forecast = _mapping(normalized_forecast)
    contract = validate_external_forecast_raw_envelope(envelope)
    reasons: list[str] = []
    source_id = _text(forecast.get("source_id") or envelope.get("source_id"))
    event_date = _text(forecast.get("target_event_date_local") or forecast.get("event_date"))[:10]
    response_sha256 = _text(envelope.get("response_sha256"))
    if contract.get("ok") is not True:
        reasons.extend(f"raw_envelope:{reason}" for reason in contract.get("issues") or [])
    if not source_id:
        reasons.append("forecast_capture_source_id_missing")
    if not event_date:
        reasons.append("forecast_capture_target_event_date_missing")
    if _text(forecast.get("forecast_snapshot_id")) != response_sha256:
        reasons.append("forecast_capture_snapshot_hash_not_bound_to_raw_envelope")
    if forecast.get("forecast_peak_c") is None:
        reasons.append("forecast_capture_peak_missing")
    provenance = _text(envelope.get("capture_time_provenance") or "unknown")
    availability_mode = _text(envelope.get("availability_mode") or "live_fetch")
    raw_provider_response_present = bool(forecast.get("raw_provider_response_present"))
    # The envelope is the immutable transport fact.  A derived normalization
    # must never be able to upgrade a capture merely by setting a boolean.
    response_headers_observed = envelope.get("response_headers_observed") is True
    if (
        "response_headers_observed" in forecast
        and forecast.get("response_headers_observed") is not response_headers_observed
    ):
        reasons.append("forecast_capture_response_headers_binding_mismatch")
    source_matches = _text(envelope.get("source_id")) == source_id
    if not source_matches:
        reasons.append("forecast_capture_source_id_raw_envelope_mismatch")
    common_ok = not reasons
    if (
        common_ok
        and availability_mode == "live_fetch"
        and provenance == "observed_transport"
        and raw_provider_response_present
        and response_headers_observed
    ):
        tier = TIER_A
        first_system_seen_at = _text(envelope.get("response_completed_at"))
        available_at = first_system_seen_at
        training_eligible = True
        classification_reasons: list[str] = []
    elif (
        common_ok
        and availability_mode == "historical_archive_replay"
        and _text(envelope.get("provider_available_at"))
        and _text(envelope.get("provider_available_at_quality"))
    ):
        tier = TIER_B
        first_system_seen_at = _text(envelope.get("response_completed_at"))
        available_at = _text(envelope.get("provider_available_at"))
        training_eligible = False
        classification_reasons = ["historical_retrieval_cannot_prove_system_first_seen_at_historical_cutoff"]
    else:
        tier = TIER_C
        first_system_seen_at = _text(envelope.get("response_completed_at"))
        available_at = ""
        training_eligible = False
        classification_reasons = []
        if common_ok:
            if availability_mode == "live_fetch" and provenance != "observed_transport":
                classification_reasons.append("live_capture_transport_time_not_observed")
            if availability_mode == "live_fetch" and not raw_provider_response_present:
                classification_reasons.append("live_capture_raw_provider_response_missing")
            if availability_mode == "live_fetch" and not response_headers_observed:
                classification_reasons.append("live_capture_response_headers_not_observed")
            if availability_mode not in {"live_fetch", "historical_archive_replay"}:
                classification_reasons.append("forecast_capture_availability_mode_unsupported")
    identity = {
        "source_id": source_id,
        "event_date": event_date,
        "response_sha256": response_sha256,
        "forecast_snapshot_id": _text(forecast.get("forecast_snapshot_id")),
        "tier": tier,
    }
    return {
        "schema": SCHEMA_VERSION,
        "ok": tier != TIER_C,
        "state": "strict_training_eligible" if training_eligible else "auxiliary_only" if tier == TIER_B else "blocked",
        "forecast_capture_id": _capture_id(identity),
        "source_id": source_id,
        "event_date": event_date,
        "source_revision_id": _text(forecast.get("source_event_id") or envelope.get("source_event_id")),
        "source_observation_id": _text(forecast.get("source_observation_id")),
        "forecast_snapshot_id": _text(forecast.get("forecast_snapshot_id")),
        "raw_response_sha256": response_sha256,
        "first_system_seen_at": first_system_seen_at,
        "available_at": available_at,
        "provider_declared_available_at": _text(envelope.get("provider_available_at")),
        "evidence_tier": tier,
        "strict_training_eligible": training_eligible,
        "allowed_uses": (
            ["P3_strict_asof_event_training", "P4_P6_OOS_candidate"]
            if tier == TIER_A
            else ["historical_coverage_diagnostic", "prior_or_path_auxiliary"]
            if tier == TIER_B
            else ["audit_only"]
        ),
        "reasons": sorted(set(reasons + classification_reasons)),
        "boundary": {
            "system_first_seen_not_backdated_from_provider_time": True,
            "historical_replay_never_promoted_to_tier_a": True,
            "raw_provider_response_required_for_tier_a": True,
            "response_headers_observed_required_for_tier_a": True,
            "writes_archive_db": False,
            "creates_model_artifact": False,
            "creates_posterior": False,
            "places_orders": False,
        },
        "read_only": True,
    }


def build_strict_asof_forecast_capture_manifest(
    captures: Iterable[Mapping[str, Any]],
    *,
    required_source_ids: Iterable[str],
    minimum_complete_event_days: int = 365,
) -> dict[str, Any]:
    """Summarize tiered capture coverage without promoting any evidence.

    The safe default is a full annual prospective sample.  Callers that only
    audit whether the collector contract has started must explicitly request a
    smaller threshold; a one-day green result is never the implicit training
    default.
    """

    rows = [dict(_mapping(row)) for row in captures]
    required = sorted({_text(source) for source in required_source_ids if _text(source)})
    by_event: dict[str, set[str]] = {}
    counts = {TIER_A: 0, TIER_B: 0, TIER_C: 0}
    for row in rows:
        tier = _text(row.get("evidence_tier"))
        if tier in counts:
            counts[tier] += 1
        if row.get("strict_training_eligible") is True:
            by_event.setdefault(_text(row.get("event_date")), set()).add(_text(row.get("source_id")))
    complete_days = sorted(
        event_date
        for event_date, sources in by_event.items()
        if event_date and all(source in sources for source in required)
    )
    minimum_days = max(1, int(minimum_complete_event_days))
    capture_contract_ready = bool(complete_days and required)
    return {
        "schema": MANIFEST_SCHEMA_VERSION,
        "capture_count": len(rows),
        "required_source_ids": required,
        "tier_counts": counts,
        "tier_a_source_ids_by_event_date": {event: sorted(sources) for event, sources in sorted(by_event.items())},
        "strict_training_complete_event_dates": complete_days,
        "strict_training_complete_event_day_count": len(complete_days),
        "capture_contract_ready": capture_contract_ready,
        "minimum_complete_event_days": minimum_days,
        "strict_training_ready": bool(capture_contract_ready and len(complete_days) >= minimum_days),
        "boundary": {
            "tier_b_not_counted_as_strict_training_coverage": True,
            "tier_c_not_counted_as_strict_training_coverage": True,
            "read_only": True,
        },
    }


__all__ = [
    "MANIFEST_SCHEMA_VERSION",
    "SCHEMA_VERSION",
    "TIER_A",
    "TIER_B",
    "TIER_C",
    "build_strict_asof_forecast_capture_manifest",
    "classify_strict_asof_forecast_capture",
]
