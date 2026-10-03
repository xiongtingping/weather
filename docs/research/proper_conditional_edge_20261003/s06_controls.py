"""Controls: is a positive slice the MODEL's doing, or the market's own price structure?  2x2: slice x (rule fires / does not)."""
import numpy as np, pandas as pd
from common import frame, boot, fmt

FEE = 0.05
df = frame()
P = df[(df.stage == "P0") & (df.h > 36)].copy()
mid = np.where(P.bid.notna() & P.ask.notna(), (P.bid + P.ask) / 2, np.where(P.ask.notna(), P.ask / 2, np.where(P.bid.notna(), (P.bid + 1) / 2, 0.0)))
P["mid"] = mid
P["mrank"] = P.groupby("dec")["mid"].rank(ascending=False, method="first")
P["dist"] = P.ord - P.dec.map(P[P.mrank == 1].set_index("dec")["ord"])
P["adist"] = P.dist.abs().clip(upper=4)
P.to_pickle("p0_gt36.pkl")

def cell(s):
    if len(s) == 0: return "-"
    w = 1.0 / s.groupby(["st", "d", "ord"])["pnl"].transform("size")
    if w.sum() < 30: return f"n={w.sum():.0f}"
    return f"n={w.sum():5.0f} px={np.average(s.px, weights=w):.3f} hit={np.average(s.y, weights=w):.3f} {fmt(boot(s.pnl, w, s.d)).split(' (')[0]}"

NO = P[P.bid.notna()].copy(); NO["px"] = NO.bid; NO["pnl"] = NO.bid - NO.y - FEE * NO.bid * (1 - NO.bid); NO["fires"] = NO.p < NO.bid
YE = P[P.ask.notna()].copy(); YE["px"] = YE.ask; YE["pnl"] = YE.y - YE.ask - FEE * YE.ask * (1 - YE.ask); YE["fires"] = YE.p > YE.ask
for name, T in (("NO taker @ YES bid", NO), ("YES taker @ ask", YE)):
    for era in ("B+C", "C"):
        E = T[T.era.isin(["B:09-12..25", "C:09-26+"])] if era == "B+C" else T[T.era == "C:09-26+"]
        print(f"=== {name} | era {era} ===  realised pnl/share after fee")
        for a in range(5):
            s = E[E.adist == a]
            print(f"rungs from market mode={a}{'+' if a==4 else ' '} | fires: {cell(s[s.fires])}")
            print(f"{'':28s}| not  : {cell(s[~s.fires])}")
        print()

# the far-tail NO slice with the same price floor either way: bid >= 2c, 2+ rungs out
print("=== far tail NO (2+ rungs from mode, YES bid >= 2c): model-conditional or market structure? ===")
F = NO[(NO.adist >= 2) & (NO.bid >= 0.02)]
for era in ("A:<09-12", "B:09-12..25", "C:09-26+"):
    s = F[F.era == era]
    print(f"{era:12s} fires: {cell(s[s.fires])}")
    print(f"{'':12s} not  : {cell(s[~s.fires])}")
    print(f"{'':12s} all  : {cell(s)}")
# and matched on price: within price bins, fires minus not
F = F.copy(); F["pb"] = pd.cut(F.bid, [0.0199, 0.03, 0.05, 0.08, 0.12, 0.2, 1])
print("price-matched (bid bins), era B+C:")
E = F[F.era != "A:<09-12"]
for b, s in E.groupby("pb", observed=True):
    print(f"  bid {str(b):14s} fires: {cell(s[s.fires])}")
    print(f"  {'':18s} not  : {cell(s[~s.fires])}")
print()
print("=== YES at the market's modal bucket: model-conditional or structural? ===")
M = YE[YE.adist == 0]
for era in ("A:<09-12", "B:09-12..25", "C:09-26+"):
    s = M[M.era == era]
    print(f"{era:12s} fires (p>ask): {cell(s[s.fires])}")
    print(f"{'':12s} not          : {cell(s[~s.fires])}")
