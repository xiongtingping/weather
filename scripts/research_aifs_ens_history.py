#!/usr/bin/env python3
"""Research only: ECMWF AIFS ENS members for the runs PF50 actually served.

The question this answers is whether AIFS ENS belongs in the P0 stable pool,
and the only honest way to ask it is through production's own replay: the
same walk-forward fits, bridge and gate tables, with AIFS added as one more
member.  So this builds AIFS rows in the exact shape of
``calibration_native_ensemble_historical_member_run`` -- but into a separate
research database, never the SSOT -- for exactly the runs the IFS PF50 history
holds in the window, paired run for run:

* same initialisation time as the PF50 run, same station-days it served;
* ``archive_query_at_utc`` copied from that PF50 row, so in the replay AIFS
  arrives when IFS did.  Its real advantage -- it publishes 1h08m-1h41m
  earlier -- is deliberately left out; what is measured is skill alone.

AIFS ENS open data carries no period maximum (IFS has ``mx2t3``), only the
instantaneous ``2t`` every six hours, so a member's daily high is the largest
of the instants inside the station's local day: all four for a run issued
before the day starts, whatever remains for a run issued after (the same
floor the PF50 collector applies to its same-day runs).

Objects come from the official ECMWF open-data replica on GCS -- the S3
regional endpoint throttles single requests (see the native backfill lane)
-- one ``2t`` message per member and step by byte range from the step's
``.index``; nothing raw is kept.  Values are read at each station's nearest
grid cell by index, and on the first message of every run each station's
index is proven bit-identical against eccodes' own nearest-point search;
a mismatch stops the run rather than falling back.

Resumable and bounded: runs already written are skipped, and
``--max-seconds`` ends the pass between runs.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import sqlite3
import sys
import threading
import time
import zlib
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.dwd_icon_eps_archive import _eccodes  # noqa: E402
from bayes_basket_target.native_archive_lead import (  # noqa: E402
    capture_lead_bucket,
)
from bayes_basket_target.noaa_conus_archive import (  # noqa: E402
    nearest_point_kelvin,
)
from bayes_basket_target.station_registry import (  # noqa: E402
    coordinate_registry_manifest,
    station_coordinates,
)

SSOT = ROOT / "data" / "bayes_basket_target_ssot.db"
# Internal disk, outside data/runtime (retention lanes delete by age there)
# and outside data/research (an offloaded symlink to the external volume).
DEFAULT_DB = ROOT / "data" / "aifs_ens_research" / "aifs_ens_members.db"
GCS = "https://storage.googleapis.com/ecmwf-open-data"
SYSTEM_ID = "ECMWF_AIFS_ENS_GLOBAL"
PAIRED_SYSTEM_ID = "ECMWF_IFS_ENS_OPEN_PF50"
PROVIDER_ID = "ECMWF"
# The same official objects PF50's rows name, fetched through the same GCS
# replica; the transport id is the one the OOS reader admits for ECMWF.
TRANSPORT_PROVIDER_ID = "ECMWF_OPEN_DATA_S3"
EXTRACTION_TEMPLATE = "research_ecmwf_open_data_aifs_ens_0p25_2t6_{cycle}z_d{lead}_v1"
STEP_HOURS = 6
MAX_STEP = 360
MEMBERS = ("cf",) + tuple(f"pf{n:02d}" for n in range(1, 51))
MAX_MESSAGE_BYTES = 2 * 1024 * 1024
KELVIN = 273.15

# Regular lat-lon, north to south, no bitmap, rows all running the same way:
# for this layout the latitudes/longitudes iterator and ``values`` share an
# order, which is what indexing by the iterator's position relies on.
_REGULAR_LL_TEMPLATE = 0
_LAYOUT_KEYS = (
    b"gridDefinitionTemplateNumber", b"iScansNegatively", b"jScansPositively",
    b"bitmapPresent", b"alternativeRowScanning", b"Ni", b"Nj",
)

_LIBRARY_LOCK = threading.Lock()
_LIBRARY: ctypes.CDLL | None = None


def _library() -> ctypes.CDLL:
    global _LIBRARY
    with _LIBRARY_LOCK:
        if _LIBRARY is None:
            _LIBRARY = _eccodes()
        return _LIBRARY


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# plan: the PF50 runs production served in the window, paired station-days
# ---------------------------------------------------------------------------

def load_plan(start: str, end: str) -> dict[str, dict[tuple[str, str], str]]:
    """PF50 run init -> {(station, target date): PF50 arrival stamp}."""

    fleet = set(coordinate_registry_manifest()["station_ids"])
    conn = sqlite3.connect(f"file:{SSOT}?mode=ro", uri=True, timeout=60)
    try:
        rows = conn.execute(
            "SELECT station_id,target_local_date,native_run_init_utc,"
            "MIN(archive_query_at_utc) "
            "FROM calibration_native_ensemble_historical_member_run "
            "WHERE ensemble_system_id=? AND target_local_date BETWEEN ? AND ? "
            "AND qc_status='ACCEPTED' AND native_run_init_utc<>'' "
            "GROUP BY 1,2,3",
            (PAIRED_SYSTEM_ID, start, end),
        ).fetchall()
    finally:
        conn.close()
    plan: dict[str, dict[tuple[str, str], str]] = defaultdict(dict)
    for station, target, run_init, arrival in rows:
        if str(station) in fleet and arrival:
            plan[_stamp(_utc(run_init))][(str(station), str(target))] = str(arrival)
    return dict(plan)


def load_timezones() -> dict[str, str]:
    conn = sqlite3.connect(f"file:{SSOT}?mode=ro", uri=True, timeout=60)
    try:
        return {
            str(station): str(zone)
            for station, zone in conn.execute(
                "SELECT station_id,settlement_timezone "
                "FROM calibration_station_market_timezone_binding"
            )
        }
    finally:
        conn.close()


def day_instants(*, run_init: datetime, target: str, zone: str) -> list[int] | None:
    """Steps whose valid instant falls inside the station's local day.

    None when the run cannot describe the day: short of all four instants
    for a run issued before the day starts, or none left for one issued
    after -- the PF50 collector's same-day floor, restated for instants.
    """

    day = date.fromisoformat(target)
    start = datetime(day.year, day.month, day.day, tzinfo=ZoneInfo(zone)).astimezone(timezone.utc)
    end = start + timedelta(days=1)
    steps = [
        step for step in range(0, MAX_STEP + 1, STEP_HOURS)
        if start <= run_init + timedelta(hours=step) < end
    ]
    same_day = run_init >= start
    if not steps or (not same_day and len(steps) != 24 // STEP_HOURS):
        return None
    return steps


# ---------------------------------------------------------------------------
# transport and decode
# ---------------------------------------------------------------------------

_CLIENTS = threading.local()


def _client() -> httpx.Client:
    client = getattr(_CLIENTS, "client", None)
    if client is None:
        client = httpx.Client(timeout=60.0, follow_redirects=True)
        _CLIENTS.client = client
    return client


def _get(url: str, *, headers: dict[str, str] | None = None) -> bytes:
    last: Exception | None = None
    for attempt in range(5):
        try:
            response = _client().get(url, headers=headers)
            response.raise_for_status()
            return response.content
        except (httpx.HTTPError, OSError) as exc:
            last = exc
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status == 404:
                break
            time.sleep(min(1.0 * (2 ** attempt), 16.0))
    raise RuntimeError(f"aifs_fetch_failed:{url}:{type(last).__name__}:{last}")


def _prefix(run_init: datetime, step: int, kind: str) -> str:
    day = run_init.strftime("%Y%m%d")
    cycle = run_init.strftime("%H")
    stamp = run_init.strftime("%Y%m%d%H%M%S")
    return f"{GCS}/{day}/{cycle}z/aifs-ens/0p25/enfo/{stamp}-{step}h-enfo-{kind}"


def step_messages(run_init: datetime, step: int) -> dict[str, dict]:
    """member -> index row of its ``2t`` message at this step."""

    found: dict[str, dict] = {}
    for kind in ("cf", "pf"):
        raw = _get(_prefix(run_init, step, kind) + ".index")
        for line in raw.decode("utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("param") != "2t" or row.get("levtype") != "sfc":
                continue
            if int(row.get("step") or -1) != step:
                continue
            member = "cf" if kind == "cf" else f"pf{int(row['number']):02d}"
            if member in found:
                raise RuntimeError(f"aifs_index_duplicate_member:{member}:{step}")
            length = int(row["_length"])
            if not 16 < length <= MAX_MESSAGE_BYTES:
                raise RuntimeError(f"aifs_index_range_invalid:{member}:{step}")
            found[member] = {**row, "_kind": kind}
    if set(found) != set(MEMBERS):
        raise RuntimeError(
            f"aifs_index_members_incomplete:{step}:{len(found)}"
        )
    return found


class StationIndex:
    """Each station's position in ``values``, proven against eccodes once."""

    def __init__(self, stations: list[str]) -> None:
        self.stations = stations
        self.points = [
            (float(station_coordinates(s)["latitude"]), float(station_coordinates(s)["longitude"]))
            for s in stations
        ]
        self.by_grid: dict[str, list[int]] = {}

    @staticmethod
    def _long(library, handle, key: bytes) -> int | None:
        value = ctypes.c_long()
        if library.codes_get_long(handle, key, ctypes.byref(value)) != 0:
            return None
        return int(value.value)

    @staticmethod
    def _array(library, handle, key: bytes) -> np.ndarray:
        size = ctypes.c_size_t()
        if library.codes_get_size(handle, key, ctypes.byref(size)) != 0:
            raise RuntimeError(f"aifs_grib_size_unreadable:{key!r}")
        out = np.empty(size.value, dtype=np.float64)
        length = ctypes.c_size_t(size.value)
        if library.codes_get_double_array(
            handle, key, out.ctypes.data_as(ctypes.POINTER(ctypes.c_double)), ctypes.byref(length),
        ) != 0:
            raise RuntimeError(f"aifs_grib_array_unreadable:{key!r}")
        return out

    def _layout(self, library, handle) -> dict[bytes, int | None]:
        return {key: self._long(library, handle, key) for key in _LAYOUT_KEYS}

    def values_at_stations(self, message: bytes, *, prove: bool) -> np.ndarray:
        library = _library()
        buffer = ctypes.create_string_buffer(message, len(message))
        handle = library.codes_handle_new_from_message(None, buffer, len(message))
        if not handle:
            raise RuntimeError("aifs_grib_unreadable")
        try:
            layout = self._layout(library, handle)
            key = repr(tuple(layout.values()))
            if not (
                layout[b"gridDefinitionTemplateNumber"] == _REGULAR_LL_TEMPLATE
                and layout[b"iScansNegatively"] == 0
                and layout[b"jScansPositively"] == 0
                and layout[b"bitmapPresent"] == 0
                and layout[b"alternativeRowScanning"] == 0
            ):
                raise RuntimeError(f"aifs_grid_layout_unsupported:{key}")
            values = self._array(library, handle, b"values")
            indexes = self.by_grid.get(key)
            if indexes is None:
                lat = np.radians(self._array(library, handle, b"latitudes"))
                lon = np.radians(self._array(library, handle, b"longitudes") % 360.0)
                sin_lat, cos_lat = np.sin(lat), np.cos(lat)
                indexes = []
                for p_lat, p_lon in self.points:
                    a, b = math.radians(p_lat), math.radians(p_lon % 360.0)
                    indexes.append(int(np.argmax(
                        math.sin(a) * sin_lat + math.cos(a) * cos_lat * np.cos(lon - b)
                    )))
                self.by_grid[key] = indexes
                prove = True
            out = values[indexes]
        finally:
            library.codes_handle_delete(handle)
        if prove:
            for station, (p_lat, p_lon), value in zip(self.stations, self.points, out):
                reference, _distance = nearest_point_kelvin(message, latitude=p_lat, longitude=p_lon)
                if abs(float(reference) - float(value)) > 1e-9:
                    raise RuntimeError(
                        f"aifs_index_disagrees_with_eccodes:{station}:{value}:{reference}"
                    )
        if not np.all((out > 180.0) & (out < 340.0)):
            raise RuntimeError("aifs_value_out_of_range")
        return out


