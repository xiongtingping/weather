"""Round 63 scoring (PROTOCOL_R63_KEEP_F.md): keep F's bucket probability, move mass between the two sides by direction.

T1 = the cluster's first-pick class {<=F-2, F-1, F, F+1, >=F+2}; base = walk-forward class frequencies.
T2 = truth on the ladder; base = ENS@F (six native ensembles averaged, slid so the median sits at F), 1e-3 floor.
Verdict = event days >= 10-12 only.  Usage: keep_f_score_r63.py <bench>
"""
import gzip
import json
import math
import statistics
import sys
from pathlib import Path

import numpy as np

B = Path(sys.argv[1])
FORWARD_FROM = "2026-10-12"
FLOOR = 1e-3
KAPPAS = {"ln1.5": math.log(1.5), "ln2": math.log(2.0), "ln3": math.log(3.0)}
GRID = [0.0, math.log(1.25), math.log(1.5), math.log(2.0), math.log(3.0)]
MIN_TRAIN_DAYS = 5

R = [r for r in json.load(gzip.open(B / "rows_r63.json.gz", "rt")) if not r["formed"] and len(r["raw"]) >= 2
     and r["first"]["bucket"] in r["labels"]]


def edges_of(r):
    out = [[*r["bounds"][lab]] for lab in r["labels"]]
    if out[0][0] is None:
        out[0][0] = out[0][1] - r["width"]
    if out[-1][1] is None:
        out[-1][1] = out[-1][0] + r["width"]
    return out


def bucket(edges, x):
    if x < edges[0][1]:
        return 0
    for i, (lo, hi) in enumerate(edges):
        if lo <= x < hi:
            return i
    return len(edges) - 1


def recentre(p, edges, target):
    cdf = np.cumsum(p)
    j = int(np.searchsorted(cdf, 0.5))
    lo, hi = edges[j]
    below = cdf[j - 1] if j > 0 else 0.0
    med = lo + (0.5 - below) / max(1e-12, p[j]) * (hi - lo)
    d = target - med
    out = np.zeros(len(p))
    for k, (a, b) in enumerate(edges):
        if p[k] <= 0:
            continue
        for m, (c, e) in enumerate(edges):
            c_ = -1e9 if m == 0 else c
            e_ = 1e9 if m == len(edges) - 1 else e
            out[m] += p[k] * max(0.0, min(b + d, e_) - max(a + d, c_)) / (b - a)
    return out / out.sum()


def floor(p):
    q = np.maximum(p, FLOOR)
    return q / q.sum()


def keep_f(p, fb, d, kappa):
    if not d or kappa == 0.0:
        return p
    above, below = p[fb + 1:].sum(), p[:fb].sum()
    if above <= 0.0 or below <= 0.0:
        return p
    Rm = above + below
    a2 = Rm * above * math.exp(kappa) / (above * math.exp(kappa) + below) if d > 0 else Rm * above / (above + below * math.exp(kappa))
    q = p.copy()
    q[fb + 1:] *= a2 / above
    q[:fb] *= (Rm - a2) / below
    return q


def scores(p, y, width):
    """LogScore, CRPS (ladder RPS x bucket width, C), multi-class Brier."""
    onehot = np.zeros(len(p))
    onehot[y] = 1.0
    return np.array([-math.log(p[y]), float(((np.cumsum(p) - np.cumsum(onehot)) ** 2).sum()) * width, float(((p - onehot) ** 2).sum())])


# ------------------------------------------------------------ per-row quantities
rows = []
for r in R:
    edges = edges_of(r)
    fb = bucket(edges, r["f"])
    n = len(edges)
    fams = list(r["raw"].values())
    praw = np.array([statistics.mean(fm["probs"].get(lab, 0.0) for fm in fams) for lab in r["labels"]])
    if praw.sum() <= 0:
        continue
    ens = recentre(praw / praw.sum(), edges, r["f"])
    tilt = (ens[fb + 1] if fb + 1 < n else 0.0) - (ens[fb - 1] if fb >= 1 else 0.0)
    lo, hi = edges[fb]
    cen = list(r["f_systems"].values())
    shift = r["f"] - statistics.median(cen)
    cen = [c + shift for c in cen]
    mass = (sum(bucket(edges, c) > fb for c in cen) - sum(bucket(edges, c) < fb for c in cen)) / len(cen)
    choice = r["labels"].index(r["first"]["bucket"])
    k = choice - fb
    y = r["labels"].index(r["truth"]) if r.get("truth") in r["labels"] else None
    rows.append({"st": r["st"], "ed": r["ed"], "n": n, "fb": fb, "width": r["width"], "ens": floor(ens),
                 "x": [tilt, (r["f"] - lo) / (hi - lo) - 0.5, mass, float(r["M"] or 0.0)],
                 "M": float(r["M"] or 0.0), "cls": max(-2, min(2, k)), "cside": int(np.sign(k)),
                 "y": y, "tside": None if y is None else int(np.sign(y - fb))})
