"""Readings of PROTOCOL.md over days.json.

    uv run python docs/research/tropical_tmax_regimes_20261018/analyze.py
"""

from __future__ import annotations

import json
import math
import random
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent
rng = random.Random(20261018)
MON = "1 2 3 4 5 6 7 8 9 10 11 12".split()


def rate(rows, cls):
    return sum(r["cls"] == cls for r in rows) / len(rows) if rows else float("nan")


def line(label, rows, base):
    if len(rows) < 20:
        return f"  {label:<22} n={len(rows):4d}  （太少）"
    c, w = rate(rows, "cool"), rate(rows, "warm")
    # year-block bootstrap of the cool / warm rate
    by = defaultdict(list)
    for r in rows:
        by[r["year"]].append(r)
    ys = list(by)
    cs, ws = [], []
    for _ in range(500):
        s = [r for _ in ys for r in by[rng.choice(ys)]]
        cs.append(rate(s, "cool"))
        ws.append(rate(s, "warm"))
    cs.sort()
    ws.sort()
    return (f"  {label:<22} n={len(rows):4d}  降温 {100 * c:4.0f}% [{100 * cs[12]:.0f},{100 * cs[487]:.0f}] ×{c / base[0]:.2f}"
            f"   突破 {100 * w:4.0f}% [{100 * ws[12]:.0f},{100 * ws[487]:.0f}] ×{w / base[1]:.2f}")


def main() -> None:
    days = json.loads((HERE / "days.json").read_text())
    for st in ("WSSS", "WMKK"):
        R = [r for r in days if r["station"] == st]
        base = (rate(R, "cool"), rate(R, "warm"))
        hist = defaultdict(int)
        for r in R:
            hist[r["tmax"]] += 1
        print(f"\n# {st}  {len(R)} 天  降温日 {100 * base[0]:.0f}%  突破日 {100 * base[1]:.0f}%  "
              f"日最高温分布 " + " ".join(f"{k}:{v}" for k, v in sorted(hist.items())))
        print(f"  距基准 Δ 分布 " + " ".join(f"{k:+d}:{sum(r['anom'] == k for r in R)}" for k in range(-5, 5)))

        print("\n## 季节（按月）")
        for m in range(1, 13):
            print(line(f"{m} 月", [r for r in R if r["month"] == m], base))

        print("\n## 当天：首次降水/雷暴的时刻（事后解释）")
        cats = (("10 点前", lambda h: h is not None and h < 10), ("10–13 点", lambda h: h is not None and 10 <= h < 13),
                ("13–16 点", lambda h: h is not None and 13 <= h < 16), ("16 点后", lambda h: h is not None and h >= 16),
                ("全天无", lambda h: h is None))
        for name, f in cats:
            print(line(f"降水 {name}", [r for r in R if f(r["first_rain_h"])], base))
        for name, f in cats:
            print(line(f"雷暴（含附近）{name}", [r for r in R if f(r["first_ts_h"])], base))
        print(line("午间(12–15)低云≥一半", [r for r in R if (r["noon_low_cloud"] or 0) >= 0.5], base))
        print(line("午间低云<一半", [r for r in R if r["noon_low_cloud"] is not None and r["noon_low_cloud"] < 0.5], base))
        peaks = defaultdict(list)
        for r in R:
            peaks[r["cls"]].append(r["peak_h"])
        print("  最高温首次出现的当地时刻（中位）：" + "，".join(
            f"{c} {sorted(v)[len(v) // 2]:.1f} 点" for c, v in peaks.items()))

        print("\n## 事前（当地 10 点前可知）")
        groups = [
            ("前一天比基准低≥1", lambda r: r["prev_anom"] is not None and r["prev_anom"] <= -1),
            ("前一天≈基准", lambda r: r["prev_anom"] is not None and -1 < r["prev_anom"] < 1),
            ("前一天比基准高≥1", lambda r: r["prev_anom"] is not None and r["prev_anom"] >= 1),
            ("前一天傍晚下雨", lambda r: r["prev_eve_rain"]),
            ("10 点前已下雨", lambda r: r["morning_rain"]),
            ("早上 8–9 点比往常冷≥1°", lambda r: r["morning_t_anom"] is not None and r["morning_t_anom"] <= -1),
            ("早上 8–9 点≈往常", lambda r: r["morning_t_anom"] is not None and -1 < r["morning_t_anom"] < 1),
            ("早上 8–9 点比往常暖≥1°", lambda r: r["morning_t_anom"] is not None and r["morning_t_anom"] >= 1),
            ("早上露点比往常低≥1°", lambda r: r["morning_td_anom"] is not None and r["morning_td_anom"] <= -1),
            ("早上露点比往常高≥1°", lambda r: r["morning_td_anom"] is not None and r["morning_td_anom"] >= 1),
            ("早上低云≥一半", lambda r: (r["morning_low_cloud"] or 0) >= 0.5),
            ("早上风速≥8 节", lambda r: (r["morning_kt"] or 0) >= 8),
            ("早上风速<4 节", lambda r: r["morning_kt"] is not None and r["morning_kt"] < 4),
            ("早上偏北风（v<−2）", lambda r: r["morning_v"] is not None and r["morning_v"] < -2),
            ("早上偏南风（v>+2）", lambda r: r["morning_v"] is not None and r["morning_v"] > 2),
            ("早上偏西风（u>+2）", lambda r: r["morning_u"] is not None and r["morning_u"] > 2),
            ("早上偏东风（u<−2）", lambda r: r["morning_u"] is not None and r["morning_u"] < -2),
        ]
        for name, f in groups:
            print(line(name, [r for r in R if f(r)], base))
        GROUPS[st] = groups


GROUPS: dict = {}


def walk_forward() -> None:
    """Year blocks: each test year uses only earlier years to pick, among the
    pre-registered morning groups, the one with the highest cool (warm) rate
    lift; report that pick's lift in the test year, and every group's test-year
    lift beside it."""

    days = json.loads((HERE / "days.json").read_text())
    print("\n# 前推（以年为块，只用之前年份挑分组）")
    for st in ("WSSS", "WMKK"):
        R = [r for r in days if r["station"] == st]
        groups = GROUPS[st]
        for cls, word in (("cool", "降温"), ("warm", "突破")):
            print(f"\n## {st} {word}")
            for year in (2023, 2024, 2025, 2026):
                train = [r for r in R if r["year"] < year]
                test = [r for r in R if r["year"] == year]
                tb, sb = rate(train, cls), rate(test, cls)
                lifts = []
                for name, f in groups:
                    a = [r for r in train if f(r)]
                    if len(a) >= 30:
                        lifts.append((rate(a, cls) / tb, name, f))
                lifts.sort(reverse=True)
                if not lifts:
                    continue
                lift, name, f = lifts[0]
                t = [r for r in test if f(r)]
                tl = rate(t, cls) / sb if t and sb > 0 else float("nan")
                print(f"  {year}：之前年份挑「{name}」（训练 ×{lift:.2f}）→ 当年 n={len(t)}，"
                      f"{word}率 {100 * rate(t, cls) if t else float('nan'):.0f}% 对全年 {100 * sb:.0f}%（×{tl:.2f}）")


if __name__ == "__main__":
    main()
    walk_forward()
