#!/usr/bin/env python3
"""PROTOCOL.md, parts A and B.

    python3 readings.py
"""
import json
import os
import pickle
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
ACCEPT = os.path.join(ROOT, "docs", "research", "final_vector_acceptance_20261004")
PEAK = os.path.join(ROOT, "docs", "research", "width_map_peak_20261004")
sys.path.insert(0, ROOT)
sys.path.insert(0, ACCEPT)
sys.path.insert(0, PEAK)
import common as c  # noqa: E402  (final_vector_acceptance_20261004)

US = ("KATL", "KAUS", "KBKF", "KDAL", "KHOU", "KLAX", "KLGA", "KMIA", "KORD", "KSEA", "KSFO")
BINS_A = [0, 0.005, 0.02, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.7, 1.0001]
TOP_BINS = ((0.3, 0.4), (0.4, 0.5), (0.5, 1.01))
BOOT = 2000
SEED = 20261004


def boot(values, weights, codes, k):
    values, weights = np.asarray(values, float), np.asarray(weights, float)
    if len(values) == 0:
        return (float("nan"), float("nan"), float("nan"))
    s = np.bincount(codes, values * weights, k)
    w = np.bincount(codes, weights, k)
    idx = np.random.default_rng(SEED).integers(0, k, size=(BOOT, k))
    with np.errstate(invalid="ignore", divide="ignore"):
        b = s[idx].sum(1) / w[idx].sum(1)
    lo, hi = np.nanpercentile(b, [2.5, 97.5])
    return (float(s.sum() / w.sum()), float(lo), float(hi))


def f(t, scale=1.0, digits=3):
    star = "*" if (t[1] > 0 or t[2] < 0) else " "
    return f"{t[0]*scale:+.{digits}f} [{t[1]*scale:+.{digits}f},{t[2]*scale:+.{digits}f}]{star}"


def vector_readings(rec, key, codes, k, wt):
    """A2-A5 for one vector on the served record (axis = rungs)."""
    out = {}
    tops = np.array([float(x[key].max()) for x in rec])
    mode = np.array([int(np.argmax(x[key])) for x in rec])
    y = np.array([x["y"] for x in rec])
    hit = (mode == y).astype(float)
    out["top"] = []
    for lo, hi in TOP_BINS:
        m = (tops >= lo) & (tops < hi)
        if m.sum() < 20:
            out["top"].append({"bin": [lo, hi], "n": int(m.sum())})
            continue
        out["top"].append({"bin": [lo, hi], "n": int(m.sum()), "claim": float(np.average(tops[m], weights=wt[m])),
                           "hit": float(np.average(hit[m], weights=wt[m])),
                           "gap": boot(hit[m] - tops[m], wt[m], codes[m], k)})
    mom = np.array([c.moments(x[key]) for x in rec])
    err = y - mom[:, 0]
    out["bias"] = boot(err, wt, codes, k)
    out["mae"] = boot(np.abs(err), wt, codes, k)
    out["z2"] = boot((err ** 2 + c.WITHIN) / mom[:, 1] ** 2, wt, codes, k)
    out["sd"] = float(np.average(mom[:, 1], weights=wt))
    rel = np.clip(y - mode, -2, 2)
    out["landing"] = {}
    for off in (-2, -1, 0, 1, 2):
        claimed = np.array([
            float(sum(v for i, v in enumerate(x[key]) if (i - m == off if abs(off) < 2 else (i - m <= -2 if off < 0 else i - m >= 2))))
            for x, m in zip(rec, mode)])
        out["landing"][off] = {"hit": float(np.average((rel == off).astype(float), weights=wt)),
                               "claimed": float(np.average(claimed, weights=wt))}
    out["logscore"] = boot(np.array([np.log(x[key][x["y"]]) for x in rec]), wt, codes, k)
    return out


def show_vector(name, r):
    tops = "  ".join(
        (f"{b['bin'][0]:.1f}-{min(b['bin'][1],1):.1f}: n {b['n']} 说 {b['claim']:.3f} 中 {b['hit']:.3f} 差 {f(b['gap'],100,1)}"
         if "claim" in b else f"{b['bin'][0]:.1f}-{min(b['bin'][1],1):.1f}: n {b['n']}")
        for b in r["top"])
    print(f"   {name}")
    print(f"     最高桶  {tops}")
    print(f"     中心  真值−均值 {f(r['bias'])} 档  |误差| {r['mae'][0]:.3f} [{r['mae'][1]:.3f},{r['mae'][2]:.3f}]   宽度  σ {r['sd']:.3f} 档  E[z²] {r['z2'][0]:.3f} [{r['z2'][1]:.3f},{r['z2'][2]:.3f}]"
          f"   LogScore {r['logscore'][0]:.4f}")
    land = "  ".join(f"{'≤−2' if o == -2 else '≥+2' if o == 2 else f'{o:+d}'}: 中 {v['hit']:.3f} 说 {v['claimed']:.3f}" for o, v in r["landing"].items())
    print(f"     真值相对众数桶  {land}")


