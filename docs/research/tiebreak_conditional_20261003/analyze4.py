#!/usr/bin/env python3
"""PROTOCOL_3.md: the tiebreak lane buying the market's mean bucket.
Reads ``data.json``, ``data3.json`` and ``data4.json``; writes
``results4.json``."""

from __future__ import annotations

import json
import statistics as st
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
for p in (ROOT, ROOT / "scripts", HERE):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from bayes_basket_target import tiebreak_cell_shadow as shadow  # noqa: E402
from analyze import boot_contrast, boot_days, pct, pts  # noqa: E402

DISCOVERY_CEILING = "2026-09-20"


def roi(rows):
    return shadow._roi(rows)


def net_roi(rows):
    """After the taker fee 0.05 * p * (1 - p) a share."""

    values = [
        (r["hit"] - r["best_ask"] - 0.05 * r["best_ask"] * (1.0 - r["best_ask"])) / r["best_ask"]
        for r in rows if r.get("best_ask")
    ]
    return st.mean(values) if values else None


def windows(rows):
    return (
        ("发现期", [r for r in rows if r["event_date"] <= DISCOVERY_CEILING]),
        ("验证期", [r for r in rows if r["event_date"] > DISCOVERY_CEILING]),
        ("全期", list(rows)),
    )


def summary(rows):
    out = {}
    for name, pool in windows(rows):
        ci = boot_days(pool, roi) if len(pool) >= 5 else None
        net_ci = boot_days(pool, net_roi) if len(pool) >= 5 else None
        out[name] = {
            "n": len(pool), "event_days": len({r["event_date"] for r in pool}),
            "hit_rate": (sum(r["hit"] for r in pool) / len(pool)) if pool else None,
            "avg_ask": st.mean(r["best_ask"] for r in pool) if pool else None,
            "roi": roi(pool), "ci90": ci, "net_roi": net_roi(pool), "net_ci90": net_ci,
        }
    return out


def show(label, s):
    print(f"  {label}")
    for name, x in s.items():
        ci, nci = x["ci90"], x["net_ci90"]
        print(f"     {name} n={x['n']:4d} 日={x['event_days']:2d} 中 {pct(x['hit_rate'])}@"
              f"{(x['avg_ask'] or 0):.3f} ROI {pct(x['roi'])}"
              f" [{pct(ci[0]) if ci else 'n/a'},{pct(ci[1]) if ci else 'n/a'}]"
              f" 扣费后 {pct(x['net_roi'])} [{pct(nci[0]) if nci else 'n/a'},{pct(nci[1]) if nci else 'n/a'}]")


def contrast(a_rows, b_rows):
    out = {}
    for (name, a), (_n, b) in zip(windows(a_rows), windows(b_rows)):
        point = None if (roi(a) is None or roi(b) is None) else roi(a) - roi(b)
        out[name] = {"point": point, "ci90": boot_contrast(a, b, roi) if (a and b) else None}
    return out


def show_contrast(label, c):
    print(f"  {label}: " + "；".join(
        f"{name} {pts(x['point'])} [{pts(x['ci90'][0]) if x['ci90'] else 'n/a'},"
        f"{pts(x['ci90'][1]) if x['ci90'] else 'n/a'}]" for name, x in c.items()))


