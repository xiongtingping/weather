#!/usr/bin/env python3
"""PROTOCOL.md, the amendment: S1-S4.

    python3 supplement.py
"""
import json
import os
import pickle
import sys
from datetime import datetime, timedelta

import numpy as np
from scipy.stats import spearmanr

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
ACCEPT = os.path.join(ROOT, "docs", "research", "final_vector_acceptance_20261004")
PEAK = os.path.join(ROOT, "docs", "research", "width_map_peak_20261004")
sys.path.insert(0, ROOT)
sys.path.insert(0, ACCEPT)
sys.path.insert(0, HERE)
from bayes_basket_target.center_width_store import _proper_score_gate  # noqa: E402
import common as c  # noqa: E402
import readings as R  # noqa: E402

GRID = tuple(round(0.5 + 0.025 * i, 3) for i in range(41))
G75 = GRID.index(0.75)
MIN_PREFIX_DAYS = 8


def top_line(tops, hit, wt, codes, k):
    parts = []
    out = []
    for lo, hi in R.TOP_BINS:
        m = (tops >= lo) & (tops < hi)
        if m.sum() < 20:
            parts.append(f"{lo:.1f}-{min(hi,1):.1f}: n {int(m.sum())}")
            out.append({"bin": [lo, hi], "n": int(m.sum())})
            continue
        gap = R.boot(hit[m] - tops[m], wt[m], codes[m], k)
        parts.append(f"{lo:.1f}-{min(hi,1):.1f}: n {int(m.sum())} 说 {np.average(tops[m], weights=wt[m]):.3f} 中 {np.average(hit[m], weights=wt[m]):.3f} 差 {R.f(gap,100,1)}")
        out.append({"bin": [lo, hi], "n": int(m.sum()), "gap": gap})
    return "  ".join(parts), out


def s1(result):
    rec_all = c.load()
    days = sorted({x["d"] for x in rec_all})
    code = {d: i for i, d in enumerate(days)}
    result["S1"] = {}
    print("== S1 实服记录按时段")
    for era, lo, hi in (("09-12..09-21", "2026-09-12", "2026-09-21"), ("09-22..10-03", "2026-09-22", "2026-10-03")):
        for unit in ("C", "F"):
            rec = [x for x in rec_all if x["unit"] == unit and lo <= x["d"] <= hi]
            codes = np.array([code[x["d"]] for x in rec]); wt = np.array([x["wt"] for x in rec]); k = len(days)
            tops = np.array([float(x["p"].max()) for x in rec])
            hit = np.array([float(int(np.argmax(x["p"])) == x["y"]) for x in rec])
            mom = np.array([c.moments(x["p"]) for x in rec]); y = np.array([x["y"] for x in rec])
            line, tb = top_line(tops, hit, wt, codes, k)
            mae = R.boot(np.abs(y - mom[:, 0]), wt, codes, k)
            z2 = R.boot(((y - mom[:, 0]) ** 2 + c.WITHIN) / mom[:, 1] ** 2, wt, codes, k)
            qmom = np.array([c.moments(x["q"]) for x in rec])
            qmae = R.boot(np.abs(y - qmom[:, 0]), wt, codes, k)
            result["S1"][f"{era} {unit}"] = {"decisions": len(rec), "top": tb, "mae": mae, "z2": z2, "market_mae": qmae}
            print(f"   [{era}] [{unit} 站] 决策 {len(rec)} 站日 {len({(x['st'], x['d']) for x in rec})}\n      最高桶  {line}")
            print(f"      |误差| 模型 {mae[0]:.3f} [{mae[1]:.3f},{mae[2]:.3f}] 市场 {qmae[0]:.3f} 档   E[z²] {z2[0]:.3f} [{z2[1]:.3f},{z2[2]:.3f}]")


def load_rows():
    rows = []
    for name in sorted(os.listdir(os.path.join(PEAK, "rows"))):
        with open(os.path.join(PEAK, "rows", name), "rb") as fh:
            rows.extend(pickle.load(fh))
    return rows


