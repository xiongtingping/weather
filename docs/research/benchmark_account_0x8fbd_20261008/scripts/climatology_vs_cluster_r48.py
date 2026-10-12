"""Round 48 (熊先生 10-12 「历史数据呢」→「拉」): same-season climatology.

``iem_daily_r48.json`` (``fetch_climatology_r48.py``): IEM daily local-day
maxima 2016..2026 per station.  (0) IEM against our settlement truth on the
overlap (06-28..10-11): mean and sd of truth - IEM per station; the
climatology is shifted by each station's mean offset (only days before the
pick's own listing count for that shift).  Climatology for a pick on event
day D: the 2016..2025 highs within +-7 calendar days of D's month-day --
mean, sd, median; F's anomaly z = (F - mean)/sd; last year's same window.
(1) Direction on one-off picks against the base rate: clim - F, last year -
F, z.  (2) Single-bucket rules: bucket(clim mean); bucket(F + k (clim - F)),
k by leave one event day out; F moved one toward clim when |z| > t.
(3) The money centre - F on clim - F and z (shrinkage toward normal?),
leave one day out.  (4) Does climatology add to F on the truth (every
first-rows station-day, F at listing + 5 min)?
"""
import json
import statistics
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np

here = Path(__file__)
B = Path(sys.argv[1])
src = here.with_name("cluster_money_target_r45.py").read_text()
ns = {"__name__": "r45", "__file__": str(here.with_name("cluster_money_target_r45.py")), "sys": sys}
sys.argv = [sys.argv[0], str(B)]
exec(compile(src.split('print(f"{len(rows)} of {len(events)}')[0], "r45_head", "exec"), ns)
rows, bucket, prod_f, FR_ALL = ns["rows"], ns["bucket"], ns["prod_f"], ns["FR_ALL"]
edges_of = ns["ns"]["edges_of"]
ALL_TRUTHS = ns["ns"]["ns"]["ALL_TRUTHS"]
TRUTH = {(st, date.fromisoformat(d)): v for (st, d), v in ALL_TRUTHS.items()}
IEM = {k: v for k, v in json.load(open(B / "iem_daily_r48.json")).items() if not k.startswith("_")}


def paired(a, b):
    w = sum(x and not y for x, y in zip(a, b))
    l_ = sum(y and not x for x, y in zip(a, b))
    n = len(a)
    d, h = (w - l_) / n, 1.96 * (w + l_) ** 0.5 / n
    return f"{d:+.3f} [{d - h:+.3f}, {d + h:+.3f}]"


# (0) IEM against our truth
print("(0) IEM daily max against our settlement truth (overlap 2026-06-28..)")
diffs = {}
for (st, d), t in TRUTH.items():
    v = IEM.get(st, {}).get(d.isoformat())
    if v is not None:
        diffs.setdefault(st, []).append((d, t - v))
allv = [x for v in diffs.values() for _d, x in v]
print(f"    {len(allv)} station-days, {len(diffs)} stations: truth - IEM mean {statistics.mean(allv):+.2f} C, |diff| <= 0.5 C {np.mean(np.abs(allv) <= 0.5):.1%},"
      f" per-station mean range {min(statistics.mean(x for _d, x in v) for v in diffs.values()):+.2f}..{max(statistics.mean(x for _d, x in v) for v in diffs.values()):+.2f}")
missing = sorted(st for st in {r['ev']['st'] for r in rows} if st not in IEM or len(IEM[st]) < 1000)
print(f"    stations with thin IEM history (<1000 days): {missing}")


def offset(st, before):
    v = [x for d, x in diffs.get(st, []) if d < before]
    return statistics.mean(v) if len(v) >= 10 else 0.0


def clim(st, D, years=range(2016, 2026), half=7):
    vals = []
    for y in years:
        try:
            c = D.replace(year=y)
        except ValueError:
            c = D.replace(year=y, day=28)
        for k in range(-half, half + 1):
            v = IEM.get(st, {}).get((c + timedelta(days=k)).isoformat())
            if v is not None:
                vals.append(v)
    return vals


def features(st, D, f, listing_day):
    vals = clim(st, D)
    if len(vals) < 60:
        return None
    off = offset(st, listing_day)
    mean, sd = statistics.mean(vals) + off, statistics.pstdev(vals)
    last = clim(st, D, years=[D.year - 1], half=3)
    return {"clim": mean, "sd": sd, "z": (f - mean) / sd if sd > 0 else 0.0, "med": statistics.median(vals) + off,
            "last": statistics.mean(last) + off if len(last) >= 4 else None, "n": len(vals)}


for r in rows:
    ev = r["ev"]
    D = date.fromisoformat(ev["ed"])
    r["cl"] = features(ev["st"], D, r["f"], D - timedelta(days=2))
