"""Readings 1-5 of PROTOCOL.md over days.jsonl.gz.

    uv run python docs/research/d0_skew_direction_backtest_20261017/judge.py
"""

from __future__ import annotations

import gzip
import json
import random
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent
FEE = 0.05
BOOT = 2000
rng = random.Random(20261017)
GROUP = {-1: "左偏→众数−1", 1: "右偏→众数+1", 0: "对称→众数"}


def pick(cp: dict, *, direction: bool, sym_quiet: bool) -> dict | None:
    """The bucket this checkpoint would buy, or None."""

    s = cp["skew"]
    if s != 0 and direction and cp["side"] != s:
        return None
    if s == 0 and sym_quiet and cp["side"] != 0:
        return None
    c = cp["cand"].get(str(s))
    if not c or c["ask"] is None or not 0.0 < c["ask"] < 1.0:
        return None
    return c


def trades(days: list[dict], *, direction: bool, sym_quiet: bool, first_only: bool = False) -> list[dict]:
    out = []
    for d in days:
        cps = d["checkpoints"][:1] if first_only else d["checkpoints"]
        for cp in cps:
            c = pick(cp, direction=direction, sym_quiet=sym_quiet)
            if c is None:
                continue
            a = c["ask"]
            fee = FEE * a * (1 - a)
            out.append({"event_date": d["event_date"], "station_id": d["station_id"], "skew": cp["skew"],
                        "hour": cp["local_hour"], "ask": a, "hit": c["hit"], "pnl": c["hit"] - a - fee,
                        "cost": a + fee, "p": c["p"], "depth": c["ask_shares"]})
            break
    return out


def baskets(days: list[dict], *, direction: bool, first_only: bool = False) -> list[dict]:
    """User 2026-10-17 「2桶篮子保本策略：右偏买众数和+1桶，左偏买众数和-1桶」: the
    same number of shares of both, so either landing pays the same; only when
    the two asks plus fees are under 1 (a hit then covers the cost).  Skewed
    checkpoints only; one basket a station-day."""

    out = []
    for d in days:
        cps = d["checkpoints"][:1] if first_only else d["checkpoints"]
        for cp in cps:
            s = cp["skew"]
            if s == 0 or (direction and cp["side"] != s):
                continue
            legs = [cp["cand"].get("0"), cp["cand"].get(str(s))]
            if any(c is None or c["ask"] is None or not 0.0 < c["ask"] < 1.0 for c in legs):
                continue
            cost = sum(c["ask"] + FEE * c["ask"] * (1 - c["ask"]) for c in legs)
            if cost >= 1.0:
                continue
            hit = max(c["hit"] for c in legs)
            out.append({"event_date": d["event_date"], "station_id": d["station_id"], "skew": s,
                        "hour": cp["local_hour"], "ask": sum(c["ask"] for c in legs), "hit": hit,
                        "pnl": hit - cost, "cost": cost, "depth": min(c["ask_shares"] or 0 for c in legs),
                        "mode_hit": legs[0]["hit"], "side_hit": legs[1]["hit"]})
            break
    return out


def summary(t: list[dict]) -> str:
    if not t:
        return "0 笔"
    by = defaultdict(lambda: [0.0, 0.0, 0])
    for r in t:
        x = by[r["event_date"]]
        x[0] += r["pnl"]
        x[1] += r["cost"]
        x[2] += 1
    days = list(by)
    pnl, cost = sum(r["pnl"] for r in t), sum(r["cost"] for r in t)
    roi, per = [], []
    for _ in range(BOOT):
        s = [by[days[rng.randrange(len(days))]] for _ in days]
        roi.append(sum(x[0] for x in s) / sum(x[1] for x in s))
        per.append(sum(x[0] for x in s) / sum(x[2] for x in s))
    roi.sort()
    per.sort()
    lo, hi = int(0.025 * BOOT), int(0.975 * BOOT) - 1
    hit = sum(r["hit"] for r in t) / len(t)
    ask = sum(r["ask"] for r in t) / len(t)
    return (f"{len(t)} 笔/{len(days)} 天  命中 {100 * hit:.1f}%  均买价 {ask:.3f}  每股 {100 * pnl / len(t):+.2f}¢"
            f" [{100 * per[lo]:+.2f}, {100 * per[hi]:+.2f}]  回报率 {100 * pnl / cost:+.1f}% [{100 * roi[lo]:+.1f}, {100 * roi[hi]:+.1f}]"
            f"  合计 {pnl:+.2f} 美元")


