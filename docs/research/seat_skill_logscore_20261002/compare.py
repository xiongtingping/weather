import gzip, json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE))); sys.path.insert(0, ROOT)
from bayes_basket_target import center_width_store as store
def load(p): return {(r["station"], r["day"]): r for r in map(json.loads, gzip.open(p, "rt"))}
mv = load(os.path.join(ROOT, "docs/research/seat_weight_kappa_20261002/sd_k0.25.jsonl.gz")); ls = load(os.path.join(HERE, "sd_ls.jsonl.gz"))
k = sorted(set(mv) & set(ls)); out = {}
for view in ("seat", "served"):
    out[view] = {}
    for name, pred in (("all", lambda x: True), ("04-06", lambda x: x[1] < "2026-07-01"), ("07-09", lambda x: x[1] >= "2026-07-01")):
        kk = [x for x in k if pred(x)]
        g = store._proper_score_gate([tuple(mv[x][view]) for x in kk], [tuple(ls[x][view]) for x in kk], kk)
        out[view][name] = {f: g[f] for f in ("d_logscore", "ci_logscore", "d_brier", "ci_brier", "d_crps", "ci_crps", "lower")}
        print(f"{view:6s} {name:5s} LS-MV dLog={g['d_logscore']:+.4f} [{g['ci_logscore'][0]:+.4f},{g['ci_logscore'][1]:+.4f}] dBrier={g['d_brier']:+.4f} [{g['ci_brier'][0]:+.4f},{g['ci_brier'][1]:+.4f}] dCRPS={g['d_crps']:+.4f} [{g['ci_crps'][0]:+.4f},{g['ci_crps'][1]:+.4f}] lower={g['lower']}")
json.dump(out, open(os.path.join(HERE, "results.json"), "w"), indent=1)
