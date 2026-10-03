#!/usr/bin/env python3
"""PROTOCOL_6.md: the four centres together -- which pattern of model
mode / model mean / market mode / market mean makes which bucket worth its
ask.  Reads ``data7.json``; writes ``results7.json``."""

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

from analyze import boot_days, pct  # noqa: E402
from analyze4 import net_roi  # noqa: E402

CUT = "2026-09-20"
MIN_HALF = 30
CENTRES = (("model_mode", "模型众数桶"), ("model_mean", "模型均值桶"),
           ("market_mode", "市场众数桶"), ("market_mean", "市场均值桶"))


def pattern(r) -> str:
    mo, me = r["model_mode"]["ordinal"], r["model_mean"]["ordinal"]
    ko, ke = r["market_mode"]["ordinal"], r["market_mean"]["ordinal"]
    if mo == ko:
        if ke != ko:
            return "C 两众数同桶·市场均值桶偏出"
        return "A 四心同桶" if me == mo else "B 两众数同桶·仅模型均值桶偏出"
    if abs(mo - ko) >= 2:
        return "F 两众数相隔≥2档"
    if ke == mo:
        return "D 两众数相邻·市场均值桶在模型众数桶"
    if ke == ko:
        return "E 两众数相邻·市场均值桶在市场众数桶"
    return "E′ 两众数相邻·市场均值桶两者都不是"


PATTERNS = ("A 四心同桶", "B 两众数同桶·仅模型均值桶偏出", "C 两众数同桶·市场均值桶偏出",
            "D 两众数相邻·市场均值桶在模型众数桶", "E 两众数相邻·市场均值桶在市场众数桶",
            "E′ 两众数相邻·市场均值桶两者都不是", "F 两众数相隔≥2档")
GAPS = ((None, 0.25), (0.25, 0.5), (0.5, 1.0), (1.0, None))


def gap_band(r):
    g = abs(r["model_mu"] - r["market_mu"])
    for low, high in GAPS:
        if (low is None or g >= low) and (high is None or g < high):
            return (low, high)
    return GAPS[-1]


def gap_label(bounds):
    low, high = bounds
    return f"两中心相距 <{high}档" if low is None else (
        f"两中心相距 ≥{low}档" if high is None else f"两中心相距 {low}–{high}档")


def trades(rows, centre, side="YES"):
    out = []
    for r in rows:
        c = r[centre]
        if side == "YES":
            if c["best_ask"]:
                out.append({"event_date": r["event_date"], "hit": c["hit"], "best_ask": c["best_ask"]})
        elif c["best_bid"]:
            out.append({"event_date": r["event_date"], "hit": 1 - c["hit"], "best_ask": 1.0 - c["best_bid"]})
    return out