def row_stats(rows):
    cells = [r["P"][G75] for r in rows]
    tops = np.array([cl[2] for cl in cells])
    hit = np.array([max((y for p, y in cl[1] if abs(p - cl[2]) < 1e-12), default=0.0) for cl in cells])
    keys = [[float(int(x)) for x in r["keys"]] for r in rows]
    mean = np.array([float(np.dot(kc, r["p75"]) / sum(r["p75"])) for kc, r in zip(keys, rows)])
    var = np.array([float(np.dot((np.array(kc) - mu) ** 2, r["p75"]) / sum(r["p75"])) + 1 / 12 for kc, r, mu in zip(keys, rows, mean)])
    truth = np.array([float(r["truth"]) for r in rows])
    return tops, hit, np.abs(truth - mean), ((truth - mean) ** 2 + 1 / 12) / var


def s2(rows, result):
    days = sorted({r["d"] for r in rows}); code = {d: i for i, d in enumerate(days)}; k = len(days)
    result["S2"] = {}
    print("\n== S2 回放按时段")
    for scope, keep in ((">36h", lambda r: r["bin"] <= 1), ("全部提前量", lambda r: True)):
        for seg, lo, hi in (("07-30..08-31", "2026-07-30", "2026-08-31"), ("09-01..10-01", "2026-09-01", "2026-10-01")):
            for group, member in (("美国 11 站", lambda r: r["st"] in R.US), ("其余 34 站", lambda r: r["st"] not in R.US)):
                sub = [r for r in rows if keep(r) and member(r) and lo <= r["d"] <= hi]
                codes = np.array([code[r["d"]] for r in sub]); wt = np.ones(len(sub))
                tops, hit, abserr, z2 = row_stats(sub)
                line, tb = top_line(tops, hit, wt, codes, k)
                mae, zz = R.boot(abserr, wt, codes, k), R.boot(z2, wt, codes, k)
                result["S2"][f"{scope} {seg} {group}"] = {"decisions": len(sub), "top": tb, "mae": mae, "z2": zz}
                print(f"   [{scope}] [{seg}] [{group}] 决策 {len(sub)}\n      最高桶  {line}\n      |误差| {mae[0]:.3f} [{mae[1]:.3f},{mae[2]:.3f}] °C   E[z²] {zz[0]:.3f} [{zz[1]:.3f},{zz[2]:.3f}]")


def s3(rows, result):
    print("\n== S3 回放逐站，两段之间是否延续（全部提前量）")
    stations = sorted({r["st"] for r in rows})
    table = {}
    for st in stations:
        row = {}
        for seg, lo, hi in (("a", "2026-07-30", "2026-08-31"), ("b", "2026-09-01", "2026-10-01")):
            sub = [r for r in rows if r["st"] == st and lo <= r["d"] <= hi]
            tops, hit, abserr, z2 = row_stats(sub)
            row[seg] = {"n": len(sub), "mae": float(abserr.mean()), "z2": float(z2.mean()),
                        "top_gap": float((hit - tops)[tops >= 0.4].mean()) if (tops >= 0.4).sum() >= 20 else float("nan")}
        table[st] = row
    result["S3"] = {"stations": table}
    for name, pick in (("全队 45 站", stations), ("美国 11 站", [s for s in stations if s in R.US])):
        for metric in ("z2", "mae", "top_gap"):
            a = np.array([table[s]["a"][metric] for s in pick]); b = np.array([table[s]["b"][metric] for s in pick])
            ok = ~(np.isnan(a) | np.isnan(b))
            rho, pval = spearmanr(a[ok], b[ok])
            result["S3"][f"{name} {metric}"] = {"rho": float(rho), "p": float(pval), "n": int(ok.sum())}
            print(f"   {name}  {metric:<7} 两段秩相关 {rho:+.2f}（p={pval:.2f}，n={int(ok.sum())}）")
    print("   美国站   站 / 前段 E[z²] |误差| 最高桶差 / 后段 E[z²] |误差| 最高桶差")
    for s in [s for s in stations if s in R.US]:
        a, b = table[s]["a"], table[s]["b"]
        print(f"     {s}  {a['z2']:.2f} {a['mae']:.2f} {a['top_gap']*100:+6.1f}   {b['z2']:.2f} {b['mae']:.2f} {b['top_gap']*100:+6.1f}")


