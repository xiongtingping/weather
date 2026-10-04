#!/usr/bin/env python3
"""PROTOCOL.md: pass1_c -> rows/<station>.pkl.

For every representative decision (station-day x hours-to-local-day-end bin,
the bin's last update): the pre-width vector, and at every lambda of the grid
the floored vector's (LogScore, Brier, CRPS), its buckets at 0.3 or more with
whether the truth landed there, its top bucket and z^2 -- under the production
point map (``center_width_adjusted_grid``, pinned bit for bit to
``apply_center_width_calibration``) and under the block map.

    python3 rows.py            # every station of pass1_c
    python3 rows.py ZSPD,EGLC
"""
import gzip
import json
import math
import os
import pickle
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, ROOT)
from bayes_basket_target import center_width_calibration as cwc  # noqa: E402
from bayes_basket_target import native_ensemble_oos as oos  # noqa: E402
from bayes_basket_target.materialization import floor_probabilities  # noqa: E402
from bayes_basket_target.served_shape import apply_served_table_layers  # noqa: E402

SOURCE = os.path.join(ROOT, "docs", "research", "conditional_width_lambda_20261003", "pass1_c")
OUT = os.path.join(HERE, "rows")
GRID = tuple(round(0.5 + 0.025 * i, 3) for i in range(41))
BINS = ((48, 1e9), (36, 48), (24, 36), (12, 24), (0, 12))
PAIR_MIN = 0.3


def utc(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def block_map(vec, centers, mean, lams, shift):
    """Interior buckets as uniform blocks on their own intervals, moved by
    x -> mean + lam (x - mean) + shift and re-binned through the preimage of
    every target edge -- the open edge's arithmetic (charter 190), applied to
    an interval instead of a half-line.  One row per lambda."""
    centers = np.asarray(centers, dtype=float)
    k = len(centers)
    mid = (centers[:-1] + centers[1:]) / 2.0
    lo = np.concatenate([[centers[0] - (mid[0] - centers[0])], mid])
    hi = np.concatenate([mid, [centers[-1] + (centers[-1] - mid[-1])]])
    lams = np.asarray(lams, dtype=float)
    pre = mean + (mid[None, :] - shift - mean) / lams[:, None]           # (G, K-1) preimages of the edges
    frac = np.clip((pre[:, None, :] - lo[None, :, None]) / (hi - lo)[None, :, None], 0.0, 1.0)
    cum = (np.asarray(vec, dtype=float)[None, :, None] * frac).sum(axis=1)  # (G, K-1) mass below each edge
    total = float(np.sum(vec))
    out = np.empty((len(lams), k))
    out[:, 0] = cum[:, 0]
    out[:, 1:-1] = cum[:, 1:] - cum[:, :-1]
    out[:, -1] = total - cum[:, -1]
    return out / out.sum(axis=1, keepdims=True)


def read(vector, keys, kc, actual, truth):
    """The floored vector's scores, its buckets at PAIR_MIN or more, top and z^2."""
    served = dict(zip(keys, (float(v) for v in vector)))
    floored, _ = floor_probabilities({**({truth: 0.0} if truth not in served else {}), **served})
    m = oos._metrics(floored, actual)
    values = np.array([floored[k] for k in keys])
    mean = float((np.array(kc) * values).sum() / values.sum())
    var = float(((np.array(kc) - mean) ** 2 * values).sum() / values.sum()) + 1.0 / 12.0
    z2 = ((float(truth) - mean) ** 2 + 1.0 / 12.0) / var
    pairs = [(float(floored[k]), 1 if k == truth else 0) for k in keys if floored[k] >= PAIR_MIN]
    return [m["nll"], m["brier"], m["crps"]], pairs, float(values.max()), z2


def main(stations):
    conn = sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=30,
                           isolation_level=None)
    os.makedirs(OUT, exist_ok=True)
    for station in stations:
        layer = oos.p0_gate_tables(conn, station_id=station)["center_width_layer"]
        assert abs(layer.width_lambda - 0.75) < 1e-12 and abs(layer.delta_c) < 1e-12, layer
        assert layer.width_tier_lambdas is None and layer.width_slice is None, layer
        recs = [json.loads(line) for line in gzip.open(os.path.join(SOURCE, f"{station}.jsonl.gz"), "rt")]
        by_day = defaultdict(list)
        for r in recs:
            by_day[r["event_date"]].append(r)
        out, worst = [], 0.0
        for day, rs in sorted(by_day.items()):
            end = datetime.fromisoformat(day).replace(tzinfo=ZoneInfo(rs[0]["tz"])) + timedelta(days=1)
            for bin_index, (lo, hi) in enumerate(BINS):
                inbin = [r for r in rs if lo <= (end - utc(r["at"])).total_seconds() / 3600 < hi]
                if not inbin:
                    continue
                r = max(inbin, key=lambda x: x["at"])
                keys = list(r["base"]["pre"])
                kc = [float(int(k)) for k in keys]
                total = sum(r["base"]["pre"].values())
                pre = {k: v / total for k, v in r["base"]["pre"].items()}
                truth = oos._temperature_bucket(r["actual"])
                lift, _sigma, _rung = cwc.sigma_floor_lift_v1(
                    pre, ordered_keys=keys, key_centers_c=kc, floor_rungs=layer.sigma_floor_rungs)
                composed = np.maximum(cwc.MIN_COMPOSED_WIDTH_LAMBDA,
                                      np.array(GRID) * layer.station_width_scale * lift)
                point, refused = cwc.center_width_adjusted_grid(
                    pre, ordered_keys=keys, key_centers_c=kc, shifts=[layer.delta_c] * len(GRID), lambdas=GRID,
                    station_width_scale=layer.station_width_scale, sigma_floor_rungs=layer.sigma_floor_rungs)
                assert not refused.any()
                vec = np.array([pre[k] for k in keys])
                mean = float((np.array(kc) * vec).sum())
                block = block_map(vec, kc, mean, composed, layer.delta_c)
                # the grid twin against the layer serving calls, at the lambda in force
                served, _ = apply_served_table_layers(pre, ordered_keys=keys, key_centers_c=kc, center_width=layer)
                at75 = GRID.index(0.75)
                worst = max(worst, max(abs(served[k] - point[at75][i]) for i, k in enumerate(keys)))
                row = {"st": station, "d": day, "bin": bin_index, "at": r["at"], "known": r["truth_known_at"],
                       "actual": r["actual"], "truth": truth, "keys": keys, "nf": r["base"]["nf"],
                       "pre": read(vec, keys, kc, r["actual"], truth), "p75": [float(v) for v in point[at75]],
                       "P": [read(point[g], keys, kc, r["actual"], truth) for g in range(len(GRID))],
                       "B": [read(block[g], keys, kc, r["actual"], truth) for g in range(len(GRID))]}
                out.append(row)
        assert worst < 1e-12, worst
        with open(os.path.join(OUT, f"{station}.pkl"), "wb") as fh:
            pickle.dump(out, fh)
        print(station, len(out), "decisions", f"grid-vs-served max diff {worst:.1e}", flush=True)


if __name__ == "__main__":
    names = sys.argv[1].split(",") if len(sys.argv) > 1 else sorted(
        f[:-9] for f in os.listdir(SOURCE) if f.endswith(".jsonl.gz"))
    main(names)
