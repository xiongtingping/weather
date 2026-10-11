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
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from . import sqlite_busy
from .contracts import ContractError, content_sha256
from .sqlite_busy import WriteBlocked

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
    # Charter 201: the deterministic-forecast family's new members.  Never
    # ensemble members (not registered in calibration_native_ensemble_system,
    # no fusion weight) and never read by P1 (regional_features freezes P1's
    # pairs); captured here for the P0 feature and its shadow record.
    "AIFS": "ECMWF_AIFS_SINGLE_POINT",
    "IFS9KM": "ECMWF_IFS_9KM_RELAY_POINT",
    "UKMO10": "UKMO_GLOBAL_10KM_RELAY_POINT",
    # Charter 267 (二): native deterministic global runs for the listing-time
    # forecast F.  Captured beside the family, read by F only: not in
    # ``deterministic_forecast_family.FAMILY_SYSTEM_IDS``, so no P0 feature,
    # gate or shadow reads them, and never P0-M or P1.
    "IFS_HRES": "ECMWF_IFS_HRES_NATIVE_POINT",
    "GFS": "NOAA_GFS_025_NATIVE_POINT",
    "AIGFS": "NOAA_AIGFS_025_NATIVE_POINT",
    "GDPS": "ECCC_GDPS_15KM_NATIVE_POINT",
}
# Charter 267 (二): the native deterministic products and their module.
NATIVE_DETERMINISTIC_PRODUCTS = frozenset({"IFS_HRES", "GFS", "AIGFS", "GDPS"})
# Charter 201: when a backfilled row is stamped knowable, where a product's
# holding lag is longer than its first-ask lag.  AIFS: the GCS mirror's
# step-96 index created T+5:25..5:43 over twelve runs 09-20..22 -> 6 h.  CMA
# (named runs, charter 152): the relay holds a cycle 10-11 h after init
# -> 11 h, late rather than early.  The other relays have no measured lag and
# are not backfilled.
BACKFILL_KNOWABLE_LAG_HOURS = {"AIFS": 6.0, "CMA": 11.0}
# Forecast-step spacing, hours (charter 201: AIFS Single's open data is six-hourly).
STEP_HOURS = {"AIFS": 6, "IFS_HRES": 3, "GFS": 3, "AIGFS": 6, "GDPS": 3}
# Charter 201: which products reach the lane through the aggregator relay
# (knowable = when we fetched it; quota-charged) rather than a publisher's own
# archive.
RELAY_PRODUCTS = frozenset({"CMA", "IFS9KM", "UKMO10"})
TRANSPORTS = {
    "MSM": "RISH_KYOTO_JMA_ARCHIVE",
    "CMA": "OPEN_METEO_CMA_GRAPES_GLOBAL",
    "HRRR": "NOAA_HRRR_PDS_S3",
    "NBM": "NOAA_NBM_PDS_S3",
    "ICON_EU": "DWD_OPENDATA_ICON_EU",
    "AIFS": "ECMWF_OPEN_DATA_GCS_AIFS_SINGLE",
    "IFS9KM": "OPEN_METEO_ECMWF_IFS",
    "UKMO10": "OPEN_METEO_UKMO_GLOBAL_10KM",
    "IFS_HRES": "ECMWF_OPEN_DATA_GCS_IFS_OPER",
    "GFS": "NOAA_GFS_BDP_PDS_S3",
    "AIGFS": "NOAA_AIGFS_NODD_S3",
    "GDPS": "ECCC_MSC_DATAMART_GDPS",
}
EXTRACTION_VERSIONS = {
    "MSM": "jma_msm_surface_nearest_cell_v1",
    # v3 (2026-09-19): named runs.  v1/v2 read the aggregator's current
    # forecast and DERIVED the cycle from the clock; checked against its
    # single-run endpoint, every v2 row was the run six hours older than its
    # label, and 52 of 360 consecutive labels repeated the row before.  The
    # lane now asks for a cycle by name, so the label is the run.  v1/v2
    # rows are kept -- their curves and knowable_at_utc are sound -- except
    # where a v3 row lands on the same key: that row is superseded (see
    # ``write_run``), because its label names a run whose data it does not
    # hold.
    "CMA": "cma_grapes_global_open_meteo_single_run_v3",
    # v2 (2026-09-23, charter 200): the station is read at the nearest cell
    # HRRR's own LAND mask calls land (``noaa_conus_archive.SAMPLING_RULE``).
    # Every station whose nearest cell is land reads the same cell and value
    # as v1, bit for bit; KSFO's nearest cell is San Francisco Bay and moves
    # 3.1 km onto the peninsula.
    "HRRR": "ncep_hrrr_surface_nearest_land_cell_v2",
    "NBM": "ncep_nbm_core_nearest_cell_v1",
    # Unchanged by charter 201: ICON-EU's own FR_LAND now places every station
    # (charter 200's rule), and all nine stations' nearest cells are land.
    "ICON_EU": "dwd_icon_eu_surface_nearest_cell_v1",
    "AIFS": "ecmwf_aifs_single_2t_6h_nearest_land_cell_v1",
    "IFS9KM": "open_meteo_ecmwf_ifs_single_run_land_cell_v1",
    "UKMO10": "open_meteo_ukmo_10km_single_run_land_cell_v1",
    # Charter 267 (二): 2 m at the product's own steps; the cell by the run's
    # own land field (HRES ``lsm``, GFS ``LAND``) or GFS's of the same init.
    "IFS_HRES": "ecmwf_ifs_hres_2t_3h_nearest_land_cell_v1",
    "GFS": "ncep_gfs_tmp2m_3h_nearest_land_cell_v1",
    "AIGFS": "noaa_aigfs_tmp2m_6h_gfs_land_cell_v1",
    "GDPS": "eccc_gdps_airtemp2m_3h_gfs_land_cell_v1",
}
# Charter 200: (system, station) -> extraction versions whose rows were read
# at a water cell.  Measured on the lane's own peak-complete rows, KSFO/HRRR v1
# read 3.75 C below settled truth (sd 1.64) where the other eleven HRRR
# stations read -1.1..+2.0; on native bytes over 09-08..09-21 the water cell
# read +3.86 (MAE 3.86) and the nearest land cell -0.02 (MAE 1.57).  These rows
# are never read again (``runs_for``, the debias fit), are not counted as
# holding their run (``stored_run_inits``), so a pass re-fetches any run still
# inside its window, and are superseded -- audit line first -- when the
# land-cell row for the same key lands (``write_run``).
RETIRED_CELL_VERSIONS: Mapping[tuple[str, str], frozenset[str]] = {
    ("NCEP_HRRR_POINT", "KSFO"): frozenset({"ncep_hrrr_surface_nearest_cell_v1"}),
}
# Charter 200: the evidence clock of a moved (system, station) starts again.
# Until this instant nothing of the pair reaches a reader -- not the retired
# rows, and not the new-cell rows either, whose offset has no settled record
# yet -- so the station reads its other regional lane (KSFO: the NBM) or none.
# Six days: the point-forecast minimum of five settled training days
# (``regional_point_debias.MIN_TRAIN_DAYS``) plus the day the fifth settles
# on, counted from the declared start of P1 v20.
CELL_EVIDENCE_FROM_UTC: Mapping[tuple[str, str], str] = {
    ("NCEP_HRRR_POINT", "KSFO"): "2026-09-29T04:15:00Z",
}


