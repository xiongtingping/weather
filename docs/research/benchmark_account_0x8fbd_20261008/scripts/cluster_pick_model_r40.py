"""Round 40 (熊先生 10-12 「太低了，继续打直至90%匹配！…上下两档的概率质量，或者方向性
信号、历史数据、统计学（均值、众数、中位数等）」).

Target: the bucket of the cluster's first YES on a station-day, placed before
the market formed (all first-trade records 09-24..10-12).  Candidates: F-2..F+2
around production F (six native systems, lambda 0.5) read strictly as of the
trade's own time.  Each candidate is scored on: adjacent-bucket mass (share of
F's corrected members, native ensemble probability, our P0 probability), F's
position relative to the candidate's centre, statistics (the candidate is the
bucket of the members' mean / corrected median / raw median / F at lambda 0,
0.3, 1 / the members' vote mode / the ensembles' mode / P0's mode / the market's
mode), direction (ECMWF momentum x side), the cluster's own history (its mean
offset from F at this station over earlier days, known before this listing),
and the market prices visible at its trade.  A conditional-logit choice model,
fitted leave-one-event-day-out; feature groups added one at a time.
"""
import collections
import gzip
import json
import math
import statistics
import sys
from pathlib import Path

import numpy as np

here = Path(__file__)
B = Path(sys.argv[1])
src = here.with_name("prod_f_backtest_r39.py").read_text()
ns = {"__name__": "r39", "__file__": str(here.with_name("prod_f_backtest_r39.py"))}
exec(compile(src.split("INSTANTS = [")[0], "r39_head", "exec"), ns)
prod_f = ns["prod_f"]
FR_ALL = json.load(gzip.open(B / "first_rows.json.gz", "rt"))


def edges_of(r):
    out = []
    for lab in r["labels"]:
        lo, hi = r["bounds"][lab]
        out.append([lo, hi])
    w = float(r.get("width") or 1.0)
    if out[0][0] is None:
        out[0][0] = out[0][1] - w
    if out[-1][1] is None:
        out[-1][1] = out[-1][0] + w
    return out


def bucket(edges, x):
    if x < edges[0][1]:
        return 0
    for i, (lo, hi) in enumerate(edges):
        if lo <= x < hi:
            return i
    return len(edges) - 1


def preformation(r):
    v = list((r.get("now") or {}).values())
    return not (v and all(x is not None for x in v) and 0.7 <= sum(v) <= 1.4)


events = []
for r in FR_ALL:
    if r["first"]["side"] != "Yes" or r["first"]["bucket"] not in r["labels"] or not preformation(r):
        continue
    e = prod_f(int(r["ts"]))["station_days"].get(f"{r['st']}|{r['ed']}")
    if not e or e.get("state") != "READY":
        continue
    edges = edges_of(r)
    width = float(r.get("width") or 1.0)
    fb = bucket(edges, e["f_c"])
    members = [v["tmax_c"] for v in e["systems"].values()]
    corrected = [v["tmax_c"] + v.get("bias_c", 0.0) for v in e["systems"].values()]
    raw_med, c = statistics.median(members), e["fleet_c"]
    corr = statistics.median(corrected) - raw_med
    centres = {
        "lam0": raw_med + c, "lam03": raw_med + c + 0.3 * corr, "lam1": raw_med + c + corr,
        "mean": statistics.mean(members) + c + 0.5 * (statistics.mean(corrected) - statistics.mean(members)),
        "rawmed": raw_med + c,
    }
    votes = collections.Counter(bucket(edges, x + c - (statistics.mean(corrected) - statistics.mean(members)) * 0.5 + 0.5 * 0)
                                for x in corrected)
    cvotes = collections.Counter(bucket(edges, x + c * 0 + (raw_med + c + 0.5 * corr - statistics.median(corrected))) for x in corrected)
    ens = collections.defaultdict(list)
    for sysv in (r.get("raw") or {}).values():
        for lab, p in (sysv.get("probs") or {}).items():
            if lab in r["labels"]:
                ens[r["labels"].index(lab)].append(float(p))
    ensp = {j: statistics.mean(v) for j, v in ens.items()}
    p0 = {r["labels"].index(l): float(p) for l, p in ((r.get("served") or {}).get("p") or {}).items() if l in r["labels"]}
    now = {r["labels"].index(l): p for l, p in (r.get("now") or {}).items() if l in r["labels"] and p is not None}
    events.append({
        "st": r["st"], "ed": r["ed"], "ts": int(r["ts"]), "listed": float(r["listed"]), "unit": r.get("unit"),
        "choice": r["labels"].index(r["first"]["bucket"]), "truth": r["labels"].index(r["truth"]) if r.get("truth") in r["labels"] else None,
        "fb": fb, "f": e["f_c"], "edges": edges, "width": width, "n": len(r["labels"]),
        "centres": {k: bucket(edges, v) for k, v in centres.items()},
        "cvotes": cvotes, "nmem": len(corrected), "ensp": ensp, "p0": p0, "now": now,
        "M": float(r.get("M") or 0.0),
        "ensmode": max(ensp, key=ensp.get) if ensp else None, "p0mode": max(p0, key=p0.get) if p0 else None,
        "votemode": max(cvotes, key=lambda j: (cvotes[j], -abs(j - fb))) if cvotes else None,
        "nowmode": max(now, key=now.get) if now else None,
    })
