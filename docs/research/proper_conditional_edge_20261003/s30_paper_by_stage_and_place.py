"""The paper's decomposition by stage / lead, and its bid-ask-aware Log bet by where the leg sits. Served vectors."""
import numpy as np, pandas as pd
from common import frame
FEE = 0.05
F = frame()
F = F[F.q.notna()].copy()
p, q, y, ask, bid = F.p.values, F.q.values, F.y.values.astype(float), F.ask.values, F.bid.values
sl = np.log(p / q)
F["pi0"] = sl * (y - q); F["D0"] = q * np.log(q / p); F["c0"] = np.where(sl > 0, sl * q, -sl * (1 - q)); F["dS0"] = np.where(y == 1, sl, 0.0)
yes = np.isfinite(ask) & (p > ask); no = np.isfinite(bid) & (p < bid) & ~yes
ref = np.where(yes, ask, np.where(no, bid, np.nan)); lg = lambda x: np.log(x / (1 - x))
with np.errstate(invalid="ignore", divide="ignore"):
    s = np.where(yes | no, lg(np.clip(p, 1e-6, 1 - 1e-6)) - lg(np.clip(ref, 1e-6, 1 - 1e-6)), 0.0)
    F["pi1"] = np.where(yes | no, s * (y - ref), 0.0) - np.where(yes | no, np.abs(s) * FEE * ref * (1 - ref), 0.0)
    F["c1"] = np.abs(s) * np.where(yes, ask, np.where(no, 1 - bid, 0.0))
F["yes"] = yes; F["no"] = no
mid = np.where(F.bid.notna() & F.ask.notna(), (F.bid + F.ask) / 2, np.where(F.ask.notna(), F.ask / 2, (F.bid + 1) / 2))
F["mid"] = mid; F["mrank"] = F.groupby("dec")["mid"].rank(ascending=False, method="first")
F["adist"] = (F.ord - F.dec.map(F[F.mrank == 1].set_index("dec")["ord"])).abs()
F["w"] = 1.0 / F.groupby(["st", "d", "stage", "hb", "ord"], observed=True)["p"].transform("size")
def roi(S, num, den):
    if S[den].abs().sum() == 0 or len(S) < 50: return "      n/a"
    codes, uniq = pd.factorize(S.d.values); k = len(uniq)
    sn = np.bincount(codes, S.w * S[num], k); sd = np.bincount(codes, S.w * S[den], k)
    idx = np.random.default_rng(5).integers(0, k, size=(3000, k)); r = sn[idx].sum(1) / sd[idx].sum(1); lo, hi = np.percentile(r, [2.5, 97.5])
    return f"{100*sn.sum()/sd.sum():+6.1f}% [{100*lo:+5.1f},{100*hi:+5.1f}]{'*' if lo>0 or hi<0 else ' '}"
print("=== by stage and hours to settlement: Log score gap + D at the mid, and the bid-ask-aware Log bet after fee ===")
for era, E in (("09-12+", F[F.d >= "2026-09-12"]), ("09-26+", F[F.d >= "2026-09-26"])):
    for stage, hb in (("P0", "48-60"), ("P0", "36-48"), ("P0-M", "36-48"), ("P0-M", "24-36"), ("P1", "12-24"), ("P1", "6-12"), ("P1", "0-6")):
        S = E[(E.stage == stage) & (E.hb == hb)]
        if S.dec.nunique() < 100: continue
        sd = S.groupby(["st", "d"]).ngroups; tot = S.w.sum() / S.groupby("dec").ngroups  # normaliser not needed for ratios
        dS = (S.w * S.dS0).sum() / sd; D = (S.w * S.D0).sum() / sd
        sp = (S.ask - S.bid)[(S.mid >= 0.05)].median()
        print(f"{era} {stage:4s} {hb:6s} sd={sd:4d}  score gap {dS:+.3f} + D {D:+.3f} = {dS+D:+.3f}  at-mid ROI {roi(S, 'pi0', 'c0')}   bid-ask-aware after fee {roi(S, 'pi1', 'c1')}   median spread (mid>=5c) {sp:.3f}")
print()
print("=== the bid-ask-aware Log bet after fee, P0 >36h, by side and rungs from the market mode ===")
P = F[(F.stage == "P0") & (F.h > 36)].copy(); P["w"] = 1.0 / P.groupby(["st", "d", "ord"])["p"].transform("size")
for era, E in (("09-12+", P[P.d >= "2026-09-12"]), ("09-26+", P[P.d >= "2026-09-26"])):
    tot = (E.w * E.c1).sum()
    for side, m in (("NO ", E.no), ("YES", E.yes)):
        for name, r in (("at the mode", E.adist == 0), ("1 rung", E.adist == 1), ("2 rungs", E.adist == 2), ("3+ rungs", E.adist >= 3)):
            S = E[m & r & (E.c1 > 0)]
            print(f"{era} {side} {name:11s} share of cost {(S.w * S.c1).sum()/tot:5.1%}   ROI after fee {roi(S, 'pi1', 'c1')}")
