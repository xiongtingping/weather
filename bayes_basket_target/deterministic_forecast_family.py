"""Charter 201: the deterministic-forecast feature family.

User ruling 2026-09-23: 「区分 metar 和区域模型，metar 是观测，区域模型是预测，把
区域模型改为特征，不一定只进 p1 阶段，根据区域模型的 run 和发布时间，全部采集，谁新
就用谁……但是和全球成员模型区分开」.  So there are two kinds of evidence and they
never share a slot:

* **Observation** -- METAR/ASOS and the settlement truth: P1's hard floor and
  its observation likelihood, as before.
* **Deterministic forecast** -- regional, high-resolution and AI single-member
  runs.  Never ensemble members: not registered in
  ``calibration_native_ensemble_system``, no fusion weight, no seat.  Each is
  a *feature*: P1 reads two covariates off it (charter 150, pairs frozen in
  ``regional_features.P1_FEATURE_PAIRS``); P0 reads a station-level centre
  correction off it (step 2 records it as a shadow, step 3 serves the pairs
  that clear the gate or a ruling).

This module is the family's register and its one as-of read.  Capture lives
in ``regional_point_lane`` (one table, ``calibration_regional_point_run``,
one row = one run of one source for one station-day, the curve kept on valid
instants); every source's run is captured when current for every listed
target day it reaches (D-1 .. D+2), and ``feature_at`` hands a decision the
newest run knowable at its clock that covers the day's afternoon -- 「谁新就用
谁」, and never an older one than it could already read.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from .regional_point_lane import SYSTEM_IDS, runs_for

# The family: product key -> what it is.  ``relay`` sources reach us through
# the Open-Meteo aggregator (knowable = our fetch time; quota-charged);
# ``domain`` sources are captured only at the stations inside the model's
# own domain (their archive modules' rosters), ``global`` ones at every
# roster station with coordinates.
FAMILY: Mapping[str, Mapping[str, Any]] = {
    "HRRR": {"publisher": "NOAA/NCEP", "relay": False, "coverage": "domain", "step_hours": 1},
    "NBM": {"publisher": "NOAA/NCEP", "relay": False, "coverage": "domain", "step_hours": 1},
    "ICON_EU": {"publisher": "DWD", "relay": False, "coverage": "domain", "step_hours": 1},
    "MSM": {"publisher": "JMA (RISH mirror)", "relay": False, "coverage": "domain", "step_hours": 1},
    "CMA": {"publisher": "CMA", "relay": True, "coverage": "global", "step_hours": 1},
    "AIFS": {"publisher": "ECMWF", "relay": False, "coverage": "global", "step_hours": 6},
    "IFS9KM": {"publisher": "ECMWF", "relay": True, "coverage": "global", "step_hours": 1},
    "UKMO10": {"publisher": "Met Office", "relay": True, "coverage": "global", "step_hours": 1},
}
PRODUCT_BY_SYSTEM = {SYSTEM_IDS[product]: product for product in FAMILY}
FAMILY_SYSTEM_IDS = frozenset(PRODUCT_BY_SYSTEM)
# The local afternoon a day's maximum lives in (charter 95's peak window).
PEAK_START_HOUR, PEAK_END_HOUR = 11, 19


def global_roster(conn: sqlite3.Connection) -> list[tuple[str, float, float, str]]:
    """Every roster station with coordinates and a registry timezone.

    Fail-soft per station: a listed station without coordinates (or without a
    registry row) is left out, never guessed -- the same rule as every
    capture lane that reads the roster.
    """

    from .station_registry import _COORDINATES  # noqa: PLC0415

    zones = {
        str(station).upper(): str(zone)
        for station, zone in conn.execute(
            "SELECT station_id, timezone_name FROM calibration_station_registry"
        )
    }
    return [
        (station, float(lat), float(lon), zones[station])
        for station, (lat, lon) in sorted(_COORDINATES.items())
        if station in zones
    ]


def product_roster(
    conn: sqlite3.Connection, product: str,
) -> list[tuple[str, float, float, str]]:
    """The stations a product is captured at."""

    if FAMILY[product]["coverage"] == "global":
        return global_roster(conn)
    if product in ("HRRR", "NBM"):
        from .noaa_conus_archive import CAPTURE_STATIONS as conus  # noqa: PLC0415

        return list(conus[product])
    if product == "ICON_EU":
        from .dwd_icon_eu_point_archive import CAPTURE_STATIONS as icon  # noqa: PLC0415

        return list(icon)
    if product == "MSM":
        from .jma_msm_archive import CAPTURE_STATIONS as msm  # noqa: PLC0415

        return list(msm)
    raise KeyError(product)


def day_maximum(
    curve: Mapping[str, Any], *, event_date: date, settlement_timezone: str,
    step_hours: int = 1,
) -> tuple[float | None, bool]:
    """The curve's maximum over the local day, and whether it saw the afternoon.

    Complete = every valid instant the product publishes inside the local
    peak window (11:00-18:59) is on the curve -- all eight hours for an
    hourly product, every 6-hourly step that falls there for a 6-hourly one
    (at least one).  An incomplete day is never a maximum: "breakfast is not
    the day's high".
    """

    zone = ZoneInfo(settlement_timezone)
    start = datetime.combine(event_date, datetime.min.time(), tzinfo=zone)
    end = start + timedelta(days=1)
    values: dict[datetime, float] = {}
    for stamp, value in dict(curve).items():
        try:
            when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            number = float(value)
        except (TypeError, ValueError):
            continue
        if start <= when.astimezone(zone) < end:
            values[when] = number
    if not values:
        return None, False
    peak_start = start + timedelta(hours=PEAK_START_HOUR)
    peak_end = start + timedelta(hours=PEAK_END_HOUR)
    if int(step_hours) <= 1:
        wanted = [peak_start + timedelta(hours=h) for h in range(PEAK_END_HOUR - PEAK_START_HOUR)]
    else:
        wanted = []
        # The product's steps are whole UTC multiples of ``step_hours``.
        cursor = peak_start.astimezone(ZoneInfo("UTC")).replace(minute=0, second=0, microsecond=0)
        while cursor.hour % int(step_hours):
            cursor += timedelta(hours=1)
        while cursor < peak_end:
            if cursor >= peak_start:
                wanted.append(cursor)
            cursor += timedelta(hours=int(step_hours))
    have = {when.astimezone(ZoneInfo("UTC")) for when in values}
    complete = bool(wanted) and all(w.astimezone(ZoneInfo("UTC")) in have for w in wanted)
    return max(values.values()), complete


def feature_at(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    as_of_utc: str,
    settlement_timezone: str,
    systems: Sequence[str] | None = None,
    rows: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Per family source: the newest run knowable at ``as_of_utc`` whose curve
    covers the day's afternoon, and that run's local-day maximum.

    ``rows`` lets a caller that already read the station-day hand them in
    (one read per station-day, every decision resolving in memory);
    otherwise one ``runs_for`` read -- which applies charter 200's retired
    cells and evidence clocks.  Newest by run init: the set of knowable runs
    only grows with the clock, so the run a decision reads never goes back.
    """

    if rows is None:
        rows = runs_for(conn, station_id=station_id, event_date=event_date, as_of_utc=as_of_utc)
    wanted = set(systems) if systems is not None else set(FAMILY_SYSTEM_IDS)
    day = date.fromisoformat(str(event_date))
    out: dict[str, dict[str, Any]] = {}
    ordered = sorted(
        (row for row in rows if str(row.get("knowable_at_utc") or "") <= str(as_of_utc)),
        key=lambda row: str(row["run_init_utc"]), reverse=True,
    )
    for row in ordered:
        system = str(row["ensemble_system_id"])
        if system in out or system not in wanted or system not in PRODUCT_BY_SYSTEM:
            continue
        product = PRODUCT_BY_SYSTEM[system]
        value, complete = day_maximum(
            row["hourly_c"], event_date=day, settlement_timezone=settlement_timezone,
            step_hours=int(FAMILY[product]["step_hours"]),
        )
        if value is None or not complete:
            continue
        out[system] = {
            "product": product,
            "day_max_c": float(value),
            "run_init_utc": str(row["run_init_utc"]),
            "knowable_at_utc": str(row.get("knowable_at_utc") or ""),
            "lead_hours_to_local_day_start": float(row.get("lead_hours_to_local_day_start") or 0.0),
        }
    return out


__all__ = [
    "FAMILY", "FAMILY_SYSTEM_IDS", "PRODUCT_BY_SYSTEM", "day_maximum",
    "feature_at", "global_roster", "product_roster",
]
