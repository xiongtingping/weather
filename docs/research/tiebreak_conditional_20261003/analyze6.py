#!/usr/bin/env python3
"""PROTOCOL_5.md: the market's own mode, mean and runner-up buckets -- is
any of them mispriced on a condition the market's ladder itself shows.
Reads ``data6.json``; writes ``results6.json``."""

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
from analyze4 import net_roi, roi  # noqa: E402

CUT = "2026-09-20"
CEILING = "2026-10-02"
MIN_HALF = 30


def width(row):
    return 1.0 if row["unit"] == "C" else 10.0 / 9.0


def band(value, cuts):
    low = None
    for cut in cuts:
        if value < cut:
            return (low, cut)
        low = cut
    return (low, None)


def band_label(name, bounds, unit=""):
    low, high = bounds
    if low is None:
        return f"{name} <{high}{unit}"
    if high is None:
        return f"{name} ≥{low}{unit}"
    return f"{name} {low}–{high}{unit}"


def arms():
    """(label, target centre, predicate) -- declared in PROTOCOL_5.md."""

    out = []
    for bounds in ((None, 0.30), (0.30, 0.40), (0.40, 0.50), (0.50, 0.60), (0.60, 0.70), (0.70, None)):
        out.append((band_label("M1 众数桶 份额", bounds), "mode",
                    (lambda b: lambda r: band(r["mode"]["share"], (0.30, 0.40, 0.50, 0.60, 0.70)) == b)(bounds)))
    side = lambda r: r["mean"]["ordinal"] - r["mode"]["ordinal"]  # noqa: E731
    out += [
        ("M2 众数桶｜均值桶同桶", "mode", lambda r: side(r) == 0),
        ("M2 众数桶｜均值桶在冷侧", "mode", lambda r: side(r) < 0),
        ("M2 众数桶｜均值桶在暖侧", "mode", lambda r: side(r) > 0),
        ("M3 均值桶（≠众数桶）", "mean", lambda r: side(r) != 0),
    ]
    lean = lambda r: abs(r["market_mu"] - r["mode"]["ordinal"])  # noqa: E731
    for bounds in ((None, 0.15), (0.15, 0.35), (0.35, None)):
        out.append((band_label("M4 众数桶 |b|", bounds, "档"), "mode",
                    (lambda b: lambda r: band(lean(r), (0.15, 0.35)) == b)(bounds)))
    gap = lambda r: r["mode"]["share"] - (r["second"]["share"] if r["second"] else 0.0)  # noqa: E731
    for bounds in ((None, 0.10), (0.10, 0.25), (0.25, None)):
        out.append((band_label("M5 众数桶 领先第二名", bounds), "mode",
                    (lambda b: lambda r: band(gap(r), (0.10, 0.25)) == b)(bounds)))
    out.append(("M5 第二名（领先 <0.10）", "second", lambda r: r["second"] is not None and gap(r) < 0.10))
    sigma = lambda r: r["sigma_buckets"] * width(r)  # noqa: E731
    for bounds in ((None, 0.9), (0.9, 1.3), (1.3, None)):
        out.append((band_label("M6 众数桶 市场σ", bounds, "°C"), "mode",
                    (lambda b: lambda r: band(sigma(r), (0.9, 1.3)) == b)(bounds)))
    for stage in ("P0", "P0-M", "P1"):
        out.append((f"M7 众数桶 阶段 {stage}", "mode", (lambda s: lambda r: r["stage"] == s)(stage)))
    for bounds in ((None, 12), (12, 24), (24, 36), (36, 48), (48, None)):
        out.append((band_label("M7 众数桶 距结算", bounds, "h"), "mode",
                    (lambda b: lambda r: band(r["tau"], (12, 24, 36, 48)) == b)(bounds)))
    out += [("M8 众数桶 °C 站", "mode", lambda r: r["unit"] == "C"),
            ("M8 众数桶 °F 站", "mode", lambda r: r["unit"] == "F")]
    return out


def trades(rows, target, pred, side):
    """The purchase as replay rows: YES at the ask, NO at one minus the bid."""

    out = []
    for r in rows:
        c = r.get(target)
        if c is None or not pred(r):
            continue
        if side == "YES":
            if not c["best_ask"]:
                continue
            out.append({"event_date": r["event_date"], "hit": c["hit"], "best_ask": c["best_ask"]})
        else:
            if not c["best_bid"]:
                continue
            out.append({"event_date": r["event_date"], "hit": 1 - c["hit"], "best_ask": 1.0 - c["best_bid"]})
    return out


