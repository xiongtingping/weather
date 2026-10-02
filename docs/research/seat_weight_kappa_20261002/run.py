#!/usr/bin/env python3
"""One kappa per run (SEAT_KAPPA env): seat-only and served (charter 242 EBP*) station-day scores."""
import os, sys, io, contextlib, json, gzip
from collections import defaultdict
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
SMW = os.path.join(ROOT, "docs/research/station_model_weights_20261001")
sys.path.insert(0, ROOT)
KAPPA = float(os.environ["SEAT_KAPPA"])
os.environ.update(SMW_PASS="pass1_all", SMW_FROM="2026-04-01", SMW_SPLIT="2026-07-01", SMW_TAUS="5,10,20,40,80,160", SMW_SKIP_HF="1", SMW_TAG=f"_k{KAPPA}")
src = open(os.path.join(SMW, "score.py")).read()
# 1) load decisions, then rewrite every w0 to the kappa arm BEFORE records/fits/tuning are built
cut = src.index("def proxy(d, w):")
head, rest = src[:cut], src[cut:]
patch = '''
def _baseline(d):
    fams = defaultdict(list)
    for s in d["pool"]: fams[d["family"][s]].append(s)
    return {s: 1.0 / len(fams) / len(m) for m in fams.values() for s in m}
for d in decs:
    b = _baseline(d)
    skill = {s: max(2.0 * d["w0"][s] - b[s], 0.0) for s in d["pool"]}
    tot = sum(skill.values())
    skill = {s: v / tot for s, v in skill.items()} if tot > 0 else dict(b)
    w = {s: (1.0 - KAPPA) * skill[s] + KAPPA * b[s] for s in d["pool"]}
    t = sum(w.values()); d["w0"] = {s: v / t for s, v in w.items()}
'''
rest = rest.replace('combos = {"H": [(e, h) for e in ETAS for h in HALVES], "HL": [(e, h) for e in ETAS for h in HALVES],', 'combos = {')
rest = rest.replace('"HF": [(e, h) for e in ETAS for h in HALVES], "EB": [(t,) for t in TAUS], "EBP": [(t,) for t in TAUS]}', '"EBP": [(t,) for t in TAUS]}')
rest = rest.replace('if SKIP_HF: del combos["HF"]', '')
rest = rest.replace('ARMS = ["A0", "H*", "HL"] + ([] if SKIP_HF else ["HF"]) + ["EB", "EBP*", "SEAT*", "SEAT+H", "ORACLE-S", "ORACLE-SL"]', 'ARMS = ["A0", "EBP*"]')
rest = rest[:rest.index("# ---------------------------------------------------------------- verdicts")]
# oracles are not needed
rest = rest.replace(rest[rest.index("# ---------------------------------------------------------------- oracle upper bounds"):rest.index("# ---------------------------------------------------------------- arm weights at a scored decision")],
                    "scored = [d for d in decs if SCORE_FROM <= d['day'] <= SCORE_TO]\n")
g = {"__name__": "kappa", "__file__": os.path.join(SMW, "score.py"), "KAPPA": KAPPA}
with contextlib.redirect_stdout(io.StringIO()):
    exec(compile(head + patch + rest, "score_kappa", "exec"), g)
tab, sd = g["tab"], g["sd"]
with gzip.open(os.path.join(HERE, f"sd_k{KAPPA}.jsonl.gz"), "wt") as z:
    for k in sd: z.write(json.dumps({"station": k[0], "day": k[1], "seat": tab["A0"][k], "served": tab["EBP*"][k]}) + "\n")
print("kappa", KAPPA, "station-days", len(sd))
