#!/usr/bin/env python3
"""Is "centres far apart and the families agree -> the model is worse" a fact?
Stratify consistency inside |Δμ| bins, both windows (main 09-12..10-05; early
08-17..09-11, not read by the condition study).  Read-only; prints a table and
writes results_fact.json.
"""
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import analysis as an  # noqa: E402

BINS = [("0.3-1", 0.3, 1.0), ("1-2", 1.0, 2.0), ("≥2", 2.0, 99.0)]
CONS = [("<0.8", lambda c: c < 0.8), ("≥0.8", lambda c: c >= 0.8)]


def read(items):
    if not items:
        return {"station_days": 0}
    days = sorted({f["d"] for f in items})
    boot = an.Boot(days)
    num, den = an.day_sums(items, days, lambda f: f["d_"], lambda f: f["wt"])
    s = boot.summarise(num, den) or {}
    loses = sum(f["wt"] for f in items if f["d_"] < 0) / sum(f["wt"] for f in items)
    return {"station_days": len({(f["st"], f["d"]) for f in items}), "days": len(days),
            "mean_abs_dmu": float(np.average([abs(f["dmu"]) for f in items], weights=[f["wt"] for f in items])),
            "share_model_worse": loses, **s}


def main():
    out = {}
    for wname, rows_dir in (("main", "rows"), ("early", "rows_early")):
        feats = an.featurise(an.load(os.path.join(HERE, rows_dir)))
        cnt = {}
        for f in feats:
            cnt[(f["fam"], f["st"], f["d"])] = cnt.get((f["fam"], f["st"], f["d"]), 0) + 1
        w = {}
        for fam in ("P0>36h", "P0-M", "P1"):
            ff = [f for f in feats if f["fam"] == fam and f["consist"] is not None]
            fam_res = {}
            for bname, lo, hi in BINS:
                for cname, rule in CONS:
                    items = [f for f in ff if lo <= abs(f["dmu"]) < hi and rule(f["consist"])]
                    fam_res[f"{bname}|{cname}"] = read(items)
            fam_res["|Δμ|<0.3"] = read([f for f in feats if f["fam"] == fam and abs(f["dmu"]) < 0.3])
            w[fam] = fam_res
        out[wname] = w
    json.dump(out, open(os.path.join(HERE, "results_fact.json"), "w"), ensure_ascii=False, indent=1)
    def fm(x): return "—" if x is None else f"{x:+.3f}"
    for wname, w in out.items():
        for fam, res in w.items():
            print(f"\n== {wname} {fam}")
            for k, r in res.items():
                if not r.get("station_days"):
                    print(f"  {k:12s} n=0"); continue
                print(f"  {k:12s} n={r['station_days']:4d}/{r['days']:2d}d |Δμ|={r['mean_abs_dmu']:.2f} "
                      f"E[d] {fm(r.get('mean'))} [{fm(r.get('lo'))},{fm(r.get('hi'))}] model worse {r['share_model_worse']:.0%}")


if __name__ == "__main__":
    main()
