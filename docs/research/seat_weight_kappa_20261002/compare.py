import gzip, json, os, sys
import numpy as np
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE))); sys.path.insert(0, ROOT)
from bayes_basket_target import center_width_store as store
load = lambda k: {(r["station"], r["day"]): r for r in map(json.loads, gzip.open(os.path.join(HERE, f"sd_k{k}.jsonl.gz"), "rt"))}
base = load(0.5); out = {}
# sanity: served(0.5) vs seat(0.5) must reproduce the charter-242 verdict
k = sorted(base); g = store._proper_score_gate([tuple(base[x]["seat"]) for x in k], [tuple(base[x]["served"]) for x in k], k)
print(f"sanity served-vs-seat at k=0.5: dLog={g['d_logscore']:+.4f} {['%+.4f'%v for v in g['ci_logscore']]}  (verdict_all primary -0.0132)")
for kap in (0.0, 0.25, 0.75, 1.0):
    arm = load(kap); out[kap] = {}
    for view in ("seat", "served"):
        res = {}
        for name, pred in (("all", lambda x: True), ("04-06", lambda x: x[1] < "2026-07-01"), ("07-09", lambda x: x[1] >= "2026-07-01")):
            kk = [x for x in k if pred(x)]
            gg = store._proper_score_gate([tuple(base[x][view]) for x in kk], [tuple(arm[x][view]) for x in kk], kk)
            res[name] = {f: gg[f] for f in ("d_logscore", "ci_logscore", "d_brier", "ci_brier", "d_crps", "lower")}
        out[kap][view] = res
        a = res["all"]
        print(f"kappa {kap:4.2f} {view:6s} vs k0.5: dLog={a['d_logscore']:+.4f} [{a['ci_logscore'][0]:+.4f},{a['ci_logscore'][1]:+.4f}] dBrier={a['d_brier']:+.4f} dCRPS={a['d_crps']:+.4f} lower={a['lower']} halves={res['04-06']['d_logscore']:+.4f}/{res['07-09']['d_logscore']:+.4f}")
json.dump({str(k): v for k, v in out.items()}, open(os.path.join(HERE, "results.json"), "w"), indent=1)
