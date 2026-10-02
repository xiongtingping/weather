#!/usr/bin/env python3
"""Score PROTOCOL.md: each downstream arm against base under charter 240, on
all days and 09-14..10-01, plus the thin-far subset for the slice arms.
Writes results.json."""
from __future__ import annotations

import gzip
import importlib.util
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "score_fit", HERE.parent / "single_system_stretch_vs_dress_20261002" / "score_fit.py")
S = importlib.util.module_from_spec(spec)
spec.loader.exec_module(S)
ARMS = ("bridge_off", "bridge_old", "bridge_early", "bridge_lmax06", "rad_off", "rad_half", "rad_x15",
        "lam_0.6", "lam_0.9", "lam_1.0", "lam_1.2", "slice_1.1", "slice_1.2", "slice_1.3")
LAYER = {"bridge": ARMS[:4], "radiation": ARMS[4:7], "width_lambda": ARMS[7:11], "slice_239": ARMS[11:]}


def load():
    rows = []
    for path in sorted((HERE / "pass1_down").glob("*.jsonl.gz")):
        with gzip.open(path, "rt") as handle:
            rows.extend(json.loads(line) for line in handle)
    return rows


def main():
    rows = load()
    seg = {"all": rows, "recent_0914_1001": [r for r in rows if r["event_date"] >= "2026-09-14"]}
    thin_far = [r for r in rows if r["base"].get("seated", 9) <= 3 and (r["base"].get("hours_to_settlement") or 0) > 36.0]
    res = {"protocol": "PROTOCOL.md", "updates": len(rows), "stations": len({r["station"] for r in rows}),
           "width_layer_active_share": sum(bool(r["base"].get("cw_active")) for r in rows) / len(rows),
           "slice_applied_share": sum(bool(r["base"].get("slice")) for r in rows) / len(rows),
           "radiation_signal_share": sum(r["base"].get("cooling_shift_sigma") is not None for r in rows) / len(rows),
           "thin_far_updates": len(thin_far),
           "vs_base": {a: {k: S.compare(v, a, "base") for k, v in seg.items()} for a in ARMS}}
    for a in ("slice_1.1", "slice_1.2", "slice_1.3", "lam_1.2", "lam_1.0"):
        res["vs_base"][a]["thin_far_le3_gt36h"] = S.compare(thin_far, a, "base")
    verdict = {}
    for layer, arms in LAYER.items():
        passing = [a for a in arms if res["vs_base"][a]["all"]["verdict_240"] and res["vs_base"][a]["recent_0914_1001"]["verdict_240"]]
        verdict[layer] = {"arms_passing_both": passing, "needs_sync": bool(passing)}
    res["verdict"] = verdict
    (HERE / "results.json").write_text(json.dumps(res, ensure_ascii=False, indent=1))
    f = lambda v: "%+.5f [%+.5f,%+.5f]" % tuple(v)
    print("updates", res["updates"], "stations", res["stations"], "width layer active", round(res["width_layer_active_share"], 3),
          "slice applied", round(res["slice_applied_share"], 3), "thin-far updates", res["thin_far_updates"])
    for layer, arms in LAYER.items():
        print("==", layer, "needs_sync:", verdict[layer]["needs_sync"], verdict[layer]["arms_passing_both"])
        for a in arms:
            for k, c in res["vs_base"][a].items():
                print(f"  {a:14s} {k:20s} sd={c['station_days']:5d}  dLog {f(c['d_nll'])}  dCRPS {f(c['d_crps'])}  dBrier {f(c['d_brier'])}  -> {'PASS' if c['verdict_240'] else 'FAIL'}")


if __name__ == "__main__":
    main()
