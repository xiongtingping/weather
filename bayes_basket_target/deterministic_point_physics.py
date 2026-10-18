"""Charter 267 补记八: the F systems' own physics at the stations.

User 2026-10-18 「接原生模型」→「接，从现在开始采，先回补」→「全部站点都接」.
Research round 74 found the listing-time physics it could read (Open-Meteo,
an aggregator, half the station-days) too thin to tell ZUUU's cold-bias days
from its accurate ones.  This lane reads, from the publishers' own objects,
the shortwave radiation, total cloud, 2 m dew point and precipitation of the
very deterministic runs F is built from -- the same run, the same land cell
the 2 m temperature row was read at -- for every roster station.

Per product (what each publisher actually serves, probed 2026-10-18):

* ``IFS_HRES`` / ``AIFS`` -- ECMWF open data: ``ssrd`` and ``tp`` accumulated
  from the run's start, ``tcc`` and ``2d`` instantaneous.
* ``GFS`` -- ``DSWRF`` (period averages that restart every 6 h), ``TCDC``
  entire atmosphere (instant record), ``DPT`` 2 m, ``APCP`` (6 h buckets).
* ``AIGFS`` -- ``DPT`` 2 m and ``APCP`` only: the AI GFS publishes no
  radiation or cloud.
* ``GDPS`` -- one file a field on the MSC Datamart: downward shortwave and
  precipitation accumulated from the start, total cloud, dew point.
* ``ICON_GLOBAL`` -- DWD open data: ``ASWDIR_S`` + ``ASWDIFD_S`` (direct and
  diffuse, means from the start), ``CLCT``, ``TD_2M``, ``TOT_PREC``.

Every message is read as GRIB says it is -- stepType, start and end step,
units -- and stored as ``[start, end, kind, value]`` records in fixed units
(integrals in J m-2 and mm, cloud in %, dew point in C); anything else fails
closed.  The local-day numbers (``daily_json``) are derived from those
records only: an integral over consecutive read steps is a record that spans
them, or the difference of two records sharing a start; the intervals whose
midpoint falls inside the local day are summed and kept only when they cover
the whole day.  Cloud and dew point are means over the local 09-17 h steps
(one step for a 6-hourly product; ``*_day_n`` says how many).

Rows are ``reference_only`` research evidence: no P0, P0-M, P1 or lane reads
them.  A row's ``knowable_at_utc`` is its temperature row's (same run, same
publication; the fields sit in the same objects or beside them), and
``captured_at_utc`` says when this lane read it.  Only IFS HRES, AIFS, GFS and
AIGFS keep their history online, so only they are backfilled; GDPS and ICON
start from the first forward pass.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence

from . import aifs_single_point_archive as aifs
from . import native_deterministic_point_archive as native
from . import sqlite_busy
from .contracts import ContractError
from .noaa_conus_archive import GridPointReader, nearest_land_points
from .regional_point_lane import SYSTEM_IDS, utc_offset_hours

TABLE = "calibration_deterministic_point_physics_run"
SCHEMA_VERSION = "deterministic_point_physics_v1"
EXTRACTION_VERSION = "deterministic_point_physics_fields_v1"
AGGREGATION_VERSION = "local_day_midpoint_v1"
PRODUCTS = ("GDPS", "ICON_GLOBAL", "IFS_HRES", "AIFS", "GFS", "AIGFS")
BACKFILLABLE = ("IFS_HRES", "AIFS", "GFS", "AIGFS")
SYSTEM_BY_PRODUCT = {product: SYSTEM_IDS[product] for product in PRODUCTS}
PRODUCT_BY_SYSTEM = {system: product for product, system in SYSTEM_BY_PRODUCT.items()}

# variable -> the product's own name for it.  NOAA names are wgrib2 VAR:LEVEL
# with the record kind the inventory must show.
FIELDS: Mapping[str, Mapping[str, Any]] = {
    "IFS_HRES": {"sw": "ssrd", "tcc": "tcc", "td": "2d", "pr": "tp"},
    "AIFS": {"sw": "ssrd", "tcc": "tcc", "td": "2d", "pr": "tp"},
    "GFS": {
        "sw": ("DSWRF:surface", "avg"), "tcc": ("TCDC:entire atmosphere", "instant"),
        "td": ("DPT:2 m above ground", "instant"), "pr": ("APCP:surface", "accum"),
    },
    "AIGFS": {"td": ("DPT:2 m above ground", "instant"), "pr": ("APCP:surface", "accum")},
    "GDPS": {
        "sw": "DownwardShortwaveRadiationFlux-Accum_Sfc", "tcc": "TotalCloudCover_Sfc",
        "td": "DewPoint_AGL-2m", "pr": "Precip-Accum_Sfc",
    },
    "ICON_GLOBAL": {"sw_dir": "aswdir_s", "sw_dif": "aswdifd_s", "tcc": "clct", "td": "td_2m", "pr": "tot_prec"},
}
INTEGRALS = ("sw", "sw_dir", "sw_dif", "pr")
INSTANTS = ("tcc", "td")
DAYTIME_LOCAL_HOURS = (9, 17)
FETCH_WORKERS = 4
# One whole small file a field a step: latency-bound, not bandwidth-bound
# (a 10-step, 3-station GDPS or ICON run took ~180 s at four).
FETCH_WORKERS_BY_PRODUCT = {"GDPS": 8, "ICON_GLOBAL": 8}


# ---------------------------------------------------------------------------
# Where each field lives
# ---------------------------------------------------------------------------

def _stamp(run_init: datetime) -> datetime:
    return run_init.astimezone(timezone.utc)


def step_hours(product: str) -> int:
    return aifs.STEP_HOURS if product == "AIFS" else native.SPECS[product].step_hours


def local_day_steps(product: str, run_init: datetime, event_date: date, offset_hours: int) -> list[int]:
    if product == "AIFS":
        return aifs.local_day_steps(run_init=run_init, event_date=event_date, utc_offset_hours=offset_hours)
    return native.local_day_steps(
        product=product, run_init=run_init, event_date=event_date, utc_offset_hours=offset_hours,
    )


def wanted_steps(product: str, run_init: datetime, event_date: date, offset_hours: int) -> list[int]:
    """The local day's steps and the one before it (an integral needs both ends)."""

    steps = local_day_steps(product, run_init, event_date, offset_hours)
    if not steps:
        return []
    hours = step_hours(product)
    # The steps either side: an interval straddling midnight is the day's
    # when its midpoint is, and its far end lies outside the day.
    before = [steps[0] - hours] if steps[0] - hours >= 0 else []
    return [*before, *steps, steps[-1] + hours]


