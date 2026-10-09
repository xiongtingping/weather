#!/usr/bin/env python3
"""PROTOCOL.md section 3: per signal x instant, walk-forward logistic of
P(truth above the market mode | not at the mode) on the market lean, with and
without the signal; block bootstrap, halves, Bonferroni; hit rates; agreement
with the cluster's direction."""
from __future__ import annotations

import gzip
import json
import math
import warnings
from collections import defaultdict
from pathlib import Path

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

warnings.filterwarnings("ignore")
HERE = Path(__file__).resolve().parent
MIN_TRAIN_DAYS, MIN_TEST_DAYS, BLOCK, DRAWS, SEED = 8, 8, 3, 2000, 20261010
BONF = 0.05 / 40
F_STATIONS = {"KATL", "KAUS", "KBKF", "KDAL", "KHOU", "KLAX", "KLGA", "KMIA", "KORD", "KSEA", "KSFO"}
ASIA = tuple("ZRVWO")
CORE = ["S1 我方分布倾向", "S2 六家加权中心", "S4 ΔT24", "S4 ΔTd24", "S4 ΔP24", "S4 持续性(昨日METAR最高−众数)", "S5 市场误差延续", "S6 市场漂移"]

data = [json.loads(l) for l in gzip.open(HERE / "signals.jsonl.gz", "rt")]
inst = [r for r in data if r["tag"] in ("I1", "I2")]
events = [r for r in data if r["tag"] == "EV"]


def logit(x):
    x = min(max(x, 1e-6), 1 - 1e-6)
    return math.log(x / (1 - x))


def bll(p, y):
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return -(y * np.log(p) + (1 - y) * np.log(1 - p))


def walk(sub, feats):
    days = sorted({r["d"] for r in sub})
    X = np.array([[logit(r["Lq"])] + [float(r["x"][f]) for f in feats] for r in sub])
    y = np.array([1.0 if r["y_side"] > 0 else 0.0 for r in sub]); d = np.array([r["d"] for r in sub])
    pred = np.full(len(sub), np.nan)
    for i, day in enumerate(days):
        if i < MIN_TRAIN_DAYS:
            continue
        tr, te = d < day, d == day
        if y[tr].min() == y[tr].max():
            continue
        sc = StandardScaler().fit(X[tr])
        m = LogisticRegression(C=1.0, max_iter=5000).fit(sc.transform(X[tr]), y[tr])
        pred[te] = m.predict_proba(sc.transform(X[te]))[:, 1]
    return pred, y, d


def boot(days_k, vals, rng):
    days = sorted(set(days_k)); n = len(days)
    by = defaultdict(list)
    for dd, v in zip(days_k, vals):
        by[dd].append(v)
    sums = np.array([sum(by[dd]) for dd in days]); cnt = np.array([len(by[dd]) for dd in days], float)
    draws = []
    for _ in range(DRAWS):
        starts = rng.integers(0, n, size=math.ceil(n / BLOCK))
        idx = np.array([(s + j) % n for s in starts for j in range(BLOCK)][:n])
        draws.append(sums[idx].sum() / cnt[idx].sum())
    draws = np.array(draws)
    return float(np.quantile(draws, 0.025)), float(np.quantile(draws, 0.975)), float((draws >= 0).mean())


