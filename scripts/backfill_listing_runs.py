#!/usr/bin/env python3
"""Backfill only the listing-time run of each event day (charter 267 补记三).

The listing-time forecast F reads, for every past day, the run knowable at
that day's listing instant (D-2 05:00Z) -- the 00Z run two days before -- and
only for that event day.  ``backfill_regional_point_runs.py`` takes every run
in reach for every day in a batch (all steps 0-96 of each run); for a
history this script fetches, per event day, just that run and just that
day's local steps across the roster: ~27 hourly files instead of 97 for
GFS_HOURLY, ~27 of UKMO's 68.  Rows are ordinary lane rows (``write_run``
with ``backfill=True``: knowable at run init + the product's publication
lag), so the lane and F read them as they read any other.

Run UKMO with the project venv's python (h5py).
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DB = ROOT / "data" / "bayes_basket_target_ssot.db"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--products", required=True)
    parser.add_argument("--from-date", required=True, help="newest event date (YYYY-MM-DD)")
    parser.add_argument("--to-date", required=True, help="oldest event date (YYYY-MM-DD)")
    parser.add_argument("--lead-days", type=int, default=2)
    parser.add_argument("--cycle", type=int, default=0)
    parser.add_argument("--max-seconds", type=float, default=0.0)
    args = parser.parse_args()

    from bayes_basket_target import native_deterministic_point_archive as native  # noqa: PLC0415
    from bayes_basket_target.deterministic_forecast_family import product_roster  # noqa: PLC0415
    from bayes_basket_target.regional_point_lane import (  # noqa: PLC0415
        NATIVE_DETERMINISTIC_PRODUCTS, SYSTEM_IDS, utc_offset_hours, write_run,
    )

    products = [p.strip() for p in args.products.split(",") if p.strip()]
    for product in products:
        if product not in NATIVE_DETERMINISTIC_PRODUCTS:
            raise SystemExit(f"{product}: not a native deterministic product")
    conn = sqlite3.connect(str(DB), timeout=120.0)
    conn.execute("PRAGMA busy_timeout=60000")
    started = time.monotonic()
    newest, oldest = date.fromisoformat(args.from_date), date.fromisoformat(args.to_date)
    try:
        day = newest
        while day >= oldest:
            for product in products:
                if args.max_seconds and time.monotonic() - started > args.max_seconds:
                    print("时间预算用完，停在", day, product, flush=True)
                    return 0
                run_init = datetime.combine(day - timedelta(days=args.lead_days), datetime.min.time(),
                                            tzinfo=timezone.utc) + timedelta(hours=args.cycle)
                stamp = run_init.strftime("%Y-%m-%dT%H:%M:%SZ")
                system = SYSTEM_IDS[product]
                # One read for the day (the native products carry no derived
                # or retired extraction versions that ``stored_run_inits`` skips).
                held = {str(row[0]).upper() for row in conn.execute(
                    "SELECT station_id FROM calibration_regional_point_run"
                    " WHERE ensemble_system_id=? AND target_local_date=? AND run_init_utc=?",
                    (system, day.isoformat(), stamp),
                )}
                targets = [
                    {"station_id": st, "event_date": day, "utc_offset_hours": utc_offset_hours(day, tz),
                     "latitude": lat, "longitude": lon, "settlement_timezone": tz}
                    for st, lat, lon, tz in product_roster(conn, product)
                    if st not in held
                ]
                if not targets:
                    print(f"{day} {product}: 已有", flush=True)
                    continue
                t0 = time.monotonic()
                try:
                    results = native.fetch_run_for_targets(product=product, run_init=run_init, targets=targets)
                except Exception as exc:  # noqa: BLE001 -- one day's failure is reported, the rest go on
                    print(f"{day} {product}: 失败 {type(exc).__name__}:{str(exc)[:120]}", flush=True)
                    continue
                from bayes_basket_target.sqlite_busy import WriteBlocked  # noqa: PLC0415

                for attempt in range(10):
                    written = 0
                    try:
                        for target in targets:
                            got = results.get((target["station_id"], day.isoformat()))
                            if not got or not got["hourly_c"]:
                                continue
                            written += bool(write_run(
                                conn, product=product, station_id=target["station_id"], event_date=day,
                                settlement_timezone=target["settlement_timezone"], run_init=run_init,
                                hourly_c=got["hourly_c"], hours_wanted=int(got["hours_wanted"]),
                                archive_query_at=datetime.now(timezone.utc), source_url=str(got["source_url"]),
                                backfill=True,
                            ))
                        conn.commit()
                        break
                    except (WriteBlocked, sqlite3.OperationalError) as exc:
                        # The production writer holds the lock: undo this day's
                        # rows and write them again shortly (already fetched).
                        conn.rollback()
                        print(f"   {day} {product}: 写锁忙（{str(exc)[:60]}），30 秒后重写", flush=True)
                        time.sleep(30)
                print(f"{day} {product} {run_init:%m-%dT%HZ}: 写入 {written}/{len(targets)}  {time.monotonic() - t0:.0f}s",
                      flush=True)
            day -= timedelta(days=1)
    finally:
        native.close_client()
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
