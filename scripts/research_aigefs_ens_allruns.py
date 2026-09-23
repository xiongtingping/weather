#!/usr/bin/env python3
"""Research only: NOAA AIGEFS (the EAGLE AI ensemble) -- every run, seated, newest used.

The AIFS ENS lane answered whether an AI ensemble belongs in the P0 stable
pool through production's own replay (``research_aifs_ens_allruns.py`` for
the rows, ``research_replay_aifs_stable_pool.py`` for the arms).  This does
the same for NOAA's AIGEFS, the SSOT system ``NOAA_AIGEFS_025`` (family
``NOAA_AIGEFS``, 31 members) that was registered through the Open-Meteo era
and never captured from its official source.  Rows go to their own research
database (``data/aigefs_ens_research/aigefs_ens_allruns.db``, a symlink to
the external volume), never to the SSOT, in the member-row shape production's
replay reads.

Source (verified by HTTP listing 2026-09-23): the public NODD bucket
``noaa-nws-graphcastgfs-pds``, prefix
``EAGLE_ensemble/aigefs.YYYYMMDD/HH/memNNN/model/atmos/grib2/`` with all four
cycles, member directories ``mem000``..``mem030`` (``mem000`` carries
``ENS=+0`` in the index and is the control; ``memNNN`` carries ``ENS=+N``),
files ``aigefs.tHHz.sfc.fFFF.grib2`` every six hours to 384h with an NCEP
``.idx``.  A surface file is about 5.3 MB; the ``TMP:2 m above ground``
message inside it is 0.46-0.59 MB and is fetched alone by byte range.  The
grid is regular 0.25 degree lat-lon, north to south, no bitmap, so the AIFS
lane's proven station index applies (and is proven again on the first
message of every run against eccodes' own nearest search).  Values are
kelvin.

Under the user's ruling (2026-09-22, 「全采全入座，谁新用谁，用最新一轮」):

* plan: every run whose target local days fall in the window at calendar
  leads 0-2, for every fleet station whose day the run can describe (all
  four instants before the day starts, what remains after -- the AIFS lane's
  ``day_instants``);
* arrival: the run's own publication.  Measured on the S3 ``LastModified``
  of ``mem000``'s surface files for the 36 runs 2026-09-14 00Z..09-22 18Z
  (every step of a run lands in the same minute): init + 381..491 minutes,
  35 of 36 within 418, the one outlier 09-20 00Z at 491.  The stamp is the
  measured maximum plus five minutes to capture, init + 496 minutes
  (8h16m) -- conservative for the arm that carries it;
* a step cache keyed (run, step) of float32 kelvin (members x stations,
  zlib), so a daily-high estimator other than the in-day maximum can be
  re-derived without another download.

Transport id on the rows is ``NOAA_GEFS_PDS_S3``: the research copy of the
production DDL admits only the listed transports, the bucket is the same
NOAA open-data programme, and the replay registers the scope contract with
the same id in-process, so the two agree.

Resumable and bounded: cached steps are never fetched again, runs already
written are skipped, a run S3 does not hold is recorded and counted, and
``--max-seconds`` ends the pass between step batches.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT, ROOT / "scripts"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import research_aifs_ens_history as base  # noqa: E402
from bayes_basket_target.contracts import content_sha256  # noqa: E402
from bayes_basket_target.native_archive_lead import capture_lead_bucket  # noqa: E402
from bayes_basket_target.offloaded_paths import assert_present  # noqa: E402
from bayes_basket_target.station_registry import coordinate_registry_manifest  # noqa: E402

RESEARCH_DIR = ROOT / "data" / "aigefs_ens_research"
DB = RESEARCH_DIR / "aigefs_ens_allruns.db"
S3 = "https://noaa-nws-graphcastgfs-pds.s3.amazonaws.com"
SYSTEM_ID = "NOAA_AIGEFS_025"
PROVIDER_ID = "NOAA"
TRANSPORT_PROVIDER_ID = "NOAA_GEFS_PDS_S3"
EXTRACTION_TEMPLATE = "research_noaa_nodd_aigefs_0p25_2t6_{cycle}z_d{lead}_v1"
MEMBER_COUNT = 31
MEMBERS = tuple(f"mem{n:03d}" for n in range(MEMBER_COUNT))
ARRIVAL_AFTER_INIT = timedelta(minutes=496)
CYCLES = (0, 6, 12, 18)
MAX_MESSAGE_BYTES = 1024 * 1024
KELVIN = 273.15
MESSAGE_MB_ESTIMATE = 0.58
THROUGHPUT_MB_S_ESTIMATE = 2.9


def member_identity(member: str) -> tuple[str, str]:
    number = int(member[3:])
    if number == 0:
        return "control", "CONTROL"
    return f"perturbed_{number:02d}", "PERTURBED"


class MissingObject(RuntimeError):
    """The bucket answered 404: the run (or this member/step of it) is not there."""


# ---------------------------------------------------------------------------
# transport
# ---------------------------------------------------------------------------

_CLIENTS = threading.local()


def _client() -> httpx.Client:
    client = getattr(_CLIENTS, "client", None)
    if client is None:
        # The local proxy answers 503 in bursts (AIFS lane, 2026-09-21); the
        # bucket is reachable without it.
        client = httpx.Client(timeout=30.0, follow_redirects=True, trust_env=False)
        _CLIENTS.client = client
    return client


def _get(url: str, *, headers: dict[str, str] | None = None) -> bytes:
    last: Exception | None = None
    for attempt in range(10):
        try:
            response = _client().get(url, headers=headers)
            if response.status_code == 404:
                raise MissingObject(url)
            response.raise_for_status()
            return response.content
        except MissingObject:
            raise
        except (httpx.HTTPError, OSError) as exc:
            last = exc
            # Loud: a batch waits for its slowest message, so one stalled
            # request is the whole pool's stall (segment 3, 2026-09-23: 62
            # messages took 285 s with nothing printed).
            print(f"  retry {attempt + 1} {type(exc).__name__} {url[-60:]}", flush=True)
            time.sleep(min(1.0 * (2 ** attempt), 15.0))
    raise RuntimeError(f"aigefs_fetch_failed:{url}:{type(last).__name__}:{last}")


def object_url(run_init: datetime, member: str, step: int) -> str:
    day = run_init.strftime("%Y%m%d")
    cycle = run_init.strftime("%H")
    return (
        f"{S3}/EAGLE_ensemble/aigefs.{day}/{cycle}/{member}/model/atmos/grib2/"
        f"aigefs.t{cycle}z.sfc.f{step:03d}.grib2"
    )


def locate_2t(run_init: datetime, member: str, step: int) -> tuple[int, int | None]:
    """(start, end) byte range of the ``TMP:2 m above ground`` message.

    ``end`` is None when it is the last message of the file (the range then
    runs to the end of the object).  The index line is checked against the
    run and the member it is supposed to describe.
    """

    raw = _get(object_url(run_init, member, step) + ".idx").decode("utf-8", "replace")
    lines = [line for line in raw.splitlines() if line.strip()]
    wanted_stamp = f"d={run_init.strftime('%Y%m%d%H')}"
    wanted_fcst = "anl" if step == 0 else f"{step} hour fcst"
    wanted_ens = f"ENS=+{int(member[3:])}"
    for i, line in enumerate(lines):
        parts = line.split(":")
        if len(parts) < 7 or parts[3] != "TMP" or parts[4] != "2 m above ground":
            continue
        if parts[2] != wanted_stamp or parts[5] != wanted_fcst or parts[6] != wanted_ens:
            raise RuntimeError(f"aigefs_index_line_unexpected:{member}:{step}:{line}")
        start = int(parts[1])
        end = int(lines[i + 1].split(":")[1]) - 1 if i + 1 < len(lines) else None
        if end is not None and not 16 < end - start + 1 <= MAX_MESSAGE_BYTES:
            raise RuntimeError(f"aigefs_index_range_invalid:{member}:{step}")
        return start, end
    raise RuntimeError(f"aigefs_index_2t_missing:{member}:{step}")


def fetch_message(run_init: datetime, member: str, step: int) -> bytes:
    start, end = locate_2t(run_init, member, step)
    span = f"bytes={start}-{end}" if end is not None else f"bytes={start}-"
    message = _get(object_url(run_init, member, step), headers={"Range": span})
    if (
        not message.startswith(b"GRIB")
        or not message.endswith(b"7777")
        or (end is not None and len(message) != end - start + 1)
        or len(message) > MAX_MESSAGE_BYTES
    ):
        raise RuntimeError(f"aigefs_message_truncated:{member}:{step}:{len(message)}")
    return message


# ---------------------------------------------------------------------------
# plan and database
# ---------------------------------------------------------------------------

def build_plan(start: str, end: str, timezones: dict[str, str]) -> dict[str, dict[tuple[str, str], str]]:
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    fleet = sorted(coordinate_registry_manifest()["station_ids"])
    plan: dict[str, dict[tuple[str, str], str]] = {}
    day = first - timedelta(days=2)
    while day <= last:
        for cycle in CYCLES:
            run = datetime(day.year, day.month, day.day, cycle, tzinfo=timezone.utc)
            arrival = base._stamp(run + ARRIVAL_AFTER_INIT)
            pairs = {}
            for lead in (0, 1, 2):
                target = day + timedelta(days=lead)
                if not first <= target <= last:
                    continue
                for station in fleet:
                    zone = timezones.get(station)
                    if zone and base.day_instants(run_init=run, target=target.isoformat(), zone=zone):
                        pairs[(station, target.isoformat())] = arrival
            if pairs:
                plan[base._stamp(run)] = pairs
        day += timedelta(days=1)
    return plan


def load_plan(start: str, end: str, timezones: dict[str, str], *, cache_dir: Path) -> dict:
    """The plan for a closed window never changes; building it costs ~18 s a
    segment, so it is kept as JSON beside the database."""

    cache = cache_dir / f"plan_{start}_{end}.json"
    if cache.exists():
        raw = json.loads(cache.read_text())
        return {run: {tuple(k.split("|")): v for k, v in pairs.items()} for run, pairs in raw.items()}
    plan = build_plan(start, end, timezones)
    cache_dir.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_suffix(".partial")
    tmp.write_text(json.dumps({run: {"|".join(k): v for k, v in pairs.items()} for run, pairs in plan.items()}))
    tmp.replace(cache)
    return plan


def run_steps(run: str, pairs: dict, timezones: dict[str, str]) -> list[int]:
    run_init = base._utc(run)
    return sorted({
        step for (station, target) in pairs
        for step in (base.day_instants(run_init=run_init, target=target, zone=timezones[station]) or [])
    })


def open_db(path: Path = DB) -> sqlite3.Connection:
    # data/aigefs_ens_research is a symlink to the external volume: a glob or
    # exists() on it unplugged answers silently, so prove it first.
    assert_present(path.parent, what="AIGEFS research directory")
    conn = sqlite3.connect(path, timeout=60)
    # The database lives on the external HDD: WAL keeps a commit sequential,
    # and a large page cache keeps the member table's three random-keyed
    # B-trees from being re-read from the disk on every insert (a 4,000-row
    # commit measured 100-144 s under the defaults, 2026-09-23).
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-262144")
    conn.execute("PRAGMA temp_store=MEMORY")
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='calibration_native_ensemble_historical_member_run'"
    ).fetchone()
    if not exists:
        src = sqlite3.connect(f"file:{base.SSOT}?mode=ro", uri=True, timeout=60)
        try:
            ddl = src.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='calibration_native_ensemble_historical_member_run'"
            ).fetchone()[0]
        finally:
            src.close()
        conn.execute(ddl)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS research_aigefs_member_scope "
            "ON calibration_native_ensemble_historical_member_run"
            "(station_id,target_local_date,ensemble_system_id)"
        )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS research_aigefs_step_values("
        " run_init_utc TEXT, step INTEGER, members_json TEXT, stations_json TEXT,"
        " kelvin_f32_zlib BLOB, message_sha_json TEXT, bytes INTEGER, fetched_at_utc TEXT,"
        " PRIMARY KEY(run_init_utc, step))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS research_aigefs_runs("
        " run_init_utc TEXT PRIMARY KEY, steps INTEGER, messages INTEGER,"
        " bytes INTEGER, seconds REAL, station_days INTEGER, rows INTEGER,"
        " skipped_station_days INTEGER, finished_at_utc TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS research_aigefs_missing_runs("
        " run_init_utc TEXT PRIMARY KEY, reason TEXT, recorded_at_utc TEXT)"
    )
    conn.commit()
    return conn


def cached_steps(conn: sqlite3.Connection, run: str) -> set[int]:
    return {int(r[0]) for r in conn.execute(
        "SELECT step FROM research_aigefs_step_values WHERE run_init_utc=?", (run,)
    )}


def build_rows(
    *,
    run_init: datetime,
    pairs: dict[tuple[str, str], str],
    timezones: dict[str, str],
    stations: list[str],
    samples: dict[tuple[str, int], np.ndarray],
    message_sha: dict[tuple[str, int], str],
) -> tuple[list[tuple], int]:
    """``research_aifs_ens_history.build_rows`` with this system's identity."""

    rows: list[tuple] = []
    skipped = 0
    position = {s: i for i, s in enumerate(stations)}
    cycle = run_init.strftime("%H")
    for (station, target), arrival in sorted(pairs.items()):
        zone = timezones.get(station)
        steps = base.day_instants(run_init=run_init, target=target, zone=zone) if zone else None
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
            member_id, role = member_identity(member)
            identity = {
                "ensemble_system_id": SYSTEM_ID,
                "station_id": station,
                "target_local_date": target,
                "archive_lead_bucket": bucket,
                "run_cycle": f"{cycle}Z",
                "member_id": member_id,
                "raw_tmax_c": tmax_c,
                "extraction_version": extraction,
                "native_run_init_utc": base._stamp(run_init),
                "source_object_sha256": receipt,
            }
            sha = content_sha256(identity)
            rows.append((
                f"aigefs-research-member-{sha[:28]}", SYSTEM_ID, station, target, bucket,
                f"{cycle}Z", member_id, role, tmax_c, "official_daily_tmax_c", extraction,
                PROVIDER_ID, TRANSPORT_PROVIDER_ID, base._stamp(run_init), 1, arrival,
                f"{S3}/EAGLE_ensemble/aigefs.{run_init.strftime('%Y%m%d')}/{cycle}/",
                f"aigefs_field_receipt_set_sha256:{receipt}", receipt, "ACCEPTED",
                "CALIBRATION_TRAIN_ONLY", 0, sha,
            ))
    return rows, skipped


