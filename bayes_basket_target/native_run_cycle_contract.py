"""Derive each native system's (lead, run cycle) instead of hardcoding it.

Every collector used to carry its own ``RUN_CYCLE`` constant.  Three of them
said ``18Z`` and ECCC said ``00Z``, with no recorded reason for either -- and
the ECCC constant turned out to matter: it fires 18 hours before the 18Z the
other families use, so ECCC was being compared against them while carrying an
information deficit that had nothing to do with the model's skill.  ECCC is
also the weakest candidate in every station scan, which is exactly what a
self-inflicted deficit would look like.

The cycle is not a free parameter.  Once a lead bucket is fixed, the right
cycle is the *last run published on the run day*, because that run has seen the
most observations while still being available at the same lead as everyone
else.  That rule yields 18Z for ECMWF/GEFS/ICON and 12Z for ECCC -- not because
ECCC is special, but because ECCC does not publish an 18Z run at all.

Deriving it here means the answer to "why this cycle" is a published-schedule
fact rather than a constant nobody can explain.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from types import MappingProxyType
from typing import Mapping

from .contracts import ContractError


LEAD_DAYS: Mapping[str, int] = MappingProxyType({
    "d0": 0,
    "d1": 1,
    "d2": 2,
})

# Cycles each provider actually publishes, verified against the live archive
# directories rather than assumed from the model's nominal schedule.  ECCC's
# 06Z and 18Z paths return 404; the other three publish all four.
PUBLISHED_RUN_CYCLE_HOURS: Mapping[str, tuple[int, ...]] = MappingProxyType({
    "ECMWF_IFS_ENS_OPEN_PF50": (0, 6, 12, 18),
    "NOAA_GEFS_05": (0, 6, 12, 18),
    "ECCC_GEM_GLOBAL_ENS": (0, 12),
    "DWD_ICON_GLOBAL_EPS": (0, 6, 12, 18),
})

# Widest civil-time offsets in use, in hours from UTC.  A run must still reach
# the end of the target local day at the most westward station.
MIN_UTC_OFFSET_HOURS = -12
MAX_UTC_OFFSET_HOURS = 14

# Longest forecast hour each provider publishes.  A cycle is only admissible
# if its horizon actually covers the target local day.
PUBLISHED_FORECAST_HORIZON_HOURS: Mapping[str, int] = MappingProxyType({
    "ECMWF_IFS_ENS_OPEN_PF50": 360,
    "NOAA_GEFS_05": 384,
    "ECCC_GEM_GLOBAL_ENS": 384,
    "DWD_ICON_GLOBAL_EPS": 180,
})


def lead_days(lead_bucket: str) -> int:
    """Days between the run day and the target local date."""

    try:
        return LEAD_DAYS[lead_bucket]
    except KeyError as exc:
        raise ContractError("native_run_cycle_lead_bucket_unknown") from exc


def published_cycles(system_id: str) -> tuple[int, ...]:
    try:
        return PUBLISHED_RUN_CYCLE_HOURS[system_id]
    except KeyError as exc:
        raise ContractError("native_run_cycle_system_unknown") from exc


def _covers_target_local_day(
    *,
    system_id: str,
    lead_bucket: str,
    cycle_hour: int,
) -> bool:
    """Whether a run at this cycle still reaches the end of the target day.

    The binding case is the most westward station: its local midnight falls
    latest in UTC, so it needs the longest forecast hour.
    """

    hours_to_run_end = lead_days(lead_bucket) * 24 - cycle_hour
    latest_needed = hours_to_run_end + 24 - MIN_UTC_OFFSET_HOURS
    return latest_needed <= PUBLISHED_FORECAST_HORIZON_HOURS[system_id]


def resolve_run_cycle_hour(*, system_id: str, lead_bucket: str) -> int:
    """The freshest run this system publishes at the given lead.

    Later cycles have seen more observations, so at a fixed lead the last
    published run of the run day is strictly the most informed choice.  Systems
    with no 18Z run fall back to their own latest, which is the whole reason
    this is derived rather than declared.
    """

    candidates = [
        cycle
        for cycle in published_cycles(system_id)
        if _covers_target_local_day(
            system_id=system_id,
            lead_bucket=lead_bucket,
            cycle_hour=cycle,
        )
    ]
    if not candidates:
        raise ContractError("native_run_cycle_no_covering_cycle")
    return max(candidates)


def resolve_run_cycle_label(*, system_id: str, lead_bucket: str) -> str:
    return f"{resolve_run_cycle_hour(
        system_id=system_id, lead_bucket=lead_bucket
    ):02d}Z"


def resolve_run_init_utc(
    *,
    system_id: str,
    lead_bucket: str,
    target_local_date: str,
) -> datetime:
    """The exact run this system should be read from for the target date."""

    target = date.fromisoformat(target_local_date)
    cycle_hour = resolve_run_cycle_hour(
        system_id=system_id, lead_bucket=lead_bucket
    )
    return datetime.combine(
        target - timedelta(days=lead_days(lead_bucket)),
        time(hour=cycle_hour),
        tzinfo=timezone.utc,
    )


def information_deficit_hours(
    *,
    system_id: str,
    lead_bucket: str,
    reference_system_id: str,
) -> int:
    """How much older this system's run is than the reference system's.

    A non-zero deficit means the two systems are not being compared on equal
    information, which is a property of the publication schedule rather than of
    forecast skill.  Surfacing it keeps that asymmetry auditable in any weight
    or promotion decision that ranks them against each other.
    """

    mine = resolve_run_cycle_hour(
        system_id=system_id, lead_bucket=lead_bucket
    )
    theirs = resolve_run_cycle_hour(
        system_id=reference_system_id, lead_bucket=lead_bucket
    )
    return theirs - mine
