import gzip, json, os, sys
from collections import defaultdict
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE))); sys.path.insert(0, ROOT)
from bayes_basket_target import center_width_store as store
rows = [json.loads(l) for l in gzip.open(os.path.join(HERE, "rows.jsonl.gz"), "rt")]
def tab(arm):
    acc = defaultdict(list)
    for r in rows: acc[(r["station"], r["day"])].append(r["s"][arm])
    return {k: tuple(float(np.mean([t[i] for t in v])) for i in range(3)) for k, v in acc.items()}
T = {a: tab(a) for a in rows[0]["s"]}; k = sorted(T["k1"]); out = {}
def gate(a, b, conf):
    res = {}
    for name, p in (("all", lambda x: True), ("h1", lambda x: x[1] < "2026-09-10"), ("h2", lambda x: x[1] >= "2026-09-10")):
        kk = [x for x in k if p(x)]; store.GATE_CONFIDENCE = conf
        g = store._proper_score_gate([T[b][x] for x in kk], [T[a][x] for x in kk], kk); store.GATE_CONFIDENCE = 0.95
        res[name] = {f: g[f] for f in ("d_logscore", "ci_logscore", "d_brier", "ci_brier", "d_crps", "ci_crps", "lower")}
    return res
def show(label, r):
    x = r["all"]; print(f"{label:28s} dLog={x['d_logscore']:+.4f} [{x['ci_logscore'][0]:+.4f},{x['ci_logscore'][1]:+.4f}] dBrier={x['d_brier']:+.4f} [{x['ci_brier'][0]:+.4f},{x['ci_brier'][1]:+.4f}] dCRPS={x['d_crps']:+.4f} [{x['ci_crps'][0]:+.4f},{x['ci_crps'][1]:+.4f}] lower={x['lower']} halves={r['h1']['d_logscore']:+.4f}/{r['h2']['d_logscore']:+.4f}")
print("station-days", len(k)); print("A. physics shift arms vs k=1 (99.375%):")
for a in ("k0", "k0.5", "k1.5", "k2", "warm0.5", "warm1.5", "cold0.5", "cold1.5"):
    out[a] = gate(a, "k1", 1 - 0.05 / 8); show(a, out[a])
print("B. with-physics recheck (95%, candidate vs alternative):")
for label, a, b in (("242 served vs seat", "k1", "seat"), ("245 thin 1.1 vs 0.75", "k1", "thin0.75"), ("246 bridge new vs old", "k1", "bridge_old")):
    out[label] = gate(a, b, 0.95); show(label, out[label])
sig = [r["signal"] for r in rows if r["signal"] is not None]
print("signal: share>0", round(np.mean([s > 0 for s in sig]), 3), "mean", round(float(np.mean(sig)), 4), "p10/p90", np.round(np.percentile(sig, [10, 90]), 3))
json.dump(out, open(os.path.join(HERE, "results.json"), "w"), indent=1)