# ---------------------------------------------------------------------------
# research database
# ---------------------------------------------------------------------------

def open_research_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=60)
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='calibration_native_ensemble_historical_member_run'"
    ).fetchone()
    if not exists:
        src = sqlite3.connect(f"file:{SSOT}?mode=ro", uri=True, timeout=60)
        try:
            ddl = src.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='calibration_native_ensemble_historical_member_run'"
            ).fetchone()[0]
        finally:
            src.close()
        conn.execute(ddl)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS research_aifs_member_scope "
            "ON calibration_native_ensemble_historical_member_run"
            "(station_id,target_local_date,ensemble_system_id)"
        )
    # The instants themselves, not only the daily highs derived from them:
    # which estimator turns four six-hourly samples into a daily high is the
    # production decision this data exists to inform, and an estimator can
    # only be compared on the samples.  float32 kelvin, zlib, one blob a run.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS research_aifs_run_samples("
        " run_init_utc TEXT PRIMARY KEY, steps_json TEXT, members_json TEXT,"
        " stations_json TEXT, kelvin_f32_zlib BLOB)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS research_aifs_runs("
        " run_init_utc TEXT PRIMARY KEY, steps INTEGER, messages INTEGER,"
        " bytes INTEGER, seconds REAL, station_days INTEGER, rows INTEGER,"
        " skipped_station_days INTEGER, finished_at_utc TEXT)"
    )
    conn.commit()
    return conn


