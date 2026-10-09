#!/usr/bin/env python3
"""PROTOCOL.md: latest-run change across every family and our served P0;
what it means for the Proper lane.  Read-only."""
from __future__ import annotations

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
sys.path.insert(0, str(ROOT / "docs/research/direction_signal_20261010"))
from build import load_rows, floored, argmax_low, logit, utc  # noqa: E402

FIRST, LAST = "2026-09-12", "2026-10-08"
NATIVE = ["ECMWF_AIFS_ENS_GLOBAL", "ECMWF_IFS_ENS_OPEN_PF50", "NOAA_GEFS_05", "NOAA_AIGEFS_025",
          "DWD_ICON_GLOBAL_EPS", "ECCC_GEM_GLOBAL_ENS"]
POINT = ["ECMWF_IFS_9KM_RELAY_POINT", "ECMWF_AIFS_SINGLE_POINT", "UKMO_GLOBAL_10KM_RELAY_POINT",
         "CMA_GRAPES_GLOBAL_POINT", "DWD_ICON_EU_POINT", "JMA_MSM_POINT", "NCEP_HRRR_POINT", "NCEP_NBM_POINT"]
COMPOSITES = {
    "合成·ECMWF四家": ["ECMWF_AIFS_ENS_GLOBAL", "ECMWF_IFS_ENS_OPEN_PF50", "ECMWF_IFS_9KM_RELAY_POINT", "ECMWF_AIFS_SINGLE_POINT"],
    "合成·非ECMWF原生四家": ["NOAA_GEFS_05", "NOAA_AIGEFS_025", "DWD_ICON_GLOBAL_EPS", "ECCC_GEM_GLOBAL_ENS"],
    "合成·全部14家": NATIVE + POINT,
}
HOURS = (1, 3, 6, 12)
BONF = 0.05 / 20
SEED = 20261010

rows = load_rows()
by_sd = {k: v for k, v in rows.items() if FIRST <= k[1] <= LAST}
times = {k: [r["_t"] for r in v] for k, v in by_sd.items()}


def at(key, when, not_before=None, max_age_h=2.0):
    v = by_sd.get(key)
    if not v:
        return None
    i = bisect_right(times[key], when) - 1
    while i >= 0:
        r = v[i]
        if (when - r["_t"]).total_seconds() > max_age_h * 3600:
            return None
        if not_before is None or (r.get("snap_at") and utc(r["snap_at"]) >= not_before):
            return r
        return None
    return None


def mu(vec, temps):
    v = floored(vec)
    return sum(a * b for a, b in zip(v, temps))


# ---- runs per (station, day, system): [(arrived, init, value)] sorted by init
ssot = sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=30)
runs = defaultdict(list)
stations = sorted({k[0] for k in by_sd})
for st in stations:
    for system, day, init, arrived, value, count in ssot.execute(
            "SELECT ensemble_system_id, target_local_date, native_run_init_utc, MIN(archive_query_at_utc), AVG(raw_tmax_c), COUNT(*)"
            " FROM calibration_native_ensemble_historical_member_run WHERE station_id=? AND target_local_date BETWEEN ? AND ?"
            " AND qc_status='ACCEPTED' AND native_run_init_utc IS NOT NULL AND raw_tmax_c IS NOT NULL GROUP BY 1,2,3",
            (st, FIRST, LAST)):
        if system in NATIVE and arrived and count >= 10:
            runs[(st, day, system)].append((utc(arrived), str(init), float(value)))
    first = {}
    for system, day, init, knowable, value in ssot.execute(
            "SELECT ensemble_system_id, target_local_date, run_init_utc, knowable_at_utc, forecast_tmax_c FROM calibration_regional_point_run"
            " WHERE station_id=? AND target_local_date BETWEEN ? AND ? AND knowable_at_utc IS NOT NULL AND forecast_tmax_c IS NOT NULL",
            (st, FIRST, LAST)):
        if system not in POINT:
            continue
        k = (st, day, system, str(init)); a = utc(knowable)
        if k not in first or a < first[k][0]:
            first[k] = (a, str(init), float(value))
    for (s, day, system, _i), row in first.items():
        runs[(s, day, system)].append(row)
