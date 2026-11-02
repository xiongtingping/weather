"""Checks A, B and C of PROTOCOL.md.

    uv run python docs/research/run_arrival_first_mover_20261017/evaluate.py hist events_hist.jsonl.gz
    uv run python docs/research/run_arrival_first_mover_20261017/evaluate.py forward events_fwd.jsonl.gz
"""

from __future__ import annotations

import gzip
import json
import random
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).parent
THRESHOLDS = (1.0, 1.5, 2.0)
WARMUP = 14
MIN_PAST = 30
FORWARD_FROM = "2026-10-18"
rng = random.Random(20261017)


def load(path: str) -> list[dict]:
    with gzip.open(HERE / path, "rt", encoding="utf-8") as f:
        return [json.loads(line) for line in f]


def dedupe(events: list[dict], min_z: float) -> list[dict]:
    """Rule R 3: one trade a station-day, the earliest enterable event."""

    by = defaultdict(list)
    for e in events:
        if e["entry"] is not None and abs(e["z"]) >= min_z:
            by[(e["station_id"], e["event_date"])].append(e)
    return [min(v, key=lambda e: e["had"]) for v in by.values()]


def summary(trades: list[dict]) -> str:
    if not trades:
        return "0 笔"
    by = defaultdict(lambda: [0.0, 0.0])
    for t in trades:
        by[t["event_date"]][0] += t["entry"]["pnl"]
        by[t["event_date"]][1] += t["entry"]["cost"]
    days = list(by)
    sims = []
    for _ in range(2000):
        s = [by[days[rng.randrange(len(days))]] for _ in days]
        sims.append(sum(x[0] for x in s) / sum(x[1] for x in s))
    sims.sort()
    pnl = sum(t["entry"]["pnl"] for t in trades)
    cost = sum(t["entry"]["cost"] for t in trades)
    hit = sum(t["entry"]["hit"] for t in trades) / len(trades)
    ask = sum(t["entry"]["ask"] for t in trades) / len(trades)
    return (f"{len(trades)} 笔/{len(days)} 天  命中 {100 * hit:.1f}%  均买价 {ask:.3f}  每股 {100 * pnl / len(trades):+.2f}¢"
            f"  回报率 {100 * pnl / cost:+.1f}% [{100 * sims[50]:+.1f}, {100 * sims[1949]:+.1f}]  合计 {pnl:+.2f}")


def breakdown(trades: list[dict]) -> None:
    for label, f in (("Δ>0 买 +1", lambda t: t["z"] > 0), ("Δ<0 买 −1", lambda t: t["z"] < 0)):
        print(f"    {label}  {summary([t for t in trades if f(t)])}")
    for lo, hi in ((0, 12), (12, 24), (24, 36), (36, 48.01)):
        print(f"    距结算 {lo}–{int(hi)}h  {summary([t for t in trades if lo <= t['lead_h'] < hi])}")
    systems = sorted({t["system"] for t in trades})
    for s in systems:
        print(f"    {s}  {summary([t for t in trades if t['system'] == s])}")
    for lo, hi in ((0, 0.1), (0.1, 0.2), (0.2, 0.3), (0.3, 1.01)):
        print(f"    买价 {lo:.1f}–{min(hi, 1):.1f}  {summary([t for t in trades if lo <= t['entry']['ask'] < hi])}")


def main(mode: str, path: str) -> None:
    events = load(path)
    print(f"事件 {len(events)}，能入场 {sum(e['entry'] is not None for e in events)}，"
          f"站日 {len({(e['station_id'], e['event_date']) for e in events})}")
    if mode == "forward":
        trades = [t for t in dedupe(events, 1.0) if t["event_date"] >= FORWARD_FROM]
        days = sorted({t["event_date"] for t in trades})
        print(f"\n## C 冻结后新数据（事件日 ≥ {FORWARD_FROM}，已结算 {len(days)} 个事件日；满 20 个再判）\n  {summary(trades)}")
        breakdown(trades)
        return
    print("\n## A 历史，规则 R 原样（|z| ≥ 1，按站日去重）——非样本外")
    a = dedupe(events, 1.0)
    print(f"  {summary(a)}")
    breakdown(a)
    for th in THRESHOLDS[1:]:
        print(f"  参考 |z| ≥ {th}  {summary(dedupe(events, th))}")
    halves = sorted({t["event_date"] for t in a})
    mid = halves[len(halves) // 2]
    print(f"  前半（< {mid}）  {summary([t for t in a if t['event_date'] < mid])}")
    print(f"  后半（≥ {mid}）  {summary([t for t in a if t['event_date'] >= mid])}")

    print("\n## B 历史内前推：每日只用 ≤ d−2 的已结算交易选 |z| 门槛")
    per = {th: dedupe(events, th) for th in THRESHOLDS}
    days = sorted({t["event_date"] for t in per[1.0]})
    chosen, picks = [], defaultdict(int)
    for i, d in enumerate(days):
        if i < WARMUP:
            continue
        cutoff = (date.fromisoformat(d) - timedelta(days=2)).isoformat()
        score = {}
        for th, tr in per.items():
            past = [t for t in tr if t["event_date"] <= cutoff]
            if len(past) >= MIN_PAST:
                score[th] = sum(t["entry"]["pnl"] for t in past) / sum(t["entry"]["cost"] for t in past)
        if not score:
            continue
        best = max(score, key=score.get)
        picks[best] += 1
        chosen += [t for t in per[best] if t["event_date"] == d]
    print(f"  {summary(chosen)}\n  门槛被选天数：" + "  ".join(f"|z|≥{k}: {v}" for k, v in sorted(picks.items())))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
