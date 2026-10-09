#!/usr/bin/env python3
"""PROTOCOL.md sections 3-4: walk-forward logistic per information group and
family on the market mean bucket +-1 rows; paired binary log loss against the
market baseline, 3-day block bootstrap, halves, Bonferroni; Murphy RES/REL."""
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
MIN_TRAIN_DAYS, BLOCK, DRAWS, SEED = 8, 3, 2000, 20261010
CLIP = 1e-6
BONF = 0.05 / 24
EDGES = np.array([0.0, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0 + 1e-9])
FAMILIES = ("P0>36h", "P0≤36h", "P0-M", "P1")

rows = [json.loads(l) for l in gzip.open(HERE / "features.jsonl.gz", "rt")]


def logit(x):
    x = min(max(float(x), 1e-6), 1 - 1e-6)
    return math.log(x / (1 - x))


def posd(r):
    return [1.0 if r["pos"] == k else 0.0 for k in (-1, 0, 1)]


def day_terms(r, keys):
    """A day-level x enters as x*pos (shift) and x*1[pos=0] (concentration); missing -> 0 plus a flag."""
    out = []
    for k in keys:
        v = r["day"].get(k)
        miss = v is None
        v = 0.0 if miss else float(v)
        out += [v * r["pos"], v * (1.0 if r["pos"] == 0 else 0.0), float(miss)]
    return out


def bucket_terms(r, keys):
    out = []
    for k in keys:
        v = r["bucket"].get(k)
        miss = v is None
        out += [0.0 if miss else float(v), float(miss)]
    return out


def base(r):
    lq = logit(r["q"])
    return posd(r) + [lq * d for d in posd(r)]


GROUPS = {
    "B0": base,
    "G1 模型概率": lambda r: base(r) + [logit(r["p"]), logit(r["p"]) * r["pos"]],
    "G2 模型与市场相对位置": lambda r: base(r) + bucket_terms(r, ["z", "is_model_mode"]) + [r["bucket"]["z"] ** 2]
        + day_terms(r, ["dmu", "adm", "dmode", "meanbucket_eq_M"]),
    "G3 历史温度": lambda r: base(r) + bucket_terms(r, ["d_last", "d_last3"]) + day_terms(r, ["last_lag", "model_err", "anom30"]),
    "G4 云雨辐射": lambda r: base(r) + day_terms(r, ["rad_z", "cloud", "precip", "wind"]),
    "G5 入座权重": lambda r: base(r) + day_terms(r, ["neff", "nseat", "disp", "wmax", "ai", "agree_sgn"]),
    "G6 时效与盘口": lambda r: base(r) + day_terms(r, ["hours", "spread", "snap_age", "midsum_dev"]),
}
GROUPS["ALL"] = lambda r: (GROUPS["G1 模型概率"](r) + GROUPS["G2 模型与市场相对位置"](r)[6:] + GROUPS["G3 历史温度"](r)[6:]
                           + GROUPS["G4 云雨辐射"](r)[6:] + GROUPS["G5 入座权重"](r)[6:] + GROUPS["G6 时效与盘口"](r)[6:])
GROUPS["P-only（对照）"] = lambda r: posd(r) + [logit(r["p"]) * d for d in posd(r)]


def walk_forward(sub, builder):
    days = sorted({r["d"] for r in sub})
    X = np.array([builder(r) for r in sub]); y = np.array([r["y"] for r in sub]); d = np.array([r["d"] for r in sub])
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
    return pred


def bll(p, y):
    p = np.clip(p, CLIP, 1 - CLIP)
    return -(y * np.log(p) + (1 - y) * np.log(1 - p))


def murphy(p, y):
    k = np.digitize(p, EDGES) - 1; N = len(p); base_rate = y.mean(); rel = res = 0.0
    for j in range(len(EDGES) - 1):
        m = k == j
        if m.sum():
            rel += m.sum() * (p[m].mean() - y[m].mean()) ** 2; res += m.sum() * (y[m].mean() - base_rate) ** 2
    return rel / N, res / N


