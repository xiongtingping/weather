#!/usr/bin/env python3
"""One-off: back-fill the mode-agreement reading's station-days whose books
left the SSOT for the cold tier (user 2026-10-21 「回填：一次性从外置硬盘按事件日读
盘口，补回众数同桶丢的 9 月上中旬站日，在生产空闲时跑，不长时间占着主库」).

The board's walk (``board_readings.mode_agreement_days``) is run unchanged on
each missing station-day; only its books come from both stores.  Per event
day: wait for a production gap, read that day's books for the missing
stations once from the cold file through its event_date index (no SSOT
connection is open while the slow external disk is read), copy them with the
SSOT's own rows for the same station-days into a TEMP table that shadows the
book table on a fresh read-only connection (TEMP lives in a local temp file:
nothing is written to the SSOT or its WAL), and walk.  Every SSOT read is a
short autocommit SELECT.  At the end the new days join the stored ones, the
board's own summary is rebuilt over them, and the reading is written with the
walk's code key so the hourly job keeps reusing them.

    .venv/bin/python scripts/backfill_mode_agreement_from_cold.py [--dry-run] [--days N]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bayes_basket_target import board_readings as br  # noqa: E402
from bayes_basket_target import label_knowability  # noqa: E402
from scripts.ssot_cold_offload import cold_path, cold_tier_root  # noqa: E402

SSOT = ROOT / "data" / "bayes_basket_target_ssot.db"
BOOKS = "basket_official_ws_snapshot"
WAL_LINE = 256 * 1024 * 1024


def _wait_for_gap(limit_sec: float = 1800.0) -> None:
    """The WAL under the resident's line before a day's SSOT reads.

    Not "no cycle in flight": cycles run back to back, so that gap almost
    never opens.  Every SSOT read here is a short autocommit SELECT, so the
    WAL line is what protects the writer."""

    waited = 0.0
    while waited < limit_sec:
        wal = SSOT.with_name(SSOT.name + "-wal")
        if (wal.stat().st_size if wal.exists() else 0) < WAL_LINE:
            return
        time.sleep(10.0)
        waited += 10.0
    raise SystemExit("the WAL stayed past the line; stopped")


def _columns(conn: sqlite3.Connection, schema: str) -> list[str]:
    return [str(r[1]) for r in conn.execute(f"PRAGMA {schema}.table_info({BOOKS})")]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="walk the first missing day only; write nothing")
    parser.add_argument("--days", type=int, default=0, help="at most this many event days (0: all)")
    args = parser.parse_args()

    stored = br.load_reading("mode_agreement") or {}
    stored_value = stored.get("value") or {}
    have = {(d["station_id"], d["event_date"]) for d in stored_value.get("days") or []}
    main_conn = sqlite3.connect(f"file:{SSOT}?mode=ro", uri=True, timeout=60.0)
    labels = {k: v for k, v in label_knowability.knowable_labels(main_conn).items()
              if str(k[1]) >= br.TRADED_CALIBRATION_SINCE}
    hot_cols = _columns(main_conn, "main")
    main_conn.close()
    cold_file = cold_path(cold_tier_root(), BOOKS)
    cold = sqlite3.connect(f"file:{cold_file}?mode=ro", uri=True, timeout=60.0)
    if _columns(cold, "main") != hot_cols:
        raise SystemExit("cold book table schema differs from the SSOT's")
    cold_first, cold_last = cold.execute(f"SELECT MIN(event_date), MAX(event_date) FROM {BOOKS}").fetchone()
    missing: dict[str, list[str]] = defaultdict(list)
    for station, day in labels:
        if (station, day) not in have and cold_first <= day <= cold_last:
            missing[day].append(station)
    days = sorted(missing)
    if args.days:
        days = days[:args.days]
    if args.dry_run:
        days = days[:1]
    print(f"missing station-days {sum(len(v) for v in missing.values())} over {len(missing)} event days;"
          f" walking {sum(len(missing[d]) for d in days)} on {len(days)}", flush=True)

    found: list[dict] = []
    walk_sha = None
    listed = ",".join(f'"{c}"' for c in hot_cols)
    for day in days:
        stations = sorted(missing[day])
        marks = ",".join("?" for _ in stations)
        started = time.perf_counter()
        rows = cold.execute(
            f"SELECT {listed} FROM {BOOKS} WHERE event_date=? AND station_id IN ({marks})",
            (day, *stations),
        ).fetchall()
        cold_sec = time.perf_counter() - started
        _wait_for_gap()
        conn = sqlite3.connect(f"file:{SSOT}?mode=ro", uri=True, timeout=60.0)
        try:
            conn.execute("PRAGMA temp_store=FILE")
            conn.execute(f"CREATE TEMP TABLE {BOOKS} AS SELECT {listed} FROM main.{BOOKS} WHERE 0")
            conn.execute(f"INSERT INTO temp.{BOOKS} SELECT {listed} FROM main.{BOOKS}"
                         f" WHERE event_date=? AND station_id IN ({marks})", (day, *stations))
            hot_ids = {r[0] for r in conn.execute(f"SELECT snapshot_id FROM temp.{BOOKS}")}
            index = hot_cols.index("snapshot_id")
            conn.executemany(f"INSERT INTO temp.{BOOKS} VALUES ({','.join('?' for _ in hot_cols)})",
                             [r for r in rows if r[index] not in hot_ids])
            conn.execute(f"CREATE INDEX temp.backfill_books_idx ON {BOOKS}(station_id, event_date, state, observed_at)")
            day_labels = {(s, day): labels[(s, day)] for s in stations}
            with mock.patch.object(label_knowability, "knowable_labels", return_value=day_labels), \
                    mock.patch.object(br, "load_reading", return_value=None):
                built = br.mode_agreement_days(conn)
        finally:
            conn.close()
        walk_sha = built.get("walk_sha256")
        got = [d for d in built["days"] if (d["station_id"], d["event_date"]) not in have]
        found.extend(got)
        print(f"  {day}: {len(stations)} missing, cold books {len(rows)} ({cold_sec:.0f}s),"
              f" paired {len(got)} ({time.perf_counter() - started:.0f}s)", flush=True)
    cold.close()
    if args.dry_run:
        print("dry run: nothing written")
        return 0
    if not found:
        print("nothing paired; reading unchanged")
        return 0
    merged = sorted((stored_value.get("days") or []) + found, key=lambda d: (d["event_date"], d["station_id"]))
    counts = dict(stored_value.get("counts") or {})
    counts["backfilled_days"] = len(found)
    counts["paired_days"] = len(merged)
    with mock.patch.object(br, "mode_agreement_days",
                           return_value={"days": merged, "counts": counts, "walk_sha256": walk_sha}):
        value = br.mode_agreement(None)
    if (br.load_reading("mode_agreement") or {}).get("computed_at_utc") != stored.get("computed_at_utc"):
        print("the hourly job rewrote the reading meanwhile; run again")
        return 1
    br.write_reading("mode_agreement", value, code=br._code_sha(*br._READINGS["mode_agreement"][1]), build_sec=0.0)
    print(f"written: {len(merged)} station-days ({len(found)} back-filled)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