def main() -> int:
    data = json.loads((HERE / "data.json").read_text())
    centres = json.loads((HERE / "data3.json").read_text())
    firsts = json.loads((HERE / "data4.json").read_text())["first_decisions"]
    result: dict = {"schema": "tiebreak_conditional_results4_v1"}

    current = [dict(r) for r in data["replay"]]
    current_grain = shadow.one_per_station_day(current)

    # -- A: the same trigger, the market's mean bucket bought ----------------
    a_rows, swapped, unquoted = [], 0, 0
    for row in current:
        c = centres["replay_centres"]["|".join((row["station_id"], row["event_date"], row["stage"]))]
        target = c["market_mean"]
        if target["ordinal"] == c["model_mode"]["ordinal"]:
            a_rows.append(dict(row))
            continue
        if not target["best_ask"]:
            unquoted += 1
            continue
        swapped += 1
        a_rows.append(dict(row, bucket=target["bucket"], hit=target["hit"], best_ask=target["best_ask"]))
    a_grain = shadow.one_per_station_day(a_rows)
    result["A"] = {
        "rows": len(a_rows), "swapped": swapped, "unquoted_skipped": unquoted,
        "firings": summary(a_rows), "current_firings": summary(current),
        "grain": summary(a_grain), "current_grain": summary(current_grain),
        "a_minus_current_firings": contrast(a_rows, current),
    }
    print(f"== 改法 A：触发不变，改买市场均值桶（{len(current)} 个触发行里换桶 {swapped} 行，"
          f"市场均值桶无卖一而不下单 {unquoted} 行）")
    show("现行（买模型众数桶），每个触发行", result["A"]["current_firings"])
    show("改法 A，每个触发行", result["A"]["firings"])
    show_contrast("A − 现行", result["A"]["a_minus_current_firings"])

    # -- B: no model, the market's mean bucket on its own ---------------------
    def fired_rows(name, keep=lambda e: True):
        return [
            {"station_id": f["station_id"], "event_date": f["event_date"], "stage": f["stage"],
             "decision_at_utc": f["decision_at_utc"], "hit": f[name]["hit"],
             "best_ask": f[name]["best_ask"], "is_model_mode": f[name]["is_model_mode"]}
            for f in firsts if f[name]["fired"] and f[name]["best_ask"] and keep(f[name])
        ]

    b_rows = fired_rows("market_mean")
    b_grain = shadow.one_per_station_day(b_rows)
    overlap = [r for r in b_rows if r["is_model_mode"]]
    market_only = [r for r in b_rows if not r["is_model_mode"]]
    mode_rows = fired_rows("market_mode")
    result["B"] = {
        "first_decisions_read": len(firsts),
        "firings": summary(b_rows), "grain": summary(b_grain),
        "overlap_with_model_mode": summary(overlap), "market_only": summary(market_only),
        "reference_market_mode_firings": summary(mode_rows),
        "b_minus_current_firings": contrast(b_rows, current),
        "overlap_minus_market_only": contrast(overlap, market_only),
    }
    print(f"\n== 改法 B：不看模型，市场均值桶自己在 0.40–0.60 且点差 ≤0.031 就买"
          f"（读到 {len(firsts)} 个首决策）")
    show("改法 B，每个触发行", result["B"]["firings"])
    show("改法 B，站日最早一条", result["B"]["grain"])
    show("  其中：市场均值桶＝模型众数桶（现行规则也会买）", result["B"]["overlap_with_model_mode"])
    show("  其中：市场均值桶≠模型众数桶（只有 B 会买）", result["B"]["market_only"])
    show("参照：同口径换成市场众数桶", result["B"]["reference_market_mode_firings"])
    show_contrast("B − 现行（各自触发行）", result["B"]["b_minus_current_firings"])
    show_contrast("B 内部：模型同意的行 − 模型不同意的行", result["B"]["overlap_minus_market_only"])

    # -- the live trades where the two buckets differed -----------------------
    live = []
    for t in data["live"]["trades"]["rows"]:
        c = centres["live_centres"].get(str(t["order_intent_id"]))
        if c is None or t["hit"] is None:
            continue
        if c["market_mean"]["bucket"] != t["bucket"]:
            live.append({
                "event_date": t["event_date"], "station_id": t["station_id"],
                "bought": t["bucket"], "paid": t["ask_paid"], "hit": bool(t["hit"]),
                "pnl_usdc": t["pnl_usdc"], "market_mean": c["market_mean"]["bucket"],
                "market_mean_ask": c["market_mean"]["best_ask"],
                "market_mean_hit": c["market_mean"]["hit"], "settled": t["settled_bucket"],
            })
    result["live_differing"] = live
    print(f"\n== 实盘：市场均值桶与买入桶不同的 {len(live)} 笔")
    for x in live:
        print(f"   {x['event_date'][5:]} {x['station_id']} 买 {x['bought']}@{x['paid']:.2f}"
              f" {'中' if x['hit'] else '空'}({x['pnl_usdc']:+.2f}U) | 市场均值桶 {x['market_mean']}"
              f"@{x['market_mean_ask']} {'中' if x['market_mean_hit'] else '空'} | 真值 {x['settled']}")
    (HERE / "results4.json").write_text(json.dumps(result, ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