def period(day: str) -> str:
    return "08-20..09-21" if day < "2026-09-22" else "09-22..10-08 样本内" if day <= "2026-10-08" else "10-09 起 拟合后"


def main() -> None:
    with gzip.open(HERE / "days.jsonl.gz", "rt", encoding="utf-8") as f:
        days = [json.loads(line) for line in f]
    print(f"站日 {len(days)}，事件日 {len({d['event_date'] for d in days})}"
          f"（{min(d['event_date'] for d in days)}..{max(d['event_date'] for d in days)}），"
          f"无物理特征的站日 {sum(not d['has_physics'] for d in days)}")
    arms = {
        "主策略 A（对称不看方向）": dict(direction=True, sym_quiet=False),
        "主策略 B（对称须方向安静）": dict(direction=True, sym_quiet=True),
        "对照：去掉方向条件": dict(direction=False, sym_quiet=False),
        "对照：主策略 A，只在 00:00 后第一个检查点决定": dict(direction=True, sym_quiet=False, first_only=True),
    }
    for name, kw in arms.items():
        t = trades(days, **kw)
        print(f"\n## {name}\n  全部  {summary(t)}")
        for g in (-1, 1, 0):
            print(f"  {GROUP[g]}  {summary([r for r in t if r['skew'] == g])}")
        if name.startswith("主策略 A") and "第一个" not in name:
            print("  按入场当地时段")
            for lo, hi in ((0, 6), (6, 12), (12, 18), (18, 24)):
                print(f"    {lo:02d}–{hi:02d}  {summary([r for r in t if lo <= r['hour'] < hi])}")
            print("  按时期")
            for p in ("08-20..09-21", "09-22..10-08 样本内", "10-09 起 拟合后"):
                print(f"    {p}  {summary([r for r in t if period(r['event_date']) == p])}")
            print("  按买价")
            for lo, hi in ((0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 1.01)):
                print(f"    {lo:.1f}–{min(hi, 1):.1f}  {summary([r for r in t if lo <= r['ask'] < hi])}")
            dep = sorted(r["depth"] for r in t if r["depth"] is not None)
            print(f"  卖一挂量 中位 {dep[len(dep) // 2]:.0f} 股，10% 分位 {dep[len(dep) // 10]:.0f} 股")
    basket_report(days)


def basket_report(days: list[dict]) -> None:
    for name, kw in (("两桶篮子，方向同向", dict(direction=True)), ("两桶篮子，不看方向", dict(direction=False)),
                     ("两桶篮子，方向同向，只在 00:00 后首个检查点", dict(direction=True, first_only=True))):
        t = baskets(days, **kw)
        print(f"\n## {name}（「均买价」＝两腿卖一之和，命中＝任一腿中）\n  全部  {summary(t)}")
        for g, w in ((-1, "左偏：众数＋众数−1"), (1, "右偏：众数＋众数+1")):
            a = [r for r in t if r["skew"] == g]
            print(f"  {w}  {summary(a)}")
            if a:
                print(f"    其中 众数中 {sum(r['mode_hit'] for r in a)}、偏侧桶中 {sum(r['side_hit'] for r in a)}、"
                      f"都不中 {sum(1 - r['hit'] for r in a)}")
        if "首个" not in name and "不看" not in name:
            for p in ("08-20..09-21", "09-22..10-08 样本内", "10-09 起 拟合后"):
                print(f"    {p}  {summary([r for r in t if period(r['event_date']) == p])}")
            dep = sorted(r["depth"] for r in t)
            print(f"  两腿卖一挂量较小者 中位 {dep[len(dep) // 2]:.0f} 股")


if __name__ == "__main__":
    main()
