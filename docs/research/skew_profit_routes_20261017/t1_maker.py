"""Route 1 of PROTOCOL.md: a resting bid instead of taking the ask.

    uv run python docs/research/skew_profit_routes_20261017/t1_maker.py
"""

from __future__ import annotations

import gzip
import json
import random
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parents[2]
PRIOR = ROOT / "docs" / "research" / "d0_skew_direction_backtest_20261017"
sys.path.insert(0, str(PRIOR))
from judge import pick  # noqa: E402

DB = ROOT / "data" / "bayes_basket_target_ssot.db"
FEE = 0.05
HORIZONS = (("60 分钟", timedelta(minutes=60)), ("6 小时", timedelta(hours=6)), ("到当日结束", timedelta(days=2)))
rng = random.Random(20261017)


def _t(s: str) -> datetime:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def book_at(conn, station, day, at):
    row = conn.execute(
        "SELECT buckets_json FROM basket_official_ws_snapshot WHERE station_id=? AND event_date=? AND state='COMMITTED'"
        " AND observed_at<=? ORDER BY observed_at DESC LIMIT 1", (station, day, at)).fetchone()
    return {str(b["bucket"]): b for b in json.loads(row[0])} if row else {}


def best(levels, side):
    px = [float(x["price"]) for x in levels or [] if float(x.get("shares") or 0) > 0]
    return (max(px) if side == "bid" else min(px)) if px else None


def roi(rows):
    if not rows:
        return "0 笔"
    by = defaultdict(lambda: [0.0, 0.0])
    for r in rows:
        by[r["event_date"]][0] += r["pnl"]
        by[r["event_date"]][1] += r["cost"]
    days = list(by)
    sims = []
    for _ in range(2000):
        s = [by[days[rng.randrange(len(days))]] for _ in days]
        sims.append(sum(x[0] for x in s) / sum(x[1] for x in s))
    sims.sort()
    pnl, cost = sum(r["pnl"] for r in rows), sum(r["cost"] for r in rows)
    hit = sum(r["hit"] for r in rows) / len(rows)
    px = sum(r["price"] for r in rows) / len(rows)
    return (f"{len(rows)} 笔  命中 {100 * hit:.1f}%  均价 {px:.3f}  回报率 {100 * pnl / cost:+.1f}%"
            f" [{100 * sims[50]:+.1f}, {100 * sims[1949]:+.1f}]  合计 {pnl:+.2f}")


def main() -> None:
    with gzip.open(PRIOR / "days.jsonl.gz", "rt", encoding="utf-8") as f:
        days = [json.loads(line) for line in f]
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    conn.execute("PRAGMA query_only=1")
    orders = []  # one per leg
    for d in days:
        for cp in d["checkpoints"]:
            c = pick(cp, direction=True, sym_quiet=False)
            if c is None:
                continue
            legs = [("single", c)]
            if cp["skew"] != 0 and cp["cand"].get("0"):
                legs.append(("basket_mode", cp["cand"]["0"]))
                legs.append(("basket_side", c))
            for kind, leg in legs:
                orders.append({"station_id": d["station_id"], "event_date": d["event_date"], "at": cp["at"],
                               "skew": cp["skew"], "kind": kind, "bucket": leg["bucket"], "hit": leg["hit"],
                               "ask": leg["ask"]})
            break
    out = []
    for o in orders:
        book = book_at(conn, o["station_id"], o["event_date"], o["at"]).get(o["bucket"])
        if not book:
            continue
        tick = float(book.get("price_tick_size") or 0.01)
        bid = best(book.get("bid_levels"), "bid")
        ask = best(book.get("ask_levels"), "ask")
        if ask is None:
            continue
        price = round(min((bid or 0.0) + tick, ask - tick), 6)
        if price <= 0:
            continue
        later = conn.execute(
            "SELECT observed_at, buckets_json FROM basket_official_ws_snapshot WHERE station_id=? AND event_date=?"
            " AND state='COMMITTED' AND observed_at>? ORDER BY observed_at", (o["station_id"], o["event_date"], o["at"]),
        ).fetchall()
        start = _t(o["at"])
        fill_at = None
        for at, raw in later:
            for b in json.loads(raw):
                if str(b["bucket"]) == o["bucket"]:
                    a2 = best(b.get("ask_levels"), "ask")
                    if a2 is not None and a2 <= price + 1e-9:
                        fill_at = _t(at)
                    break
            if fill_at:
                break
        out.append({**o, "price": price, "taker_ask": ask,
                    "fill_after_min": None if fill_at is None else (fill_at - start).total_seconds() / 60})
    with gzip.open(HERE / "t1_orders.jsonl.gz", "wt", encoding="utf-8") as f:
        for r in out:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def maker(r):
        return {**r, "pnl": r["hit"] - r["price"], "cost": r["price"]}

    def taker(r):
        a = r["taker_ask"]
        return {**r, "price": a, "pnl": r["hit"] - a - FEE * a * (1 - a), "cost": a + FEE * a * (1 - a)}

    print(f"挂单 {len(out)} 条（单桶 {sum(r['kind'] == 'single' for r in out)}，篮子腿 {sum(r['kind'] != 'single' for r in out)}）")
    for name, kinds in (("单桶主策略 A", ("single",)), ("两桶篮子（逐腿）", ("basket_mode", "basket_side"))):
        rows = [r for r in out if r["kind"] in kinds]
        print(f"\n## {name}\n  全部吃单（对照）  {roi([taker(r) for r in rows])}")
        for label, h in HORIZONS:
            filled = [r for r in rows if r["fill_after_min"] is not None and r["fill_after_min"] <= h.total_seconds() / 60]
            missed = [r for r in rows if r not in filled]
            print(f"  挂单 {label}：成交 {len(filled)}/{len(rows)}（{100 * len(filled) / max(1, len(rows)):.0f}%）")
            print(f"    成交单（挂价成交、无手续费）  {roi([maker(r) for r in filled])}")
            print(f"    成交单若当初吃单              {roi([taker(r) for r in filled])}")
            print(f"    未成交单若当初吃单            {roi([taker(r) for r in missed])}")


if __name__ == "__main__":
    main()
