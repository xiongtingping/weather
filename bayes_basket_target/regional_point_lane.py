"""Charter 95: the regional deterministic models as P1-stage evidence.

User rulings 2026-09-06: the regional models (JMA MSM, NCEP HRRR, NCEP NBM,
DWD ICON-EU) are moved to the P1 stage beside the METAR path -- never P0
fusion members -- captured at their own cadence, every run, into a table of
their own so nothing of this lane is confused with the P0 global-model
facts.  Each row is one run of one model for one station-day, with the
forecast curve over the local day kept hour by hour: the P1 question is
"how much more will it warm from here", and that is a curve question, not a
daily-maximum question.
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from .contracts import ContractError, content_sha256

TABLE = "calibration_regional_point_run"
SCHEMA_VERSION = "regional_point_run_v1"
DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
  regional_point_run_id TEXT PRIMARY KEY,
  ensemble_system_id TEXT NOT NULL,
  station_id TEXT NOT NULL,
  target_local_date TEXT NOT NULL,
  settlement_timezone TEXT NOT NULL,
  run_init_utc TEXT NOT NULL,
  run_cycle_hour INTEGER NOT NULL CHECK(run_cycle_hour BETWEEN 0 AND 23),
  lead_hours_to_local_day_start REAL NOT NULL,
  forecast_tmax_c REAL NOT NULL,
  remaining_peak_complete INTEGER NOT NULL CHECK(remaining_peak_complete IN (0,1)),
  hours_wanted INTEGER NOT NULL,
  hours_covered INTEGER NOT NULL,
  hourly_c_json TEXT NOT NULL,
  transport_provider_id TEXT NOT NULL,
  extraction_version TEXT NOT NULL,
  archive_query_at_utc TEXT NOT NULL,
  knowable_at_utc TEXT NOT NULL,
  source_url TEXT NOT NULL,
  schema_version TEXT NOT NULL,
  content_sha256 TEXT NOT NULL UNIQUE CHECK(length(content_sha256)=64),
  UNIQUE(ensemble_system_id, station_id, target_local_date, run_init_utc)
);
CREATE INDEX IF NOT EXISTS {TABLE}_scope_idx
  ON {TABLE}(station_id, target_local_date, archive_query_at_utc);
"""
SYSTEM_IDS = {
    "MSM": "JMA_MSM_POINT",
    "CMA": "CMA_GRAPES_GLOBAL_POINT",
    "HRRR": "NCEP_HRRR_POINT",
    "NBM": "NCEP_NBM_POINT",
    "ICON_EU": "DWD_ICON_EU_POINT",
}
TRANSPORTS = {
    "MSM": "RISH_KYOTO_JMA_ARCHIVE",
    "CMA": "OPEN_METEO_CMA_GRAPES_GLOBAL",
    "HRRR": "NOAA_HRRR_PDS_S3",
    "NBM": "NOAA_NBM_PDS_S3",
    "ICON_EU": "DWD_OPENDATA_ICON_EU",
}
EXTRACTION_VERSIONS = {
    "MSM": "jma_msm_surface_nearest_cell_v1",
    # "derived_cycle" is the contract, not a caveat: the aggregator serving
    # this product publishes no run stamp, so the row's cycle label is
    # inferred from the product's own cadence.  What the replay actually
    # filters on -- knowable_at_utc -- is the fetch clock and is exact.
    "CMA": "cma_grapes_global_open_meteo_derived_cycle_v1",
    "HRRR": "ncep_hrrr_surface_nearest_cell_v1",
    "NBM": "ncep_nbm_core_nearest_cell_v1",
    "ICON_EU": "dwd_icon_eu_surface_nearest_cell_v1",
}
# Every cycle the model actually issues.
RUN_CYCLE_HOURS = {
    "MSM": tuple(range(0, 24, 3)),
    "CMA": (0, 6, 12, 18),
    "HRRR": tuple(range(0, 24)),
    "NBM": tuple(range(0, 24)),
    "ICON_EU": tuple(range(0, 24, 3)),
}
# How long after its nominal hour a run is normally on the archive.
PUBLICATION_LAG_HOURS = {
    "MSM": 3.0, "HRRR": 1.5, "NBM": 2.0, "ICON_EU": 4.0, "CMA": 4.0,
}
# The local afternoon window the P1 question is about.
PEAK_LOCAL_HOURS = tuple(range(11, 19))
# A live sweep fetches runs issued within the last day or so; a fetch further
# behind its own run is a backfill, whatever clock it ran on.  Used only to
# repair rows written before ``knowable_at_utc`` existed -- new rows are
# stamped by the writer, which knows which pass it is.
BACKFILL_FETCH_LAG_HOURS = 36.0
DEFAULT_PUBLICATION_LAG_HOURS = 2.0


