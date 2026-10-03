#!/usr/bin/env python3
"""PROTOCOL_7.md: resting at the bid -- how much of it fills, and which part.
Reads ``data8.json`` (live legs) and the probe extract in the scratch
directory; writes ``results8.json``."""

from __future__ import annotations

import json
import os
import random
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
for p in (ROOT, ROOT / "scripts", HERE):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from analyze import boot_contrast, boot_days, pct, pts  # noqa: E402

SCRATCH = Path(os.environ.get("TIEBREAK_SCRATCH", "/tmp"))
DRAWS = 4000
SEED = 164


def price_band(price):
    for low, high in ((None, 0.20), (0.20, 0.40), (0.40, 0.60), (0.60, None)):
        if (low is None or price >= low) and (high is None or price < high):
            return f"挂价 {'<' + str(high) if low is None else ('≥' + str(low) if high is None else f'{low}–{high}')}"
    return "挂价 ?"


def live(data):
    groups = defaultdict(list)
    for leg in data["legs"]:
        if leg.get("execution_candidate") == "TAKER_FOK" or "POST_ACK" not in leg["events"]:
            continue
        if leg["settled_bucket"] is None or leg["limit_price"] is None:
            continue
        groups[(leg["lane"], leg["station_id"], leg["event_date"], leg["bucket"])].append(leg)
    rows = []
    for (lane, station, day, bucket), members in groups.items():
        members.sort(key=lambda m: str(m["created_at"]))
        shares = sum(m["filled_size"] for m in members)
        first = members[0]
        book = next((m["book"] for m in members if m.get("book")), None)
        price = (sum(m["filled_notional_usdc"] for m in members) / shares) if shares > 0 else float(first["limit_price"])
        rows.append({
            "lane": lane, "station_id": station, "event_date": day, "bucket": bucket,
            "filled": shares > 0, "hit": int(first["settled_bucket"] == bucket),
            "price": price, "first_limit": float(first["limit_price"]), "posts": len(members),
            "is_market_mode": None if not book else bool(book["is_market_mode"]),
            "ticks_over_bid": (
                None if not book or book.get("best_bid") is None
                else float(first["limit_price"]) - float(book["best_bid"])
            ),
        })
    return rows


def edge(group):
    return st.mean(r["hit"] - r["price"] for r in group) if group else None


def hit_rate(group):
    return (sum(r["hit"] for r in group) / len(group)) if group else None


def block(rows):
    got = [r for r in rows if r["filled"]]
    missed = [r for r in rows if not r["filled"]]

    def side(g):
        ci = boot_days(g, edge) if len(g) >= 5 else None
        return {"n": len(g), "event_days": len({r["event_date"] for r in g}),
                "hit_rate": hit_rate(g), "avg_price": st.mean(r["price"] for r in g) if g else None,
                "edge": edge(g), "edge_ci90": ci}

    return {
        "posted": len(rows), "fill_rate": (len(got) / len(rows)) if rows else None,
        "filled": side(got), "unfilled": side(missed),
        "hit_rate_gap": (None if not (got and missed) else hit_rate(got) - hit_rate(missed)),
        "hit_rate_gap_ci90": boot_contrast(got, missed, hit_rate) if (got and missed) else None,
    }


def show_block(label, b):
    f, u = b["filled"], b["unfilled"]
    fc, gc = f["edge_ci90"], b["hit_rate_gap_ci90"]
    print(f"  {label:<18} 挂过 {b['posted']:4d} 成交率 {pct(b['fill_rate'])}"
          f" | 成交 n={f['n']:3d} 中 {pct(f['hit_rate'])} 价 {(f['avg_price'] or 0):.3f} 每份 {pts(f['edge'])}点"
          f" [{pts(fc[0]) if fc else 'n/a'},{pts(fc[1]) if fc else 'n/a'}]"
          f" | 没成交 n={u['n']:3d} 中 {pct(u['hit_rate'])} 挂价 {(u['avg_price'] or 0):.3f} 若成交 {pts(u['edge'])}点"
          f" | 命中差 {pts(b['hit_rate_gap'])} [{pts(gc[0]) if gc else 'n/a'},{pts(gc[1]) if gc else 'n/a'}]")


