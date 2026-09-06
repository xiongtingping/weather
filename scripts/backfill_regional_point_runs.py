#!/usr/bin/env python3
"""Backfill the P1 regional lane with same-day runs the archives still hold.

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
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    products = [p.strip().upper() for p in args.products.split(",") if p.strip()]
    cycles = {int(c) for c in args.cycles.split(",") if c.strip()} if args.cycles else None
    from bayes_basket_target import regional_point_lane as lane  # noqa: PLC0415
    if cycles:
        # Narrow the lane's cadence for this pass only.
        for product in products:
            lane.RUN_CYCLE_HOURS[product] = tuple(h for h in RUN_CYCLE_HOURS[product] if h in cycles)
    stations = {}
    if "HRRR" in products or "NBM" in products:
        from bayes_basket_target.noaa_conus_archive import CAPTURE_STATIONS as noaa  # noqa: PLC0415
        stations.update({k: v for k, v in noaa.items() if k in products})
    if "MSM" in products:
        from bayes_basket_target.jma_msm_archive import CAPTURE_STATIONS as msm  # noqa: PLC0415
        stations["MSM"] = msm
    now = datetime.now(timezone.utc)
    conn = sqlite3.connect(str(DB), timeout=120.0)
    conn.execute("PRAGMA busy_timeout=60000")
    ensure_schema(conn)
    totals = {"written": 0, "existed": 0, "empty": 0, "failed": 0}
    try:
        for back in range(args.days, -1, -1):
            for product in products:
                receipt = capture_p1(
                    conn, product=product, now=now, stations=stations[product],
                    day_offsets=(-back,), max_runs=args.max_runs_per_day, dry_run=args.dry_run,
                    # Charter 99: a pass reaching weeks back asserts the run was
                    # on the archive then, not that it became readable today.
                    backfill=True,
                )
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
        try:
            from bayes_basket_target.noaa_conus_archive import close_client  # noqa: PLC0415
            close_client()
        except Exception:  # noqa: BLE001
            pass
        conn.close()
    print("合计", totals)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
