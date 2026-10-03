"""The Proper rule with conditions: which conditions carry the result, and what the adjusted rule makes end to end.
   P0, 36-60h, NO legs, taker at the YES bid, fee included, one entry per station-day-bucket at the first firing."""
import itertools
import numpy as np, pandas as pd
from scipy import stats
from common import boot, frame

FEE = 0.05
F = frame()

def prep(T, col):
    T = T[(T.h > 36) & (T.h <= 60)].copy()
    mid = np.where(T.bid.notna() & T.ask.notna(), (T.bid + T.ask) / 2, np.where(T.ask.notna(), T.ask / 2, np.where(T.bid.notna(), (T.bid + 1) / 2, 0.0)))
    T["mid"] = mid; T["mrank"] = T.groupby("dec")["mid"].rank(ascending=False, method="first")
    mode = T[T.mrank == 1].set_index("dec")["ord"]
    T["adist"] = (T.ord - T.dec.map(mode)).abs()
    T["pm"] = T[col]
    T["prank"] = T.groupby("dec")["pm"].rank(ascending=False, method="first")
    T["centre_gap"] = (T.dec.map(T[T.prank == 1].set_index("dec")["ord"]) - T.dec.map(mode)).abs()
    T = T[T.bid.notna()].copy()
    fee = FEE * T.bid * (1 - T.bid)
    T["cost"] = (1 - T.bid) + fee; T["pnl"] = (1 - T.y) - T.cost
    T["grid"] = np.abs(T.bid * 100 - np.round(T.bid * 100)) < 1e-6
    T["half"] = np.where(T.d <= "2026-09-17", "H1", "H2")
    return T

def first(T, mask):
    return T[mask].sort_values("t").drop_duplicates(["st", "d", "ord"])

def line(S, w=None):
    if len(S) < 40: return f"n={len(S):4d}"
    w = np.ones(len(S)) if w is None else np.asarray(w, float)
    codes, uniq = pd.factorize(S.d.values); k = len(uniq)
    sp = np.bincount(codes, w * S.pnl, k); sc = np.bincount(codes, w * S.cost, k); sw = np.bincount(codes, w, k)
    idx = np.random.default_rng(4).integers(0, k, size=(3000, k))
    per = sp[idx].sum(1) / sw[idx].sum(1); lo, hi = np.percentile(per, [2.5, 97.5])
    return f"n={len(S):4d} bid={np.average(S.bid, weights=w):.3f} hit={np.average(S.y, weights=w):.3f} pnl/share {sp.sum()/sw.sum():+.4f} [{lo:+.4f},{hi:+.4f}]{'*' if lo>0 or hi<0 else ' '} ROI {sp.sum()/sc.sum():+.4f} +d {int((sp>0).sum())}/{k}"

C = pd.read_pickle("replay_c.pkl")
SETS = [("SERVED 09-12+, 45 stations", prep(F[(F.stage == "P0") & (F.d >= "2026-09-12")], "p")),
        ("REPLAY current chain, 34 C stations, 38 days", prep(C.assign(p=C.rp), "p"))]
for label, T in SETS:
    print(f"##### {label} #####")
    base = (T.p < T.bid)
    print("(1) one condition at a time, on top of the rule's own trigger p < bid")
    print(f"  no condition (every NO leg the rule fires)         : {line(first(T, base))}")
    for r in (0, 1, 2, 3, 4):
        print(f"  exactly {r}{'+' if r==4 else ' '} rungs from the market mode                : {line(first(T, base & ((T.adist == r) if r < 4 else (T.adist >= 4))))}")
    print(f"  2+ rungs                                            : {line(first(T, base & (T.adist >= 2)))}")
    print(f"  2+ rungs, bid >= 2c                                 : {line(first(T, base & (T.adist >= 2) & (T.bid >= 0.02)))}")
    print(f"  2+ rungs, bid <  2c                                 : {line(first(T, base & (T.adist >= 2) & (T.bid < 0.02)))}")
    core = base & (T.adist >= 2) & (T.bid >= 0.02)
    print("(2) further conditions on the core (2+ rungs, bid >= 2c)")
    print(f"  whole ladder quoted (market_reference complete)     : {line(first(T, core & T.q.notna()))}")
    print(f"  ladder NOT fully quoted                             : {line(first(T, core & T.q.isna()))}")
    print(f"  p < bid/2                                           : {line(first(T, core & (T.p < T.bid / 2)))}")
    print(f"  bid/2 <= p < bid                                    : {line(first(T[~T.index.isin(first(T, core & (T.p < T.bid / 2)).index)], core & (T.p >= T.bid / 2)))}")
    print(f"  bid 2-8c                                            : {line(first(T, core & (T.bid < 0.08)))}")
    print(f"  bid 8-20c                                           : {line(first(T, core & (T.bid >= 0.08) & (T.bid < 0.20)))}")
    print(f"  bid >= 20c                                          : {line(first(T, core & (T.bid >= 0.20)))}")
    print(f"  model mode == market mode                           : {line(first(T, core & (T.centre_gap == 0)))}")
    print(f"  model mode 1 rung from market mode                  : {line(first(T, core & (T.centre_gap == 1)))}")
    print(f"  model mode 2+ rungs from market mode                : {line(first(T, core & (T.centre_gap >= 2)))}")
    if "unit" in T:
        for u in ("C", "F"):
            if (T.unit == u).any(): print(f"  {u} stations                                          : {line(first(T, core & (T.unit == u)))}")
    for h in ("H1", "H2"):
        print(f"  {h}                                                  : {line(first(T, core & (T.half == h)))}")
    print("(3) the adjusted rule end to end: core + whole ladder quoted + cent grid + depth >= 7, 10 USDC a leg capped at 0.8 of the bid's size")
    A = first(T, core & T.q.notna() & T.grid & (T.bsz >= 7))
    sh = np.minimum(10.0 / A.cost, np.minimum(0.8 * A.bsz, A.bsz - 1))
    print(f"  per share                                           : {line(A)}")
    print(f"  10 USDC a leg                                       : {line(A, sh)}")
    day = pd.DataFrame({"d": A.d, "pnl": sh * A.pnl, "cost": sh * A.cost}).groupby("d").sum()
    per_sd = A.groupby(["st", "d"]).size()
    print(f"     legs/day median {A.groupby('d').size().median():.0f}; cost/day median {day.cost.median():.0f} U; pnl/day mean {day.pnl.mean():+.2f} U, worst {day.pnl.min():+.1f} U, best {day.pnl.max():+.1f} U; legs per station-day median {per_sd.median():.0f}, max {per_sd.max()}")
    s = np.abs(np.log(A.p / A.q))
    print(f"  shares ~ |log(p/q)| (the lane's own sizing)          : {line(A, s)}")
    in234 = (A.q <= 0.05) & (A.adist >= 3)
    print(f"  of which already 234's legs                         : {line(A[in234])}")
    print(f"  of which new (234 does not buy)                     : {line(A[~in234])}")
    print(f"     new legs at 10 USDC: {line(A[~in234], sh[~in234.values])}")
    print()