def gdps_url(run_init: datetime, step: int, name: str) -> str:
    stamp = _stamp(run_init)
    return (
        f"{native.ECCC_BASE_URL}/{stamp:%Y%m%d}/WXO-DD/model_gdps/15km/{stamp:%H}/{step:03d}/"
        f"{stamp:%Y%m%d}T{stamp:%H}Z_MSC_GDPS_{name}_LatLon0.15_PT{step:03d}H.grib2"
    )


def icon_url(run_init: datetime, step: int, field: str) -> str:
    stamp = _stamp(run_init)
    return (
        f"{native.DWD_ICON_BASE_URL}/{stamp:%H}/{field}/"
        f"icon_global_icosahedral_single-level_{stamp:%Y%m%d%H}_{step:03d}_{field.upper()}.grib2.bz2"
    )


def noaa_span(text: str, *, field: str, kind: str) -> tuple[int, int | None] | None:
    """(offset, length) of the first ``VAR:LEVEL`` record of ``kind`` in a wgrib2 inventory.

    ``kind`` reads the record's time field: ``avg`` "... ave fcst", ``accum``
    "... acc fcst", ``instant`` neither (a bare "N hour fcst" or "anl").
    """

    records: list[tuple[int, str, str]] = []
    for line in text.splitlines():
        parts = line.split(":")
        if len(parts) < 6:
            continue
        try:
            records.append((int(parts[1]), f"{parts[3]}:{parts[4]}", parts[5]))
        except ValueError:
            continue
    for position, (offset, name, when) in enumerate(records):
        if name != field:
            continue
        is_avg, is_acc = " ave" in when, " acc" in when
        if (kind == "avg" and is_avg) or (kind == "accum" and is_acc) or (
            kind == "instant" and not is_avg and not is_acc
        ):
            length = records[position + 1][0] - offset if position + 1 < len(records) else None
            return offset, length
    return None


