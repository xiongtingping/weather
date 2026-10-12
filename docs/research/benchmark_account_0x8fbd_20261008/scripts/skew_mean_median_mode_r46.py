"""Round 46 (熊先生 10-12 「不要收口，均值相对于众数的位置呢？或者中位数」).

Does the cluster choose F, F-1 or F+1 by the SHAPE of a distribution: where its
mean (or median) sits relative to its mode?  Distributions over the market's
own ladder, read as of the cluster's first YES unless marked:
  ENS     the raw native ensembles at the trade (six global systems' member
          shares per bucket, equal weight per system)
  ENS@F   ENS's shape moved onto F (its median slid to F, mass re-binned by
          overlap, uniform within a bucket)
  P0      our served vector at the trade (only 81 picks had one)
  P0first our first P0 of the station-day (mostly after the trade; not as-of
          -- does a shape rule explain the choice at all?)
Shape readings: production ``mean_mode_skew_reading_v1`` (mean ordinal - mode
ordinal, leaning past 0.35 sigma of the distribution itself; the
``mean_median`` signal for mean - median) plus median - mode.
(a) where the pick falls relative to each distribution's mode, by skew state;
(b) on the picks one bucket off F: does each gap's sign point the right way,
    against the base rate;
(c) single-bucket rules: mode, mean bucket (``mean_center_bucket_v1``),
    median; mode / median moved one toward the mean when skewed; F moved one
    toward the distribution's skew side when |gap| > t sigma (t chosen leave
    one event day out from 0.25..1.5); all against F, by halves;
(d) the money centre - F on the gaps (C), leave one day out.
"""
import collections
import statistics
import sys
from pathlib import Path

import numpy as np

here = Path(__file__)
B = Path(sys.argv[1])
src = here.with_name("cluster_money_target_r45.py").read_text()
ns = {"__name__": "r45", "__file__": str(here.with_name("cluster_money_target_r45.py")), "sys": sys}
sys.argv = [sys.argv[0], str(B)]
exec(compile(src.split('print(f"{len(rows)} of {len(events)}')[0], "r45_head", "exec"), ns)
rows, bucket, labels_of = ns["rows"], ns["bucket"], ns["labels_of"]
conn = ns["ns"]["ns"]["conn"]
sys.path.insert(0, "/Users/xiong/weather")
from bayes_basket_target.basket_kernel.support import mean_center_bucket_v1, mean_mode_skew_reading_v1  # noqa: E402


def paired(a, b):
    w = sum(x and not y for x, y in zip(a, b))
    l_ = sum(y and not x for x, y in zip(a, b))
    n = len(a)
    d, h = (w - l_) / n, 1.96 * (w + l_) ** 0.5 / n
    return f"{d:+.3f} [{d - h:+.3f}, {d + h:+.3f}]"


def first_p0(st, ed, labels):
    row = conn.execute("SELECT history_id FROM basket_probability_bridge_history WHERE station_id=? AND event_date=? AND stage='P0'"
                       " AND state='COMMITTED' ORDER BY decision_at_utc LIMIT 1", (st, ed)).fetchone()
    if not row:
        return None
    vec = dict(conn.execute("SELECT bucket, probability FROM basket_probability_bridge_bucket_probability WHERE history_id=?", (row[0],)).fetchall())
    p = np.array([float(vec.get(lab, 0.0)) for lab in labels])
    return p / p.sum() if p.sum() > 0 else None


def recentre(p, edges, target):
    """Slide the distribution (uniform within each bucket) so its median sits at ``target``; re-bin by overlap."""
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
        a2, b2 = a + d, b + d
        for m, (c, e) in enumerate(edges):
            c_ = -1e9 if m == 0 else c
            e_ = 1e9 if m == len(edges) - 1 else e
            ov = max(0.0, min(b2, e_) - max(a2, c_))
            out[m] += p[k] * ov / (b - a)
    return out / out.sum()


def shape(p):
    probs = {str(i): float(v) for i, v in enumerate(p)}
    ords = {str(i): i for i in range(len(p))}
    mm = mean_mode_skew_reading_v1(probs, ords)
    md = mean_mode_skew_reading_v1(probs, ords, signal="mean_median")
    if not mm["readable"]:
        return None
    mode = mm["mode_ordinal"]
    median = int(np.searchsorted(np.cumsum(p), 0.5))
    return {"mode": mode, "median": min(median, len(p) - 1), "meanb": int(mean_center_bucket_v1(probs, ords)),
            "mean": mm["mean_ordinal"], "sigma": mm["sigma_buckets"], "g_mm": mm["gap_buckets"], "s_mm": mm["sign"],
            "g_md": md["gap_buckets"], "s_md": md["sign"], "g_dm": median - mode}


