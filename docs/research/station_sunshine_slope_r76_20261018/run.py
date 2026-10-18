"""Round 76 (PROTOCOL.md): a per-station forecast-sunshine slope on top of r75's L3.

Reuses round 75's dataset and walk (``deterministic_centre_mos_r75_20261018/
run.py``) unchanged; adds L3st and scores it.  Read-only.

    uv run python -I docs/research/station_sunshine_slope_r76_20261018/run.py --arm=O
    uv run python -I docs/research/station_sunshine_slope_r76_20261018/run.py --arm=N
"""

from __future__ import annotations

import importlib.util
import json
import math
import statistics
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
R75_DIR = HERE.parent / "deterministic_centre_mos_r75_20261018"
spec = importlib.util.spec_from_file_location("r75", R75_DIR / "run.py")
R75 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R75)
L = R75.L

CLIP = 5.0
MIN_ROWS = 10
FOCUS = "ZUUU"


def sunshine(row, system, arm):
    if arm == "O":
        om = row.get("om") or {}
        return om.get("sw_mj")
    phys = row["members"].get(system, {}).get("phys") or {}
    return phys.get("sw_mj")


def slopes(train, l3, arm):
    """Per member: {station: (delta, se2, mean_sw)} and tau2."""

    out = {}
    for system in R75.SYSTEMS:
        pairs = defaultdict(list)
        for r in train:
            key = (r["station"], r["day"])
            if system not in l3.get(key, {}):
                continue
            sw = sunshine(r, system, arm)
            if sw is None:
                continue
            resid = max(-CLIP, min(CLIP, r["truth"] - l3[key][system]))
            pairs[r["station"]].append((float(sw), resid))
        fits = {}
        for station, xy in pairs.items():
            if len(xy) < MIN_ROWS:
                continue
            x = np.array([a for a, _ in xy])
            y = np.array([b for _, b in xy])
            sxx = float(((x - x.mean()) ** 2).sum())
            if sxx <= 1e-9:
                continue
            delta = float(((x - x.mean()) * (y - y.mean())).sum() / sxx)
            sse = float(((y - y.mean() - delta * (x - x.mean())) ** 2).sum())
            se2 = sse / (len(xy) - 2) / sxx
            fits[station] = (delta, se2, float(x.mean()))
        if len(fits) >= 2:
            deltas = np.array([f[0] for f in fits.values()])
            tau2 = max(float(deltas.var(ddof=1)) - statistics.mean(f[1] for f in fits.values()), 0.0)
        else:
            tau2 = 0.0
        out[system] = (fits, tau2)
    return out


def main(arm: str) -> None:
    R75.FEATURE_SOURCE = "openmeteo" if arm == "O" else "native"
    rows = json.load(open(R75_DIR / "dataset.json"))
    walk = R75.Walk(rows)
    walk.run()
    a0 = {}
    if R75.R73.exists():
        for line in open(R75.R73):
            q = json.loads(line)
            a0[(q["station"], q["day"])] = q["arms"]["A0"]
    cache = {}
    out = []
    focus_k = {}
    for r in rows:
        day = date.fromisoformat(r["day"])
        if not (R75.EVAL_FROM <= day <= R75.EVAL_TO) or len(r["xst"]) < R75.MIN_MEMBERS:
            continue
        key = (r["station"], r["day"])
        if r["day"] not in cache:
            cache[r["day"]] = slopes(walk.train(r["listing"]), walk.l3, arm)
        fitted = cache[r["day"]]
        members = {}
        for system, value in walk.l3[key].items():
            add = 0.0
            fits, tau2 = fitted.get(system, ({}, 0.0))
            sw = sunshine(r, system, arm)
            if sw is not None and r["station"] in fits and tau2 > 0:
                delta, se2, mean_sw = fits[r["station"]]
                k = tau2 / (tau2 + se2)
                add = k * delta * (float(sw) - mean_sw)
                if r["station"] == FOCUS:
                    focus_k[system] = (r["day"], round(k, 3), round(delta, 3), round(math.sqrt(se2), 3), round(tau2, 4))
            members[system] = value + add
        arms = {
            "L2": statistics.median(r["xst"][s] + walk.l2[key][s] for s in r["xst"]),
            "L3": statistics.median(walk.l3[key].values()),
            "L3st": statistics.median(members.values()),
        }
        if key in a0:
            arms["F"] = a0[key]
        edges = r["edges"]
        if edges:
            tb = L._rung_of(edges, r["truth"])
            hit = {k: L._rung_of(edges, v) == tb for k, v in arms.items()}
        else:
            hit = {k: math.floor(v + 0.5) == math.floor(r["truth"] + 0.5) for k, v in arms.items()}
        out.append({"station": r["station"], "day": r["day"], "truth": r["truth"],
                    "arms": {k: round(v, 3) for k, v in arms.items()}, "hit": hit})
    with open(HERE / f"walk_{arm}.jsonl", "w") as fh:
        for q in out:
            fh.write(json.dumps(q) + "\n")
    print(f"# 第 76 轮 臂 {arm}（{'Open-Meteo' if arm == 'O' else '原生'}）")
    arms = ["L2", "L3", "L3st"]
    R75.table("全队", out, arms)
    for a, b in (("L3st", "L3"), ("L3st", "L2")):
        p, lo, hi, h = R75.paired(out, a, b)
        print(f"{a}−{b}: {p:+.4f} [{lo:+.4f}, {hi:+.4f}]  两半 {h[0]:+.4f} / {h[1]:+.4f}")
    inter = [q for q in out if "F" in q["arms"]]
    if inter:
        R75.table("与现行 F 的交集", inter, arms + ["F"])
        p, lo, hi, h = R75.paired(inter, "L3st", "F")
        print(f"L3st−F: {p:+.4f} [{lo:+.4f}, {hi:+.4f}]  两半 {h[0]:+.4f} / {h[1]:+.4f}")
    z = [q for q in out if q["station"] == FOCUS]
    zf = [q for q in z if "F" in q["arms"]]
    R75.table(f"{FOCUS}（只报不判）", z, arms)
    for a, b, rows_ in (("L3st", "L3", z), ("L3st", "L2", z), ("L3st", "F", zf)):
        if rows_:
            p, lo, hi, h = R75.paired(rows_, a, b)
            print(f"{FOCUS} {a}−{b}（{len(rows_)} 天）: {p:+.3f} [{lo:+.3f}, {hi:+.3f}]  两半 {h[0]:+.3f} / {h[1]:+.3f}")
    print(f"{FOCUS} 最后一次斜率（日, k, δ °C/MJ, se, τ²）：")
    for system, v in sorted(focus_k.items()):
        print(f"  {system}: {v}")


if __name__ == "__main__":
    arm = next((a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--arm=")), "O")
    main(arm)
