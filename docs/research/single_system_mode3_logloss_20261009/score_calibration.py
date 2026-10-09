#!/usr/bin/env python3
"""Per-bucket calibration of the two arms (PROTOCOL.md, 2026-10-10 revision):
one-vs-rest binary log loss and the reliability diagram -- does a bucket the
P0 calls 30% happen about 30% of the time -- over every bucket of the ladder
and over the market mode +-1 window, with the market's q beside."""
from __future__ import annotations

import gzip
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from capture import Modes, _utc  # noqa: E402
STATIONS = "CYYZ,EFHK,EHAM,FACT,KAUS,KDAL,KLAX,KMIA,KSEA,LEMD,LIMC,MMMX,NZWN,OPKC,RJTT,RKSI,SAEZ,VILK,WSSS,ZGGG,ZHHH,ZSQD,ZUUU".split(",")
F_STATIONS = {"KATL", "KAUS", "KBKF", "KDAL", "KHOU", "KLAX", "KLGA", "KMIA", "KORD", "KSEA", "KSFO"}
BINS = ((48, 1e9, "≥48h"), (36, 48, "36–48h"), (24, 36, "24–36h"), (12, 24, "12–24h"), (0, 12, "<12h"))
SPLIT = "2026-09-20"

EDGES = np.array([0.0, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0 + 1e-9])
CLIP = 1e-6
DRAWS, SEED = 2000, 20261010


def bll(p, y):
    p = np.clip(p, CLIP, 1.0 - CLIP)
    return -(y * np.log(p) + (1.0 - y) * np.log(1.0 - p))


# one row per (representative decision, bucket): cluster, p_cur, p_m3, q (nan if none), y, in_window
rows = []
for station in STATIONS:
    path = HERE / "pass1" / f"{station}.jsonl.gz"
    if not path.exists():
        continue
    modes = Modes(station)
    by_day = defaultdict(list)
    for line in gzip.open(path, "rt"):
        r = json.loads(line)
        by_day[r["event_date"]].append(r)
    for day, decisions in by_day.items():
        for lo, hi, name in BINS:
            inbin = [r for r in decisions if lo <= r["hours"] < hi]
            if not inbin:
                continue
            r = max(inbin, key=lambda x: x["at"])
            found = modes.at(day, _utc(r["at"]))
            window, q = set(), None
            if found and found[0] in r["ordered"]:
                i = r["ordered"].index(found[0])
                window = set(r["ordered"][max(0, i - 1): i + 2])
                total = sum(found[1].get(b, 0.0) for b in r["ordered"])
                q = {b: found[1].get(b, 0.0) / total for b in r["ordered"]} if total > 0 else None
            for b in r["ordered"]:
                rows.append(((station, day), name, "F" if station in F_STATIONS else "C", day,
                             r["cur"]["p"][b], r["m3"]["p"][b], q[b] if q else math.nan,
                             1.0 if b == r["hit"] else 0.0, b in window, f"{station}|{day}|{name}"))

cluster_ids = {}
cl = np.array([cluster_ids.setdefault(x[0], len(cluster_ids)) for x in rows])
lead = np.array([x[1] for x in rows]); unit = np.array([x[2] for x in rows]); dayv = np.array([x[3] for x in rows])
P = {"cur": np.array([x[4] for x in rows]), "m3": np.array([x[5] for x in rows]), "mkt": np.array([x[6] for x in rows])}
Y = np.array([x[7] for x in rows]); WIN = np.array([x[8] for x in rows]); DEC = np.array([x[9] for x in rows])
NC = len(cluster_ids)
rng = np.random.default_rng(SEED)
PICK = rng.integers(0, NC, size=(DRAWS, NC))
MULT = np.zeros((DRAWS, NC))
np.add.at(MULT, (np.repeat(np.arange(DRAWS), NC), PICK.ravel()), 1.0)   # how often each station-day is drawn


def _per_cluster(mask, values):
    return np.bincount(cl[mask], weights=values, minlength=NC)


def reliability(mask, arm):
    p, y = P[arm][mask], Y[mask]
    k = np.digitize(p, EDGES) - 1
    table = []
    for j in range(len(EDGES) - 1):
        m = k == j
        if m.sum():
            table.append({"bin": [float(EDGES[j]), float(min(EDGES[j + 1], 1.0))], "n": int(m.sum()),
                          "said": float(p[m].mean()), "landed": float(y[m].mean())})
    return table


def calib_stats(mask, arm):
    """REL / RES / ECE (Murphy, the bins above) and per-cluster sums for the bootstrap."""
    p, y = P[arm][mask], Y[mask]
    k = np.digitize(p, EDGES) - 1
    nb = len(EDGES) - 1
    c = cl[mask]
    n = np.zeros((NC, nb)); sp = np.zeros((NC, nb)); sy = np.zeros((NC, nb))
    np.add.at(n, (c, k), 1.0); np.add.at(sp, (c, k), p); np.add.at(sy, (c, k), y)
    return n, sp, sy


def rel_from(n, sp, sy):
    N = n.sum(axis=-2)
    tot = N.sum(axis=-1)
    with np.errstate(invalid="ignore", divide="ignore"):
        pbar = np.where(N > 0, sp.sum(axis=-2) / N, 0.0)
        obar = np.where(N > 0, sy.sum(axis=-2) / N, 0.0)
    base = sy.sum(axis=(-2, -1)) / tot
    rel = (N * (pbar - obar) ** 2).sum(axis=-1) / tot
    res = (N * (obar - base[..., None]) ** 2).sum(axis=-1) / tot
    ece = (N * np.abs(pbar - obar)).sum(axis=-1) / tot
    return rel, res, ece