# ---------------------------------------------------------------------------
# capture
# ---------------------------------------------------------------------------

def fetch_step_batch(
    run_init: datetime, steps: list[int], index: base.StationIndex, workers: int, *, prove_first: bool,
) -> tuple[dict[tuple[str, int], np.ndarray], dict[tuple[str, int], str], int]:
    proven = threading.Event()
    if not prove_first:
        proven.set()

    def fetch(key: tuple[str, int]):
        member, step = key
        message = fetch_message(run_init, member, step)
        prove = not proven.is_set()
        values = index.values_at_stations(message, prove=prove)
        if prove:
            proven.set()
        return key, values, hashlib.sha256(message).hexdigest(), len(message)

    ordered = [(member, step) for step in steps for member in MEMBERS]
    out, shas, size = {}, {}, 0
    if prove_first:
        # One message proves the station index against eccodes before the
        # pool fans out on the rest.
        key, values, sha, n = fetch(ordered[0])
        out[key], shas[key], size = values, sha, size + n
        ordered = ordered[1:]
    with ThreadPoolExecutor(workers) as pool:
        for key, values, sha, n in pool.map(fetch, ordered):
            out[key], shas[key], size = values, sha, size + n
    return out, shas, size


def load_samples(conn: sqlite3.Connection, run: str, stations: list[str]):
    samples, message_sha = {}, {}
    for step, members_json, stations_json, blob, sha_json in conn.execute(
        "SELECT step,members_json,stations_json,kelvin_f32_zlib,message_sha_json "
        "FROM research_aigefs_step_values WHERE run_init_utc=?", (run,)
    ):
        members = json.loads(members_json)
        cached_stations = json.loads(stations_json)
        cube = np.frombuffer(zlib.decompress(blob), dtype=np.float32).reshape(
            len(members), len(cached_stations)
        ).astype(np.float64)
        order = [cached_stations.index(s) for s in stations]
        shas = json.loads(sha_json)
        for i, member in enumerate(members):
            samples[(member, int(step))] = cube[i, order]
            message_sha[(member, int(step))] = shas[i]
    return samples, message_sha


