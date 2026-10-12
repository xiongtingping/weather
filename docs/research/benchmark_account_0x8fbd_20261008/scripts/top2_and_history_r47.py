"""Round 47 (熊先生 10-12 「最高概率桶和次高概率桶呢？历史数据呢」).

(47A) The highest and the second-highest bucket of a distribution: does the
cluster choose between them, and when the second?  Distributions as round 46
(P0first = our first P0, mostly after the trade; P0 = served at the trade;
ENS@F = the raw ensembles' shape moved onto F; ENS = the raw ensembles).
Share of picks on top1 / top2 / either; by the ratio p2/p1 and by the side
the second sits on; rules "top2 when p2/p1 >= r" (optionally only when top2
is the warmer one), and "F, moved to top2's side when top1 is F's bucket and
p2/p1 >= r", r chosen leave one event day out; all against F, by halves.
(47B) History known at the listing (settled days <= D-3):
  ladder history  the station's mean (truth - ladder centre) over its earlier
                  days -> predictor L + that offset (Polymarket's ladder bias
                  learned per station)
  recent truth    the mean settled high of the last 3 / 7 days, and the trend
                  (last 3 - the 4 before)
  own errors      the cluster's own earlier first picks at the station: mean
                  (truth - pick centre) -- a bot correcting itself
Direction on one-off picks against the base rate, single-bucket rules, and
the money centre - F regressed on each (leave one event day out).
"""
import collections
import json
import statistics
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np

