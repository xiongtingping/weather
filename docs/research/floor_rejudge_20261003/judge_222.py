#!/usr/bin/env python3
"""PROTOCOL.md addendum: charter 222 re-judged.  ``physics_shift_long_20261002/
judge.py``'s rule (eight arms vs k=1 at 99.375%, halves at 09-10, station-day
clusters) on both scorings, and on today's chain (thin cells at 0.75).
Writes results_222.json."""
import gzip
import json
import os
import sys
from collections import defaultdict

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, ROOT)
from bayes_basket_target import center_width_store as store  # noqa: E402

rows = [json.loads(line) for line in gzip.open(os.path.join(HERE, "rows_222.jsonl.gz"), "rt")]
ARMS8 = ("k0", "k0.5", "k1.5", "k2", "warm0.5", "warm1.5", "cold0.5", "cold1.5")


def tab(arm, j):
    acc = defaultdict(list)
    for r in rows:
        acc[(r["station"], r["day"])].append(r["s"][arm][j])
    return {k: tuple(float(np.mean([t[i] for t in v])) for i in range(3)) for k, v in acc.items()}


def gate(T, a, b, conf):
    ks = sorted(T[b]); res = {}
    for name, p in (("all", lambda x: True), ("h1", lambda x: x[1] < "2026-09-10"), ("h2", lambda x: x[1] >= "2026-09-10")):
        kk = [x for x in ks if p(x)]
        saved = store.GATE_CONFIDENCE; store.GATE_CONFIDENCE = conf
        try:
            g = store._proper_score_gate([T[b][x] for x in kk], [T[a][x] for x in kk], kk)
        finally:
            store.GATE_CONFIDENCE = saved
        res[name] = {f: g[f] for f in ("d_logscore", "ci_logscore", "d_brier", "ci_brier", "d_crps", "ci_crps", "lower", "station_days")}
    res["passes"] = bool(res["all"]["lower"] and res["h1"]["d_logscore"] < 0 and res["h2"]["d_logscore"] < 0)
    return res


out = {}
for key, j in (("u", 0), ("f", 1)):
    T = {a: tab(a, j) for a in rows[0]["s"]}
    R = {}
    for a in ARMS8:
        R[a] = gate(T, a, "k1", 1 - 0.05 / 8)
        R[f"now_{a}"] = gate(T, f"now_{a}", "now_k1", 1 - 0.05 / 8)
    for label, a, b in (("242 served vs seat", "k1", "seat"), ("245 thin 1.1 vs 0.75", "k1", "thin0.75"),
                        ("246 bridge new vs old", "k1", "bridge_old")):
        R[label] = gate(T, a, b, 0.95)
    out[key] = R
with open(os.path.join(HERE, "results_222.json"), "w") as handle:
    json.dump(out, handle, ensure_ascii=False, indent=1)
for key in ("u", "f"):
    print(f"===== scoring {key} ({'original, no floor' if key == 'u' else 'charter 224 floored'}), station-days {out[key]['k0']['all']['station_days']}")
    for name, r in out[key].items():
        x = r["all"]
        print(f"  {name:24s} pass={r['passes']!s:5s} dLog {x['d_logscore']:+.4f} [{x['ci_logscore'][0]:+.4f},{x['ci_logscore'][1]:+.4f}]"
              f" dBrier {x['d_brier']:+.5f} [{x['ci_brier'][0]:+.5f},{x['ci_brier'][1]:+.5f}]"
              f" dCRPS {x['d_crps']:+.5f} [{x['ci_crps'][0]:+.5f},{x['ci_crps'][1]:+.5f}] halves {r['h1']['d_logscore']:+.4f}/{r['h2']['d_logscore']:+.4f}")