def part_a(result):
    rec_all = c.load()
    days = sorted({x["d"] for x in rec_all})
    code = {d: i for i, d in enumerate(days)}
    result["A"] = {}
    for unit in ("C", "F"):
        rec = [x for x in rec_all if x["unit"] == unit]
        codes = np.array([code[x["d"]] for x in rec])
        wt = np.array([x["wt"] for x in rec])
        k = len(days)
        out = {"decisions": len(rec), "station_days": len({(x["st"], x["d"]) for x in rec}), "stations": len({x["st"] for x in rec})}
        print(f"\n== A 实服记录 [{unit} 站]  决策 {out['decisions']}  站日 {out['station_days']}  站 {out['stations']}")
        # A1 every bucket, ten bins
        P = np.concatenate([x["p"] for x in rec]); Y = np.concatenate([(np.arange(x["k"]) == x["y"]).astype(float) for x in rec])
        W = np.concatenate([np.full(x["k"], x["wt"]) for x in rec]); CD = np.concatenate([np.full(x["k"], code[x["d"]]) for x in rec])
        out["bins"] = []
        print("   A1 全部桶   档 / 桶数 / 预测 / 命中 / 差")
        for lo, hi in zip(BINS_A[:-1], BINS_A[1:]):
            m = (P >= lo) & (P < hi)
            if m.sum() == 0:
                continue
            gap = boot(Y[m] - P[m], W[m], CD[m], k)
            row = {"bin": [lo, hi], "n": int(m.sum()), "pred": float(np.average(P[m], weights=W[m])),
                   "hit": float(np.average(Y[m], weights=W[m])), "gap": gap}
            out["bins"].append(row)
            print(f"     {lo:5.3f}-{min(hi,1):5.3f}  {row['n']:6d}  {row['pred']:.4f}  {row['hit']:.4f}  {f(gap,100,1)}")
        out["model"] = vector_readings(rec, "p", codes, k, wt)
        out["market"] = vector_readings(rec, "q", codes, k, wt)
        show_vector("A2–A5 模型（最终向量）", out["model"])
        show_vector("A6 市场（归一中价）", out["market"])
        gap = boot(np.array([np.log(x["p"][x["y"]]) - np.log(x["q"][x["y"]]) for x in rec]), wt, codes, k)
        out["logscore_vs_market"] = gap
        print(f"     LogScore 模型−市场 {f(gap,1,4)}")
        # A7 layers
        lay = [x for x in rec if x["lp"] is not None]
        if lay:
            lc = np.array([code[x["d"]] for x in lay]); lw = np.array([x["wt"] for x in lay])
            out["layers"] = {"decisions": len(lay)}
            print(f"   A7 逐层（09-22 起，决策 {len(lay)}）")
            for name, key in (("单模型（权重最大的一家）", "lead"), ("线性池", "lp"), ("最终", "p"), ("市场", "q")):
                out["layers"][key] = vector_readings(lay, key, lc, k, lw)
                show_vector(name, out["layers"][key])
        # A8 per station
        out["stations_detail"] = []
        if unit == "F":
            print("   A8 逐站   站 / 站日 / 偏差(档) / |误差| / E[z²] / 最高桶声称 / 命中 / 市场最高桶声称 / 命中")
            for st in sorted({x["st"] for x in rec}):
                sub = [x for x in rec if x["st"] == st]
                w = np.array([x["wt"] for x in sub])
                mom = np.array([c.moments(x["p"]) for x in sub]); y = np.array([x["y"] for x in sub])
                top = np.array([float(x["p"].max()) for x in sub]); hit = np.array([float(int(np.argmax(x["p"])) == x["y"]) for x in sub])
                qtop = np.array([float(x["q"].max()) for x in sub]); qhit = np.array([float(int(np.argmax(x["q"])) == x["y"]) for x in sub])
                row = {"station": st, "station_days": len({x["d"] for x in sub}),
                       "bias": float(np.average(y - mom[:, 0], weights=w)), "mae": float(np.average(np.abs(y - mom[:, 0]), weights=w)),
                       "z2": float(np.average(((y - mom[:, 0]) ** 2 + c.WITHIN) / mom[:, 1] ** 2, weights=w)),
                       "top_claim": float(np.average(top, weights=w)), "top_hit": float(np.average(hit, weights=w)),
                       "q_top_claim": float(np.average(qtop, weights=w)), "q_top_hit": float(np.average(qhit, weights=w))}
                out["stations_detail"].append(row)
                print(f"     {st}  {row['station_days']:3d}  {row['bias']:+.2f}  {row['mae']:.2f}  {row['z2']:.2f}  {row['top_claim']:.3f}  {row['top_hit']:.3f}  {row['q_top_claim']:.3f}  {row['q_top_hit']:.3f}")
        result["A"][unit] = out