here = Path(__file__)
B = Path(sys.argv[1])
src = here.with_name("skew_mean_median_mode_r46.py").read_text()
ns = {"__name__": "r46", "__file__": str(here.with_name("skew_mean_median_mode_r46.py")), "sys": sys}
sys.argv = [sys.argv[0], str(B)]
exec(compile(src.split("days = sorted({r[\"ev\"][\"ed\"] for r in rows})")[0], "r46_head", "exec"), ns)
rows, bucket, paired, first_p0, recentre = (ns[k] for k in ("rows", "bucket", "paired", "first_p0", "recentre"))
r45 = ns["ns"]
conn = r45["ns"]["ns"]["conn"]
ALL_TRUTHS = r45["ns"]["ns"]["ALL_TRUTHS"]
TRUTH = {(st, date.fromisoformat(d)): v for (st, d), v in ALL_TRUTHS.items()}
days = sorted({r["ev"]["ed"] for r in rows})
mid = days[len(days) // 2]
base_all = [r["ev"]["fb"] == r["ev"]["choice"] for r in rows]

# Distribution vectors per pick (as round 46).
for r in rows:
    ev = r["ev"]
    n = ev["n"]
    vec = {}
    if ev["ensp"]:
        p = np.array([ev["ensp"].get(j, 0.0) for j in range(n)])
        if p.sum() > 0:
            vec["ENS"] = p / p.sum()
            vec["ENS@F"] = recentre(p / p.sum(), ev["edges"], r["f"])
    if ev["p0"]:
        p = np.array([ev["p0"].get(j, 0.0) for j in range(n)])
        if p.sum() > 0:
            vec["P0"] = p / p.sum()
    p = first_p0(ev["st"], ev["ed"], r45["labels_of"][(ev["st"], ev["ed"])])
    if p is not None:
        vec["P0first"] = p
    r["vec"] = vec


def line(label, sub, picks):
    same = [p == r["ev"]["choice"] for p, r in zip(picks, sub)]
    base = [r["ev"]["fb"] == r["ev"]["choice"] for r in sub]
    h1 = [s for s, r in zip(same, sub) if r["ev"]["ed"] < mid]
    h2 = [s for s, r in zip(same, sub) if r["ev"]["ed"] >= mid]
    tr = [(p, r) for p, r in zip(picks, sub) if r["ev"]["truth"] is not None]
    print(f"    {label:48s} = cluster {statistics.mean(same):.1%} vs F {statistics.mean(base):.1%} {paired(same, base)}"
          f" (halves {statistics.mean(h1):.1%}/{statistics.mean(h2):.1%}); hits {statistics.mean(p == r['ev']['truth'] for p, r in tr):.1%}"
          f" vs F {statistics.mean(r['ev']['fb'] == r['ev']['truth'] for _p, r in tr):.1%}")


def lodo(sub, grid, rule):
    picks, chosen = [], collections.Counter()
    for r in sub:
        tr = [x for x in sub if x["ev"]["ed"] != r["ev"]["ed"]]
        best = max(grid, key=lambda g: (sum(rule(x, g) == x["ev"]["choice"] for x in tr), -abs(g - grid[-1])))
        chosen[best] += 1
        picks.append(rule(r, best))
    return picks, dict(chosen)


print("(47A) highest and second-highest bucket")
RS = [0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 1.01]
for name in ("P0first", "P0", "ENS@F", "ENS"):
    sub = [r for r in rows if name in r["vec"]]
    for r in sub:
        p = r["vec"][name]
        order = np.argsort(-p, kind="stable")
        r["t1"], r["t2"] = int(order[0]), int(order[1])
        r["ratio"] = float(p[order[1]] / p[order[0]]) if p[order[0]] > 0 else 0.0
    k = len(sub)
    c = collections.Counter("top1" if r["ev"]["choice"] == r["t1"] else "top2" if r["ev"]["choice"] == r["t2"] else "other" for r in sub)
    adj = statistics.mean(abs(r["t1"] - r["t2"]) == 1 for r in sub)
    print(f"\n  == {name} (n {k}): pick = top1 {c['top1'] / k:.1%}, = top2 {c['top2'] / k:.1%}, either {(c['top1'] + c['top2']) / k:.1%};"
          f" top1 = F's bucket {statistics.mean(r['t1'] == r['ev']['fb'] for r in sub):.1%}; top2 next to top1 {adj:.0%}")
    for lo_, hi_ in ((0.0, 0.5), (0.5, 0.75), (0.75, 0.9), (0.9, 1.01)):
        g = [r for r in sub if lo_ <= r["ratio"] < hi_]
        if not g:
            continue
        m = len(g)
        print(f"    p2/p1 {lo_:.2f}-{hi_:.2f} n {m:3d}: pick top1 {sum(r['ev']['choice'] == r['t1'] for r in g) / m:.0%},"
              f" top2 {sum(r['ev']['choice'] == r['t2'] for r in g) / m:.0%}, F {sum(r['ev']['choice'] == r['ev']['fb'] for r in g) / m:.0%};"
              f" truth top1 {statistics.mean(r['ev']['truth'] == r['t1'] for r in g if r['ev']['truth'] is not None):.0%}"
              f" top2 {statistics.mean(r['ev']['truth'] == r['t2'] for r in g if r['ev']['truth'] is not None):.0%}")
    for side, lab in ((1, "warmer"), (-1, "colder")):
        g = [r for r in sub if np.sign(r["t2"] - r["t1"]) == side]
        if g:
            m = len(g)
            print(f"    top2 {lab} than top1 n {m:3d}: pick top1 {sum(r['ev']['choice'] == r['t1'] for r in g) / m:.0%}, top2 {sum(r['ev']['choice'] == r['t2'] for r in g) / m:.0%}")
    line("top1", sub, [r["t1"] for r in sub])
    line("top2", sub, [r["t2"] for r in sub])
    picks, ch = lodo(sub, RS, lambda r, g: r["t2"] if r["ratio"] >= g else r["t1"])
    line(f"top2 if p2/p1 >= r else top1 (r {ch})", sub, picks)
    picks, ch = lodo(sub, RS, lambda r, g: r["t2"] if (r["ratio"] >= g and r["t2"] > r["t1"]) else r["t1"])
    line(f"top2 if warmer and p2/p1 >= r (r {ch})", sub, picks)
    clip = lambda j, r: max(0, min(r["ev"]["n"] - 1, j))  # noqa: E731
    picks, ch = lodo(sub, RS, lambda r, g: clip(r["ev"]["fb"] + int(np.sign(r["t2"] - r["t1"])), r)
                     if (r["t1"] == r["ev"]["fb"] and r["ratio"] >= g) else r["ev"]["fb"])
    line(f"F, to top2's side if top1 = F and p2/p1 >= r (r {ch})", sub, picks)
    picks, ch = lodo(sub, RS, lambda r, g: clip(r["ev"]["fb"] + 1, r)
                     if (r["t1"] == r["ev"]["fb"] and r["t2"] > r["t1"] and r["ratio"] >= g) else r["ev"]["fb"])
    line(f"F+1 if top1 = F, top2 warmer, p2/p1 >= r (r {ch})", sub, picks)

# ------------------------------------------------------------------ (47B) history
print("\n(47B) history known at the listing")
_LAD = {}


def ladder_centre(st, d):
    key = (st, d)
    if key not in _LAD:
        rows_ = conn.execute("SELECT outcome, registry_json, settlement_unit FROM basket_target_market_registry"
                             " WHERE station_id=? AND event_date=? ORDER BY refreshed_at DESC LIMIT 60", (st, d.isoformat())).fetchall()
        lows, highs = [], []
        seen = set()
        for outcome, rj, unit in rows_:
            if outcome in seen:
                continue
            seen.add(outcome)
            try:
                j = json.loads(rj)
            except ValueError:
                continue
            f_unit = str(unit).upper().startswith("F")
            conv = (lambda v: (v - 32.0) * 5.0 / 9.0) if f_unit else (lambda v: v)
            if j.get("lower") is None and j.get("upper") is not None:
                lows.append(conv(float(j["upper"]) + 0.5))
            if j.get("upper") is None and j.get("lower") is not None:
                highs.append(conv(float(j["lower"]) - 0.5))
        _LAD[key] = (lows[0] + highs[0]) / 2.0 if lows and highs else None
    return _LAD[key]


picks_by_st = collections.defaultdict(list)
for r in sorted(rows, key=lambda r: r["ev"]["ts"]):
    picks_by_st[r["ev"]["st"]].append(r)
for r in rows:
    ev = r["ev"]
    st, D = ev["st"], date.fromisoformat(ev["ed"])
    cut = D - timedelta(days=3)
    hist = [(d, TRUTH[(st, d)]) for d in (D - timedelta(days=k) for k in range(3, 75)) if (st, d) in TRUTH]
    offs = [TRUTH[(st, d)] - L for d, _t in hist if (L := ladder_centre(st, d)) is not None]
    r["n_ladder_hist"] = len(offs)
    r["ladder_hist"] = (ladder_centre(st, D) + statistics.mean(offs)) if (offs and len(offs) >= 3 and ladder_centre(st, D) is not None) else None
    recent = [t for d, t in sorted(hist, reverse=True)]
    r["recent3"] = statistics.mean(recent[:3]) if len(recent) >= 3 else None
    r["recent7"] = statistics.mean(recent[:7]) if len(recent) >= 7 else None
    r["trend"] = (statistics.mean(recent[:3]) - statistics.mean(recent[3:7])) if len(recent) >= 7 else None
    errs = []
    for x in picks_by_st[st]:
        dx = date.fromisoformat(x["ev"]["ed"])
        if dx <= cut and (st, dx) in TRUTH and x["ev"]["ts"] < ev["ts"]:
            lo, hi = x["ev"]["edges"][x["ev"]["choice"]]
            errs.append(TRUTH[(st, dx)] - (lo + hi) / 2)
    r["own_err"] = statistics.mean(errs) if len(errs) >= 2 else None
    r["n_own"] = len(errs)

FEATS = {
    "ladder centre + station history - F": lambda r: None if r["ladder_hist"] is None else r["ladder_hist"] - r["f"],
    "recent 3-day truth - F": lambda r: None if r["recent3"] is None else r["recent3"] - r["f"],
    "recent 7-day truth - F": lambda r: None if r["recent7"] is None else r["recent7"] - r["f"],
    "trend (last 3 - 4 before)": lambda r: r["trend"],
    "cluster's own past error": lambda r: r["own_err"],
}
one = [r for r in rows if abs(r["ev"]["choice"] - r["ev"]["fb"]) == 1]
for name, fn in FEATS.items():
    have = [r for r in rows if fn(r) is not None]
    told = [(fn(r), r) for r in one if fn(r) not in (None, 0.0)]
    right = sum((v > 0) == (r["ev"]["choice"] > r["ev"]["fb"]) for v, r in told)
    ups = sum(r["ev"]["choice"] > r["ev"]["fb"] for _v, r in told)
    print(f"  {name:38s} available {len(have):3d}; direction on {len(told):3d} one-off picks {right / max(1, len(told)):.1%} (base {max(ups, len(told) - ups) / max(1, len(told)):.1%})")
sub = [r for r in rows if r["ladder_hist"] is not None]
line("bucket(ladder centre + station history)", sub, [bucket(r["ev"]["edges"], r["ladder_hist"]) for r in sub])
sub = [r for r in rows if r["recent7"] is not None]
line("bucket(recent 7-day truth)", sub, [bucket(r["ev"]["edges"], r["recent7"]) for r in sub])
sub = [r for r in rows if r["own_err"] is not None]
line("bucket(F + cluster's own past error)", sub, [bucket(r["ev"]["edges"], r["f"] + r["own_err"]) for r in sub])

# Money centre - F on all history features (missing -> 0 + flag), leave one event day out; with and without them.
sub = [r for r in rows if "1 h" in r["win"]]
y = np.array([r["win"]["1 h"]["centre"] - r["f"] for r in sub])


def design(r):
    out = []
    for fn in FEATS.values():
        v = fn(r)
        out += [0.0 if v is None else v, 1.0 if v is None else 0.0]
    return out


X = np.array([design(r) for r in sub])
A = np.c_[X, np.ones(len(X))]
oos = np.zeros(len(sub))
for d in days:
    tr = np.array([r["ev"]["ed"] != d for r in sub])
    P = 2.0 * np.eye(A.shape[1])
    P[-1, -1] = 0
    b = np.linalg.solve(A[tr].T @ A[tr] + P, A[tr].T @ y[tr])
    oos[~tr] = A[~tr] @ b
P = 2.0 * np.eye(A.shape[1])
P[-1, -1] = 0
b_all = np.linalg.solve(A.T @ A + P, A.T @ y)
got = [bucket(r["ev"]["edges"], r["f"] + oos[i]) == r["ev"]["choice"] for i, r in enumerate(sub)]
base = [r["ev"]["fb"] == r["ev"]["choice"] for r in sub]
print(f"  money centre - F on the history features: coefficients per C " + ", ".join(f"{n} {v:+.2f}" for n, v in zip(FEATS, b_all[0:-1:2]))
      + f"; centre MAE {np.abs(y - oos).mean():.3f} vs F {np.abs(y).mean():.3f}; = first pick {statistics.mean(got):.1%} vs F {statistics.mean(base):.1%} {paired(got, base)}")

# (47C) the one rule that cleared zero: F+1 when the distribution's top1 is F's bucket, top2 is the warmer
# neighbour and p2/p1 >= r.  Stability by fixed r, early picks, halves against F's own halves, paired truth hits,
# and how often it fires.
print("\n(47C) F+1 if top1 = F's bucket, top2 warmer, p2/p1 >= r -- fixed r")
for name in ("ENS@F", "P0first", "P0"):
    sub = [r for r in rows if name in r["vec"]]
    for r in sub:
        p = r["vec"][name]
        order = np.argsort(-p, kind="stable")
        r["t1"], r["t2"] = int(order[0]), int(order[1])
        r["ratio"] = float(p[order[1]] / p[order[0]]) if p[order[0]] > 0 else 0.0
    for rr in (0.5, 0.6, 0.7, 0.8, 0.9):
        fire = [r["t1"] == r["ev"]["fb"] and r["t2"] == r["ev"]["fb"] + 1 and r["ratio"] >= rr for r in sub]
        picks = [min(r["ev"]["n"] - 1, r["ev"]["fb"] + 1) if f else r["ev"]["fb"] for f, r in zip(fire, sub)]
        same = [p == r["ev"]["choice"] for p, r in zip(picks, sub)]
        base = [r["ev"]["fb"] == r["ev"]["choice"] for r in sub]
        e_s = [s for s, r in zip(same, sub) if r["ev"]["ts"] - r["ev"]["listed"] < 900]
        e_b = [s for s, r in zip(base, sub) if r["ev"]["ts"] - r["ev"]["listed"] < 900]
        h = {k: ([s for s, r in zip(same, sub) if (r["ev"]["ed"] < mid) == k], [s for s, r in zip(base, sub) if (r["ev"]["ed"] < mid) == k]) for k in (True, False)}
        tr = [(p, r) for p, r in zip(picks, sub) if r["ev"]["truth"] is not None]
        th = [p == r["ev"]["truth"] for p, r in tr]
        tb = [r["ev"]["fb"] == r["ev"]["truth"] for _p, r in tr]
        fired = [r for f, r in zip(fire, sub) if f]
        print(f"  {name:8s} r {rr:.1f}: fires {sum(fire):3d}/{len(sub)} (there pick F+1 {statistics.mean(r['ev']['choice'] == r['ev']['fb'] + 1 for r in fired) if fired else 0:.0%},"
              f" F {statistics.mean(r['ev']['choice'] == r['ev']['fb'] for r in fired) if fired else 0:.0%}); = cluster {statistics.mean(same):.1%} vs F {statistics.mean(base):.1%} {paired(same, base)};"
              f" halves {statistics.mean(h[True][0]):.1%} vs {statistics.mean(h[True][1]):.1%} / {statistics.mean(h[False][0]):.1%} vs {statistics.mean(h[False][1]):.1%};"
              f" early {statistics.mean(e_s):.1%} vs {statistics.mean(e_b):.1%}; truth hits {statistics.mean(th):.1%} vs {statistics.mean(tb):.1%} {paired(th, tb)}")