def block_boot(dec_days, values, rng):
    days = sorted(set(dec_days)); n = len(days)
    by = defaultdict(list)
    for dd, v in zip(dec_days, values):
        by[dd].append(v)
    sums = np.array([sum(by[dd]) for dd in days]); cnts = np.array([len(by[dd]) for dd in days], float)
    draws = []
    for _ in range(DRAWS):
        starts = rng.integers(0, n, size=math.ceil(n / BLOCK))
        idx = np.array([(s + j) % n for s in starts for j in range(BLOCK)][:n])
        draws.append(sums[idx].sum() / cnts[idx].sum())
    draws = np.array(draws)
    return float(np.quantile(draws, 0.025)), float(np.quantile(draws, 0.975)), float((draws >= 0).mean())


result = {}
for fam in FAMILIES:
    sub = [r for r in rows if r["fam"] == fam]
    n_dec = len({r["dec"] for r in sub}); n_days = len({r["d"] for r in sub})
    if n_days < MIN_TRAIN_DAYS + 8 or n_dec < 200:
        result[fam] = {"state": "样本不足", "decisions": n_dec, "event_days": n_days}
        continue
    preds = {g: walk_forward(sub, b) for g, b in GROUPS.items()}
    ok = ~np.isnan(preds["B0"])
    for g in preds:
        ok &= ~np.isnan(preds[g])
    y = np.array([r["y"] for r in sub]); q = np.array([r["q"] for r in sub]); p = np.array([r["p"] for r in sub])
    dec = np.array([r["dec"] for r in sub]); dd = np.array([r["d"] for r in sub])
    test_days = sorted(set(dd[ok])); half = test_days[len(test_days) // 2]
    out = {"decisions_scored": int(len(set(dec[ok]))), "rows_scored": int(ok.sum()), "test_days": [test_days[0], test_days[-1], len(test_days)]}
    raw = {"市场q原样": q, "模型p原样": p}
    for name, v in list(raw.items()) + [(g, preds[g]) for g in GROUPS]:
        l = bll(v[ok], y[ok]); rel, res = murphy(v[ok], y[ok])
        per_dec = defaultdict(float)
        for k, val in zip(dec[ok], l):
            per_dec[k] += val
        out[name] = {"bll_per_decision": float(np.mean(list(per_dec.values()))), "REL": rel, "RES": res}
    rng = np.random.default_rng(SEED)
    lb0 = bll(preds["B0"][ok], y[ok])
    for g in list(GROUPS) + ["模型p原样"]:
        if g == "B0":
            continue
        lg = bll((preds[g] if g in preds else p)[ok], y[ok])
        per = defaultdict(float); dday = {}
        for k, a, b, day in zip(dec[ok], lg, lb0, dd[ok]):
            per[k] += a - b; dday[k] = day
        keys = sorted(per)
        vals = [per[k] for k in keys]; days_k = [dday[k] for k in keys]
        lo, hi, p_one = block_boot(days_k, vals, rng)
        h1 = np.mean([v for v, dy in zip(vals, days_k) if dy < half]); h2 = np.mean([v for v, dy in zip(vals, days_k) if dy >= half])
        mean = float(np.mean(vals))
        verdict = "有" if (hi < 0 and h1 < 0 and h2 < 0 and p_one < BONF) else ("苗头" if hi < 0 and h1 < 0 and h2 < 0 else "没有")
        out[g]["delta_vs_B0"] = {"mean": mean, "ci95": [lo, hi], "halves": [float(h1), float(h2)], "p_one_sided": p_one, "verdict": verdict}
    result[fam] = out
(HERE / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))

for fam, out in result.items():
    if "state" in out:
        print(f"== {fam}: {out['state']}（{out['decisions']} 决策 / {out['event_days']} 事件日）")
        continue
    print(f"== {fam}: {out['decisions_scored']} 决策、{out['rows_scored']} 桶行、检验日 {out['test_days']}")
    for name, v in out.items():
        if not isinstance(v, dict) or "bll_per_decision" not in v:
            continue
        line = f"   {name:14s} 二元LogLoss/决策 {v['bll_per_decision']:.4f}  RES {v['RES']*1e3:6.2f}e-3  REL {v['REL']*1e3:5.2f}e-3"
        if "delta_vs_B0" in v:
            dlt = v["delta_vs_B0"]
            line += f"  Δ对B0 {dlt['mean']:+.4f} [{dlt['ci95'][0]:+.4f},{dlt['ci95'][1]:+.4f}] 两半 {dlt['halves'][0]:+.4f}/{dlt['halves'][1]:+.4f} p={dlt['p_one_sided']:.4f} → {dlt['verdict']}"
        print(line)
