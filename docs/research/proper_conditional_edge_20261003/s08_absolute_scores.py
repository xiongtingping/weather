"""Absolute scores by era (did the model improve or the market worsen?), by P0 version."""
import numpy as np, pandas as pd
from common import boot, fmt
dec = pd.read_pickle("dec_scores.pkl")
P = dec[(dec.stage == "P0") & (dec.h > 36)].copy()
print("era          n_sd  LS_model  LS_market   Brier gap   |centre err| model / market   mean |gap| (buckets)")
for era, s in P.groupby("era"):
    w = s.w
    print(f"{era:12s} {s.groupby(['st','d']).ngroups:4d}  {np.average(s.ls_p, weights=w):+.3f}    {np.average(s.ls_q, weights=w):+.3f}     {np.average(s.dbr, weights=w):+.3f}       {np.average(s.ae_p, weights=w):.3f} / {np.average(s.ae_q, weights=w):.3f}            {np.average(s.gap.abs(), weights=w):.3f}")
print()
P["vg"] = pd.cut(P.ver, [0, 2, 5, 9, 13, 18, 19, 20, 99], labels=["v1-2", "v3-5", "v6-9", "v10-13", "v14-18", "v19", "v20", "v21+"])
print("version   n_sd days  LS_model  LS_market  dLS [95% CI]")
for vg, s in P.groupby("vg", observed=True):
    print(f"{vg:8s} {s.groupby(['st','d']).ngroups:5d} {s.d.nunique():3d}   {np.average(s.ls_p, weights=s.w):+.3f}    {np.average(s.ls_q, weights=s.w):+.3f}    {fmt(boot(s.dls, s.w, s.d))}")
print()
print("same station-days, model at P0>36h vs the MARKET LATER (market at 24-36h, 12-24h, 6-12h): how fast does the price sharpen?")
for stage, hb in (("P0", "48-60"), ("P0", "36-48"), ("P0-M", "24-36"), ("P1", "12-24"), ("P1", "6-12")):
    s = dec[(dec.stage == stage) & (dec.hb == hb) & (dec.era == "C:09-26+")]
    print(f"era C {stage:4s} {hb:6s}: LS_model {np.average(s.ls_p, weights=s.w):+.3f}  LS_market {np.average(s.ls_q, weights=s.w):+.3f}")