def retired_cell_row(system_id: str, station_id: str, extraction_version: str) -> bool:
    """A row read at a cell charter 200 retired."""

    retired = RETIRED_CELL_VERSIONS.get((str(system_id), str(station_id).upper()))
    return bool(retired) and str(extraction_version) in retired


def cell_evidence_held(system_id: str, station_id: str, as_of_utc: str) -> bool:
    """The pair's evidence clock has not restarted yet at ``as_of_utc``."""

    start = CELL_EVIDENCE_FROM_UTC.get((str(system_id), str(station_id).upper()))
    return bool(start) and str(as_of_utc) < start
# Every cycle the model actually issues.
RUN_CYCLE_HOURS = {
    "MSM": tuple(range(0, 24, 3)),
    "CMA": (0, 6, 12, 18),
    "HRRR": tuple(range(0, 24)),
    "NBM": tuple(range(0, 24)),
    "ICON_EU": tuple(range(0, 24, 3)),
    "AIFS": (0, 6, 12, 18),
    "IFS9KM": (0, 6, 12, 18),
    "UKMO10": (0, 6, 12, 18),
    "IFS_HRES": (0, 12),
    "GFS": (0, 6, 12, 18),
    "AIGFS": (0, 6, 12, 18),
    "GDPS": (0, 12),
}
# How long after its nominal hour a run is normally on the archive.
PUBLICATION_LAG_HOURS = {
    "MSM": 3.0, "HRRR": 1.5, "NBM": 2.0, "ICON_EU": 4.0, "CMA": 6.0,
    # Relays hold a run well after its centre publishes it (measured
    # 2026-09-23: the relay's UKMO 12Z still missing 16 h after init), so
    # the first ask waits and the run stays wanted longer (CURRENT_RUN_HOURS).
    "AIFS": 5.0, "IFS9KM": 6.0, "UKMO10": 12.0,
    # Charter 267 (二): measured 10-11 -- ECMWF open data's oper stream holds
    # 00Z some 7-8 h after init; GFS f096 ~4 h; GDPS ~4-5 h; AIGFS after GFS.
    # The doorbell (probe_deterministic_runs) asks earlier than these anyway.
    "IFS_HRES": 8.0, "GFS": 4.5, "AIGFS": 6.0, "GDPS": 5.0,
}
# Charter 201: every listed target day (station-local D-1 .. D+2) and every
# run still current (issued in the last day) that reaches its afternoon --
# "全部采集，谁新就用谁".  The P1 lane's original reach (D-1..D+1, runs from
# the day before the day, reaching the afternoon or not) is kept as it was,
# so the rows P1 reads are captured exactly as before.
DAY_OFFSETS = (-1, 0, 1, 2)
LEGACY_DAY_OFFSETS = (-1, 0, 1)
LEGACY_PRODUCTS = frozenset({"MSM", "CMA", "HRRR", "NBM", "ICON_EU"})
CURRENT_RUN_HOURS = 24.0
CURRENT_RUN_HOURS_BY_PRODUCT = {"UKMO10": 36.0}
# How long before the local day starts a run may be issued and still count
# for that day (bounded by what each product publishes).
LOOKBACK_HOURS = {
    "HRRR": 30.0, "NBM": 30.0, "MSM": 60.0, "ICON_EU": 72.0, "CMA": 60.0,
    "AIFS": 72.0, "IFS9KM": 72.0, "UKMO10": 72.0,
    "IFS_HRES": 72.0, "GFS": 72.0, "AIGFS": 72.0, "GDPS": 72.0,
}
# The end of the local peak window, hours after local midnight.
_PEAK_END_HOUR = 19


