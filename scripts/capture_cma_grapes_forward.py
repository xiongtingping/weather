#!/usr/bin/env python3
"""Capture CMA GRAPES Global point curves for the mainland Chinese stations.

Eight of the roster's stations had no regional model of any kind: HRRR and
the NBM are CONUS, ICON-EU stops well west of them, and MSM's domain begins
at 120E, which reaches Shanghai and Qingdao and nothing further inland.
This lane is the first regional coverage Beijing, Guangzhou, Shenzhen,
Wuhan, Chongqing and Chengdu have had.

P1 only, and capture only.  The rows land in the regional point lane beside
MSM, HRRR, NBM and ICON-EU; whether any of them reaches a served
distribution is decided by scripts/measure_regional_serving_value.py and
recorded in regional_path_prior.SERVING_SYSTEMS, which today admits the MSM
alone.  Nothing in this script changes what P1 serves.

Forward only: see bayes_basket_target/cma_grapes_point_archive.py for why a
backfilled row could not honestly state when it became knowable.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target import sqlite_busy  # noqa: E402
from bayes_basket_target.cma_grapes_point_archive import (  # noqa: E402
    CAPTURE_STATIONS, close_client,
)
from bayes_basket_target.pass_deadline import (  # noqa: E402
    announce_write_blocked,
)

DB = ROOT / "data" / "bayes_basket_target_ssot.db"
STATIONS = CAPTURE_STATIONS
# One pass fetches the whole series per station, so a run costs eight
# requests.  Two runs a pass keeps an hourly sweep inside its own hour even
# when the aggregator is slow.
P1_MAX_RUNS_PER_PASS = 2
# Seconds of writer occupancy this lane's P1 pass may spend.  The resident
# admits one writer at a time, so a capture that runs to its subprocess
# timeout is a production cycle that cannot start for that long.  The run
# cap counts productive runs only, so an unpublished-but-slow run is
# unbounded work; see the budget note in regional_point_lane.capture_p1.
P1_BUDGET_SEC = 240.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DB)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-runs", type=int, default=P1_MAX_RUNS_PER_PASS)
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    conn = sqlite3.connect(str(args.db), timeout=120.0)
    try:
        # ``prepare`` registers the system and ensures the schema; both
        # write, so a locked file is answered here rather than after eight
        # station fetches this pass could not store.
        from bayes_basket_target.regional_point_lane import (  # noqa: PLC0415
            capture_p1, prepare,
        )
        try:
            prepare(conn)
        except sqlite_busy.WriteBlocked as exc:
            announce_write_blocked(f"建表/注册 {exc}")
            return 0
        conn.execute("PRAGMA busy_timeout=60000")
        receipt = capture_p1(
            conn, product="CMA", now=now, stations=STATIONS,
            max_runs=int(args.max_runs), budget_sec=P1_BUDGET_SEC,
            dry_run=args.dry_run,
        )
        print(
            f"P1 CMA: 计划 {receipt['runs_planned']} run，"
            f"有数据 {receipt['runs_with_data']}，写入 {receipt['written']}，"
            f"已有 {receipt['existed']}，空 {receipt['empty']}，"
            f"失败 {len(receipt['failed'])}"
        )
        for line in receipt["unpublished"][:4]:
            print(f"  尚未发布：{line}")
        for line in receipt["failed"][:4]:
            print(f"  {line}")
        if receipt.get("write_blocked"):
            # Another writer held the SSOT lock past this connection's
            # busy_timeout, so the pass stopped rather than fetching runs it
            # could not store.  Said on stdout in the resident's own words:
            # the lane gets its own state on the board and comes due again
            # in minutes instead of six hours.  Six hours is the whole
            # window here -- this transport serves only the run it holds.
            announce_write_blocked(receipt["write_blocked"])
        return 0
    finally:
        close_client()
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