def ensure_schema(conn: sqlite3.Connection) -> bool:
    """Create the lane's table once; True when it was created now.

    Also adds ``knowable_at_utc`` to a table created before it existed: the
    column is when the archive first held this run, which is what an as-of
    replay must filter on.  ``archive_query_at_utc`` stays what it always
    was, the instant this process fetched it -- for a live sweep the two are
    minutes apart, for a backfill they are weeks, and filtering a replay on
    the fetch time hides every backfilled run behind its own repair date.
    """

    present = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
    ).fetchone()
    if not present:
        conn.executescript(DDL)
        return True
    columns = {
        str(row[1]) for row in conn.execute(f"PRAGMA table_info({TABLE})")
    }
    if "knowable_at_utc" not in columns:
        conn.execute(
            f"ALTER TABLE {TABLE} ADD COLUMN knowable_at_utc TEXT NOT NULL DEFAULT ''"
        )
    # Self-healing rather than once-at-migration.  Repairing only at the
    # moment the column was added left 550 of 880 NBM rows and 354 of 700
    # HRRR rows empty -- written by a backfill on 2026-09-06 that raced the
    # column's arrival -- and an empty string sorts before every timestamp,
    # so ``runs_for``'s as-of filter treated every one of them as knowable at
    # every instant.  Measured 2026-09-07: 74.1% of NBM and 78.0% of HRRR
    # serving decisions in the admission window were reading a run from their
    # own future.  The cost of asking every time is one indexed count.
    if conn.execute(
        f"SELECT 1 FROM {TABLE} WHERE knowable_at_utc='' LIMIT 1"
    ).fetchone():
        repair_knowable_at(conn)
    return False


def repair_knowable_at(conn: sqlite3.Connection, *, only_missing: bool = True) -> int:
    """Fill ``knowable_at_utc`` on rows written before the column existed.

    A row fetched more than ``BACKFILL_FETCH_LAG_HOURS`` after its own run was
    issued came from a backfill: the archive held that run one publication lag
    after its nominal hour, whatever day this process got around to reading
    it.  Everything else is a live sweep, whose fetch time is the honest
    answer to "when could this have been read".
    """

    where = " WHERE knowable_at_utc=''" if only_missing else ""
    rows = conn.execute(
        f"SELECT regional_point_run_id, ensemble_system_id, run_init_utc,"
        f" archive_query_at_utc FROM {TABLE}{where}"
    ).fetchall()
    lag_by_system = {
        SYSTEM_IDS[product]: hours
        for product, hours in PUBLICATION_LAG_HOURS.items()
    }
    repaired = 0
    for run_id, system_id, run_init_utc, archive_query_utc in rows:
        try:
            run_init = _parse(run_init_utc)
            fetched = _parse(archive_query_utc)
        except ValueError:
            continue
        behind = (fetched - run_init).total_seconds() / 3600.0
        knowable = (
            run_init + timedelta(
                hours=lag_by_system.get(str(system_id), DEFAULT_PUBLICATION_LAG_HOURS)
            )
            if behind > BACKFILL_FETCH_LAG_HOURS else fetched
        )
        conn.execute(
            f"UPDATE {TABLE} SET knowable_at_utc=? WHERE regional_point_run_id=?",
            (_iso(knowable), str(run_id)),
        )
        repaired += 1
    return repaired


def _iso(stamp: datetime) -> str:
    return stamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def lead_hours_to_local_day_start(
    run_init: datetime, event_date: date, settlement_timezone: str
) -> float:
    start = datetime.combine(
        event_date, datetime.min.time(), tzinfo=ZoneInfo(settlement_timezone)
    ).astimezone(timezone.utc)
    return (start - run_init.astimezone(timezone.utc)).total_seconds() / 3600.0