events.sort(key=lambda x: x["ts"])
# The cluster's own history at the station: offset from F on earlier days whose
# first YES was already placed before this listing.
for i, ev in enumerate(events):
    past = [p["choice"] - p["fb"] for p in events[:i] if p["st"] == ev["st"] and p["ts"] < ev["listed"]]
    ev["hist"] = statistics.mean(past[-5:]) if past else 0.0
    ev["hist_n"] = len(past)

GROUPS = {
    "base": ["d0", "d1", "d2"],
    "position": ["pos"],
    "adjacent mass": ["vote", "ens", "p0"],
    "statistics": ["is_lam0", "is_lam03", "is_lam1", "is_mean", "is_votemode", "is_ensmode", "is_p0mode"],
    "direction": ["mom"],
    "history": ["hist"],
    "market": ["price", "price_missing", "is_nowmode"],
}


def features(ev, j):
    d = j - ev["fb"]
    lo, hi = ev["edges"][j]
    centre = (lo + hi) / 2.0
    price = ev["now"].get(j)
    return {
        "d0": 1.0 if d == 0 else 0.0, "d1": 1.0 if abs(d) == 1 else 0.0, "d2": 1.0 if abs(d) == 2 else 0.0,
        "pos": -abs(ev["f"] - centre) / ev["width"],
        "vote": ev["cvotes"].get(j, 0) / max(1, ev["nmem"]),
        "ens": ev["ensp"].get(j, 0.0), "p0": ev["p0"].get(j, 0.0),
        "is_lam0": float(ev["centres"]["lam0"] == j), "is_lam03": float(ev["centres"]["lam03"] == j),
        "is_lam1": float(ev["centres"]["lam1"] == j), "is_mean": float(ev["centres"]["mean"] == j),
        "is_votemode": float(ev["votemode"] == j), "is_ensmode": float(ev["ensmode"] == j),
        "is_p0mode": float(ev["p0mode"] == j),
        "mom": float(np.sign(d)) * ev["M"],
        "hist": -abs(d - ev["hist"]),
        "price": float(price) if price is not None else 0.0, "price_missing": 1.0 if price is None else 0.0,
        "is_nowmode": float(ev["nowmode"] == j),
    }


def design(evs, names):
    X, Y, idx = [], [], []
    for k, ev in enumerate(evs):
        cands = [j for j in range(ev["fb"] - 2, ev["fb"] + 3) if 0 <= j < ev["n"]]
        if ev["choice"] not in cands:
            continue
        X.append(np.array([[features(ev, j)[n] for n in names] for j in cands]))
        Y.append(cands.index(ev["choice"]))
        idx.append(k)
    return X, Y, idx


def fit(X, Y, l2=0.5, iters=400, lr=0.3):
    w = np.zeros(X[0].shape[1])
    for _ in range(iters):
        g = l2 * w
        for x, y in zip(X, Y):
            u = x @ w
            p = np.exp(u - u.max()); p /= p.sum()
            g += x.T @ p - x[y]
        w -= lr * g / len(X)
    return w


def lodo(names):
    X, Y, idx = design(events, names)
    days = sorted({events[k]["ed"] for k in idx})
    hit = n = 0
    for day in days:
        tr = [i for i, k in enumerate(idx) if events[k]["ed"] != day]
        te = [i for i, k in enumerate(idx) if events[k]["ed"] == day]
        w = fit([X[i] for i in tr], [Y[i] for i in tr])
        for i in te:
            n += 1
            hit += int(np.argmax(X[i] @ w)) == Y[i]
    return hit / max(1, n), n, fit(X, Y)


all_events = len(events)
outside = sum(1 for ev in events if abs(ev["choice"] - ev["fb"]) > 2)
print(f"cluster pre-formation first YES with F ready: {all_events} station-days over {len(set(e['ed'] for e in events))} event days;"
      f" F bucket = choice {sum(ev['choice'] == ev['fb'] for ev in events) / all_events:.1%},"
      f" within one {sum(abs(ev['choice'] - ev['fb']) <= 1 for ev in events) / all_events:.1%},"
      f" beyond F+-2 {outside}")
for key in ("lam0", "lam03", "lam1", "mean"):
    print(f"   rule: bucket of {key:8s} = choice {sum(ev['centres'][key] == ev['choice'] for ev in events) / all_events:.1%}")
for key in ("votemode", "ensmode", "p0mode", "nowmode"):
    have = [ev for ev in events if ev[key] is not None]
    print(f"   rule: {key:9s} = choice {sum(ev[key] == ev['choice'] for ev in have) / max(1, len(have)):.1%} (n {len(have)})")
names = []
for group, cols in GROUPS.items():
    names = names + cols
    acc, n, w = lodo(names)
    print(f"+ {group:14s} leave-one-day-out = choice {acc:.1%} (n {n})")
acc, n, w = lodo(names)
print("weights (all groups):", {k: round(float(v), 2) for k, v in zip(names, w)})
for group, cols in GROUPS.items():
    if group == "base":
        continue
    acc_g, n_g, _ = lodo([c for c in names if c not in cols])
    print(f"   without {group:14s} {acc_g:.1%}")
