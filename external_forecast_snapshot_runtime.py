"""Immutable external forecast response envelopes for research-only skill data.

This module is intentionally independent from archive.db, facts.sqlite, model
parameters, posterior facts, strategy signals, and order execution.  It only
normalizes already-fetched response bytes and validates their causal lineage.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


SCHEMA_VERSION = "external_forecast_raw_envelope_v1"
CONTRACT_SCHEMA_VERSION = "external_forecast_raw_envelope_contract_v1"
CAPTURE_TIME_PROVENANCE_VALUES = frozenset(
    {
        "observed_transport",
        "batch_clock_proxy",
        "historical_declared",
        "unknown",
    }
)

SENSITIVE_KEYS = {
    "access_token",
    "api-key",
    "api_key",
    "apikey",
    "authorization",
    "key",
    "password",
    "secret",
    "token",
}
_SENSITIVE_RAW_RESPONSE_PATTERN = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|authorization|password|secret|token)\s*(?:\"\s*)?[:=]"
)

PROVIDER_FAMILY_BY_SOURCE_ID = {
    "ecmwf_model_peak": "nwp_ecmwf",
    "gfs_model_peak": "nwp_gfs",
    "icon_model_peak": "nwp_icon",
    "meteoblue_model_peak": "model_vendor_meteoblue",
    "zuuu_taf_forecast_amsc": "taf_zuuu",
    "zuuu_taf_forecast_peak": "taf_zuuu",
    "wu_forecast_peak": "twc_provider",
    "weather_com_forecast_peak": "twc_provider",
    "twc_forecast_peak": "twc_provider",
    "twc_forecast_hourly_peak": "twc_provider",
    "polymarket_gamma_settlement": "market_settlement",
}


def _guards() -> dict[str, bool]:
    return {
        "writes_archive_db": False,
        "writes_facts_sqlite": False,
        "writes_strategy_signal": False,
        "writes_auto_trade_decisions": False,
        "places_orders": False,
        "changes_parameters": False,
        "restarts_launchd": False,
        "historical_backfill": False,
    }


def _parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def redact_request_url(value: Any) -> str:
    """Remove credential-bearing query values without changing request identity."""

    text = str(value or "").strip()
    if not text:
        return ""
    parts = urlsplit(text)
    query = []
    for key, raw_value in parse_qsl(parts.query, keep_blank_values=True):
        query.append((key, "[REDACTED]" if key.strip().lower() in SENSITIVE_KEYS else raw_value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def redact_request_parameters(value: Mapping[str, Any] | None) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, raw_value in (value or {}).items():
        output[str(key)] = "[REDACTED]" if str(key).strip().lower() in SENSITIVE_KEYS else raw_value
    return output


def default_provider_family(source_id: Any) -> str:
    source = str(source_id or "").strip()
    if source in PROVIDER_FAMILY_BY_SOURCE_ID:
        return PROVIDER_FAMILY_BY_SOURCE_ID[source]
    lowered = source.lower()
    if "ecmwf" in lowered:
        return "nwp_ecmwf"
    if "gfs" in lowered:
        return "nwp_gfs"
    if "icon" in lowered:
        return "nwp_icon"
    if "taf" in lowered:
        return "taf_zuuu"
    if any(token in lowered for token in ("wunderground", "weather_com", "twc", "wu_")):
        return "twc_provider"
    return "other_forecast"


def envelope_as_of_eligible_at(envelope: Mapping[str, Any]) -> str | None:
    completed = _parse_datetime(envelope.get("response_completed_at"))
    provider_available = _parse_datetime(envelope.get("provider_available_at"))
    if str(envelope.get("availability_mode") or "") == "historical_archive_replay":
        return _iso(provider_available) if provider_available is not None else None
    values = [value for value in (completed, provider_available) if value is not None]
    return _iso(max(values)) if values else None


def build_external_forecast_raw_envelope(
    *,
    source_id: str,
    response_body_utf8: str,
    requested_at: Any,
    response_started_at: Any,
    response_completed_at: Any,
    http_status: int,
    parser_version: str,
    provider_family: str = "",
    provider_available_at: Any = None,
    request_url: str = "",
    request_parameters: Mapping[str, Any] | None = None,
    source_event_id: str = "",
    availability_mode: str = "live_fetch",
    provider_available_at_quality: str = "",
    capture_time_provenance: str = "unknown",
    response_headers_observed: bool = False,
) -> dict[str, Any]:
    body = str(response_body_utf8)
    body_bytes = body.encode("utf-8")
    envelope = {
        "schema": SCHEMA_VERSION,
        "source_id": str(source_id or "").strip(),
        "provider_family": str(provider_family or "").strip() or default_provider_family(source_id),
        "source_event_id": str(source_event_id or "").strip(),
        "request_url_redacted": redact_request_url(request_url),
        "request_parameters": redact_request_parameters(request_parameters),
        "requested_at": str(requested_at or ""),
        "response_started_at": str(response_started_at or ""),
        "response_completed_at": str(response_completed_at or ""),
        "provider_available_at": str(provider_available_at or "") or None,
        "provider_available_at_quality": str(provider_available_at_quality or ""),
        "capture_time_provenance": str(capture_time_provenance or "unknown"),
        # Tier-A means the capture process observed the point at which the
        # provider response became available (headers), not merely the local
        # clock just before issuing a request.
        "response_headers_observed": response_headers_observed is True,
        "availability_mode": str(availability_mode or "live_fetch"),
        "http_status": int(http_status),
        "response_sha256": hashlib.sha256(body_bytes).hexdigest(),
        "response_size_bytes": len(body_bytes),
        "response_body_utf8": body,
        "credential_fields_persisted": False,
        "parser_version": str(parser_version or "").strip(),
        "read_only_normalization": True,
        "guards": _guards(),
    }
    envelope["as_of_eligible_at"] = envelope_as_of_eligible_at(envelope)
    return envelope


def validate_external_forecast_raw_envelope(value: Mapping[str, Any] | None) -> dict[str, Any]:
    envelope = value if isinstance(value, Mapping) else {}
    issues: list[str] = []
    if envelope.get("schema") != SCHEMA_VERSION:
        issues.append("external_forecast_envelope_schema_invalid")
    for field in ("source_id", "provider_family", "parser_version", "response_sha256"):
        if not str(envelope.get(field) or "").strip():
            issues.append(f"{field}_missing")
    requested = _parse_datetime(envelope.get("requested_at"))
    started = _parse_datetime(envelope.get("response_started_at"))
    completed = _parse_datetime(envelope.get("response_completed_at"))
    provider_available = _parse_datetime(envelope.get("provider_available_at"))
    if requested is None:
        issues.append("requested_at_invalid")
    if started is None:
        issues.append("response_started_at_invalid")
    if completed is None:
        issues.append("response_completed_at_invalid")
    if requested and started and started < requested:
        issues.append("response_started_before_request")
    if started and completed and completed < started:
        issues.append("response_completed_before_start")
    body = envelope.get("response_body_utf8")
    if not isinstance(body, str):
        issues.append("response_body_utf8_missing")
    else:
        expected_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
        if expected_sha != str(envelope.get("response_sha256") or ""):
            issues.append("response_sha256_mismatch")
        if int(envelope.get("response_size_bytes") or -1) != len(body.encode("utf-8")):
            issues.append("response_size_bytes_mismatch")
        # The v2 forward-metrics wrapper stores a provider body beside its
        # derived peak. Reject it rather than redact it if it resembles a
        # credential payload; otherwise its claimed raw hash would be false.
        try:
            wrapper = json.loads(body)
        except (TypeError, ValueError):
            wrapper = {}
        if isinstance(wrapper, Mapping) and wrapper.get("schema") == "forecast_source_forward_capture_payload_v2":
            raw_provider_response = str(wrapper.get("raw_provider_response_utf8") or "")
            if raw_provider_response and _SENSITIVE_RAW_RESPONSE_PATTERN.search(raw_provider_response):
                issues.append("raw_provider_response_contains_credential_shaped_field")
    if envelope.get("credential_fields_persisted") is not False:
        issues.append("credential_fields_persisted_must_be_false")
    availability_mode = str(envelope.get("availability_mode") or "live_fetch")
    if availability_mode not in {"live_fetch", "historical_archive_replay"}:
        issues.append("availability_mode_invalid")
    if availability_mode == "historical_archive_replay":
        if provider_available is None:
            issues.append("historical_provider_available_at_missing")
        if not str(envelope.get("provider_available_at_quality") or ""):
            issues.append("historical_provider_available_at_quality_missing")
    if str(envelope.get("capture_time_provenance") or "unknown") not in CAPTURE_TIME_PROVENANCE_VALUES:
        issues.append("capture_time_provenance_invalid")
    # Pre-header-capture immutable envelopes remain valid historical evidence
    # (normally Tier B or C).  Absence is never interpreted as observed, and
    # the stricter Tier-A classifier independently requires literal `True`.
    if "response_headers_observed" in envelope and not isinstance(envelope.get("response_headers_observed"), bool):
        issues.append("response_headers_observed_not_boolean")
    url = str(envelope.get("request_url_redacted") or "")
    for key, raw_value in parse_qsl(urlsplit(url).query, keep_blank_values=True):
        if key.strip().lower() in SENSITIVE_KEYS and raw_value != "[REDACTED]":
            issues.append("credential_value_present_in_request_url")
    parameters = envelope.get("request_parameters") if isinstance(envelope.get("request_parameters"), Mapping) else {}
    for key, raw_value in parameters.items():
        if str(key).strip().lower() in SENSITIVE_KEYS and raw_value != "[REDACTED]":
            issues.append("credential_value_present_in_request_parameters")
    guards = envelope.get("guards") if isinstance(envelope.get("guards"), Mapping) else {}
    if any(bool(value) for value in guards.values()):
        issues.append("external_forecast_envelope_guard_enabled")
    return {
        "schema": CONTRACT_SCHEMA_VERSION,
        "ok": not issues,
        "issues": sorted(set(issues)),
        "source_id": str(envelope.get("source_id") or ""),
        "provider_family": str(envelope.get("provider_family") or ""),
        "as_of_eligible_at": envelope_as_of_eligible_at(envelope),
        "response_sha256": str(envelope.get("response_sha256") or ""),
        "guards": _guards(),
    }


def canonical_envelope_json(envelope: Mapping[str, Any]) -> str:
    """Return deterministic JSON suitable for gzip storage and hash-based replay."""

    return json.dumps(dict(envelope), ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


__all__ = [
    "CONTRACT_SCHEMA_VERSION",
    "CAPTURE_TIME_PROVENANCE_VALUES",
    "PROVIDER_FAMILY_BY_SOURCE_ID",
    "SCHEMA_VERSION",
    "build_external_forecast_raw_envelope",
    "canonical_envelope_json",
    "default_provider_family",
    "envelope_as_of_eligible_at",
    "redact_request_parameters",
    "redact_request_url",
    "validate_external_forecast_raw_envelope",
]