ssot.close()
for v in runs.values():
    v.sort(key=lambda r: r[1])
# our served P0 as a "system": each P0 decision vs the latest P0 decision at least 3h before it
for key, v in by_sd.items():
    p0 = [r for r in v if r["stage"] == "P0" and r.get("p")]
    seq = []
    for r in p0:
        seq.append((r["_t"], r["_t"].isoformat(), mu(r["p"], r["temps"])))
    runs[(key[0], key[1], "我方P0")] = seq


def changes(st, day, system):
    """(arrived, delta) for each run after the first; P0 pairs 3h apart."""
    v = runs.get((st, day, system)) or []
    out = []
    if system == "我方P0":
        ts = [r[0] for r in v]
        for i, (a, _i, val) in enumerate(v):
            j = bisect_right(ts, a - timedelta(hours=3)) - 1
            if j >= 0:
                out.append((a, val - v[j][2]))
        return out
    for prev, new in zip(v, v[1:]):
        if new[1] > prev[1] and prev[0] <= new[0]:
            out.append((new[0], new[2] - prev[2]))
    return out


def latest_in_hand(st, day, system, when):
    ch = [c for c in changes(st, day, system) if c[0] <= when]
    return ch[-1][1] if ch else None


def instance(st, day, arrived, delta):
    t = arrived + timedelta(hours=1)
    r = at((st, day), t, not_before=arrived)
    if r is None or r["y"] not in r["names"]:
        return None
    q = floored(r["q"]); M = argmax_low(q); yk = r["names"].index(r["y"])
    Qp, Qm = sum(q[M + 1:]), sum(q[:M])
    drift = {}
    for h in HOURS:
        r2 = at((st, day), t + timedelta(hours=h))
        drift[h] = (mu(r2["q"], r2["temps"]) - mu(r["q"], r["temps"])) if r2 is not None and r2["_t"] > r["_t"] else None
    lead = (r["h"] or 0) - (t - r["_t"]).total_seconds() / 3600.0
    return {"st": st, "d": day, "delta": delta, "rung": float(r["rung"] or 1.0), "Lq": Qp / (Qp + Qm) if Qp + Qm > 0 else 0.5,
            "y_side": 1 if yk > M else -1 if yk < M else 0, "M": M, "yk": yk, "q_mode": q[M], "drift": drift, "lead": lead,
            "book": r.get("book"), "n": len(r["names"])}


inst = defaultdict(list)
for (st, day) in sorted(by_sd):
    for system in NATIVE + POINT + ["我方P0"]:
        for arrived, delta in changes(st, day, system):
            x = instance(st, day, arrived, delta)
            if x:
                inst[system].append(x)
    for name, members in COMPOSITES.items():
        moments = sorted({a for s in members for a, _d in changes(st, day, s)})
        for a in moments:
            vals = [latest_in_hand(st, day, s, a) for s in members]
            vals = [v for v in vals if v is not None]
            if vals:
                x = instance(st, day, a, sum(vals))
                if x:
                    inst[name].append(x)


def block(days_k, vals, draws=2000):
    days = sorted(set(days_k)); n = len(days)
    by = defaultdict(list)
    for d, v in zip(days_k, vals):
        by[d].append(v)
    sums = np.array([sum(by[d]) for d in days]); cnt = np.array([len(by[d]) for d in days], float)
    rng = np.random.default_rng(SEED); out = []
    for _ in range(draws):
        starts = rng.integers(0, n, size=math.ceil(n / 3))
        idx = np.array([(s + j) % n for s in starts for j in range(3)][:n])
        out.append(sums[idx].sum() / cnt[idx].sum())
    out = np.array(out)
    return float(np.quantile(out, 0.025)), float(np.quantile(out, 0.975)), out


