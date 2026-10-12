#!/usr/bin/env python3
"""Backfill the regional lane with runs the archives still hold.

Charter 201: also the deterministic-forecast family's members (AIFS Single
from ECMWF open data; CMA by named run through the relay, stamped knowable
at its measured holding lag), for the P0 feature's history -- and at a pair
P1 reads, only runs no P1 prefix could have read (``plan_p1(backfill=True)``).

Charter 95 (re-ruled 2026-09-06).  The forward lanes only ever kept the
00Z/12Z runs of the day before; the P1 question needs the runs issued
during the local day.  HRRR and NBM keep every run on the public bucket
for years, MSM keeps sixty days on the RISH mirror; ICON-EU keeps nothing,
so it accumulates forward only.  Bounded by days and cycles so a pass is a
few hours of ranged reads, not a crawl.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from bayes_basket_target.regional_point_lane import (  # noqa: E402
    RUN_CYCLE_HOURS, capture_p1, ensure_schema,
)

DB = ROOT / "data" / "bayes_basket_target_ssot.db"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--products", default="HRRR,NBM,MSM")
    parser.add_argument("--days", type=int, default=14, help="local days back from today, inclusive")
    parser.add_argument(
        "--cycles", default="",
        help="comma-separated UTC cycle hours to restrict to (default: the product's full cadence)",
    )
    parser.add_argument("--max-runs-per-day", type=int, default=6)
    parser.add_argument(
        "--stations", default="",
        help="comma-separated station ids to restrict to (default: the product's roster)",
    )
    parser.add_argument("--batch-days", type=int, default=1,
                        help="plan this many consecutive days in one pass, so a run that reaches several is fetched once")
    parser.add_argument("--from-day", type=int, default=0,
                        help="stop this many local days back from today (inclusive)")
    parser.add_argument("--max-seconds", type=float, default=0.0,
                        help="stop starting new product-days after this many seconds (0 = no bound)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--respect-production-network", action="store_true",
        help="before each product-day batch, wait while the supply watch reads production's egress red/amber",
    )
    args = parser.parse_args()
    products = [p.strip().upper() for p in args.products.split(",") if p.strip()]
    cycles = {int(c) for c in args.cycles.split(",") if c.strip()} if args.cycles else None
    from bayes_basket_target import regional_point_lane as lane  # noqa: PLC0415
    if cycles:
        # Narrow the lane's cadence for this pass only.
        for product in products:
            lane.RUN_CYCLE_HOURS[product] = tuple(h for h in RUN_CYCLE_HOURS[product] if h in cycles)
    now = datetime.now(timezone.utc)
    conn = sqlite3.connect(str(DB), timeout=120.0)
    conn.execute("PRAGMA busy_timeout=60000")
    ensure_schema(conn)
    # Charter 201: every family member's roster from one place; a station
    # filter narrows it (e.g. the stations whose P0 feature needs history).
    from bayes_basket_target.deterministic_forecast_family import product_roster  # noqa: PLC0415

    wanted = {s.strip().upper() for s in args.stations.split(",") if s.strip()}
    stations = {}
    for product in products:
        roster = product_roster(conn, product)
        stations[product] = [s for s in roster if not wanted or s[0] in wanted]
    import time as _time  # noqa: PLC0415

    started = _time.monotonic()
    totals = {"written": 0, "existed": 0, "empty": 0, "failed": 0}
    try:
        batch = max(1, int(args.batch_days))
        for back in range(args.days, args.from_day - 1, -batch):
            offsets = tuple(-b for b in range(back, max(args.from_day, back - batch + 1) - 1, -1))
            for product in products:
                if args.max_seconds and _time.monotonic() - started > args.max_seconds:
                    print("时间预算用完，停在", f"day-{back}", product, flush=True)
                    raise SystemExit(0)
                if args.respect_production_network:
                    # Charter 267 补记三: a backfill is never worth production's fetches.
                    from bayes_basket_target.native_deterministic_point_archive import (  # noqa: PLC0415
                        production_network_strained,
                    )
                    waited = 0
                    while (strained := production_network_strained()):
                        if args.max_seconds and _time.monotonic() - started > args.max_seconds:
                            print("时间预算用完（等出口），停在", f"day-{back}", product, flush=True)
                            raise SystemExit(0)
                        if waited == 0:
                            print(f"   生产出口 {strained}，让路等待…", flush=True)
                        _time.sleep(60)
                        waited += 60
                    if waited:
                        print(f"   出口恢复，等了 {waited}s", flush=True)
                strained_check = None
                if args.respect_production_network:
                    from bayes_basket_target.native_deterministic_point_archive import (  # noqa: PLC0415
                        production_network_strained as strained_check,
                    )
                while True:
                    receipt = capture_p1(
                        conn, product=product, now=now, stations=stations[product],
                        day_offsets=offsets, max_runs=args.max_runs_per_day, dry_run=args.dry_run,
                        # Charter 99: a pass reaching weeks back asserts the run was
                        # on the archive then, not that it became readable today.
                        backfill=True,
                        # Charter 267 补记三: asked before every run, not just every batch.
                        should_yield=strained_check,
                    )
                    if not receipt.get("yielded"):
                        break
                    print(f"   生产出口 {receipt['yielded']}，本批中途让路（已写 {receipt['written']}）", flush=True)
                    for key in ("written", "existed", "empty"):
                        totals[key] += int(receipt[key])
                    while strained_check():
                        if args.max_seconds and _time.monotonic() - started > args.max_seconds:
                            print("时间预算用完（等出口），停在", f"day-{back}", product, flush=True)
                            raise SystemExit(0)
                        _time.sleep(60)
                for key in ("written", "existed", "empty"):
                    totals[key] += int(receipt[key])
                totals["failed"] += len(receipt["failed"])
                print(
                    f"{now:%H:%M:%S}Z day-{back} {product}: 计划 {receipt['runs_planned']} 写入 {receipt['written']}"
                    f" 已有 {receipt['existed']} 空 {receipt['empty']} 失败 {len(receipt['failed'])}",
                    flush=True,
                )
                for line in receipt["failed"][:3]:
                    print(f"    {line}", flush=True)
                now = datetime.now(timezone.utc)
    finally:
        for module in ("noaa_conus_archive", "aifs_single_point_archive",
                       "open_meteo_relay_archive", "cma_grapes_point_archive"):
            try:
                __import__(f"bayes_basket_target.{module}", fromlist=["close_client"]).close_client()
            except Exception:  # noqa: BLE001
                pass
        conn.close()
    print("合计", totals)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