def probe_block(rows):
    """Per-day sums, so the day-clustered bootstrap resamples 43 numbers and
    not a hundred thousand rows."""

    days = defaultdict(lambda: defaultdict(float))
    for r in rows:
        d = days[r["event_date"]]
        d["n"] += 1
        d["fills"] += r["filled"]
        d["fill_edge"] += r["filled"] * (r["hit"] - r["limit_price"])
        d["fill_hit"] += r["filled"] * r["hit"]
        d["fill_limit"] += r["filled"] * r["limit_price"]
        d["miss_hit"] += (1 - r["filled"]) * r["hit"]
        d["miss_limit"] += (1 - r["filled"]) * r["limit_price"]
        d["all_edge"] += r["hit"] - r["limit_price"]
    keys = sorted(days)

    def stats(picked):
        t = defaultdict(float)
        for k in picked:
            for name, value in days[k].items():
                t[name] += value
        misses = t["n"] - t["fills"]
        return {
            "fill_rate": t["fills"] / t["n"] if t["n"] else None,
            "filled_edge": t["fill_edge"] / t["fills"] if t["fills"] else None,
            "filled_hit": t["fill_hit"] / t["fills"] if t["fills"] else None,
            "filled_limit": t["fill_limit"] / t["fills"] if t["fills"] else None,
            "unfilled_hit": t["miss_hit"] / misses if misses else None,
            "unfilled_limit": t["miss_limit"] / misses if misses else None,
            "unfilled_edge": (t["miss_hit"] - t["miss_limit"]) / misses if misses else None,
            "per_posted_order": t["fill_edge"] / t["n"] if t["n"] else None,
            "if_all_filled": t["all_edge"] / t["n"] if t["n"] else None,
        }

    point = stats(keys)
    out = {"orders": len(rows), "event_days": len(keys),
           "station_days": len({(r["station_id"], r["event_date"]) for r in rows}), **point}
    if len(keys) >= 3:
        rng = random.Random(SEED)
        draws = [stats([rng.choice(keys) for _ in keys]) for _ in range(DRAWS)]
        for name in ("filled_edge", "per_posted_order", "if_all_filled", "fill_rate"):
            values = sorted(d[name] for d in draws if d[name] is not None)
            if values:
                out[name + "_ci90"] = [values[int(0.05 * len(values))], values[int(0.95 * len(values))]]
    return out


def show_probe(label, b):
    fc = b.get("filled_edge_ci90")
    ac = b.get("if_all_filled_ci90")
    pc = b.get("per_posted_order_ci90")
    print(f"  {label:<22} 单 {b['orders']:6d} 日 {b['event_days']:2d} 成交率 {pct(b['fill_rate'])}"
          f" | 成交的 中 {pct(b['filled_hit'])} 价 {(b['filled_limit'] or 0):.3f} 每份 {pts(b['filled_edge'])}点"
          f" [{pts(fc[0]) if fc else 'n/a'},{pts(fc[1]) if fc else 'n/a'}]"
          f" | 没成交的 中 {pct(b['unfilled_hit'])} 价 {(b['unfilled_limit'] or 0):.3f} 若成交 {pts(b['unfilled_edge'])}点"
          f" | 全成交 {pts(b['if_all_filled'])} [{pts(ac[0]) if ac else 'n/a'},{pts(ac[1]) if ac else 'n/a'}]"
          f" | 每张挂单 {pts(b['per_posted_order'])} [{pts(pc[0]) if pc else 'n/a'},{pts(pc[1]) if pc else 'n/a'}]")


def main() -> int:
    data = json.loads((HERE / "data8.json").read_text())
    rows = live(data)
    result: dict = {"schema": "tiebreak_conditional_results8_v1", "live": {}, "probe": {}}
    print(f"== 甲 实盘真实挂单（YES 买单、有 POST_ACK、已结算；站日桶 {len(rows)} 个）")
    cuts = [("全部车道", lambda r: True)]
    cuts += [(name, (lambda n: lambda r: r["lane"] == n)(name)) for name in sorted({r["lane"] for r in rows})]
    cuts += [("是市场众数桶", lambda r: r["is_market_mode"] is True),
             ("不是市场众数桶", lambda r: r["is_market_mode"] is False),
             ("盘口未取到", lambda r: r["is_market_mode"] is None)]
    cuts += [(band, (lambda b: lambda r: price_band(r["first_limit"]) == b)(band))
             for band in ("挂价 <0.2", "挂价 0.2–0.4", "挂价 0.4–0.6", "挂价 ≥0.6")]
    for label, pred in cuts:
        g = [r for r in rows if pred(r)]
        if not g:
            continue
        b = block(g)
        result["live"][label] = b
        show_block(label, b)
    over = [r["ticks_over_bid"] for r in rows if r["ticks_over_bid"] is not None]
    if over:
        result["live_limit_minus_bid"] = {"n": len(over), "median": st.median(over), "mean": st.mean(over)}
        print(f"  首挂价 − 下单时买一：中位 {st.median(over):+.3f}，均值 {st.mean(over):+.3f}（{len(over)} 个）")

    probe = json.loads((SCRATCH / "probe_mode_rows.json").read_text())
    print(f"\n== 乙 探针（事前写下的假想挂单，60 分钟内卖一落到挂价才算成交；"
          f"{probe['placements']} 个下单时刻，全部桶成交率 {probe['all_buckets']}）")
    for rule in ("bid", "bid+1t"):
        mine = [r for r in probe["rows"] if r["rule"] == rule]
        label = "挂买一" if rule == "bid" else "挂买一+1跳"
        result["probe"][rule] = {}
        for name, pred in (
            ("市场众数桶·全部", lambda r: True),
            ("中价 <0.40", lambda r: r["mid"] < 0.40),
            ("中价 0.40–0.60", lambda r: 0.40 <= r["mid"] < 0.60),
            ("中价 ≥0.60", lambda r: r["mid"] >= 0.60),
            ("距事件日 ≥12h", lambda r: r["lead_hours"] >= 12),
            ("距事件日 <12h", lambda r: r["lead_hours"] < 12),
        ):
            g = [r for r in mine if pred(r)]
            if not g:
                continue
            b = probe_block(g)
            result["probe"][rule][name] = b
            show_probe(f"{label} {name}", b)
    (HERE / "results8.json").write_text(json.dumps(result, ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
