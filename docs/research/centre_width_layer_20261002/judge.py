import gzip, json, os, sys
from collections import defaultdict, Counter
from datetime import date, datetime, timedelta
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE))); sys.path.insert(0, ROOT)
from bayes_basket_target import center_width_store as store
rows = [json.loads(l) for l in gzip.open(os.path.join(HERE, "rows.jsonl.gz"), "rt")]
LAMS = [str(round(0.6 + 0.05 * i, 2)) for i in range(15)]; DELTAS = [str(round(-0.6 + 0.1 * i, 1)) for i in range(13)]
by_day = defaultdict(list)
for r in rows: by_day[r["day"]].append(r)
def forward(cell, grid, key, default):
    picks = {}
    for d in sorted(by_day):
        if d < "2026-09-01": continue
        cut = datetime.fromisoformat(d + "T00:00:00+00:00"); lim = (date.fromisoformat(d) - timedelta(days=2)).isoformat()
        prior = [r for x in by_day if x <= lim for r in by_day[x] if cell(r) and datetime.fromisoformat(r["known"]) <= cut]
        picks[d] = default if len({r["day"] for r in prior}) < 8 else min(grid, key=lambda g: (np.mean([r[key][g][0] for r in prior]), g))
    return picks
nonthin = lambda r: not r["thin"]; four = lambda r: (not r["thin"]) and r["nf"] == 4
P_stage = forward(nonthin, LAMS, "lam", "0.75"); P_shift = forward(lambda r: True, DELTAS, "delta", "0.0"); P_four = forward(four, LAMS, "lam", "0.75")
def score(r, arm):
    if arm == "PROD": return r["lam"]["1.1" if r["thin"] else "0.75"]
    if arm == "STAGE": return r["lam"]["1.1"] if r["thin"] else r["lam"][P_stage[r["day"]]]
    if arm == "SHIFT": return r["delta"][P_shift[r["day"]]]
    if arm == "FOUR": return r["lam"][P_four[r["day"]]] if four(r) else score(r, "PROD")
def tab(arm):
    acc = defaultdict(list)
    for r in rows:
        if r["day"] >= "2026-09-01": acc[(r["station"], r["day"])].append(score(r, arm))
    return {k: tuple(float(np.mean([t[i] for t in v])) for i in range(3)) for k, v in acc.items()}
T = {a: tab(a) for a in ("PROD", "STAGE", "SHIFT", "FOUR")}; k = sorted(T["PROD"]); out = {}
print("picks STAGE", dict(Counter(P_stage.values())), "SHIFT", dict(Counter(P_shift.values())), "FOUR", dict(Counter(P_four.values())))
for a in ("STAGE", "SHIFT", "FOUR"):
    res = {}
    for name, p in (("all", lambda x: True), ("h1", lambda x: x[1] < "2026-09-16"), ("h2", lambda x: x[1] >= "2026-09-16")):
        kk = [x for x in k if p(x)]; store.GATE_CONFIDENCE = 1 - 0.05 / 3
        g = store._proper_score_gate([T["PROD"][x] for x in kk], [T[a][x] for x in kk], kk); store.GATE_CONFIDENCE = 0.95
        res[name] = {f: g[f] for f in ("d_logscore", "ci_logscore", "d_brier", "ci_brier", "d_crps", "ci_crps", "lower")}
    out[a] = res; x = res["all"]
    print(f"{a:6s} dLog={x['d_logscore']:+.4f} [{x['ci_logscore'][0]:+.4f},{x['ci_logscore'][1]:+.4f}] dBrier={x['d_brier']:+.4f} [{x['ci_brier'][0]:+.4f},{x['ci_brier'][1]:+.4f}] dCRPS={x['d_crps']:+.4f} [{x['ci_crps'][0]:+.4f},{x['ci_crps'][1]:+.4f}] lower={x['lower']} halves={res['h1']['d_logscore']:+.4f}/{res['h2']['d_logscore']:+.4f}")
# in-sample curves (diagnostic): non-thin lambda and shift
sc = [r for r in rows if r["day"] >= "2026-09-01"]
print("in-sample non-thin lambda dLog vs 0.75:", {l: round(np.mean([r["lam"][l][0] - r["lam"]["0.75"][0] for r in sc if not r["thin"]]), 4) for l in LAMS[::2]})
print("in-sample shift dLog vs 0:", {x: round(np.mean([r["delta"][x][0] - r["delta"]["0.0"][0] for r in sc]), 4) for x in DELTAS})
print("station-days", len(k), "| scored decisions: thin", sum(r["thin"] for r in sc), "four", sum(four(r) for r in sc), "non-thin", sum(not r["thin"] for r in sc))
json.dump(out, open(os.path.join(HERE, "results.json"), "w"), indent=1)
