"""Capture ICON-EU day-maxima for the European stations the sweep admitted.

Run-scoped where the other lanes are station-scoped, because the fields
are whole-domain: one download of a run serves every station and both the
event days its hourly window reaches, so the unit of work is the run and
the per-pass cap counts runs, not station-days.  One run is ~45 files of
~1 MB and yields up to fourteen rows.

The archive keeps no history -- each cycle directory holds only the
newest run of that cycle -- so this lane has no backfill: a run is
captured in the ~24 hours it exists or never.  That is the ICON family's
known fragility, and it is why the sweep cadence is hourly against a
six-hourly product: the cost of a wasted pass is a couple of directory
probes, the cost of a missed run is permanent.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import sqlite_busy
from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.dwd_icon_eu_point_archive import (
    CAPTURE_STATIONS, PROVIDER_ID, SYSTEM_ID, TRANSPORT_PROVIDER_ID,
    close_client, fetch_run_day_maxima, run_directory_url,
)
from bayes_basket_target.native_archive_lead import capture_lead_bucket
from bayes_basket_target.pass_deadline import (
    announce_write_blocked,
)

DB = ROOT / "data" / "bayes_basket_target_ssot.db"
EXTRACTION_VERSION = "dwd_icon_eu_surface_nearest_cell_v1"
SETTLEMENT_DEFINITION = "official_daily_tmax_c"
MEMBER_ID = "ICON_EU_CONTROL"
MEMBER_ROLE = "CONTROL"
# The roster is the archive module's, so the board that reports which
# pairs were ruled in reads the same tuple this capture walks.
STATIONS = CAPTURE_STATIONS
RUN_CYCLES = (0, 12)
P1_MAX_RUNS_PER_PASS = 2
# Runs per pass.  Two is every run the archive can currently hold for the
# captured cycles.
MAX_RUNS_PER_PASS = 2
# ...and a wall-clock bound beside it, the one this lane's P1 pass has had
# since 2026-09-09 and its own loop had not.  Two runs is a count, not a
# duration: what a run costs is whatever the DWD mirror takes, and this
# lane's clock read ``timeout_killed`` at 2026-09-10T06:19Z with its last
# success four hours earlier -- thirty minutes of the exclusive SSOT write
# domain spent and nothing written.  A pass by hand the same afternoon took
# 2m53s, so the budget is not the usual case; it is the bound on the bad
# one.  It bounds the whole pass: the P1 pass draws its 240s from it and
# the run loop takes what is left, rather than each starting its own count
# (the NOAA sibling's three counts reached 1,380s by budget alone and were
# killed at 1,800s on 2026-09-15).
BUDGET_SEC_PER_PASS = 900.0
# Seconds of writer occupancy this lane's P1 pass may spend.  The resident
# admits one writer at a time, so a capture that runs to its subprocess
# timeout is a production cycle that cannot start for that long.  The run
# cap counts productive runs only, so an unpublished-but-slow run is
# unbounded work; see the budget note in regional_point_lane.capture_p1.
P1_BUDGET_SEC = 240.0
# DWD uploads a run's t_2m files roughly 2.5-4 hours after init.
PUBLICATION_LAG_HOURS = 4


def _transport_is_writable(conn: sqlite3.Connection) -> tuple[bool, str]:
    """Same pre-flight as the sibling lanes."""

    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        ("calibration_native_ensemble_historical_member_run",),
    ).fetchone()
    if not row or not row[0]:
        return False, "表不存在"
    ddl = re.sub(r"--[^\n]*", "", str(row[0]))
    match = re.search(r"transport_provider_id[^(]*IN\s*\(", ddl, re.S)
    if not match:
        return True, ""
    start = match.end()
    depth = 1
    end = start
    while end < len(ddl) and depth:
        if ddl[end] == "(":
            depth += 1
        elif ddl[end] == ")":
            depth -= 1
        end += 1
    allowed = {
        part.strip().strip("'")
        for part in ddl[start:end - 1].split(",")
        if part.strip()
    }
    if TRANSPORT_PROVIDER_ID in allowed:
        return True, ""
    return False, f"transport {TRANSPORT_PROVIDER_ID} 不在表的白名单里"


def _held_buckets(
    conn: sqlite3.Connection, station: str, event_date: str
) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT archive_lead_bucket"
            " FROM calibration_native_ensemble_historical_member_run"
            " WHERE ensemble_system_id=? AND station_id=?"
            " AND target_local_date=?",
            (SYSTEM_ID, station, event_date),
        )
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    pass_ends = time.monotonic() + BUDGET_SEC_PER_PASS
    conn = sqlite3.connect(str(DB), timeout=120.0)
    # Charter 95 (re-ruled 2026-09-06): the P1 lane -- every ICON-EU cycle.
    # ``prepare`` registers the system and ensures the schema; both write, so
    # a locked file is answered here rather than after a whole-domain
    # download.
    from bayes_basket_target.regional_point_lane import (  # noqa: PLC0415
        capture_p1, prepare,
    )
    try:
        prepare(conn)
    except sqlite_busy.WriteBlocked as exc:
        announce_write_blocked(f"建表/注册 {exc}")
        conn.close()
        return 0
    _receipt = capture_p1(
        conn, product="ICON_EU", now=now, stations=STATIONS,
        max_runs=P1_MAX_RUNS_PER_PASS,
        budget_sec=max(
            0.0, min(P1_BUDGET_SEC, pass_ends - time.monotonic())
        ),
        dry_run=args.dry_run,
    )
    print(
        f"P1 ICON_EU: 计划 {_receipt['runs_planned']} run，"
        f"取 {_receipt['runs_attempted']} run"
        + ("（预算用尽）" if _receipt["budget_exhausted"] else "")
        + f"，写入 {_receipt['written']}，"
        f"已有 {_receipt['existed']}，空 {_receipt['empty']}，失败 {len(_receipt['failed'])}"
    )
    for _line in _receipt["failed"][:4]:
        print(f"  {_line}")
    # A P1 pass that stopped on the write lock has already answered what the
    # loop below would spend a whole-domain download asking: the file is not
    # taking writes.  Report and stop; the DWD archive keeps no history, so
    # what matters is coming back in minutes, not in three hours.
    if _receipt.get("write_blocked"):
        announce_write_blocked(_receipt["write_blocked"])
        conn.close()
        return 0
    conn.execute("PRAGMA busy_timeout=60000")
    writable, reason = _transport_is_writable(conn)
    if not writable and not args.dry_run:
        print(f"  采集已就绪但无法落库：{reason}")
        conn.close()
        return 0

    written = 0
    skipped = 0
    runs_fetched = 0
    failed: list[str] = []
    deadline = pass_ends
    budget_exhausted = False
    write_blocked = ""

    try:
        for hour in RUN_CYCLES:
            # The newest published run of this cycle -- the only one the
            # archive still holds.
            candidate = now.replace(
                hour=hour, minute=0, second=0, microsecond=0
            )
            if candidate > now - timedelta(hours=PUBLICATION_LAG_HOURS):
                candidate -= timedelta(days=1)
            run = candidate
            init = run.isoformat().replace("+00:00", "Z")
            cycle = f"{run:%H}Z"

            # Every (station, event day, bucket) this run can still serve.
            targets = []
            target_meta = {}
            for station, latitude, longitude, zone_name in STATIONS:
                zone = ZoneInfo(zone_name)
                for day_offset in (1, 2):
                    event = (run.astimezone(zone).date()
                             + timedelta(days=day_offset))
                    day_start_local = datetime.combine(
                        event, datetime.min.time(), tzinfo=zone
                    )
                    if run >= day_start_local.astimezone(timezone.utc):
                        continue
                    bucket = capture_lead_bucket(
                        event_date=event.isoformat(),
                        settlement_timezone=zone_name,
                        run_init=run,
                    )
                    if bucket not in ("h12_24", "h24_48"):
                        continue
                    if bucket in _held_buckets(
                        conn, station, event.isoformat()
                    ):
                        skipped += 1
                        continue
                    offset_hours = int(
                        day_start_local.utcoffset().total_seconds() // 3600
                    )
                    targets.append({
                        "station_id": station,
                        "latitude": latitude,
                        "longitude": longitude,
                        "event_date": event,
                        "utc_offset_hours": offset_hours,
                    })
                    target_meta[(station, event.isoformat())] = bucket
            if not targets:
                continue
            if runs_fetched >= MAX_RUNS_PER_PASS:
                continue
            # Before the fetch, never during: a run that has started
            # finishes, so the budget cannot leave one half written.
            if time.monotonic() >= deadline:
                budget_exhausted = True
                continue
            runs_fetched += 1
            try:
                results = fetch_run_day_maxima(
                    run_init=run, targets=targets
                )
            except Exception as exc:  # noqa: BLE001
                failed.append(
                    f"{cycle} {type(exc).__name__}: {str(exc)[:80]}"
                )
                continue
            for key, captured in results.items():
                station, event_iso = key
                bucket = target_meta[key]
                if not captured["complete"]:
                    failed.append(
                        f"{station} {event_iso} {cycle} 时次不全"
                        f" {len(captured['hours_covered'])}"
                        f"/{len(captured['hours_wanted'])}"
                    )
                    continue
                identity = {
                    "ensemble_system_id": SYSTEM_ID,
                    "station_id": station,
                    "target_local_date": event_iso,
                    "run_cycle": cycle,
                    "native_run_init_utc": init,
                    "member_id": MEMBER_ID,
                    "raw_tmax_c": round(captured["forecast_tmax_c"], 6),
                    "extraction_version": EXTRACTION_VERSION,
                }
                digest = content_sha256(identity)
                print(
                    f"  {station} {event_iso} {cycle} {bucket}"
                    f" {captured['forecast_tmax_c']:.2f}°C"
                    + ("  [dry-run]" if args.dry_run else "")
                )
                if args.dry_run:
                    continue
                # Committed row by row, and a contended file stops the
                # pass rather than ending it: see ``sqlite_busy``.
                sqlite_busy.write_row(
                    conn,
                    "INSERT INTO"
                    " calibration_native_ensemble_historical_member_run("
                    " historical_member_run_id, ensemble_system_id,"
                    " station_id, target_local_date,"
                    " archive_lead_bucket, run_cycle, member_id,"
                    " member_role, raw_tmax_c, settlement_definition,"
                    " extraction_version, provider_id,"
                    " transport_provider_id, native_run_init_utc,"
                    " offline_oos_eligible, archive_query_at_utc,"
                    " source_url, raw_object_ref,"
                    " source_object_sha256, qc_status,"
                    " authority_scope, holdout_eligible,"
                    " content_sha256)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
                    "?,?,?,?)",
                    (
                        f"iconeu-{digest[:32]}", SYSTEM_ID, station,
                        event_iso, bucket, cycle,
                        MEMBER_ID, MEMBER_ROLE,
                        float(captured["forecast_tmax_c"]),
                        SETTLEMENT_DEFINITION, EXTRACTION_VERSION,
                        PROVIDER_ID, TRANSPORT_PROVIDER_ID, init,
                        1,
                        now.isoformat().replace("+00:00", "Z"),
                        run_directory_url(run),
                        captured["grid_geometry"], digest,
                        "ACCEPTED", "CALIBRATION_TRAIN_ONLY", 0,
                        digest,
                    ),
                )
                written += 1
    except sqlite_busy.WriteBlocked as exc:
        # Every row before this one is committed; this run is not.  Fetching
        # on would cost another whole-domain download and end at the same
        # INSERT, so the pass stops and asks to come back soon.
        write_blocked = str(exc)
    finally:
        close_client()
        conn.close()

    print(
        f"\n写入 {written}，已有跳过 {skipped}，"
        f"本轮取 run {runs_fetched}/{MAX_RUNS_PER_PASS}，"
        f"失败/不全 {len(failed)}"
    )
    if budget_exhausted:
        print(
            f"  本轮用尽 {BUDGET_SEC_PER_PASS:.0f} 秒预算，"
            "剩余的下一轮继续"
        )
    for line in failed[:10]:
        print(f"  {line}")
    if write_blocked:
        announce_write_blocked(write_blocked)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
