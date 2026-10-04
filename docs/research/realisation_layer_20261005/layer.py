#!/usr/bin/env python3
"""PROTOCOL.md: fit each candidate layer walk-forward and keep, for every
scored decision, its belief b and the 10% / 90% points of b under a bootstrap
of the fitting days.

    python3 layer.py M1        # one model per call; results in fits/<model>.pkl
"""
import os
import pickle
import sys
import time
from datetime import datetime, timedelta

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
ACCEPT = os.path.join(ROOT, "docs", "research", "final_vector_acceptance_20261004")
sys.path.insert(0, ROOT)
sys.path.insert(0, ACCEPT)
import analysis as A  # noqa: E402  (pool_fit, pooled, padded)
import common as c  # noqa: E402

MIN_PREFIX_DAYS = 8
DRAWS = 60
SEED = 20261005
CENTRE_AGREE_RUNGS = 0.25
MODELS = ("M1", "M2", "M3", "M4")


def frame():
    rec = c.load()
    P, Q, V1, V2 = (A.padded(rec, k) for k in ("p", "q", "v1", "v2"))
    mask = ~np.isnan(Q)
    with np.errstate(invalid="ignore"):
        logq, logp = np.log(Q), np.log(P)
        d1, d2, d3 = np.log(V1) - np.log(Q), np.log(V2) - np.log(V1), np.log(P) - np.log(V2)
    gap = np.array([abs(c.moments(x["p"])[0] - c.moments(x["q"])[0]) for x in rec])
    agree = (gap <= CENTRE_AGREE_RUNGS).astype(float)[:, None]
    terms = {
        "M1": [logq],
        "M2": [logq, logp],
        "M3": [logq, d1, d2, d3],
        "M4": [logq, d2 * agree, d3 * agree, d2 * (1 - agree), d3 * (1 - agree)],
    }
    # The amendment: two intercepts by distance from the market's modal bucket.
    kmax = Q.shape[1]
    pos = np.arange(kmax)[None, :]
    mode = np.array([x["mode"] for x in rec])[:, None]
    dist = np.abs(pos - mode)
    at_mode = np.where(mask, (dist == 0).astype(float), np.nan)
    beside = np.where(mask, (dist == 1).astype(float), np.nan)
    terms["M5"] = terms["M4"] + [at_mode, beside]
    base = np.where(mask, 1.0, np.nan)             # the pool starts from the flat ladder
    y = np.array([x["y"] for x in rec])
    wt = np.array([x["wt"] for x in rec])
    days = np.array([x["d"] for x in rec])
    return rec, base, terms, y, wt, days, gap


def start_of(model):
    return np.array([1.0] + [0.0] * {"M1": 0, "M2": 1, "M3": 3, "M4": 4, "M5": 6}[model])


def main(model):
    rec, base, terms, y, wt, days, _gap = frame()
    deltas = terms[model]
    uniq = sorted(set(days))
    code = {d: i for i, d in enumerate(uniq)}
    day_code = np.array([code[d] for d in days])
    rng = np.random.default_rng(SEED)
    out = {"model": model, "theta": {}, "b": {}, "lo": {}, "hi": {}}
    t0 = time.time()
    for day in uniq:
        cut = (datetime.fromisoformat(day) - timedelta(days=2)).date().isoformat()
        train = days <= cut
        train_days = sorted(set(days[train]))
        if len(train_days) < MIN_PREFIX_DAYS:
            continue
        test = np.where(days == day)[0]
        tr = np.where(train)[0]
        sub = [d[tr] for d in deltas]
        theta = A.pool_fit(base[tr], sub, y[tr], wt[tr], start=start_of(model))
        test_d = [d[test] for d in deltas]
        point = A.pooled(base[test], test_d, theta)
        draws = np.empty((DRAWS,) + point.shape)
        codes_tr = day_code[tr]
        pool = np.array([code[d] for d in train_days])
        for i in range(DRAWS):
            picked = rng.choice(pool, size=len(pool), replace=True)
            mult = np.bincount(picked, minlength=len(uniq)).astype(float)
            th = A.pool_fit(base[tr], sub, y[tr], wt[tr] * mult[codes_tr], start=theta)
            draws[i] = A.pooled(base[test], test_d, th)
        lo, hi = np.nanpercentile(draws, [10, 90], axis=0)
        out["theta"][day] = [float(v) for v in theta]
        for j, i in enumerate(test):
            k = rec[i]["k"]
            out["b"][int(i)] = point[j, :k]
            out["lo"][int(i)] = lo[j, :k]
            out["hi"][int(i)] = hi[j, :k]
        print(f"{model} {day} train days {len(train_days)} decisions {len(tr)} test {len(test)} theta {np.round(theta, 3)} {time.time()-t0:.0f}s", flush=True)
    os.makedirs(os.path.join(HERE, "fits"), exist_ok=True)
    with open(os.path.join(HERE, "fits", f"{model}.pkl"), "wb") as fh:
        pickle.dump(out, fh)


if __name__ == "__main__":
    main(sys.argv[1])
