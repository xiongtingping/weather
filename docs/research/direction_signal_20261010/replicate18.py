#!/usr/bin/env python3
"""PROTOCOL.md addendum 2: independent replication of round 18 -- does the latest
ECMWF-family run change call the side of the market mode, and does the market
drift toward it afterwards?  Own data path; read-only."""
from __future__ import annotations

import gzip
import json
import math
import sqlite3
import statistics
import sys
import warnings
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

warnings.filterwarnings("ignore")
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE))
from build import load_rows, floored, argmax_low, logit, utc  # noqa: E402

SYSTEMS = {"ECMWF_AIFS_ENS_GLOBAL": "AIFS集合", "ECMWF_IFS_ENS_OPEN_PF50": "IFS集合"}
FIRST, LAST = "2026-09-12", "2026-10-08"
HOURS = (1, 3, 6, 12)
STRICT = "--strict" in sys.argv

rows = load_rows()
by_sd = {k: v for k, v in rows.items() if FIRST <= k[1] <= LAST}
times = {k: [r["_t"] for r in v] for k, v in by_sd.items()}


def at(key, when, max_age_h=2.0):
    v = by_sd.get(key)
    if not v:
        return None
    i = bisect_right(times[key], when) - 1
    if i < 0 or (when - v[i]["_t"]).total_seconds() > max_age_h * 3600:
        return None
    return v[i]


def mu_q(r):
    q = floored(r["q"])
    return sum(a * b for a, b in zip(q, r["temps"]))


ssot = sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=30)
inst = []
stations = sorted({k[0] for k in by_sd})
for st in stations:
    for system, name in SYSTEMS.items():
        runs = defaultdict(lambda: {"vals": [], "arr": None})
        for day, init, arrived, val in ssot.execute(
                "SELECT target_local_date, native_run_init_utc, archive_query_at_utc, raw_tmax_c"
                " FROM calibration_native_ensemble_historical_member_run"
                " WHERE station_id=? AND target_local_date BETWEEN ? AND ? AND ensemble_system_id=? AND qc_status='ACCEPTED'",
                (st, FIRST, LAST, system)):
            if val is None or not init or not arrived:
                continue
            r = runs[(day, init)]
            r["vals"].append(float(val))
            a = utc(arrived)
            r["arr"] = a if r["arr"] is None or a < r["arr"] else r["arr"]
        by_day = defaultdict(list)
        for (day, init), r in runs.items():
            if len(r["vals"]) >= 10:
                by_day[day].append((r["arr"], init, statistics.fmean(r["vals"])))
        for day, lst in by_day.items():
            lst.sort()
            for (a0, i0, m0), (a1, i1, m1) in zip(lst, lst[1:]):
                if i1 <= i0:
                    continue
                t = a1 + timedelta(hours=1)
                r = at((st, day), t)
                if r is None or r["y"] not in r["names"] or STRICT and (not r.get("snap_at") or utc(r["snap_at"]) < a1):
                    continue
                q = floored(r["q"]); M = argmax_low(q); yk = r["names"].index(r["y"])
                Qp, Qm = sum(q[M + 1:]), sum(q[:M])
                rung = float(r["rung"] or 1.0)
                drift = {}
                for h in HOURS:
                    r2 = at((st, day), t + timedelta(hours=h))
                    drift[h] = (mu_q(r2) - mu_q(r)) if r2 is not None and r2["_t"] > r["_t"] else None
                inst.append({"st": st, "d": day, "sys": name, "delta": m1 - m0, "rung": rung, "Lq": Qp / (Qp + Qm) if Qp + Qm > 0 else 0.5,
                             "y_side": 1 if yk > M else -1 if yk < M else 0, "drift": drift, "t": t.isoformat()})
ssot.close()


def block_ci(days_k, vals, seed=20261010, draws=2000):
    days = sorted(set(days_k)); n = len(days)
    by = defaultdict(list)
    for d, v in zip(days_k, vals):
        by[d].append(v)
    sums = np.array([sum(by[d]) for d in days]); cnt = np.array([len(by[d]) for d in days], float)
    rng = np.random.default_rng(seed); out = []
    for _ in range(draws):
        starts = rng.integers(0, n, size=math.ceil(n / 3))
        idx = np.array([(s + j) % n for s in starts for j in range(3)][:n])
        out.append(sums[idx].sum() / cnt[idx].sum())
    return float(np.quantile(out, 0.025)), float(np.quantile(out, 0.975))