def product_horizon_hours(product: str, run_init: datetime) -> int:
    """The last forecast hour this product publishes for this run."""

    hour = run_init.astimezone(timezone.utc).hour
    if product == "HRRR":
        return 48 if hour in (0, 6, 12, 18) else 18
    if product == "NBM":
        return 48
    if product == "MSM":
        return 78 if hour in (0, 12) else 39
    if product == "ICON_EU":
        return 120 if hour in (0, 6, 12, 18) else 30
    if product == "CMA":
        return 78
    if product == "AIFS":
        return 96
    if product in ("IFS9KM", "UKMO10"):
        return 95
    if product in NATIVE_DETERMINISTIC_PRODUCTS:
        return 96
    raise ContractError("regional_point_product_unknown")
# Extraction versions whose run label was derived from the clock rather than
# read from the transport (CMA v1/v2): a named-run row on the same key
# supersedes them.
DERIVED_LABEL_VERSIONS = frozenset({
    "cma_grapes_global_open_meteo_derived_cycle_v1",
    "cma_grapes_global_open_meteo_derived_cycle_v2",
})
SUPERSEDED_AUDIT = (
    Path(__file__).resolve().parents[1]
    / "data" / "runtime" / "regional_point_superseded_rows.jsonl"
)
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
    hourly_c: Mapping[Any, Any], step_hours: int = 1,
) -> bool:
    """Every afternoon hour the run could still speak to is on the curve.

    ``step_hours`` (charter 201): a product published every six hours (AIFS
    Single) speaks only at its steps, so "every afternoon hour" is every step
    inside the afternoon it could still reach -- at least one.
    """

    zone = ZoneInfo(settlement_timezone)
    if int(step_hours) > 1:
        covered_utc = {run_init + timedelta(hours=int(hour)) for hour in hourly_c}
        start = datetime.combine(event_date, datetime.min.time(), tzinfo=zone)
        window_start = max(start + timedelta(hours=PEAK_LOCAL_HOURS[0]), run_init)
        window_end = start + timedelta(hours=PEAK_LOCAL_HOURS[-1] + 1)
        steps = []
        stamp = run_init
        while stamp < window_end:
            if stamp >= window_start:
                steps.append(stamp)
            stamp += timedelta(hours=int(step_hours))
        return bool(steps) and all(step in covered_utc for step in steps)
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
    """Runs stored for the station-day under a label that IS the run.

    A clock-derived label (CMA v1/v2) does not count as holding its run: it
    holds another run's data, so the named run is still wanted, and
    ``write_run`` supersedes the derived row when it lands.
    """

    unheld = sorted(
        DERIVED_LABEL_VERSIONS
        | RETIRED_CELL_VERSIONS.get((str(system_id), str(station_id).upper()), frozenset())
    )
    marks = ",".join("?" for _ in unheld)
    return {
        str(row[0]) for row in conn.execute(
            f"SELECT run_init_utc FROM {TABLE} WHERE ensemble_system_id=? "
            f"AND station_id=? AND target_local_date=? AND extraction_version NOT IN ({marks})",
            (system_id, station_id, event_date, *unheld),
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
        run_init + timedelta(hours=BACKFILL_KNOWABLE_LAG_HOURS.get(
            product, PUBLICATION_LAG_HOURS[product]
        ))
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
    _supersede_derived_label(
        conn, system_id=system_id, station_id=str(station_id).upper(),
        event_date=event_date.isoformat(), run_init=_iso(run_init),
        extraction_version=EXTRACTION_VERSIONS[product],
    )
    with sqlite_busy.blocked_when_busy():
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
                int(remaining_peak_complete(
                    run_init, event_date, settlement_timezone, hourly_c,
                    step_hours=STEP_HOURS.get(product, 1),
                )),
                int(hours_wanted), len(valid_map), json.dumps(valid_map, sort_keys=True),
                TRANSPORTS[product], EXTRACTION_VERSIONS[product], _iso(archive_query_at),
                _iso(knowable_at), str(source_url), SCHEMA_VERSION, digest,
            ),
        )
    return int(cursor.rowcount or 0) > 0


