"""Read-only AMSC TAF raw-object to strict-as-of forecast-capture bridge.

TAF is a weak forecast source, never a strong observation.  A raw AMSC object
is insufficient by itself: this adapter requires the matching *first* poll
manifest to prove request/start/complete transport timing and refuses a TAF
whose system-first-seen time is already after its forecast peak.  It produces
only a future training proposal; no archive, P1, posterior, or order is
written here.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any, Mapping

from amsc_raw_history_archive import (
    POLL_MANIFEST_SCHEMA,
    RAW_OBJECT_SCHEMA,
    raw_text_sha256,
    validate_amsc_raw_poll_manifest,
)
from external_forecast_snapshot_runtime import build_external_forecast_raw_envelope
from scripts.fetch_zuuu_metar_taf import source_payloads_from_amsc_payload
from scripts.forecast_source_forward_snapshot import write_forward_snapshot
from strict_asof_forecast_capture_runtime import TIER_A, TIER_C, classify_strict_asof_forecast_capture


SCHEMA_VERSION = "amsc_taf_strict_asof_capture_proposal_v1"
SOURCE_ID = "zuuu_taf_forecast_amsc"
STATION_ID = "ZUUU"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _parse_datetime(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _response_status_ok(value: Any) -> bool:
    try:
        return int(value) == 200
    except (TypeError, ValueError):
        return False


def _matching_manifest_raw_row(
    manifest: Mapping[str, Any],
    *,
    raw_sha256: str,
    raw_object_ref: str,
) -> dict[str, Any] | None:
    rows = [
        _mapping(row)
        for row in manifest.get("raw_objects") or []
        if _text(_mapping(row).get("raw_sha256")) == raw_sha256
    ]
    if len(rows) != 1:
        return None
    row = rows[0]
    return row if _text(row.get("raw_object_ref")) == raw_object_ref else None


def _parse_taf_forecast(raw_text: str, *, first_seen_at: str) -> dict[str, Any] | None:
    """Use the same TAF parser as acquisition without opening any transport."""

    payload = {"code": 200, "data": [{"cccc": STATION_ID, "content": raw_text}]}
    candidates = source_payloads_from_amsc_payload(payload, fetched_at=first_seen_at)
    taf = next((row for row in candidates if _text(row.get("source_id")) == SOURCE_ID), None)
    return dict(taf) if isinstance(taf, Mapping) else None


def _blocked(*, reasons: list[str], raw_envelope: Mapping[str, Any] | None = None, normalized_forecast: Mapping[str, Any] | None = None, capture: Mapping[str, Any] | None = None) -> dict[str, Any]:
    evidence = _mapping(capture)
    if evidence:
        evidence = {
            **evidence,
            "ok": False,
            "state": "blocked",
            "evidence_tier": TIER_C,
            "strict_training_eligible": False,
            "allowed_uses": ["audit_only"],
            "reasons": sorted(set(list(evidence.get("reasons") or []) + reasons)),
        }
    return {
        "schema": SCHEMA_VERSION,
        "ok": False,
        "state": "blocked",
        "block_reasons": sorted(set(reasons)),
        "raw_envelope": dict(raw_envelope or {}),
        "normalized_forecast": dict(normalized_forecast or {}),
        "capture": evidence,
        "boundary": {
            "taf_is_weak_forecast_only": True,
            "requires_first_poll_transport_manifest": True,
            "requires_first_seen_before_forecast_peak": True,
            "writes_archive_db": False,
            "writes_research_files": False,
            "creates_p1_forecast_revision": False,
            "creates_posterior": False,
            "places_orders": False,
        },
        "read_only": True,
    }


def build_amsc_taf_strict_asof_capture_proposal(
    *,
    raw_object: Mapping[str, Any],
    raw_object_ref: str,
    first_poll_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Build one strict TAF capture proposal from a raw object and first receipt.

    The caller must select the manifest whose completion time equals the raw
    object's immutable ``first_seen_at``.  A later re-fetch cannot be supplied
    as an alternate receipt to make an old forecast look newly available.
    """

    raw = _mapping(raw_object)
    manifest = _mapping(first_poll_manifest)
    reasons: list[str] = []
    raw_sha256 = _text(raw.get("raw_sha256"))
    raw_text = _text(raw.get("raw_text"))
    first_seen_at = _text(raw.get("first_seen_at"))
    if raw.get("schema") != RAW_OBJECT_SCHEMA:
        reasons.append("amsc_taf_raw_object_schema_invalid")
    if _text(raw.get("source_id")) != SOURCE_ID:
        reasons.append("amsc_taf_raw_object_source_invalid")
    if _text(raw.get("station_id")).upper() != STATION_ID:
        reasons.append("amsc_taf_raw_object_station_invalid")
    if _text(raw.get("report_type")).upper() != "TAF":
        reasons.append("amsc_taf_raw_object_report_type_invalid")
    if not raw_sha256 or raw_text_sha256(raw_text) != raw_sha256:
        reasons.append("amsc_taf_raw_object_hash_invalid")
    if _parse_datetime(first_seen_at) is None:
        reasons.append("amsc_taf_raw_object_first_seen_invalid")
    if not _text(raw_object_ref):
        reasons.append("amsc_taf_raw_object_ref_missing")

    manifest_contract = validate_amsc_raw_poll_manifest(manifest)
    if manifest.get("schema") != POLL_MANIFEST_SCHEMA:
        reasons.append("amsc_taf_first_manifest_schema_invalid")
    if manifest_contract.get("ok") is not True:
        reasons.extend("amsc_taf_first_manifest:" + reason for reason in manifest_contract.get("issues") or [])
    if manifest_contract.get("strict_transport_observed") is not True:
        reasons.append("amsc_taf_first_manifest_response_start_not_observed")
    if manifest.get("response_headers_observed") is not True:
        reasons.append("amsc_taf_first_manifest_response_headers_not_observed")
    if not _response_status_ok(manifest.get("response_status")):
        reasons.append("amsc_taf_first_manifest_response_status_not_ok")
    if raw_sha256 not in {str(value or "") for value in manifest.get("returned_raw_sha256_list") or []}:
        reasons.append("amsc_taf_first_manifest_raw_sha_not_returned")
    manifest_raw = _matching_manifest_raw_row(
        manifest,
        raw_sha256=raw_sha256,
        raw_object_ref=_text(raw_object_ref),
    )
    if manifest_raw is None:
        reasons.append("amsc_taf_first_manifest_raw_object_binding_invalid")
    else:
        if _text(manifest_raw.get("first_seen_at")) != first_seen_at:
            reasons.append("amsc_taf_first_manifest_first_seen_mismatch")
        if _text(manifest_raw.get("arrival_time_quality")) != "observed_first_seen":
            reasons.append("amsc_taf_first_manifest_arrival_quality_invalid")
    if _text(manifest.get("response_completed_at")) != first_seen_at:
        reasons.append("amsc_taf_first_manifest_not_object_first_arrival")
    if reasons:
        return _blocked(reasons=reasons)

    taf = _parse_taf_forecast(raw_text, first_seen_at=first_seen_at)
    if taf is None:
        return _blocked(reasons=["amsc_taf_raw_text_parse_failed"])
    peak = taf.get("forecast_peak_c")
    peak_at = _text(taf.get("valid_time_utc"))
    event_date = _text(taf.get("event_date"))
    first_seen_dt = _parse_datetime(first_seen_at)
    peak_dt = _parse_datetime(peak_at)
    if peak is None or peak_dt is None or not event_date:
        return _blocked(reasons=["amsc_taf_parsed_forecast_peak_or_target_missing"])
    if first_seen_dt is None or first_seen_dt > peak_dt:
        return _blocked(reasons=["amsc_taf_first_seen_after_forecast_peak"])

    # Keep the raw provider TAF untouched beside a derived forecast descriptor,
    # just as the model/WU/TWC forward collector does.  The outer envelope hash
    # identifies the immutable capture wrapper; ``raw_object_sha256`` preserves
    # the original AMSC object identity for receipt verification.
    normalized_forecast = {
        "schema": "amsc_taf_forward_metrics_v1",
        "source_id": SOURCE_ID,
        "station_id": STATION_ID,
        "target_event_date_local": event_date,
        "forecast_peak_c": float(peak),
        "forecast_peak_valid_time": peak_at,
        "valid_time": peak_at,
        "forecast_snapshot_id": "",
        "source_event_id": "amsc_taf_" + raw_sha256,
        "raw_provider_response_present": True,
        "raw_object_ref": _text(raw_object_ref),
        "raw_object_sha256": raw_sha256,
        "first_system_seen_at": first_seen_at,
        "issue_time_utc": _text(taf.get("issue_time_utc")),
    }
    envelope_body = json.dumps(
        {
            "schema": "forecast_source_forward_capture_payload_v2",
            "derived_forecast": normalized_forecast,
            "raw_provider_response_utf8": raw_text,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    raw_envelope = build_external_forecast_raw_envelope(
        source_id=SOURCE_ID,
        response_body_utf8=envelope_body,
        requested_at=_text(manifest.get("poll_started_at")),
        response_started_at=_text(manifest.get("response_started_at")),
        response_completed_at=_text(manifest.get("response_completed_at")),
        provider_available_at=first_seen_at,
        http_status=200,
        parser_version=SCHEMA_VERSION,
        request_url="amsc://" + _text(manifest.get("endpoint_id")),
        request_parameters=_mapping(manifest.get("sanitized_query")),
        source_event_id="amsc_taf_" + raw_sha256,
        availability_mode="live_fetch",
        capture_time_provenance="observed_transport",
        response_headers_observed=manifest.get("response_headers_observed") is True,
    )
    normalized_forecast["forecast_snapshot_id"] = _text(raw_envelope.get("response_sha256"))
    capture = classify_strict_asof_forecast_capture(
        raw_envelope=raw_envelope,
        normalized_forecast=normalized_forecast,
    )
    if capture.get("evidence_tier") != TIER_A or capture.get("strict_training_eligible") is not True:
        return _blocked(
            reasons=["amsc_taf_strict_capture_not_tier_a"],
            raw_envelope=raw_envelope,
            normalized_forecast=normalized_forecast,
            capture=capture,
        )
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "strict_training_eligible",
        "raw_envelope": raw_envelope,
        "normalized_forecast": normalized_forecast,
        "capture": capture,
        "boundary": {
            "taf_is_weak_forecast_only": True,
            "requires_first_poll_transport_manifest": True,
            "requires_first_seen_before_forecast_peak": True,
            "writes_archive_db": False,
            "writes_research_files": False,
            "creates_p1_forecast_revision": False,
            "creates_posterior": False,
            "places_orders": False,
        },
        "read_only": True,
    }


def persist_amsc_taf_strict_asof_capture_proposal(
    proposal: Mapping[str, Any],
    *,
    output_root: Any = None,
    allow_research_write: bool = False,
) -> dict[str, Any]:
    """Persist an already-eligible TAF wrapper only under explicit research authority."""

    value = _mapping(proposal)
    if value.get("ok") is not True or _mapping(value.get("capture")).get("evidence_tier") != TIER_A:
        return {
            "schema": SCHEMA_VERSION,
            "ok": False,
            "state": "blocked",
            "state_reason": "amsc_taf_strict_capture_proposal_not_tier_a",
            "writes_research_files": False,
            "read_only": True,
        }
    kwargs = {"allow_research_write": allow_research_write}
    if output_root is not None:
        kwargs["output_root"] = output_root
    result = write_forward_snapshot(_mapping(value.get("raw_envelope")), **kwargs)
    return {
        "schema": SCHEMA_VERSION,
        "ok": result.get("ok") is True,
        "state": "persisted" if result.get("state") in {"written", "already_present"} else str(result.get("state") or "blocked"),
        "persistence": result,
        "writes_research_files": result.get("writes_research_files") is True,
        "boundary": {
            "requires_explicit_research_write": True,
            "writes_archive_db": False,
            "creates_p1_forecast_revision": False,
            "creates_posterior": False,
            "places_orders": False,
        },
    }


__all__ = [
    "SCHEMA_VERSION",
    "SOURCE_ID",
    "STATION_ID",
    "build_amsc_taf_strict_asof_capture_proposal",
    "persist_amsc_taf_strict_asof_capture_proposal",
]