def part_b(result):
    rows = []
    for name in sorted(os.listdir(os.path.join(PEAK, "rows"))):
        with open(os.path.join(PEAK, "rows", name), "rb") as fh:
            rows.extend(pickle.load(fh))
    grid = tuple(round(0.5 + 0.025 * i, 3) for i in range(41))
    g75 = grid.index(0.75)
    days = sorted({r["d"] for r in rows})
    code = {d: i for i, d in enumerate(days)}
    result["B"] = {}
    for scope, keep in (("全部提前量", lambda r: True), (">36h", lambda r: r["bin"] <= 1)):
        for group, member in (("美国 11 站", lambda r: r["st"] in US), ("其余 34 站", lambda r: r["st"] not in US)):
            sub = [r for r in rows if keep(r) and member(r)]
            codes = np.array([code[r["d"]] for r in sub]); k = len(days); wt = np.ones(len(sub))
            cells = [r["P"][g75] for r in sub]
            tops = np.array([cl[2] for cl in cells])
            hit = np.array([max((y for p, y in cl[1] if abs(p - cl[2]) < 1e-12), default=0.0) for cl in cells])
            line = []
            out = {"decisions": len(sub), "top": []}
            for lo, hi in TOP_BINS:
                m = (tops >= lo) & (tops < hi)
                gap = boot(hit[m] - tops[m], wt[m], codes[m], k)
                out["top"].append({"bin": [lo, hi], "n": int(m.sum()), "claim": float(tops[m].mean()), "hit": float(hit[m].mean()), "gap": gap})
                line.append(f"{lo:.1f}-{min(hi,1):.1f}: n {int(m.sum())} 说 {tops[m].mean():.3f} 中 {hit[m].mean():.3f} 差 {f(gap,100,1)}")
            keys = [[float(int(x)) for x in r["keys"]] for r in sub]
            mean = np.array([float(np.dot(kc, r["p75"]) / sum(r["p75"])) for kc, r in zip(keys, sub)])
            var = np.array([float(np.dot((np.array(kc) - mu) ** 2, r["p75"]) / sum(r["p75"])) + 1 / 12 for kc, r, mu in zip(keys, sub, mean)])
            actual = np.array([r["actual"] for r in sub])
            truth = np.array([float(r["truth"]) for r in sub])
            out["bias"] = boot(truth - mean, wt, codes, k)
            out["mae"] = boot(np.abs(truth - mean), wt, codes, k)
            out["z2"] = boot(((truth - mean) ** 2 + 1 / 12) / var, wt, codes, k)
            out["sd"] = float(np.sqrt(var).mean())
            out["logscore"] = boot(-np.array([cl[0][0] for cl in cells]), wt, codes, k)
            result["B"][f"{scope} {group}"] = out
            print(f"\n== B 现行链回放 [{scope}] [{group}]  决策 {len(sub)}")
            print("     最高桶  " + "  ".join(line))
            print(f"     中心  真值−均值 {f(out['bias'])} °C  |误差| {out['mae'][0]:.3f} [{out['mae'][1]:.3f},{out['mae'][2]:.3f}]"
                  f"   宽度  σ {out['sd']:.3f} °C  E[z²] {out['z2'][0]:.3f} [{out['z2'][1]:.3f},{out['z2'][2]:.3f}]   LogScore {out['logscore'][0]:.4f}")
            del actual


if __name__ == "__main__":
    res = {}
    part_a(res)
    part_b(res)
    with open(os.path.join(HERE, "results.json"), "w") as fh:
        json.dump(res, fh, ensure_ascii=False, indent=1, default=float)
