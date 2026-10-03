#!/usr/bin/env python3
"""Exploratory (after the protocol's verdict, not evidence): is data B's
failure the family-count mix?  Per-station lambda trained and scored on
5-6-family decisions only, and the persistence of each station's preferred
lambda between 08-01..09-15 and 09-16..10-01.  Writes results_explore_b.json."""
import json, os, sys
from collections import defaultdict
from datetime import date, datetime, timedelta
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(HERE))))
from bayes_basket_target import center_width_store as store  # noqa: E402

rows = []
for n in sorted(os.listdir(os.path.join(HERE, "rows_b"))):
    rows += json.load(open(os.path.join(HERE, "rows_b", n)))
rows = [r for r in rows if r["nf"] >= 5]
G = sorted(rows[0]["s"], key=float); i75 = G.index("0.75")
out = {"rows": len(rows)}


def per_station(sub):
    acc = defaultdict(lambda: defaultdict(list))
    for r in sub:
        acc[r["station"]][r["day"]].append([r["s"][g][0] for g in G])
    return {s: (np.mean([np.mean(v, axis=0) for v in d.values()], axis=0), len(d)) for s, d in acc.items()}


h1 = per_station([r for r in rows if "2026-08-01" <= r["day"] <= "2026-09-15"])
h2 = per_station([r for r in rows if "2026-09-16" <= r["day"] <= "2026-10-01"])
com = [s for s in h1 if s in h2 and h1[s][1] >= 10 and h2[s][1] >= 8]
best = lambda h, s: np.log(float(G[int(np.argmin(h[s][0]))]))  # noqa: E731
rel = lambda h, s, g: h[s][0][G.index(g)] - h[s][0][i75]  # noqa: E731
out["persistence"] = {
    "stations": len(com),
    "corr_log_best_lambda": float(np.corrcoef([best(h1, s) for s in com], [best(h2, s) for s in com])[0, 1]),
    "corr_dlog_at_0.9": float(np.corrcoef([rel(h1, s, "0.9") for s in com], [rel(h2, s, "0.9") for s in com])[0, 1]),
    "corr_dlog_at_0.65": float(np.corrcoef([rel(h1, s, "0.65") for s in com], [rel(h2, s, "0.65") for s in com])[0, 1]),
}
station = np.array([r["station"] for r in rows]); day = np.array([r["day"] for r in rows])
known = np.array([datetime.fromisoformat(r["known"]).timestamp() for r in rows])
S = np.array([[r["s"][g] for g in G] for r in rows])
score_days = sorted({d for d in day if "2026-09-01" <= d <= "2026-10-01"})
picks = {0: {}, 30: {}}
for d in score_days:
    cut = (date.fromisoformat(d) - timedelta(days=2)).isoformat(); ts = datetime.fromisoformat(d + "T00:00:00+00:00").timestamp()
    prior = (day <= cut) & (known <= ts); fleet = S[prior, :, 0].mean(axis=0)
    for s in set(station):
        m = prior & (station == s); n = len(set(day[m]))
        for k in picks:
            picks[k][(s, d)] = "0.75" if n < 20 else G[int(np.argmin((n * S[m, :, 0].mean(axis=0) + k * fleet) / (n + k)))]
idx = defaultdict(list)
for i, (s, d) in enumerate(zip(station, day)):
    if "2026-09-01" <= d <= "2026-10-01":
        idx[(s, d)].append(i)
keys = sorted(idx)
base = {k: tuple(S[idx[k], i75, :].mean(axis=0)) for k in keys}
out["nf5_6_only_arms"] = {}
for kap in picks:
    cand = {k: tuple(S[idx[k], G.index(picks[kap][k]), :].mean(axis=0)) for k in keys}
    g = store._proper_score_gate([base[k] for k in keys], [cand[k] for k in keys], keys)
    out["nf5_6_only_arms"][f"kappa_{kap}"] = {f: g[f] for f in ("station_days", "d_logscore", "ci_logscore", "d_brier", "ci_brier", "d_crps", "ci_crps", "lower")}
json.dump(out, open(os.path.join(HERE, "results_explore_b.json"), "w"), indent=1)
print(json.dumps(out, indent=1))