def main() -> int:
    rows = json.loads((HERE / "data7.json").read_text())["rows"]
    for r in rows:
        r["pattern"] = pattern(r)
    disc = [r for r in rows if r["event_date"] <= CUT]
    val = [r for r in rows if r["event_date"] > CUT]
    days = sorted({r["event_date"] for r in disc})
    split = days[len(days) // 2]
    result: dict = {"schema": "tiebreak_conditional_results7_v1", "rows": len(rows),
                    "discovery_split": split, "patterns": [], "arms": []}
    print(f"首决策 {len(rows)}：发现期 {len(disc)} / 验证期 {len(val)}")

    print("\n== 形态表：各形态下四个桶的 命中率 / 买一 / 卖一（两侧都有报价的行；前段 | 近段）")
    for name in PATTERNS:
        entry = {"pattern": name, "windows": {}}
        line = []
        for label, pool in (("前段", disc), ("近段", val)):
            g = [r for r in pool if r["pattern"] == name]
            cell = {"n": len(g), "share_of_rows": len(g) / len(pool)}
            parts = []
            for key, short in CENTRES:
                # Hit, bid and ask over the SAME rows: the ones quoted on both
                # sides.  A bucket with no ask is a near-certain favourite, and
                # counting its hit without its price reads as an edge.
                quoted = [r for r in g if r[key]["best_ask"] and r[key]["best_bid"]]
                hit = st.mean(r[key]["hit"] for r in quoted) if quoted else None
                ask = st.mean(r[key]["best_ask"] for r in quoted) if quoted else None
                bid = st.mean(r[key]["best_bid"] for r in quoted) if quoted else None
                cell[key] = {"n_quoted": len(quoted), "hit": hit, "ask": ask, "bid": bid,
                             "model_probability": st.mean(r[key]["model_probability"] for r in quoted) if quoted else None}
                parts.append(f"{short[:4]} 中{0 if hit is None else hit:.3f} 买一{0 if bid is None else bid:.3f}"
                             f" 卖一{0 if ask is None else ask:.3f}")
            entry["windows"][label] = cell
            line.append(f"{label} n={len(g):4d}（{cell['share_of_rows']:.0%}） " + "  ".join(parts))
        result["patterns"].append(entry)
        print(f"  {name}\n     " + "\n     ".join(x.replace("  模型", "\n        模型").replace("  市场", "\n        市场") for x in line))

    def judge(label, pool_pred, centre, side):
        d = trades([r for r in disc if pool_pred(r)], centre, side)
        v = trades([r for r in val if pool_pred(r)], centre, side)
        a = [t for t in d if t["event_date"] < split]
        b = [t for t in d if t["event_date"] >= split]
        entry = {
            "arm": label, "side": side, "n_discovery": len(d), "n_validation": len(v),
            "net_first": net_roi(a), "net_second": net_roi(b), "net_discovery": net_roi(d),
            "net_validation": net_roi(v),
            "net_validation_ci90": boot_days(v, net_roi) if len(v) >= 5 else None,
            "hit_discovery": (sum(t["hit"] for t in d) / len(d)) if d else None,
            "price_discovery": st.mean(t["best_ask"] for t in d) if d else None,
            "hit_validation": (sum(t["hit"] for t in v) / len(v)) if v else None,
            "price_validation": st.mean(t["best_ask"] for t in v) if v else None,
        }
        entry["selected"] = bool(
            len(a) >= MIN_HALF and len(b) >= MIN_HALF and entry["net_first"] > 0
            and entry["net_second"] > 0 and entry["net_discovery"] >= 0.03
        )
        result["arms"].append(entry)
        ci = entry["net_validation_ci90"]
        print(f"  {label:<34} {side:<3} 发现 n={len(d):4d} {pct(entry['net_first'])}/{pct(entry['net_second'])}"
              f"/{pct(entry['net_discovery'])} → 验证 n={len(v):4d} {pct(entry['net_validation'])}"
              f" [{pct(ci[0]) if ci else 'n/a'},{pct(ci[1]) if ci else 'n/a'}]"
              f" {'★入选' if entry['selected'] else ''}")
        return entry, v

    print("\n== 各臂（扣吃单手续费后 ROI）  发现期 前半/后半/合并 → 验证期")
    selected = []
    for name in PATTERNS:
        for key, short in CENTRES:
            entry, v = judge(f"{name[:2]}买{short}", (lambda n: lambda r: r["pattern"] == n)(name), key, "YES")
            if entry["selected"]:
                selected.append((entry, v))
    for bounds in GAPS:
        for key, short in (("market_mode", "市场众数桶"), ("model_mode", "模型众数桶")):
            entry, v = judge(f"{gap_label(bounds)} 买{short}", (lambda b: lambda r: gap_band(r) == b)(bounds), key, "YES")
            if entry["selected"]:
                selected.append((entry, v))
    for name in (PATTERNS[3], PATTERNS[4], PATTERNS[6]):
        entry, v = judge(f"{name[:2]}做空模型众数桶", (lambda n: lambda r: r["pattern"] == n)(name), "model_mode", "NO")
        if entry["selected"]:
            selected.append((entry, v))

    k = len(selected)
    result["k"] = k
    print(f"\n== 入选 {k} 个臂" + ("：" if k else "。"))
    for entry, v in selected:
        lower = boot_days(v, net_roi, quantiles=(0.05 / k,))
        entry["lower_bound_at_0.05_over_k"] = lower[0] if lower else None
        entry["found"] = bool(lower and lower[0] > 0)
        print(f"   {entry['arm']} {entry['side']}: 发现期 {pct(entry['net_discovery'])}（中"
              f" {pct(entry['hit_discovery'])}@{entry['price_discovery']:.3f}，n={entry['n_discovery']}）→ 验证期"
              f" {pct(entry['net_validation'])}（中 {pct(entry['hit_validation'])}@{entry['price_validation']:.3f}，"
              f"n={entry['n_validation']}），0.05/{k} 下界 {pct(entry['lower_bound_at_0.05_over_k'])}"
              f" → {'有' if entry['found'] else '不过'}")
    (HERE / "results7.json").write_text(json.dumps(result, ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
