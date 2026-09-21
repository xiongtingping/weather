#!/usr/bin/env python3
"""Research only: AIFS ENS under the user's ruling -- every run, seated, newest used.

User ruling 2026-09-22: 「全采全入座，谁新用谁，用最新一轮」 -- capture every
run, seat every system, whichever is newest is used, use the latest round.
For AIFS ENS that means all four runs a day (00/06/12/18Z), each available
the moment it is published, not only the runs PF50 happened to serve and not
at PF50's arrival time.  ``research_aifs_ens_history.py`` built the paired,
conservative version; this builds the ruling's version into its own database
(``data/aifs_ens_research/aifs_ens_allruns.db``), in the same member-row shape
production's replay reads:

* plan: every run whose target local days fall in the window at calendar
  leads 0-2, for every fleet station whose day the run can describe (all four
  instants before the day starts, what remains after);
* arrival: the run's own publication, init + 6h15m -- every step of all four
  cycles was on the official bucket by init + 6h10m on 2026-09-20 (00Z
  05:59-06:09, 06Z 11:55-12:04, 12Z 17:58-18:07, 18Z 23:51-00:00), plus five
  minutes to capture;
* a step cache keyed by (run, step): the paired backfill's stored instants
  are imported once, and only the steps no run has fetched yet are
  downloaded.

Station values, the index proof against eccodes, the daily-high convention
(largest in-day instant) and the extraction versions are the paired
backfill's own, imported rather than restated.
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

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT, ROOT / "scripts"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import research_aifs_ens_history as base  # noqa: E402
from bayes_basket_target.station_registry import coordinate_registry_manifest  # noqa: E402

DB = ROOT / "data" / "aifs_ens_research" / "aifs_ens_allruns.db"
PAIRED_DB = base.DEFAULT_DB
ARRIVAL_AFTER_INIT = timedelta(hours=6, minutes=15)
CYCLES = (0, 6, 12, 18)


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


def open_db() -> sqlite3.Connection:
    conn = base.open_research_db(DB)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS research_aifs_step_values("
        " run_init_utc TEXT, step INTEGER, members_json TEXT, stations_json TEXT,"
        " kelvin_f32_zlib BLOB, message_sha_json TEXT, source TEXT,"
        " PRIMARY KEY(run_init_utc, step))"
    )
    conn.commit()
    return conn


def import_paired_samples(conn: sqlite3.Connection) -> int:
    """Seed the step cache from the paired backfill's stored instants."""

    if not PAIRED_DB.exists():
        return 0
    src = sqlite3.connect(f"file:{PAIRED_DB}?mode=ro", uri=True, timeout=120)
    imported = 0
    try:
        for run, steps_json, members_json, stations_json, blob in src.execute(
            "SELECT run_init_utc,steps_json,members_json,stations_json,kelvin_f32_zlib "
            "FROM research_aifs_run_samples"
        ):
            steps = json.loads(steps_json)
            members = json.loads(members_json)
            stations = json.loads(stations_json)
            cube = np.frombuffer(zlib.decompress(blob), dtype=np.float32).reshape(
                len(steps), len(members), len(stations)
            )
            for i, step in enumerate(steps):
                values = np.ascontiguousarray(cube[i])
                digest = hashlib.sha256(values.tobytes()).hexdigest()
                cur = conn.execute(
                    "INSERT OR IGNORE INTO research_aifs_step_values VALUES(?,?,?,?,?,?,?)",
                    (run, int(step), members_json, stations_json,
                     zlib.compress(values.tobytes(), 6),
                     json.dumps([f"paired_values:{digest}"] * len(members)), "paired_backfill"),
                )
                imported += cur.rowcount
        conn.commit()
    finally:
        src.close()
    return imported


def cached_steps(conn: sqlite3.Connection, run: str) -> set[int]:
    return {int(r[0]) for r in conn.execute(
        "SELECT step FROM research_aifs_step_values WHERE run_init_utc=?", (run,)
    )}