days = sorted({r["ed"] for r in rows})
print(f"rows {len(rows)} pre-formation first YES with F READY and >=2 ensembles; event days {days[0]}..{days[-1]} ({len(days)});"
      f" forward (>= {FORWARD_FROM}) {sum(r['ed'] >= FORWARD_FROM for r in rows)} rows / {sum(d >= FORWARD_FROM for d in days)} days")
cc = {c: sum(r["cls"] == c for r in rows) for c in (-2, -1, 0, 1, 2)}
print(f"cluster pick vs F: <=-2 {cc[-2]}, -1 {cc[-1]}, 0 {cc[0]}, +1 {cc[1]}, >=+2 {cc[2]}; truth on F bucket"
      f" {statistics.mean(r['y'] == r['fb'] for r in rows if r['y'] is not None):.3f}"
      f" ({sum(r['y'] is not None for r in rows)} settled)")


# ------------------------------------------------------------ directions
def fit_logit(X, y, l2=1.0, iters=600, lr=0.3):
    w = np.zeros(X.shape[1] + 1)
    Xb = np.c_[X, np.ones(len(X))]
    for _ in range(iters):
        p = 1 / (1 + np.exp(-Xb @ w))
        g = Xb.T @ (p - y) / len(X)
        g[:-1] += l2 * w[:-1] / len(X)
        w -= lr * g
    return w


def combined(side_key):
    out = {}
    for d in days:
        prior = [r for r in rows if r["ed"] < d and r[side_key]]
        if len({r["ed"] for r in prior}) < MIN_TRAIN_DAYS or len(prior) < 10:
            continue
        X = np.array([r["x"] for r in prior])
        mu, sd = X.mean(0), X.std(0)
        sd[sd < 1e-9] = 1.0
        w = fit_logit((X - mu) / sd, np.array([r[side_key] > 0 for r in prior], float))
        for i, r in enumerate(rows):
            if r["ed"] == d:
                pw = 1 / (1 + math.exp(-(np.r_[(np.array(r["x"]) - mu) / sd, 1.0] @ w)))
                out[i] = int(np.sign(pw - 0.5)) if abs(pw - 0.5) >= 0.10 else 0
    return out


def dirs(target):
    side_key = "cside" if target == "T1" else "tside"
    comb = combined(side_key)
    D = {
        "ENS tilt (main)": {i: (int(np.sign(r["x"][0])) if abs(r["x"][0]) >= 0.05 else 0) for i, r in enumerate(rows)},
        "momentum": {i: (int(np.sign(r["M"])) if abs(r["M"]) >= 0.25 * r["width"] else 0) for i, r in enumerate(rows)},
        "combined walk-forward": comb,
        "always warm": {i: 1 for i in range(len(rows))},
    }
    if target == "T2":
        D["follow the cluster"] = {i: r["cside"] for i, r in enumerate(rows)}
    D["oracle (diagnostic)"] = {i: r[side_key] for i, r in enumerate(rows) if r[side_key] is not None}
    return D


# ------------------------------------------------------------ bases
def t1_base():
    """Walk-forward five-class frequencies (+1 smoothing); cold start = research-segment frequencies, flagged."""
    research = [r for r in rows if r["ed"] < FORWARD_FROM]
    cold = np.array([sum(r["cls"] == c for r in research) + 1.0 for c in (-2, -1, 0, 1, 2)])
    out, flag = {}, {}
    for d in days:
        prior = [r for r in rows if r["ed"] < d]
        if len({r["ed"] for r in prior}) >= MIN_TRAIN_DAYS:
            v = np.array([sum(r["cls"] == c for r in prior) + 1.0 for c in (-2, -1, 0, 1, 2)])
            flag[d] = False
        else:
            v = cold
            flag[d] = True
        out[d] = v / v.sum()
    return out, flag


T1B, T1COLD = t1_base()


def case(target, i):
    """(base vector, kept index, outcome index, width) or None."""
    r = rows[i]
    if target == "T1":
        return T1B[r["ed"]], 2, r["cls"] + 2, 1.0
    if r["y"] is None:
        return None
    return r["ens"], r["fb"], r["y"], r["width"]


# ------------------------------------------------------------ bootstrap
rng = np.random.default_rng(63)


def boot(vals, ed, draws=2000):
    vals, ed = np.asarray(vals, float), np.asarray(ed)
    ud = sorted(set(ed))
    if len(ud) < 2:
        return float(vals.mean()), float("nan"), float("nan")
    by = [vals[ed == d] for d in ud]
    sums = np.array([b.sum() for b in by])
    cnts = np.array([len(b) for b in by])
    idx = rng.integers(0, len(ud), size=(draws, len(ud)))
    bs = sums[idx].sum(1) / cnts[idx].sum(1)
    return float(vals.mean()), float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5))