def build_rows(
    *,
    run_init: datetime,
    pairs: dict[tuple[str, str], str],
    timezones: dict[str, str],
    stations: list[str],
    samples: dict[tuple[str, int], np.ndarray],
    message_sha: dict[tuple[str, int], str],
) -> tuple[list[tuple], int]:
    rows: list[tuple] = []
    skipped = 0
    position = {s: i for i, s in enumerate(stations)}
    cycle = run_init.strftime("%H")
    for (station, target), arrival in sorted(pairs.items()):
        zone = timezones.get(station)
        steps = day_instants(run_init=run_init, target=target, zone=zone) if zone else None
        lead = (date.fromisoformat(target) - run_init.date()).days
        if steps is None or lead not in (0, 1, 2) or station not in position:
            skipped += 1
            continue
        if any((member, step) not in samples for member in MEMBERS for step in steps):
            skipped += 1
            continue
        i = position[station]
        extraction = EXTRACTION_TEMPLATE.format(cycle=cycle, lead=lead)
        bucket = capture_lead_bucket(event_date=target, settlement_timezone=zone, run_init=run_init)
        receipt = content_sha256(sorted(message_sha[(m, s)] for m in MEMBERS for s in steps))
        for member in MEMBERS:
            tmax_c = max(float(samples[(member, s)][i]) for s in steps) - KELVIN
            member_id = "control" if member == "cf" else f"perturbed_{member[2:]}"
            role = "CONTROL" if member == "cf" else "PERTURBED"
            identity = {
                "ensemble_system_id": SYSTEM_ID,
                "station_id": station,
                "target_local_date": target,
                "archive_lead_bucket": bucket,
                "run_cycle": f"{cycle}Z",
                "member_id": member_id,
                "raw_tmax_c": tmax_c,
                "extraction_version": extraction,
                "native_run_init_utc": _stamp(run_init),
                "source_object_sha256": receipt,
            }
            sha = content_sha256(identity)
            rows.append((
                f"aifs-research-member-{sha[:28]}", SYSTEM_ID, station, target, bucket,
                f"{cycle}Z", member_id, role, tmax_c, "official_daily_tmax_c", extraction,
                PROVIDER_ID, TRANSPORT_PROVIDER_ID, _stamp(run_init), 1, arrival,
                f"{GCS}/{run_init.strftime('%Y%m%d')}/{cycle}z/aifs-ens/0p25/enfo/",
                f"aifs_field_receipt_set_sha256:{receipt}", receipt, "ACCEPTED",
                "CALIBRATION_TRAIN_ONLY", 0, sha,
            ))
    return rows, skipped