def fetch_field(product: str, run_init: datetime, step: int, field: Any) -> bytes | None:
    """One field's GRIB message at one step, from the publisher's own object."""

    if product == "IFS_HRES":
        return native.read_message(product, run_init, step, field=str(field))
    if product == "AIFS":
        return aifs._message(run_init, step, str(field))
    if product in ("GFS", "AIGFS"):
        name, kind = field
        raw = native._get(str(native.index_url(product, run_init, step)), cap=native.MAX_INDEX_BYTES)
        if raw is None:
            return None
        span = noaa_span(raw.decode("utf-8", "replace"), field=name, kind=kind)
        if span is None:
            return None
        start, length = span
        rng = f"bytes={start}-" if length is None else f"bytes={start}-{start + length - 1}"
        return native._get(native.step_url(product, run_init, step), {"Range": rng}, cap=native.MAX_MESSAGE_BYTES)
    if product == "GDPS":
        return native._get(gdps_url(run_init, step, str(field)), cap=native.MAX_MESSAGE_BYTES)
    if product == "ICON_GLOBAL":
        return native._read_bz2_message(icon_url(run_init, step, str(field)))
    raise ContractError("deterministic_physics_product_unknown")


def mask(product: str, run_init: datetime) -> bytes | None:
    """The land field the temperature lane placed the stations with."""

    if product == "AIFS":
        return aifs._message(run_init, 0, "lsm")
    return native.mask_message(product, run_init)


# ---------------------------------------------------------------------------
# Decoding: what the message says it is, and its value at each cell
# ---------------------------------------------------------------------------

def _handle(message: bytes):
    from .dwd_icon_eps_archive import _eccodes  # noqa: PLC0415

    library = _eccodes()
    buffer = ctypes.create_string_buffer(message, len(message))
    handle = library.codes_handle_new_from_message(None, buffer, ctypes.c_size_t(len(message)))
    if not handle:
        raise ContractError("deterministic_physics_message_unreadable")
    return library, handle, buffer


def nearest_value(message: bytes, *, latitude: float, longitude: float) -> tuple[float, float]:
    """(value, distance_km) of the cell nearest the point: the eccodes search
    ``nearest_point_kelvin`` makes, without its 2 m temperature range check
    (these fields are not temperatures; ``normalise`` checks each one)."""

    from .noaa_conus_archive import _bind_nearest  # noqa: PLC0415

    library, handle, _buffer = _handle(message)
    _bind_nearest(library)
    try:
        err = ctypes.c_int()
        nearest = library.codes_grib_nearest_new(handle, ctypes.byref(err))
        if not nearest or err.value != 0:
            raise ContractError("deterministic_physics_nearest_unavailable")
        try:
            lats, lons = (ctypes.c_double * 4)(), (ctypes.c_double * 4)()
            values, distances = (ctypes.c_double * 4)(), (ctypes.c_double * 4)()
            indexes, length = (ctypes.c_int * 4)(), ctypes.c_size_t(4)
            if library.codes_grib_nearest_find(
                nearest, handle, ctypes.c_double(latitude), ctypes.c_double(longitude % 360.0), 0,
                lats, lons, values, distances, indexes, ctypes.byref(length),
            ) != 0:
                raise ContractError("deterministic_physics_nearest_find_failed")
            best = min(range(int(length.value)), key=lambda i: distances[i])
            return float(values[best]), float(distances[best])
        finally:
            library.codes_grib_nearest_delete(nearest)
    finally:
        library.codes_handle_delete(handle)


