"""Round 66 (PROTOCOL_R66_KEEP_F_MEMBER_TILT.md): member structure + run change as a keep-F side tilt, scored on the whole ladder.

Reuses round 65's sample and features (exec of raw_shoulder_r65.py up to its scoring).  Usage: keep_f_member_tilt_r66.py <bench>
"""
import math
import statistics
import sys
from pathlib import Path

import numpy as np

here = Path(__file__)
B = sys.argv[1]
src = here.with_name("raw_shoulder_r65.py").read_text()
ns = {"__name__": "r65", "__file__": str(here.with_name("raw_shoulder_r65.py"))}
sys.argv = [sys.argv[0], B, "score"]
exec(compile(src.split("dl = sorted({r[\"ed\"] for r in rows})")[0], "r65_head", "exec"), ns)
rows, GROUPS = ns["rows"], ns["GROUPS"]

FLOOR = 1e-3
COLS_FULL = ["pos"] + GROUPS["G2 member structure"] + ["G1 change median"]
days = sorted({r["ed"] for r in rows})
MIN_DAYS = 7


def fit_logit(X, yv, l2=1.0, iters=800, lr=0.3):
    w = np.zeros(X.shape[1] + 1)
    Xb = np.c_[X, np.ones(len(X))]
    for _ in range(iters):
        p = 1 / (1 + np.exp(-Xb @ w))
        g = Xb.T @ (p - yv) / len(X)
        g[:-1] += l2 * w[:-1] / len(X)
        w -= lr * g
    return w


def logit_wf(cols):
    """Walk-forward logit of P(warm) for every row (all rows, not only off-F)."""
    out = {}
    for d in days:
        tr = [r for r in rows if r["ed"] < d and r["side"] != 0]
        if len({r["ed"] for r in tr}) < MIN_DAYS:
            continue
        X = np.array([[r["x"][c] for c in cols] for r in tr])
        mu, sd = X.mean(0), X.std(0)
        sd[sd < 1e-9] = 1.0
        w = fit_logit((X - mu) / sd, np.array([r["side"] > 0 for r in tr], float))
        for r in rows:
            if r["ed"] == d:
                out[r["k"]] = float(np.r_[(np.array([r["x"][c] for c in cols]) - mu) / sd, 1.0] @ w)
    return out


Z_POS, Z_FULL = logit_wf(["pos"]), logit_wf(COLS_FULL)


def truth_c(r):
    lo, hi = r["edges"][r["y"]]
    return (lo + hi) / 2


# walk-forward fleet sigma of (truth - F), with the truth taken at its bucket centre (open ends at the nominal width)
SIG = {}
for d in days:
    prior = [truth_c(r) - r["f"] for r in rows if r["ed"] < d]
    if len({r["ed"] for r in rows if r["ed"] < d}) >= MIN_DAYS:
        SIG[d] = math.sqrt(statistics.mean(e * e for e in prior))


def ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def base_vec(r):
    s = SIG[r["ed"]]
    e = r["edges"]
    cdf = [0.0] + [ncdf((e[i][1] - r["f"]) / s) for i in range(len(e) - 1)] + [1.0]
    p = np.diff(np.array(cdf))
    p = np.maximum(p, FLOOR)
    return p / p.sum()


def tilt(p, fb, ratio_mult=None, share=None):
    A, Bm = p[fb + 1:].sum(), p[:fb].sum()
    if A <= 0 or Bm <= 0:
        return p
    R = A + Bm
    if share is not None:
        a2 = R * min(max(share, 1e-6), 1 - 1e-6)
    else:
        o = (A / Bm) * ratio_mult
        a2 = R * o / (1 + o)
    q = p.copy()
    q[fb + 1:] *= a2 / A
    q[:fb] *= (R - a2) / Bm
    q = np.maximum(q, FLOOR)
    return q / q.sum()


def scores(p, y, w):
    oh = np.zeros(len(p))
    oh[y] = 1.0
    return np.array([-math.log(p[y]), float(((np.cumsum(p) - np.cumsum(oh)) ** 2).sum()) * w, float(((p - oh) ** 2).sum())])


