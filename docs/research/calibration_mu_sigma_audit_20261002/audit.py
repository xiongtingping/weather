#!/usr/bin/env python3
"""mu / sigma audit of the served P0 after charter 247 (2026-10-02, read-only).

Source A -- the current arithmetic (dress_v1, charter 247) on the served P0
replay: docs/research/single_system_stretch_vs_dress_20261002/pass1_prod
(45 stations, 07-30..10-01, every decision before local day end, integer-C
grid).  Source B -- what actually served (stretch form, v19..v24) from the
10-01 audit's live capture (last committed P0 before station-local midnight,
09-25..09-30, market ladder).  Same definitions as the 10-01 audit: the
served vector read as a piecewise-uniform density, PIT from it, z = (truth -
mean) / sd; uncertainty by date-block bootstrap (dates are the clusters).
"""
from __future__ import annotations

import collections
import gzip
import json
import math
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
PROD = ROOT / "docs/research/single_system_stretch_vs_dress_20261002/pass1_prod"
LIVE = ROOT / "docs/research/calibration_shape_audit_20261001/capture.json"
F_STATIONS = {"KATL", "KAUS", "KBKF", "KDAL", "KHOU", "KLAX", "KLGA", "KMIA", "KORD", "KSEA", "KSFO"}
rng = np.random.default_rng(20261002)
BOOT = 1000


def read_vec(vec, truth, rung=1.0):
    """mean, sd, PIT u, p at truth bucket, truth index; integer-C grid, closed buckets."""
    keys = sorted(vec, key=int)
    k = np.array([int(x) for x in keys], float)
    pv = np.array([max(float(vec[x]), 0.0) for x in keys]); pv /= pv.sum()
    mu = float((pv * k).sum()); var = float((pv * ((k - mu) ** 2 + rung * rung / 12.0)).sum())
    idx = int(np.clip(np.searchsorted(k, truth + 0.5 * rung, side="right") - 1, 0, len(k) - 1))
    frac = min(1.0, max(0.0, (truth - (k[idx] - 0.5 * rung)) / rung))
    u = float(pv[:idx].sum() + pv[idx] * frac)
    return mu, math.sqrt(var), u, float(pv[idx]), idx


def load_prod():
    rows = []
    for path in sorted(PROD.glob("*.jsonl.gz")):
        with gzip.open(path, "rt") as handle:
            for line in handle:
                r = json.loads(line)
                mu, sd, u, pt, idx = read_vec(r["base"]["vec"], float(r["actual"]))
                day_end = datetime.fromisoformat(r["event_date"]).replace(tzinfo=ZoneInfo(r["tz"])) + timedelta(days=1)
                at = datetime.fromisoformat(r["at"].replace("Z", "+00:00"))
                rows.append(dict(station=r["station"], date=r["event_date"], at=r["at"], truth=float(r["actual"]), mu=mu, sd=sd, u=u,
                                 p_truth=pt, lead=r["lead"], n_pool=len(r["pool"]), hours_to_end=(day_end - at).total_seconds() / 3600.0,
                                 unit="F" if r["station"] in F_STATIONS else "C", ls=-math.log(max(pt, 1e-3))))
    return rows


def load_live():
    if not LIVE.exists():
        return []
    D = json.load(open(LIVE))
    rows = []
    for r in D["rows"]:
        e = r.get("last")
        if not e or "rejected" in e:
            continue
        inp = e["inputs"]; intervals = inp["bucket_intervals_c_json"]; p = e["probability"]
        items = sorted(intervals.items(), key=lambda kv: (-1e9 if kv[1][0] is None else kv[1][0]))
        keys = [k for k, _ in items]; lo = [v[0] for _, v in items]; hi = [v[1] for _, v in items]
        if set(keys) != set(p):
            continue
        widths = [h - l for l, h in zip(lo, hi) if l is not None and h is not None]; rung = float(np.median(widths))
        pv = np.array([max(float(p.get(k, 0.0)), 0.0) for k in keys]); pv /= pv.sum()
        m = np.zeros(len(keys)); v = np.zeros(len(keys))
        for i, (l, h) in enumerate(zip(lo, hi)):
            if l is None: m[i], v[i] = h - rung, rung ** 2
            elif h is None: m[i], v[i] = l + rung, rung ** 2
            else: m[i], v[i] = (l + h) / 2, (h - l) ** 2 / 12
        mu = float((pv * m).sum()); var = float((pv * (v + (m - mu) ** 2)).sum())
        truth = float(r["truth_c"]); idx = len(keys) - 1
        for i, (l, h) in enumerate(zip(lo, hi)):
            if (l is None or truth >= l) and (h is None or truth < h): idx = i; break
        l, h = lo[idx], hi[idx]
        frac = math.exp((truth - h) / rung) if l is None else (1 - math.exp(-(truth - l) / rung) if h is None else (truth - l) / (h - l))
        u = float(pv[:idx].sum() + pv[idx] * frac)
        rows.append(dict(station=r["station"], date=r["date"], truth=truth, mu=mu, sd=math.sqrt(var), u=u, p_truth=float(pv[idx]),
                         version=e["posterior_version"], hours_to_end=e["hours_to_midnight"] + 24.0,
                         n_pool=len(inp.get("system_weights_json") or {}), unit="F" if r["station"] in F_STATIONS else "C",
                         ls=-math.log(max(float(pv[idx]), 1e-3))))
    return rows