def fmt(t):
    p, lo, hi = t
    sig = "*" if (lo > 0 or hi < 0) else " "
    return f"{p:+.4f} [{lo:+.4f},{hi:+.4f}]{sig}"


report = {}
for target in ("T1", "T2"):
    D = dirs(target)
    print(f"\n==================== {target}: {'cluster first-pick class (5 classes)' if target == 'T1' else 'truth on the ladder (base ENS@F)'}")
    idx = [i for i in range(len(rows)) if case(target, i) is not None]
    base = {i: scores(floor(case(target, i)[0]) if target == "T2" else case(target, i)[0], case(target, i)[2], case(target, i)[3]) for i in idx}
    for arm, dmap in D.items():
        dd_k = {}
        for kname, kap in [("k0", 0.0)] + [(k, v) for k, v in KAPPAS.items()] + [("ln1.25", math.log(1.25))]:
            dd = {}
            for i in idx:
                if i not in dmap:
                    continue
                p, fb, y, w = case(target, i)
                q = keep_f(p, fb, dmap[i], kap)
                if target == "T2":
                    q = floor(q)
                dd[i] = scores(q, y, w) - base[i]
            dd_k[kap] = dd
        # walk-forward fleet kappa
        wf = {}
        for d in days:
            prior = [i for i in dd_k[0.0] if rows[i]["ed"] < d]
            best = 0.0
            if len({rows[i]["ed"] for i in prior}) >= MIN_TRAIN_DAYS:
                vals = {k: np.mean([dd_k[k][i][0] for i in prior]) for k in GRID[1:]}
                kb = min(vals, key=vals.get)
                best = kb if vals[kb] < 0 else 0.0
            for i in dd_k[0.0]:
                if rows[i]["ed"] == d:
                    wf[i] = dd_k[best][i]
        variants = {k: dd_k[v] for k, v in KAPPAS.items()}
        variants["walk-forward kappa"] = wf
        acc = [(dmap[i] == (rows[i]["cside"] if target == "T1" else rows[i]["tside"])) for i in idx
               if i in dmap and dmap[i] != 0 and (rows[i]["cside"] if target == "T1" else rows[i]["tside"]) not in (0, None)]
        active = sum(1 for i in idx if dmap.get(i, 0) != 0)
        print(f"\n  -- {arm}: active {active}/{len(idx)}; side right (rows off F, active) {np.mean(acc) if acc else float('nan'):.3f} (n {len(acc)})")
        for vname, dd in variants.items():
            out = {}
            for seg, keep in (("forward", lambda e: e >= FORWARD_FROM), ("research", lambda e: e < FORWARD_FROM), ("all", lambda e: True)):
                ii = [i for i in dd if keep(rows[i]["ed"])]
                if not ii:
                    continue
                ed = [rows[i]["ed"] for i in ii]
                out[seg] = {m: boot([dd[i][k] for i in ii], ed) for k, m in enumerate(("dlog", "dcrps", "dbrier"))} | {"n": len(ii), "days": len(set(ed))}
            f = out.get("forward")
            sideoff = rows
            ii = [i for i in dd if rows[i]["ed"] >= FORWARD_FROM]
            tsk = "cside" if target == "T1" else "tside"
            parts = {
                "on F": [dd[i][0] for i in ii if rows[i][tsk] == 0],
                "off right": [dd[i][0] for i in ii if rows[i][tsk] not in (0, None) and dmap.get(i, 0) == rows[i][tsk]],
                "off wrong": [dd[i][0] for i in ii if rows[i][tsk] not in (0, None) and dmap.get(i, 0) == -rows[i][tsk]],
            }
            mid = days[len(days) // 2]
            halves = [np.mean([dd[i][0] for i in dd if (rows[i]["ed"] < mid) == h]) for h in (True, False)]
            line = f"     {vname:18s} FORWARD n{f['n']:3d}/{f['days']}d dLog {fmt(f['dlog'])} CRPS {fmt(f['dcrps'])} Brier {fmt(f['dbrier'])}" if f else f"     {vname:18s} (no forward rows)"
            r_ = out.get("research")
            a_ = out["all"]
            line += (f" | research dLog {fmt(r_['dlog'])}" if r_ else "") + f" | all dLog {fmt(a_['dlog'])} halves {halves[0]:+.4f}/{halves[1]:+.4f}"
            line += " | fwd parts " + " ".join(f"{k} {np.mean(v):+.3f}(n{len(v)})" for k, v in parts.items() if v)
            print(line)
            report.setdefault(target, {}).setdefault(arm, {})[vname] = out
json.dump(report, open(B / "keep_f_r63.json", "w"), indent=1)
print(f"\nT1 base cold-start days (research-segment frequencies): {[d for d, v in T1COLD.items() if v]}")