def _supersede_derived_label(
    conn: sqlite3.Connection, *, system_id: str, station_id: str,
    event_date: str, run_init: str, extraction_version: str,
) -> None:
    """Clear a clock-derived row off the key a named-run row is about to take.

    A derived label (CMA v1/v2) names a run the row's data is not -- checked
    2026-09-19, it held the run six hours older -- so the key is wanted by
    the row that really is that run.  The removed row is appended to
    ``SUPERSEDED_AUDIT`` whole first, so nothing is lost; a named-run row is
    never superseded.
    """

    if extraction_version in DERIVED_LABEL_VERSIONS:
        return
    retired = RETIRED_CELL_VERSIONS.get((str(system_id), str(station_id).upper()), frozenset())
    if extraction_version in retired:
        return
    replaced = sorted(DERIVED_LABEL_VERSIONS | retired)
    marks = ",".join("?" for _ in replaced)
    rows = conn.execute(
        f"SELECT * FROM {TABLE} WHERE ensemble_system_id=? AND station_id=?"
        f" AND target_local_date=? AND run_init_utc=? AND extraction_version IN ({marks})",
        (system_id, station_id, event_date, run_init, *replaced),
    ).fetchall()
    if not rows:
        return
    names = [d[0] for d in conn.execute(f"SELECT * FROM {TABLE} LIMIT 0").description]
    SUPERSEDED_AUDIT.parent.mkdir(parents=True, exist_ok=True)
    with SUPERSEDED_AUDIT.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps({
                "superseded_at_utc": _iso(datetime.now(timezone.utc)),
                "reason": (
                    "water_cell_replaced_by_land_cell"
                    if row[names.index("extraction_version")] in retired
                    else "derived_label_replaced_by_named_run"
                ),
                "replaced_by_extraction_version": extraction_version,
                "row": dict(zip(names, row)),
            }, sort_keys=True) + "\n")
    with sqlite_busy.blocked_when_busy():
        conn.execute(
            f"DELETE FROM {TABLE} WHERE ensemble_system_id=? AND station_id=?"
            f" AND target_local_date=? AND run_init_utc=? AND extraction_version IN ({marks})",
            (system_id, station_id, event_date, run_init, *replaced),
        )


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
        f" forecast_tmax_c, remaining_peak_complete, hourly_c_json, knowable_at_utc,"
        f" extraction_version"
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
        # Charter 200: a water-cell row is never read, and a moved pair says
        # nothing until its evidence clock restarts.
        if not retired_cell_row(r[0], station_id, r[7])
        and not cell_evidence_held(r[0], station_id, as_of_utc)
    ]


