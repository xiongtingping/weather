import gzip, json, os, sys
from collections import defaultdict
from datetime import date, datetime, timedelta
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE))); sys.path.insert(0, ROOT)
from bayes_basket_target import center_width_store as store
LAMS = [str(round(0.6 + 0.05 * i, 2)) for i in range(15)]
def load(op): return [json.loads(l) for l in gzip.open(os.path.join(HERE, f"rows_{op}.jsonl.gz"), "rt")]
def forward(rows):
    """Per scored day and slice, the lambda chosen on 02-15..d-2 known decisions (default 0.75/1.1)."""
    days = sorted({r["day"] for r in rows if r["day"] >= "2026-04-01"}); pick = {}
    by_day = defaultdict(list)
    for r in rows: by_day[r["day"]].append(r)
    for d in days:
        cut = datetime.fromisoformat(d + "T00:00:00+00:00"); lim = (date.fromisoformat(d) - timedelta(days=2)).isoformat()
        for thin in (True, False):
            prior = [r for x in by_day if x <= lim for r in by_day[x] if r["thin"] == thin and datetime.fromisoformat(r["known"]) <= cut]
            if len({r["day"] for r in prior}) < 8: pick[(d, thin)] = "1.1" if thin else "0.75"; continue
            pick[(d, thin)] = min(LAMS, key=lambda l: (np.mean([r["s"][l][0] for r in prior]), l))
    return pick
def tab(rows, choose):
    acc = defaultdict(list)
    for r in rows:
        if r["day"] >= "2026-04-01": acc[(r["station"], r["day"])].append(r["s"][choose(r)])
    return {k: tuple(float(np.mean([t[i] for t in v])) for i in range(3)) for k, v in acc.items()}
arms = {}; picks = {}
for op in ("LIN", "GEO", "VIN"):
    rows = load(op); p = forward(rows); picks[op] = p
    arms[op] = tab(rows, lambda r, p=p: p[(r["day"], r["thin"])])
    if op == "LIN": arms["A0"] = tab(rows, lambda r: "1.1" if r["thin"] else "0.75")
from collections import Counter
for op in picks: print(op, "lambda picks thin:", dict(Counter(v for (d, t), v in picks[op].items() if t)), "rest:", dict(Counter(v for (d, t), v in picks[op].items() if not t)))
k = sorted(set.intersection(*[set(a) for a in arms.values()])); out = {}
def gate(a, b, conf=0.975, part=lambda x: True):
    kk = [x for x in k if part(x)]; store.GATE_CONFIDENCE = conf
    g = store._proper_score_gate([arms[b][x] for x in kk], [arms[a][x] for x in kk], kk); store.GATE_CONFIDENCE = 0.95
    return {f: g[f] for f in ("d_logscore", "ci_logscore", "d_brier", "ci_brier", "d_crps", "ci_crps", "lower")}
for a, b in (("GEO", "LIN"), ("VIN", "LIN"), ("GEO", "A0"), ("VIN", "A0"), ("LIN", "A0")):
    res = {"all": gate(a, b), "04-06": gate(a, b, part=lambda x: x[1] < "2026-07-01"), "07-09": gate(a, b, part=lambda x: x[1] >= "2026-07-01")}
    out[f"{a}_vs_{b}"] = res; x = res["all"]
    print(f"{a} vs {b}: dLog={x['d_logscore']:+.4f} [{x['ci_logscore'][0]:+.4f},{x['ci_logscore'][1]:+.4f}] dBrier={x['d_brier']:+.4f} [{x['ci_brier'][0]:+.4f},{x['ci_brier'][1]:+.4f}] dCRPS={x['d_crps']:+.4f} [{x['ci_crps'][0]:+.4f},{x['ci_crps'][1]:+.4f}] lower={x['lower']} halves={res['04-06']['d_logscore']:+.4f}/{res['07-09']['d_logscore']:+.4f}")
json.dump(out, open(os.path.join(HERE, "results.json"), "w"), indent=1)
