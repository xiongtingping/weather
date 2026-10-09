#!/usr/bin/env python3
"""Score the two arms (PROTOCOL.md): layer A by charter 240's gate on the served
P0, layer B by the binary log loss of the market mode +-1 window at the
decision instant (ours vs ours, and each against the market)."""
from __future__ import annotations

import gzip
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
from bayes_basket_target import center_width_store as store  # noqa: E402
from capture import Modes, _utc  # noqa: E402

STATIONS = "CYYZ,EFHK,EHAM,FACT,KAUS,KDAL,KLAX,KMIA,KSEA,LEMD,LIMC,MMMX,NZWN,OPKC,RJTT,RKSI,SAEZ,VILK,WSSS,ZGGG,ZHHH,ZSQD,ZUUU".split(",")
F_STATIONS = {"KATL", "KAUS", "KBKF", "KDAL", "KHOU", "KLAX", "KLGA", "KMIA", "KORD", "KSEA", "KSFO"}
BINS = ((48, 1e9, "≥48h"), (36, 48, "36–48h"), (24, 36, "24–36h"), (12, 24, "12–24h"), (0, 12, "<12h"))
SPLIT = "2026-09-20"
CLIP = 1e-6


def bll(p, y):
    p = min(1.0 - CLIP, max(CLIP, float(p)))
    return -(y * math.log(p) + (1.0 - y) * math.log(1.0 - p))


def boot(values, clusters, draws=2000, seed=20261009):
    ids = {}
    index = np.array([ids.setdefault(c, len(ids)) for c in clusters])
    v = np.asarray(values, float)
    sums = np.bincount(index, weights=v, minlength=len(ids))
    counts = np.bincount(index, minlength=len(ids)).astype(float)
    pick = np.random.default_rng(seed).integers(0, len(ids), size=(draws, len(ids)))
    means = sums[pick].sum(axis=1) / counts[pick].sum(axis=1)
    return [float(v.mean()), float(np.quantile(means, 0.025)), float(np.quantile(means, 0.975))]


cells = []
missing = [s for s in STATIONS if not (HERE / "pass1" / f"{s}.jsonl.gz").exists()]
if missing:
    print("capture incomplete:", missing)
for station in STATIONS:
    path = HERE / "pass1" / f"{station}.jsonl.gz"
    if not path.exists():
        continue
    modes = Modes(station)
    by_day = defaultdict(list)
    for line in gzip.open(path, "rt"):
        r = json.loads(line)
        by_day[r["event_date"]].append(r)
    for day, rows in by_day.items():
        for lo, hi, name in BINS:
            inbin = [r for r in rows if lo <= r["hours"] < hi]
            if not inbin:
                continue
            r = max(inbin, key=lambda x: x["at"])
            cell = {"station": station, "day": day, "bin": name, "unit": "F" if station in F_STATIONS else "C",
                    "A": {arm: (r[arm]["m"]["nll"], r[arm]["m"]["brier"], r[arm]["m"]["crps"]) for arm in ("cur", "m3")}}
            found = modes.at(day, _utc(r["at"]))
            if found and found[0] in r["ordered"]:
                mode, q = found
                i = r["ordered"].index(mode)
                window = r["ordered"][max(0, i - 1): i + 2]
                total = sum(q.get(b, 0.0) for b in r["ordered"])
                cell["B"] = {
                    "cur": sum(bll(r["cur"]["p"][b], 1.0 if b == r["hit"] else 0.0) for b in window),
                    "m3": sum(bll(r["m3"]["p"][b], 1.0 if b == r["hit"] else 0.0) for b in window),
                    "mkt": sum(bll(q.get(b, 0.0) / total, 1.0 if b == r["hit"] else 0.0) for b in window),
                    "hit_in_window": r["hit"] in window, "hit_mode": r["hit"] == mode,
                    "p_mode": {"cur": r["cur"]["p"][mode], "m3": r["m3"]["p"][mode], "mkt": q.get(mode, 0.0) / total},
                }
            cells.append(cell)


def gate(subset):
    keys = [(c["station"], c["day"]) for c in subset]
    g = store._proper_score_gate([c["A"]["cur"] for c in subset], [c["A"]["m3"] for c in subset], keys)
    return {k: g[k] for k in ("cells", "station_days", "d_logscore", "ci_logscore", "d_brier", "ci_brier",
                                 "d_crps", "ci_crps", "lower")} | {
        "logscore_cur": float(np.mean([c["A"]["cur"][0] for c in subset])),
        "logscore_m3": float(np.mean([c["A"]["m3"][0] for c in subset]))}


