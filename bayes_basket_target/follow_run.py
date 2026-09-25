"""Charter 192: ICON and ECCC follow their newest published run, every lead.

User rulings 2026-09-21: "全球模型的 run 全采，按起报时刻滚动用，谁新用谁（只要覆盖
得到目标日）；新轮次没发布前不倒退", revised the same evening to "当天、明天、
后天都谁新用谁".  ECMWF and GEFS already walk a six-hourly ladder; ICON and ECCC
read one contracted cycle per lead (00Z at d1 and d0, 12Z at d2), so a D+1
packet sat on the day's 00Z ICON/ECCC run for up to a day while ICON issued
three newer runs and ECCC one, and a D+2 packet had no ICON/ECCC at all from
its listing (~05Z D-2) until D-2 12Z landed.

What this module owns, and why each piece is shaped the way it is:

* **Only added, never swapped.**  The lead's own cycle is captured, trained
  and served exactly as before; the other cycles are *extra* captures, written
  to their own files (``{family}_{date}_d{lead}_{HH}z.json`` -- a name
  charter 148's other-lead hold never matches) under their own extraction
  identity.  The ~30 days of 00Z history keep their scope untouched.
* **A followed run serves only with its own certificate.**  Per station, a
  followed run replaces the run the station already holds only when an exact
  single-system calibration activation exists for that station, that run's
  own hour bucket and its own cycle -- the key charter 149's first step reads
  -- and the fit behind it has at least ``FOLLOW_RUN_MIN_FIT_CASES`` cases.
  Anything less leaves the station on the run it would have served anyway,
  so charter 101/149's cross-cycle borrow is never how a followed run gets
  seated.
* **Never older.**  The run the pass already chose (captured, reused or held)
  is always a candidate and a followed run must be strictly newer, so no
  station can end up on an older run than before.
* **ICON's 6-hourly cycles are seated like the others** (user ruling
  2026-09-21/22), on the same certificate; ``PM_FOLLOW_SECONDARY_CYCLES=off``
  holds them back (see ``SECONDARY_SERVING_ENV``).

The minimum fit size is measured, not assumed: walked forward with
production's own fit/apply/score functions over 08-15..09-20, ECMWF's and
GEFS's 12Z-vs-00Z d1 runs gained RPS -6.1% / -3.9% once each slot's
calibration rested on >=10 days, but only -1.7% / -1.1% (both intervals across
zero, NLL worse) at the OOS gate's 5+3 floor -- a thin fit gives back the
freshness it was meant to buy, and here a mature fit on the older run is
always there to fall back on.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .contracts import content_sha256
from .dwd_icon_eps_archive import (
    ARCHIVE_RUN_CYCLE_HOURS_BY_LEAD as ICON_ARCHIVE_CYCLES_BY_LEAD,
    MULTISTATION_SCHEMA as ICON_MULTISTATION_SCHEMA,
    RUN_CYCLE_HOUR_BY_LEAD as ICON_CYCLE_BY_LEAD,
    SECONDARY_CYCLE_HOURS as ICON_SECONDARY_CYCLE_HOURS,
    SYSTEM_ID as ICON_SYSTEM_ID,
)
from .ecmwf_ifs_archive import (
    FOLLOW_ONLY_LEAD_CYCLES as ECMWF_FOLLOW_ONLY_LEAD_CYCLES,
    MULTISTATION_SCHEMA as ECMWF_MULTISTATION_SCHEMA,
    SYSTEM_ID as ECMWF_SYSTEM_ID,
)
from .eccc_geps_archive import (
    ARCHIVE_RUN_CYCLE_HOURS_BY_LEAD as ECCC_ARCHIVE_CYCLES_BY_LEAD,
    MULTISTATION_SCHEMA as ECCC_MULTISTATION_SCHEMA,
    RUN_CYCLE_HOUR_BY_LEAD as ECCC_CYCLE_BY_LEAD,
    SYSTEM_ID as ECCC_SYSTEM_ID,
)
from .dwd_icon_eps_archive import (
    _forecast_hours as _icon_own_cycle_hours,
    follow_forecast_hours as _icon_follow_hours,
)
from .eccc_geps_archive import (
    _forecast_hours as _eccc_own_cycle_hours,
    follow_forecast_hours as _eccc_follow_hours,
)
from .ecmwf_aifs_ens_archive import (
    MULTISTATION_SCHEMA as AIFS_MULTISTATION_SCHEMA,
    SUPPORTED_LEAD_DAYS as AIFS_LEADS,
    SUPPORTED_RUN_CYCLE_HOURS as AIFS_CYCLE_HOURS,
    SYSTEM_ID as AIFS_SYSTEM_ID,
    day_instants as _aifs_day_instants,
)
from .noaa_aigefs_archive import (
    MULTISTATION_SCHEMA as AIGEFS_MULTISTATION_SCHEMA,
    SUPPORTED_LEAD_DAYS as AIGEFS_LEADS,
    SUPPORTED_RUN_CYCLE_HOURS as AIGEFS_CYCLE_HOURS,
    SYSTEM_ID as AIGEFS_SYSTEM_ID,
)
from .native_archive_lead import capture_lead_bucket
from .resource_bounds import load_bounded_json
from .station_season import station_season_bin


ICON_FAMILY = "DWD_OPENDATA_ICON_EPS"
ECCC_FAMILY = "ECCC_DD_WEATHER"
ECMWF_FAMILY = "ECMWF_OPEN_DATA_S3"
FOLLOW_FAMILIES = (ICON_FAMILY, ECCC_FAMILY)
# Charter 204: AIFS ENS.  A lane key, not a transport id -- it shares PF50's
# transport (``ECMWF_OPEN_DATA_S3``) and must not be confused with it.
AIFS_FAMILY = "ECMWF_AIFS_ENS"
# Charter 212: NOAA AIGEFS, a lane key too (its rows carry the NOAA NODD
# transport id the GEFS lane uses).
AIGEFS_FAMILY = "NOAA_AIGEFS_NODD"
# Charter 193 amendment (2026-09-21, user ruling "A 维持现状 / C 先用 ECMWF 前一天
# 18Z"): runs a family follows per event date rather than per ladder rung.
# ECMWF walks the ladder itself; the one run it does not reach is D-3 18Z for
# a D+2 day -- the newest ECMWF run covering that day at its listing (~05Z
# D-2), while D-2 00Z is complete on the replica only around 08:10Z.  It is
# banked here for training in its own scope; seating it waits for its own
# certificate like every follow run.
EVENT_RUN_FAMILIES = (ECMWF_FAMILY, AIFS_FAMILY, AIGEFS_FAMILY)
_EVENT_RUNS = {
    ECMWF_FAMILY: tuple(
        (int(lead), int(hour))
        for lead, hours in sorted(ECMWF_FOLLOW_ONLY_LEAD_CYCLES.items())
        for hour in hours
    ),
    # Charter 204: AIFS ENS has no rung of its own -- every run at every lead
    # is followed per event date, at its own publication (init + ~6h10m on
    # 2026-09-20, all four cycles; 1h08m-1h41m before the IFS run of the same
    # cycle), and the newest certified one seats.  User ruling 2026-09-22:
    # 「全采全入座，谁新用谁，用最新一轮」.
    AIFS_FAMILY: tuple(
        (int(lead), int(hour)) for lead in AIFS_LEADS for hour in AIFS_CYCLE_HOURS
    ),
    # Charter 212: AIGEFS the same way, at its own publication (every step
    # of a run lands in one minute, init + 6h21m..8h11m over 36 runs, 35 of
    # them within 6h58m).
    AIGEFS_FAMILY: tuple(
        (int(lead), int(hour)) for lead in AIGEFS_LEADS for hour in AIGEFS_CYCLE_HOURS
    ),
}
# Families whose every run is followed: a same-day run can land in the bucket
# a longer lead's run of the same cycle already trains, so the generic
# ``scope_trained_by_longer_lead`` walk applies to them, unlike ECMWF's
# single follow-only lead.
_ALL_RUN_FAMILIES = (AIFS_FAMILY, AIGEFS_FAMILY)
# Every lead the ladder serves (user ruling the same evening: "当天、明天、后天
# 都谁新用谁").  At d2 the lead's own run stays D-2 12Z; the D-2 00Z/06Z runs
# are what can seat ICON/ECCC in a D+2 packet from its listing (~05Z D-2)
# instead of from ~15-17Z, and 18Z follows 12Z.
FOLLOW_LEADS = (0, 1, 2)
FOLLOW_RUN_MIN_FIT_CASES = 10
# ... and those cases have to be recent.  ``train_n_effective`` is the fit's
# Kish size under the bias half-life (5 days): ten consecutive days give 8.7,
# eight give 7.3.  The count alone would pass a scope revived after a gap --
# ECCC's D-2 00Z identity was last written 2026-08-12, so its first new days
# would sit beside twenty stale ones and read as thirty cases.
FOLLOW_RUN_MIN_EFFECTIVE_CASES = 8.5
# Kill switch, read by the worker: "off" restores the pre-charter capture and
# packet byte for byte.
FOLLOW_ENV = "PM_FOLLOW_NEW_RUNS"
# ICON's 06Z/18Z runs are seated on the same certificate as every other
# follow run (user ruling 2026-09-21/22, "ICON 的 06Z/18Z 允许上座"; default on).
# What that knowingly accepts: their t_2m is 6-hourly while every other seated
# family seals a 3-hourly curve, and the P0 root curve is the intersection of
# the seated families' valid times, so a seated 6-hour member halves the curve
# P1's path features read (4 points a day instead of 8) and on a late same-day
# pass can leave fewer than the 4 it needs, dropping it; the d1 06Z slot also
# measured no gain on the proxies (ECMWF -1.1%, GEFS +2.7%).  "off" holds the
# two cycles back while still capturing and training them.
SECONDARY_SERVING_ENV = "PM_FOLLOW_SECONDARY_CYCLES"
# Hours after init before a follow capture is worth asking for.  Measured on
# the open-data listing 2026-09-21: ICON 06Z objects landed 08:41-08:48Z and
# 12Z ones 14:42-14:49Z (about 2h45m); ECCC's 12Z uploads around 17:10Z.  An
# early ask costs a round of 404s, nothing more.
# ECMWF's margin is the scheduler's (``D2_PUBLICATION_MARGIN_HOURS``); its 18Z
# is on the replica well inside it.
FOLLOW_PUBLICATION_LAG_HOURS = {
    ICON_FAMILY: 2.5,
    ECCC_FAMILY: 5.0,
    ECMWF_FAMILY: 8.0,
    # Measured 2026-09-20 on the official bucket: every step of all four
    # AIFS ENS cycles was up by init + 6h10m (00Z 05:59-06:09, 06Z
    # 11:55-12:04, 12Z 17:58-18:07, 18Z 23:51-00:00).
    AIFS_FAMILY: 6.25,
    # 35 of 36 measured runs were complete by init + 6h58m; the probe sees
    # the rest land (charter 195), this is only the fallback.
    AIGEFS_FAMILY: 7.0,
}
# Charter 212 kill switch, the AIFS one's twin.
AIGEFS_SERVING_ENV = "PM_SEAT_AIGEFS"
# Charter 204 kill switch: "off" keeps capturing and training AIFS ENS but
# seats none of its runs, so the packet is the one this pass always built.
AIFS_SERVING_ENV = "PM_SEAT_AIFS_ENS"

_PREFIX = {
    ICON_FAMILY: "icon", ECCC_FAMILY: "eccc", ECMWF_FAMILY: "ecmwf",
    AIFS_FAMILY: "aifs", AIGEFS_FAMILY: "aigefs",
}
_SYSTEM = {
    ICON_FAMILY: ICON_SYSTEM_ID,
    ECCC_FAMILY: ECCC_SYSTEM_ID,
    ECMWF_FAMILY: ECMWF_SYSTEM_ID,
    AIFS_FAMILY: AIFS_SYSTEM_ID,
    AIGEFS_FAMILY: AIGEFS_SYSTEM_ID,
}
_SCHEMA = {
    ICON_FAMILY: ICON_MULTISTATION_SCHEMA,
    ECCC_FAMILY: ECCC_MULTISTATION_SCHEMA,
    ECMWF_FAMILY: ECMWF_MULTISTATION_SCHEMA,
    AIFS_FAMILY: AIFS_MULTISTATION_SCHEMA,
    AIGEFS_FAMILY: AIGEFS_MULTISTATION_SCHEMA,
}
_ARCHIVE_CYCLES = {
    ICON_FAMILY: ICON_ARCHIVE_CYCLES_BY_LEAD,
    ECCC_FAMILY: ECCC_ARCHIVE_CYCLES_BY_LEAD,
    # Only the follow-only lead: nothing else of ECMWF's is followed here.
    ECMWF_FAMILY: {
        int(lead): tuple(int(hour) for hour in hours)
        for lead, hours in ECMWF_FOLLOW_ONLY_LEAD_CYCLES.items()
    },
    AIFS_FAMILY: {int(lead): tuple(AIFS_CYCLE_HOURS) for lead in AIFS_LEADS},
    AIGEFS_FAMILY: {int(lead): tuple(AIGEFS_CYCLE_HOURS) for lead in AIGEFS_LEADS},
}
_DEFAULT_CYCLE = {ICON_FAMILY: ICON_CYCLE_BY_LEAD, ECCC_FAMILY: ECCC_CYCLE_BY_LEAD}
# The ECMWF/GEFS ladder's own reading of a rung that names no cycle
# (``production_worker._official_native_run_ladder``).
_RUNG_DEFAULT_CYCLE = {2: 18, 1: 0, 0: 0}


def follow_enabled() -> bool:
    return (
        os.environ.get(FOLLOW_ENV, "on").strip().lower()
        not in {"0", "off", "false", "no"}
    )


def follow_serving_allowed(family: str, cycle_hour: int) -> bool:
    """Whether a followed cycle may be seated at all (its certificate aside)."""

    if family == ICON_FAMILY and int(cycle_hour) in ICON_SECONDARY_CYCLE_HOURS:
        return (
            os.environ.get(SECONDARY_SERVING_ENV, "on").strip().lower()
            not in {"0", "off", "false", "no"}
        )
    if family == AIFS_FAMILY:
        return (
            os.environ.get(AIFS_SERVING_ENV, "on").strip().lower()
            not in {"0", "off", "false", "no"}
        )
    if family == AIGEFS_FAMILY:
        return (
            os.environ.get(AIGEFS_SERVING_ENV, "on").strip().lower()
            not in {"0", "off", "false", "no"}
        )
    return True


def system_id(family: str) -> str:
    return _SYSTEM[family]


def archive_schema(family: str) -> str:
    return _SCHEMA[family]


def event_follow_runs(family: str) -> tuple[tuple[int, int], ...]:
    """The (lead, cycle) runs a family follows per event date, off the ladder."""

    return _EVENT_RUNS.get(family, ())


def follow_cycle_hours(family: str, lead_days: int) -> tuple[int, ...]:
    """The cycles a family is followed at on this lead, beyond its own."""

    lead = int(lead_days)
    if family in _EVENT_RUNS:
        return tuple(
            hour for run_lead, hour in _EVENT_RUNS[family] if run_lead == lead
        )
    if lead not in FOLLOW_LEADS:
        return ()
    default = _DEFAULT_CYCLE[family][lead]
    return tuple(
        int(hour)
        for hour in _ARCHIVE_CYCLES[family][lead]
        if int(hour) != int(default)
    )


def follow_cycle_for_rung(
    family: str, lead_days: int, rung_cycle_hour: int | None
) -> int | None:
    """The followed cycle a ladder rung reaches for this family, or None.

    The rung names the ECMWF/GEFS cycle; a family reads the newest of its own
    published cycles at or before it.  ECCC publishes no 06Z or 18Z, so an
    18Z rung reaches its 12Z.  When that newest cycle is the lead's own, the
    pass already reads it and there is nothing to follow.
    """

    lead = int(lead_days)
    if lead not in FOLLOW_LEADS or family in _EVENT_RUNS:
        return None
    rung = (
        _RUNG_DEFAULT_CYCLE.get(lead, 0)
        if rung_cycle_hour is None
        else int(rung_cycle_hour)
    )
    published = [
        int(hour) for hour in _ARCHIVE_CYCLES[family][lead] if int(hour) <= rung
    ]
    if not published:
        return None
    newest = max(published)
    return None if newest == int(_DEFAULT_CYCLE[family][lead]) else newest


def follow_run_init(
    event_date: str, lead_days: int, cycle_hour: int
) -> datetime:
    return datetime.combine(
        date.fromisoformat(str(event_date)) - timedelta(days=int(lead_days)),
        time(hour=int(cycle_hour)),
        tzinfo=timezone.utc,
    )


def follow_archive_path(
    root: Path, family: str, event_date: str, lead_days: int, cycle_hour: int
) -> Path:
    return Path(root) / (
        f"{_PREFIX[family]}_{event_date}_d{int(lead_days)}_"
        f"{int(cycle_hour):02d}z.json"
    )


def follow_archive_paths(
    root: Path, family: str, event_date: str
) -> list[tuple[datetime, Path]]:
    """Every follow file the event date may have, newest run first."""

    leads = (
        sorted({lead for lead, _hour in _EVENT_RUNS[family]})
        if family in _EVENT_RUNS
        else FOLLOW_LEADS
    )
    out = [
        (
            follow_run_init(event_date, lead, hour),
            follow_archive_path(root, family, event_date, lead, hour),
        )
        for lead in leads
        for hour in follow_cycle_hours(family, lead)
    ]
    out.sort(key=lambda item: item[0], reverse=True)
    return out


def follow_publication_due(
    family: str,
    run_init: datetime,
    *,
    as_of: datetime,
    event_date: str | None = None,
    probe_state: Mapping[str, Any] | None = None,
) -> bool:
    """Whether the followed run is worth asking the server for.

    Charter 195 (二): the arrival probe's answer when it can give one -- the
    run has landed for this day, or has not -- and the fixed lag otherwise
    (no fresh probe state, a day it does not list, a run outside its window).
    """

    from .nwp_arrival_probe import run_landed  # noqa: PLC0415

    landed = run_landed(
        probe_state,
        family=family,
        run_init=run_init,
        event_date=event_date,
        as_of=as_of,
    )
    if landed is not None:
        return landed
    return as_of >= run_init + timedelta(
        hours=FOLLOW_PUBLICATION_LAG_HOURS[family]
    )


def _json(value: Any) -> str:
    # Byte-identical to ``native_ensemble_p0._json``: the activation key is
    # compared as text.
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _run_covers(
    family: str,
    *,
    event_date: str,
    settlement_timezone: str,
    lead_days: int,
    run_init: datetime,
) -> bool:
    """Whether this family's capture of that run seals this station's day."""

    if family in (AIFS_FAMILY, AIGEFS_FAMILY):
        # Both publish six-hourly instants under the same in-day floor.
        return _aifs_day_instants(
            event_date=event_date,
            settlement_timezone=settlement_timezone,
            run_init_utc=run_init,
        ) is not None
    if int(run_init.hour) == int(_DEFAULT_CYCLE[family][int(lead_days)]):
        own = (
            _icon_own_cycle_hours
            if family == ICON_FAMILY
            else _eccc_own_cycle_hours
        )
        try:
            own(
                event_date=event_date,
                settlement_timezone=settlement_timezone,
                run_init_utc=run_init,
            )
        except Exception:  # noqa: BLE001 - a short grid is "does not cover"
            return False
        return True
    seal = _icon_follow_hours if family == ICON_FAMILY else _eccc_follow_hours
    return seal(
        event_date=event_date,
        settlement_timezone=settlement_timezone,
        run_init_utc=run_init,
    ) is not None


def scope_trained_by_longer_lead(
    family: str,
    *,
    event_date: str,
    settlement_timezone: str,
    run_init: datetime,
) -> bool:
    """Whether this run's (hour bucket x cycle) scope trains on another run.

    One station reaches the same bucket at the same cycle from two calendar
    leads when a same-day run is issued twelve hours or more after the local
    day began: Europe's D 12Z (-10h to -14h) and D-1 12Z (+10h to +14h) are
    both ``h00_12``.  That scope's certificate describes the earlier run, not
    a same-day run that only sees the afternoon, so such a run is not seated
    on it -- and, for the families whose every run is seated through here,
    the OOS reader leaves it out of that scope's training too
    (``live_never_seats``, charter 219).  It used to rely on keeping the
    first-captured case per scope and day, which only holds for a forward
    archive: backfilled AIFS ENS and AIGEFS history lands both runs at once,
    and the same-day run won 58-59 of 60 days on every station checked.
    """

    run = run_init.astimezone(timezone.utc)
    lead = (date.fromisoformat(str(event_date)) - run.date()).days
    try:
        bucket = capture_lead_bucket(
            event_date=str(event_date),
            settlement_timezone=str(settlement_timezone),
            run_init=run,
        )
    except Exception:  # noqa: BLE001 - unkeyable: treat as not ours to seat
        return True
    if family in _EVENT_RUNS and family not in _ALL_RUN_FAMILIES:
        # A follow-only lead is longer than every lead the ladder reads, and
        # its own scope is the only one it can reach at its cycle.
        return False
    for longer in range(lead + 1, max(_ARCHIVE_CYCLES[family]) + 1):
        if run.hour not in _ARCHIVE_CYCLES[family].get(longer, ()):
            continue
        other = follow_run_init(event_date, longer, run.hour)
        if not _run_covers(
            family,
            event_date=str(event_date),
            settlement_timezone=str(settlement_timezone),
            lead_days=longer,
            run_init=other,
        ):
            continue
        if capture_lead_bucket(
            event_date=str(event_date),
            settlement_timezone=str(settlement_timezone),
            run_init=other,
        ) == bucket:
            return True
    return False


def live_never_seats(
    system: str,
    *,
    event_date: str,
    settlement_timezone: str,
    run_init: datetime,
) -> bool:
    """Whether live seating can never put this run on its own scope.

    Charter 219.  True only for the families whose every run is seated
    through ``follow_serving_ready`` (``_ALL_RUN_FAMILIES``), and only when
    that function's first check -- ``scope_trained_by_longer_lead`` -- turns
    the run away.  A family with a ladder rung can still seat the same run as
    its base, so for it nothing is ruled out here.  The OOS reader drops
    exactly these runs from training, so a scope trains on what it serves.
    """

    family = next(
        (name for name in _ALL_RUN_FAMILIES if _SYSTEM[name] == str(system)),
        None,
    )
    if family is None:
        return False
    return scope_trained_by_longer_lead(
        family,
        event_date=str(event_date),
        settlement_timezone=str(settlement_timezone),
        run_init=run_init,
    )


def follow_serving_ready(
    conn: sqlite3.Connection,
    *,
    family: str,
    station_id: str,
    event_date: str,
    settlement_timezone: str,
    run_init: datetime,
    as_of_utc: str,
) -> bool:
    """Whether a followed run has its own certified fit for this station.

    The same key charter 149's first step reads at compile -- station, system,
    season, the run's own hour bucket, its own cycle, the single-system cycle
    map, production authority, valid now, one top generation -- plus the fit
    behind it resting on at least ``FOLLOW_RUN_MIN_FIT_CASES`` cases whose
    recency-weighted size is at least ``FOLLOW_RUN_MIN_EFFECTIVE_CASES``, and
    that scope being trained on this run rather than on a longer-lead run
    that lands in the same bucket (``scope_trained_by_longer_lead``).
    """

    if scope_trained_by_longer_lead(
        family,
        event_date=str(event_date),
        settlement_timezone=str(settlement_timezone),
        run_init=run_init,
    ):
        return False
    system = _SYSTEM[family]
    cycle = f"{run_init.astimezone(timezone.utc).hour:02d}Z"
    try:
        lead_bucket = capture_lead_bucket(
            event_date=str(event_date),
            settlement_timezone=str(settlement_timezone),
            run_init=run_init,
        )
        season_bin = station_season_bin(str(station_id), str(event_date))
    except Exception:  # noqa: BLE001 - an unkeyable station keeps its run
        return False
    try:
        rows = conn.execute(
            "SELECT generation,artifact_id "
            "FROM calibration_native_ensemble_activation "
            "WHERE artifact_kind='NATIVE_ENSEMBLE_SYSTEM_CALIBRATION' "
            "AND station_id=? AND ensemble_system_id_or_star=? "
            "AND season_bin=? AND lead_bucket=? AND run_cycle=? "
            "AND available_system_set_hash_or_star='*' "
            "AND run_cycle_by_system_json=? "
            "AND run_cycle_by_system_sha256=? "
            "AND authority_scope='PRODUCTION_NO_LIVE_ONLY' "
            "AND valid_from_utc<=? AND valid_until_utc>? "
            "ORDER BY generation DESC LIMIT 2",
            (
                str(station_id),
                system,
                season_bin,
                lead_bucket,
                cycle,
                _json({system: cycle}),
                content_sha256({system: cycle}),
                str(as_of_utc),
                str(as_of_utc),
            ),
        ).fetchall()
        if not rows or (
            len(rows) > 1 and int(rows[0][0]) == int(rows[1][0])
        ):
            return False
        artifact = conn.execute(
            "SELECT json_extract(reliability_parameters_json,"
            "'$.fit_case_count'),"
            "json_extract(reliability_parameters_json,"
            "'$.train_n_effective') "
            "FROM calibration_native_ensemble_system_artifact "
            "WHERE system_artifact_id=? AND ensemble_system_id=? "
            "AND lead_bucket=? AND run_cycle=? "
            "AND valid_from_utc<=? AND valid_until_utc>?",
            (
                str(rows[0][1]),
                system,
                lead_bucket,
                cycle,
                str(as_of_utc),
                str(as_of_utc),
            ),
        ).fetchone()
    except sqlite3.Error:
        return False
    if artifact is None or artifact[0] is None or artifact[1] is None:
        return False
    try:
        mature = (
            int(artifact[0]) >= FOLLOW_RUN_MIN_FIT_CASES
            and float(artifact[1]) >= FOLLOW_RUN_MIN_EFFECTIVE_CASES
        )
    except (TypeError, ValueError):
        return False
    if not mature:
        return False
    return _same_grid_weights_exist(
        conn,
        family=family,
        station_id=str(station_id),
        season_bin=season_bin,
        lead_bucket=lead_bucket,
        run_hour=int(run_init.astimezone(timezone.utc).hour),
        as_of_utc=str(as_of_utc),
    )


def _same_grid_weights_exist(
    conn: sqlite3.Connection,
    *,
    family: str,
    station_id: str,
    season_bin: str,
    lead_bucket: str,
    run_hour: int,
    as_of_utc: str,
) -> bool:
    """Charter 193 (三): a coarse-grid run needs cross weights on its own grid.

    ICON's 06Z/18Z read t_2m 6-hourly.  P0 never seats a member on weights
    fitted with it at a 3-hourly cycle (``native_ensemble_p0`` passes such
    activations over), so seating one where no pool carries ICON at 06Z/18Z
    would drop ICON from P0 instead of serving its 3-hourly run.  Other
    families and ICON's 00Z/12Z are single-grid with what they replace: True.
    """

    if family != ICON_FAMILY or run_hour not in ICON_SECONDARY_CYCLE_HOURS:
        return True
    system = _SYSTEM[family]
    patterns = [
        f'%"{system}":"{int(hour):02d}Z"%' for hour in ICON_SECONDARY_CYCLE_HOURS
    ]
    try:
        row = conn.execute(
            "SELECT 1 FROM calibration_native_ensemble_activation "
            "WHERE artifact_kind='CROSS_NATIVE_ENSEMBLE' "
            "AND station_id=? AND season_bin=? AND lead_bucket=? "
            "AND authority_scope='PRODUCTION_NO_LIVE_ONLY' "
            "AND valid_from_utc<=? AND valid_until_utc>? AND ("
            + " OR ".join("run_cycle_by_system_json LIKE ?" for _ in patterns)
            + ") LIMIT 1",
            (station_id, season_bin, lead_bucket, as_of_utc, as_of_utc, *patterns),
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def readiness_from_database(
    database: Path, *, as_of_utc: str
) -> Callable[..., bool]:
    """A ``follow_serving_ready`` bound to a read-only connection."""

    conn = sqlite3.connect(
        f"file:{Path(database).expanduser().resolve()}?mode=ro",
        uri=True,
        timeout=30.0,
    )
    conn.execute("PRAGMA query_only=1")

    def ready(**kwargs: Any) -> bool:
        return follow_serving_ready(conn, as_of_utc=as_of_utc, **kwargs)

    ready.close = conn.close  # type: ignore[attr-defined]
    return ready


def choose_station_run(
    *,
    base_run_init: datetime | None,
    candidates: list[tuple[datetime, Any]],
    ready: Callable[[datetime, Any], bool],
) -> Any | None:
    """The newest certified followed run strictly newer than the base, or None.

    ``None`` means the station keeps the run the pass already chose for it.
    Candidates are tried newest first, so an uncertified newest run falls
    through to a certified older follow before falling back to the base.
    """

    for run_init, key in sorted(
        candidates, key=lambda item: item[0], reverse=True
    ):
        if base_run_init is not None and run_init <= base_run_init:
            break
        if ready(run_init, key):
            return key
    return None


def load_follow_archive(
    path: Path, *, family: str, event_date: str, run_init: datetime
) -> Mapping[str, Any] | None:
    """A retained follow archive, or None if absent, altered or another run."""

    candidate = Path(path)
    if candidate.is_symlink() or not candidate.is_file():
        return None
    try:
        payload = load_bounded_json(candidate)
    except Exception:  # noqa: BLE001 - unreadable is absent
        return None
    if not isinstance(payload, dict):
        return None
    identity = {
        key: value for key, value in payload.items() if key != "content_sha256"
    }
    captures = payload.get("captures")
    if (
        payload.get("schema") != _SCHEMA[family]
        or payload.get("content_sha256") != content_sha256(identity)
        or str(payload.get("event_date") or "") != str(event_date)
        or payload.get("training_only") is not True
        or payload.get("holdout_eligible") is not False
        or not isinstance(captures, list)
        or not captures
    ):
        return None
    try:
        held = datetime.fromisoformat(
            str(payload.get("run_init_utc") or "").replace("Z", "+00:00")
        ).astimezone(timezone.utc)
    except ValueError:
        return None
    if held != run_init.astimezone(timezone.utc):
        return None
    return payload


def archive_station_ids(archive: Mapping[str, Any]) -> set[str]:
    return {
        str(row.get("station_id") or "").strip().upper()
        for row in archive.get("captures") or []
        if isinstance(row, Mapping) and str(row.get("station_id") or "").strip()
    }


__all__ = [
    "AIFS_FAMILY",
    "AIFS_SERVING_ENV",
    "AIGEFS_FAMILY",
    "AIGEFS_SERVING_ENV",
    "ECCC_FAMILY",
    "ECMWF_FAMILY",
    "EVENT_RUN_FAMILIES",
    "FOLLOW_ENV",
    "FOLLOW_FAMILIES",
    "FOLLOW_LEADS",
    "FOLLOW_RUN_MIN_EFFECTIVE_CASES",
    "FOLLOW_RUN_MIN_FIT_CASES",
    "ICON_FAMILY",
    "archive_schema",
    "archive_station_ids",
    "choose_station_run",
    "event_follow_runs",
    "follow_archive_path",
    "follow_archive_paths",
    "follow_cycle_for_rung",
    "follow_cycle_hours",
    "follow_enabled",
    "follow_publication_due",
    "follow_run_init",
    "follow_serving_allowed",
    "follow_serving_ready",
    "live_never_seats",
    "load_follow_archive",
    "readiness_from_database",
    "scope_trained_by_longer_lead",
    "system_id",
]