for r in rows:
    ev = r["ev"]
    labels = labels_of[(ev["st"], ev["ed"])]
    n = ev["n"]
    dist = {}
    if ev["ensp"]:
        p = np.array([ev["ensp"].get(j, 0.0) for j in range(n)])
        if p.sum() > 0:
            p = p / p.sum()
            dist["ENS"] = p
            dist["ENS@F"] = recentre(p, ev["edges"], r["f"])
    if ev["p0"]:
        p = np.array([ev["p0"].get(j, 0.0) for j in range(n)])
        if p.sum() > 0:
            dist["P0"] = p / p.sum()
    p = first_p0(ev["st"], ev["ed"], labels)
    if p is not None:
        dist["P0first"] = p
    r["shape"] = {k: s for k, v in dist.items() if (s := shape(v)) is not None}

days = sorted({r["ev"]["ed"] for r in rows})
mid = days[len(days) // 2]
print(f"{len(rows)} picks; distributions available: " + ", ".join(f"{k} {sum(k in r['shape'] for r in rows)}" for k in ("ENS", "ENS@F", "P0", "P0first")))

for name in ("ENS", "ENS@F", "P0", "P0first"):
    sub = [r for r in rows if name in r["shape"]]
    if not sub:
        continue
    print(f"\n== {name} (n {len(sub)})")
    st = collections.Counter(r["shape"][name]["s_mm"] for r in sub)
    print(f"  mean vs mode at 0.35 sigma: left {st[-1]}, symmetric {st[0]}, right {st[1]};  mean gap {statistics.mean(r['shape'][name]['g_mm'] for r in sub):+.2f} buckets,"
          f" sigma {statistics.mean(r['shape'][name]['sigma'] for r in sub):.2f} buckets;  mode = F's bucket {statistics.mean(r['shape'][name]['mode'] == r['ev']['fb'] for r in sub):.1%}")
    # (a) the pick relative to the mode, by skew state
    for s_ in (-1, 0, 1):
        g = [r for r in sub if r["shape"][name]["s_mm"] == s_]
        if not g:
            continue
        rel = collections.Counter(max(-2, min(2, (r["ev"]["choice"] - r["shape"][name]["mode"]) * (s_ if s_ else 1))) for r in g)
        k = len(g)
        side = "toward the mean" if s_ else "up"
        print(f"    {['left', 'symmetric', 'right'][s_ + 1]:9s} n {k:3d}: pick = mode {rel[0] / k:.0%}, one {side} {rel[1] / k:.0%}, one the other way {rel[-1] / k:.0%},"
              f" further {(rel[2] + rel[-2]) / k:.0%};  pick = F {statistics.mean(r['ev']['choice'] == r['ev']['fb'] for r in g):.0%}")
    # (b) direction on one-off picks
    one = [r for r in sub if abs(r["ev"]["choice"] - r["ev"]["fb"]) == 1]
    for gname, key in (("mean - mode", "g_mm"), ("median - mode", "g_dm"), ("mean - median", "g_md"), ("mean - F bucket", None), ("median - F bucket", "med_f")):
        told = []
        for r in one:
            s = r["shape"][name]
            v = (s["mean"] - r["ev"]["fb"]) if key is None else (s["median"] - r["ev"]["fb"]) if key == "med_f" else s[key]
            if v != 0:
                told.append((v, r))
        if not told:
            continue
        right = sum((v > 0) == (r["ev"]["choice"] > r["ev"]["fb"]) for v, r in told)
        ups = sum(r["ev"]["choice"] > r["ev"]["fb"] for _v, r in told)
        print(f"    direction by {gname:17s} on {len(told):3d} one-off picks: {right / len(told):.1%} (base {max(ups, len(told) - ups) / len(told):.1%})")
    # (c) single-bucket rules
    base = [r["ev"]["fb"] == r["ev"]["choice"] for r in sub]

    def rule_line(label, picks):
        same = [p == r["ev"]["choice"] for p, r in zip(picks, sub)]
        h1 = [s for s, r in zip(same, sub) if r["ev"]["ed"] < mid]
        h2 = [s for s, r in zip(same, sub) if r["ev"]["ed"] >= mid]
        tr = [(p, r) for p, r in zip(picks, sub) if r["ev"]["truth"] is not None]
        print(f"    {label:40s} = cluster {statistics.mean(same):.1%} vs F {statistics.mean(base):.1%} {paired(same, base)}"
              f" (halves {statistics.mean(h1) if h1 else 0:.1%}/{statistics.mean(h2) if h2 else 0:.1%}); hits {statistics.mean(p == r['ev']['truth'] for p, r in tr):.1%}")

    clip = lambda j, r: max(0, min(r["ev"]["n"] - 1, j))  # noqa: E731
    rule_line("mode", [r["shape"][name]["mode"] for r in sub])
    rule_line("mean bucket", [r["shape"][name]["meanb"] for r in sub])
    rule_line("median bucket", [r["shape"][name]["median"] for r in sub])
    rule_line("mode, one toward the mean if skewed", [clip(r["shape"][name]["mode"] + r["shape"][name]["s_mm"], r) for r in sub])
    rule_line("median, one toward the mean if skewed", [clip(r["shape"][name]["median"] + r["shape"][name]["s_md"], r) for r in sub])
    rule_line("F, one toward the skew side (0.35 sigma)", [clip(r["ev"]["fb"] + r["shape"][name]["s_mm"], r) for r in sub])
    # threshold chosen leave one day out
    TS = (0.25, 0.35, 0.5, 0.75, 1.0, 1.5)

    def f_shift(r, t, key="g_mm"):
        s = r["shape"][name]
        g = s[key]
        return clip(r["ev"]["fb"] + (int(np.sign(g)) if abs(g) >= t * s["sigma"] and s["sigma"] > 0 else 0), r)

    for key, label in (("g_mm", "mean-mode"), ("g_md", "mean-median"), ("g_dm", "median-mode")):
        picks = []
        chosen = collections.Counter()
        for r in sub:
            tr = [x for x in sub if x["ev"]["ed"] != r["ev"]["ed"]]
            best = max(TS, key=lambda t: (sum(f_shift(x, t, key) == x["ev"]["choice"] for x in tr), t))
            chosen[best] += 1
            picks.append(f_shift(r, best, key))
        rule_line(f"F, one toward {label} side, t by LODO", picks)
        print(f"      (t chosen: {dict(chosen)})")

# (d) the money centre - F on the gaps (C), leave one day out, for each as-of distribution
print("\n(d) money centre (1 h) - F on the shape gaps in C, leave one event day out")
for name in ("ENS", "ENS@F", "P0first"):
    sub = [r for r in rows if name in r["shape"] and "1 h" in r["win"]]
    w = np.array([r["ev"]["edges"][r["ev"]["fb"]][1] - r["ev"]["edges"][r["ev"]["fb"]][0] for r in sub])
    X = np.array([[r["shape"][name]["g_mm"], r["shape"][name]["g_dm"], r["shape"][name]["g_md"],
                   r["shape"][name]["mean"] - r["ev"]["fb"], r["shape"][name]["mode"] - r["ev"]["fb"]] for r in sub]) * w[:, None]
    y = np.array([r["win"]["1 h"]["centre"] - r["f"] for r in sub])
    A = np.c_[X, np.ones(len(X))]
    oos = np.zeros(len(sub))
    for d in days:
        tr = np.array([r["ev"]["ed"] != d for r in sub])
        if (~tr).sum() == 0:
            continue
        P = 2.0 * np.eye(A.shape[1])
        P[-1, -1] = 0
        b = np.linalg.solve(A[tr].T @ A[tr] + P, A[tr].T @ y[tr])
        oos[~tr] = A[~tr] @ b
    P = 2.0 * np.eye(A.shape[1])
    P[-1, -1] = 0
    b_all = np.linalg.solve(A.T @ A + P, A.T @ y)
    got = [bucket(r["ev"]["edges"], r["f"] + oos[i]) == r["ev"]["choice"] for i, r in enumerate(sub)]
    base = [r["ev"]["fb"] == r["ev"]["choice"] for r in sub]
    print(f"  {name:8s} n {len(sub)}: coefficients (mean-mode, median-mode, mean-median, mean-F, mode-F per C) {np.round(b_all[:-1], 3).tolist()};"
          f" centre MAE {np.abs(y - oos).mean():.3f} vs F {np.abs(y).mean():.3f}; = first pick {statistics.mean(got):.1%} vs F {statistics.mean(base):.1%} {paired(got, base)}")

# (e) the two asymmetries above, checked directly.
print("\n(e) when a distribution's median / mean bucket differs from F's bucket: where the pick goes")
for name in ("ENS@F", "P0", "P0first"):
    for key, label in (("median", "median"), ("meanb", "mean bucket")):
        g = [r for r in rows if name in r["shape"] and r["shape"][name][key] != r["ev"]["fb"]]
        if not g:
            continue
        k = len(g)
        at_f = sum(r["ev"]["choice"] == r["ev"]["fb"] for r in g)
        at_x = sum(r["ev"]["choice"] == r["shape"][name][key] for r in g)
        tr = [r for r in g if r["ev"]["truth"] is not None]
        print(f"  {name:8s} {label:11s} != F on {k:3d}: pick = F {at_f / k:.0%}, = the {label} {at_x / k:.0%}, elsewhere {(k - at_f - at_x) / k:.0%};"
              f" truth in F {statistics.mean(r['ev']['truth'] == r['ev']['fb'] for r in tr):.0%}, in the {label} {statistics.mean(r['ev']['truth'] == r['shape'][name][key] for r in tr):.0%}")
print("\n(e2) F + 1 only when the distribution reads right-skewed (0.35 sigma), else F")
for name in ("ENS", "ENS@F", "P0first"):
    sub = [r for r in rows if name in r["shape"]]
    picks = [min(r["ev"]["n"] - 1, r["ev"]["fb"] + (1 if r["shape"][name]["s_mm"] > 0 else 0)) for r in sub]
    same = [p == r["ev"]["choice"] for p, r in zip(picks, sub)]
    base = [r["ev"]["fb"] == r["ev"]["choice"] for r in sub]
    h1 = [s for s, r in zip(same, sub) if r["ev"]["ed"] < mid]
    h2 = [s for s, r in zip(same, sub) if r["ev"]["ed"] >= mid]
    tr = [(p, r) for p, r in zip(picks, sub) if r["ev"]["truth"] is not None]
    print(f"  {name:8s} n {len(sub)}: = cluster {statistics.mean(same):.1%} vs F {statistics.mean(base):.1%} {paired(same, base)} (halves {statistics.mean(h1):.1%}/{statistics.mean(h2):.1%});"
          f" hits {statistics.mean(p == r['ev']['truth'] for p, r in tr):.1%} vs F {statistics.mean(r['ev']['fb'] == r['ev']['truth'] for _p, r in tr):.1%}")

# (e3) the cluster splits between F and P0's centre: a blend bucket(w F + (1 - w) P0 mean C), w chosen leave one day out.
print("\n(e3) bucket(w F + (1 - w) x the distribution's mean in C), w by leave one event day out")
WS = [i / 10 for i in range(11)]
for name in ("ENS@F", "P0first"):
    sub = [r for r in rows if name in r["shape"]]
    for r in sub:
        cen = np.array([(a + b) / 2 for a, b in r["ev"]["edges"]])
        vec = None
        if name == "P0first":
            vec = first_p0(r["ev"]["st"], r["ev"]["ed"], labels_of[(r["ev"]["st"], r["ev"]["ed"])])
        else:
            p = np.array([r["ev"]["ensp"].get(j, 0.0) for j in range(r["ev"]["n"])])
            vec = recentre(p / p.sum(), r["ev"]["edges"], r["f"])
        r[f"mean_c_{name}"] = float(vec @ cen)

    def blend(r, w):
        return bucket(r["ev"]["edges"], w * r["f"] + (1 - w) * r[f"mean_c_{name}"])

    picks, chosen = [], collections.Counter()
    for r in sub:
        tr = [x for x in sub if x["ev"]["ed"] != r["ev"]["ed"]]
        best = max(WS, key=lambda w: (sum(blend(x, w) == x["ev"]["choice"] for x in tr), w))
        chosen[best] += 1
        picks.append(blend(r, best))
    same = [p == r["ev"]["choice"] for p, r in zip(picks, sub)]
    base = [r["ev"]["fb"] == r["ev"]["choice"] for r in sub]
    h1 = [s for s, r in zip(same, sub) if r["ev"]["ed"] < mid]
    h2 = [s for s, r in zip(same, sub) if r["ev"]["ed"] >= mid]
    fixed = {w: statistics.mean(blend(r, w) == r["ev"]["choice"] for r in sub) for w in (0.0, 0.25, 0.5, 0.75, 1.0)}
    print(f"  {name:8s} n {len(sub)}: LODO = cluster {statistics.mean(same):.1%} vs F {statistics.mean(base):.1%} {paired(same, base)} (halves {statistics.mean(h1):.1%}/{statistics.mean(h2):.1%}),"
          f" w chosen {dict(chosen)};  fixed w: " + ", ".join(f"{w:.2f} {v:.1%}" for w, v in fixed.items()))
