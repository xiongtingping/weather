"""Versioned primary AMSC ZUUU routine-METAR reporting schedule.

The final physical X label must be complete against the station's actual
routine reporting schedule, not an assumed 48 half-hour grid.  Observed AMSC
coverage for 2026-07-10/11 is hourly from 00:00 through 03:00 local and
half-hourly from 04:00 through 23:30 local: 44 acquisition slots.

Half-hour slots are acquisition/reference slots only.  Settlement facts for
the physical hard boundary are restricted to the 24 whole-hour (:00) slots.
This module intentionally excludes SPECI.  SPECI remains strong evidence but
cannot fill, replace, or create a scheduled regular-METAR slot for X.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo


SCHEDULE_SCHEMA = "zuuu_primary_metar_reporting_schedule_v1"
SCHEDULE_ID = "zuuu_primary_metar_local_44_slot_2026_07_v1"
STATION_ID = "ZUUU"
LOCAL_TIMEZONE = "Asia/Shanghai"
STATION_TZ = ZoneInfo(LOCAL_TIMEZONE)
EXPECTED_REGULAR_SLOT_COUNT = 44
EXPECTED_SETTLEMENT_SLOT_COUNT = 24


def _text(value: Any) -> str:
    return str(value or "").strip()


def _parse_event_date(value: str) -> date | None:
    try:
        return date.fromisoformat(_text(value))
    except ValueError:
        return None


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def expected_regular_metar_slots_utc(event_date: str) -> list[str]:
    """Return acquisition slots for one Asia/Shanghai station day."""

    local_day = _parse_event_date(event_date)
    if local_day is None:
        return []
    slots: list[str] = []
    for hour in range(24):
        minutes = (0,) if hour < 4 else (0, 30)
        for minute in minutes:
            local_at = datetime.combine(local_day, time(hour=hour, minute=minute), tzinfo=STATION_TZ)
            slots.append(_iso_utc(local_at))
    return slots


def expected_settlement_metar_slots_utc(event_date: str) -> list[str]:
    """Return whole-hour AMSC slots allowed to advance the settlement fact."""

    local_day = _parse_event_date(event_date)
    if local_day is None:
        return []
    return [
        _iso_utc(datetime.combine(local_day, time(hour=hour, minute=0), tzinfo=STATION_TZ))
        for hour in range(24)
    ]


def is_settlement_metar_slot_utc(event_date: str, valid_time_utc: Any) -> bool:
    """Whether a valid time is a whole-hour settlement/fact slot."""

    return _text(valid_time_utc) in set(expected_settlement_metar_slots_utc(event_date))


def build_zuuu_primary_metar_schedule_contract(event_date: str) -> dict[str, Any]:
    slots = expected_regular_metar_slots_utc(event_date)
    return {
        "schema": SCHEDULE_SCHEMA,
        "schedule_id": SCHEDULE_ID,
        "station_id": STATION_ID,
        "timezone": LOCAL_TIMEZONE,
        "event_date": _text(event_date),
        "regular_metar_slot_rule": "00:00-03:00 local hourly; 04:00-23:30 local every 30 minutes",
        "expected_regular_metar_slots_utc": slots,
        "expected_regular_metar_slot_count": len(slots),
        "settlement_metar_slot_rule": "all local whole hours (:00) only; half-hour slots are reference-only",
        "expected_settlement_metar_slots_utc": expected_settlement_metar_slots_utc(event_date),
        "expected_settlement_metar_slot_count": EXPECTED_SETTLEMENT_SLOT_COUNT,
        "half_hour_is_reference_only": True,
        "specli_can_fill_regular_slot": False,
        "read_only": True,
    }


def validate_regular_metar_slot_coverage(
    *,
    event_date: str,
    observed_valid_times_utc: Iterable[Any],
) -> dict[str, Any]:
    """Compare observed primary-METAR times to the fixed station schedule."""

    schedule = build_zuuu_primary_metar_schedule_contract(event_date)
    expected = list(schedule["expected_regular_metar_slots_utc"])
    observed = sorted({_text(value) for value in observed_valid_times_utc if _text(value)})
    expected_set = set(expected)
    observed_set = set(observed)
    missing = sorted(expected_set - observed_set)
    unexpected = sorted(observed_set - expected_set)
    return {
        **schedule,
        "observed_regular_metar_slots_utc": observed,
        "observed_regular_metar_slot_count": len(observed),
        "missing_regular_metar_slots_utc": missing,
        "unexpected_regular_metar_slots_utc": unexpected,
        "complete": bool(expected) and not missing and not unexpected,
        "read_only": True,
    }


__all__ = [
    "EXPECTED_REGULAR_SLOT_COUNT",
    "EXPECTED_SETTLEMENT_SLOT_COUNT",
    "LOCAL_TIMEZONE",
    "SCHEDULE_ID",
    "SCHEDULE_SCHEMA",
    "STATION_ID",
    "build_zuuu_primary_metar_schedule_contract",
    "expected_regular_metar_slots_utc",
    "expected_settlement_metar_slots_utc",
    "is_settlement_metar_slot_utc",
    "validate_regular_metar_slot_coverage",
]