def bll(p, y):
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return -(y * np.log(p) + (1 - y) * np.log(1 - p))


def evaluate(sub):
    res = {"instances": len(sub)}
    days = sorted({x["d"] for x in sub})
    if len(days) < 16:
        return {**res, "state": "样本不足"}
    half = days[len(days) // 2]
    sk = [x for x in sub if x["y_side"] != 0 and abs(x["delta"]) >= 0.1]
    if len(sk) >= 100:
        sc = [math.copysign(1, x["delta"]) * x["y_side"] for x in sk]
        lo, hi, _ = block([x["d"] for x in sk], sc)
        res["direction_score"] = [float(np.mean(sc)), lo, hi,
                                  float(np.mean([s for s, x in zip(sc, sk) if x["d"] < half])),
                                  float(np.mean([s for s, x in zip(sc, sk) if x["d"] >= half])), len(sk)]
    for h in HOURS:
        dd = [x for x in sub if x["drift"][h] is not None and abs(x["delta"]) >= 0.1]
        if len(dd) >= 100:
            vals = [math.copysign(1, x["delta"]) * x["drift"][h] for x in dd]
            lo, hi, _ = block([x["d"] for x in dd], vals)
            res[f"drift_{h}h"] = [float(np.mean(vals)), lo, hi, len(dd)]
    wf = [x for x in sub if x["y_side"] != 0]
    dl = sorted({x["d"] for x in wf})
    X0 = np.array([[logit(x["Lq"])] for x in wf]); X1 = np.array([[logit(x["Lq"]), x["delta"] / x["rung"]] for x in wf])
    y = np.array([1.0 if x["y_side"] > 0 else 0.0 for x in wf]); dv = np.array([x["d"] for x in wf])
    p0 = np.full(len(y), np.nan); p1 = np.full(len(y), np.nan)
    for i, day in enumerate(dl):
        if i < 8:
            continue
        tr, te = dv < day, dv == day
        if not te.any() or y[tr].min() == y[tr].max():
            continue
        for X, p in ((X0, p0), (X1, p1)):
            s = StandardScaler().fit(X[tr]); m = LogisticRegression(C=1.0, max_iter=5000).fit(s.transform(X[tr]), y[tr])
            p[te] = m.predict_proba(s.transform(X[te]))[:, 1]
    ok = ~np.isnan(p0) & ~np.isnan(p1)
    if ok.sum() >= 100:
        diff = bll(p1[ok], y[ok]) - bll(p0[ok], y[ok])
        lo, hi, draws = block(list(dv[ok]), list(diff))
        tdays = sorted(set(dv[ok])); half2 = tdays[len(tdays) // 2]
        h1, h2 = float(diff[dv[ok] < half2].mean()), float(diff[dv[ok] >= half2].mean())
        p_one = float((draws >= 0).mean())
        verdict = "有" if hi < 0 and h1 < 0 and h2 < 0 and p_one < BONF else ("苗头" if hi < 0 and h1 < 0 and h2 < 0 else "没有")
        res["increment"] = [float(diff.mean()), lo, hi, h1, h2, p_one, int(ok.sum()), verdict]
    return res


result = {"systems": {}, "proper": {}}
for name in NATIVE + POINT + ["我方P0"] + list(COMPOSITES):
    result["systems"][name] = evaluate(inst.get(name, []))

# ---- Proper: the modal bucket by change size, and the paper rule R
for name in ("合成·ECMWF四家", "合成·全部14家"):
    sub = inst[name]
    mode_tab = []
    for lo_, hi_ in ((0, 0.3), (0.3, 1.0), (1.0, 99)):
        g = [x for x in sub if lo_ <= abs(x["delta"]) < hi_]
        if g:
            mode_tab.append({"abs_delta": [lo_, hi_], "n": len(g), "q_mode": float(np.mean([x["q_mode"] for x in g])),
                             "mode_landed": float(np.mean([x["yk"] == x["M"] for x in g]))})
    trades = {}
    for x in sorted(sub, key=lambda x: x["d"]):
        if abs(x["delta"]) < 0.3 or not x["book"]:
            continue
        k = x["M"] + (1 if x["delta"] > 0 else -1)
        if not 0 <= k < x["n"]:
            continue
        key = (x["st"], x["d"])
        if key in trades:
            continue                       # one entry per station-day, the first instant that qualifies
        bid, ask = x["book"][k][0], x["book"][k][1]
        hit = 1.0 if x["yk"] == k else 0.0
        trades[key] = {"d": x["d"], "lead": x["lead"], "hit": hit,
                       "taker": (hit - ask - 0.05 * ask * (1 - ask)) if ask is not None else None,
                       "maker": (hit - (bid + 0.01)) if bid is not None else None,
                       "ask": ask, "bid": bid}
    rule = {}
    for slice_name, pred in (("全部", lambda t: True), ("距结算12–48h", lambda t: 12 <= t["lead"] < 48)):
        for leg in ("taker", "maker"):
            tt = [t for t in trades.values() if pred(t) and t[leg] is not None]
            if len(tt) < 30:
                continue
            days = sorted({t["d"] for t in tt}); half = days[len(days) // 2]
            vals = [t[leg] for t in tt]
            lo, hi, _ = block([t["d"] for t in tt], vals)
            rule[f"{slice_name}·{'吃单(卖一+费)' if leg == 'taker' else '挂单情景(买一+1)'}"] = {
                "n": len(tt), "per_share": float(np.mean(vals)), "ci": [lo, hi],
                "halves": [float(np.mean([t[leg] for t in tt if t["d"] < half])), float(np.mean([t[leg] for t in tt if t["d"] >= half]))],
                "hit": float(np.mean([t["hit"] for t in tt])),
                "price": float(np.mean([t["ask"] if leg == "taker" else t["bid"] + 0.01 for t in tt]))}
    result["proper"][name] = {"mode_by_change": mode_tab, "rule_R": rule}
(HERE / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=1, default=float))


def f(v, d=3):
    return "—" if v is None else f"{v:+.{d}f}"


print(f"{'系统':24s} {'实例':>6s} | 方向分数 [CI] 两半 | 前推增量 [CI] 两半 判 | 漂移12h")
for name, r in result["systems"].items():
    if r.get("state"):
        print(f"{name:24s} {r['instances']:6d} | {r['state']}"); continue
    ds = r.get("direction_score"); inc = r.get("increment"); d12 = r.get("drift_12h")
    s1 = f"{f(ds[0])} [{f(ds[1])},{f(ds[2])}] {f(ds[3],2)}/{f(ds[4],2)} n={ds[5]}" if ds else "—"
    s2 = f"{f(inc[0],4)} [{f(inc[1],4)},{f(inc[2],4)}] {f(inc[3],4)}/{f(inc[4],4)} {inc[7]}" if inc else "—"
    s3 = f"{f(d12[0])} [{f(d12[1])},{f(d12[2])}]" if d12 else "—"
    print(f"{name:24s} {r['instances']:6d} | {s1} | {s2} | {s3}")
for name, pr in result["proper"].items():
    print(f"== Proper · {name}")
    for m in pr["mode_by_change"]:
        print(f"   |Δ| {m['abs_delta']}: n={m['n']} 市场说众数 {m['q_mode']:.3f} 众数实中 {m['mode_landed']:.3f}")
    for k, v in pr["rule_R"].items():
        print(f"   R {k}: n={v['n']} 每股 {v['per_share']:+.3f} [{v['ci'][0]:+.3f},{v['ci'][1]:+.3f}] 两半 {v['halves'][0]:+.3f}/{v['halves'][1]:+.3f} 命中 {v['hit']:.3f} 价 {v['price']:.3f}")
