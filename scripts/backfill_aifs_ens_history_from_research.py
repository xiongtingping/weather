#!/usr/bin/env python3
"""Backfill AIFS ENS member rows into the SSOT from the research capture.

The research backfill (``research_aifs_ens_allruns.py``) already read every
AIFS ENS run from 2026-06-17 through 2026-09-20 off the official GCS replica
and stored each member's in-day maximum for every fleet station-day, in the
member-row shape the replay reads.  Fetching the same objects again through
``ecmwf_aifs_ens_archive`` would cost ~70 GB for values that agree with these
to 1.5e-5 C (measured 2026-09-23, four stations, one run: the float32 the
research cache stored).  So the history the rolling training window needs is
written from that capture instead, under the production identity:

* the production extraction version (the research one with its
  ``research_`` prefix dropped -- ``production_rows`` refuses anything else);
* ``archive_query_at_utc`` = when the research run finished, i.e. when the
  values were actually in hand, never the run's publication time;
* ``raw_object_ref`` names the research receipt set, so a row says where it
  came from and differs from a live capture only there.

Dry run by default: counts what would be written.  ``--apply`` writes in
short transactions of ``--chunk-station-days`` station-days, taking the lock
the way the calibration sweep does (``_begin_immediate``) and pausing after
each commit so the resident's writers are not starved.  A
station-day already present must hold the same members (REPLAY); anything
else is a frozen-fact conflict and stops the pass.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import ecmwf_aifs_ens_archive as aifs_module  # noqa: E402
from bayes_basket_target import noaa_aigefs_archive as aigefs_module  # noqa: E402
from bayes_basket_target.band_reliability_store import _begin_immediate  # noqa: E402
from bayes_basket_target.native_archive_lead import capture_lead_bucket  # noqa: E402
from bayes_basket_target.offloaded_paths import assert_present  # noqa: E402

SSOT = ROOT / "data" / "bayes_basket_target_ssot.db"
RESEARCH_PREFIX = "research_"
# Charter 212: the same lane fills NOAA AIGEFS from its research capture
# (``research_aigefs_ens_allruns.py``); ``--system`` picks the collector,
# the research database and its run table.  ``aifs`` stays the default.
SYSTEMS = {
    "aifs": (aifs_module, ROOT / "data" / "aifs_ens_research" / "aifs_ens_allruns.db", "research_aifs_runs"),
    "aigefs": (aigefs_module, ROOT / "data" / "aigefs_ens_research" / "aigefs_ens_allruns.db", "research_aigefs_runs"),
}
aifs = aifs_module  # rebound to the chosen collector in main()
RUNS_TABLE = "research_aifs_runs"
# The rolling training window is 60 days (native_ensemble_oos
# ROLLING_TRAIN_WINDOW_DAYS); a D+2 run is issued two days before its day.
DEFAULT_WINDOW_DAYS = 62


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def load_window(
    research: sqlite3.Connection, first: str, last: str
) -> dict[str, dict[tuple[str, str], list[tuple]]]:
    """Every research member row of the window, grouped by run and station-day.

    One sequential pass, deliberately ``NOT INDEXED``: the research database
    lives on the external USB disk, where the per-run lookup the index
    offered (every row is AIFS, so it was an index walk with a table seek per
    row) took about four minutes a run -- 2 runs in 9 minutes, measured.
    """

    runs: dict[str, dict[tuple[str, str], list[tuple]]] = defaultdict(lambda: defaultdict(list))
    for row in research.execute(
        "SELECT native_run_init_utc,station_id,target_local_date,archive_lead_bucket,"
        "run_cycle,member_id,member_role,raw_tmax_c,extraction_version,source_url,"
        "source_object_sha256,ensemble_system_id "
        "FROM calibration_native_ensemble_historical_member_run NOT INDEXED "
        "WHERE native_run_init_utc>=? AND native_run_init_utc<=?",
        (first, last),
    ):
        if row[11] != aifs.SYSTEM_ID:
            raise SystemExit(f"unexpected_system:{row[11]}")
        runs[str(row[0])][(str(row[1]), str(row[2]))].append(row[3:11])
    return runs


def production_rows(
    *, run: str, finished_at: str, station: str, target: str, rows: list[tuple],
    zones: dict[str, str],
) -> list[dict]:
    run_init = _parse(run)
    if len(rows) != aifs.EXPECTED_MEMBER_COUNT or len({r[2] for r in rows}) != len(rows):
        raise SystemExit(f"members_incomplete:{run}:{station}:{target}:{len(rows)}")
    buckets = {r[0] for r in rows}
    cycles = {r[1] for r in rows}
    versions = {r[5] for r in rows}
    urls = {r[6] for r in rows}
    receipts = {r[7] for r in rows}
    lead = (date.fromisoformat(target) - run_init.date()).days
    version = aifs.extraction_version_for_lead(lead, run_init.hour)
    bucket = capture_lead_bucket(
        event_date=target, settlement_timezone=zones[station], run_init=run_init,
    )
    if (
        buckets != {bucket}
        or cycles != {f"{run_init.hour:02d}Z"}
        or versions != {RESEARCH_PREFIX + version}
        or len(urls) != 1
        or len(receipts) != 1
        or aifs.day_instants(
            event_date=target, settlement_timezone=zones[station], run_init_utc=run_init,
        ) is None
    ):
        raise SystemExit(f"research_row_identity_mismatch:{run}:{station}:{target}")
    receipt = next(iter(receipts))
    return aifs.member_run_rows(
        station=station,
        event_date=target,
        lead_bucket=bucket,
        run_cycle=f"{run_init.hour:02d}Z",
        run_init_utc=aifs._stamp(run_init),
        extraction_version=version,
        members=[
            {"member_id": r[2], "member_role": r[3], "raw_tmax_c": float(r[4])}
            for r in sorted(rows, key=lambda r: r[2])
        ],
        archive_query_at_utc=finished_at,
        source_url=next(iter(urls)),
        raw_object_ref="aifs_ens_research_receipt_set_sha256:" + receipt,
        source_object_sha256=receipt,
    )


def _hold_wal_under(writer: sqlite3.Connection, ssot: Path, limit_mib: float) -> bool:
    """Checkpoint (TRUNCATE) until the WAL is back under ``limit_mib``.

    2026-09-24: eleven runs (~50 MB of rows) grew the WAL to 709 MiB -- each
    insert touches pages across every index of the member table and the WAL
    logs whole pages -- past the resident's 512 MiB bound, and its writer
    failed closed on the reopen.  Readers pinning old snapshots keep a
    TRUNCATE busy, so it is retried for up to two minutes; False stops the
    pass rather than write on past the bound.
    """

    wal = ssot.with_name(ssot.name + "-wal")
    if not wal.is_file() or wal.stat().st_size <= limit_mib * 1024 * 1024:
        return True
    deadline = time.monotonic() + 120.0
    while time.monotonic() < deadline:
        busy = writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
        if not busy and wal.stat().st_size <= limit_mib * 1024 * 1024:
            return True
        time.sleep(3.0)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--system", choices=sorted(SYSTEMS), default="aifs")
    parser.add_argument("--research-db", type=Path, default=None,
                        help="default: the chosen system's research database")
    parser.add_argument("--ssot", type=Path, default=SSOT)
    parser.add_argument("--first-run", default="",
                        help=f"earliest run init (default: today - {DEFAULT_WINDOW_DAYS} days)")
    parser.add_argument("--last-run", default="9999-12-31")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--max-seconds", type=float, default=480.0)
    parser.add_argument("--pause-seconds", type=float, default=1.0,
                        help="pause after each chunk's commit")
    parser.add_argument("--chunk-station-days", type=int, default=15)
    parser.add_argument("--max-wal-mib", type=float, default=300.0,
                        help="after a commit that leaves the WAL past this, "
                             "TRUNCATE-checkpoint before writing more")
    args = parser.parse_args()
    global aifs, RUNS_TABLE
    aifs, default_db, RUNS_TABLE = SYSTEMS[args.system]
    if args.research_db is None:
        args.research_db = default_db

    first = args.first_run or (
        datetime.now(timezone.utc).date() - timedelta(days=DEFAULT_WINDOW_DAYS)
    ).isoformat()
    # Charter 237: days past the member table's hot window live on the cold
    # tier; writing one back would leave it in both places.
    from scripts.ssot_cold_offload import OffloadRefused, assert_within_hot_window  # noqa: PLC0415

    try:
        assert_within_hot_window("calibration_native_ensemble_historical_member_run", first)
    except OffloadRefused as exc:
        raise SystemExit(str(exc)) from exc
    research_path = assert_present(args.research_db, what=f"{args.system.upper()} research capture")
    research = sqlite3.connect(f"file:{research_path}?mode=ro", uri=True, timeout=60)
    reader = sqlite3.connect(f"file:{args.ssot}?mode=ro", uri=True, timeout=60)
    zones = dict(reader.execute(
        "SELECT station_id,settlement_timezone FROM calibration_station_market_timezone_binding"
    ))
    registered = {r[0] for r in reader.execute("SELECT station_id FROM calibration_station_registry")}
    runs = [
        (str(run), str(finished))
        for run, finished in research.execute(
            f"SELECT run_init_utc,finished_at_utc FROM {RUNS_TABLE} "
            "WHERE run_init_utc>=? AND run_init_utc<=? ORDER BY run_init_utc",
            (first, args.last_run),
        )
    ]
    writer = None
    if args.apply:
        writer = sqlite3.connect(str(args.ssot), timeout=60.0, isolation_level=None)
        writer.execute("PRAGMA busy_timeout=60000")
        writer.execute("PRAGMA foreign_keys=ON")
    started = time.monotonic()
    window = load_window(research, first, args.last_run)
    print(json.dumps({"loaded_runs": len(window), "load_seconds": round(time.monotonic() - started, 1)}), flush=True)
    totals = defaultdict(int)
    for run, finished in runs:
        if time.monotonic() - started > args.max_seconds:
            break
        grouped = window.get(run, {})
        batches = []
        for (station, target), rows in sorted(grouped.items()):
            if station not in registered or station not in zones:
                totals["station_days_unregistered"] += 1
                continue
            batches.append(production_rows(
                run=run, finished_at=finished, station=station, target=target,
                rows=rows, zones=zones,
            ))
        if writer is None:
            totals["runs"] += 1
            totals["station_days"] += len(batches)
            totals["rows"] += sum(len(b) for b in batches)
            continue
        # Small transactions: on the live SSOT a whole run (~135 station-days)
        # held the write lock 3.6-6 s against a 0.5 s pause (2026-09-23), about
        # nine tenths of the time.  A chunk holds it about half a second and
        # the pause after it leaves the resident's writers most of the clock.
        states = defaultdict(int)
        held = 0.0
        attempts_max = 0
        for start in range(0, len(batches), args.chunk_station_days):
            attempts = _begin_immediate(writer)
            t0 = time.monotonic()
            try:
                aifs.ensure_system(writer)
                for batch in batches[start:start + args.chunk_station_days]:
                    states[aifs.write_member_runs(writer, batch)] += 1
                writer.execute("COMMIT")
            except Exception:
                writer.execute("ROLLBACK")
                raise
            held = max(held, time.monotonic() - t0)
            attempts_max = max(attempts_max, attempts)
            if not _hold_wal_under(writer, args.ssot, args.max_wal_mib):
                print(json.dumps({"stopped": "wal_checkpoint_busy", "run": run}), flush=True)
                return 3
            time.sleep(args.pause_seconds)
        totals["runs"] += 1
        for state, count in states.items():
            totals[f"station_days_{state.lower()}"] += count
        print(json.dumps({
            "run": run, "station_days": len(batches), "states": dict(states),
            "lock_attempts_max": attempts_max, "lock_held_seconds_max": round(held, 2),
        }), flush=True)
    remaining = len(runs) - totals["runs"]
    print(json.dumps({
        "mode": "apply" if args.apply else "dry_run", "window": [first, args.last_run],
        "runs_in_window": len(runs), "remaining_runs": remaining, **totals,
        "estimated_ssot_gib": round(totals["rows"] * 1167 / 2**30, 2) if not args.apply else None,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
