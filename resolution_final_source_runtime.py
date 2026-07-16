"""Pure, strict builders for the accepted ZUUU final X and S observations.

They deliberately do not fetch providers or write ``archive.db``.  Their only
output is a final-source observation candidate for the isolated label staging
registry.  Thus an accepted source definition cannot accidentally become a
production posterior, trading, or settlement shortcut.
"""

from __future__ import annotations

import hashlib
import math
from datetime import date, datetime, timezone
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from resolution_authority_contract_runtime import validate_resolution_authority_contract
from source_observation_runtime import build_source_observation, raw_payload_sha256
from zuuu_metar_reporting_schedule import (
    EXPECTED_REGULAR_SLOT_COUNT,
    SCHEDULE_ID,
    SCHEDULE_SCHEMA,
    expected_regular_metar_slots_utc,
    validate_regular_metar_slot_coverage,
)


SCHEMA_VERSION = "resolution_final_source_builder_v2"
STRICT_AMSC_READER_SCHEMA = "strict_amsc_hard_observation_reader_v1"
RAW_COVERAGE_READER_SCHEMA = "amsc_primary_raw_coverage_reader_v1"
PHYSICAL_SOURCE_ID = "amsc_zuuu_scheduled_metar_daily_max_v2"
SETTLEMENT_SOURCE_ID = "wunderground_zuuu_historical_observation_daily_high_final_v3"
PHYSICAL_ROLE = "physical_daily_high_final_truth"
SETTLEMENT_ROLE = "resolution_settlement_final"
STATION_TZ = ZoneInfo("Asia/Shanghai")


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _parse_time(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


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
        "writes_archive_db": False,
        "places_orders": False,
    }


def _authority_or_block(contract: Mapping[str, Any]) -> dict[str, Any] | None:
    audit = validate_resolution_authority_contract(contract)
    if audit.get("ok") is not True:
        return _blocked(*(f"resolution_authority_contract:{item}" for item in audit.get("blockers") or []))
    return None


