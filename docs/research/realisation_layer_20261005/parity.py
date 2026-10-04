#!/usr/bin/env python3
"""The production kernel (bayes_basket_target.realisation_layer) against the
research arm M5 it was read on: same decisions, same walk-forward days."""
import gzip, json, os, pickle, sys, time
from datetime import datetime, timedelta
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, ROOT)
from bayes_basket_target import realisation_layer as R

SRC = os.path.join(ROOT, "docs/research/final_vector_acceptance_20261004/decisions.jsonl.gz")
t0 = time.time()
decisions = []
with gzip.open(SRC, "rt") as fh:
    for line in fh:
        r = json.loads(line)
        rows = sorted(r["b"], key=lambda x: x[1])
        names = [x[0] for x in rows]
        if r["q"] is None or any(x[2] is None for x in rows) or r["y"] not in names:
            continue
        p_raw = [float(x[2]) for x in rows]
        if not sum(p_raw) > 0:
            continue
        f = R.features(p_raw, [float(r["q"][n]) for n in names])
        decisions.append({**f, "y": names.index(r["y"]), "st": r["st"], "d": r["d"]})
counts = {}
for d in decisions:
    counts[(d["st"], d["d"])] = counts.get((d["st"], d["d"]), 0) + 1
for d in decisions:
    d["weight"] = 1.0 / counts[(d["st"], d["d"])]
print(f"features for {len(decisions)} decisions in {time.time()-t0:.0f}s")
m5 = pickle.load(open(os.path.join(HERE, "fits", "M5.pkl"), "rb"))
days = np.array([d["d"] for d in decisions])
worst_theta, worst_b = 0.0, 0.0
research_index = {}
# the research frame's decision order is the same filter over the same file
for day in sorted(m5["theta"])[-3:]:
    cut = (datetime.fromisoformat(day) - timedelta(days=2)).date().isoformat()
    train = [d for d, dd in zip(decisions, days) if dd <= cut]
    arrays = R.stack(train)
    t1 = time.time()
    theta = R.fit(arrays)
    worst_theta = max(worst_theta, float(np.abs(theta - np.array(m5["theta"][day])).max()))
    test = [i for i, dd in enumerate(days) if dd == day]
    for i in test:
        b = R.belief(decisions[i]["terms"], theta)
        worst_b = max(worst_b, float(np.abs(b - m5["b"][i]).max()))
    print(f"{day}: production theta {np.round(theta, 4)}  research {np.round(m5['theta'][day], 4)}  fit {time.time()-t1:.1f}s")
print(f"max |theta diff| {worst_theta:.2e}   max |b diff| {worst_b:.2e}")
