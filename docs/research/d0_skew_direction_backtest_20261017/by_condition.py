"""User 2026-10-17 「你不要用平均，按照条件，比如价格低于命中率，或者按照不同站点
统计」: the two arms (single bucket, two-bucket basket; direction agreeing) per
station and per price band, each with how far its hits sit from what its
prices promised (z = (hits - sum of prices) / sqrt(sum p(1-p))), and a
walk-forward check: trade only the stations whose record before d-1 was in
profit, and see what those trades then did.

    uv run python docs/research/d0_skew_direction_backtest_20261017/by_condition.py
"""

from __future__ import annotations

import gzip
import json
import math
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from judge import baskets, summary, trades

HERE = Path(__file__).parent


def z(rows: list[dict]) -> float:
    """Hits against the prices' promise, in standard deviations (basket legs'
    sum is the basket's promise)."""

    exp = sum(min(r["ask"], 0.999) for r in rows)
    var = sum(min(r["ask"], 0.999) * (1 - min(r["ask"], 0.999)) for r in rows)
    return (sum(r["hit"] for r in rows) - exp) / math.sqrt(var) if var > 0 else 0.0


def station_table(t: list[dict], name: str) -> None:
    by = defaultdict(list)
    for r in t:
        by[r["station_id"]].append(r)
    rows = sorted(by.items(), key=lambda kv: -sum(r["pnl"] for r in kv[1]))
    print(f"\n## {name}：按站点（按合计盈亏排序）")
    print("站点  笔  命中  均买价  合计盈亏  回报率  z(命中−价格)")
    for s, a in rows:
        hit = sum(r["hit"] for r in a) / len(a)
        ask = sum(r["ask"] for r in a) / len(a)
        pnl = sum(r["pnl"] for r in a)
        print(f"{s:5} {len(a):3} {100 * hit:5.1f}% {ask:6.3f} {pnl:+8.2f} {100 * pnl / sum(r['cost'] for r in a):+6.1f}% {z(a):+5.2f}")
    win = [s for s, a in rows if sum(r["pnl"] for r in a) > 0]
    big = [s for s, a in rows if z(a) >= 1.96]
    print(f"盈利站 {len(win)}/{len(rows)}；z≥1.96 的站 {len(big)}（{', '.join(big) or '无'}）；"
          f"{len(rows)} 站各检验一次，纯偶然也会有约 {0.025 * len(rows):.1f} 站 z≥1.96")


def walk_forward(t: list[dict], name: str, min_n: int = 8) -> None:
    """Day d trades a station only if its trades on event days <= d-2 summed
    to a profit over at least ``min_n`` of them."""

    days = sorted({r["event_date"] for r in t})
    picked = []
    for d in days:
        cutoff = (date.fromisoformat(d) - timedelta(days=2)).isoformat()
        past = defaultdict(list)
        for r in t:
            if r["event_date"] <= cutoff:
                past[r["station_id"]].append(r["pnl"])
        good = {s for s, v in past.items() if len(v) >= min_n and sum(v) > 0}
        picked += [r for r in t if r["event_date"] == d and r["station_id"] in good]
    print(f"\n## {name}：前推选站（只交易此前 ≥{min_n} 笔且合计盈利的站）\n  {summary(picked)}")
    print(f"  对照：同期全部站  {summary([r for r in t if picked and r['event_date'] >= min(x['event_date'] for x in picked)])}")


def band_table(t: list[dict], name: str, bands) -> None:
    print(f"\n## {name}：按买价档")
    for lo, hi in bands:
        a = [r for r in t if lo <= r["ask"] < hi]
        if a:
            print(f"  {lo:.2f}–{hi:.2f}  {summary(a)}  z {z(a):+.2f}")


def main() -> None:
    with gzip.open(HERE / "days.jsonl.gz", "rt", encoding="utf-8") as f:
        days = [json.loads(line) for line in f]
    single = trades(days, direction=True, sym_quiet=False)
    skewed = [r for r in single if r["skew"] != 0]
    basket = baskets(days, direction=True)
    band_table(skewed, "单桶（偏斜组：众数±1）", ((0, .2), (.2, .3), (.3, .4), (.4, .5), (.5, 1.01)))
    band_table([r for r in single if r["skew"] == 0], "单桶（对称组：众数）", ((0, .4), (.4, .5), (.5, .6), (.6, .7), (.7, 1.01)))
    band_table(basket, "两桶篮子（两腿买价和）", ((0, .7), (.7, .8), (.8, .85), (.85, .9), (.9, 1.01)))
    station_table(single, "单桶（主策略 A）")
    station_table(basket, "两桶篮子")
    walk_forward(single, "单桶（主策略 A）")
    walk_forward(basket, "两桶篮子")


if __name__ == "__main__":
    main()