def materialise(conn, plan, timezones, stations, deadline) -> int:
    """Member rows for every downloaded run that has none yet.

    Rows are INSERT OR IGNORE with a content hash, so a run materialised
    twice writes nothing new; commits are batched a few runs at a time.
    """

    pending = [
        str(r[0]) for r in conn.execute(
            "SELECT run_init_utc FROM research_aigefs_runs WHERE rows=0 ORDER BY run_init_utc DESC"
        ) if str(r[0]) in plan
    ]
    written = 0
    for i, run in enumerate(pending):
        if time.monotonic() >= deadline:
            break
        run_init = base._utc(run)
        pairs = plan[run]
        t0 = time.monotonic()
        samples, message_sha = load_samples(conn, run, stations)
        rows, skipped = build_rows(
            run_init=run_init, pairs=pairs, timezones=timezones,
            stations=stations, samples=samples, message_sha=message_sha,
        )
        if skipped:
            raise RuntimeError(f"aigefs_materialise_incomplete_cache:{run}:{skipped}")
        with conn:
            conn.executemany(base.INSERT, rows)
            conn.execute(
                "UPDATE research_aigefs_runs SET rows=?, station_days=?, skipped_station_days=0 "
                "WHERE run_init_utc=?", (len(rows), len(pairs) - skipped, run),
            )
        written += 1
        print(f"{run}  rows={len(rows)} station_days={len(pairs)} {time.monotonic() - t0:.1f}s", flush=True)
    left = conn.execute("SELECT COUNT(*) FROM research_aigefs_runs WHERE rows=0").fetchone()[0]
    print(json.dumps({"materialised_runs": written, "runs_without_rows": int(left)}, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start", default="2026-06-17", help="first target local date")
    parser.add_argument("--end", default="2026-09-20", help="last target local date")
    parser.add_argument("--db", type=Path, default=DB)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--max-seconds", type=float, default=540.0)
    parser.add_argument("--steps-per-batch", type=int, default=2,
                        help="steps fetched between deadline checks (31 messages a step)")
    parser.add_argument("--priority-from", default="",
                        help="run init date (YYYY-MM-DD) from which runs are captured first, newest first; "
                             "the older runs follow, also newest first")
    parser.add_argument("--run", action="append", default=[], help="only these run inits")
    parser.add_argument("--stations", nargs="*", default=None,
                        help="restrict the plan to these stations (testing only; rows are then partial)")
    parser.add_argument("--estimate", action="store_true", help="report what is left and exit")
    parser.add_argument("--materialise", action="store_true",
                        help="build the member rows for every downloaded run that has none yet, then exit")
    args = parser.parse_args()

    deadline = time.monotonic() + args.max_seconds
    timezones = base.load_timezones()
    plan = load_plan(args.start, args.end, timezones, cache_dir=args.db.parent)
    if args.stations:
        keep = set(args.stations)
        plan = {run: {k: v for k, v in pairs.items() if k[0] in keep} for run, pairs in plan.items()}
        plan = {run: pairs for run, pairs in plan.items() if pairs}
    conn = open_db(args.db)
    stations = sorted(coordinate_registry_manifest()["station_ids"])
    index = base.StationIndex(stations)
    done = {r[0] for r in conn.execute("SELECT run_init_utc FROM research_aigefs_runs")}
    missing = {r[0] for r in conn.execute("SELECT run_init_utc FROM research_aigefs_missing_runs")}
    wanted = [r for r in sorted(plan, reverse=True) if r not in done and r not in missing]
    if args.priority_from:
        first = [r for r in wanted if r[:10] >= args.priority_from]
        wanted = first + [r for r in wanted if r[:10] < args.priority_from]
    if args.run:
        only = set(args.run)
        wanted = [r for r in wanted if r in only]
    if args.estimate:
        need = cached = 0
        for run in wanted:
            steps = set(run_steps(run, plan[run], timezones))
            have = cached_steps(conn, run)
            need += len(steps - have)
            cached += len(steps & have)
        gb = need * MEMBER_COUNT * MESSAGE_MB_ESTIMATE / 1024
        print(json.dumps({
            "runs": len(plan), "runs_done": len(done), "runs_missing_on_s3": len(missing),
            "runs_left": len(wanted), "steps_to_download": need, "steps_cached": cached,
            "messages_to_download": need * MEMBER_COUNT, "download_gb": round(gb, 1),
            "hours_at_measured_rate": round(gb * 1024 / THROUGHPUT_MB_S_ESTIMATE / 3600, 1),
            "message_mb_estimate": MESSAGE_MB_ESTIMATE, "throughput_mb_s_estimate": THROUGHPUT_MB_S_ESTIMATE,
        }, sort_keys=True))
        return 0
    if args.materialise:
        return materialise(conn, plan, timezones, stations, deadline)
    stopped = False
    for run in wanted:
        if stopped or time.monotonic() >= deadline:
            break
        run_init = base._utc(run)
        pairs = plan[run]
        steps = run_steps(run, pairs, timezones)
        todo = sorted(set(steps) - cached_steps(conn, run))
        started = time.monotonic()
        size = 0
        downloaded = 0
        prove_first = True
        try:
            while todo:
                if time.monotonic() >= deadline:
                    stopped = True
                    break
                batch, todo = todo[:args.steps_per_batch], todo[args.steps_per_batch:]
                t_batch = time.monotonic()
                values, shas, n = fetch_step_batch(run_init, batch, index, args.workers, prove_first=prove_first)
                prove_first = False
                if time.monotonic() - t_batch > 45.0:
                    print(f"  slow batch {run} steps={batch} {time.monotonic() - t_batch:.0f}s", flush=True)
                size += n
                downloaded += len(batch)
                t_write = time.monotonic()
                with conn:
                    for step in batch:
                        cube = np.array([values[(m, step)] for m in MEMBERS], dtype=np.float32)
                        conn.execute(
                            "INSERT OR REPLACE INTO research_aigefs_step_values VALUES(?,?,?,?,?,?,?,?)",
                            (run, step, json.dumps(list(MEMBERS)), json.dumps(stations),
                             zlib.compress(cube.tobytes(), 6),
                             json.dumps([shas[(m, step)] for m in MEMBERS]),
                             int(n / len(batch)),
                             base._stamp(datetime.now(timezone.utc))),
                        )
                if time.monotonic() - t_write > 10.0:
                    print(f"  slow step-cache commit {run} {time.monotonic() - t_write:.0f}s", flush=True)
        except MissingObject as exc:
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO research_aigefs_missing_runs VALUES(?,?,?)",
                    (run, f"404:{str(exc)[-96:]}", base._stamp(datetime.now(timezone.utc))),
                )
            print(f"{run}  missing_on_s3 {str(exc)[-96:]}", flush=True)
            continue
        except RuntimeError as exc:
            # A transport failure past every retry leaves the run unrecorded
            # (its fetched steps are cached); the next segment tries again.
            print(f"{run}  fetch_failed {str(exc)[:160]}", flush=True)
            continue
        if stopped and todo:
            print(f"{run}  paused steps_cached={len(steps) - len(todo)}/{len(steps)}", flush=True)
            break
        seconds = round(time.monotonic() - started, 1)
        # The member rows are derived from the step cache in a separate pass
        # (``--materialise``): inserting 4,000 rows into three random-keyed
        # B-trees on the external HDD cost 100-144 s a run inside the
        # download loop, more than the download itself.
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO research_aigefs_runs VALUES(?,?,?,?,?,?,?,?,?)",
                (run, len(steps), downloaded * MEMBER_COUNT, size, seconds,
                 len(pairs), 0, 0, base._stamp(datetime.now(timezone.utc))),
            )
        mb = size / 1e6
        print(f"{run}  steps={len(steps):2d} downloaded={downloaded:2d} {mb:6.1f}MB {seconds:6.1f}s "
              f"({mb / max(seconds, 0.1):.2f}MB/s) station_days={len(pairs)}", flush=True)
    done = {r[0] for r in conn.execute("SELECT run_init_utc FROM research_aigefs_runs")}
    missing = {r[0] for r in conn.execute("SELECT run_init_utc FROM research_aigefs_missing_runs")}
    left = [r for r in plan if r not in done and r not in missing]
    print(json.dumps({"runs": len(plan), "runs_done": len(done), "runs_missing_on_s3": len(missing),
                      "remaining_runs": len(left)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
