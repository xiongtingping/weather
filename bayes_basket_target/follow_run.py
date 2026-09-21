"""Charter 192: ICON and ECCC follow their newest published run at d1 and d0.

User ruling 2026-09-21: "全球模型的 run 全采，按起报时刻滚动用，谁新用谁（只要覆盖
得到目标日）；新轮次没发布前不倒退；后天（D+2）不动".  ECMWF and GEFS already walk
a six-hourly ladder; ICON and ECCC read one contracted cycle per lead (00Z at
d1 and d0), so a D+1 packet sat on the day's 00Z ICON/ECCC run for up to a
day while ICON issued three newer runs and ECCC one.

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
* **ICON's 6-hourly cycles wait for a ruling.**  06Z/18Z are captured and
  trained, but seated only under ``PM_FOLLOW_SECONDARY_CYCLES`` (see
  ``SECONDARY_SERVING_ENV``).

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
from .eccc_geps_archive import (
    ARCHIVE_RUN_CYCLE_HOURS_BY_LEAD as ECCC_ARCHIVE_CYCLES_BY_LEAD,
    MULTISTATION_SCHEMA as ECCC_MULTISTATION_SCHEMA,
    RUN_CYCLE_HOUR_BY_LEAD as ECCC_CYCLE_BY_LEAD,
    SYSTEM_ID as ECCC_SYSTEM_ID,
)
from .native_archive_lead import capture_lead_bucket
from .resource_bounds import load_bounded_json
from .station_season import station_season_bin


ICON_FAMILY = "DWD_OPENDATA_ICON_EPS"
ECCC_FAMILY = "ECCC_DD_WEATHER"
FOLLOW_FAMILIES = (ICON_FAMILY, ECCC_FAMILY)
# d2 is unchanged by the ruling: ICON and ECCC keep reading D-2 12Z there.
FOLLOW_LEADS = (0, 1)
FOLLOW_RUN_MIN_FIT_CASES = 10
# Kill switch, read by the worker: "off" restores the pre-charter capture and
# packet byte for byte.
FOLLOW_ENV = "PM_FOLLOW_NEW_RUNS"
# ICON's 06Z/18Z runs are captured and trained like the others, but seating
# one waits on a ruling (default off).  Their t_2m is 6-hourly, and every
# other seated family seals a 3-hourly curve: the P0 root curve is the
# intersection of the seated families' valid times, so one 6-hour member
# halves the curve P1's path features read (4 points a day instead of 8) and
# on a late same-day pass leaves fewer than the 4 it needs, dropping it.  The
# d1 06Z slot also measured no gain on the proxies (ECMWF -1.1%, GEFS +2.7%).
SECONDARY_SERVING_ENV = "PM_FOLLOW_SECONDARY_CYCLES"
# Hours after init before a follow capture is worth asking for.  Measured on
# the open-data listing 2026-09-21: ICON 06Z objects landed 08:41-08:48Z and
# 12Z ones 14:42-14:49Z (about 2h45m); ECCC's 12Z uploads around 17:10Z.  An
# early ask costs a round of 404s, nothing more.
FOLLOW_PUBLICATION_LAG_HOURS = {ICON_FAMILY: 2.5, ECCC_FAMILY: 5.0}

_PREFIX = {ICON_FAMILY: "icon", ECCC_FAMILY: "eccc"}
_SYSTEM = {ICON_FAMILY: ICON_SYSTEM_ID, ECCC_FAMILY: ECCC_SYSTEM_ID}
_SCHEMA = {
    ICON_FAMILY: ICON_MULTISTATION_SCHEMA,
    ECCC_FAMILY: ECCC_MULTISTATION_SCHEMA,
}
_ARCHIVE_CYCLES = {
    ICON_FAMILY: ICON_ARCHIVE_CYCLES_BY_LEAD,
    ECCC_FAMILY: ECCC_ARCHIVE_CYCLES_BY_LEAD,
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
            os.environ.get(SECONDARY_SERVING_ENV, "off").strip().lower()
            in {"1", "on", "true", "yes"}
        )
    return True


def system_id(family: str) -> str:
    return _SYSTEM[family]


def archive_schema(family: str) -> str:
    return _SCHEMA[family]


def follow_cycle_hours(family: str, lead_days: int) -> tuple[int, ...]:
    """The cycles a family is followed at on this lead, beyond its own."""

    lead = int(lead_days)
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
    """The newest followed cycle a ladder rung reaches for this family.

    The rung names the ECMWF/GEFS cycle; a family follows the newest of its
    own published cycles at or before it.  ECCC publishes no 06Z or 18Z, so
    an 18Z rung asks it for 12Z; a rung at or before the lead's own cycle
    asks nothing, because that run is already the pass's own.
    """

    lead = int(lead_days)
    rung = (
        _RUNG_DEFAULT_CYCLE.get(lead, 0)
        if rung_cycle_hour is None
        else int(rung_cycle_hour)
    )
    eligible = [
        hour for hour in follow_cycle_hours(family, lead) if hour <= rung
    ]
    return max(eligible) if eligible else None


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

    out = [
        (
            follow_run_init(event_date, lead, hour),
            follow_archive_path(root, family, event_date, lead, hour),
        )
        for lead in FOLLOW_LEADS
        for hour in follow_cycle_hours(family, lead)
    ]
    out.sort(key=lambda item: item[0], reverse=True)
    return out


def follow_publication_due(
    family: str, run_init: datetime, *, as_of: datetime
) -> bool:
    return as_of >= run_init + timedelta(
        hours=FOLLOW_PUBLICATION_LAG_HOURS[family]
    )


def _json(value: Any) -> str:
    # Byte-identical to ``native_ensemble_p0._json``: the activation key is
    # compared as text.
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
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
    behind it resting on at least ``FOLLOW_RUN_MIN_FIT_CASES`` cases.
    """

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
            "'$.fit_case_count') "
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
    if artifact is None or artifact[0] is None:
        return False
    try:
        return int(artifact[0]) >= FOLLOW_RUN_MIN_FIT_CASES
    except (TypeError, ValueError):
        return False


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
    "ECCC_FAMILY",
    "FOLLOW_ENV",
    "FOLLOW_FAMILIES",
    "FOLLOW_LEADS",
    "FOLLOW_RUN_MIN_FIT_CASES",
    "ICON_FAMILY",
    "archive_schema",
    "archive_station_ids",
    "choose_station_run",
    "follow_archive_path",
    "follow_archive_paths",
    "follow_cycle_for_rung",
    "follow_cycle_hours",
    "follow_enabled",
    "follow_publication_due",
    "follow_run_init",
    "follow_serving_allowed",
    "follow_serving_ready",
    "load_follow_archive",
    "readiness_from_database",
    "system_id",
]
