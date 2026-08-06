"""Frozen 45-station pre-observation schedule contract for P1 source choice."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from bayes_basket_target.contracts import content_sha256


SCHEMA = "canonical_station_p1_preobservation_schedule_v1"
SOURCE_PRIORITY = ("EXACT_ASOS_1MIN", "MADIS_HFMETAR_5MIN", "AWC_METAR_SPECI")
RULE_TYPES = {"FIXED_SINGLE_MINUTE", "FIXED_DUAL_MINUTE", "FIXED_WINDOW_ANCHOR"}
DEFAULT_SCHEDULE_PATH = (
    Path(__file__).resolve().parent
    / "config/trading/canonical_station_p1_preobservation_schedule_v1.json"
)


def load_schedule(path: Path = DEFAULT_SCHEDULE_PATH) -> dict[str, Any]:
    return validate_schedule(json.loads(path.read_text(encoding="utf-8")))


def validate_schedule(raw: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(raw)
    if value.get("schema") != SCHEMA:
        raise ValueError("station_p1_schedule_schema_invalid")
    rows = value.get("stations")
    if not isinstance(rows, list) or len(rows) != 45:
        raise ValueError("station_p1_schedule_45station_required")
    seen: set[str] = set()
    for row in rows:
        station = str(row.get("station_id") or "").upper()
        if not station or station in seen:
            raise ValueError("station_p1_schedule_station_duplicate")
        seen.add(station)
        if row.get("rule_type") not in RULE_TYPES:
            raise ValueError("station_p1_schedule_rule_type_invalid")
        minutes = row.get("scheduled_minutes")
        if (
            not isinstance(minutes, list)
            or not minutes
            or minutes != sorted(set(minutes))
            or any(not isinstance(item, int) or not 0 <= item <= 59 for item in minutes)
        ):
            raise ValueError("station_p1_schedule_minutes_invalid")
        if row["rule_type"] == "FIXED_SINGLE_MINUTE" and len(minutes) != 1:
            raise ValueError("station_p1_schedule_single_minute_invalid")
        if row["rule_type"] == "FIXED_DUAL_MINUTE" and len(minutes) != 2:
            raise ValueError("station_p1_schedule_dual_minute_invalid")
        window = row.get("observation_minute_window")
        if not isinstance(window, list) or len(window) != 2 or not 0 <= window[0] <= window[1] <= 59:
            raise ValueError("station_p1_schedule_window_invalid")
        if row["rule_type"] != "FIXED_WINDOW_ANCHOR" and window != [minutes[0], minutes[-1]]:
            raise ValueError("station_p1_schedule_fixed_window_invalid")
        if row["rule_type"] == "FIXED_WINDOW_ANCHOR" and any(
            not window[0] <= minute <= window[1] for minute in minutes
        ):
            raise ValueError("station_p1_schedule_anchor_outside_window")
        sources = row.get("source_priority")
        asos = bool(row.get("asos_p1_oos_ready"))
        expected = list(SOURCE_PRIORITY if asos else ("AWC_METAR_SPECI",))
        if sources != expected:
            raise ValueError("station_p1_schedule_source_priority_invalid")
        madis = row.get("madis_preceding_minutes")
        expected_madis = sorted({(minute // 5) * 5 for minute in minutes}) if asos else []
        if madis != expected_madis:
            raise ValueError("station_p1_schedule_madis_minutes_invalid")
    supplied = str(value.pop("content_sha256", ""))
    if supplied != content_sha256(value):
        raise ValueError("station_p1_schedule_hash_mismatch")
    return {**value, "content_sha256": supplied}


def next_preobservation_plan(
    schedule: Mapping[str, Any],
    *,
    station_id: str,
    as_of_utc: datetime,
) -> dict[str, Any]:
    contract = validate_schedule(schedule)
    station = station_id.upper()
    row = next((item for item in contract["stations"] if item["station_id"] == station), None)
    if row is None:
        raise ValueError("station_p1_schedule_station_missing")
    now = as_of_utc.astimezone(timezone.utc)
    candidates = []
    for hour_delta in (0, 1):
        hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=hour_delta)
        for minute in row["scheduled_minutes"]:
            candidate = hour.replace(minute=minute)
            if candidate >= now:
                candidates.append(candidate)
    scheduled = min(candidates)
    madis_minute = (scheduled.minute // 5) * 5
    madis = scheduled.replace(minute=madis_minute)
    return {
        "station_id": station,
        "scheduled_metar_observation_at_utc": scheduled.isoformat().replace("+00:00", "Z"),
        "exact_asos_observation_at_utc": (
            scheduled.isoformat().replace("+00:00", "Z")
            if row["asos_p1_oos_ready"] else None
        ),
        "madis_preobservation_at_utc": (
            madis.isoformat().replace("+00:00", "Z")
            if row["asos_p1_oos_ready"] else None
        ),
        "madis_lead_minutes": (
            int((scheduled - madis).total_seconds() // 60)
            if row["asos_p1_oos_ready"] else None
        ),
        "source_priority": list(row["source_priority"]),
        "schedule_content_sha256": contract["content_sha256"],
    }


def validate_observation_against_plan(
    schedule: Mapping[str, Any],
    *,
    station_id: str,
    source_product: str,
    planned_metar_observation_at_utc: str,
    observed_at_utc: str,
) -> dict[str, Any]:
    """Fail closed unless a source row is the frozen ex-ante slot for the station."""

    contract = validate_schedule(schedule)
    station = station_id.upper()
    row = next((item for item in contract["stations"] if item["station_id"] == station), None)
    if row is None:
        raise ValueError("station_p1_schedule_station_missing")
    if source_product not in row["source_priority"]:
        raise ValueError("station_p1_schedule_source_not_eligible")
    planned = datetime.fromisoformat(planned_metar_observation_at_utc.replace("Z", "+00:00"))
    observed = datetime.fromisoformat(observed_at_utc.replace("Z", "+00:00"))
    if planned.tzinfo is None or observed.tzinfo is None:
        raise ValueError("station_p1_schedule_timezone_required")
    planned = planned.astimezone(timezone.utc).replace(second=0, microsecond=0)
    observed = observed.astimezone(timezone.utc).replace(second=0, microsecond=0)
    if planned.minute not in row["scheduled_minutes"]:
        raise ValueError("station_p1_schedule_unfrozen_planned_minute")
    expected = planned if source_product != "MADIS_HFMETAR_5MIN" else planned.replace(
        minute=(planned.minute // 5) * 5
    )
    accepted = observed == expected
    if source_product == "AWC_METAR_SPECI" and row["rule_type"] == "FIXED_WINDOW_ANCHOR":
        accepted = (
            observed.replace(minute=0) == planned.replace(minute=0)
            and row["observation_minute_window"][0]
            <= observed.minute
            <= row["observation_minute_window"][1]
        )
    if not accepted:
        raise ValueError("station_p1_schedule_observation_slot_mismatch")
    return {
        "station_id": station,
        "source_product": source_product,
        "planned_metar_observation_at_utc": planned.isoformat().replace("+00:00", "Z"),
        "expected_source_observation_at_utc": expected.isoformat().replace("+00:00", "Z"),
        "schedule_content_sha256": contract["content_sha256"],
    }


def select_scheduled_p1_source(
    schedule: Mapping[str, Any],
    *,
    station_id: str,
    planned_metar_observation_at_utc: str,
    candidates: Mapping[str, Mapping[str, Any] | None],
) -> dict[str, Any]:
    """Select the first valid frozen source; never choose by observed value."""

    contract = validate_schedule(schedule)
    station = station_id.upper()
    row = next((item for item in contract["stations"] if item["station_id"] == station), None)
    if row is None:
        raise ValueError("station_p1_schedule_station_missing")
    unknown = set(candidates) - set(row["source_priority"])
    if unknown:
        raise ValueError("station_p1_schedule_candidate_source_invalid")
    rejected: list[dict[str, str]] = []
    for source_product in row["source_priority"]:
        candidate = candidates.get(source_product)
        if candidate is None:
            rejected.append({"source_product": source_product, "reason": "DATA_MISSING"})
            continue
        try:
            binding = validate_observation_against_plan(
                contract,
                station_id=station,
                source_product=source_product,
                planned_metar_observation_at_utc=planned_metar_observation_at_utc,
                observed_at_utc=str(candidate["observed_at_utc"]),
            )
        except (KeyError, ValueError) as exc:
            rejected.append({"source_product": source_product, "reason": str(exc)})
            continue
        return {
            "state": "SELECTED",
            "source_product": source_product,
            "candidate": dict(candidate),
            "binding": binding,
            "rejected_higher_priority_sources": rejected,
            "schedule_content_sha256": contract["content_sha256"],
        }
    return {
        "state": "BLOCKED",
        "failure_code": "DATA_MISSING",
        "rejected_sources": rejected,
        "schedule_content_sha256": contract["content_sha256"],
    }


__all__ = [
    "DEFAULT_SCHEDULE_PATH", "SCHEMA", "load_schedule", "next_preobservation_plan",
    "select_scheduled_p1_source", "validate_observation_against_plan", "validate_schedule",
]
