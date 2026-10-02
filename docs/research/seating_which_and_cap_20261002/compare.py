import gzip, json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE))); sys.path.insert(0, ROOT)
from bayes_basket_target import center_width_store as store
def load(p): return {(r["station"], r["day"]): r for r in map(json.loads, gzip.open(p, "rt"))}
full = load(os.path.join(ROOT, "docs/research/seat_weight_kappa_20261002/sd_k0.25.jsonl.gz"))
cap = load(os.path.join(HERE, "sd_cap.jsonl.gz")); k = sorted(full)
diff = max(abs(full[x]["served"][0] - cap[x]["CAP1.0"][0]) for x in k)
print(f"sanity: uncapped served == full-pool served, max |dLog| = {diff:.2e}")
out = {"sanity_max_diff": diff, "drop": {}, "cap": {}}
def gate(base, cand, kk):
    g = store._proper_score_gate(base, cand, kk); return {f: g[f] for f in ("d_logscore", "ci_logscore", "d_brier", "ci_brier", "d_crps", "ci_crps", "lower")}
def fmt(r): return f"dLog={r['d_logscore']:+.4f} [{r['ci_logscore'][0]:+.4f},{r['ci_logscore'][1]:+.4f}] dBrier={r['d_brier']:+.4f} dCRPS={r['d_crps']:+.4f} lower={r['lower']}"
print("A. drop one system fleet-wide (served, vs six-system pool):")
for s in ("DWD_ICON_GLOBAL_EPS", "ECCC_GEM_GLOBAL_ENS", "ECMWF_AIFS_ENS_GLOBAL", "ECMWF_IFS_ENS_OPEN_PF50", "NOAA_AIGEFS_025", "NOAA_GEFS_05"):
    d = load(os.path.join(HERE, f"sd_drop_{s}.jsonl.gz")); res = {}
    for name, pred in (("all", lambda x: True), ("04-06", lambda x: x[1] < "2026-07-01"), ("07-09", lambda x: x[1] >= "2026-07-01")):
        kk = [x for x in k if pred(x)]; res[name] = gate([tuple(full[x]["served"]) for x in kk], [tuple(d[x]["EBP*"]) for x in kk], kk)
    out["drop"][s] = res
    print(f"  drop {s[:22]:22s} {fmt(res['all'])}  07-09: {res['07-09']['d_logscore']:+.4f} [{res['07-09']['ci_logscore'][0]:+.4f},{res['07-09']['ci_logscore'][1]:+.4f}]")
print("B. family cap (served, vs 0.75):")
for c in ("0.5", "0.6", "0.9", "1.0"):
    res = {}
    for name, pred in (("all", lambda x: True), ("04-06", lambda x: x[1] < "2026-07-01"), ("07-09", lambda x: x[1] >= "2026-07-01")):
        kk = [x for x in k if pred(x)]; res[name] = gate([tuple(cap[x]["CAP0.75"]) for x in kk], [tuple(cap[x]["CAP" + c]) for x in kk], kk)
    out["cap"][c] = res
    print(f"  cap {c:4s} {fmt(res['all'])}  halves {res['04-06']['d_logscore']:+.4f}/{res['07-09']['d_logscore']:+.4f}")
json.dump(out, open(os.path.join(HERE, "results.json"), "w"), indent=1)
