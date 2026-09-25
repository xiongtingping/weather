#!/usr/bin/env python3
"""Flips of the cross gate between the floor off and on (PROTOCOL.md); writes results.json."""
from __future__ import annotations

import gzip
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
AS_OF = "2026-09-25T090000Z"
F_STATIONS = {"KATL", "KAUS", "KBKF", "KDAL", "KHOU", "KLAX", "KLGA", "KMIA", "KORD", "KSEA", "KSFO"}
FLOORED_NLL = -math.log(1e-3)


def load(mode):
    out = {}
    for line in gzip.open(HERE / "runs" / f"{AS_OF}_{mode}.jsonl.gz", "rt"):
        rec = json.loads(line)
        for c in rec["candidates"]:
            out[json.dumps(c["key"])] = c
    return out


def main():
    off, on = load("off"), load("on")
    shared = sorted(set(off) & set(on))
    table = defaultdict(Counter); by_station = defaultdict(Counter); flips = []
    groups = defaultdict(lambda: {"off": 0, "on": 0})
    means = defaultdict(list)
    for key in shared:
        a, b = off[key], on[key]
        station, lead = a["key"][0], a["key"][2]
        unit = "F" if station in F_STATIONS else "C"
        cell = {(True, True): "both_pass", (False, False): "both_reject",
                (False, True): "reject_to_pass", (True, False): "pass_to_reject"}[(a["pass"], b["pass"])]
        table[unit][cell] += 1; by_station[station][cell] += 1
        groups[(station, lead)]["off"] += a["pass"]; groups[(station, lead)]["on"] += b["pass"]
        for arm, rec in (("off", a), ("on", b)):
            means[(arm, "learned_brier")].append(rec["oos_metrics"]["brier"]); means[(arm, "learned_nll")].append(rec["oos_metrics"]["nll"])
            means[(arm, "climate_brier")].append(rec["climate_metrics"]["brier"]); means[(arm, "climate_nll")].append(rec["climate_metrics"]["nll"])
        if cell in ("reject_to_pass", "pass_to_reject"):
            floored_learned = sum(1 for d in a["daily"] if d["learned"]["nll"] > FLOORED_NLL)
            floored_climate = sum(1 for d in a["daily"] if d["climate"]["nll"] > FLOORED_NLL)
            flips.append({"key": a["key"], "cell": cell, "path_off": a["path"], "path_on": b["path"],
                          "floored_learned_days": floored_learned, "floored_climate_days": floored_climate,
                          "off": {"learned": a["oos_metrics"], "climate": a["climate_metrics"]},
                          "on": {"learned": b["oos_metrics"], "climate": b["climate_metrics"]}})
    unexplained = [f for f in flips if f["floored_learned_days"] + f["floored_climate_days"] == 0]
    lost = sorted(f"{s} {l}" for (s, l), g in groups.items() if g["off"] > 0 and g["on"] == 0)
    gained = sorted(f"{s} {l}" for (s, l), g in groups.items() if g["off"] == 0 and g["on"] > 0)
    result = {
        "as_of": AS_OF, "shared_keys": len(shared), "only_off": len(set(off) - set(on)), "only_on": len(set(on) - set(off)),
        "table": {u: dict(c) for u, c in table.items()},
        "by_station": {s: dict(c) for s, c in sorted(by_station.items()) if c.get("reject_to_pass") or c.get("pass_to_reject")},
        "station_lead_groups_lost_all": lost, "station_lead_groups_gained": gained,
        "flips": flips, "unexplained_flips": len(unexplained),
        "means": {f"{arm}_{m}": sum(v) / len(v) for (arm, m), v in means.items()},
    }
    (HERE / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))
    print("shared", len(shared), "only_off", result["only_off"], "only_on", result["only_on"])
    for u, c in result["table"].items():
        print(u, c)
    print("by station (flips only):", {s: {k: v for k, v in c.items() if k in ('reject_to_pass', 'pass_to_reject')} for s, c in result["by_station"].items()})
    print("groups lost all:", lost, "| gained:", gained)
    print("flips:", len(flips), "| unexplained (no floored holdout day):", len(unexplained))
    print("means:", {k: round(v, 4) for k, v in sorted(result["means"].items())})
    paths = Counter((f["cell"], f["path_off"], f["path_on"]) for f in flips)
    print("flip paths:", dict(paths))


if __name__ == "__main__":
    main()
