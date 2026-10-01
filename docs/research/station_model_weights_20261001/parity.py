#!/usr/bin/env python3
"""Production (fusion_blend) against the study's EBP* arm, decision by decision:
same facts, same prefix rule, same tau grid and window.  Not a gate; a parity check."""
import io, contextlib, json, math, os, sys
from datetime import datetime, timezone, date
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, ROOT)
os.environ.update(SMW_PASS="pass1_all", SMW_FROM="2026-02-15", SMW_SPLIT="2026-07-01", SMW_TAUS="5,10,20,40,80,160", SMW_SKIP_HF="", SMW_TAG="_parity")
src = open(os.path.join(HERE, "score.py")).read(); src = src[:src.index("# ---------------------------------------------------------------- oracle upper bounds")]
src = src.replace('combos = {"H": [(e, h) for e in ETAS for h in HALVES], "HL": [(e, h) for e in ETAS for h in HALVES],', 'combos = {')
src = src.replace('"HF": [(e, h) for e in ETAS for h in HALVES], "EB": [(t,) for t in TAUS], "EBP": [(t,) for t in TAUS]}', '"EBP": [(t,) for t in TAUS]}')
g = {"__name__": "parity", "__file__": os.path.join(HERE, "score.py")}
with contextlib.redirect_stdout(io.StringIO()): exec(compile(src, "score_head", "exec"), g)
import sqlite3
from bayes_basket_target import fusion_blend as FB
db = sys.argv[1]
conn = sqlite3.connect(db); conn.row_factory = sqlite3.Row
facts = [FB.WeightFact(r) for r in conn.execute("SELECT * FROM station_model_weight_fact")]
systems = sorted({s for f in facts for s in f.pool})
by_station = {}
for f in facts: by_station.setdefault(f.station, []).append(f)
decs = g["decs"]; chosen = g["chosen"]["EBP"]
sample = [d for d in decs if d["day"] in chosen][::97]
worst = 0.0; worst_case = None
for d in sample:
    tau = chosen[d["day"]][0]
    prefix = FB.knowable_facts(by_station[d["station"]], d["at"])
    w_prod, _ = FB.mixture_weights(d["w0"], FB.fit_theta(prefix, systems), FB.days_by_system(prefix), tau)
    w_res = g["weights_for"]("EBP", (tau,), d)
    diff = max(abs(w_prod[s] - w_res[s]) for s in d["pool"])
    if diff > worst: worst, worst_case = diff, (d["station"], d["day"], d["bin"])
print("weights: decisions", len(sample), "max |prod - research|", worst, worst_case)
days = sorted(chosen)[::9]; mism = 0
for day in days:
    tau, ev = FB.choose_tau(facts, systems, datetime.fromisoformat(day + "T12:00:00+00:00"))
    if tau != chosen[day][0]: mism += 1; print("tau mismatch", day, tau, chosen[day][0], ev["mean_log_score_by_tau"])
print("tau: days", len(days), "mismatches", mism)
