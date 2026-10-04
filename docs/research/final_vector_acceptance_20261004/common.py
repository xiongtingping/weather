#!/usr/bin/env python3
"""Shared frame for the final-vector acceptance: padded arrays, one row a decision.

Axis: the bucket's position on its own ladder (0..K-1), so a C ladder (1 degC a
rung) and an F ladder (2 degF a rung) are read in the same unit -- rungs.
"""
import gzip
import json
import os
import pickle
import re

import numpy as np
from scipy.optimize import brentq

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "decisions.jsonl.gz")
CACHE = os.path.join(HERE, "frame.pkl")
FLOOR = 1e-3            # charter 224's floor, as floor_probabilities applies it
WITHIN = 1.0 / 12.0     # variance of a uniform density inside one rung
B = 2000
SEED = 20261004


def floor_vec(v):
    v = np.asarray(v, dtype=float)
    v = v / v.sum()
    if not (v < FLOOR).any():
        return v
    lifted = np.maximum(v, FLOOR)
    return lifted / lifted.sum()


def moments(v):
    i = np.arange(len(v))
    mu = float((i * v).sum())
    return mu, float(np.sqrt(((i - mu) ** 2 * v).sum() + WITHIN))


def affine(v, shift, scale, about):
    """Move a piecewise-uniform ladder density: x -> about + scale*(x-about) + shift,
    then re-bin; the end rungs take whatever leaves the ladder."""
    k = len(v)
    lo = about + scale * (np.arange(k) - 0.5 - about) + shift
    hi = lo + scale
    edges_lo = np.arange(k) - 0.5
    edges_hi = edges_lo + 1.0
    edges_lo[0], edges_hi[-1] = -np.inf, np.inf
    overlap = np.clip(np.minimum(hi[:, None], edges_hi[None, :]) - np.maximum(lo[:, None], edges_lo[None, :]),
                      0.0, None)
    return (v[:, None] * overlap / scale).sum(0)


def shifted_to(v, mean):
    """v moved along the ladder until its mean is ``mean``."""
    mu, _ = moments(v)
    k = len(v)
    f = lambda s: moments(affine(v, s, 1.0, mu))[0] - mean  # noqa: E731
    lo, hi = -float(k), float(k)
    if f(lo) >= 0:
        return affine(v, lo, 1.0, mu)
    if f(hi) <= 0:
        return affine(v, hi, 1.0, mu)
    return affine(v, brentq(f, lo, hi, xtol=1e-6), 1.0, mu)


def matched_to(v, mean, sd):
    """v moved and stretched about its mean until mean and sd are the targets
    (as near as the ladder's ends let it)."""
    mu, _ = moments(v)
    k = len(v)

    def at(scale):
        f = lambda s: moments(affine(v, s, scale, mu))[0] - mean  # noqa: E731
        lo, hi = -2.0 * k, 2.0 * k
        if f(lo) >= 0:
            s = lo
        elif f(hi) <= 0:
            s = hi
        else:
            s = brentq(f, lo, hi, xtol=1e-5)
        return affine(v, s, scale, mu)

    g = lambda ls: moments(at(np.exp(ls)))[1] - sd  # noqa: E731
    lo, hi = np.log(0.05), np.log(20.0)
    if g(lo) >= 0:
        return at(np.exp(lo))
    if g(hi) <= 0:
        return at(np.exp(hi))
    return at(np.exp(brentq(g, lo, hi, xtol=1e-4)))


