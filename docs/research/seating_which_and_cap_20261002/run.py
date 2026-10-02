#!/usr/bin/env python3
"""MODE=drop SYS=<system> | MODE=cap : served (charter 242) station-day scores on the six-system replay."""
import os, sys, io, contextlib, json, gzip
from collections import defaultdict
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
SMW = os.path.join(ROOT, "docs/research/station_model_weights_20261001")
sys.path.insert(0, ROOT)
MODE = os.environ["MODE"]; DROP = os.environ.get("SYS", "")
os.environ.update(SMW_PASS="pass1_all", SMW_FROM="2026-04-01", SMW_SPLIT="2026-07-01", SMW_TAUS="5,10,20,40,80,160", SMW_SKIP_HF="1", SMW_TAG="_wc")
src = open(os.path.join(SMW, "score.py")).read()
cut = src.index("def proxy(d, w):"); head, rest = src[:cut], src[cut:]
patch = '''
def _baseline(d):
    fams = defaultdict(list)
    for s in d["pool"]: fams[d["family"][s]].append(s)
    return {s: 1.0 / len(fams) / len(m) for m in fams.values() for s in m}
KEPT = [0, 0]
for d in decs:
    b = _baseline(d)
    skill = {s: max(2.0 * d["w0"][s] - b[s], 0.0) for s in d["pool"]}; tot = sum(skill.values())
    skill = {s: v / tot for s, v in skill.items()} if tot > 0 else dict(b)
    if DROP and DROP in d["pool"]:
        rest_ = [s for s in d["pool"] if s != DROP]
        if len({d["family"][s] for s in rest_}) >= 2:
            d["pool"] = rest_
            for key in ("p", "c", "slead", "py"): d[key] = {s: d[key][s] for s in rest_}
            skill = {s: skill[s] for s in rest_}; t = sum(skill.values())
            skill = {s: v / t for s, v in skill.items()} if t > 0 else None
            b = _baseline(d); skill = skill or dict(b)
            KEPT[1] += 1
        else:
            KEPT[0] += 1
    w = {s: 0.75 * skill[s] + 0.25 * b[s] for s in d["pool"]}; t = sum(w.values()); d["w0"] = {s: v / t for s, v in w.items()}
'''
rest = rest.replace('combos = {"H": [(e, h) for e in ETAS for h in HALVES], "HL": [(e, h) for e in ETAS for h in HALVES],', 'combos = {')
rest = rest.replace('"HF": [(e, h) for e in ETAS for h in HALVES], "EB": [(t,) for t in TAUS], "EBP": [(t,) for t in TAUS]}', '"EBP": [(t,) for t in TAUS]}')
rest = rest.replace('if SKIP_HF: del combos["HF"]', '')
if MODE == "cap":
    rest = rest.replace('ARMS = ["A0", "H*", "HL"] + ([] if SKIP_HF else ["HF"]) + ["EB", "EBP*", "SEAT*", "SEAT+H", "ORACLE-S", "ORACLE-SL"]',
                        'ARMS = ["CAP0.5", "CAP0.6", "CAP0.75", "CAP0.9", "CAP1.0"]')
    rest = rest.replace('def arm_w(d, arm):\n', '''def arm_w(d, arm):
    if arm.startswith("CAP"):
        from bayes_basket_target.fusion_blend import feasible as _feasible
        cap = float(arm[3:]); w = weights_for("EBP", chosen["EBP"][d["day"]], d)
        fams = {s: d["family"][s] for s in w}; nf = len(set(fams.values()))
        if cap * nf < 1.0 - 1e-12: return w
        out = _feasible(w, fams, {f: cap for f in set(fams.values())})
        CAPSTAT[arm][0] += 1; CAPSTAT[arm][1] += int(max(abs(out[s] - w[s]) for s in w) > 1e-9)
        return out
''', 1)
else:
    rest = rest.replace('ARMS = ["A0", "H*", "HL"] + ([] if SKIP_HF else ["HF"]) + ["EB", "EBP*", "SEAT*", "SEAT+H", "ORACLE-S", "ORACLE-SL"]', 'ARMS = ["EBP*"]')
rest = rest[:rest.index("# ---------------------------------------------------------------- verdicts")]
rest = rest.replace(rest[rest.index("# ---------------------------------------------------------------- oracle upper bounds"):rest.index("# ---------------------------------------------------------------- arm weights at a scored decision")],
                    "scored = [d for d in decs if SCORE_FROM <= d['day'] <= SCORE_TO]\n")
g = {"__name__": "wc", "__file__": os.path.join(SMW, "score.py"), "DROP": DROP, "CAPSTAT": defaultdict(lambda: [0, 0])}
with contextlib.redirect_stdout(io.StringIO()):
    exec(compile(head + patch + rest, "score_wc", "exec"), g)
tab, sd, arms = g["tab"], g["sd"], g["ARMS"]
tag = "cap" if MODE == "cap" else ("drop_" + (DROP or "none"))
with gzip.open(os.path.join(HERE, f"sd_{tag}.jsonl.gz"), "wt") as z:
    for k in sd: z.write(json.dumps({"station": k[0], "day": k[1], **{a: tab[a][k] for a in arms}}) + "\n")
print(tag, "station-days", len(sd), "kept-original (too few families)/dropped:", g["KEPT"], {a: v for a, v in g["CAPSTAT"].items()})