def layer_b(subset):
    sub = [c for c in subset if "B" in c]
    if len(sub) < 15:
        return None
    keys = [(c["station"], c["day"]) for c in sub]
    out = {"cells": len(sub), "station_days": len(set(keys))}
    for arm in ("cur", "m3", "mkt"):
        out[f"bll_{arm}"] = float(np.mean([c["B"][arm] for c in sub]))
    out["d_m3_cur"] = boot([c["B"]["m3"] - c["B"]["cur"] for c in sub], keys)
    out["d_cur_mkt"] = boot([c["B"]["cur"] - c["B"]["mkt"] for c in sub], keys)
    out["d_m3_mkt"] = boot([c["B"]["m3"] - c["B"]["mkt"] for c in sub], keys)
    out["mode_said"] = {arm: float(np.mean([c["B"]["p_mode"][arm] for c in sub])) for arm in ("cur", "m3", "mkt")}
    out["mode_landed"] = float(np.mean([c["B"]["hit_mode"] for c in sub]))
    out["window_landed"] = float(np.mean([c["B"]["hit_in_window"] for c in sub]))
    return out


slices = {"all": lambda c: True, "前半": lambda c: c["day"] < SPLIT, "后半": lambda c: c["day"] >= SPLIT,
          "C站": lambda c: c["unit"] == "C", "F站": lambda c: c["unit"] == "F"}
for _lo, _hi, name in BINS:
    slices[name] = (lambda n: lambda c: c["bin"] == n)(name)
result = {}
for name, pred in slices.items():
    subset = [c for c in cells if pred(c)]
    if not subset:
        continue
    result[name] = {"A": gate(subset), "B": layer_b(subset)}

fits = defaultdict(list)
for station in STATIONS:
    path = HERE / "pass1" / f"{station}.jsonl.gz"
    if not path.exists():
        continue
    for line in gzip.open(path, "rt"):
        r = json.loads(line)
        for f in r["fit"].values():
            if "m3_s" in f:
                for k in ("cur_shift", "m3_shift", "cur_s", "m3_s", "moment_s"):
                    fits[k].append(f[k])
                fits["fallback"].append(1.0 if f.get("fallback") else 0.0)
result["fit_summary"] = {k: {"median": float(np.median(v)), "mean": float(np.mean(v))} for k, v in fits.items()}
result["fit_summary"]["|shift| cur/m3"] = [float(np.mean(np.abs(fits["cur_shift"]))), float(np.mean(np.abs(fits["m3_shift"])))]
(HERE / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))


def f(x, d=4):
    return "—" if x is None else f"{x:+.{d}f}"


for name, r in result.items():
    if name == "fit_summary":
        continue
    a, b = r["A"], r["B"]
    print(f"{name:7s} A n={a['cells']:5d} sd={a['station_days']:4d} ΔLog={f(a['d_logscore'])} [{f(a['ci_logscore'][0])},{f(a['ci_logscore'][1])}]"
          f" ΔBrier={f(a['d_brier'])} [{f(a['ci_brier'][0])},{f(a['ci_brier'][1])}] ΔCRPS={f(a['d_crps'])} [{f(a['ci_crps'][0])},{f(a['ci_crps'][1])}] lower={a['lower']}")
    if b:
        print(f"        B n={b['cells']:5d} BLL cur/m3/mkt={b['bll_cur']:.4f}/{b['bll_m3']:.4f}/{b['bll_mkt']:.4f}"
              f" Δm3−cur={f(b['d_m3_cur'][0])} [{f(b['d_m3_cur'][1])},{f(b['d_m3_cur'][2])}]"
              f" Δcur−mkt={f(b['d_cur_mkt'][0])} [{f(b['d_cur_mkt'][1])},{f(b['d_cur_mkt'][2])}]"
              f" Δm3−mkt={f(b['d_m3_mkt'][0])} [{f(b['d_m3_mkt'][1])},{f(b['d_m3_mkt'][2])}]"
              f" 众数说 cur/m3/mkt={b['mode_said']['cur']:.3f}/{b['mode_said']['m3']:.3f}/{b['mode_said']['mkt']:.3f} 中={b['mode_landed']:.3f}")
print(json.dumps(result["fit_summary"], ensure_ascii=False))
