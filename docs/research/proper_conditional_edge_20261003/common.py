import numpy as np, pandas as pd
from load import load

HB = [-100, 0, 6, 12, 24, 36, 48, 60, 200]
HBL = ["<0", "0-6", "6-12", "12-24", "24-36", "36-48", "48-60", ">60"]


def frame():
    df = load()
    df["hb"] = pd.cut(df["h"], HB, labels=HBL)
    df["era"] = np.where(df.d >= "2026-09-26", "C:09-26+", np.where(df.d >= "2026-09-12", "B:09-12..25", "A:<09-12"))
    return df


def boot(x, w, cl, B=2000, seed=7, fn=None):
    """Weighted mean with a cluster (event-date) bootstrap CI."""
    x = np.asarray(x, float); w = np.asarray(w, float)
    codes, uniq = pd.factorize(np.asarray(cl))
    k = len(uniq)
    sx = np.bincount(codes, w * x, k); sw = np.bincount(codes, w, k)
    est = sx.sum() / sw.sum()
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, k, size=(B, k))
    bs = sx[idx].sum(1) / sw[idx].sum(1)
    lo, hi = np.percentile(bs, [2.5, 97.5])
    return est, lo, hi, k


def fmt(t):
    est, lo, hi, k = t
    star = "*" if (lo > 0 or hi < 0) else " "
    return f"{est:+.4f} [{lo:+.4f},{hi:+.4f}]{star} (days={k})"
