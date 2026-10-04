#!/usr/bin/env python3
"""PROTOCOL.md, the fallback written before the readings: an isotonic map
after the layer, fitted on the prefix's own leave-one-day-out predictions,
then the ladder renormalised.

    python3 fallback_iso.py M4      # -> fits/M4i.pkl
"""
import os
import pickle
import sys
import time
from datetime import datetime, timedelta

import numpy as np
from sklearn.isotonic import IsotonicRegression

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import layer as L  # noqa: E402
from layer import A  # noqa: E402

FLOOR = 1e-4


def main(model):
    rec, base, terms, y, wt, days, _gap = L.frame()
    deltas = terms[model]
    with open(os.path.join(HERE, "fits", f"{model}.pkl"), "rb") as fh:
        fitted = pickle.load(fh)
    uniq = sorted(set(days))
    out = {"model": model + "i", "theta": fitted["theta"], "b": {}, "lo": {}, "hi": {}, "map": {}}
    t0 = time.time()
    for day in sorted(fitted["theta"]):
        cut = (datetime.fromisoformat(day) - timedelta(days=2)).date().isoformat()
        train_days = [d for d in uniq if d <= cut]
        theta = np.array(fitted["theta"][day])
        xs, ys, ws = [], [], []
        for held in train_days:
            tr = np.where((days <= cut) & (days != held))[0]
            te = np.where(days == held)[0]
            th = A.pool_fit(base[tr], [d[tr] for d in deltas], y[tr], wt[tr], start=theta)
            pred = A.pooled(base[te], [d[te] for d in deltas], th)
            for j, i in enumerate(te):
                k = rec[i]["k"]
                xs.append(pred[j, :k]); ys.append((np.arange(k) == rec[i]["y"]).astype(float)); ws.append(np.full(k, rec[i]["wt"]))
        iso = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip")
        iso.fit(np.concatenate(xs), np.concatenate(ys), sample_weight=np.concatenate(ws))
        grid = np.array([0.001, 0.01, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7])
        out["map"][day] = [float(v) for v in iso.predict(grid)]
        for i in np.where(days == day)[0]:
            i = int(i)
            b = np.maximum(iso.predict(fitted["b"][i]), FLOOR)
            total = b.sum()
            out["b"][i] = b / total
            out["lo"][i] = np.maximum(iso.predict(fitted["lo"][i]), FLOOR) / total
            out["hi"][i] = np.maximum(iso.predict(fitted["hi"][i]), FLOOR) / total
        print(f"{model}i {day} folds {len(train_days)} map(0.1,0.3,0.5)={out['map'][day][3]:.3f},{out['map'][day][5]:.3f},{out['map'][day][7]:.3f} {time.time()-t0:.0f}s", flush=True)
    with open(os.path.join(HERE, "fits", f"{model}i.pkl"), "wb") as fh:
        pickle.dump(out, fh)


if __name__ == "__main__":
    main(sys.argv[1])