INSERT = (
    "INSERT OR IGNORE INTO calibration_native_ensemble_historical_member_run("
    "historical_member_run_id,ensemble_system_id,station_id,target_local_date,"
    "archive_lead_bucket,run_cycle,member_id,member_role,raw_tmax_c,"
    "settlement_definition,extraction_version,provider_id,transport_provider_id,"
    "native_run_init_utc,offline_oos_eligible,archive_query_at_utc,source_url,"
    "raw_object_ref,source_object_sha256,qc_status,authority_scope,"
    "holdout_eligible,content_sha256) VALUES(" + ",".join("?" * 23) + ")"
)


def capture_run(
    *,
    run_init: datetime,
    pairs: dict[tuple[str, str], str],
    timezones: dict[str, str],
    index: StationIndex,
    workers: int,
) -> tuple[list[tuple], dict]:
    steps = sorted({
        step
        for (station, target) in pairs
        if timezones.get(station)
        for step in (day_instants(run_init=run_init, target=target, zone=timezones[station]) or [])
    })
    started = time.monotonic()
    catalog: dict[tuple[str, int], dict] = {}
    with ThreadPoolExecutor(workers) as pool:
        for step, found in zip(steps, pool.map(lambda s: step_messages(run_init, s), steps)):
            for member, row in found.items():
                catalog[(member, step)] = row
    samples: dict[tuple[str, int], np.ndarray] = {}
    message_sha: dict[tuple[str, int], str] = {}
    fetched = 0
    proven = threading.Event()

    def fetch(key: tuple[str, int]):
        row = catalog[key]
        url = _prefix(run_init, key[1], row["_kind"]) + ".grib2"
        start = int(row["_offset"])
        message = _get(url, headers={"Range": f"bytes={start}-{start + int(row['_length']) - 1}"})
        if len(message) != int(row["_length"]) or not message.startswith(b"GRIB"):
            raise RuntimeError(f"aifs_message_truncated:{key}")
        prove = not proven.is_set()
        values = index.values_at_stations(message, prove=prove)
        if prove:
            proven.set()
        return key, values, hashlib.sha256(message).hexdigest(), len(message)

    ordered = sorted(catalog)
    # Prove the index on one message before fanning out.
    key, values, sha, size = fetch(ordered[0])
    samples[key], message_sha[key] = values, sha
    fetched += size
    with ThreadPoolExecutor(workers) as pool:
        for key, values, sha, size in pool.map(fetch, ordered[1:]):
            samples[key], message_sha[key] = values, sha
            fetched += size
    rows, skipped = build_rows(
        run_init=run_init, pairs=pairs, timezones=timezones,
        stations=index.stations, samples=samples, message_sha=message_sha,
    )
    cube = np.array(
        [[samples[(member, step)] for member in MEMBERS] for step in steps],
        dtype=np.float32,
    )
    stats_blob = (
        json.dumps(steps), json.dumps(list(MEMBERS)), json.dumps(index.stations),
        zlib.compress(cube.tobytes(), 6),
    )
    stats = {
        "blob": stats_blob,
        "steps": len(steps), "messages": len(catalog), "bytes": fetched,
        "seconds": round(time.monotonic() - started, 1),
        "station_days": len(pairs) - skipped, "rows": len(rows), "skipped": skipped,
    }
    return rows, stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2026-07-01")
    parser.add_argument("--end", default="2026-09-20")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--max-seconds", type=float, default=540.0)
    parser.add_argument("--max-runs", type=int, default=0)
    parser.add_argument("--run", action="append", default=[],
                        help="only these run inits (e.g. 2026-08-20T00:00:00Z)")
    args = parser.parse_args()

    deadline = time.monotonic() + float(args.max_seconds)
    plan = load_plan(args.start, args.end)
    timezones = load_timezones()
    stations = sorted(coordinate_registry_manifest()["station_ids"])
    index = StationIndex(stations)
    conn = open_research_db(args.db)
    done = {row[0] for row in conn.execute("SELECT run_init_utc FROM research_aifs_runs")}
    # Runs captured before the samples table existed have rows but no
    # samples; they are fetched again (rows are INSERT OR IGNORE, so the
    # member rows stay exactly as first written) once the rest is done.
    sampled = {row[0] for row in conn.execute("SELECT run_init_utc FROM research_aifs_run_samples")}
    wanted = [r for r in sorted(plan, reverse=True) if r not in done]
    wanted += [r for r in sorted(plan, reverse=True) if r in done and r not in sampled]
    if args.run:
        wanted = [r for r in wanted if r in set(args.run)]
    summary = {"planned_runs": len(plan), "already_done": len(done), "this_pass": []}
    for run in wanted:
        if args.max_runs and len(summary["this_pass"]) >= args.max_runs:
            break
        if time.monotonic() >= deadline:
            break
        rows, stats = capture_run(
            run_init=_utc(run), pairs=plan[run], timezones=timezones,
            index=index, workers=int(args.workers),
        )
        blob = stats.pop("blob")
        with conn:
            conn.executemany(INSERT, rows)
            conn.execute(
                "INSERT OR REPLACE INTO research_aifs_run_samples VALUES(?,?,?,?,?)",
                (run, *blob),
            )
            conn.execute(
                "INSERT OR REPLACE INTO research_aifs_runs VALUES(?,?,?,?,?,?,?,?,?)",
                (run, stats["steps"], stats["messages"], stats["bytes"], stats["seconds"],
                 stats["station_days"], stats["rows"], stats["skipped"],
                 _stamp(datetime.now(timezone.utc))),
            )
        summary["this_pass"].append({"run": run, **stats})
        mb = stats["bytes"] / 1e6
        print(
            f"{run}  steps={stats['steps']:2d} msgs={stats['messages']:4d} "
            f"{mb:6.1f}MB {stats['seconds']:6.1f}s ({mb / max(stats['seconds'], 0.1):.2f}MB/s) "
            f"station_days={stats['station_days']} skipped={stats['skipped']}",
            flush=True,
        )
    remaining = len([r for r in plan if r not in done or r not in sampled]) - len(summary["this_pass"])
    summary["remaining_runs"] = remaining
    print(json.dumps({k: v for k, v in summary.items() if k != "this_pass"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
