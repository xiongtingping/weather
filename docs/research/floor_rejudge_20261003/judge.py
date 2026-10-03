#!/usr/bin/env python3
"""PROTOCOL.md verdicts, both scorings (u = original no-floor, f = charter 224
floored).  The u column must reproduce the original verdicts (parity).
Writes results.json."""
import gzip
import json
import math
import os
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, ROOT)
from bayes_basket_target import center_width_store as store  # noqa: E402

FROM, TO, SPLIT = "2026-04-01", "2026-09-30", "2026-07-01"
LAMS = [str(round(0.6 + 0.05 * i, 2)) for i in range(15)]
rows = []
for name in sorted(os.listdir(os.path.join(HERE, "rows"))):
    rows.extend(json.loads(line) for line in gzip.open(os.path.join(HERE, "rows", name), "rt"))
SC = {"u": 0, "f": 1}


def table(get, key):
    acc = defaultdict(list)
    for r in rows:
        if FROM <= r["day"] <= TO:
            acc[(r["station"], r["day"])].append(get(r)[SC[key]])
    return {k: tuple(float(np.mean([t[i] for t in v])) for i in range(3)) for k, v in acc.items()}


def gate(cand, base, conf=0.95, part=lambda k: True):
    ks = sorted(k for k in set(cand) & set(base) if part(k))
    saved = store.GATE_CONFIDENCE
    store.GATE_CONFIDENCE = conf
    try:
        g = store._proper_score_gate([base[k] for k in ks], [cand[k] for k in ks], ks)
    finally:
        store.GATE_CONFIDENCE = saved
    return {f: g[f] for f in ("d_logscore", "ci_logscore", "d_brier", "ci_brier", "d_crps", "ci_crps", "lower", "station_days", "confidence")}


def block3(cand, base, seed=20261003):
    ks = sorted(set(cand) & set(base)); days = sorted({k[1] for k in ks}); by_day = defaultdict(list)
    for j, k in enumerate(ks):
        by_day[k[1]].append(j)
    dl = np.array([cand[k][0] - base[k][0] for k in ks]); rng = np.random.default_rng(seed)
    nb = math.ceil(len(days) / 3); boots = []
    for _ in range(2000):
        ii = np.concatenate([by_day[days[(s0 + j) % len(days)]] for s0 in rng.integers(0, len(days), nb) for j in range(3)])
        boots.append(float(dl[ii].mean()))
    return [float(dl.mean()), float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))]


def verdict(cand, base, conf=0.95):
    out = {"all": gate(cand, base, conf), "04-06": gate(cand, base, conf, lambda k: k[1] < SPLIT),
           "07-09": gate(cand, base, conf, lambda k: k[1] >= SPLIT), "block3": block3(cand, base)}
    out["passes"] = bool(out["all"]["lower"] and out["04-06"]["d_logscore"] < 0 and out["07-09"]["d_logscore"] < 0)
    return out


DAY = np.array([r["day"] for r in rows])
KNOWN = np.array([datetime.fromisoformat(r["known"]).timestamp() for r in rows])
THIN = np.array([bool(r["thin"]) for r in rows])


def forward(key, bridge):
    """consensus_bridge_long_20261002/judge.py's rule, vectorised: per scored
    day and slice, the lambda with the lowest mean LogScore on the slice's
    decisions of days <= d-2 whose truth was known by d 00:00Z (fewer than 8
    such days: 0.75, or 1.1 on the thin slice)."""
    nll = np.array([[r["br"][bridge][l][SC[key]][0] for l in LAMS] for r in rows])
    pick = {}
    for d in sorted({r["day"] for r in rows if r["day"] >= FROM}):
        cut = datetime.fromisoformat(d + "T00:00:00+00:00").timestamp(); lim = (date.fromisoformat(d) - timedelta(days=2)).isoformat()
        base = (DAY <= lim) & (KNOWN <= cut)
        for thin in (True, False):
            m = base & (THIN == thin)
            if len(set(DAY[m].tolist())) < 8:
                pick[(d, thin)] = "1.1" if thin else "0.75"; continue
            pick[(d, thin)] = LAMS[int(np.argmin(nll[m].mean(axis=0)))]
    return pick


res = {}
for key in ("u", "f"):
    o = {arm: table(lambda r, a=arm: r["o"][a], key) for arm in ("A0a", "EBPa", "A0b", "EBPb")}
    cur = {arm: table(lambda r, a=arm: r["cur"][a], key) for arm in ("A0b", "EBPb")}
    R = {"242_EBP_vs_seat": verdict(o["EBPa"], o["A0a"]),
         "244_seat_k025_vs_k05": verdict(o["A0b"], o["A0a"]),
         "244_served_k025_vs_k05": verdict(o["EBPb"], o["EBPa"]),
         "current_EBP_vs_seat_bridge_1.0": verdict(cur["EBPb"], cur["A0b"])}
    picks, arms = {}, {}
    for br in ("CUR", "OFF", "STRONG", "EARLY"):
        p = forward(key, br); picks[br] = p
        arms[br] = table(lambda r, br=br, p=p: r["br"][br][p[(r["day"], r["thin"])]], key)
    for a in ("OFF", "STRONG", "EARLY"):
        R[f"246_{a}_vs_CUR"] = verdict(arms[a], arms["CUR"], conf=1 - 0.05 / 3)
    R["246_lambda_picks"] = {br: {"thin": dict(Counter(v for (d, t), v in p.items() if t)),
                                  "rest": dict(Counter(v for (d, t), v in p.items() if not t))} for br, p in picks.items()}
    res[key] = R
with open(os.path.join(HERE, "results.json"), "w") as handle:
    json.dump(res, handle, ensure_ascii=False, indent=1)
for key in ("u", "f"):
    print(f"===== scoring {key} ({'original, no floor' if key == 'u' else 'charter 224 floored'})")
    for name, v in res[key].items():
        if name.endswith("picks"):
            print(" ", name, {br: x["rest"] for br, x in v.items()}); continue
        x = v["all"]
        print(f"  {name:32s} pass={v['passes']!s:5s} sd={x['station_days']} dLog {x['d_logscore']:+.4f} [{x['ci_logscore'][0]:+.4f},{x['ci_logscore'][1]:+.4f}]"
              f" dBrier {x['d_brier']:+.5f} [{x['ci_brier'][0]:+.5f},{x['ci_brier'][1]:+.5f}] dCRPS {x['d_crps']:+.5f} [{x['ci_crps'][0]:+.5f},{x['ci_crps'][1]:+.5f}]"
              f" halves {v['04-06']['d_logscore']:+.4f}/{v['07-09']['d_logscore']:+.4f} block3 [{v['block3'][1]:+.4f},{v['block3'][2]:+.4f}]")
