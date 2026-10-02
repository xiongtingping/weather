#!/usr/bin/env python3
"""Addendum (exploratory, labelled so): every protocol arm's full verdict on
recent/all, its shape, and -- outside the protocol grid -- the stage-lambda
vector with tail mixtures at kappa 1.5 and 3.0.  Writes results_explore.json."""
import gzip, json, math, importlib.util, sys
from collections import Counter
from pathlib import Path
import numpy as np
HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("tw", HERE / "tail_weight.py"); T = importlib.util.module_from_spec(spec); spec.loader.exec_module(T)
rows = []
for path in sorted(T.SRC.glob("*.jsonl.gz")):
    with gzip.open(path, "rt") as h:
        for line in h:
            r = json.loads(line); b = r["base"]
            if (b.get("seated") or 9) <= 3 and (b.get("hours_to_settlement") or 0.0) > 36.0 and b.get("cw_active"):
                rows.append(r)
arms = {"now": lambda r: T.served(r["slice_1.1"]["vec"]), "noslice": lambda r: T.served(r["base"]["vec"]), "l10": lambda r: T.served(r["lam_1.0"]["vec"]),
        "l09": lambda r: T.served(r["lam_0.9"]["vec"])}
for k in (1.5, 2.0, 3.0):
    for e in (0.02, 0.05, 0.10, 0.15, 0.20, 0.30):
        arms[f"noslice_tail_k{k}_e{e}"] = (lambda r, k=k, e=e: T.served(T.tail_mix(r["base"]["vec"], k, e)))
        arms[f"l10_tail_k{k}_e{e}"] = (lambda r, k=k, e=e: T.served(T.tail_mix(r["lam_1.0"]["vec"], k, e)))
scored, vecs = [], {n: [] for n in arms}
for r in rows:
    a = float(r["actual"]); e = {"station": r["station"], "event_date": r["event_date"]}
    for n, fn in arms.items():
        p = fn(r); e[n] = {"served": T.score(p, a)}; vecs[n].append((p, a, r["station"], r["event_date"], r["at"]))
    scored.append(e)
early = [e for e in scored if e["event_date"] <= "2026-09-13"]; recent = [e for e in scored if e["event_date"] >= "2026-09-14"]
def shape(n):
    recs = []
    for p, a, st, day, at in vecs[n]:
        mu, sd, u, pt, idx = T.A.read_vec(p, a); recs.append(dict(station=st, date=day, at=at, truth=a, mu=mu, sd=sd, u=u, p_truth=pt, ls=-math.log(max(pt, 1e-3))))
    rep = T.A.report(recs, n); return {k: rep[k] for k in ("z_meansq", "pit_var_ratio", "cover_50", "cover_90", "pit_tail_lt05_gt95")}
out = {}
f = lambda v: "%+.4f [%+.4f,%+.4f]" % tuple(v)
print(f"{'arm':26s} {'recent dLog vs now':>28s} {'recent dCRPS':>24s} 240r  {'all dLog vs now':>28s} 240a   E[z²]  PITvar cov50 cov90  tails")
for n in arms:
    if n == "now": continue
    cr, ca = T.S.compare(recent, n, "now"), T.S.compare(scored, n, "now"); sh = shape(n)
    out[n] = {"recent": cr, "all": ca, "shape": sh}
    print(f"{n:26s} {f(cr['d_nll']):>28s} {f(cr['d_crps']):>24s} {'PASS' if cr['verdict_240'] else 'fail'}  {f(ca['d_nll']):>28s} {'PASS' if ca['verdict_240'] else 'fail'}   {sh['z_meansq'][0]:.3f}  {sh['pit_var_ratio'][0]:.3f}  {sh['cover_50'][0]:.3f} {sh['cover_90'][0]:.3f}  {sh['pit_tail_lt05_gt95'][0]:.3f}/{sh['pit_tail_lt05_gt95'][1]:.3f}")
sh = shape("now"); print(f"{'now (slice 1.1)':26s} {'':>28s} {'':>24s}       {'':>28s}        {sh['z_meansq'][0]:.3f}  {sh['pit_var_ratio'][0]:.3f}  {sh['cover_50'][0]:.3f} {sh['cover_90'][0]:.3f}  {sh['pit_tail_lt05_gt95'][0]:.3f}/{sh['pit_tail_lt05_gt95'][1]:.3f}")
# the thin-far narrowness by segment under noslice
for label, seg in (("early", [v for v in vecs["noslice"] if v[3] <= "2026-09-13"]), ("recent", [v for v in vecs["noslice"] if v[3] >= "2026-09-14"])):
    recs = []
    for p, a, st, day, at in seg:
        mu, sd, u, pt, idx = T.A.read_vec(p, a); recs.append(dict(station=st, date=day, at=at, truth=a, mu=mu, sd=sd, u=u, p_truth=pt, ls=-math.log(max(pt, 1e-3))))
    rep = T.A.report(recs, label); print(f"noslice {label:7s} sd={rep['station_days']} E[z²] {rep['z_meansq'][0]:.3f} [{rep['z_meansq'][1]:.3f},{rep['z_meansq'][2]:.3f}] PITvar {rep['pit_var_ratio'][0]:.3f} tails {rep['pit_tail_lt05_gt95'][0]:.3f}/{rep['pit_tail_lt05_gt95'][1]:.3f}")
(HERE / "results_explore.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
