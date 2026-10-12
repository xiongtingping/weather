#!/usr/bin/env python3
"""Capture the deterministic-forecast family's global members (charter 201).

AIFS Single from ECMWF's own open data (GCS mirror), and the relayed IFS 9 km
and UKMO 10 km runs from the Open-Meteo single-runs endpoint, and (charter 267
(二)) the native IFS HRES / GFS / AIGFS / GDPS runs for the listing-time
forecast F -- read by F only, never by P0, P0-M or P1 -- at every roster
station with coordinates, every run while it is current, for every listed
target day it reaches (D-1 .. D+2).  Rows land in the regional point lane's
table beside HRRR, NBM, ICON-EU, MSM and CMA; none of them reaches P1 (its
pairs are frozen, ``regional_features.P1_FEATURE_PAIRS``), none is an
ensemble member.  They are the P0 feature's inputs and its shadow record.

The relayed runs are knowable when this pass fetched them, never at the
model's init, and every relay call is charged to the family's Open-Meteo
budget first (``open_meteo_quota``): a spent budget or an exhausted day skips
the relay products for the pass, AIFS still runs.

Same shape as every capture lane: a pass plans with SSOT reads only, takes
no write lock when nothing is new, fetches a run, writes it, commits, and
only then fetches the next (``WRITER_TURN_EXEMPT_SWEEPS``).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import open_meteo_quota, sqlite_busy  # noqa: E402
from bayes_basket_target.pass_deadline import (  # noqa: E402
    announce_write_blocked, set_pass_deadline,
)

DB = ROOT / "data" / "bayes_basket_target_ssot.db"
# Charter 267 (二): plus the native deterministic runs for the listing-time F.
PRODUCTS = ("AIFS", "IFS9KM", "UKMO10", "IFS_HRES", "GFS", "AIGFS", "GDPS", "ICON_GLOBAL", "ARPEGE", "UKMO_GLOBAL")
# Runs a pass may take per product: the newest, and one it missed.
MAX_RUNS_PER_PRODUCT = 2
# Writer-free seconds per product; the pass as a whole stays inside the
# sweep's timeout with room for the relay's pauses.
BUDGET_SEC_PER_PRODUCT = 240.0
# Charter 267 (二): a native deterministic run of the whole roster for D-1..D+2
# measured 6-31 s (AIGFS 6, GFS 12, HRES 17, GDPS 31; 2026-10-11) once each
# field is decoded once for every station; two runs fit well inside this, and
# the pass's worst case (3 x 240 + 4 x 120 plus the per-product margin) stays
# under the sweep's 1800 s timeout.  The lane commits before each next fetch
# (WRITER_TURN_EXEMPT_SWEEPS), so a fetch never holds the writer.
NATIVE_BUDGET_SEC_PER_PRODUCT = 120.0
# Charter 267 补记二: ARPEGE walks four unindexed packages' headers (~60 s a
# run) and ICON reads ~33 whole bz2 steps (~45 s); one run a pass each, the
# newest, so a pass does not hold the capture claim the P1 lanes wait on.
NATIVE_BUDGET_OVERRIDE_SEC = {"ARPEGE": 180.0, "UKMO_GLOBAL": 240.0}
MAX_RUNS_OVERRIDE = {"ARPEGE": 1, "ICON_GLOBAL": 1, "UKMO_GLOBAL": 1}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DB)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--products", default=",".join(PRODUCTS))
    parser.add_argument("--max-runs", type=int, default=MAX_RUNS_PER_PRODUCT)
    args = parser.parse_args()

    from bayes_basket_target.aifs_single_point_archive import (  # noqa: PLC0415
        close_client as close_aifs,
    )
    from bayes_basket_target.deterministic_forecast_family import (  # noqa: PLC0415
        product_roster,
    )
    from bayes_basket_target.native_deterministic_point_archive import (  # noqa: PLC0415
        HEAVY_PRODUCTS, production_network_strained,
        close_client as close_native,
    )
    from bayes_basket_target.open_meteo_relay_archive import (  # noqa: PLC0415
        close_client as close_relay,
    )
    from bayes_basket_target.regional_point_lane import (  # noqa: PLC0415
        NATIVE_DETERMINISTIC_PRODUCTS, RELAY_PRODUCTS, capture_p1, ensure_schema, plan_p1,
    )

    now = datetime.now(timezone.utc)
    conn = sqlite3.connect(str(args.db), timeout=120.0)
    try:
        plans = {}
        for product in [p.strip() for p in args.products.split(",") if p.strip()]:
            if product not in PRODUCTS:
                print(f"{product}: 不在本车道")
                continue
            if product in RELAY_PRODUCTS and open_meteo_quota.exhausted(now):
                print(f"{product}: Open-Meteo 今日额度已耗尽，跳过")
                continue
            if product in HEAVY_PRODUCTS and (strained := production_network_strained()):
                # Charter 267 补记三: tens of MB a run over slow links; production first.
                print(f"{product}: 生产出口 {strained}，本遍让路")
                continue
            try:
                stations = product_roster(conn, product)
                plan = plan_p1(conn, product=product, now=now, stations=stations,
                               max_runs=int(args.max_runs))
            except sqlite3.Error as exc:
                print(f"{product}: 计划读库失败 {exc}")
                continue
            if not plan:
                print(f"{product}: 没有待取的轮次")
                continue
            plans[product] = stations
        if not plans:
            return 0
        try:
            # The lane's table exists in production; this only creates it on a
            # fresh file.  No registry rows: the family is not an ensemble.
            with sqlite_busy.blocked_when_busy():
                ensure_schema(conn)
                conn.commit()
        except sqlite_busy.WriteBlocked as exc:
            announce_write_blocked(f"建表 {exc}")
            return 0
        conn.execute("PRAGMA busy_timeout=60000")
        for product, stations in plans.items():
            budget = NATIVE_BUDGET_OVERRIDE_SEC.get(product, (
                NATIVE_BUDGET_SEC_PER_PRODUCT
                if product in NATIVE_DETERMINISTIC_PRODUCTS else BUDGET_SEC_PER_PRODUCT
            ))
            set_pass_deadline(budget + 60.0)
            receipt = capture_p1(
                conn, product=product, now=now, stations=stations,
                max_runs=min(int(args.max_runs), MAX_RUNS_OVERRIDE.get(product, int(args.max_runs))),
                budget_sec=budget,
                # 补记三: a heavy family stops between runs when production's egress strains.
                should_yield=production_network_strained if product in HEAVY_PRODUCTS else None,
                dry_run=args.dry_run,
            )
            set_pass_deadline(None)
            print(
                f"{product}: 计划 {receipt['runs_planned']} run，有数据 {receipt['runs_with_data']}，"
                f"写入 {receipt['written']}，已有 {receipt['existed']}，空 {receipt['empty']}，"
                f"失败 {len(receipt['failed'])}"
            )
            for line in receipt["unpublished"][:3]:
                print(f"  尚未发布：{line}")
            for line in receipt["failed"][:3]:
                print(f"  {line}")
            if receipt.get("write_blocked"):
                announce_write_blocked(receipt["write_blocked"])
                break
        print(f"Open-Meteo 本车道今日已用 {open_meteo_quota.spent('deterministic_relay', now)}"
              f"/{open_meteo_quota.DAILY_BUDGET['deterministic_relay']}")
        return 0
    finally:
        close_aifs()
        close_native()
        close_relay()
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
