"""The adjusted rule's legs split by whether charter 234 would EVER buy that bucket in its own window (P0, >36h),
   with the lane's own gate (whole ladder quoted) on both rules."""
import numpy as np, pandas as pd
from common import frame

FEE = 0.05
F = frame()
def prep(T):
    T = T[T.h > 36].copy()
    mid = np.where(T.bid.notna() & T.ask.notna(), (T.bid + T.ask) / 2, np.where(T.ask.notna(), T.ask / 2, np.where(T.bid.notna(), (T.bid + 1) / 2, 0.0)))
    T["mid"] = mid; T["mrank"] = T.groupby("dec")["mid"].rank(ascending=False, method="first")
    T["adist"] = (T.ord - T.dec.map(T[T.mrank == 1].set_index("dec")["ord"])).abs()
    T = T[T.bid.notna() & T.q.notna()].copy()
    fee = FEE * T.bid * (1 - T.bid); T["cost"] = (1 - T.bid) + fee; T["pnl"] = (1 - T.y) - T.cost
    T["grid"] = np.abs(T.bid * 100 - np.round(T.bid * 100)) < 1e-6
    return T
def line(S, w=None):
    if len(S) < 40: return f"n={len(S):4d}"
    w = np.ones(len(S)) if w is None else np.asarray(w, float)
    codes, uniq = pd.factorize(S.d.values); k = len(uniq)
    sp = np.bincount(codes, w * S.pnl, k); sc = np.bincount(codes, w * S.cost, k); sw = np.bincount(codes, w, k)
    idx = np.random.default_rng(4).integers(0, k, size=(3000, k)); per = sp[idx].sum(1) / sw[idx].sum(1); lo, hi = np.percentile(per, [2.5, 97.5])
    roi = sp[idx].sum(1) / sc[idx].sum(1); rlo, rhi = np.percentile(roi, [2.5, 97.5])
    return f"n={len(S):4d} bid={np.average(S.bid, weights=w):.3f} hit={np.average(S.y, weights=w):.3f} pnl/share {sp.sum()/sw.sum():+.4f} [{lo:+.4f},{hi:+.4f}]{'*' if lo>0 or hi<0 else ' '} ROI {sp.sum()/sc.sum():+.4f} [{rlo:+.4f},{rhi:+.4f}] +d {int((sp>0).sum())}/{k}"
C = pd.read_pickle("replay_c.pkl")
for label, T in (("SERVED 09-12+, 45 stations", prep(F[(F.stage == "P0") & (F.d >= "2026-09-12")])),
                 ("SERVED 09-18+ (second half), 45 stations", prep(F[(F.stage == "P0") & (F.d >= "2026-09-18")])),
                 ("REPLAY current chain, 34 C stations, 38 days", prep(C.assign(p=C.rp)))):
    key = ["st", "d", "ord"]
    r234 = T[(T.q <= 0.05) & (T.adist >= 3) & (T.bid >= 0.02) & T.grid & (T.bsz >= 7)].sort_values("t").drop_duplicates(key)
    adj = T[(T.h <= 60) & (T.p < T.bid) & (T.adist >= 2) & (T.bid >= 0.02) & T.grid & (T.bsz >= 7)].sort_values("t").drop_duplicates(key)
    i234 = pd.MultiIndex.from_frame(r234[key]); iadj = pd.MultiIndex.from_frame(adj[key])
    both = iadj.isin(i234)
    print(f"##### {label} #####")
    print(f"  234 as it runs (sendable legs)              : {line(r234)}")
    print(f"  adjusted Proper rule (sendable legs)        : {line(adj)}")
    print(f"    buckets 234 also buys at some point       : {line(adj[both])}")
    new = adj[~both]
    print(f"    buckets 234 never buys = the added book   : {line(new)}")
    sh = np.minimum(10.0 / new.cost, np.minimum(0.8 * new.bsz, new.bsz - 1))
    print(f"       the added book at 10 USDC a leg        : {line(new, sh)}")
    day = pd.DataFrame({"d": new.d, "pnl": sh * new.pnl, "cost": sh * new.cost}).groupby("d").sum()
    print(f"       legs/day median {new.groupby('d').size().median():.0f}; cost/day median {day.cost.median():.0f} U; pnl/day mean {day.pnl.mean():+.2f} U; worst day {day.pnl.min():+.1f} U; winning days {int((day.pnl>0).sum())}/{len(day)}")
    print(f"       exactly 2 rungs                        : {line(new[new.adist == 2])}")
    print(f"       3+ rungs (market mid above 5%)         : {line(new[new.adist >= 3])}")
    if "unit" in new:
        for u in ("C", "F"):
            if (new.unit == u).sum() >= 40: print(f"       {u} stations                             : {line(new[new.unit == u])}")
    only234 = r234[~i234.isin(iadj)]
    print(f"  234's legs the adjusted rule does not take  : {line(only234)}")
    union = pd.concat([r234, new])
    sh_u = np.minimum(10.0 / union.cost, np.minimum(0.8 * union.bsz, union.bsz - 1))
    print(f"  both lanes, one bucket once, 10 USDC a leg  : {line(union, sh_u)}")
    print()
