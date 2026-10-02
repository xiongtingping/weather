#!/usr/bin/env python3
"""LS skill arm: seat-only and served (charter 242 EBP*) station-day scores.  Baseline MV arm = seat_weight_kappa_20261002 sd_k0.25."""
import os, sys, io, contextlib, json, gzip, math
from collections import defaultdict
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
SMW = os.path.join(ROOT, "docs/research/station_model_weights_20261001")
sys.path.insert(0, ROOT)
os.environ.update(SMW_PASS="pass1_all", SMW_FROM="2026-04-01", SMW_SPLIT="2026-07-01", SMW_TAUS="5,10,20,40,80,160", SMW_SKIP_HF="1", SMW_TAG="_ls")
src = open(os.path.join(SMW, "score.py")).read()
cut = src.index("def proxy(d, w):"); head, rest = src[:cut], src[cut:]
patch = '''
import numpy as _np
from datetime import date as _date
def _baseline(d):
    fams = defaultdict(list)
    for s in d["pool"]: fams[d["family"][s]].append(s)
    return {s: 1.0 / len(fams) / len(m) for m in fams.values() for s in m}
_by_sl = defaultdict(list)
for d in decs: _by_sl[(d["station"], d["lead"])].append(d)
_fit_cache = {}; _ls_used = [0, 0]
def _ls_skill(d):
    today = _date.fromisoformat(d["day"])
    prior = [x for x in _by_sl[(d["station"], d["lead"])]
             if 0 < (today - _date.fromisoformat(x["day"])).days <= 60 and x["known"] < d["at"]]
    if len({x["day"] for x in prior}) < 5: return None
    key = (d["station"], d["lead"], tuple(sorted(id(x) for x in prior)))
    if key not in _fit_cache:
        M = _np.zeros((len(prior), len(FAM))); P = _np.zeros((len(prior), len(FAM)))
        for i, x in enumerate(prior):
            for s in x["pool"]: M[i, FI[s]] = 1.0; P[i, FI[s]] = x["py"][s]
        th = _np.zeros(len(FAM))
        for _ in range(300):
            e = _np.exp(th - th.max()) * M; q = e / e.sum(1, keepdims=True); num = (q * P).sum(1, keepdims=True)
            grad = (q * P / num - q).mean(0); th += 2.0 * grad; th -= th.mean()
            if _np.abs(grad).max() < 1e-6: break
        _fit_cache[key] = th
    th = _fit_cache[key]; raw = {s: math.exp(th[FI[s]] - th.max()) for s in d["pool"]}; t = sum(raw.values())
    return {s: v / t for s, v in raw.items()}
for d in decs:
    b = _baseline(d)
    mv = {s: max(2.0 * d["w0"][s] - b[s], 0.0) for s in d["pool"]}; tot = sum(mv.values())
    mv = {s: v / tot for s, v in mv.items()} if tot > 0 else dict(b)
    ls = _ls_skill(d)
    _ls_used[0 if ls is None else 1] += 1
    skill = mv if ls is None else ls
    w = {s: 0.75 * skill[s] + 0.25 * b[s] for s in d["pool"]}; t = sum(w.values()); d["w0"] = {s: v / t for s, v in w.items()}
'''
rest = rest.replace('combos = {"H": [(e, h) for e in ETAS for h in HALVES], "HL": [(e, h) for e in ETAS for h in HALVES],', 'combos = {')
rest = rest.replace('"HF": [(e, h) for e in ETAS for h in HALVES], "EB": [(t,) for t in TAUS], "EBP": [(t,) for t in TAUS]}', '"EBP": [(t,) for t in TAUS]}')
rest = rest.replace('if SKIP_HF: del combos["HF"]', '')
rest = rest.replace('ARMS = ["A0", "H*", "HL"] + ([] if SKIP_HF else ["HF"]) + ["EB", "EBP*", "SEAT*", "SEAT+H", "ORACLE-S", "ORACLE-SL"]', 'ARMS = ["A0", "EBP*"]')
rest = rest[:rest.index("# ---------------------------------------------------------------- verdicts")]
rest = rest.replace(rest[rest.index("# ---------------------------------------------------------------- oracle upper bounds"):rest.index("# ---------------------------------------------------------------- arm weights at a scored decision")],
                    "scored = [d for d in decs if SCORE_FROM <= d['day'] <= SCORE_TO]\n")
g = {"__name__": "ls", "__file__": os.path.join(SMW, "score.py")}
with contextlib.redirect_stdout(io.StringIO()):
    exec(compile(head + patch + rest, "score_ls", "exec"), g)
tab, sd = g["tab"], g["sd"]
with gzip.open(os.path.join(HERE, "sd_ls.jsonl.gz"), "wt") as z:
    for k in sd: z.write(json.dumps({"station": k[0], "day": k[1], "seat": tab["A0"][k], "served": tab["EBP*"][k]}) + "\n")
print("station-days", len(sd), "decisions with LS skill / fallback MV:", g["_ls_used"][1], "/", g["_ls_used"][0])