result = {}
for name in list(SYSTEMS.values()):
    sub = [x for x in inst if x["sys"] == name]
    days = sorted({x["d"] for x in sub}); half = days[len(days) // 2]
    sk = [x for x in sub if x["y_side"] != 0 and abs(x["delta"]) >= 0.1]
    score = [math.copysign(1, x["delta"]) * x["y_side"] for x in sk]
    mkt = [(1 if x["Lq"] > 0.5 else -1) * x["y_side"] for x in sk]
    lo, hi = block_ci([x["d"] for x in sk], score)
    h1 = float(np.mean([s for s, x in zip(score, sk) if x["d"] < half])); h2 = float(np.mean([s for s, x in zip(score, sk) if x["d"] >= half]))
    res = {"instances": len(sub), "skill_rows": len(sk), "direction_score": float(np.mean(score)), "ci": [lo, hi], "halves": [h1, h2],
           "market_lean_score": float(np.mean(mkt)),
           "skill_verdict": "复现" if lo > 0 and h1 > 0 and h2 > 0 else "不复现"}
    for h in HOURS:
        dd = [x for x in sub if x["drift"][h] is not None and abs(x["delta"]) >= 0.1]
        vals = [math.copysign(1, x["delta"]) * x["drift"][h] for x in dd]
        l2, h2_ = block_ci([x["d"] for x in dd], vals)
        res[f"drift_{h}h_C"] = [float(np.mean(vals)), l2, h2_, len(dd)]
    res["lag_verdict"] = "复现" if res["drift_12h_C"][1] > 0 else "不复现"
    # walk-forward increment over the market lean (report only)
    rows_wf = [x for x in sub if x["y_side"] != 0]
    dlist = sorted({x["d"] for x in rows_wf})
    X0 = np.array([[logit(x["Lq"])] for x in rows_wf]); X1 = np.array([[logit(x["Lq"]), x["delta"] / x["rung"]] for x in rows_wf])
    y = np.array([1.0 if x["y_side"] > 0 else 0.0 for x in rows_wf]); dv = np.array([x["d"] for x in rows_wf])
    p0 = np.full(len(y), np.nan); p1 = np.full(len(y), np.nan)
    for i, day in enumerate(dlist):
        if i < 8:
            continue
        tr, te = dv < day, dv == day
        for X, p in ((X0, p0), (X1, p1)):
            sc = StandardScaler().fit(X[tr]); m = LogisticRegression(C=1.0, max_iter=5000).fit(sc.transform(X[tr]), y[tr])
            p[te] = m.predict_proba(sc.transform(X[te]))[:, 1]
    ok = ~np.isnan(p0)
    def bll(p, yy):
        p = np.clip(p, 1e-6, 1 - 1e-6); return -(yy * np.log(p) + (1 - yy) * np.log(1 - p))
    diff = bll(p1[ok], y[ok]) - bll(p0[ok], y[ok])
    l3, h3 = block_ci(list(dv[ok]), list(diff))
    res["wf_delta_bll"] = [float(diff.mean()), l3, h3, int(ok.sum())]
    result[name] = res
(HERE / ("results_replicate18_strict.json" if STRICT else "results_replicate18.json")).write_text(json.dumps(result, ensure_ascii=False, indent=1))
for name, r in result.items():
    print(f"== {name}: {r['instances']} 实例；|Δ|≥0.1 且真值离众数 {r['skill_rows']}")
    print(f"   方向分数 {r['direction_score']:+.3f} [{r['ci'][0]:+.3f},{r['ci'][1]:+.3f}] 两半 {r['halves'][0]:+.3f}/{r['halves'][1]:+.3f}（市场倾向自身 {r['market_lean_score']:+.3f}）→ {r['skill_verdict']}")
    print("   市场随后朝改动方向漂（°C）： " + "  ".join(f"{h}h {r[f'drift_{h}h_C'][0]:+.3f} [{r[f'drift_{h}h_C'][1]:+.3f},{r[f'drift_{h}h_C'][2]:+.3f}] n={r[f'drift_{h}h_C'][3]}" for h in HOURS) + f" → {r['lag_verdict']}")
    print(f"   前推：加 Δ 后二元 LogLoss {r['wf_delta_bll'][0]:+.4f} [{r['wf_delta_bll'][1]:+.4f},{r['wf_delta_bll'][2]:+.4f}] n={r['wf_delta_bll'][3]}")