# ---------------------------------------------------------------------------
# capture: every cycle, run-scoped where the archive allows it
# ---------------------------------------------------------------------------

def _target_days(now: datetime, zone_name: str, offsets: Sequence[int]) -> list[date]:
    today = now.astimezone(ZoneInfo(zone_name)).date()
    return [today + timedelta(days=offset) for offset in offsets]


def _candidate_runs(
    product: str, now: datetime, event_date: date, zone_name: str,
    *, legacy: bool = True, backfill: bool = False,
) -> list[datetime]:
    """Every run this day takes, oldest first (both reaches)."""

    return sorted(_candidate_run_tiers(
        product, now, event_date, zone_name, legacy=legacy, backfill=backfill,
    ))


def _candidate_run_tiers(
    product: str, now: datetime, event_date: date, zone_name: str,
    *, legacy: bool = True, backfill: bool = False,
) -> dict[datetime, int]:
    """Runs already on the archive by now that this day takes.

    Two reaches, one union.  The P1 lane's own (``legacy``): runs issued
    from a day before the local day start up to its end -- kept exactly as
    it was for the products and days P1 reads.  Charter 201's: every run
    still current (issued within ``CURRENT_RUN_HOURS``) from up to
    ``LOOKBACK_HOURS`` before the day that reaches the end of its afternoon.
    """

    zone = ZoneInfo(zone_name)
    day_start = datetime.combine(event_date, datetime.min.time(), tzinfo=zone).astimezone(timezone.utc)
    day_end = day_start + timedelta(days=1)
    latest = now - timedelta(hours=PUBLICATION_LAG_HOURS[product])
    # run -> 0 when the P1 lane's own reach takes it, 1 when only charter
    # 201's does; a capture pass works tier 0 first, so the rows P1 reads are
    # fetched and committed exactly when they were before.
    runs: dict[datetime, int] = {}
    # A backfill reaches days of any age: the P1 lane's own reach for every
    # day it names (as the backfill always did), and the wider reach without
    # the "still current" window.
    if (legacy or backfill) and product in LEGACY_PRODUCTS:
        first = (day_start - timedelta(days=1)).replace(minute=0, second=0, microsecond=0)
        # CMA's transport is a rate-limited aggregator and only the last day's
        # cycles can still serve P1 (``regional_features`` admits twelve hours),
        # so older cycles are not asked for.
        oldest = now - timedelta(hours=24) if product == "CMA" else first
        stamp = first
        while stamp < day_end:
            if (stamp.hour in RUN_CYCLE_HOURS[product] and stamp >= day_start - timedelta(days=1)
                    and oldest <= stamp <= latest):
                runs[stamp] = 0
            stamp += timedelta(hours=1)
    first = (day_start - timedelta(hours=LOOKBACK_HOURS[product])).replace(
        minute=0, second=0, microsecond=0
    )
    oldest = first if backfill else now - timedelta(
        hours=CURRENT_RUN_HOURS_BY_PRODUCT.get(product, CURRENT_RUN_HOURS)
    )
    peak_end = day_start + timedelta(hours=_PEAK_END_HOUR)
    stamp = first
    while stamp < day_end:
        if (
            stamp.hour in RUN_CYCLE_HOURS[product]
            and oldest <= stamp <= latest
            and stamp + timedelta(hours=product_horizon_hours(product, stamp)) >= peak_end
        ):
            runs.setdefault(stamp, 1)
        stamp += timedelta(hours=1)
    return runs


def prepare(conn: sqlite3.Connection) -> None:
    """Register the lane's systems and ensure its schema, guarded as one step.

    Both halves write: ``register_regional_point_systems`` inserts the
    registry rows Charter 95 resolves each system from, and ``ensure_schema``
    runs DDL.  Every capture lane does them, in this order, before it fetches
    anything -- so this is also where a pass learns, for free, that the file
    is not taking writes, before it has spent a byte of network on runs it
    could not store.  ``WriteBlocked`` if that is the answer.
    """

    from .native_ensemble_training_facts import (  # noqa: PLC0415
        register_regional_point_systems,
    )

    with sqlite_busy.blocked_when_busy():
        register_regional_point_systems(conn)
        conn.commit()
        ensure_schema(conn)