def fetch_steps(run_init: datetime, steps: list[int], index: base.StationIndex, workers: int):
    catalog: dict[tuple[str, int], dict] = {}
    with ThreadPoolExecutor(workers) as pool:
        for step, found in zip(steps, pool.map(lambda s: base.step_messages(run_init, s), steps)):
            for member, row in found.items():
                catalog[(member, step)] = row
    proven = threading.Event()

    def fetch(key):
        row = catalog[key]
        url = base._prefix(run_init, key[1], row["_kind"]) + ".grib2"
        start = int(row["_offset"])
        message = base._get(url, headers={"Range": f"bytes={start}-{start + int(row['_length']) - 1}"})
        if len(message) != int(row["_length"]) or not message.startswith(b"GRIB"):
            raise RuntimeError(f"aifs_message_truncated:{key}")
        prove = not proven.is_set()
        values = index.values_at_stations(message, prove=prove)
        if prove:
            proven.set()
        return key, values, hashlib.sha256(message).hexdigest(), len(message)

    ordered = sorted(catalog)
    out, shas, size = {}, {}, 0
    key, values, sha, n = fetch(ordered[0])
    out[key], shas[key], size = values, sha, size + n
    with ThreadPoolExecutor(workers) as pool:
        for key, values, sha, n in pool.map(fetch, ordered[1:]):
            out[key], shas[key], size = values, sha, size + n
    return out, shas, size


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2026-07-01")
    parser.add_argument("--end", default="2026-09-20")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--max-seconds", type=float, default=480.0)
    parser.add_argument("--estimate", action="store_true", help="report what is left and exit")
    args = parser.parse_args()

    deadline = time.monotonic() + args.max_seconds
    timezones = base.load_timezones()
    plan = build_plan(args.start, args.end, timezones)
    conn = open_db()
    if not conn.execute("SELECT COUNT(*) FROM research_aifs_step_values").fetchone()[0]:
        print(json.dumps({"imported_paired_steps": import_paired_samples(conn)}), flush=True)
    stations = sorted(coordinate_registry_manifest()["station_ids"])
    index = base.StationIndex(stations)
    done = {r[0] for r in conn.execute("SELECT run_init_utc FROM research_aifs_runs")}
    wanted = [r for r in sorted(plan, reverse=True) if r not in done]
    if args.estimate:
        need = cached = 0
        for run in wanted:
            steps = {s for (st, d) in plan[run] for s in (base.day_instants(
                run_init=base._utc(run), target=d, zone=timezones[st]) or [])}
            have = cached_steps(conn, run)
            need += len(steps - have)
            cached += len(steps & have)
        gb = need * len(base.MEMBERS) * 0.63 / 1024
        print(json.dumps({"runs": len(plan), "runs_left": len(wanted), "steps_to_download": need,
                          "steps_cached": cached, "download_gb": round(gb, 1),
                          "hours_at_3mb_s": round(gb * 1024 / 3.0 / 3600, 1)}))
        return 0
    for run in wanted:
        if time.monotonic() >= deadline:
            break
        run_init = base._utc(run)
        pairs = plan[run]
        steps = sorted({s for (st, d) in pairs for s in (base.day_instants(
            run_init=run_init, target=d, zone=timezones[st]) or [])})
        missing = sorted(set(steps) - cached_steps(conn, run))
        started = time.monotonic()
        size = 0
        if missing:
            values, shas, size = fetch_steps(run_init, missing, index, args.workers)
            with conn:
                for step in missing:
                    cube = np.array([values[(m, step)] for m in base.MEMBERS], dtype=np.float32)
                    conn.execute(
                        "INSERT OR REPLACE INTO research_aifs_step_values VALUES(?,?,?,?,?,?,?)",
                        (run, step, json.dumps(list(base.MEMBERS)), json.dumps(stations),
                         zlib.compress(cube.tobytes(), 6),
                         json.dumps([shas[(m, step)] for m in base.MEMBERS]), "gcs"),
                    )
        samples, message_sha = {}, {}
        for step, members_json, stations_json, blob, sha_json in conn.execute(
            "SELECT step,members_json,stations_json,kelvin_f32_zlib,message_sha_json "
            "FROM research_aifs_step_values WHERE run_init_utc=?", (run,)
        ):
            members = json.loads(members_json)
            cached_stations = json.loads(stations_json)
            cube = np.frombuffer(zlib.decompress(blob), dtype=np.float32).reshape(
                len(members), len(cached_stations)
            ).astype(np.float64)
            order = [cached_stations.index(s) for s in stations]
            for i, member in enumerate(members):
                samples[(member, int(step))] = cube[i, order]
                message_sha[(member, int(step))] = json.loads(sha_json)[i]
        rows, skipped = base.build_rows(
            run_init=run_init, pairs=pairs, timezones=timezones,
            stations=stations, samples=samples, message_sha=message_sha,
        )
        seconds = round(time.monotonic() - started, 1)
        with conn:
            conn.executemany(base.INSERT, rows)
            conn.execute(
                "INSERT OR REPLACE INTO research_aifs_runs VALUES(?,?,?,?,?,?,?,?,?)",
                (run, len(steps), len(missing) * len(base.MEMBERS), size, seconds,
                 len(pairs) - skipped, len(rows), skipped,
                 base._stamp(datetime.now(timezone.utc))),
            )
        mb = size / 1e6
        print(f"{run}  steps={len(steps):2d} downloaded={len(missing):2d} {mb:6.1f}MB {seconds:6.1f}s "
              f"station_days={len(pairs) - skipped} skipped={skipped}", flush=True)
    left = len([r for r in plan if r not in {x[0] for x in conn.execute('SELECT run_init_utc FROM research_aifs_runs')}])
    print(json.dumps({"runs": len(plan), "remaining_runs": left}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
