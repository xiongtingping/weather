import json, os, sys
from collections import defaultdict
from datetime import date, datetime, timedelta
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE))); sys.path.insert(0, ROOT)
from bayes_basket_target import center_width_store as store
rows = json.load(open(os.path.join(HERE, "rows.json")))
GRID = ("0.75", "0.8", "0.825", "0.85", "0.9", "0.95", "1.0", "1.1", "1.2")
SLICES = {"239: <=3 fam & >=36h": lambda r: r["nf"] <= 3 and r["bin"] >= 36, "<=3 fam all horizons": lambda r: r["nf"] <= 3, "4 fam": lambda r: r["nf"] == 4}
out = {}
for name, pred in SLICES.items():
    rs = [r for r in rows if pred(r)]
    days = sorted({r["day"] for r in rs})
    chosen = {}
    for d in days:
        if d < "2026-04-01": continue
        cut = datetime.fromisoformat(d + "T00:00:00+00:00")
        prior = [r for r in rs if r["day"] <= (date.fromisoformat(d) - timedelta(days=2)).isoformat() and datetime.fromisoformat(r["known"]) <= cut]
        if len({r["day"] for r in prior}) < 8: chosen[d] = "0.75"; continue
        chosen[d] = min(GRID, key=lambda l: (np.mean([r["s"][l][0] for r in prior]), l))
    scored = [r for r in rs if r["day"] >= "2026-04-01"]
    def tab(pick):
        acc = defaultdict(list)
        for r in scored: acc[(r["station"], r["day"])].append(r["s"][pick(r)])
        return {k: tuple(float(np.mean([t[i] for t in v])) for i in range(3)) for k, v in acc.items()}
    base = tab(lambda r: "0.75"); cand = tab(lambda r: chosen[r["day"]]); k = sorted(base)
    res = {"station_days": len(k), "chosen": dict(sorted(__import__("collections").Counter(chosen.values()).items()))}
    for part, p in (("all", lambda x: True), ("04-06", lambda x: x[1] < "2026-07-01"), ("07-09", lambda x: x[1] >= "2026-07-01")):
        kk = [x for x in k if p(x)]
        if not kk: continue
        g = store._proper_score_gate([base[x] for x in kk], [cand[x] for x in kk], kk)
        res[part] = {f: g[f] for f in ("d_logscore", "ci_logscore", "d_brier", "ci_brier", "d_crps", "ci_crps", "lower", "station_days")}
    # fixed-lambda diagnostic (in-sample over scored period)
    res["fixed"] = {l: float(np.mean([np.mean([t[0] for t in v]) for v in (lambda acc: acc)( {kk: [r["s"][l] for r in scored if (r["station"], r["day"]) == kk] for kk in []} ).values()])) if False else None for l in GRID}
    sd_fixed = {}
    for l in GRID:
        acc = defaultdict(list)
        for r in scored: acc[(r["station"], r["day"])].append(r["s"][l][0])
        sd_fixed[l] = float(np.mean([np.mean(v) for v in acc.values()]))
    res["fixed"] = {l: round(v - sd_fixed["0.75"], 5) for l, v in sd_fixed.items()}
    out[name] = res
    a = res.get("all", {})
    print(f"== {name}: station-days {len(k)}  chosen {res['chosen']}")
    for part in ("all", "04-06", "07-09"):
        if part in res:
            x = res[part]; print(f"   {part:5s} dLog={x['d_logscore']:+.4f} [{x['ci_logscore'][0]:+.4f},{x['ci_logscore'][1]:+.4f}] dBrier={x['d_brier']:+.4f} [{x['ci_brier'][0]:+.4f},{x['ci_brier'][1]:+.4f}] dCRPS={x['d_crps']:+.4f} lower={x['lower']} n={x['station_days']}")
    print("   fixed-lambda dLog vs 0.75 (in-sample, scored period):", res["fixed"])
json.dump(out, open(os.path.join(HERE, "results.json"), "w"), indent=1)
