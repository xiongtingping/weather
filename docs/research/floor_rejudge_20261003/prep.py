#!/usr/bin/env python3
"""PROTOCOL.md: decisions and weights from the long replay, as the original
studies built them.

    python3 prep.py A   # seat as captured (kappa 0.5): charter 242's original run
    python3 prep.py B   # seat rewritten to kappa 0.25 (the 244 / 246 patch)

Writes cache_<A|B>.pkl: per representative decision its inputs, the seat
weights (A0) and the charter 242 served weights (EBP*, tau chosen walk-forward
by score.py's own rule).
"""
import contextlib
import io
import os
import pickle
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
SMW = os.path.join(ROOT, "docs/research/station_model_weights_20261001")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
VARIANT = sys.argv[1]
assert VARIANT in ("A", "B")
os.environ.update(SMW_PASS="pass1_all", SMW_FROM="2026-02-15", SMW_SPLIT="2026-07-01",
                  SMW_TAUS="5,10,20,40,80,160", SMW_SKIP_HF="1", SMW_TAG="_rejudge")
src = open(os.path.join(SMW, "score.py")).read()
cut = src.index("def proxy(d, w):")
head, rest = src[:cut], src[cut:]
patch = '''
def _baseline(d):
    fams = defaultdict(list)
    for s in d["pool"]: fams[d["family"][s]].append(s)
    return {s: 1.0 / len(fams) / len(m) for m in fams.values() for s in m}
for d in decs:
    b = _baseline(d)
    skill = {s: max(2.0 * d["w0"][s] - b[s], 0.0) for s in d["pool"]}; tot = sum(skill.values())
    skill = {s: v / tot for s, v in skill.items()} if tot > 0 else dict(b)
    w = {s: 0.75 * skill[s] + 0.25 * b[s] for s in d["pool"]}; t = sum(w.values()); d["w0"] = {s: v / t for s, v in w.items()}
''' if VARIANT == "B" else ""
old_a = 'combos = {"H": [(e, h) for e in ETAS for h in HALVES], "HL": [(e, h) for e in ETAS for h in HALVES],'
old_b = '"HF": [(e, h) for e in ETAS for h in HALVES], "EB": [(t,) for t in TAUS], "EBP": [(t,) for t in TAUS]}'
assert rest.count(old_a) == 1 and rest.count(old_b) == 1
rest = rest.replace(old_a, "combos = {").replace(old_b, '"EBP": [(t,) for t in TAUS]}')
rest = rest.replace('if SKIP_HF: del combos["HF"]', "")
rest = rest[:rest.index("# ---------------------------------------------------------------- oracle upper bounds")]
g = {"__name__": "rejudge", "__file__": os.path.join(SMW, "score.py")}
t0 = time.monotonic()
with contextlib.redirect_stdout(io.StringIO()):
    exec(compile(head + patch + rest, "score_rejudge", "exec"), g)
decs, chosen, weights_for = g["decs"], g["chosen"], g["weights_for"]
out = []
for d in decs:
    if d["day"] < "2026-02-15" or d["day"] > "2026-09-30" or d["day"] not in chosen["EBP"]:
        continue
    out.append({
        "station": d["station"], "day": d["day"], "known": d["known"].isoformat(), "bin": d["bin"],
        "actual": d["actual"], "lat": d["lat"], "cooling": d["cooling"], "family": dict(d["family"]),
        "p": {s: d["p"][s] for s in d["pool"]}, "c": {s: d["c"][s] for s in d["pool"]},
        "A0": dict(d["w0"]), "EBP": dict(weights_for("EBP", chosen["EBP"][d["day"]], d)),
    })
with open(os.path.join(HERE, f"cache_{VARIANT}.pkl"), "wb") as handle:
    pickle.dump(out, handle)
print(VARIANT, "decisions", len(out), "station-days", len({(r["station"], r["day"]) for r in out}),
      "%.0fs" % (time.monotonic() - t0))