def utc_offset_hours(event_date: date, settlement_timezone: str) -> int:
    local = datetime.combine(
        event_date, datetime.min.time(), tzinfo=ZoneInfo(settlement_timezone)
    )
    return int(local.utcoffset().total_seconds() // 3600)


def hourly_valid_map(run_init: datetime, hourly_c: Mapping[Any, Any]) -> dict[str, float]:
    """Forecast-hour offsets -> valid UTC instants, so a reader never needs the run."""

    out: dict[str, float] = {}
    for hour, value in hourly_c.items():
        out[_iso(run_init + timedelta(hours=int(hour)))] = float(value)
    return dict(sorted(out.items()))


def remaining_peak_complete(
    run_init: datetime, event_date: date, settlement_timezone: str,
    hourly_c: Mapping[Any, Any],
) -> bool:
    """Every afternoon hour the run could still speak to is on the curve."""

    zone = ZoneInfo(settlement_timezone)
    covered = {
        (run_init + timedelta(hours=int(hour))).astimezone(zone)
        for hour in hourly_c
    }
    covered_local_hours = {
        stamp.hour for stamp in covered if stamp.date() == event_date
    }
    run_local = run_init.astimezone(zone)
    wanted = [
        hour for hour in PEAK_LOCAL_HOURS
        if datetime.combine(event_date, datetime.min.time(), tzinfo=zone)
        + timedelta(hours=hour) >= run_local
    ]
    return bool(wanted) and all(hour in covered_local_hours for hour in wanted)


def stored_run_inits(
    conn: sqlite3.Connection, *, system_id: str, station_id: str, event_date: str
) -> set[str]:
    return {
        str(row[0]) for row in conn.execute(
            f"SELECT run_init_utc FROM {TABLE} WHERE ensemble_system_id=? "
            "AND station_id=? AND target_local_date=?",
            (system_id, station_id, event_date),
        )
    }


def write_run(
    conn: sqlite3.Connection,
    *,
    product: str,
    station_id: str,
    event_date: date,
    settlement_timezone: str,
    run_init: datetime,
    hourly_c: Mapping[Any, Any],
    hours_wanted: int,
    archive_query_at: datetime,
    source_url: str,
    backfill: bool = False,
) -> bool:
    """Persist one run of one model for one station-day; False if it was there."""

    if not hourly_c:
        raise ContractError("regional_point_run_curve_empty")
    system_id = SYSTEM_IDS[product]
    valid_map = hourly_valid_map(run_init, hourly_c)
    # When the archive first held this run.  A live sweep answers with its own
    # fetch; a backfill must answer with the run's publication time, or every
    # row it writes reads as "not knowable until the day I repaired it".
    knowable_at = (
        run_init + timedelta(hours=PUBLICATION_LAG_HOURS[product])
        if backfill else archive_query_at
    )
    identity = {
        "schema_version": SCHEMA_VERSION,
        "ensemble_system_id": system_id,
        "station_id": str(station_id).upper(),
        "target_local_date": event_date.isoformat(),
        "run_init_utc": _iso(run_init),
        "hourly_c": valid_map,
        "extraction_version": EXTRACTION_VERSIONS[product],
    }
    digest = content_sha256(identity)
    cursor = conn.execute(
        f"INSERT OR IGNORE INTO {TABLE}("
        "regional_point_run_id, ensemble_system_id, station_id, target_local_date,"
        " settlement_timezone, run_init_utc, run_cycle_hour,"
        " lead_hours_to_local_day_start, forecast_tmax_c, remaining_peak_complete,"
        " hours_wanted, hours_covered, hourly_c_json, transport_provider_id,"
        " extraction_version, archive_query_at_utc, knowable_at_utc, source_url,"
        " schema_version, content_sha256) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            f"{product.lower()}-{digest[:32]}", system_id, str(station_id).upper(),
            event_date.isoformat(), settlement_timezone, _iso(run_init),
            int(run_init.astimezone(timezone.utc).hour),
            round(lead_hours_to_local_day_start(run_init, event_date, settlement_timezone), 3),
            float(max(valid_map.values())),
            int(remaining_peak_complete(run_init, event_date, settlement_timezone, hourly_c)),
            int(hours_wanted), len(valid_map), json.dumps(valid_map, sort_keys=True),
            TRANSPORTS[product], EXTRACTION_VERSIONS[product], _iso(archive_query_at),
            _iso(knowable_at), str(source_url), SCHEMA_VERSION, digest,
        ),
    )
    return int(cursor.rowcount or 0) > 0


