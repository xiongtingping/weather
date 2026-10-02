#!/usr/bin/env python3
"""Join the production dress_v1 verification rows to the study's stored arms
and judge prod against base (stretch) and against the dress arm.  Writes
results_prod.json."""
from __future__ import annotations

import gzip
import json
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location("score_fit", HERE / "score_fit.py")
S = importlib.util.module_from_spec(spec)
spec.loader.exec_module(S)


def load(folder):
    rows = []
    for path in sorted((HERE / folder).glob("*.jsonl.gz")):
        with gzip.open(path, "rt") as handle:
            rows.extend(json.loads(line) for line in handle)
    return rows


def main():
    prod = load("pass1_prod")
    fit = {(r["station"], r["event_date"], r["at"]): r for r in load("pass1_fit")}
    joined, missing = [], 0
    for r in prod:
        ref = fit.get((r["station"], r["event_date"], r["at"]))
        if ref is None or ref["pool"] != r["pool"]:
            missing += 1
            continue
        joined.append({**{k: r[k] for k in ("station", "event_date", "tz", "at", "pool")},
                       "prod": r["base"], "base": ref["base"], "dress": ref["dress"], "fit": r["fit"], "fit_ref": ref["fit"]})
    seg = {"all": joined, "recent_0914_1001": [r for r in joined if r["event_date"] >= "2026-09-14"],
           "early_0730_0913": [r for r in joined if r["event_date"] <= "2026-09-13"],
           "high_spread_dM_ge_1p5": [r for r in joined if any(f["ratio"] >= 1.5 for f in r["fit"].values())]}
    res = {"prod_updates": len(prod), "joined": len(joined), "unjoined": missing,
           "stations": len({r["station"] for r in joined}),
           "prod_vs_base": {k: S.compare(v, "prod", "base") for k, v in seg.items()},
           "prod_vs_dress": {k: S.compare(v, "prod", "dress") for k, v in seg.items()}}
    fits = [f for r in joined for f in r["fit"].values()]
    refs = [f for r in joined for f in r["fit_ref"].values()]
    res["single_system_multimodal_share"] = {
        "prod": float(np.mean([f["modes"] >= 2 for f in fits])),
        "base": float(np.mean([f["base_modes"] >= 2 for f in refs])),
        "dress": float(np.mean([f["dress_modes"] >= 2 for f in refs])),
    }
    res["spread_factor_p10_50_90"] = {"prod": S.pct([f["s"] for f in fits]), "base": S.pct([f["base_s"] for f in refs])}
    res["pooled_multimodal_share"] = {a: float(np.mean([S.modes(r[a]["vec"]) >= 2 for r in joined])) for a in ("prod", "base", "dress")}
    (HERE / "results_prod.json").write_text(json.dumps(res, ensure_ascii=False, indent=1))
    f = lambda v: "%+.5f [%+.5f,%+.5f]" % tuple(v)
    print("prod updates", len(prod), "joined", len(joined), "unjoined", missing, "stations", res["stations"])
    print("single-system >=2 modes", res["single_system_multimodal_share"], "pooled", res["pooled_multimodal_share"])
    print("s p10/50/90", {k: [round(x, 3) for x in v] for k, v in res["spread_factor_p10_50_90"].items()})
    for name in ("prod_vs_base", "prod_vs_dress"):
        print("==", name)
        for k, c in res[name].items():
            print(f"  {k:24s} sd={c['station_days']:5d} d={c['event_days']:2d}  dLog {f(c['d_nll'])}  dCRPS {f(c['d_crps'])}  dBrier {f(c['d_brier'])}  -> {'PASS' if c['verdict_240'] else 'FAIL'}{' no-harm' if c['no_harm'] else ' HARM'}")


if __name__ == "__main__":
    main()