scored = [r for r in rows if r["k"] in Z_FULL and r["k"] in Z_POS and r["ed"] in SIG]
sig_days = {d: round(v, 3) for d, v in SIG.items()}
print(f"scored station-days {len(scored)} on {len({r['ed'] for r in scored})} event days (first {MIN_DAYS} days train only);"
      f" sigma(truth-F) walk-forward {min(sig_days.values())}..{max(sig_days.values())} C")
BASE = {r["k"]: scores(base_vec(r), r["y"], r["w"]) for r in scored}


def sig(z):
    return 1 / (1 + math.exp(-z))


ARMS = {
    "kappa 1 (main)": lambda r, p: tilt(p, r["fb"], ratio_mult=math.exp(1.0 * (Z_FULL[r["k"]] - Z_POS[r["k"]]))),
    "kappa 0.5": lambda r, p: tilt(p, r["fb"], ratio_mult=math.exp(0.5 * (Z_FULL[r["k"]] - Z_POS[r["k"]]))),
    "replace with P_full": lambda r, p: tilt(p, r["fb"], share=sig(Z_FULL[r["k"]])),
    "replace with P_pos (control)": lambda r, p: tilt(p, r["fb"], share=sig(Z_POS[r["k"]])),
}
rng = np.random.default_rng(66)


def boot(vals, eds, draws=2000):
    vals, eds = np.asarray(vals, float), np.asarray(eds)
    ud = sorted(set(eds))
    sums = np.array([vals[eds == d].sum() for d in ud])
    cnts = np.array([(eds == d).sum() for d in ud])
    idx = rng.integers(0, len(ud), size=(draws, len(ud)))
    bs = sums[idx].sum(1) / cnts[idx].sum(1)
    return float(vals.mean()), float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5))


def fmt(t):
    m, lo, hi = t
    return f"{m:+.4f} [{lo:+.4f}, {hi:+.4f}]{'*' if lo > 0 or hi < 0 else ' '}"


b = np.array(list(BASE.values()))
print(f"base N(F, sigma): LogScore {b[:, 0].mean():.4f}, CRPS {b[:, 1].mean():.4f} C, Brier {b[:, 2].mean():.4f};"
      f" truth on F {statistics.mean(r['y'] == r['fb'] for r in scored):.1%}")
DD = {}
for name, fn in ARMS.items():
    DD[name] = {r["k"]: scores(fn(r, base_vec(r)), r["y"], r["w"]) - BASE[r["k"]] for r in scored}
# walk-forward kappa
KGRID = [0.0, 0.5, 1.0, 1.5]
dk = {kap: {r["k"]: scores(tilt(base_vec(r), r["fb"], ratio_mult=math.exp(kap * (Z_FULL[r["k"]] - Z_POS[r["k"]]))), r["y"], r["w"]) - BASE[r["k"]]
            for r in scored} for kap in KGRID}
wf, chosen = {}, {}
for d in sorted({r["ed"] for r in scored}):
    prior = [r["k"] for r in scored if r["ed"] < d]
    best = 0.0
    if len({r["ed"] for r in scored if r["ed"] < d}) >= 3:
        vals = {kap: statistics.mean(dk[kap][k][0] for k in prior) for kap in KGRID[1:]}
        kb = min(vals, key=vals.get)
        best = kb if vals[kb] < 0 else 0.0
    chosen[d] = best
    for r in scored:
        if r["ed"] == d:
            wf[r["k"]] = dk[best][r["k"]]
DD["walk-forward kappa"] = wf
print(f"walk-forward kappa by day: {chosen}")