def wmean(x, w): return float((x * w).sum() / w.sum())
def wvar(x, w): m = wmean(x, w); return float((w * (x - m) ** 2).sum() / w.sum())


class Boot:
    """Station-day equal weights; dates resampled with replacement (the 10-01 audit's clusters).

    The date draws are made once per row set and shared by every statistic, so a
    report costs one pass of vectorised weights per statistic, not a Counter per rep.
    """

    def __init__(self, rows):
        dates = sorted({r["date"] for r in rows}); index = {d: i for i, d in enumerate(dates)}
        self.date_idx = np.array([index[r["date"]] for r in rows])
        copies = collections.Counter((r["station"], r["date"]) for r in rows)
        self.base_w = np.array([1.0 / copies[(r["station"], r["date"])] for r in rows])
        draws = rng.integers(0, len(dates), size=(BOOT, len(dates)))
        self.counts = np.stack([np.bincount(d, minlength=len(dates)) for d in draws])   # BOOT x n_dates

    def __call__(self, fn):
        point = fn(self.base_w)
        vals = []
        for cnt in self.counts:
            w = self.base_w * cnt[self.date_idx]
            if w.sum() > 0:
                vals.append(fn(w))
        lo, hi = np.percentile(vals, [2.5, 97.5])
        return [float(point), float(lo), float(hi)]


def report(rows, label):
    res = np.array([r["truth"] - r["mu"] for r in rows]); sd = np.array([r["sd"] for r in rows]); z = res / sd
    u = np.array([r["u"] for r in rows]); ls = np.array([r["ls"] for r in rows])
    copies = collections.Counter((r["station"], r["date"]) for r in rows)
    out = {"label": label, "updates": len(rows), "station_days": len(copies), "dates": len({r["date"] for r in rows})}
    boot = Boot(rows)
    out["bias_c"] = boot(lambda w: wmean(res, w))
    out["bias_z"] = boot(lambda w: wmean(z, w))
    out["pit_mean_minus_half"] = boot(lambda w: wmean(u, w) - 0.5)
    out["rms_res_c"] = float(np.sqrt(np.mean(res ** 2))); out["rms_sd_c"] = float(np.sqrt(np.mean(sd ** 2)))
    out["z_meansq"] = boot(lambda w: wmean(z ** 2, w))
    out["z_sd_after_bias"] = boot(lambda w: math.sqrt(wvar(z, w)))
    out["pit_var_ratio"] = boot(lambda w: wvar(u, w) * 12)
    out["cover_50"] = boot(lambda w: wmean(((u >= 0.25) & (u <= 0.75)).astype(float), w))
    out["cover_90"] = boot(lambda w: wmean(((u >= 0.05) & (u <= 0.95)).astype(float), w))
    out["pit_tail_lt05_gt95"] = [float((u < 0.05).mean()), float((u > 0.95).mean())]
    out["logscore_floor1e-3"] = boot(lambda w: wmean(ls, w))
    # spread-skill: quintiles of served sd, rms residual over rms sd in each (1 = width right in that bin)
    order = np.argsort(sd); bins = np.array_split(order, 5)
    out["spread_skill_quintiles"] = [{"mean_sd": float(sd[b].mean()), "rms_res": float(np.sqrt(np.mean(res[b] ** 2))),
                                      "z_meansq": float(np.mean(z[b] ** 2))} for b in bins]
    xs = np.array([q["mean_sd"] for q in out["spread_skill_quintiles"]]); ys = np.array([q["rms_res"] for q in out["spread_skill_quintiles"]])
    out["spread_skill_slope"] = float(np.polyfit(xs, ys, 1)[0])
    return out


def station_bias(rows):
    g = collections.defaultdict(list)
    for r in rows:
        g[r["station"]].append(r["truth"] - r["mu"])
    t = {}
    for st, e in g.items():
        e = np.array(e); n = len(e)
        if n >= 10:
            t[st] = {"n": n, "bias_c": float(e.mean()), "t": float(e.mean() / (e.std(ddof=1) / math.sqrt(n)))}
    sig = sorted([(st, v) for st, v in t.items() if abs(v["t"]) > 2], key=lambda kv: -abs(kv[1]["t"]))
    return {"stations": len(t), "abs_t_gt_2": len(sig), "list": {st: {k: round(x, 3) for k, x in v.items()} for st, v in sig[:12]}}


