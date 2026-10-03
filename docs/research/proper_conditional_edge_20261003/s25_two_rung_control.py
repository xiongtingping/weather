"""Exactly two rungs from the market mode, sendable legs (bid >= 2c on the cent grid, depth >= 7, whole ladder quoted), 36-60h:
   the rule's trigger (p < bid) against the same buckets bought blind."""
import numpy as np, pandas as pd
from common import frame
FEE = 0.05
F = frame()
def prep(T):
    T = T[(T.h > 36) & (T.h <= 60)].copy()
    mid = np.where(T.bid.notna() & T.ask.notna(), (T.bid + T.ask) / 2, np.where(T.ask.notna(), T.ask / 2, np.where(T.bid.notna(), (T.bid + 1) / 2, 0.0)))
    T["mid"] = mid; T["mrank"] = T.groupby("dec")["mid"].rank(ascending=False, method="first")
    T["adist"] = (T.ord - T.dec.map(T[T.mrank == 1].set_index("dec")["ord"])).abs()
    T = T[T.bid.notna() & T.q.notna() & (T.adist == 2) & (T.bid >= 0.02) & (T.bsz >= 7) & (np.abs(T.bid * 100 - np.round(T.bid * 100)) < 1e-6)].copy()
    fee = FEE * T.bid * (1 - T.bid); T["cost"] = (1 - T.bid) + fee; T["pnl"] = (1 - T.y) - T.cost
    return T
def line(S):
    if len(S) < 40: return f"n={len(S)}"
    codes, uniq = pd.factorize(S.d.values); k = len(uniq)
    sp = np.bincount(codes, S.pnl, k); sn = np.bincount(codes, minlength=k).astype(float)
    idx = np.random.default_rng(4).integers(0, k, size=(3000, k)); per = sp[idx].sum(1) / sn[idx].sum(1); lo, hi = np.percentile(per, [2.5, 97.5])
    return f"n={len(S):4d} bid={S.bid.mean():.3f} model={S.p.mean():.3f} hit={S.y.mean():.3f} pnl/share {S.pnl.mean():+.4f} [{lo:+.4f},{hi:+.4f}]{'*' if lo>0 or hi<0 else ' '} +d {int((sp>0).sum())}/{k}"
C = pd.read_pickle("replay_c.pkl")
key = ["st", "d", "ord"]
for label, T in (("SERVED 09-12+, 45 stations", prep(F[(F.stage == "P0") & (F.d >= "2026-09-12")])),
                 ("SERVED 09-12+, C stations", prep(F[(F.stage == "P0") & (F.d >= "2026-09-12") & (F.unit == "C")])),
                 ("REPLAY current chain, C stations, 38 days", prep(C.assign(p=C.rp)))):
    fire = T[T.p < T.bid].sort_values("t").drop_duplicates(key)
    blind = T.sort_values("t").drop_duplicates(key)
    never = blind[~pd.MultiIndex.from_frame(blind[key]).isin(pd.MultiIndex.from_frame(fire[key]))]
    print(f"--- {label} ---")
    print(f"  bought blind (first sendable book)   : {line(blind)}")
    print(f"  model says dear (p < bid)            : {line(fire)}")
    print(f"  model never says dear in the window  : {line(never)}")