def compare(sub, feats):
    sub = [r for r in sub if r["y_side"] != 0 and all(r["x"].get(f) is not None for f in feats)]
    if len({r["d"] for r in sub}) < MIN_TRAIN_DAYS + MIN_TEST_DAYS:
        return {"state": "样本不足", "n": len(sub)}
    pb, y, d = walk(sub, [])
    pg, _y, _d = walk(sub, feats)
    ok = ~np.isnan(pb) & ~np.isnan(pg)
    if ok.sum() < 100:
        return {"state": "样本不足", "n": int(ok.sum())}
    diff = bll(pg[ok], y[ok]) - bll(pb[ok], y[ok])
    dd = d[ok]; tdays = sorted(set(dd)); half = tdays[len(tdays) // 2]
    lo, hi, p_one = boot(list(dd), list(diff), np.random.default_rng(SEED))
    h1, h2 = float(diff[dd < half].mean()), float(diff[dd >= half].mean())
    lq = np.array([r["Lq"] for r in sub])[ok]
    verdict = "有" if (hi < 0 and h1 < 0 and h2 < 0 and p_one < BONF) else ("苗头" if hi < 0 and h1 < 0 and h2 < 0 else "没有")
    return {"n": int(ok.sum()), "days": len(tdays), "delta": float(diff.mean()), "ci": [lo, hi], "halves": [h1, h2], "p": p_one,
            "hit_market": float(((lq > 0.5) == (y[ok] > 0.5)).mean()), "hit_model": float(((pg[ok] > 0.5) == (y[ok] > 0.5)).mean()),
            "verdict": verdict}


signals = sorted({k for r in inst for k in r["x"]})
result = {"per_signal": {}, "all_core": {}, "slices": {}, "cluster_agreement": {}}
for tag in ("I1", "I2"):
    sub = [r for r in inst if r["tag"] == tag]
    result["per_signal"][tag] = {s: compare(sub, [s]) for s in signals}
    result["all_core"][tag] = compare(sub, CORE)
    for name, pred in (("亚洲", lambda r: r["st"].startswith(ASIA)), ("C站", lambda r: r["st"] not in F_STATIONS),
                       ("市场众数<0.35", lambda r: r["q_mode"] < 0.35)):
        ss = [r for r in sub if pred(r)]
        result["slices"][f"{tag}·{name}"] = {s: compare(ss, [s]) for s in CORE}
# do they see it? sign agreement on the cluster's off-mode YES events
for s in signals:
    pairs = [(r["x"][s], r["their_side"], r["y_side"]) for r in events if r["x"].get(s) not in (None, 0)]
    if len(pairs) >= 30:
        agree = np.mean([math.copysign(1, x) == side for x, side, _y in pairs])
        sig_hit = [math.copysign(1, x) == y for x, _side, y in pairs if y != 0]
        their_hit = [side == y for _x, side, y in pairs if y != 0]
        result["cluster_agreement"][s] = {"n": len(pairs), "agree_with_cluster": float(agree),
                                          "signal_hit_off_mode": float(np.mean(sig_hit)) if sig_hit else None,
                                          "cluster_hit_off_mode": float(np.mean(their_hit)) if their_hit else None}
(HERE / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))


def show(r):
    if "state" in r:
        return f"{r['state']}（n={r['n']}）"
    return (f"n={r['n']:4d} {r['days']:2d}日 Δ {r['delta']:+.4f} [{r['ci'][0]:+.4f},{r['ci'][1]:+.4f}] 两半 {r['halves'][0]:+.4f}/{r['halves'][1]:+.4f}"
            f" 判对 市场{r['hit_market']:.3f}→{r['hit_model']:.3f} p={r['p']:.3f} → {r['verdict']}")


for tag in ("I1", "I2"):
    print(f"==== {tag}")
    for s, r in result["per_signal"][tag].items():
        print(f"   {s:34s} {show(r)}")
    print(f"   {'ALL(核心8项)':34s} {show(result['all_core'][tag])}")
print("==== 切片（核心信号）")
for k, v in result["slices"].items():
    best = sorted(((s, r) for s, r in v.items() if "delta" in r), key=lambda kv: kv[1]["delta"])[:3]
    print("  ", k, " | ".join(f"{s} {r['delta']:+.4f}[{r['ci'][0]:+.4f},{r['ci'][1]:+.4f}]{r['verdict']}" for s, r in best))
print("==== 与簇方向同向（簇离众数 YES 事件）")
for s, r in sorted(result["cluster_agreement"].items(), key=lambda kv: -abs(kv[1]["agree_with_cluster"] - 0.5)):
    print(f"   {s:34s} n={r['n']:3d} 同向 {r['agree_with_cluster']:.3f}  信号自身判对 {r['signal_hit_off_mode']}  簇判对 {r['cluster_hit_off_mode']}")