def build_amsc_physical_final_source_observation(
    *,
    event_date: str,
    strict_amsc_reader_result: Mapping[str, Any],
    coverage_manifest: Mapping[str, Any],
    coverage_ref: str,
    finalized_at: str,
    authority_contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Build final observation-defined X only from a closed, complete AMSC day.

    Every selected METAR must bind to an immutable raw object and poll manifest.
    A complete closed station day creates physical truth X even if an arrival
    was late. Late arrival only downgrades strict-as-of training eligibility;
    it must not erase the independently useful daily physical label.
    """

    authority_error = _authority_or_block(authority_contract)
    if authority_error:
        return authority_error
    day = _text(event_date)[:10]
    try:
        target_day = date.fromisoformat(day)
    except ValueError:
        return _blocked("physical_x_event_date_invalid")
    finalized = _parse_time(finalized_at)
    if finalized is None or finalized.astimezone(STATION_TZ).date() <= target_day:
        return _blocked("physical_x_finalized_before_target_day_closed")

    reader = _mapping(strict_amsc_reader_result)
    coverage = _mapping(coverage_manifest)
    reasons: list[str] = []
    if _text(reader.get("schema")) not in {STRICT_AMSC_READER_SCHEMA, RAW_COVERAGE_READER_SCHEMA} or reader.get("ok") is not True:
        reasons.append("physical_x_strict_amsc_reader_not_available")
    if _text(reader.get("event_date")) != day:
        reasons.append("physical_x_strict_reader_event_date_mismatch")
    if _text(coverage.get("event_date")) != day or _text(coverage.get("station_id")).upper() != "ZUUU":
        reasons.append("physical_x_coverage_identity_mismatch")
    coverage_state = _text(coverage.get("day_state"))
    if coverage_state not in {"forward_arrival_complete_pending_label", "historical_window_or_late_arrival_only"}:
        reasons.append("physical_x_coverage_not_closed_complete")
    if _text(coverage.get("reporting_schedule_schema")) != SCHEDULE_SCHEMA:
        reasons.append("physical_x_reporting_schedule_schema_invalid")
    if _text(coverage.get("reporting_schedule_id")) != SCHEDULE_ID:
        reasons.append("physical_x_reporting_schedule_id_invalid")
    if int(coverage.get("expected_regular_metar_slot_count") or 0) != EXPECTED_REGULAR_SLOT_COUNT:
        reasons.append("physical_x_expected_regular_metar_slot_count_invalid")
    if list(coverage.get("expected_regular_metar_slots_utc") or []) != expected_regular_metar_slots_utc(day):
        reasons.append("physical_x_expected_regular_metar_slots_invalid")
    if int(coverage.get("distinct_valid_time_count") or 0) != EXPECTED_REGULAR_SLOT_COUNT:
        reasons.append("physical_x_scheduled_coverage_not_exactly_expected")
    if int(coverage.get("event_raw_object_count") or 0) != EXPECTED_REGULAR_SLOT_COUNT:
        reasons.append("physical_x_raw_object_coverage_not_exactly_expected")
    if list(coverage.get("missing_regular_metar_slots_utc") or []):
        reasons.append("physical_x_scheduled_regular_metar_slots_missing")
    if list(coverage.get("unexpected_regular_metar_slots_utc") or []):
        reasons.append("physical_x_unexpected_regular_metar_slots_present")
    if int(coverage.get("correction_count") or 0) != 0:
        reasons.append("physical_x_corrections_not_resolved")
    if int(coverage.get("missing_first_seen_count") or 0) != 0:
        reasons.append("physical_x_first_seen_incomplete")
    if _mapping(coverage.get("raw_archive_integrity")).get("ok") is not True:
        reasons.append("physical_x_raw_archive_integrity_not_ready")
    coverage_sha = _text(coverage.get("coverage_sha256"))
    if len(coverage_sha) != 64 or any(char not in "0123456789abcdef" for char in coverage_sha.lower()):
        reasons.append("physical_x_coverage_sha256_invalid")
    if not _text(coverage_ref):
        reasons.append("physical_x_coverage_ref_missing")

    observations = list(reader.get("source_observations") or [])
    coverage_hashes = {_text(value) for value in coverage.get("raw_object_sha256_list") or []}
    selected: list[dict[str, Any]] = []
    for value in observations:
        item = _mapping(value)
        # The closed coverage manifest is the station-day membership SSOT.
        # Ignore archive rows from the same local day that are outside it
        # before applying the selected-METAR contract.
        if coverage_hashes and _text(item.get("raw_sha256")) not in coverage_hashes:
            continue
        valid = _parse_time(item.get("valid_time"))
        temperature = _finite(item.get("temperature_c"))
        if (
            _text(item.get("source_id")) != "zuuu_metar_raw_amsc"
            or _text(item.get("role")) != "metar_hard_observation_primary"
            or _text(item.get("report_type")).upper() != "METAR"
            or _text(item.get("station_id")).upper() != "ZUUU"
            or _text(item.get("event_date"))[:10] != day
            or valid is None
            or temperature is None
            or not _text(item.get("source_observation_id"))
            or not _text(item.get("raw_sha256"))
        ):
            reasons.append("physical_x_strict_selected_observation_invalid")
            continue
        selected.append(
            {
                "source_observation_id": _text(item.get("source_observation_id")),
                "raw_sha256": _text(item.get("raw_sha256")),
                "valid_time": valid.isoformat().replace("+00:00", "Z"),
                "temperature_c": temperature,
            }
        )
    selected.sort(key=lambda row: (row["valid_time"], row["raw_sha256"]))
    selected_slots = validate_regular_metar_slot_coverage(
        event_date=day,
        observed_valid_times_utc=[row["valid_time"] for row in selected],
    )
    if len(selected) != EXPECTED_REGULAR_SLOT_COUNT or selected_slots.get("complete") is not True:
        reasons.append("physical_x_strict_observation_schedule_coverage_invalid")
    if coverage_hashes and {row["raw_sha256"] for row in selected} != coverage_hashes:
        reasons.append("physical_x_coverage_manifest_membership_incomplete")
    if reasons:
        return _blocked(*reasons)

    maximum = max(row["temperature_c"] for row in selected)
    peak_times = [row["valid_time"] for row in selected if row["temperature_c"] == maximum]
    strict_asof_training_eligible = bool(
        coverage_state == "forward_arrival_complete_pending_label"
        and int(coverage.get("late_arrival_count") or 0) == 0
        and all(
            _mapping(item).get("strict_asof_replay_eligible") is not False
            for item in observations
        )
    )
    label_quality = (
        "strict_asof_complete_schedule"
        if strict_asof_training_eligible
        else "complete_schedule_late_or_non_strict_arrival"
    )
    observation = build_source_observation(
        source_id=PHYSICAL_SOURCE_ID,
        station_id="ZUUU",
        event_date=day,
        valid_time=finalized_at,
        fetched_at=finalized_at,
        parsed_at=finalized_at,
        response_completed_at=finalized_at,
        raw_ref=_text(coverage_ref),
        raw_sha256_override=coverage_sha,
        parser_version=SCHEMA_VERSION,
        payload={
            "final_physical_high_c": maximum,
            "label_finality": "final",
            "physical_x_label_quality": label_quality,
            "strict_asof_training_eligible": strict_asof_training_eligible,
            "late_arrival_count": int(coverage.get("late_arrival_count") or 0),
            "coverage_sha256": coverage_sha,
            "coverage_ref": _text(coverage_ref),
            # These rows establish physical station-day membership.  They are
            # deliberately not named "strict": a closed X remains valid even
            # when a late first-seen time makes it ineligible for strict-as-of
            # model training.
            "physical_x_provenance_schema": "amsc_station_day_raw_membership_v1",
            "physical_source_observation_ids": [row["source_observation_id"] for row in selected],
            "physical_raw_sha256_list": [row["raw_sha256"] for row in selected],
            "peak_valid_times": peak_times,
            "aggregation": "maximum_reported_temperature_c",
            "reporting_schedule_id": SCHEDULE_ID,
            "expected_regular_metar_slot_count": EXPECTED_REGULAR_SLOT_COUNT,
        },
        overrides={
            "source_class": "settlement_source",
            "role": PHYSICAL_ROLE,
            "hard_eligible": False,
            "demotion_reason": "final_label_only_not_realtime_weather_evidence",
        },
    ).as_dict()
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "final_source_observation_ready",
        "source_observation": observation,
        "coverage_sha256": coverage_sha,
        "read_only": True,
        "writes_archive_db": False,
        "places_orders": False,
    }


def build_wunderground_settlement_final_source_observation(
    *,
    event_date: str,
    capture: Mapping[str, Any],
    authority_contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Build final S from a raw Wunderground capture plus market finality proof.

    A caller must archive the raw provider body before calling this builder.  A
    parsed number without its raw body, exact source URL, extraction evidence,
    and market-resolution finality attestation is rejected.
    """

    authority_error = _authority_or_block(authority_contract)
    if authority_error:
        return authority_error
    day = _text(event_date)[:10]
    if not _parse_time(f"{day}T00:00:00Z"):
        return _blocked("settlement_s_event_date_invalid")
    record = _mapping(capture)
    settlement = _mapping(authority_contract).get("settlement_s")
    capture_url_prefix = _text(_mapping(settlement).get("capture_url_prefix"))
    required_parser_id = _text(_mapping(settlement).get("parser_id"))
    source_url = _text(record.get("source_url"))
    raw_payload = record.get("raw_payload")
    if isinstance(raw_payload, bytes):
        raw_text = raw_payload.decode("utf-8", errors="replace")
    else:
        raw_text = _text(raw_payload)
    fetched_at = _text(record.get("fetched_at"))
    response_completed_at = _text(record.get("response_completed_at"))
    parsed_at = _text(record.get("parsed_at"))
    scalar = _finite(record.get("daily_high_c"))
    evidence = _text(record.get("extraction_evidence"))
    raw_sha = raw_payload_sha256(raw_payload)
    declared_raw_sha = _text(record.get("raw_sha256"))
    attestation = _mapping(record.get("market_resolution_finality_attestation"))
    reasons: list[str] = []
    if _text(record.get("station_id")).upper() != "ZUUU" or _text(record.get("event_date"))[:10] != day:
        reasons.append("settlement_s_capture_identity_mismatch")
    date_key = day.replace("-", "")
    expected_source_url = f"{capture_url_prefix}?apiKey=REDACTED&units=m&startDate={date_key}&endDate={date_key}"
    if source_url != expected_source_url:
        reasons.append("settlement_s_capture_source_url_invalid")
    if not raw_text or not _text(record.get("raw_ref")):
        reasons.append("settlement_s_raw_capture_missing")
    if declared_raw_sha and declared_raw_sha != raw_sha:
        reasons.append("settlement_s_raw_sha256_binding_mismatch")
    if scalar is None:
        reasons.append("settlement_s_daily_high_not_finite")
    if _text(record.get("extractor_version")) != required_parser_id:
        reasons.append("settlement_s_parser_not_authority_approved")
    if not _text(record.get("extractor_version")) or not evidence or evidence not in raw_text:
        reasons.append("settlement_s_extraction_evidence_not_bound_to_raw")
    parsed_times = [_parse_time(value) for value in (fetched_at, response_completed_at, parsed_at)]
    if any(value is None for value in parsed_times):
        reasons.append("settlement_s_capture_timestamps_invalid")
    elif parsed_times[1] > parsed_times[0] or parsed_times[2] < parsed_times[1]:
        reasons.append("settlement_s_capture_timestamp_order_invalid")
    if attestation.get("market_resolution_final") is not True:
        reasons.append("settlement_s_market_resolution_not_final")
    if _text(attestation.get("event_date"))[:10] != day:
        reasons.append("settlement_s_finality_event_date_mismatch")
    if _parse_time(attestation.get("attested_at")) is None:
        reasons.append("settlement_s_finality_attested_at_invalid")
    rule_sha = _text(attestation.get("market_rule_snapshot_sha256")).lower()
    if len(rule_sha) != 64 or any(char not in "0123456789abcdef" for char in rule_sha):
        reasons.append("settlement_s_market_rule_snapshot_sha256_invalid")
    if reasons:
        return _blocked(*reasons)

    observation = build_source_observation(
        source_id=SETTLEMENT_SOURCE_ID,
        station_id="ZUUU",
        event_date=day,
        valid_time=fetched_at,
        fetched_at=fetched_at,
        parsed_at=parsed_at,
        response_completed_at=response_completed_at,
        raw_ref=_text(record.get("raw_ref")),
        raw_payload=raw_payload,
        parser_version=_text(record.get("extractor_version")),
        payload={
            "settlement_scalar_c": scalar,
            "label_finality": "final",
            "source_url": source_url,
            "extraction_evidence_sha256": hashlib.sha256(evidence.encode("utf-8")).hexdigest(),
            "market_resolution_finality_attestation": dict(attestation),
        },
        overrides={
            "source_class": "settlement_source",
            "role": SETTLEMENT_ROLE,
            "hard_eligible": False,
            "demotion_reason": "final_label_only_not_realtime_weather_evidence",
        },
    ).as_dict()
    return {
        "schema": SCHEMA_VERSION,
        "ok": True,
        "state": "final_source_observation_ready",
        "source_observation": observation,
        "read_only": True,
        "writes_archive_db": False,
        "places_orders": False,
    }


__all__ = [
    "PHYSICAL_ROLE",
    "PHYSICAL_SOURCE_ID",
    "SCHEMA_VERSION",
    "SETTLEMENT_ROLE",
    "SETTLEMENT_SOURCE_ID",
    "build_amsc_physical_final_source_observation",
    "build_wunderground_settlement_final_source_observation",
]