def runs_for(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    event_date: str,
    as_of_utc: str,
    system_id: str | None = None,
) -> list[dict[str, Any]]:
    """The lane's runs for a station-day knowable at ``as_of_utc``, freshest first."""

    sql = (
        f"SELECT ensemble_system_id, run_init_utc, lead_hours_to_local_day_start,"
        f" forecast_tmax_c, remaining_peak_complete, hourly_c_json, knowable_at_utc"
        f" FROM {TABLE} WHERE station_id=? AND target_local_date=? AND knowable_at_utc<=?"
    )
    params: list[Any] = [str(station_id).upper(), str(event_date), str(as_of_utc)]
    if system_id:
        sql += " AND ensemble_system_id=?"; params.append(system_id)
    sql += " ORDER BY run_init_utc DESC, archive_query_at_utc DESC"
    return [
        {
            "ensemble_system_id": str(r[0]), "run_init_utc": str(r[1]),
            "lead_hours_to_local_day_start": float(r[2]), "forecast_tmax_c": float(r[3]),
            "remaining_peak_complete": bool(r[4]), "hourly_c": json.loads(r[5]),
            "archive_query_at_utc": str(r[6]), "knowable_at_utc": str(r[6]),
        }
        for r in conn.execute(sql, params)
    ]


# ---------------------------------------------------------------------------
# capture: every cycle, run-scoped where the archive allows it
# ---------------------------------------------------------------------------

def _target_days(now: datetime, zone_name: str, offsets: Sequence[int]) -> list[date]:
    today = now.astimezone(ZoneInfo(zone_name)).date()
    return [today + timedelta(days=offset) for offset in offsets]


def _candidate_runs(
    product: str, now: datetime, event_date: date, zone_name: str,
) -> list[datetime]:
    """Runs issued from a day before the local day start up to its end,
    already on the archive by now."""

    zone = ZoneInfo(zone_name)
    day_start = datetime.combine(event_date, datetime.min.time(), tzinfo=zone).astimezone(timezone.utc)
    day_end = day_start + timedelta(days=1)
    latest = now - timedelta(hours=PUBLICATION_LAG_HOURS[product])
    runs: list[datetime] = []
    first = (day_start - timedelta(days=1)).replace(minute=0, second=0, microsecond=0)
    stamp = first
    while stamp < day_end:
        if stamp.hour in RUN_CYCLE_HOURS[product] and stamp >= day_start - timedelta(days=1) and stamp <= latest:
            runs.append(stamp)
        stamp += timedelta(hours=1)
    return runs


def plan_p1(
    conn: sqlite3.Connection,
    *,
    product: str,
    now: datetime,
    stations: Sequence[tuple[str, float, float, str]],
    day_offsets: Sequence[int] = (-1, 0, 1),
    max_runs: int = 4,
) -> list[dict[str, Any]]:
    """Runs not yet stored, grouped by run, newest runs first, capped."""

    system_id = SYSTEM_IDS[product]
    by_run: dict[datetime, list[dict[str, Any]]] = {}
    for station, latitude, longitude, zone_name in stations:
        for event_date in _target_days(now, zone_name, day_offsets):
            held = stored_run_inits(conn, system_id=system_id, station_id=station, event_date=event_date.isoformat())
            for run in _candidate_runs(product, now, event_date, zone_name):
                if _iso(run) in held:
                    continue
                by_run.setdefault(run, []).append({
                    "station_id": station, "latitude": latitude, "longitude": longitude,
                    "event_date": event_date, "utc_offset_hours": utc_offset_hours(event_date, zone_name),
                    "settlement_timezone": zone_name,
                })
    # Every candidate, newest first; the capture pass applies the cap to
    # runs that actually yield data, so a newest run the archive has not
    # published yet (HRRR 2026-09-06 12Z onward was missing from the bucket
    # all afternoon) costs a few 404s and does not starve the older ones.
    ordered = sorted(by_run.items(), key=lambda item: item[0], reverse=True)
    if max_runs is not None and int(max_runs) >= 0:
        ordered = ordered[:max(0, int(max_runs)) * 4]
    return [{"product": product, "run_init": run, "targets": targets} for run, targets in ordered]


