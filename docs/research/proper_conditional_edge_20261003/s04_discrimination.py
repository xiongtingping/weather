"""T5: does the model's opinion move the payoff at a GIVEN executable price?  (discrimination, with controls)"""
import numpy as np, pandas as pd
from common import frame, boot, fmt

FEE = 0.05
df = frame()
P = df[(df.stage == "P0") & (df.h > 36)].copy()
P["tick"] = P.tick.fillna(0.01)

def table(S, px, sign, label, eras=("all", "C:09-26+")):
    """sign=+1: buying YES at px (payoff Y-px-fee). sign=-1: buying NO against px (payoff px-Y-fee)."""
    S = S[S[px].notna()].copy()
    S["edge"] = sign * (S.p - S[px])            # the model's claimed edge per share, before fee
    S["pnl"] = sign * (S.y - S[px]) - FEE * S[px] * (1 - S[px])
    S["eb"] = pd.cut(S.edge, [-2, -0.10, -0.05, -0.02, 0, 0.02, 0.05, 0.10, 2])
    S["w"] = 1.0 / S.groupby(["st", "d", "ord"])["pnl"].transform("size")
    for era in eras:
        T = S if era == "all" else S[S.era == era]
        print(f"--- {label} | era {era} ---   (rule fires where edge > 0)")
        print(f"{'model edge bin':>16s} {'legs':>6s} {'px':>6s} {'p':>6s} {'hit':>6s}  realised pnl/share after fee [95% CI]")
        for b, s in T.groupby("eb", observed=True):
            if s.w.sum() < 20: continue
            print(f"{str(b):>16s} {s.w.sum():6.0f} {np.average(s[px],weights=s.w):6.3f} {np.average(s.p,weights=s.w):6.3f} {np.average(s.y,weights=s.w):6.3f}  {fmt(boot(s.pnl, s.w, s.d))}")
        fire = T[T.edge > 0]; rest = T[T.edge <= 0]
        print(f"{'FIRES (edge>0)':>16s} {fire.w.sum():6.0f} {'':20s}  {fmt(boot(fire.pnl, fire.w, fire.d))}")
        print(f"{'does not fire':>16s} {rest.w.sum():6.0f} {'':20s}  {fmt(boot(rest.pnl, rest.w, rest.d))}")
        # slope of realised excess on claimed edge, with price-bin fixed effects removed
        T = T.copy(); T["pb"] = pd.cut(T[px], [0, .02, .05, .1, .2, .35, .5, 1])
        T["ex"] = sign * (T.y - T[px])
        for c in ("ex", "edge"):
            m = T.groupby("pb", observed=True).apply(lambda g: np.average(g[c], weights=g.w), include_groups=False)
            T[c + "_c"] = T[c] - T.pb.map(m).astype(float)
        codes, uniq = pd.factorize(T.d.values); k = len(uniq)
        sxy = np.bincount(codes, T.w * T.edge_c * T.ex_c, k); sxx = np.bincount(codes, T.w * T.edge_c ** 2, k)
        idx = np.random.default_rng(9).integers(0, k, size=(2000, k)); bs = sxy[idx].sum(1) / sxx[idx].sum(1)
        lo, hi = np.percentile(bs, [2.5, 97.5])
        print(f"  slope of realised excess on claimed edge (price-bin FE): {sxy.sum()/sxx.sum():+.3f} [{lo:+.3f},{hi:+.3f}]{'*' if lo>0 or hi<0 else ''}  (1 = model's claim fully realised, 0 = price already right)")
        print()

print("##### TAKER, NO side: buy NO against the YES bid #####")
table(P, "bid", -1, "NO taker @ YES bid")
print("##### TAKER, YES side: buy YES at the ask #####")
table(P, "ask", +1, "YES taker @ ask")
B = P[P.bid.notna() & P.ask.notna()].copy()
B["m_no"] = np.maximum(B.ask - B.tick, B.bid + B.tick)   # YES-equivalent price of the NO rung
B["m_yes"] = np.minimum(B.bid + B.tick, B.ask - B.tick)
# maker: no fee; reuse table with fee by adding it back
FEE = 0.0
print("##### MAKER upper bound (every rung fills at its price, no selection), NO rung = sell YES at ask - tick #####")
table(B, "m_no", -1, "NO maker rung, all-fill bound")
print("##### MAKER upper bound, YES rung = bid + tick #####")
table(B, "m_yes", +1, "YES maker rung, all-fill bound")
