#!/usr/bin/env python3
"""Addendum 1 verdict as pre-registered: primary on 2026-04-01..09-30, halves by
truth source, 3-day date blocks, learning curve by station history length,
robustness without the four stations whose IEM and formal truth disagree."""
import gzip, json, os, sys
from collections import defaultdict
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, ROOT)
from bayes_basket_target import center_width_store as store
rows = [json.loads(l) for l in gzip.open(os.path.join(HERE, "station_day_scores_long.jsonl.gz"), "rt")]
res = json.load(open(os.path.join(HERE, "results_long.json")))
ARMS = ["EB", "H*", "HL", "ORACLE-S", "ORACLE-SL"]
def gate(rs, arm, conf=0.95):
    store.GATE_CONFIDENCE = conf
    k = [(r["station"], r["day"]) for r in rs]
    g = store._proper_score_gate([tuple(r["A0"]) for r in rs], [tuple(r[arm]) for r in rs], k); store.GATE_CONFIDENCE = 0.95; return g
def block(rs, arm, b=3, draws=2000):
    days = sorted({r["day"] for r in rs}); byday = defaultdict(list)
    for r in rs: byday[r["day"]].append(r[arm][0] - r["A0"][0])
    g = np.random.default_rng(11); out = []
    for _ in range(draws):
        ix = []
        while len(ix) < len(days):
            s = int(g.integers(len(days))); ix.extend((s + j) % len(days) for j in range(b))
        out.append(np.mean(np.concatenate([byday[days[i]] for i in ix[:len(days)]])))
    return [float(np.quantile(out, .025)), float(np.quantile(out, .975))]
def line(name, rs, arm):
    g = gate(rs, arm)
    return {"scope": name, "arm": arm, "station_days": len(rs), "d_log": g["d_logscore"], "ci_log": g["ci_logscore"], "d_brier": g["d_brier"], "ci_brier": g["ci_brier"],
            "d_crps": g["d_crps"], "ci_crps": g["ci_crps"], "lower": g["lower"], "block3": block(rs, arm)}
out = {"primary": [], "halves": [], "learning_curve": [], "robust_no4": [], "eb_tau": {}, "by_month": []}
prim = [r for r in rows if r["day"] >= "2026-04-01"]
for arm in ARMS: out["primary"].append(line("2026-04-01..09-30", prim, arm))
for name, rs in (("04-01..06-30 IEM truth", [r for r in prim if r["day"] < "2026-07-01"]), ("07-01..09-30 formal truth", [r for r in prim if r["day"] >= "2026-07-01"])):
    out["halves"].append(line(name, rs, "EB"))
for lo, hi in ((0, 30), (30, 60), (60, 120), (120, 200), (200, 999)):
    rs = [r for r in rows if lo <= r["prefix_n"] < hi]
    if len(rs) > 30: out["learning_curve"].append(line(f"prefix {lo}-{hi} days", rs, "EB") | {"days": [min(r['day'] for r in rs), max(r['day'] for r in rs)]})
no4 = [r for r in prim if r["station"] not in ("RCSS", "ZBAA", "ZGGG", "ZGSZ")]
out["robust_no4"].append(line("primary without RCSS/ZBAA/ZGGG/ZGSZ", no4, "EB"))
for m in ("2026-02", "2026-03", "2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09"):
    rs = [r for r in rows if r["day"].startswith(m)]
    if rs: out["by_month"].append({"month": m, "n": len(rs), "d_log_EB": float(np.mean([r["EB"][0] - r["A0"][0] for r in rs])), "d_log_oracle": float(np.mean([r["ORACLE-S"][0] - r["A0"][0] for r in rs]))})
from collections import Counter
out["eb_tau"] = {str(k): v for k, v in Counter(tuple(v) for v in res["chosen"]["EB"].values()).items()}
for sec in ("primary", "halves", "learning_curve", "robust_no4"):
    for x in out[sec]:
        print(f"{sec:14s} {x['scope']:42s} {x['arm']:9s} n={x['station_days']:5d} dLog={x['d_log']:+.4f} [{x['ci_log'][0]:+.4f},{x['ci_log'][1]:+.4f}] dBrier={x['d_brier']:+.4f} [{x['ci_brier'][0]:+.4f},{x['ci_brier'][1]:+.4f}] dCRPS={x['d_crps']:+.4f} lower={x['lower']} block3=[{x['block3'][0]:+.4f},{x['block3'][1]:+.4f}]")
print("by month", [(x["month"], round(x["d_log_EB"], 4), round(x["d_log_oracle"], 4)) for x in out["by_month"]])
print("EB tau chosen", out["eb_tau"])
json.dump(out, open(os.path.join(HERE, "long_verdict.json"), "w"), indent=1)