def plan_p1(
    conn: sqlite3.Connection,
    *,
    product: str,
    now: datetime,
    stations: Sequence[tuple[str, float, float, str]],
    day_offsets: Sequence[int] = DAY_OFFSETS,
    max_runs: int = 4,
    backfill: bool = False,
) -> list[dict[str, Any]]:
    """Runs not yet stored, grouped by run, newest runs first, capped.

    ``backfill`` (charter 201): a pass reaching past days.  At a (system,
    station) pair P1 reads, a backfill takes only runs issued more than
    ``regional_features.MAX_RUN_AGE_HOURS`` before the local day starts --
    runs no P1 prefix of that day (all inside the day) can read -- so a
    backfill for the P0 feature never changes what P1 would have read, or
    the samples its likelihood is refitted on.
    """

    from .regional_features import MAX_RUN_AGE_HOURS, p1_reads_pair  # noqa: PLC0415

    system_id = SYSTEM_IDS[product]
    by_run: dict[tuple[int, datetime], list[dict[str, Any]]] = {}
    for station, latitude, longitude, zone_name in stations:
        for offset, event_date in zip(day_offsets, _target_days(now, zone_name, day_offsets)):
            held = stored_run_inits(conn, system_id=system_id, station_id=station, event_date=event_date.isoformat())
            tiers = _candidate_run_tiers(
                product, now, event_date, zone_name,
                legacy=offset in LEGACY_DAY_OFFSETS, backfill=backfill,
            )
            p1_pair = backfill and p1_reads_pair(system_id, station)
            for run, tier in tiers.items():
                if _iso(run) in held:
                    continue
                if p1_pair and lead_hours_to_local_day_start(
                    run, event_date, zone_name,
                ) <= MAX_RUN_AGE_HOURS:
                    continue
                by_run.setdefault((tier, run), []).append({
                    "station_id": station, "latitude": latitude, "longitude": longitude,
                    "event_date": event_date, "utc_offset_hours": utc_offset_hours(event_date, zone_name),
                    "settlement_timezone": zone_name,
                })
    # Every candidate, newest first; the capture pass applies the cap to
    # runs that actually yield data, so a newest run the archive has not
    # published yet (HRRR 2026-09-06 12Z onward was missing from the bucket
    # all afternoon) costs a few 404s and does not starve the older ones.
    # Charter 201: the P1 lane's own reach (tier 0) first, exactly as it was
    # planned and capped before; charter 201's wider reach (tier 1) after it,
    # with its own cap, so it can never push a P1 row later.
    planned: list[dict[str, Any]] = []
    for tier in (0, 1):
        ordered = sorted(
            ((run, targets) for (t, run), targets in by_run.items() if t == tier),
            key=lambda item: item[0], reverse=True,
        )
        if max_runs is not None and int(max_runs) >= 0:
            ordered = ordered[:max(0, int(max_runs)) * 4]
        planned += [
            {"product": product, "run_init": run, "targets": targets, "tier": tier}
            for run, targets in ordered
        ]
    return planned