def fetch_noaa_run_for_targets(
    *, product: str, run_init: datetime, targets: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    """One download per forecast hour, decoded once per station that wants it."""

    from .noaa_conus_archive import (
        _field_range, _range_get, decode_field, file_url, local_day_hours,
        product_hours, run_directory_url,
    )

    published = set(product_hours(product, run_init))
    if product == "HRRR" and run_init.hour not in (0, 6, 12, 18):
        published &= set(range(0, 19))
    wanted_by_key: dict[tuple[str, str], list[int]] = {}
    for target in targets:
        key = (str(target["station_id"]), target["event_date"].isoformat())
        hours = local_day_hours(
            run_init=run_init, event_date=target["event_date"],
            utc_offset_hours=int(target["utc_offset_hours"]),
        )
        wanted_by_key[key] = [hour for hour in hours if hour in published]
    union = sorted({hour for hours in wanted_by_key.values() for hour in hours})
    messages: dict[int, bytes] = {}
    for hour in union:
        url = file_url(product, run_init, hour)
        span = _field_range(url)
        if span is None:
            continue
        message = _range_get(url, span[0], span[1])
        if message is not None:
            messages[hour] = message
    results: dict[tuple[str, str], dict[str, Any]] = {}
    for target in targets:
        key = (str(target["station_id"]), target["event_date"].isoformat())
        values: dict[int, float] = {}
        for hour in wanted_by_key[key]:
            message = messages.get(hour)
            if message is None:
                continue
            field = decode_field(
                message, latitude=float(target["latitude"]), longitude=float(target["longitude"]),
                run_init=run_init, forecast_hour=hour, url=file_url(product, run_init, hour),
            )
            values[hour] = field.value_c
        results[key] = {
            "hourly_c": values, "hours_wanted": len(wanted_by_key[key]),
            "source_url": run_directory_url(product, run_init),
        }
    return results


def capture_p1(
    conn: sqlite3.Connection,
    *,
    product: str,
    now: datetime,
    stations: Sequence[tuple[str, float, float, str]],
    day_offsets: Sequence[int] = (-1, 0, 1),
    max_runs: int = 4,
    budget_sec: float | None = None,
    dry_run: bool = False,
    backfill: bool = False,
) -> dict[str, Any]:
    """Plan, fetch and persist this pass's runs for one product.

    ``backfill`` stamps each row knowable at its run's publication time
    rather than at this pass's clock, which is what a pass reaching weeks
    back is actually asserting.

    ``budget_sec`` bounds the pass in seconds as well as in runs.  The run
    cap counts productive runs only -- deliberately, so a newest run the
    archive has not published cannot starve the older ones -- and that was
    written for a miss that costs one 404 on the ``.idx``.  An NBM run whose
    index exists but whose ranged reads are slow is a miss that costs forty
    hours of ranged reads instead, and ``plan_p1`` hands the loop four times
    the cap to work through.  Measured 2026-09-09: the CONUS sweep spent its
    whole 1800s subprocess timeout inside this loop and was killed, for the
    third day running -- and because the resident admits one writer at a
    time, those thirty minutes are thirty minutes in which the production
    cycle cannot start.  The seconds are the resource the writer lane
    actually rations, so bound them here rather than guessing a run count
    that makes them fit.
    """

    if backfill and product == "CMA":
        # A backfilled row has to say when it became knowable, and this
        # product's archive is not resolved by run: an early stamp leaks the
        # afternoon into the morning, and a stamp late enough to be safe
        # makes the row useless for the same-day question.  Forward only.
        raise ContractError("regional_point_backfill_unsupported_for_product")
    plan = plan_p1(conn, product=product, now=now, stations=stations, day_offsets=day_offsets, max_runs=max_runs)
    written = 0
    existed = 0
    empty = 0
    productive = 0
    attempted = 0
    budget_exhausted = False
    deadline = (
        None if budget_sec is None else time.monotonic() + float(budget_sec)
    )
    unpublished: list[str] = []
    failed: list[str] = []
    for item in plan:
        if productive >= max(0, int(max_runs)):
            break
        # Checked before the fetch, never during: a run that has started is
        # allowed to finish, so the budget can never leave a run half
        # written.  What is left unfetched is simply still unstored, and
        # ``plan_p1`` plans it again on the next pass.
        if deadline is not None and time.monotonic() >= deadline:
            budget_exhausted = True
            break
        attempted += 1
        run_init = item["run_init"]
        try:
            if product in ("HRRR", "NBM"):
                results = fetch_noaa_run_for_targets(product=product, run_init=run_init, targets=item["targets"])
            elif product == "MSM":
                from .jma_msm_archive import fetch_day_maximum as msm_fetch, file_url as msm_file_url
                results = {}
                for target in item["targets"]:
                    captured = msm_fetch(
                        run_init=run_init, event_date=target["event_date"],
                        utc_offset_hours=int(target["utc_offset_hours"]),
                        latitude=float(target["latitude"]), longitude=float(target["longitude"]),
                    )
                    results[(str(target["station_id"]), target["event_date"].isoformat())] = {
                        "hourly_c": captured["hourly_c"], "hours_wanted": len(captured["hours_wanted"]),
                        "source_url": msm_file_url(run_init, "FH00-15"),
                    }
            elif product == "CMA":
                from .cma_grapes_point_archive import (  # noqa: PLC0415
                    fetch_day_maximum as cma_fetch, source_url as cma_source_url,
                )
                results = {}
                for target in item["targets"]:
                    captured = cma_fetch(
                        run_init=run_init, event_date=target["event_date"],
                        utc_offset_hours=int(target["utc_offset_hours"]),
                        latitude=float(target["latitude"]),
                        longitude=float(target["longitude"]),
                    )
                    results[(str(target["station_id"]), target["event_date"].isoformat())] = {
                        "hourly_c": captured["hourly_c"],
                        "hours_wanted": len(captured["hours_wanted"]),
                        "source_url": cma_source_url(
                            float(target["latitude"]), float(target["longitude"])
                        ),
                    }
            elif product == "ICON_EU":
                from .dwd_icon_eu_point_archive import fetch_run_day_maxima, file_url as icon_file_url
                raw = fetch_run_day_maxima(run_init=run_init, targets=list(item["targets"]))
                results = {
                    key: {"hourly_c": value["hourly_c"], "hours_wanted": len(value["hours_wanted"]),
                          "source_url": icon_file_url(run_init, 0)}
                    for key, value in raw.items()
                }
            else:
                raise ContractError("regional_point_product_unknown")
        except Exception as exc:  # noqa: BLE001 -- one run's failure is a line, not a stop
            # A pass that ran out of clock INSIDE a fetch is the bound doing
            # its job, not a fault, and it means the same thing the
            # deadline check above means: stop.  Reported beside network
            # faults it would make a healthy lane read as a failing one --
            # and continuing would hand the next run a clock already spent.
            from .jma_msm_archive import (  # noqa: PLC0415
                JmaMsmPassDeadlineExceeded,
            )

            if isinstance(exc, JmaMsmPassDeadlineExceeded):
                budget_exhausted = True
                break
            failed.append(f"{product} {run_init:%Y-%m-%dT%HZ} {type(exc).__name__}: {str(exc)[:80]}")
            continue
        if not any((results.get((str(t["station_id"]), t["event_date"].isoformat())) or {}).get("hourly_c") for t in item["targets"]):
            # Nothing for any station: the run is not on the archive yet.
            unpublished.append(f"{product} {run_init:%Y-%m-%dT%HZ}")
            continue
        productive += 1
        for target in item["targets"]:
            key = (str(target["station_id"]), target["event_date"].isoformat())
            captured = results.get(key)
            if not captured or not captured["hourly_c"]:
                empty += 1
                continue
            if dry_run:
                written += 1
                continue
            if write_run(
                conn, product=product, station_id=key[0], event_date=target["event_date"],
                settlement_timezone=str(target["settlement_timezone"]), run_init=run_init,
                hourly_c=captured["hourly_c"], hours_wanted=int(captured["hours_wanted"]),
                archive_query_at=now, source_url=str(captured["source_url"]),
                backfill=backfill,
            ):
                written += 1
            else:
                existed += 1
        if not dry_run:
            conn.commit()
    return {
        "product": product, "runs_planned": len(plan), "runs_with_data": productive,
        "runs_attempted": attempted, "budget_exhausted": budget_exhausted,
        "written": written, "existed": existed, "empty": empty,
        "unpublished": unpublished, "failed": failed,
    }


__all__ = [
    "TABLE", "SCHEMA_VERSION", "SYSTEM_IDS", "RUN_CYCLE_HOURS", "PEAK_LOCAL_HOURS",
    "ensure_schema", "repair_knowable_at", "write_run", "runs_for", "plan_p1", "capture_p1",
    "fetch_noaa_run_for_targets", "lead_hours_to_local_day_start",
    "hourly_valid_map", "remaining_peak_complete", "stored_run_inits",
]
