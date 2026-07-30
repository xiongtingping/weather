"""Strict prospective model-arrival evidence for the Bayes basket target.

Historical downloads are deliberately not upgraded into arrival evidence.  A
receipt is eligible only when the native provider's publication/issue time and
the workstation's completed-receive time were observed during a prospective
capture.  This module is a pure contract boundary: it has no network, SQLite,
model fitting, or order-gateway side effects.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .contracts import ContractError, MODEL_IDS, content_sha256
from .et_history import assert_event_day_started, event_day_window


SCHEMA = "bayes_basket_target_prospective_model_arrival_v1"
BLOCKED_SCHEMA = "bayes_basket_target_prospective_model_arrival_blocked_v1"
INDEX_SCHEMA = "bayes_basket_target_model_arrival_index_v2"
ALLOWED_PROVIDERS = {
    "ecmwf_ifs025_official": "ECMWF",
    "gfs_0p25_official": "GFS",
    "icon_global_official": "ICON",
}
REQUIRED_SOURCE_FLAGS = ("historical_arrival_time_observed", "aggregator_or_republisher", "open_meteo")
MAX_PRE_EVENT_CAPTURE_HOURS = 30
MAX_PRE_EVENT_RUN_LEAD_HOURS = 42
OFFICIAL_RUN_HOURS_UTC = frozenset({0, 6, 12, 18})


def _text(name: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"{name}_missing")
    return result


def _sha(name: str, value: Any) -> str:
    result = _text(name, value).lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(f"{name}_invalid_sha256")
    return result


def _timestamp(name: str, value: Any) -> datetime:
    raw = _text(name, value)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{name}_invalid_iso8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{name}_timezone_required")
    return parsed.astimezone(timezone.utc)


def _timestamp_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _date(name: str, value: Any) -> str:
    result = _text(name, value)
    try:
        datetime.strptime(result, "%Y-%m-%d")
    except ValueError as exc:
        raise ContractError(f"{name}_invalid_date") from exc
    return result


def _raw_hashes(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray, Mapping)):
        values = list(value)
    else:
        raise ContractError("raw_payload_sha256s_missing")
    hashes = tuple(sorted({_sha("raw_payload_sha256", item) for item in values}))
    if not hashes:
        raise ContractError("raw_payload_sha256s_missing")
    return hashes


def _target_date_matches_valid_time(target_local_date: str, valid_time_utc: datetime, timezone_name: str) -> None:
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ContractError("target_timezone_invalid") from exc
    if valid_time_utc.astimezone(zone).date().isoformat() != target_local_date:
        raise ContractError("model_valid_time_target_local_date_mismatch")


def validate_prospective_capture_window(
    *,
    target_local_date: str,
    target_timezone: str,
    run_initialization_utc: str,
    decision_at_utc: str,
) -> dict[str, str]:
    """Allow bounded d2/d1 and in-event d0 native-model capture.

    Native rolling trees must be sampled when the provider publishes the run,
    which can be before the forecast's ET target day begins.  The allowance is
    deliberately narrow: only canonical d2/d1 pre-event or d0 in-event
    maturities are accepted, the run must already exist, and a not-started
    target may be at most 30 hours ahead. Closed-day retrieval remains raw
    evidence with its actual receive time; strict-as-of consumers decide
    whether that late receipt is usable.
    """

    target = datetime.strptime(_date("target_local_date", target_local_date), "%Y-%m-%d").date()
    run = _timestamp("run_initialization_utc", run_initialization_utc)
    decision = _timestamp("decision_at_utc", decision_at_utc)
    if run > decision:
        raise ContractError("model_capture_run_is_in_future")
    if run.minute or run.second or run.microsecond or run.hour not in OFFICIAL_RUN_HOURS_UTC:
        raise ContractError("model_capture_run_not_official_cycle")
    timezone_name = _text("target_timezone", target_timezone)
    window = event_day_window(
        target.isoformat(),
        as_of_utc=decision,
        target_timezone=timezone_name,
    )
    target_start = _timestamp("target_day_start_utc", window["target_day_start_utc"])
    target_end = _timestamp("target_day_end_utc", window["target_day_end_utc"])
    if target_start <= run < target_end:
        maturity = "d0"
    elif run >= target_end:
        raise ContractError("model_run_after_target_day")
    elif run.date() == target - timedelta(days=1):
        maturity = "d2"
    elif run.date() == target - timedelta(days=2) and run.hour == 18:
        maturity = "d2"
    elif run.date() == target and run.hour == 0:
        maturity = "d1"
    elif 0.0 < (target_start - run).total_seconds() / 3600.0 <= MAX_PRE_EVENT_RUN_LEAD_HOURS:
        maturity = "d2_fallback"
    else:
        raise ContractError("model_capture_run_outside_d2_d1_d0_window")
    if window["state"] == "not_started":
        hours_until_start = (target_start - decision).total_seconds() / 3600.0
        if hours_until_start > MAX_PRE_EVENT_CAPTURE_HOURS:
            raise ContractError("model_capture_target_too_far_ahead")
        phase = "prospective_pre_event"
    elif window["state"] == "closed":
        phase = "prospective_post_event"
    else:
        phase = "prospective_open_event"
    return {
        **window,
        "capture_phase": phase,
        "maturity": maturity,
        "target_timezone": timezone_name,
    }


def _validate_model_arrival(
    raw: Mapping[str, Any],
    *,
    decision_at_utc: str | None,
    require_event_day_started: bool,
) -> dict[str, Any]:
    """Shared native-arrival validator for capture and formal consumption."""

    if not isinstance(raw, Mapping):
        raise ContractError("model_arrival_mapping_required")
    provider_id = _text("provider_id", raw.get("provider_id")).lower()
    model_id = _text("model_id", raw.get("model_id")).upper()
    if provider_id not in ALLOWED_PROVIDERS:
        raise ContractError("model_arrival_provider_not_allowed")
    if ALLOWED_PROVIDERS[provider_id] != model_id or model_id not in MODEL_IDS:
        raise ContractError("model_arrival_model_provider_mismatch")
    station_id = _text("station_id", raw.get("station_id"))
    target_local_date = _date("target_local_date", raw.get("target_local_date"))
    timezone_name = _text("target_timezone", raw.get("target_timezone"))
    run_init = _timestamp("run_initialization_utc", raw.get("run_initialization_utc"))
    valid_time = _timestamp("valid_time_utc", raw.get("valid_time_utc"))
    published = _timestamp("published_at_utc", raw.get("published_at_utc"))
    received = _timestamp("received_at_utc", raw.get("received_at_utc"))
    capture_decision = _timestamp("decision_at_utc", raw.get("decision_at_utc"))
    formal_decision = (
        _timestamp("formal_decision_at_utc", decision_at_utc)
        if decision_at_utc is not None
        else capture_decision
    )
    capture_window = validate_prospective_capture_window(
        target_local_date=target_local_date,
        target_timezone=timezone_name,
        run_initialization_utc=_timestamp_text(run_init),
        decision_at_utc=_timestamp_text(capture_decision),
    )
    if require_event_day_started:
        # Raw may be captured pre-event, but indexing/decoding remains closed
        # until the formal consumer's own as-of clock reaches the ET start.
        assert_event_day_started(
            target_local_date,
            as_of_utc=formal_decision,
            target_timezone=timezone_name,
        )
    if published < run_init:
        raise ContractError("model_published_before_run_initialization")
    if published > received:
        raise ContractError("model_published_after_received")
    if received > capture_decision:
        raise ContractError("model_received_after_decision")
    _target_date_matches_valid_time(target_local_date, valid_time, timezone_name)
    if raw.get("capture_mode") != "prospective":
        raise ContractError("model_arrival_capture_not_prospective")
    if raw.get("historical_arrival_time_observed") is not True:
        raise ContractError("historical_arrival_time_not_observed")
    if raw.get("aggregator_or_republisher") is not False:
        raise ContractError("model_arrival_aggregator_forbidden")
    if raw.get("open_meteo") is not False:
        raise ContractError("model_arrival_open_meteo_forbidden")
    if raw.get("publication_time_source") != "provider_http_last_modified":
        raise ContractError("model_publication_time_source_invalid")
    for flag in REQUIRED_SOURCE_FLAGS:
        if not isinstance(raw.get(flag), bool):
            raise ContractError(f"{flag}_must_be_boolean")
    source_url = _text("source_url", raw.get("source_url"))
    if not source_url.lower().startswith("https://"):
        raise ContractError("model_arrival_source_url_invalid")
    authority = source_url[8:].split("/", 1)[0].split("?", 1)[0].strip()
    if not authority or ":" in authority:
        raise ContractError("model_arrival_source_url_invalid")
    raw_hashes = _raw_hashes(raw.get("raw_payload_sha256s", raw.get("raw_payload_sha256")))
    receipt_sha = _sha("receipt_sha256", raw.get("receipt_sha256"))
    identity = {
        "schema": SCHEMA,
        "arrival_id": _text("arrival_id", raw.get("arrival_id")),
        "provider_id": provider_id,
        "model_id": model_id,
        "station_id": station_id,
        "target_local_date": target_local_date,
        "target_timezone": timezone_name,
        "run_initialization_utc": _timestamp_text(run_init),
        "valid_time_utc": _timestamp_text(valid_time),
        "published_at_utc": _timestamp_text(published),
        "received_at_utc": _timestamp_text(received),
        "decision_at_utc": _timestamp_text(capture_decision),
        "publication_time_source": "provider_http_last_modified",
        "source_url": source_url,
        "raw_payload_sha256s": list(raw_hashes),
        "receipt_sha256": receipt_sha,
        "capture_mode": "prospective",
        "historical_arrival_time_observed": True,
        "aggregator_or_republisher": False,
        "open_meteo": False,
        "capture_phase": capture_window["capture_phase"],
        "p0_consumption_eligible_at_capture": True,
        "p1_consumption_eligible_at_capture": capture_window["capture_phase"] == "prospective_open_event",
        "production_authority": False,
        "live_order_authorized": False,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def validate_model_arrival_capture(
    raw: Mapping[str, Any],
    *,
    decision_at_utc: str | None = None,
) -> dict[str, Any]:
    """Validate raw evidence capture, including bounded pre-event d2/d1 rows."""

    return _validate_model_arrival(
        raw,
        decision_at_utc=decision_at_utc,
        require_event_day_started=False,
    )


def validate_model_arrival(
    raw: Mapping[str, Any],
    *,
    decision_at_utc: str | None = None,
) -> dict[str, Any]:
    """Validate one arrival for formal indexing after its ET day has started."""

    return _validate_model_arrival(
        raw,
        decision_at_utc=decision_at_utc,
        require_event_day_started=True,
    )


def build_blocked_receipt(*, reason_codes: Iterable[str], received_at_utc: str | None = None) -> dict[str, Any]:
    """Build an explicit non-authorizing receipt for a rejected capture."""

    reasons = sorted({str(item).strip() for item in reason_codes if str(item).strip()}) or ["model_arrival_blocked"]
    identity = {
        "schema": BLOCKED_SCHEMA,
        "status": "BLOCKED",
        "reason_codes": reasons,
        "received_at_utc": received_at_utc,
        "production_authority": False,
        "live_order_authorized": False,
        "writes_production_db": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    return {**identity, "receipt_sha256": content_sha256(identity)}


def load_model_arrival_receipts(
    rows: Iterable[Mapping[str, Any]],
    *,
    decision_at_utc: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Validate a receipt set and return an exact-key index audit."""

    normalized: list[dict[str, Any]] = []
    by_key: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    blocked: list[dict[str, str]] = []
    cutoff = _timestamp("decision_at_utc", decision_at_utc)
    for item in rows:
        try:
            # Receipt ordering is validated against its immutable capture
            # clock; the separate batch cutoff authorizes formal consumption
            # only after the ET target day starts.
            row = validate_model_arrival(item, decision_at_utc=decision_at_utc)
            row_decision = _timestamp("decision_at_utc", row["decision_at_utc"])
            row_received = _timestamp("received_at_utc", row["received_at_utc"])
            if row_decision > cutoff or row_received > cutoff:
                raise ContractError("model_arrival_after_as_of")
        except ContractError as exc:
            blocked.append({"reason": str(exc)})
            continue
        key = model_arrival_key(row)
        previous = by_key.get(key)
        if previous is not None and previous != row:
            blocked.append({"reason": "model_arrival_duplicate_key_conflict", "key": "|".join(key)})
            continue
        by_key[key] = row
    normalized = [by_key[key] for key in sorted(by_key)]
    return normalized, {
        "schema": INDEX_SCHEMA,
        "decision_at_utc": decision_at_utc,
        "accepted_count": len(normalized),
        "blocked_count": len(blocked),
        "blocked": blocked,
        "all_prospective": bool(normalized) and all(row["capture_mode"] == "prospective" for row in normalized),
        "promotion_allowed": False,
        "production_authority": False,
    }


def model_arrival_key(row: Mapping[str, Any]) -> tuple[str, str, str, str, str]:
    """Return the exact identity for one native forecast valid time.

    A single provider run legitimately contains many valid times for the
    target local day.  ``valid_time_utc`` therefore belongs in the identity;
    omitting it turns a complete hourly curve into duplicate-key conflicts.
    """

    def _timestamp_text(value: Any) -> str:
        return str(value or "").strip().replace("+00:00", "Z")

    return (
        str(row.get("provider_id") or "").strip().lower(),
        str(row.get("station_id") or "").strip(),
        str(row.get("target_local_date") or "").strip(),
        _timestamp_text(row.get("run_initialization_utc")),
        _timestamp_text(row.get("valid_time_utc")),
    )


__all__ = [
    "ALLOWED_PROVIDERS",
    "BLOCKED_SCHEMA",
    "INDEX_SCHEMA",
    "SCHEMA",
    "MAX_PRE_EVENT_CAPTURE_HOURS",
    "MAX_PRE_EVENT_RUN_LEAD_HOURS",
    "build_blocked_receipt",
    "load_model_arrival_receipts",
    "model_arrival_key",
    "validate_model_arrival_capture",
    "validate_model_arrival",
    "validate_prospective_capture_window",
]
