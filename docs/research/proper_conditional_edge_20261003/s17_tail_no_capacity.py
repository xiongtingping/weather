"""Capacity of the tail-NO slice at the best bid (first firing per station-day-bucket), and loss-day profile."""
import numpy as np, pandas as pd
from common import frame
FEE = 0.05
F = frame()
def prep(T, col):
    mid = np.where(T.bid.notna() & T.ask.notna(), (T.bid + T.ask) / 2, np.where(T.ask.notna(), T.ask / 2, np.where(T.bid.notna(), (T.bid + 1) / 2, 0.0)))
    T["mid"] = mid; T["mrank"] = T.groupby("dec")["mid"].rank(ascending=False, method="first")
    T["adist"] = (T.ord - T.dec.map(T[T.mrank == 1].set_index("dec")["ord"])).abs()
    N = T[T.bid.notna() & (T.adist >= 2) & (T.bid >= 0.02) & (T[col] < T.bid) & (T.h <= 60)].copy()
    N["cost"] = (1 - N.bid) + FEE * N.bid * (1 - N.bid); N["pnl"] = 1 - N.y - N.cost
    return N.sort_values("t").drop_duplicates(["st", "d", "ord"])
C = pd.read_pickle("replay_c.pkl"); C = C[C.h > 36].copy()
for label, T, col in (("REPLAY current chain, C stations (34)", C, "rp"), ("SERVED 09-12+, all 45 stations", F[(F.stage == "P0") & (F.h > 36) & (F.d >= "2026-09-12")].copy(), "p")):
    N = prep(T, col)
    N["usd"] = N.bsz * N.cost; N["pnl_usd"] = N.bsz * N.pnl
    day = N.groupby("d").agg(legs=("pnl", "size"), hits=("y", "sum"), usd=("usd", "sum"), pnl=("pnl_usd", "sum"), eq=("pnl", "mean"))
    print(f"##### {label}: first firing inside 36-60h, sized at the displayed best-bid depth #####")
    print(f"days {len(day)}  legs/day median {day.legs.median():.0f}  NO cost deployable/day median ${day.usd.median():.0f} (mean ${day.usd.mean():.0f})")
    print(f"total: ${day.pnl.sum():.0f} on ${day.usd.sum():.0f} deployed = {day.pnl.sum()/day.usd.sum():+.2%};  per day mean ${day.pnl.mean():+.0f}, worst day ${day.pnl.min():+.0f}, best day ${day.pnl.max():+.0f}, winning days {int((day.pnl>0).sum())}/{len(day)}")
    print(f"equal-share view: hit rate {N.y.mean():.3f} at mean bid {N.bid.mean():.3f}; median best-bid depth {N.bsz.median():.0f} shares; legs with depth < 20 shares: {(N.bsz<20).mean():.0%}")
    # fixed 10 USDC per leg, capped by depth
    N["sh10"] = np.minimum(N.bsz, 10.0 / N.cost); d10 = N.assign(p10=N.sh10 * N.pnl, c10=N.sh10 * N.cost).groupby("d")[["p10", "c10"]].sum()
    print(f"at $10 per leg (depth-capped): ${d10.p10.sum():+.0f} on ${d10.c10.sum():.0f} = {d10.p10.sum()/d10.c10.sum():+.2%}; per day mean ${d10.p10.mean():+.2f}, worst ${d10.p10.min():+.0f}, winning days {int((d10.p10>0).sum())}/{len(d10)}")
    print(N.groupby(pd.cut(N.bid, [0.0199, 0.03, 0.05, 0.08, 0.12, 0.2, 1]), observed=True).agg(legs=("y", "size"), hit=("y", "mean"), bid=("bid", "mean"), pnl=("pnl", "mean")).round(4).to_string())
    print()