sub = [r for r in rows if r["cl"]]
days = sorted({r["ev"]["ed"] for r in sub})
mid = days[len(days) // 2]
print(f"\n{len(sub)} of {len(rows)} picks with a climatology (>= 60 days in the window); clim - F mean {statistics.mean(r['cl']['clim'] - r['f'] for r in sub):+.2f} C,"
      f" sd {statistics.pstdev([r['cl']['clim'] - r['f'] for r in sub]):.2f}; F's anomaly z mean {statistics.mean(r['cl']['z'] for r in sub):+.2f}")

# (1) direction
one = [r for r in sub if abs(r["ev"]["choice"] - r["ev"]["fb"]) == 1]
for name, fn in (("clim mean - F", lambda r: r["cl"]["clim"] - r["f"]), ("clim median - F", lambda r: r["cl"]["med"] - r["f"]),
                 ("last year - F", lambda r: None if r["cl"]["last"] is None else r["cl"]["last"] - r["f"]), ("-z (toward normal)", lambda r: -r["cl"]["z"])):
    told = [(fn(r), r) for r in one if fn(r) not in (None, 0.0)]
    right = sum((v > 0) == (r["ev"]["choice"] > r["ev"]["fb"]) for v, r in told)
    ups = sum(r["ev"]["choice"] > r["ev"]["fb"] for _v, r in told)
    print(f"  direction by {name:20s} on {len(told):3d} one-off picks: {right / len(told):.1%} (base {max(ups, len(told) - ups) / len(told):.1%})")


def line(label, picks, s=sub):
    same = [p == r["ev"]["choice"] for p, r in zip(picks, s)]
    base = [r["ev"]["fb"] == r["ev"]["choice"] for r in s]
    h1 = [x for x, r in zip(same, s) if r["ev"]["ed"] < mid]
    h2 = [x for x, r in zip(same, s) if r["ev"]["ed"] >= mid]
    tr = [(p, r) for p, r in zip(picks, s) if r["ev"]["truth"] is not None]
    print(f"    {label:44s} = cluster {statistics.mean(same):.1%} vs F {statistics.mean(base):.1%} {paired(same, base)}"
          f" (halves {statistics.mean(h1):.1%}/{statistics.mean(h2):.1%}); hits {statistics.mean(p == r['ev']['truth'] for p, r in tr):.1%}"
          f" vs F {statistics.mean(r['ev']['fb'] == r['ev']['truth'] for _p, r in tr):.1%}")


# (2) rules
print("  single-bucket rules:")
line("bucket(climatology mean)", [bucket(r["ev"]["edges"], r["cl"]["clim"]) for r in sub])
KS = [-0.3, -0.2, -0.1, 0.0, 0.1, 0.2, 0.3, 0.5]
picks, chosen = [], {}
for r in sub:
    tr = [x for x in sub if x["ev"]["ed"] != r["ev"]["ed"]]
    best = max(KS, key=lambda k: (sum(bucket(x["ev"]["edges"], x["f"] + k * (x["cl"]["clim"] - x["f"])) == x["ev"]["choice"] for x in tr), -abs(k)))
    chosen[best] = chosen.get(best, 0) + 1
    picks.append(bucket(r["ev"]["edges"], r["f"] + best * (r["cl"]["clim"] - r["f"])))
line(f"bucket(F + k (clim - F)), k by LODO {chosen}", picks)
for k in (0.1, 0.2, 0.3):
    line(f"bucket(F + {k} (clim - F)) fixed", [bucket(r["ev"]["edges"], r["f"] + k * (r["cl"]["clim"] - r["f"])) for r in sub])
TS = [1.0, 1.5, 2.0, 99.0]
picks, chosen = [], {}
clip = lambda j, r: max(0, min(r["ev"]["n"] - 1, j))  # noqa: E731
rule = lambda r, t: clip(r["ev"]["fb"] - int(np.sign(r["cl"]["z"])), r) if abs(r["cl"]["z"]) >= t else r["ev"]["fb"]  # noqa: E731
for r in sub:
    tr = [x for x in sub if x["ev"]["ed"] != r["ev"]["ed"]]
    best = max(TS, key=lambda t: (sum(rule(x, t) == x["ev"]["choice"] for x in tr), t))
    chosen[best] = chosen.get(best, 0) + 1
    picks.append(rule(r, best))
line(f"F, one toward normal when |z| >= t, t by LODO {chosen}", picks)

# (3) the money centre
s1 = [r for r in sub if "1 h" in r["win"]]
y = np.array([r["win"]["1 h"]["centre"] - r["f"] for r in s1])
X = np.array([[r["cl"]["clim"] - r["f"], r["cl"]["z"], 0.0 if r["cl"]["last"] is None else r["cl"]["last"] - r["f"], 1.0 if r["cl"]["last"] is None else 0.0] for r in s1])
A = np.c_[X, np.ones(len(X))]
P = 2.0 * np.eye(A.shape[1])
P[-1, -1] = 0
b_all = np.linalg.solve(A.T @ A + P, A.T @ y)
rng = np.random.default_rng(3)
bs = []
d1 = sorted({r["ev"]["ed"] for r in s1})
for _ in range(300):
    pick = rng.choice(d1, size=len(d1), replace=True)
    idx = [i for dd in pick for i, r in enumerate(s1) if r["ev"]["ed"] == dd]
    bs.append(np.linalg.solve(A[idx].T @ A[idx] + P, A[idx].T @ y[idx])[:3])
bs = np.array(bs)
oos = np.zeros(len(s1))
for dd in d1:
    trm = np.array([r["ev"]["ed"] != dd for r in s1])
    b = np.linalg.solve(A[trm].T @ A[trm] + P, A[trm].T @ y[trm])
    oos[~trm] = A[~trm] @ b
got = [bucket(r["ev"]["edges"], r["f"] + oos[i]) == r["ev"]["choice"] for i, r in enumerate(s1)]
base = [r["ev"]["fb"] == r["ev"]["choice"] for r in s1]
print("  money centre - F on climatology: " + ", ".join(f"{n} {b_all[i]:+.3f} [{np.percentile(bs[:, i], 2.5):+.3f}, {np.percentile(bs[:, i], 97.5):+.3f}]"
                                                    for i, n in enumerate(("clim - F", "z", "last year - F")))
      + f"; centre MAE {np.abs(y - oos).mean():.3f} vs F {np.abs(y).mean():.3f}; = first pick {statistics.mean(got):.1%} vs F {statistics.mean(base):.1%} {paired(got, base)}")

# (4) climatology on the truth, every first-rows station-day at listing + 5 min
allr = []
for r in FR_ALL:
    if r.get("truth_c") is None or len(r["labels"]) < 5:
        continue
    e = prod_f(int(r["listed"]) + 300)["station_days"].get(f"{r['st']}|{r['ed']}")
    if not e or e.get("state") != "READY":
        continue
    D = date.fromisoformat(r["ed"])
    cl = features(r["st"], D, e["f_c"], D - timedelta(days=2))
    if not cl:
        continue
    edges = edges_of(r)
    allr.append({"f": e["f_c"], "c": cl["clim"], "y": float(r["truth_c"]), "edges": edges, "ed": r["ed"]})
Xa = np.array([[a["c"] - a["f"], 1.0] for a in allr])
Ya = np.array([a["y"] - a["f"] for a in allr])
pa = np.zeros(len(allr))
for dd in sorted({a["ed"] for a in allr}):
    trm = np.array([a["ed"] != dd for a in allr])
    b = np.linalg.lstsq(Xa[trm], Ya[trm], rcond=None)[0]
    pa[~trm] = Xa[~trm] @ b
ball = np.linalg.lstsq(Xa, Ya, rcond=None)[0]
hit = [bucket(a["edges"], a["f"] + pa[i]) == bucket(a["edges"], a["y"]) for i, a in enumerate(allr)]
fh = [bucket(a["edges"], a["f"]) == bucket(a["edges"], a["y"]) for a in allr]
print(f"\n(4) {len(allr)} station-days on the truth: truth - F = {ball[0]:+.3f} x (clim - F) {ball[1]:+.3f}; leave one day out MAE {np.abs(Ya - pa).mean():.3f}"
      f" vs F {np.abs(Ya).mean():.3f}; hits {statistics.mean(hit):.1%} vs F {statistics.mean(fh):.1%} {paired(hit, fh)}")

# (5) does the cluster stretch F's anomaly?  Its offset from F by F's anomaly against normal.
print("\n(5) the cluster's offset from F, by F's anomaly z = (F - clim)/sd")
for lo_, hi_ in ((-9, -0.5), (-0.5, 0.0), (0.0, 0.5), (0.5, 1.0), (1.0, 9)):
    g = [r for r in s1 if lo_ <= r["cl"]["z"] < hi_]
    if not g:
        continue
    off = [r["win"]["1 h"]["centre"] - r["f"] for r in g]
    up = sum(r["ev"]["choice"] > r["ev"]["fb"] for r in g)
    dn = sum(r["ev"]["choice"] < r["ev"]["fb"] for r in g)
    tr = [r for r in g if r["ev"]["truth"] is not None]
    print(f"    z {lo_:+.1f}..{hi_:+.1f} n {len(g):3d}: centre - F {statistics.mean(off):+.2f} C; pick above F {up}, = F {len(g) - up - dn}, below {dn};"
          f" truth - F {statistics.mean(TRUTH[(r['ev']['st'], date.fromisoformat(r['ev']['ed']))] - r['f'] for r in g if (r['ev']['st'], date.fromisoformat(r['ev']['ed'])) in TRUTH):+.2f} C")