def panel(mask, arms):
    out = {"bucket_rows": int(mask.sum()), "decisions": int(len(set(DEC[mask]))), "station_days": int(len(set(cl[mask])))}
    stats = {a: calib_stats(mask, a) for a in arms}
    for a in arms:
        rel, res, ece = rel_from(*stats[a])
        loss = bll(P[a][mask], Y[mask])
        out[a] = {"bll_per_bucket": float(loss.mean()),
                  "bll_per_decision": float(loss.sum() / out["decisions"]),
                  "REL": float(rel), "RES": float(res), "ECE": float(ece),
                  "reliability": reliability(mask, a)}
    # paired deltas, station-day bootstrap
    def boot_delta(a, b):
        la, lb = bll(P[a][mask], Y[mask]), bll(P[b][mask], Y[mask])
        d = _per_cluster(mask, la - lb); cnt = np.bincount(cl[mask], minlength=NC).astype(float)
        draws = (MULT @ d) / (MULT @ cnt)
        point = float(d.sum() / cnt.sum())
        na, spa, sya = stats[a]; nb_, spb, syb = stats[b]
        rel_a = rel_from((MULT @ na)[:, None, :], (MULT @ spa)[:, None, :], (MULT @ sya)[:, None, :])
        rel_b = rel_from((MULT @ nb_)[:, None, :], (MULT @ spb)[:, None, :], (MULT @ syb)[:, None, :])
        dr = rel_a[0] - rel_b[0]; de = rel_a[2] - rel_b[2]
        return {"d_bll": [point, float(np.quantile(draws, 0.025)), float(np.quantile(draws, 0.975))],
                "d_REL": [float(rel_from(na, spa, sya)[0] - rel_from(nb_, spb, syb)[0]),
                          float(np.quantile(dr, 0.025)), float(np.quantile(dr, 0.975))],
                "d_ECE": [float(rel_from(na, spa, sya)[2] - rel_from(nb_, spb, syb)[2]),
                          float(np.quantile(de, 0.025)), float(np.quantile(de, 0.975))]}
    out["m3_minus_cur"] = boot_delta("m3", "cur")
    if "mkt" in arms:
        out["cur_minus_mkt"] = boot_delta("cur", "mkt")
        out["m3_minus_mkt"] = boot_delta("m3", "mkt")
    return out


has_q = ~np.isnan(P["mkt"])
everything = np.ones(len(Y), bool)
result = {
    "全部桶": panel(everything, ("cur", "m3")),
    "众数±1窗口": panel(WIN, ("cur", "m3", "mkt")),
    "全部桶·有盘口·≥12h": panel(has_q & (lead != "<12h"), ("cur", "m3", "mkt")),
    "众数±1·≥12h": panel(WIN & (lead != "<12h"), ("cur", "m3", "mkt")),
    "全部桶·前半": panel(dayv < SPLIT, ("cur", "m3")),
    "全部桶·后半": panel(dayv >= SPLIT, ("cur", "m3")),
    "全部桶·F站": panel(unit == "F", ("cur", "m3")),
    "全部桶·C站": panel(unit == "C", ("cur", "m3")),
}
for _lo, _hi, name in BINS:
    result[f"全部桶·{name}"] = panel(lead == name, ("cur", "m3"))
(HERE / "results_calibration.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))


def ci(x, d=4):
    return f"{x[0]:+.{d}f} [{x[1]:+.{d}f},{x[2]:+.{d}f}]"


for name, r in result.items():
    arms = [a for a in ("cur", "m3", "mkt") if a in r]
    print(f"== {name}: {r['bucket_rows']} 桶行 / {r['decisions']} 决策 / {r['station_days']} 站日")
    print("   " + "  ".join(f"{a}: BLL/桶 {r[a]['bll_per_bucket']:.4f} REL {r[a]['REL']*1e3:.3f}e-3 ECE {r[a]['ECE']:.4f} RES {r[a]['RES']*1e3:.2f}e-3" for a in arms))
    for key in ("m3_minus_cur", "cur_minus_mkt", "m3_minus_mkt"):
        if key in r:
            x = r[key]
            print(f"   {key}: ΔBLL/桶 {ci(x['d_bll'], 5)}  ΔREL {ci([v*1e3 for v in x['d_REL']], 3)}e-3  ΔECE {ci(x['d_ECE'])}")
for name in ("全部桶", "众数±1窗口"):
    r = result[name]
    arms = [a for a in ("cur", "m3", "mkt") if a in r]
    print(f"\n可靠性图 · {name}")
    for j in range(len(EDGES) - 1):
        key = (float(EDGES[j]), float(min(EDGES[j + 1], 1.0)))
        line = f"  {key[0]:.2f}–{key[1]:.2f}"
        for a in arms:
            t = {tuple(x["bin"]): x for x in r[a]["reliability"]}.get(key)
            line += f" | {a} n={t['n']:5d} 说 {t['said']:.3f} 中 {t['landed']:.3f}" if t else f" | {a} —"
        print(line)
