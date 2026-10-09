#!/usr/bin/env python3
"""Does the realisation layer's b carry resolution beyond the market?  On the
locked acceptance population (first >36h P0 per station-day decided after the
freeze, frozen fit -- docs/research/realisation_acceptance_20261006), every
bucket one-vs-rest: Murphy REL/RES of p (model), q (market), b, and b0 = b with
the model's four terms zeroed (the frozen fit's market-only part).  Plus the
multiclass log score.  Descriptive: the acceptance itself reads ~10-16.  Read-only."""
import importlib.util, json, math, sqlite3, sys
from pathlib import Path
import numpy as np
HERE = Path(__file__).resolve().parent; ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("acc", ROOT / "docs/research/realisation_acceptance_20261006/read.py")
acc = importlib.util.module_from_spec(spec); spec.loader.exec_module(acc)
from bayes_basket_target import realisation_layer as layer
from bayes_basket_target.realisation_store import _ladder_inputs

fit = json.loads((ROOT / "docs/research/realisation_acceptance_20261006/frozen_fit.json").read_text())
theta = np.asarray(fit["theta"], float)
names = fit["param_names"]
theta0 = theta.copy()
for n in ("width_agree", "shape_agree", "width_disagree", "shape_disagree"):
    theta0[names.index(n)] = 0.0

# capture each row's terms by wrapping the belief function load() calls
captured = []
_orig = layer.belief
def _spy(terms, th):
    captured.append(np.asarray(terms))
    return _orig(terms, th)
acc.layer.belief = _spy
conn = sqlite3.connect(f"file:{acc.DB}?mode=ro", uri=True, timeout=60)
rows, counts = acc.load(conn, list(theta))
conn.close()
acc.layer.belief = _orig
# load() calls belief once per kept candidate before the label check; align by recomputing b
assert len(captured) >= len(rows)
terms_by_b = {}
for t in captured:
    terms_by_b.setdefault(tuple(np.round(_orig(t, theta), 12)), t)
for r in rows:
    r["b0"] = _orig(terms_by_b[tuple(np.round(r["b"], 12))], theta0)

EDGES = np.array([0.0, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0 + 1e-9])
def murphy(pairs):
    a = np.asarray(pairs); p, y = a[:, 0], a[:, 1]
    k = np.digitize(p, EDGES) - 1; N = len(p); base = y.mean()
    rel = res = 0.0; table = []
    for j in range(len(EDGES) - 1):
        m = k == j
        if m.sum():
            rel += m.sum() * (p[m].mean() - y[m].mean()) ** 2; res += m.sum() * (y[m].mean() - base) ** 2
            table.append((f"{EDGES[j]:.2f}–{min(EDGES[j+1],1):.2f}", int(m.sum()), float(p[m].mean()), float(y[m].mean())))
    brier = float(((p - y) ** 2).mean())
    return {"REL": rel / N, "RES": res / N, "UNC": base * (1 - base), "brier_per_bucket": brier, "table": table}

def panel(sub):
    out = {"station_days": len(sub), "event_days": sorted({r["event_date"] for r in sub})}
    for v in ("p", "q", "b", "b0"):
        pairs = [(float(r[v][k]), 1.0 if k == r["y"] else 0.0) for r in sub for k in range(len(r[v]))]
        m = murphy(pairs)
        m["nll"] = float(np.mean([-math.log(max(float(r[v][r["y"]]), 1e-300)) for r in sub]))
        m["top_bucket"] = float(np.mean([float(np.max(r[v])) for r in sub]))
        out[v] = m
    # station-day bootstrap of the paired NLL / Brier differences
    rng = np.random.default_rng(20261010)
    def nll(r, v): return -math.log(max(float(r[v][r["y"]]), 1e-300))
    def bri(r, v): return float(sum((float(r[v][k]) - (1.0 if k == r["y"] else 0.0)) ** 2 for k in range(len(r[v]))))
    for a, c in (("b", "q"), ("b", "b0"), ("b0", "q"), ("p", "q"), ("b", "p")):
        dn = np.array([nll(r, a) - nll(r, c) for r in sub]); db = np.array([bri(r, a) - bri(r, c) for r in sub])
        pick = rng.integers(0, len(sub), size=(4000, len(sub)))
        out[f"d_{a}_{c}"] = {"nll": [float(dn.mean()), float(np.quantile(dn[pick].mean(1), 0.025)), float(np.quantile(dn[pick].mean(1), 0.975))],
                              "brier": [float(db.mean()), float(np.quantile(db[pick].mean(1), 0.025)), float(np.quantile(db[pick].mean(1), 0.975))]}
    return out

result = {"counts": counts, "all": panel(rows),
          "modes_agree": panel([r for r in rows if r["mode_model"] == r["mode_market"]]),
          "modes_disagree": panel([r for r in rows if r["mode_model"] != r["mode_market"]])}
(HERE / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))
for g in ("all", "modes_agree", "modes_disagree"):
    r = result[g]
    print(f"== {g}: {r['station_days']} 站日 / {len(r['event_days'])} 事件日 {r['event_days'][0] if r['event_days'] else ''}..{r['event_days'][-1] if r['event_days'] else ''}")
    for v, name in (("p", "模型p"), ("q", "市场q"), ("b0", "b去模型项"), ("b", "兑现层b")):
        m = r[v]
        print(f"   {name:8s} NLL {m['nll']:.4f}  Brier/桶 {m['brier_per_bucket']:.5f}  REL {m['REL']*1e3:.3f}e-3  RES {m['RES']*1e3:.3f}e-3  最高桶 {m['top_bucket']:.3f}")
    for k in ("d_b_q", "d_b_b0", "d_b0_q", "d_p_q", "d_b_p"):
        x = r[k]
        print(f"   {k}: ΔNLL {x['nll'][0]:+.4f} [{x['nll'][1]:+.4f},{x['nll'][2]:+.4f}]  ΔBrier {x['brier'][0]:+.4f} [{x['brier'][1]:+.4f},{x['brier'][2]:+.4f}]")
print("\n可靠性（全部桶）  说→中：")
for (bn, n, pp, yy), q_, b_ in zip(result["all"]["b"]["table"], [None]*99, [None]*99):
    pass
for v, name in (("q", "市场q"), ("b", "兑现层b"), ("p", "模型p")):
    print(name, "  ".join(f"{bn}:{pp:.3f}→{yy:.3f}({n})" for bn, n, pp, yy in result["all"][v]["table"]))