mid = sorted({r["ed"] for r in scored})[len({r['ed'] for r in scored}) // 2]
for name, dd in DD.items():
    ks = [r for r in scored]
    eds = [r["ed"] for r in ks]
    out = [boot([dd[r["k"]][j] for r in ks], eds) for j in range(3)]
    h = [statistics.mean(dd[r["k"]][0] for r in ks if (r["ed"] < mid) == first) for first in (True, False)]
    zd = {r["k"]: Z_FULL[r["k"]] - Z_POS[r["k"]] for r in ks}
    parts = {
        "on F": [dd[r["k"]][0] for r in ks if r["side"] == 0],
        "off, tilt right": [dd[r["k"]][0] for r in ks if r["side"] != 0 and np.sign(zd[r["k"]]) == r["side"]],
        "off, tilt wrong": [dd[r["k"]][0] for r in ks if r["side"] != 0 and np.sign(zd[r["k"]]) == -r["side"]],
    }
    print(f"\n  {name:30s} dLogScore {fmt(out[0])}  CRPS {fmt(out[1])}  Brier {fmt(out[2])}  halves {h[0]:+.4f}/{h[1]:+.4f}")
    print("     " + "  ".join(f"{k} {statistics.mean(v):+.4f} (n {len(v)})" for k, v in parts.items() if v))
off = [r for r in scored if r["side"] != 0]
print(f"\nincrement direction right on off-F rows: {statistics.mean(float(np.sign(Z_FULL[r['k']] - Z_POS[r['k']]) == r['side']) for r in off):.1%} (n {len(off)});"
      f" mean |increment logit| {statistics.mean(abs(Z_FULL[r['k']] - Z_POS[r['k']]) for r in scored):.3f}")

print("\n== post-hoc (熊先生「做」, after the verdict): does the tilt change the single most likely bucket, and does the change hit more?")
for name, fn in list(ARMS.items())[:3]:
    sw = []
    for r in scored:
        p = fn(r, base_vec(r))
        top = int(np.argmax(p))
        sw.append((r, top, float(top == r["y"]), float(r["fb"] == r["y"]), float(p[top]), float(p[r["fb"]])))
    base_top_is_f = statistics.mean(float(int(np.argmax(base_vec(r))) == r["fb"]) for r in scored)
    moved = [s for s in sw if s[1] != s[0]["fb"]]
    d = [s[2] - s[3] for s in sw]
    m, lo, hi = boot(d, [s[0]["ed"] for s in sw])
    line = (f"  {name:22s} base top = F bucket {base_top_is_f:.1%}; tilted top moves off F on {len(moved)}/{len(sw)} ({len(moved) / len(sw):.1%})"
            f"; hit tilted top {statistics.mean(s[2] for s in sw):.1%} vs F {statistics.mean(s[3] for s in sw):.1%} ({m:+.3f} [{lo:+.3f}, {hi:+.3f}])")
    if moved:
        line += (f"\n     on the moved rows: new top hits {statistics.mean(s[2] for s in moved):.1%}, F would hit {statistics.mean(s[3] for s in moved):.1%};"
                 f" to F+1 {sum(s[1] > s[0]['fb'] for s in moved)}, to F-1 {sum(s[1] < s[0]['fb'] for s in moved)};"
                 f" median p(new top) {statistics.median(s[4] for s in moved):.3f} vs p(F) {statistics.median(s[5] for s in moved):.3f}")
    print(line)
# how far the shoulders sit below F under the base, and what tilt would be needed to flip
gap = [base_vec(r)[r["fb"]] - max(base_vec(r)[r["fb"] - 1] if r["fb"] > 0 else 0, base_vec(r)[r["fb"] + 1] if r["fb"] + 1 < len(r["edges"]) else 0) for r in scored]
print(f"  base: p(F) minus the larger shoulder, median {statistics.median(gap):.3f} (p(F) median {statistics.median(base_vec(r)[r['fb']] for r in scored):.3f})")
for name in ("kappa 1 (main)", "kappa 0.5"):
    fn = ARMS[name]
    for first in (True, False):
        sw = [(r, int(np.argmax(fn(r, base_vec(r))))) for r in scored if (r["ed"] < mid) == first]
        mv = [(r, t) for r, t in sw if t != r["fb"]]
        print(f"  {name:16s} {'first' if first else 'second'} half: moved {len(mv)}/{len(sw)}; on moved rows new top {statistics.mean(float(t == r['y']) for r, t in mv):.1%}"
              f" vs F {statistics.mean(float(r['fb'] == r['y']) for r, t in mv):.1%}; all rows {statistics.mean(float(t == r['y']) for r, t in sw):.1%}"
              f" vs F {statistics.mean(float(r['fb'] == r['y']) for r, t in sw):.1%}")