def message_meta(message: bytes) -> dict[str, Any]:
    library, handle, _buffer = _handle(message)
    try:
        text = native.RegularLatLonReader._string
        units = text(library, handle, b"units")
        if units in (None, "unknown"):
            # A centre's local parameter (GDPS cloud and precipitation) has no
            # entry in eccodes' tables; its GRIB2 parameter still names a unit.
            units = text(library, handle, b"parameterUnits")
        return {
            "step_type": text(library, handle, b"stepType"),
            "units": None if units is None else units.replace("m-2", "m**-2"),
            "short_name": text(library, handle, b"shortName"),
            "start": GridPointReader._long(library, handle, b"startStep"),
            "end": GridPointReader._long(library, handle, b"endStep"),
        }
    finally:
        library.codes_handle_delete(handle)


def normalise(var: str, meta: Mapping[str, Any], value: float) -> list[Any] | None:
    """``[start, end, kind, value]`` in the lane's units, or None for an
    implausible value (that station's record is dropped, never guessed).
    A step type or unit the lane does not know fails the run closed."""

    kind, units = str(meta.get("step_type")), str(meta.get("units"))
    start, end = meta.get("start"), meta.get("end")
    if start is None or end is None:
        raise ContractError("deterministic_physics_step_range_unreadable")
    start, end = int(start), int(end)
    base = var.split("_")[0]
    if base == "sw":
        if kind == "avg" and units == "W m**-2":
            total = float(value) * (end - start) * 3600.0
        elif kind == "accum" and units == "J m**-2":
            total = float(value)
        else:
            raise ContractError(f"deterministic_physics_sw_form:{kind}:{units}")
        ok = -1e3 <= total <= (end - start) * 3600.0 * 1500.0 + 1e3
        return [start, end, "integral", max(total, 0.0)] if ok else None
    if base == "pr":
        if kind != "accum":
            raise ContractError(f"deterministic_physics_pr_form:{kind}")
        if units == "kg m**-2":
            total = float(value)
        elif units == "m":
            total = float(value) * 1000.0
        else:
            raise ContractError(f"deterministic_physics_pr_units:{units}")
        return [start, end, "integral", max(total, 0.0)] if -0.5 <= total <= 3000.0 else None
    if kind != "instant":
        raise ContractError(f"deterministic_physics_instant_form:{var}:{kind}")
    if base == "tcc":
        if units in ("(0 - 1)", "0-1", "Proportion", "1"):
            pct = float(value) * 100.0
        elif units == "%":
            pct = float(value)
        else:
            raise ContractError(f"deterministic_physics_tcc_units:{units}")
        return [end, end, "instant", min(max(pct, 0.0), 100.0)] if -1.0 <= pct <= 101.0 else None
    if base == "td":
        if units != "K":
            raise ContractError(f"deterministic_physics_td_units:{units}")
        celsius = float(value) - 273.15
        return [end, end, "instant", celsius] if -90.0 <= celsius <= 40.0 else None
    raise ContractError("deterministic_physics_variable_unknown")


# ---------------------------------------------------------------------------
# The local day out of the records
# ---------------------------------------------------------------------------

def intervals(records: Sequence[Sequence[Any]]) -> list[tuple[int, int, float]]:
    """Integrals over consecutive read ends: a record spanning them, or the
    difference of two records that share a start."""

    by_end: dict[int, list[tuple[int, float]]] = {}
    for start, end, _kind, value in records:
        by_end.setdefault(int(end), []).append((int(start), float(value)))
    # A record's start is a boundary too: GFS's 18-21 bucket spans 18..21
    # whether or not step 18 itself was read.
    points = sorted(set(by_end) | {start for spans in by_end.values() for start, _v in spans})
    out: list[tuple[int, int, float]] = []
    for a, b in zip(points, points[1:]):
        if b not in by_end:
            continue
        total = None
        for start, value in by_end[b]:
            if start == a:
                total = value
                break
            match = [v for s, v in by_end.get(a, []) if s == start]
            if match:
                total = value - match[0]
                break
        if total is not None:
            out.append((a, b, max(total, 0.0)))
    return out