def main() -> int:
    rows = [r for r in json.loads((HERE / "data6.json").read_text())["rows"]
            if r["event_date"] <= CEILING]
    disc = [r for r in rows if r["event_date"] <= CUT]
    val = [r for r in rows if r["event_date"] > CUT]
    days = sorted({r["event_date"] for r in disc})
    split = days[len(days) // 2]
    result: dict = {"schema": "tiebreak_conditional_results6_v1", "rows": len(rows),
                    "discovery": len(disc), "validation": len(val), "discovery_split": split,
                    "calibration": [], "arms": []}
    print(f"首决策 {len(rows)}：发现期 {len(disc)} / 验证期 {len(val)}；发现期两半分界 {split}")

    print("\n== 校准表：市场众数桶，按归一化份额分带（全期）")
    print("   带         行数  份额   买一   卖一   命中   命中−卖一  买一−命中 | 发现期命中/卖一  验证期命中/卖一")
    for bounds in ((None, 0.30), (0.30, 0.40), (0.40, 0.50), (0.50, 0.60), (0.60, 0.70), (0.70, None)):
        keep = lambda r: band(r["mode"]["share"], (0.30, 0.40, 0.50, 0.60, 0.70)) == bounds  # noqa: E731
        g = [r for r in rows if keep(r) and r["mode"]["best_ask"] and r["mode"]["best_bid"]]
        if not g:
            continue
        m = lambda key, pool=g: st.mean(r["mode"][key] for r in pool)  # noqa: E731
        a = [r for r in g if r["event_date"] <= CUT]
        b = [r for r in g if r["event_date"] > CUT]
        entry = {"band": band_label("", bounds).strip(), "n": len(g), "share": m("share"),
                 "bid": m("best_bid"), "ask": m("best_ask"), "hit": m("hit"),
                 "hit_minus_ask": m("hit") - m("best_ask"), "bid_minus_hit": m("best_bid") - m("hit"),
                 "discovery": {"n": len(a), "hit": m("hit", a) if a else None, "ask": m("best_ask", a) if a else None},
                 "validation": {"n": len(b), "hit": m("hit", b) if b else None, "ask": m("best_ask", b) if b else None}}
        result["calibration"].append(entry)
        da, vb = entry["discovery"], entry["validation"]
        print(f"   {entry['band']:<10} {len(g):4d}  {entry['share']:.3f}  {entry['bid']:.3f}  {entry['ask']:.3f}"
              f"  {entry['hit']:.3f}   {entry['hit_minus_ask']:+.3f}     {entry['bid_minus_hit']:+.3f}"
              f"  | {(da['hit'] or 0):.3f}/{(da['ask'] or 0):.3f} (n={da['n']})"
              f"  {(vb['hit'] or 0):.3f}/{(vb['ask'] or 0):.3f} (n={vb['n']})")

    print("\n== 各臂（扣吃单手续费后 ROI）  发现期 前半/后半/合并 → 验证期")
    selected = []
    for label, target, pred in arms():
        for side in ("YES", "NO"):
            d = trades(disc, target, pred, side)
            v = trades(val, target, pred, side)
            a = [t for t in d if t["event_date"] < split]
            b = [t for t in d if t["event_date"] >= split]
            entry = {
                "arm": label, "side": side, "n_discovery": len(d), "n_validation": len(v),
                "net_first": net_roi(a), "net_second": net_roi(b), "net_discovery": net_roi(d),
                "gross_discovery": roi(d), "net_validation": net_roi(v), "gross_validation": roi(v),
                "net_validation_ci90": boot_days(v, net_roi) if len(v) >= 5 else None,
                "hit_discovery": (sum(t["hit"] for t in d) / len(d)) if d else None,
                "price_discovery": st.mean(t["best_ask"] for t in d) if d else None,
                "hit_validation": (sum(t["hit"] for t in v) / len(v)) if v else None,
                "price_validation": st.mean(t["best_ask"] for t in v) if v else None,
            }
            entry["selected"] = bool(
                len(a) >= MIN_HALF and len(b) >= MIN_HALF
                and entry["net_first"] > 0 and entry["net_second"] > 0 and entry["net_discovery"] >= 0.03
            )
            result["arms"].append(entry)
            if entry["selected"]:
                selected.append((entry, v))
            ci = entry["net_validation_ci90"]
            print(f"  {label:<24} {side:<3} 发现 n={len(d):4d} {pct(entry['net_first'])}/{pct(entry['net_second'])}"
                  f"/{pct(entry['net_discovery'])} → 验证 n={len(v):4d} {pct(entry['net_validation'])}"
                  f" [{pct(ci[0]) if ci else 'n/a'},{pct(ci[1]) if ci else 'n/a'}]"
                  f" {'★入选' if entry['selected'] else ''}")

    k = len(selected)
    result["k"] = k
    print(f"\n== 入选 {k} 个臂" + ("：" if k else "。"))
    for entry, v in selected:
        lower = boot_days(v, net_roi, quantiles=(0.05 / k,))
        entry["lower_bound_at_0.05_over_k"] = lower[0] if lower else None
        entry["found"] = bool(lower and lower[0] > 0)
        print(f"   {entry['arm']} {entry['side']}: 发现期 {pct(entry['net_discovery'])}（中"
              f" {pct(entry['hit_discovery'])}@{entry['price_discovery']:.3f}）→ 验证期"
              f" {pct(entry['net_validation'])}（中 {pct(entry['hit_validation'])}@{entry['price_validation']:.3f}，"
              f"n={entry['n_validation']}），0.05/{k} 下界 {pct(entry['lower_bound_at_0.05_over_k'])}"
              f" → {'有' if entry['found'] else '不过'}")
    (HERE / "results6.json").write_text(json.dumps(result, ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