def s4(rows, result):
    print("\n== S4 按站群定宽度（前推）")
    days = sorted({r["d"] for r in rows}); code = {d: i for i, d in enumerate(days)}; k = len(days)
    d = np.array([r["d"] for r in rows]); known = np.array([r["known"] for r in rows])
    us = np.array([r["st"] in R.US for r in rows])
    nll = np.array([[cell[0][0] for cell in r["P"]] for r in rows])
    chosen = {}
    for day in days:
        cut = (datetime.fromisoformat(day) - timedelta(days=2)).date().isoformat()
        base = (d <= cut) & (known <= day + "T00:00:00Z")
        for name, member in (("us", us), ("rest", ~us)):
            m = base & member
            chosen[(day, name)] = int(np.argmin(nll[m].mean(axis=0))) if len(set(d[m])) >= MIN_PREFIX_DAYS else G75
    late = [x for x in days if x >= "2026-09-01"]
    print("   前推 λ（后段）  美国站:", sorted({GRID[chosen[(x, 'us')]] for x in late}), "  其余:", sorted({GRID[chosen[(x, 'rest')]] for x in late}))
    print("   样本内 LogScore 最小处  美国站:", GRID[int(np.argmin(nll[us].mean(0)))], "  其余:", GRID[int(np.argmin(nll[~us].mean(0)))],
          f"  美国站 0.75 对最小处 {nll[us].mean(0)[G75] - nll[us].mean(0).min():+.5f}  其余 {nll[~us].mean(0)[G75] - nll[~us].mean(0).min():+.5f}")
    base_cells = [r["P"][G75] for r in rows]
    arm_cells = [r["P"][chosen[(r["d"], "us" if r["st"] in R.US else "rest")]] for r in rows]
    us_only = [r["P"][chosen[(r["d"], "us")]] if r["st"] in R.US else r["P"][G75] for r in rows]
    result["S4"] = {"forward": {f"{day}|{name}": GRID[g] for (day, name), g in chosen.items()}}
    for arm_name, cells in (("两组各一个 λ", arm_cells), ("只给美国站换 λ", us_only)):
        ok = True
        for scope_name, scope in (("全部站", np.ones(len(rows), bool)), ("美国站", us)):
            for seg, lo, hi in (("全期", "2026-07-30", "2026-10-01"), ("前段", "2026-07-30", "2026-08-31"), ("后段", "2026-09-01", "2026-10-01")):
                idx = np.where(scope & (d >= lo) & (d <= hi))[0]
                g = _proper_score_gate([tuple(base_cells[i][0]) for i in idx], [tuple(cells[i][0]) for i in idx],
                                       [(rows[i]["st"], rows[i]["d"]) for i in idx])
                result["S4"][f"{arm_name} {scope_name} {seg}"] = g

                def f(v, ci):
                    star = "*" if (ci[0] > 0 or ci[1] < 0) else " "
                    return f"{v:+.5f} [{ci[0]:+.5f},{ci[1]:+.5f}]{star}"

                print(f"   [{arm_name}] [{scope_name} {seg}] ΔLogScore {f(g['d_logscore'], g['ci_logscore'])}  ΔCRPS {f(g['d_crps'], g['ci_crps'])}  ΔBrier {f(g['d_brier'], g['ci_brier'])}")
                if scope_name == "全部站":
                    if seg == "全期":
                        ok &= all(g[key][0] <= 0.0 for key in ("ci_logscore", "ci_crps", "ci_brier"))
                    else:
                        ok &= g["d_logscore"] <= 0.001
        # the US stations' top bucket under the arm
        for scope_name, keep in (("全部提前量", np.ones(len(rows), bool)), (">36h", np.array([r["bin"] <= 1 for r in rows]))):
            m = us & keep
            codes = np.array([code[x] for x in d[m]])
            for label, cs in (("现役", base_cells), (arm_name, cells)):
                sub = [cs[i] for i in np.where(m)[0]]
                tops = np.array([cl[2] for cl in sub])
                hit = np.array([max((y for p, y in cl[1] if abs(p - cl[2]) < 1e-12), default=0.0) for cl in sub])
                line, _ = top_line(tops, hit, np.ones(len(sub)), codes, k)
                print(f"   美国站最高桶 [{scope_name}] [{label}]  {line}")
        result["S4"][f"{arm_name} scores_not_worse"] = bool(ok)
        print(f"   [{arm_name}] 分数不变差：{'是' if ok else '否'}")


if __name__ == "__main__":
    res = {}
    s1(res)
    rows = load_rows()
    s2(rows, res)
    s3(rows, res)
    s4(rows, res)
    with open(os.path.join(HERE, "results_supplement.json"), "w") as fh:
        json.dump(res, fh, ensure_ascii=False, indent=1, default=float)
