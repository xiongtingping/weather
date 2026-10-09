#!/usr/bin/env python3
"""PROTOCOL.md addendum 1: the standard Proper rule (charter 231) on a belief
that carries the latest-run change.  Read-only."""
from __future__ import annotations

import json
import math
import warnings
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

warnings.filterwarnings("ignore")
HERE = Path(__file__).resolve().parent
src = (HERE / "study.py").read_text()
src = src[: src.index("inst = defaultdict(list)")]
ns = {"__file__": str(HERE / "study.py"), "__name__": "study_defs"}
exec(compile(src, "study.py", "exec"), ns)
by_sd, changes, at, floored, argmax_low, logit = (ns[k] for k in ("by_sd", "changes", "at", "floored", "argmax_low", "logit"))
_study = (HERE / "study.py").read_text()
_b = _study[_study.index("def block("): _study.index("def bll(")]
exec(compile(_b, "study.py", "exec"), ns)
block = ns["block"]

FEE = 0.05
TAU = 6.0
TICK = 0.01


def fresh_latest(st, day, system, when):
    ch = [c for c in changes(st, day, system) if c[0] <= when]
    if not ch:
        return 0.0
    a, d = ch[-1]
    return d * math.exp(-max(0.0, (when - a).total_seconds() / 3600.0) / TAU)


rows = []
for (st, day) in sorted(by_sd):
    for arrived, delta in changes(st, day, "ECMWF_AIFS_ENS_GLOBAL"):
        t = arrived + timedelta(hours=1)
        r = at((st, day), t, not_before=arrived)
        if r is None or r["y"] not in r["names"] or not r.get("book"):
            continue
        q = floored(r["q"]); M = argmax_low(q); yk = r["names"].index(r["y"])
        U, D = float(sum(q[M + 1:])), float(sum(q[:M]))
        if U <= 0 or D <= 0:
            continue
        rung = float(r["rung"] or 1.0)
        rows.append({"st": st, "d": day, "t": t, "q": q, "M": M, "yk": yk, "U": U, "D": D, "book": r["book"],
                     "Lq": U / (U + D), "y_side": 1 if yk > M else -1 if yk < M else 0,
                     "x1": delta / rung,
                     "x_single": fresh_latest(st, day, "ECMWF_AIFS_SINGLE_POINT", t) / rung,
                     "x_gefs": fresh_latest(st, day, "NOAA_GEFS_05", t) / rung})

FEATS = {"B0": lambda r: [logit(r["Lq"])],
         "B1": lambda r: [logit(r["Lq"]), r["x1"]],
         "B2": lambda r: [logit(r["Lq"]), r["x1"], r["x_single"], r["x_gefs"]]}
days = sorted({r["d"] for r in rows})
dv = np.array([r["d"] for r in rows])
y = np.array([1.0 if r["y_side"] > 0 else 0.0 for r in rows]); off = np.array([r["y_side"] != 0 for r in rows])
pup = {}
for name, fe in FEATS.items():
    X = np.array([fe(r) for r in rows]); pred = np.full(len(rows), np.nan)
    for i, day in enumerate(days):
        if i < 8:
            continue
        tr, te = (dv < day) & off, dv == day
        s = StandardScaler().fit(X[tr]); m = LogisticRegression(C=1.0, max_iter=5000).fit(s.transform(X[tr]), y[tr])
        pred[te] = m.predict_proba(s.transform(X[te]))[:, 1]
    pup[name] = pred


def legs(r, p_up, mode):
    """Standard Proper on b: q's mode kept, the two sides re-split by p_up."""
    q = r["q"]; n = len(q); out = []
    for k in range(n):
        if k == r["M"]:
            continue
        b = q[k] * (p_up * (r["U"] + r["D"]) / r["U"] if k > r["M"] else (1 - p_up) * (r["U"] + r["D"]) / r["D"])
        s = math.log(b / q[k])
        if abs(s) < 1e-9:
            continue
        bid, ask = r["book"][k][0], r["book"][k][1]
        if s > 0:
            if mode == "taker":
                if ask is None:
                    continue
                price, fee = ask, FEE * ask * (1 - ask)
            else:
                if bid is None or ask is None or bid + TICK >= ask:
                    continue
                price, fee = bid + TICK, 0.0
            if price + fee < b:
                out.append({"side": "YES", "x": abs(s), "cost": price + fee, "pay": 1.0 if r["yk"] == k else 0.0})
        else:
            if mode == "taker":
                if bid is None:
                    continue
                price = 1.0 - bid
                fee = FEE * price * (1 - price)
            else:
                if bid is None or ask is None or (1.0 - ask) + TICK >= 1.0 - bid:
                    continue
                price, fee = (1.0 - ask) + TICK, 0.0
            if price + fee < 1.0 - b:
                out.append({"side": "NO", "x": abs(s), "cost": price + fee, "pay": 0.0 if r["yk"] == k else 1.0})
    return out


result = {}
for name in FEATS:
    for mode in ("taker", "maker"):
        seen, per_day = set(), []
        for r, p in zip(rows, pup[name]):
            if np.isnan(p) or (r["st"], r["d"]) in seen:
                continue
            L = legs(r, float(p), mode)
            if not L:
                continue
            seen.add((r["st"], r["d"]))
            per_day.append({"d": r["d"], "pnl": sum(l["x"] * (l["pay"] - l["cost"]) for l in L),
                            "cost": sum(l["x"] * l["cost"] for l in L),
                            "yes_pnl": sum(l["x"] * (l["pay"] - l["cost"]) for l in L if l["side"] == "YES"),
                            "no_pnl": sum(l["x"] * (l["pay"] - l["cost"]) for l in L if l["side"] == "NO"),
                            "yes_cost": sum(l["x"] * l["cost"] for l in L if l["side"] == "YES"),
                            "no_cost": sum(l["x"] * l["cost"] for l in L if l["side"] == "NO")})
        if len(per_day) < 30:
            continue
        ds = sorted({x["d"] for x in per_day}); half = ds[len(ds) // 2]
        cost = sum(x["cost"] for x in per_day)
        ret = [x["pnl"] / cost * len(per_day) for x in per_day]          # so the mean is total pnl / total cost
        lo, hi, _ = block([x["d"] for x in per_day], ret)
        h = [sum(x["pnl"] for x in per_day if (x["d"] < half) == first) / max(1e-9, sum(x["cost"] for x in per_day if (x["d"] < half) == first))
             for first in (True, False)]
        yc, nc = sum(x["yes_cost"] for x in per_day), sum(x["no_cost"] for x in per_day)
        result[f"{name}·{'吃单' if mode == 'taker' else '挂单情景'}"] = {
            "station_days": len(per_day), "return": float(np.mean(ret)), "ci": [lo, hi], "halves": h,
            "yes_return": (sum(x["yes_pnl"] for x in per_day) / yc) if yc else None,
            "no_return": (sum(x["no_pnl"] for x in per_day) / nc) if nc else None,
            "yes_cost_share": yc / cost if cost else None}
(HERE / "results_proper_standard.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))
for k, v in result.items():
    print(f"{k:12s} 站日 {v['station_days']:4d} 回报 {v['return']:+.3%} [{v['ci'][0]:+.3%},{v['ci'][1]:+.3%}] 两半 {v['halves'][0]:+.2%}/{v['halves'][1]:+.2%}"
          f"  YES {v['yes_return'] if v['yes_return'] is None else f'{v['yes_return']:+.2%}'}  NO {v['no_return'] if v['no_return'] is None else f'{v['no_return']:+.2%}'}  YES成本占比 {v['yes_cost_share']:.2f}")
