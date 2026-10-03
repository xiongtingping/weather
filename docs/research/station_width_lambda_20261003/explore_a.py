#!/usr/bin/env python3
"""Exploratory (after the verdict): does each station's preferred width
persist between 04-06 and 07-09 in data A?  Writes results_explore_a.json."""
import json, os
from collections import defaultdict
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__))
rows = []
for n in sorted(os.listdir(os.path.join(HERE, "rows_a"))):
    rows += json.load(open(os.path.join(HERE, "rows_a", n)))
G = sorted(rows[0]["s"], key=float); i75 = G.index("0.75")


def per_station(sub):
    acc = defaultdict(lambda: defaultdict(list))
    for r in sub:
        acc[r["station"]][r["day"]].append([r["s"][g][0] for g in G])
    return {s: (np.mean([np.mean(v, axis=0) for v in d.values()], axis=0), len(d)) for s, d in acc.items()}


out = {}
for label, pred in (("all", lambda r: True), ("nf2", lambda r: r["nf"] == 2)):
    h1 = per_station([r for r in rows if "2026-04-01" <= r["day"] <= "2026-06-30" and pred(r)])
    h2 = per_station([r for r in rows if "2026-07-01" <= r["day"] <= "2026-09-30" and pred(r)])
    com = [s for s in h1 if s in h2 and h1[s][1] >= 15 and h2[s][1] >= 15]
    best = lambda h, s: np.log(float(G[int(np.argmin(h[s][0]))]))  # noqa: E731
    rel = lambda h, s: h[s][0][G.index("0.9")] - h[s][0][i75]  # noqa: E731
    out[label] = {"stations": len(com),
                  "corr_log_best_lambda": float(np.corrcoef([best(h1, s) for s in com], [best(h2, s) for s in com])[0, 1]),
                  "corr_dlog_at_0.9": float(np.corrcoef([rel(h1, s) for s in com], [rel(h2, s) for s in com])[0, 1])}
json.dump(out, open(os.path.join(HERE, "results_explore_a.json"), "w"), indent=1)
print(out)
