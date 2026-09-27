#!/usr/bin/env python3
"""Watch the 远尾做空线 (charter 234) until its first fill.  Read-only.

    python3 scripts/tail_short_monitor.py [--interval 60] [--max-minutes 720] [--once]

Every pass prints what an operator needs to tell the lane's states apart: is
the lane switched on, is the pause flag up, is the intent armed (the lane
reads and writes nothing otherwise), has the resident run it at all (its
table exists), what it fired on and why it refused, and what its orders did
at the venue.  Exits 0 at the first fill, 2 when the watch runs
out, 1 when the store cannot be read.  Never writes: the store is opened
``mode=ro`` with ``query_only``.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bayes_basket_target.order_submission import live_intent_armed  # noqa: E402
from bayes_basket_target.strategy_lanes import LANE_TAIL_SHORT, lane_enabled, load_strategy_lanes  # noqa: E402

SSOT = ROOT / "data" / "bayes_basket_target_ssot.db"
PAUSE_FLAG = ROOT / "data" / "live_paused.flag"
TABLE = "tail_short_lane_evaluation"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _has(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def one_pass(conn: sqlite3.Connection) -> dict:
    lanes = load_strategy_lanes()
    out = {
        "at": _now(),
        "lane_enabled": lane_enabled(LANE_TAIL_SHORT),
        "lanes_source": lanes.get("source"),
        "pause_flag": PAUSE_FLAG.exists(),
        "armed": live_intent_armed(),
        "table": _has(conn, TABLE),
        "fills": [],
    }
    if not out["table"]:
        return out
    row = conn.execute(
        f"SELECT COUNT(*), COUNT(DISTINCT station_id||event_date||bucket), COALESCE(SUM(sent),0),"
        f" COALESCE(SUM(filled_shares > 0),0), MAX(evaluated_at_utc) FROM {TABLE}"
    ).fetchone()
    out.update(rows=row[0], buckets=row[1], sends=row[2], filled_rows=row[3], last_evaluated=row[4])
    out["refusals"] = conn.execute(
        f"SELECT substr(refusal,1,CASE WHEN instr(refusal,':')>0 THEN instr(refusal,':')-1 ELSE length(refusal) END) r,"
        f" COUNT(*) FROM {TABLE} WHERE refusal IS NOT NULL GROUP BY r ORDER BY 2 DESC LIMIT 6"
    ).fetchall()
    out["recent"] = conn.execute(
        f"SELECT evaluated_at_utc, station_id, event_date, bucket, market_q, yes_bid, no_price, shares,"
        f" sent, filled_shares, refusal FROM {TABLE} ORDER BY evaluated_at_utc DESC LIMIT 5"
    ).fetchall()
    if _has(conn, "basket_target_order_intents"):
        out["intents"] = conn.execute(
            "SELECT created_at, bucket, side, limit_price, requested_size, state"
            " FROM basket_target_order_intents WHERE json_extract(intent_json,'$.lane')=?"
            " ORDER BY created_at DESC LIMIT 5",
            (LANE_TAIL_SHORT,),
        ).fetchall()
        out["fills"] = conn.execute(
            "SELECT created_at, order_intent_id, bucket, side, limit_price, requested_size, state"
            " FROM basket_target_order_intents WHERE json_extract(intent_json,'$.lane')=? AND state='filled'"
            " ORDER BY created_at LIMIT 3",
            (LANE_TAIL_SHORT,),
        ).fetchall()
    return out


def show(p: dict) -> None:
    print(f"[{p['at']}] lane_enabled={p['lane_enabled']} (lanes {p['lanes_source']})"
          f" pause_flag={'UP' if p['pause_flag'] else 'down'} armed={'yes' if p['armed'] else 'NO'}"
          f" table={'yes' if p['table'] else 'NOT YET (resident has not run the lane)'}")
    if not p["table"]:
        return
    print(f"  rows {p['rows']} · buckets fired {p['buckets']} · sends {p['sends']} · filled {p['filled_rows']}"
          f" · last evaluated {p['last_evaluated']}")
    if p.get("refusals"):
        print("  refusals: " + ", ".join(f"{r}×{n}" for r, n in p["refusals"]))
    for r in p.get("recent") or []:
        print(f"  {r[0]} {r[1]} {r[2]} {r[3]} q={r[4]} bid={r[5]} no={r[6]} x{r[7]} sent={r[8]} filled={r[9]} {r[10] or ''}")
    for i in p.get("intents") or []:
        print(f"  intent {i[0]} {i[1]} {i[2]} @{i[3]} x{i[4]} -> {i[5]}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ssot", default=str(SSOT))
    parser.add_argument("--interval", type=float, default=60.0)
    parser.add_argument("--max-minutes", type=float, default=720.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    deadline = time.monotonic() + args.max_minutes * 60.0
    while True:
        try:
            conn = sqlite3.connect(f"file:{args.ssot}?mode=ro", uri=True, timeout=30)
            conn.execute("PRAGMA query_only=ON")
            try:
                report = one_pass(conn)
            finally:
                conn.close()
        except sqlite3.Error as exc:
            print(f"[{_now()}] store unreadable: {type(exc).__name__}: {exc}")
            return 1
        show(report)
        if report["fills"]:
            first = report["fills"][0]
            print(f"FIRST FILL: {first[0]} intent {first[1]} {first[2]} {first[3]} @{first[4]} x{first[5]} ({first[6]})")
            return 0
        if args.once or time.monotonic() >= deadline:
            return 2
        time.sleep(max(5.0, args.interval))


if __name__ == "__main__":
    sys.exit(main())