def local_day(
    fields: Mapping[str, Sequence[Sequence[Any]]], *, run_init: datetime, event_date: date, offset_hours: int,
) -> dict[str, Any]:
    """The local day's sums and daytime means, None where not fully covered."""

    start = datetime.combine(event_date, datetime.min.time(), tzinfo=timezone.utc) - timedelta(hours=offset_hours)
    stamp = _stamp(run_init)
    out: dict[str, Any] = {"aggregation_version": AGGREGATION_VERSION}

    def day_sum(var: str) -> tuple[float | None, float]:
        total, hours = 0.0, 0.0
        for a, b, value in intervals(fields.get(var) or []):
            mid = stamp + timedelta(hours=(a + b) / 2.0)
            if start <= mid < start + timedelta(days=1):
                total += value
                hours += b - a
        return (total if hours >= 24.0 - 1e-9 else None), hours

    sw_parts = ("sw_dir", "sw_dif") if "sw_dir" in fields else ("sw",)
    if all(part in fields for part in sw_parts):
        sums = [day_sum(part) for part in sw_parts]
        out["sw_hours"] = min(hours for _v, hours in sums)
        out["sw_mj"] = (
            round(sum(v for v, _h in sums) / 1e6, 3) if all(v is not None for v, _h in sums) else None
        )
    if "pr" in fields:
        value, hours = day_sum("pr")
        out["pr_hours"], out["pr_mm"] = hours, (round(value, 2) if value is not None else None)
    lo, hi = DAYTIME_LOCAL_HOURS
    for var in INSTANTS:
        if var not in fields:
            continue
        values = []
        for _start, end, _kind, value in fields[var]:
            local_hour = (stamp + timedelta(hours=int(end)) - start).total_seconds() / 3600.0
            if lo <= local_hour <= hi:
                values.append(float(value))
        out[f"{var}_day_n"] = len(values)
        out[f"{var}_day_mean"] = round(sum(values) / len(values), 2) if values else None
    td_all = [
        float(v) for _s, end, _k, v in fields.get("td") or []
        if 0 <= (stamp + timedelta(hours=int(end)) - start).total_seconds() < 86400
    ]
    if td_all:
        out["td_max"] = round(max(td_all), 2)
    return out


# ---------------------------------------------------------------------------
# One run for many station-days
# ---------------------------------------------------------------------------

def _cells(product: str, run_init: datetime, stations: Mapping[str, tuple[float, float]], first: bytes | None):
    """Station -> sampling point: the temperature lane's own placement rule."""

    if product == "ICON_GLOBAL":
        if first is None:
            return {}
        count = int(len(native._decode_values(first)))
        return native.icon_station_cells(run_init, stations, point_count=count)
    land = mask(product, run_init)
    if land is None:
        return {}
    order = sorted(stations)
    return dict(zip(order, nearest_land_points(land, [stations[s] for s in order])))


