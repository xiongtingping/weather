"""Route 2 of PROTOCOL.md: buy where our served belief beats the ask.

    uv run python docs/research/skew_profit_routes_20261017/t2_disagree.py
"""

from __future__ import annotations

import gzip
import json
import random
import sqlite3
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
from bayes_basket_target.basket_kernel.support import mean_mode_skew_reading_v1  # noqa: E402
from bayes_basket_target.board_readings import _served_belief_vector  # noqa: E402

PRIOR = ROOT / "docs" / "research" / "d0_skew_direction_backtest_20261017"
DB = ROOT / "data" / "bayes_basket_target_ssot.db"
FEE = 0.05
EDGES = (0.0, 0.05, 0.10)
WARMUP = 14
rng = random.Random(20261017)


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
    return (f"{len(rows)} 笔/{len(days)} 天  命中 {100 * sum(r['hit'] for r in rows) / len(rows):.1f}%"
            f"  均买价 {sum(r['ask'] for r in rows) / len(rows):.3f}  我方均概率 {sum(r['ours'] for r in rows) / len(rows):.3f}"
            f"  回报率 {100 * pnl / cost:+.1f}% [{100 * sims[50]:+.1f}, {100 * sims[1949]:+.1f}]  合计 {pnl:+.2f}")


def main() -> None:
    with gzip.open(PRIOR / "days.jsonl.gz", "rt", encoding="utf-8") as f:
        days = [json.loads(line) for line in f]
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    conn.execute("PRAGMA query_only=1")
    cache: dict = {}
    # Every checkpoint's candidates with our probability beside the ask.
    rows = []
    for d in days:
        for cp in d["checkpoints"]:
            stage, p, order, _rung = _served_belief_vector(conn, d["station_id"], d["event_date"], cp["at"], cache)
            if p is None:
                continue
            ords = {b: i for i, b in enumerate(order)} if isinstance(order, list) else dict(order)
            ours_skew = int(mean_mode_skew_reading_v1(p, ords).get("sign") or 0)
            for off, c in cp["cand"].items():
                if c["ask"] is None or not 0.0 < c["ask"] < 1.0 or c["bucket"] not in p:
                    continue
                a = c["ask"]
                cost = a + FEE * a * (1 - a)
                rows.append({"station_id": d["station_id"], "event_date": d["event_date"], "at": cp["at"],
                             "hour": cp["local_hour"], "off": int(off), "stage": stage, "ours": p[c["bucket"]],
                             "ask": a, "cost": cost, "hit": c["hit"], "pnl": c["hit"] - cost,
                             "edge": p[c["bucket"]] - cost, "skew_differs": ours_skew != cp["skew"]})
    with gzip.open(HERE / "t2_rows.jsonl.gz", "wt", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def first_trades(e, differs=False, allowed_days=None):
        """One trade a station-day: the first checkpoint (in time) where some
        candidate's edge clears e; the largest edge among them."""

        by = defaultdict(list)
        for r in rows:
            by[(r["station_id"], r["event_date"])].append(r)
        out = []
        for (_s, day), rs in by.items():
            if allowed_days is not None and day not in allowed_days:
                continue
            for at in sorted({r["at"] for r in rs}):
                ok = [r for r in rs if r["at"] == at and r["edge"] >= e and (not differs or r["skew_differs"])]
                if ok:
                    out.append(max(ok, key=lambda r: r["edge"]))
                    break
        return out

    print(f"检查点×候选桶 {len(rows)} 行")
    for e in EDGES:
        t = first_trades(e)
        print(f"\n## 我方概率 − 买价 − 费 ≥ {e:.2f}\n  全部  {roi(t)}")
        for off, w in ((-1, "众数−1"), (0, "众数"), (1, "众数+1")):
            print(f"    {w}  {roi([r for r in t if r['off'] == off])}")
        for st in ("P0", "P0-M", "P1"):
            print(f"    阶段 {st}  {roi([r for r in t if r['stage'] == st])}")
        print(f"  且我方偏态≠市场偏态  {roi(first_trades(e, differs=True))}")

    # Walk-forward choice of e (PROTOCOL 二).
    all_days = sorted({r["event_date"] for r in rows})
    per_e = {e: first_trades(e) for e in EDGES}
    chosen = []
    for i, d in enumerate(all_days):
        if i < WARMUP:
            continue
        cutoff = (date.fromisoformat(d) - timedelta(days=2)).isoformat()
        score = {}
        for e, t in per_e.items():
            past = [r for r in t if r["event_date"] <= cutoff]
            cost = sum(r["cost"] for r in past)
            score[e] = sum(r["pnl"] for r in past) / cost if cost else -1
        best = max(score, key=score.get)
        chosen += [r for r in per_e[best] if r["event_date"] == d]
    print(f"\n## 前推选 e（前 {WARMUP} 个事件日只积累）\n  {roi(chosen)}")


if __name__ == "__main__":
    main()
