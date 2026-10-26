#!/usr/bin/env python3
"""PROTOCOL 补记三: the TX-revision shift decaying with the hours since the revision.

    python3 docs/research/taf_tx_revision_20261026/decay.py
"""

from __future__ import annotations

import json
import math
import pickle
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import persist as P  # noqa: E402
import usage as U  # noqa: E402

KAPPAS = [round(0.02 * i, 2) for i in range(51)]
TAUS = [0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0, 12.0, 24.0]
DGRID = np.round(np.arange(-1.6, 1.6001, 0.02), 4)
CACHE = HERE / "decisions.pkl"


def main() -> int:
    if CACHE.exists():
        rows = pickle.loads(CACHE.read_bytes())
    else:
        rows = P.collect()
        CACHE.write_bytes(pickle.dumps(rows))
    print("decisions", len(rows), flush=True)
    # the log score of every decision on a grid of shifts, read by interpolation while fitting
    table = np.array([[U.score(r, float(d))["logscore"] for d in DGRID] for r in rows])
    step = np.array([r["step"] for r in rows], float)
    since = np.array([r["since_h"] for r in rows], float)
    day = np.array([r["day"] for r in rows])
    idx = np.arange(len(rows))

    def ls_at(sel, kappa, tau):
        delta = np.clip(kappa * step[sel] * np.exp(-since[sel] / tau), DGRID[0], DGRID[-1])
        pos = (delta - DGRID[0]) / 0.02
        lo = np.floor(pos).astype(int).clip(0, len(DGRID) - 2)
        frac = pos - lo
        t = table[sel]
        return t[np.arange(len(lo)), lo] * (1 - frac) + t[np.arange(len(lo)), lo + 1] * frac

    days = sorted(set(day))
    scored, path = [], []
    for d in days:
        train = idx[day < d]
        if len(set(day[train])) < 7:
            continue
        best = None
        for k in KAPPAS:
            for tau in TAUS:
                v = float(ls_at(train, k, tau).mean())
                if best is None or v < best[0]:
                    best = (v, k, tau)
        _, k, tau = best
        path.append({"day": d, "kappa": k, "tau": tau})
        for i in idx[day == d]:
            r = rows[i]
            delta = k * r["step"] * math.exp(-r["since_h"] / tau)
            out = {"day": d, "station": r["station"], "since_h": r["since_h"], "hour": r["local_hour"], "stage": r["stage"]}
            for arm, dl in (("R", 0.0), ("E", delta)):
                for m, v in U.score(r, dl).items():
                    out[f"{arm}_{m}"] = v
            scored.append(out)
        print(d, k, tau, flush=True)
    half = sorted({r["day"] for r in scored})[len({r["day"] for r in scored}) // 2]

    def cmp(keep):
        res = {}
        for m in ("logscore", "crps", "brier"):
            per_day = defaultdict(list)
            for r in scored:
                if keep(r):
                    per_day[r["day"]].append(r[f"E_{m}"] - r[f"R_{m}"])
            per_day = {k: v for k, v in per_day.items() if v}
            if not per_day:
                return None
            allv = [v for vs in per_day.values() for v in vs]
            ci = U.boot(per_day)
            res[m] = {"delta": round(statistics.mean(allv), 5), "ci": [round(ci[0], 5), round(ci[1], 5)]}
            if m == "logscore":
                res["n"], res["n_days"] = len(allv), len(per_day)
                res["days_better"] = sum(1 for v in per_day.values() if statistics.mean(v) < 0)
        return res

    slices = {
        "all": lambda r: True,
        "since_0_3": lambda r: r["since_h"] < 3, "since_3_6": lambda r: 3 <= r["since_h"] < 6,
        "since_6_12": lambda r: 6 <= r["since_h"] < 12, "since_12plus": lambda r: r["since_h"] >= 12,
        "local_00_10": lambda r: 0 <= r["hour"] < 10, "local_10_16": lambda r: 10 <= r["hour"] < 16,
        "local_16_23": lambda r: r["hour"] >= 16, "stage_P1": lambda r: r["stage"] == "P1",
        "first_half": lambda r: r["day"] < half, "second_half": lambda r: r["day"] >= half,
    }
    out = {"decisions": len(rows), "path": path, "E": {n: cmp(f) for n, f in slices.items()}}
    a = out["E"]["all"]
    out["verdict"] = ("INSUFFICIENT" if a["n_days"] < 20 else
                      "PASS" if a["logscore"]["ci"][1] < 0 and a["crps"]["ci"][0] <= 0 and a["brier"]["ci"][0] <= 0 else "REFUSED")
    by_st = defaultdict(list)
    for r in scored:
        by_st[r["station"]].append(r["E_logscore"] - r["R_logscore"])
    out["stations"] = {s: (len(v), round(statistics.mean(v), 4)) for s, v in sorted(by_st.items())}
    (HERE / "decay_results.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))
    print("verdict", out["verdict"], "kappa/tau path:", path[0], path[len(path) // 2], path[-1])
    for n, v in out["E"].items():
        if v:
            print(f"  {n:13s} LS {v['logscore']['delta']:+.4f} [{v['logscore']['ci'][0]:+.4f},{v['logscore']['ci'][1]:+.4f}]"
                  f" RPS {v['crps']['delta']:+.4f}{v['crps']['ci']} Br {v['brier']['delta']:+.4f}{v['brier']['ci']}"
                  f" n{v['n']} {v['days_better']}/{v['n_days']}d")
    print("  stations:", out["stations"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