def build():
    rec = []
    with gzip.open(SRC, "rt", encoding="utf-8") as fh:
        for line in fh:
            r = json.loads(line)
            rows = sorted(r["b"], key=lambda x: x[1])
            names = [x[0] for x in rows]
            order = {x[0]: i for i, x in enumerate(r["b"])}
            if r["q"] is None or any(x[2] is None for x in rows) or r["y"] not in names:
                continue
            p_raw = np.array([x[2] for x in rows], dtype=float)
            if not p_raw.sum() > 0:
                continue
            k = len(rows)
            p = floor_vec(p_raw)
            q = floor_vec(np.array([r["q"][n] for n in names], dtype=float))
            q_raw = np.array([r["q"][n] for n in names], dtype=float)
            mode = int(np.argmax(q_raw))            # first maximum = the lower rung on a tie
            mp, sp = moments(p)
            mq, sq = moments(q)
            v1 = floor_vec(shifted_to(q, mp))
            v2 = floor_vec(matched_to(q, mp, sp))
            r1 = floor_vec(shifted_to(p, mq))       # reverse path: the model at the market's centre
            r2 = floor_vec(matched_to(p, mq, sq))   # ... and at the market's width
            w = r.get("w") or {}
            sysv = {s: np.array([vec[order[n]] for n in names], dtype=float) for s, vec in (r.get("sys") or {}).items()}
            seated = {s: float(w.get(s, 0.0)) for s in sysv if float(w.get(s, 0.0)) > 1e-9 and sysv[s].sum() > 0}
            lp = lead = None
            if seated:
                tot = sum(seated.values())
                lp = floor_vec(sum(seated[s] / tot * sysv[s] / sysv[s].sum() for s in seated))
                top = max(sorted(seated), key=lambda s: seated[s])
                lead = floor_vec(sysv[top])
                sys_floored = {s: floor_vec(sysv[s]) for s in seated}
            m = re.search(r"_v(\d+)$", r["pv"] or "")
            bridge = r.get("bridge") or {}
            cw = r.get("cw") or {}
            rec.append({
                "st": r["st"], "d": r["d"], "t": r["t"], "h": r["h"], "ver": int(m.group(1)) if m else -1,
                "unit": "F" if "°F" in names[0] else "C", "k": k, "y": names.index(r["y"]), "mode": mode,
                "ms": r["ms"], "nsys": len(seated), "p": p, "q": q, "v1": v1, "v2": v2, "r1": r1, "r2": r2,
                "lp": lp, "lead": lead,
                "sys": [(seated[s] / sum(seated.values()), sys_floored[s]) for s in seated] if seated else [],
                "bid": np.array([np.nan if x[3] is None else x[3] for x in rows]),
                "ask": np.array([np.nan if x[4] is None else x[4] for x in rows]),
                "bridge_lambda": bridge.get("lambda"), "bridge_gap": bridge.get("center_gap_c"),
                "width_lambda": cw.get("width_lambda", cw.get("composed_width_lambda_unfloored")),
                "cw": cw, "shift_c": r.get("shift_c"),
                "floored": [names.index(n) for n in (r.get("floored") or []) if n in names],
                "names": names,
            })
    counts = {}
    for x in rec:
        counts[(x["st"], x["d"])] = counts.get((x["st"], x["d"]), 0) + 1
    for x in rec:
        x["wt"] = 1.0 / counts[(x["st"], x["d"])]
    with open(CACHE, "wb") as fh:
        pickle.dump(rec, fh)
    return rec


def load(since=None):
    if os.path.exists(CACHE) and os.path.getmtime(CACHE) > os.path.getmtime(SRC):
        with open(CACHE, "rb") as fh:
            rec = pickle.load(fh)
    else:
        rec = build()
    return [x for x in rec if since is None or x["d"] >= since]


def day_codes(days):
    uniq = sorted(set(days))
    index = {d: i for i, d in enumerate(uniq)}
    return np.array([index[d] for d in days]), len(uniq)


def boot_ratio(num, den, codes, k, n_boot=B, seed=SEED):
    """sum(num)/sum(den) with an event-day cluster bootstrap interval."""
    sn = np.bincount(codes, num, k)
    sd = np.bincount(codes, den, k)
    est = sn.sum() / sd.sum() if sd.sum() > 0 else np.nan
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, k, size=(n_boot, k))
    with np.errstate(invalid="ignore", divide="ignore"):
        bs = sn[idx].sum(1) / sd[idx].sum(1)
    lo, hi = np.nanpercentile(bs, [2.5, 97.5])
    return est, lo, hi


def fmt(t, scale=1.0, digits=4):
    est, lo, hi = t
    star = "*" if (lo > 0 or hi < 0) else " "
    return f"{est*scale:+.{digits}f} [{lo*scale:+.{digits}f},{hi*scale:+.{digits}f}]{star}"


if __name__ == "__main__":
    import time
    t0 = time.time()
    rec = build()
    days = sorted({x["d"] for x in rec})
    print(len(rec), "decisions", len({(x["st"], x["d"]) for x in rec}), "station-days", len(days), "event days",
          days[0], days[-1], f"{time.time()-t0:.0f}s")
    print("with layers:", sum(1 for x in rec if x["lp"] is not None))
    import collections
    print(sorted(collections.Counter(x["ver"] for x in rec).items()))
    print(sorted(collections.Counter(x["nsys"] for x in rec).items()))
