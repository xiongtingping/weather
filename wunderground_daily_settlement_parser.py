"""Fail-closed parser for immutable ZUUU historical observations.

Only the station-bound historical JSON payload may produce S.  Forecast arrays
embedded in rendered Wunderground HTML are deliberately outside this parser.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlsplit
from zoneinfo import ZoneInfo


SCHEMA_VERSION = "wunderground_station_history_observation_parser_v2"
STATION_TZ = ZoneInfo("Asia/Shanghai")
APPROVED_PATH = "/v1/location/ZUUU%3A9%3ACN/observations/historical.json"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _blocked(*reasons: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "ok": False,
        "state": "blocked",
        "block_reasons": sorted(set(reason for reason in reasons if reason)),
        "read_only": True,
    }


def _request_contract_ok(source_url: str, event_date: str) -> bool:
    parsed = urlsplit(_text(source_url))
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    key = event_date.replace("-", "")
    return bool(
        parsed.scheme == "https"
        and parsed.netloc == "api.weather.com"
        and parsed.path == APPROVED_PATH
        and query.get("apiKey") == "REDACTED"
        and query.get("units") == "m"
        and query.get("startDate") == key
        and query.get("endDate") == key
    )


def parse_wunderground_daily_high(
    raw_payload: bytes | str,
    *,
    event_date: str,
    source_url: str = "",
) -> dict[str, Any]:
    """Compute one target-local-day high from metric historical observations."""

    day = _text(event_date)[:10]
    try:
        datetime.fromisoformat(day)
    except ValueError:
        return _blocked("event_date_invalid")
    if not _request_contract_ok(source_url, day):
        return _blocked("station_history_request_contract_invalid_or_unit_unbound")
    raw_text = raw_payload.decode("utf-8", errors="strict") if isinstance(raw_payload, bytes) else _text(raw_payload)
    try:
        document = json.loads(raw_text)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _blocked("station_history_json_invalid")
    observations = document.get("observations") if isinstance(document, Mapping) else None
    if not isinstance(observations, list):
        return _blocked("station_history_observations_missing")

    candidates: list[dict[str, Any]] = []
    for index, value in enumerate(observations):
        if not isinstance(value, Mapping):
            continue
        timestamp = _finite(value.get("valid_time_gmt"))
        temperature = _finite(value.get("temp"))
        if timestamp is None or temperature is None:
            continue
        try:
            valid_utc = datetime.fromtimestamp(timestamp, timezone.utc)
        except (OSError, OverflowError, ValueError):
            continue
        valid_local = valid_utc.astimezone(STATION_TZ)
        if valid_local.date().isoformat() != day:
            continue
        candidates.append(
            {
                "index": index,
                "valid_time_gmt": int(timestamp),
                "temperature_c": temperature,
                "valid_time_utc": valid_utc.isoformat().replace("+00:00", "Z"),
                "valid_time_local": valid_local.isoformat(),
            }
        )
    if not candidates:
        return _blocked("target_local_day_historical_observations_missing")

    maximum = max(item["temperature_c"] for item in candidates)
    peaks = [item for item in candidates if item["temperature_c"] == maximum]
    marker = f'"valid_time_gmt":{peaks[0]["valid_time_gmt"]}'
    marker_at = raw_text.find(marker)
    start = raw_text.rfind("{", 0, marker_at) if marker_at >= 0 else -1
    end = raw_text.find("}", marker_at) if marker_at >= 0 else -1
    evidence = raw_text[start : end + 1] if start >= 0 and end > start else ""
    try:
        evidence_row = json.loads(evidence)
    except json.JSONDecodeError:
        evidence_row = {}
    if _finite(evidence_row.get("temp")) != maximum:
        return _blocked("peak_observation_literal_evidence_missing")
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "settlement_scalar_candidate_ready",
        "event_date": day,
        "daily_high_c": maximum,
        "source_value": maximum,
        "source_unit": "C",
        "field": "observations[].temp",
        "date_key": "observations[].valid_time_gmt@Asia/Shanghai",
        "observation_count": len(candidates),
        "peak_observations": peaks,
        "extractor_version": SCHEMA_VERSION,
        "extraction_evidence": evidence,
        "read_only": True,
        "writes_archive_db": False,
        "places_orders": False,
    }


__all__ = ["SCHEMA_VERSION", "parse_wunderground_daily_high"]