def main():
    prod = load_prod(); live = load_live()
    by_day = collections.defaultdict(list)
    for r in prod:
        by_day[(r["station"], r["date"])].append(r)
    roots, firsts = [], []
    for recs in by_day.values():
        before = [r for r in recs if r["hours_to_end"] >= 24.0]
        if before:
            roots.append(min(before, key=lambda r: r["hours_to_end"]))
        first = min(recs, key=lambda r: r["at"])
        if first["hours_to_end"] > 36.0:
            firsts.append(first)
    seg = {
        "all_updates": prod,
        "recent_0914_1001": [r for r in prod if r["date"] >= "2026-09-14"],
        "day_ahead_roots": roots,
        "first_decision_gt36h": firsts,
        "within_day_le12h": [r for r in prod if r["hours_to_end"] <= 12.0],
    }
    res = {"source_A": "pass1_prod (dress_v1 production form, replay)", "updates": len(prod), "stations": len({r["station"] for r in prod}),
           "segments": {k: report(v, k) for k, v in seg.items()}}
    res["by_lead"] = {k: report([r for r in prod if r["lead"] == k], k) for k in sorted({r["lead"] for r in prod})}
    res["by_pool_size"] = {str(n): report([r for r in prod if r["n_pool"] == n], f"pool={n}") for n in sorted({r["n_pool"] for r in prod})
                           if sum(r["n_pool"] == n for r in prod) >= 200}
    res["by_pool_size_gt36h_first"] = {str(n): report([r for r in firsts if r["n_pool"] == n], f"first>36h pool={n}")
                                      for n in sorted({r["n_pool"] for r in firsts}) if sum(r["n_pool"] == n for r in firsts) >= 60}
    res["by_unit"] = {k: report([r for r in prod if r["unit"] == k], k) for k in ("C", "F")}
    res["station_bias_roots"] = station_bias(roots)
    res["station_bias_recent_roots"] = station_bias([r for r in roots if r["date"] >= "2026-09-14"])
    if live:
        live_recent = [r for r in live if r["date"] >= "2026-09-25"]
        keys = {(r["station"], r["date"]) for r in live_recent}
        res["source_B_live_last_pre_midnight_0925_0930"] = report(live_recent, "live v19+ last-before-midnight")
        res["source_B_versions"] = dict(collections.Counter(r["version"] for r in live_recent))
        res["source_A_same_station_days_roots"] = report([r for r in roots if (r["station"], r["date"]) in keys], "replay roots on the same station-days")
        res["source_B_live_0901_0930_all"] = report(live, "live last-before-midnight 09-01..09-30")
    (HERE / "results.json").write_text(json.dumps(res, ensure_ascii=False, indent=1))
    f = lambda v: "%+.3f [%+.3f,%+.3f]" % tuple(v)
    g = lambda v: "%.3f [%.3f,%.3f]" % tuple(v)

    def line(s):
        print(f"  {s['label']:44s} n={s['updates']:6d} sd={s['station_days']:5d}  bias {f(s['bias_c'])}  PIT-½ {f(s['pit_mean_minus_half'])}  E[z²] {g(s['z_meansq'])}  PITvar {g(s['pit_var_ratio'])}  cov90 {g(s['cover_90'])}  slope {s['spread_skill_slope']:.2f}  rms e/sd {s['rms_res_c']:.2f}/{s['rms_sd_c']:.2f}")

    print("== A: current form (dress_v1), replay", res["updates"], "updates", res["stations"], "stations")
    for s in res["segments"].values(): line(s)
    print("-- by lead"); [line(s) for s in res["by_lead"].values()]
    print("-- by pool size (all updates)"); [line(s) for s in res["by_pool_size"].values()]
    print("-- by pool size (first decision >36h)"); [line(s) for s in res["by_pool_size_gt36h_first"].values()]
    print("-- by unit"); [line(s) for s in res["by_unit"].values()]
    print("-- station bias (roots):", res["station_bias_roots"]["abs_t_gt_2"], "of", res["station_bias_roots"]["stations"], "with |t|>2:", res["station_bias_roots"]["list"])
    print("-- station bias (roots, 09-14+):", res["station_bias_recent_roots"]["abs_t_gt_2"], "of", res["station_bias_recent_roots"]["stations"], res["station_bias_recent_roots"]["list"])
    if live:
        print("== B: live served (stretch form)", res["source_B_versions"])
        line(res["source_B_live_last_pre_midnight_0925_0930"]); line(res["source_A_same_station_days_roots"]); line(res["source_B_live_0901_0930_all"])


if __name__ == "__main__":
    main()