def fetch_run(
    *, product: str, run_init: datetime, targets: Sequence[Mapping[str, Any]],
    fetch: Callable[[str, datetime, int, Any], bytes | None] = fetch_field,
) -> dict[tuple[str, str], dict[str, Any]]:
    """Every target's field records from one read per (step, field)."""

    wanted: dict[tuple[str, str], list[int]] = {}
    stations: dict[str, tuple[float, float]] = {}
    for target in targets:
        key = (str(target["station_id"]), target["event_date"].isoformat())
        wanted[key] = wanted_steps(product, run_init, target["event_date"], int(target["utc_offset_hours"]))
        stations.setdefault(key[0], (float(target["latitude"]), float(target["longitude"])))
    union = sorted({step for steps in wanted.values() for step in steps})
    if not union:
        return {}
    jobs = [(step, var, field) for step in union for var, field in FIELDS[product].items()]
    first = None
    if product == "ICON_GLOBAL":
        # ICON's cells are placed on the grid the run's own field declares.
        first = fetch(product, run_init, union[-1], FIELDS[product]["td"])
    cells = _cells(product, run_init, stations, first)
    placed = [s for s in sorted(stations) if cells.get(s) is not None]
    if not placed:
        return {}
    reader = None
    if product != "ICON_GLOBAL":
        reader = native.RegularLatLonReader(
            [(float(cells[s]["latitude"]), float(cells[s]["longitude"])) for s in placed], slow=nearest_value,
        )
    per_station: dict[str, dict[str, dict[int, list[Any]]]] = {s: {} for s in placed}

    def decoded(message: bytes) -> list[float | None]:
        if product == "ICON_GLOBAL":
            field = native._decode_values(message)
            return [float(field[int(cells[s]["index"])]) for s in placed]
        fast = reader.kelvin(message)
        out = []
        for station, value in zip(placed, fast):
            if value is None:
                value, _distance = nearest_value(
                    message, latitude=float(cells[station]["latitude"]),
                    longitude=float(cells[station]["longitude"]),
                )
            out.append(float(value))
        return out

    # In batches of one per worker: ``map`` holds every result it has fetched,
    # and an ICON message is ~12 MB decompressed, so at most one batch of
    # fields is in memory at a time.
    workers = FETCH_WORKERS_BY_PRODUCT.get(product, FETCH_WORKERS)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for begin in range(0, len(jobs), workers):
            batch = jobs[begin:begin + workers]
            for (step, var, _field), message in zip(batch, pool.map(lambda j: fetch(product, run_init, j[0], j[2]), batch)):
                if message is None:
                    continue
                meta = message_meta(message)
                for station, value in zip(placed, decoded(message)):
                    record = normalise(var, meta, value)
                    if record is not None:
                        per_station[station].setdefault(var, {})[step] = record
    results: dict[tuple[str, str], dict[str, Any]] = {}
    for key, steps in wanted.items():
        station = key[0]
        if station not in per_station:
            continue
        fields = {
            var: [by_step[s] for s in steps if s in by_step]
            for var, by_step in per_station[station].items()
        }
        fields = {var: records for var, records in fields.items() if records}
        if not fields:
            continue
        cell = cells[station]
        results[key] = {
            "fields": fields, "steps": steps,
            "cell": {k: cell[k] for k in ("latitude", "longitude", "moved", "index") if k in cell},
        }
    return results


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS {TABLE} (
            physics_run_id TEXT PRIMARY KEY,
            ensemble_system_id TEXT NOT NULL,
            station_id TEXT NOT NULL,
            target_local_date TEXT NOT NULL,
            settlement_timezone TEXT NOT NULL,
            run_init_utc TEXT NOT NULL,
            knowable_at_utc TEXT NOT NULL,
            captured_at_utc TEXT NOT NULL,
            capture_mode TEXT NOT NULL CHECK (capture_mode IN ('forward','backfill')),
            fields_json TEXT NOT NULL,
            daily_json TEXT NOT NULL,
            cell_json TEXT NOT NULL,
            steps_json TEXT NOT NULL,
            extraction_version TEXT NOT NULL,
            schema_version TEXT NOT NULL,
            reference_only INTEGER NOT NULL CHECK (reference_only = 1),
            content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
            UNIQUE (ensemble_system_id, station_id, target_local_date, run_init_utc, extraction_version)
        )"""
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS {TABLE}_scope_idx ON {TABLE}(station_id, target_local_date, ensemble_system_id)"
    )
    for verb in ("UPDATE", "DELETE"):
        conn.execute(
            f"CREATE TRIGGER IF NOT EXISTS {TABLE}_no_{verb.lower()} BEFORE {verb} ON {TABLE}"
            f" BEGIN SELECT RAISE(ABORT, '{TABLE}_append_only'); END"
        )


def write_row(
    conn: sqlite3.Connection, *, product: str, station_id: str, event_date: date, settlement_timezone: str,
    run_init: datetime, knowable_at_utc: str, captured_at: datetime, capture_mode: str,
    captured: Mapping[str, Any],
) -> bool:
    system = SYSTEM_BY_PRODUCT[product]
    run_iso = _stamp(run_init).strftime("%Y-%m-%dT%H:%M:%SZ")
    daily = local_day(
        captured["fields"], run_init=run_init, event_date=event_date,
        offset_hours=utc_offset_hours(event_date, settlement_timezone),
    )
    identity = {
        "system": system, "station": station_id, "date": event_date.isoformat(), "run": run_iso,
        "fields": captured["fields"], "extraction_version": EXTRACTION_VERSION,
    }
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    with sqlite_busy.blocked_when_busy():
        cursor = conn.execute(
            f"INSERT OR IGNORE INTO {TABLE} VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"phys-{digest[:32]}", system, station_id, event_date.isoformat(), settlement_timezone, run_iso,
                knowable_at_utc, captured_at.strftime("%Y-%m-%dT%H:%M:%SZ"), capture_mode,
                json.dumps(captured["fields"], sort_keys=True), json.dumps(daily, sort_keys=True),
                json.dumps(captured["cell"], sort_keys=True), json.dumps(captured["steps"]),
                EXTRACTION_VERSION, SCHEMA_VERSION, 1, digest,
            ),
        )
    return int(cursor.rowcount or 0) > 0


# ---------------------------------------------------------------------------
# What to read: the temperature rows still without physics
# ---------------------------------------------------------------------------

def plan(
    conn: sqlite3.Connection, *, products: Sequence[str], since: date, until: date,
    stations: Mapping[str, tuple[float, float]], only: set[tuple[str, str, str, str]] | None = None,
) -> list[dict[str, Any]]:
    """One item per (product, run): the station-days its temperature rows
    cover that have no physics row of this extraction yet, newest run first.

    ``only`` narrows to (system, station, date, run) keys (the backfill's
    listing-instant runs)."""

    systems = [SYSTEM_BY_PRODUCT[p] for p in products]
    marks = ",".join("?" * len(systems))
    rows = conn.execute(
        f"SELECT r.ensemble_system_id, r.run_init_utc, r.station_id, r.target_local_date,"
        f" r.settlement_timezone, MIN(r.knowable_at_utc)"
        f" FROM calibration_regional_point_run r"
        f" WHERE r.ensemble_system_id IN ({marks}) AND r.target_local_date BETWEEN ? AND ?"
        f" AND NOT EXISTS (SELECT 1 FROM {TABLE} p WHERE p.ensemble_system_id=r.ensemble_system_id"
        f"  AND p.station_id=r.station_id AND p.target_local_date=r.target_local_date"
        f"  AND p.run_init_utc=r.run_init_utc AND p.extraction_version=?)"
        f" GROUP BY 1,2,3,4,5",
        (*systems, since.isoformat(), until.isoformat(), EXTRACTION_VERSION),
    ).fetchall()
    items: dict[tuple[str, str], dict[str, Any]] = {}
    for system, run_iso, station, day, zone, knowable in rows:
        if only is not None and (system, station, day, run_iso) not in only:
            continue
        if station not in stations:
            continue
        product = PRODUCT_BY_SYSTEM[system]
        item = items.setdefault((product, run_iso), {
            "product": product, "run_init": datetime.fromisoformat(run_iso.replace("Z", "+00:00")),
            "targets": [],
        })
        event_date = date.fromisoformat(day)
        lat, lon = stations[station]
        item["targets"].append({
            "station_id": station, "event_date": event_date, "settlement_timezone": zone,
            "utc_offset_hours": utc_offset_hours(event_date, zone), "latitude": lat, "longitude": lon,
            "knowable_at_utc": knowable,
        })
    return sorted(items.values(), key=lambda item: (item["run_init"], item["product"]), reverse=True)


def capture(
    conn: sqlite3.Connection, items: Sequence[Mapping[str, Any]], *, now: datetime, capture_mode: str,
    deadline: float | None = None, clock: Callable[[], float] | None = None,
    should_yield: Callable[[str], str] | None = None, log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Fetch, write and commit one run at a time (never a fetch inside an
    open transaction); a lock it cannot take stops the pass, not the lane."""

    import time  # noqa: PLC0415

    clock = clock or time.monotonic
    written = runs = 0
    failed: list[str] = []
    stopped = ""
    for item in items:
        if deadline is not None and clock() >= deadline:
            stopped = "budget"
            break
        product, run_init = item["product"], item["run_init"]
        if should_yield is not None and (reason := should_yield(product)):
            log(f"{product}: {reason}，本遍让路")
            continue
        try:
            results = fetch_run(product=product, run_init=run_init, targets=item["targets"])
        except Exception as exc:  # noqa: BLE001 -- one run's failure is a line, not a stop
            from .pass_deadline import PassDeadlineExceeded  # noqa: PLC0415

            if isinstance(exc, PassDeadlineExceeded):
                stopped = "budget"
                break
            failed.append(f"{product} {run_init:%Y-%m-%dT%HZ} {type(exc).__name__}: {str(exc)[:100]}")
            continue
        if not results:
            failed.append(f"{product} {run_init:%Y-%m-%dT%HZ} 无数据")
            continue
        try:
            run_written = 0
            for target in item["targets"]:
                captured = results.get((target["station_id"], target["event_date"].isoformat()))
                if not captured:
                    continue
                run_written += int(write_row(
                    conn, product=product, station_id=target["station_id"], event_date=target["event_date"],
                    settlement_timezone=target["settlement_timezone"], run_init=run_init,
                    knowable_at_utc=target["knowable_at_utc"], captured_at=now, capture_mode=capture_mode,
                    captured=captured,
                ))
            with sqlite_busy.blocked_when_busy():
                conn.commit()
        except sqlite_busy.WriteBlocked as exc:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            stopped = f"write_blocked {product} {run_init:%Y-%m-%dT%HZ} {exc}"
            break
        written += run_written
        runs += 1
        log(f"{product} {run_init:%Y-%m-%dT%HZ}: 站日 {len(item['targets'])}，写入 {run_written}")
    return {"runs": runs, "written": written, "failed": failed, "stopped": stopped}


def read_daily(
    conn: sqlite3.Connection, *, station_id: str, event_date: str, as_of_utc: str,
) -> dict[str, dict[str, Any]]:
    """Per system: the newest run's local-day physics knowable at ``as_of_utc``."""

    out: dict[str, dict[str, Any]] = {}
    for system, run_iso, daily in conn.execute(
        f"SELECT ensemble_system_id, run_init_utc, daily_json FROM {TABLE}"
        " WHERE station_id=? AND target_local_date=? AND knowable_at_utc<=? AND extraction_version=?"
        " ORDER BY run_init_utc DESC",
        (station_id, event_date, as_of_utc, EXTRACTION_VERSION),
    ):
        if system not in out:
            out[system] = {"run_init_utc": run_iso, **json.loads(daily)}
    return out


__all__ = [
    "BACKFILLABLE", "EXTRACTION_VERSION", "FIELDS", "PRODUCTS", "TABLE", "capture", "ensure_schema",
    "fetch_field", "fetch_run", "intervals", "local_day", "message_meta", "noaa_span", "normalise", "plan",
    "read_daily", "wanted_steps", "write_row",
]