def fetch_noaa_run_for_targets(
    *, product: str, run_init: datetime, targets: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, str], dict[str, Any]]:
    """One download per forecast hour, decoded once per station that wants it."""

    from .noaa_conus_archive import (
        GridPointReader, _field_range, _range_get, decode_field, file_url,
        local_day_hours, product_hours, run_directory_url, sampling_points,
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
    # Charter 200: where each station is read -- its own coordinates when
    # the product's land mask calls the nearest cell land (every station but
    # KSFO on HRRR, and every NBM station: the NBM core has no mask), else the
    # nearest land cell's centre; a station with no land cell in reach is not
    # read.  One mask read per run, from a file this run already publishes.
    if messages:
        sampled = sampling_points(
            product=product, run_init=run_init, forecast_hour=min(messages),
            points=[(float(t["latitude"]), float(t["longitude"])) for t in targets],
        )
    else:
        sampled = [
            {"latitude": float(t["latitude"]), "longitude": float(t["longitude"]), "moved": False}
            for t in targets
        ]
    readable = [position for position, point in enumerate(sampled) if point is not None]
    # One grid search per station per pass, not one per (station, hour):
    # see ``GridPointReader`` for the 600-second NBM decode this replaces and
    # the per-station proof against eccodes that keeps it honest.  A station
    # the reader could not prove falls through to ``decode_field`` below,
    # exactly as before, range check and all.
    reader = GridPointReader(
        [(float(sampled[p]["latitude"]), float(sampled[p]["longitude"])) for p in readable]
    )
    kelvin_by_hour = {hour: reader.kelvin(message) for hour, message in messages.items()}
    results: dict[tuple[str, str], dict[str, Any]] = {}
    for slot, position in enumerate(readable):
        target = targets[position]
        point = sampled[position]
        key = (str(target["station_id"]), target["event_date"].isoformat())
        values: dict[int, float] = {}
        for hour in wanted_by_key[key]:
            message = messages.get(hour)
            if message is None:
                continue
            kelvin = kelvin_by_hour[hour][slot]
            if kelvin is None:
                field = decode_field(
                    message, latitude=float(point["latitude"]), longitude=float(point["longitude"]),
                    run_init=run_init, forecast_hour=hour, url=file_url(product, run_init, hour),
                )
                values[hour] = field.value_c
                continue
            if not 180.0 <= kelvin <= 340.0:
                raise ContractError("noaa_conus_value_out_of_range")
            values[hour] = kelvin - 273.15
        source_url = run_directory_url(product, run_init)
        if point.get("moved"):
            # The row says where it was read; a station read where it stands
            # carries the same citation as before.
            source_url += (
                f"#land_cell={float(point['latitude']):.5f},{float(point['longitude']):.5f}"
            )
        results[key] = {
            "hourly_c": values, "hours_wanted": len(wanted_by_key[key]),
            "source_url": source_url,
        }
    return results


def capture_p1(
    conn: sqlite3.Connection,
    *,
    product: str,
    now: datetime,
    stations: Sequence[tuple[str, float, float, str]],
    day_offsets: Sequence[int] = DAY_OFFSETS,
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

    if backfill and product in RELAY_PRODUCTS and product not in BACKFILL_KNOWABLE_LAG_HOURS:
        # A backfilled relay row must say when the relay first held it; with
        # no measured holding lag it would have to guess, so it is refused.
        raise ContractError("regional_point_backfill_unsupported_for_product")
    plan = plan_p1(
        conn, product=product, now=now, stations=stations, day_offsets=day_offsets,
        max_runs=max_runs, backfill=backfill,
    )
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
    # Why the pass stopped early, when a writer other than this one is the
    # reason.  Reported rather than raised: the runs already committed are
    # real and the lane needs to come back in minutes, not in its whole
    # interval -- see ``WriteBlocked``.
    write_blocked = ""
    productive_by_tier = {0: 0, 1: 0}
    # A run found unpublished this pass is not asked for again by the wider
    # reach's item for the same run (charter 201).
    unpublished_runs: set[datetime] = set()
    for item in plan:
        # Each reach has its own productive cap (charter 201): the P1 lane's
        # own runs are taken exactly as before, and the wider reach only
        # after them.
        tier = int(item.get("tier", 0))
        if productive_by_tier[tier] >= max(0, int(max_runs)):
            continue
        if item["run_init"] in unpublished_runs:
            continue
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
                    RunNotPublished, fetch_day_maximum as cma_fetch,
                    source_url as cma_source_url,
                )
                results = {}
                for target in item["targets"]:
                    try:
                        captured = cma_fetch(
                            run_init=run_init, event_date=target["event_date"],
                            utc_offset_hours=int(target["utc_offset_hours"]),
                            latitude=float(target["latitude"]),
                            longitude=float(target["longitude"]),
                        )
                    except RunNotPublished:
                        # The cycle is not out: one request says so for all
                        # eight stations, and the run is reported unpublished.
                        results = {}
                        break
                    results[(str(target["station_id"]), target["event_date"].isoformat())] = {
                        "hourly_c": captured["hourly_c"],
                        "hours_wanted": len(captured["hours_wanted"]),
                        "source_url": cma_source_url(
                            float(target["latitude"]), float(target["longitude"]),
                            run_init,
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
            elif product == "AIFS":
                from .aifs_single_point_archive import fetch_run_for_targets as aifs_fetch  # noqa: PLC0415
                results = aifs_fetch(run_init=run_init, targets=list(item["targets"]))
            elif product in NATIVE_DETERMINISTIC_PRODUCTS:
                from .native_deterministic_point_archive import (  # noqa: PLC0415
                    fetch_run_for_targets as native_fetch,
                )
                results = native_fetch(
                    product=product, run_init=run_init, targets=list(item["targets"]),
                )
            elif product in ("IFS9KM", "UKMO10"):
                from .open_meteo_relay_archive import (  # noqa: PLC0415
                    RunNotPublished as RelayNotPublished, fetch_relay_run_for_targets,
                )
                try:
                    results = fetch_relay_run_for_targets(
                        product=product, run_init=run_init, targets=list(item["targets"]),
                    )
                except RelayNotPublished:
                    results = {}
            else:
                raise ContractError("regional_point_product_unknown")
        except Exception as exc:  # noqa: BLE001 -- one run's failure is a line, not a stop
            # A pass that ran out of clock INSIDE a fetch is the bound doing
            # its job, not a fault, and it means the same thing the
            # deadline check above means: stop.  Reported beside network
            # faults it would make a healthy lane read as a failing one --
            # and continuing would hand the next run a clock already spent.
            from .pass_deadline import PassDeadlineExceeded  # noqa: PLC0415

            if isinstance(exc, PassDeadlineExceeded):
                budget_exhausted = True
                break
            failed.append(f"{product} {run_init:%Y-%m-%dT%HZ} {type(exc).__name__}: {str(exc)[:80]}")
            continue
        if not any((results.get((str(t["station_id"]), t["event_date"].isoformat())) or {}).get("hourly_c") for t in item["targets"]):
            # Nothing for any station: the run is not on the archive yet.
            unpublished.append(f"{product} {run_init:%Y-%m-%dT%HZ}")
            unpublished_runs.add(run_init)
            continue
        productive += 1
        productive_by_tier[tier] += 1
        # The ingest of one run, all-or-nothing.  A write that cannot take
        # the lock stops the pass here: the next run's fetch would cost the
        # network the same minutes and end at the same lock, and this run's
        # half-written rows are rolled back so the next pass plans it whole.
        run_written = run_existed = run_empty = 0
        try:
            for target in item["targets"]:
                key = (str(target["station_id"]), target["event_date"].isoformat())
                captured = results.get(key)
                if not captured or not captured["hourly_c"]:
                    run_empty += 1
                    continue
                if dry_run:
                    run_written += 1
                    continue
                if write_run(
                    conn, product=product, station_id=key[0], event_date=target["event_date"],
                    settlement_timezone=str(target["settlement_timezone"]), run_init=run_init,
                    hourly_c=captured["hourly_c"], hours_wanted=int(captured["hours_wanted"]),
                    archive_query_at=now, source_url=str(captured["source_url"]),
                    backfill=backfill,
                ):
                    run_written += 1
                else:
                    run_existed += 1
            if not dry_run:
                # The COMMIT is part of the same ingest and takes the same
                # lock: one that waits out busy_timeout leaves this run's
                # rows pending, so it is classified like the inserts.
                with sqlite_busy.blocked_when_busy():
                    conn.commit()
        except WriteBlocked as exc:
            # This run's counters are folded in only after its commit, so
            # nothing here has to be taken back: a run that did not land is
            # a run the next pass plans again, whole.
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            write_blocked = f"{product} {run_init:%Y-%m-%dT%HZ} {exc}"
            break
        written += run_written
        existed += run_existed
        empty += run_empty
    return {
        "product": product, "runs_planned": len(plan), "runs_with_data": productive,
        "runs_attempted": attempted, "budget_exhausted": budget_exhausted,
        "written": written, "existed": existed, "empty": empty,
        "unpublished": unpublished, "failed": failed,
        "write_blocked": write_blocked,
    }


__all__ = [
    "TABLE", "SCHEMA_VERSION", "SYSTEM_IDS", "RUN_CYCLE_HOURS", "PEAK_LOCAL_HOURS",
    "WriteBlocked",
    "ensure_schema", "prepare", "repair_knowable_at", "write_run", "runs_for",
    "plan_p1", "capture_p1",
    "fetch_noaa_run_for_targets", "lead_hours_to_local_day_start",
    "hourly_valid_map", "remaining_peak_complete", "stored_run_inits",
]
