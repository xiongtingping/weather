"""Proper v3's legs: what is paid, what the model and the market say, what lands, and the edge a share after fee."""
import numpy as np, pandas as pd
from common import frame
FEE = 0.05
F = frame()
def prep(T):
    T = T[T.h > 36].copy()
    mid = np.where(T.bid.notna() & T.ask.notna(), (T.bid + T.ask) / 2, np.where(T.ask.notna(), T.ask / 2, np.where(T.bid.notna(), (T.bid + 1) / 2, 0.0)))
    T["mid"] = mid; T["mrank"] = T.groupby("dec")["mid"].rank(ascending=False, method="first")
    T["adist"] = (T.ord - T.dec.map(T[T.mrank == 1].set_index("dec")["ord"])).abs()
    T = T[T.bid.notna() & T.q.notna() & (T.bid >= 0.02) & (T.bsz >= 7) & (np.abs(T.bid * 100 - np.round(T.bid * 100)) < 1e-6)].copy()
    T["fee"] = FEE * T.bid * (1 - T.bid); T["edge"] = T.bid - T.y - T.fee; T["claim"] = T.bid - T.p - T.fee
    return T
def show(label, T):
    key = ["st", "d", "ord"]
    r234 = T[(T.q <= 0.05) & (T.adist >= 3)].sort_values("t").drop_duplicates(key)
    v3 = T[(T.h <= 60) & (T.adist == 2) & (T.p < T.bid)].sort_values("t").drop_duplicates(key)
    v3 = v3[~pd.MultiIndex.from_frame(v3[key]).isin(pd.MultiIndex.from_frame(r234[key]))]
    codes, uniq = pd.factorize(v3.d.values); k = len(uniq)
    se = np.bincount(codes, v3.edge, k); sn = np.bincount(codes, minlength=k).astype(float)
    idx = np.random.default_rng(6).integers(0, k, size=(4000, k)); b = se[idx].sum(1) / sn[idx].sum(1); lo, hi = np.percentile(b, [2.5, 97.5])
    print(f"{label}: legs {len(v3)}, days {k}")
    print(f"   YES bid {v3.bid.mean():.3f} (so NO is bought at {1 - v3.bid.mean():.3f}) | YES ask {v3.ask.mean():.3f} | market mid (normalised) {v3.q.mean():.3f} | model p {v3.p.mean():.3f} | actually landed {v3.y.mean():.3f} | fee {v3.fee.mean():.4f}")
    print(f"   edge a share after fee = bid - landed - fee = {v3.edge.mean():+.4f} [{lo:+.4f},{hi:+.4f}]{'*' if lo>0 or hi<0 else ''};  the model's own claim = bid - p - fee = {v3.claim.mean():+.4f};  realised / claimed = {v3.edge.mean()/v3.claim.mean():.2f}")
    for name, m in (("bid 2-5c", v3.bid < 0.05), ("bid 5-10c", (v3.bid >= 0.05) & (v3.bid < 0.10)), ("bid 10-20c", (v3.bid >= 0.10) & (v3.bid < 0.20)), ("bid >=20c", v3.bid >= 0.20)):
        s = v3[m]
        if len(s) >= 30: print(f"     {name:10s} n={len(s):4d} bid {s.bid.mean():.3f} model {s.p.mean():.3f} landed {s.y.mean():.3f} edge {s.edge.mean():+.4f}")
C = pd.read_pickle("replay_c.pkl")
S = F[(F.stage == "P0") & (F.d >= "2026-09-12")]
show("SERVED 09-12+, C stations", prep(S[S.unit == "C"]))
show("SERVED 09-12+, 45 stations", prep(S))
show("REPLAY current chain, C stations, 38 days", prep(C.assign(p=C.rp)))
# the YES side of the same rule, for the record: p above the ask, by distance from the market mode
def yes(label, T, col):
    T = T[(T.h > 36) & (T.h <= 60)].copy()
    mid = np.where(T.bid.notna() & T.ask.notna(), (T.bid + T.ask) / 2, np.where(T.ask.notna(), T.ask / 2, np.where(T.bid.notna(), (T.bid + 1) / 2, 0.0)))
    T["mid"] = mid; T["mrank"] = T.groupby("dec")["mid"].rank(ascending=False, method="first")
    T["adist"] = (T.ord - T.dec.map(T[T.mrank == 1].set_index("dec")["ord"])).abs().clip(upper=3)
    Y = T[T.ask.notna() & (T[col] > T.ask)].sort_values("t").drop_duplicates(["st", "d", "ord"]).copy()
    Y["edge"] = Y.y - Y.ask - FEE * Y.ask * (1 - Y.ask)
    print(f"{label}: YES legs the rule would fire (p > ask), edge a share after fee by rungs from the market mode:",
          " | ".join(f"{int(a)}{'+' if a==3 else ''}: n={len(s)} ask {s.ask.mean():.3f} landed {s.y.mean():.3f} {s.edge.mean():+.4f}" for a, s in Y.groupby("adist")))
yes("SERVED 09-12+, 45 stations", S, "p")
yes("REPLAY current chain, C stations", C, "rp")
