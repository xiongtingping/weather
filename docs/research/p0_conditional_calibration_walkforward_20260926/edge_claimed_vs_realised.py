"""Claimed vs realised LogScore edge over the market mid (user: 「全站新的logscore计算出来的和市场中价的edge平均有多少？」).

Claimed = the model's own expected edge, KL(p||m) = sum p log(p/m) on the floored vector; realised = LogScore(m) - LogScore(p)
on the settled bucket (positive = model better).  Station-day means.  Read-only; pass the gap study's cache.
"""
import pickle, math, sys
from collections import Counter, defaultdict
import numpy as np
sys.path.insert(0, "/Users/xiong/weather"); sys.path.insert(0, "/Users/xiong/weather/docs/research/model_market_gap_decomposition_20260926")
from bayes_basket_target.materialization import floor_probabilities
CACHE = sys.argv[1]
served, _, today, _ = pickle.load(open(CACHE, "rb"))
def fl(keys, v):
    f, _ = floor_probabilities({k: float(x) for k, x in zip(keys, v)}); a = np.array([f[k] for k in keys]); return a / a.sum()
def rows_stats(items):
    per = defaultdict(list)
    for d in items:
        p = fl(d["keys"], d["p"]); m = np.clip(np.asarray(d["m"]), 1e-12, None); m = m / m.sum()
        kl = float(np.sum(p * np.log(p / m)))          # model's own expected LogScore edge over the market
        realized = d["ls_m"] - d["ls_p"]                  # market LS minus model LS on the settled bucket (+ = model better)
        per[(d["station"], d["date"])].append((kl, realized))
    sd = [(np.mean([a for a, _ in v]), np.mean([b for _, b in v])) for v in per.values()]
    return len(sd), float(np.mean([a for a, _ in sd])), float(np.mean([b for _, b in sd]))
print("today's P0 replay, strict 24-60h:", rows_stats([d for d in today if d["bin"] in (2, 3, 4)]))
for stage, bins, label in (("P0", (3, 4), "P0 36-60h"), ("P0-M", (2, 3), "P0-M 24-48h"), ("P1", (1,), "P1 12-24h"), ("P1", (0,), "P1 0-12h")):
    items = [d for d in served if d["stage"] == stage and d["bin"] in bins and d["date"] >= "2026-09-10"]
    print("served", label, "09-10..09-24:", rows_stats(items))
