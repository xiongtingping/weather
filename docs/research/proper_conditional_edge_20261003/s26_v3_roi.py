"""ROI of the proposed Proper v3 exactly as specified: exactly two rungs from the market mode, YES bid >= 2c on the cent grid,
   depth >= 7, whole ladder quoted, p < bid, NO taker with fee, P0 36-60h, first firing, one bucket once with charter 234."""
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
    fee = FEE * T.bid * (1 - T.bid); T["cost"] = (1 - T.bid) + fee; T["pnl"] = (1 - T.y) - T.cost
    return T
def report(label, T, stake):
    key = ["st", "d", "ord"]
    r234 = T[(T.q <= 0.05) & (T.adist >= 3)].sort_values("t").drop_duplicates(key)
    v3 = T[(T.h <= 60) & (T.adist == 2) & (T.p < T.bid)].sort_values("t").drop_duplicates(key)
    v3 = v3[~pd.MultiIndex.from_frame(v3[key]).isin(pd.MultiIndex.from_frame(r234[key]))].copy()
    def roi(S, name):
        sh = np.minimum(stake / S.cost, np.minimum(0.8 * S.bsz, S.bsz - 1)); sh = np.where(sh >= 5, sh, 0.0)
        S = S[sh > 0]; sh = sh[sh > 0]
        codes, uniq = pd.factorize(S.d.values); k = len(uniq)
        sp = np.bincount(codes, sh * S.pnl, k); sc = np.bincount(codes, sh * S.cost, k)
        idx = np.random.default_rng(6).integers(0, k, size=(4000, k)); r = sp[idx].sum(1) / sc[idx].sum(1); lo, hi = np.percentile(r, [2.5, 97.5])
        occ = (sh * S.cost * S.h / 24).sum() / k      # USDC held on an average day (entry to the settlement clock)
        cum = np.cumsum(sp[np.argsort(uniq)]); dd = (cum - np.maximum.accumulate(np.maximum(cum, 0))).min()
        print(f"  {name:34s} legs {len(S):4d} over {k} days | win rate {1 - np.average(S.y, weights=sh):.1%} | ROI per trade {sp.sum()/sc.sum():+.2%} [{lo:+.2%},{hi:+.2%}]{'*' if lo>0 or hi<0 else ' '}"
              f" | median hold {S.h.median()/24:.1f} d | per day: cost {sc.mean():.0f} U, pnl {sp.mean():+.2f} U, capital held {occ:.0f} U -> {sp.mean()/occ:+.2%} a day"
              f" | day ROI worst {np.min(sp/sc):+.1%} best {np.max(sp/sc):+.1%}, winning days {int((sp>0).sum())}/{k}, max drawdown {dd:.1f} U")
    print(f"##### {label} (stake {stake:g} U a leg) #####")
    roi(v3, "Proper v3 (2 rungs, model says dear)")
    if "unit" in v3 and v3.unit.nunique() > 1:
        roi(v3[v3.unit == "C"], "  C stations only")
        roi(v3[v3.unit == "F"], "  F stations only")
    roi(r234, "234 as it runs, for comparison")
C = pd.read_pickle("replay_c.pkl")
for stake in (5.0, 10.0):
    report("SERVED 09-12+, 45 stations", prep(F[(F.stage == "P0") & (F.d >= "2026-09-12")]), stake)
    report("SERVED 09-18+", prep(F[(F.stage == "P0") & (F.d >= "2026-09-18")]), stake)
    report("REPLAY current chain, 34 C stations, 38 days", prep(C.assign(p=C.rp)), stake)
