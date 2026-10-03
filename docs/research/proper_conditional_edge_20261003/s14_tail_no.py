"""Tail-NO slice: model-conditional or market structure?  stability, capacity.  P0 >36h, taker at the YES bid, fee included."""
import numpy as np, pandas as pd
from scipy import stats
from common import boot, fmt, frame

FEE = 0.05
C = pd.read_pickle("replay_c.pkl"); C = C[C.h > 36].copy()
F = frame()
Lq = F[(F.h <= 12) & F.q.notna()].sort_values("t"); first = Lq.groupby(["st", "d"])["dec"].first()
q12 = Lq[Lq.dec.isin(first.values)].set_index(["st", "d", "ord"])["q"].rename("q12")

def prep(T):
    T = T.join(q12, on=["st", "d", "ord"])
    mid = np.where(T.bid.notna() & T.ask.notna(), (T.bid + T.ask) / 2, np.where(T.ask.notna(), T.ask / 2, np.where(T.bid.notna(), (T.bid + 1) / 2, 0.0)))
    T["mid"] = mid; T["mrank"] = T.groupby("dec")["mid"].rank(ascending=False, method="first")
    T["adist"] = (T.ord - T.dec.map(T[T.mrank == 1].set_index("dec")["ord"])).abs()
    T["sp"] = T.ask - T.bid
    N = T[T.bid.notna() & (T.adist >= 2) & (T.bid >= 0.02)].copy()
    fee = FEE * N.bid * (1 - N.bid)
    N["truth"] = N.bid - N.y - fee; N["mark"] = N.bid - N.q12 - fee; N["px"] = N.bid
    N["half"] = np.where(N.d <= "2026-09-17", "H1", "H2")
    return N

def tci(s, col="truth"):
    s = s[s[col].notna()]
    if len(s) == 0: return "n=0"
    w = 1.0 / s.groupby(["st", "d", "ord"])["px"].transform("size")
    if w.sum() < 40: return f"n={w.sum():.0f}"
    dm = pd.DataFrame({"d": s.d, "x": s[col] * w, "w": w}).groupby("d").sum(); dm = dm.x / dm.w
    half = stats.t.ppf(0.975, len(dm) - 1) * dm.std(ddof=1) / np.sqrt(len(dm))
    est, lo, hi, k = boot(s[col], w, s.d)
    return f"n={w.sum():5.0f} bid={np.average(s.px, weights=w):.3f} hit={np.average(s.y, weights=w):.3f} {est:+.4f} [{lo:+.4f},{hi:+.4f}]{'*' if lo>0 or hi<0 else ' '} t[{dm.mean()-half:+.4f},{dm.mean()+half:+.4f}] +d {int((dm>0).sum())}/{len(dm)}"

for label, T, col in (("REPLAY current chain, C stations, 38 days", C, "rp"), ("SERVED, all stations, 09-12+ (21 days)", F[(F.stage == "P0") & (F.h > 36) & (F.d >= "2026-09-12")].copy(), "p")):
    N = prep(T); N["fires"] = N[col] < N.bid
    print(f"##### {label}: tail NO = 2+ rungs from the market mode, YES bid >= 2c #####")
    print(f"all such legs (market structure only): truth {tci(N)}")
    print(f"                                       mark  {tci(N, 'mark')}")
    print(f"model fires (p < bid)               : truth {tci(N[N.fires])}")
    print(f"                                       mark  {tci(N[N.fires], 'mark')}")
    print(f"model does not fire (p >= bid)      : truth {tci(N[~N.fires])}")
    print(f"                                       mark  {tci(N[~N.fires], 'mark')}")
    for half in ("H1", "H2"):
        s = N[N.half == half]
        print(f"  {half}: fires {tci(s[s.fires])}")
        print(f"  {half}: not   {tci(s[~s.fires])}")
    print("tight books only (spread <= 2c), so the bid is close to fair either way:")
    t = N[N.sp <= 0.02]
    print(f"  fires: truth {tci(t[t.fires])}")
    print(f"         mark  {tci(t[t.fires], 'mark')}")
    print(f"  not  : truth {tci(t[~t.fires])}")
    print(f"         mark  {tci(t[~t.fires], 'mark')}")
    print("by how far the model sits under the bid (ratio p/bid):")
    N["ratio"] = pd.cut(N[col] / N.bid, [0, 0.25, 0.5, 1.0, 2.0, 1e9], labels=["p<bid/4", "bid/4..bid/2", "bid/2..bid", "bid..2bid", ">2bid"])
    for r, s in N.groupby("ratio", observed=True):
        print(f"  {str(r):14s} truth {tci(s)}")
    print("market-only far-tail rule for comparison (normalised mid <= 5%, 3+ rungs, bid >= 2c):")
    M = N[(N.q <= 0.05) & (N.adist >= 3)]
    print(f"  all        : truth {tci(M)}")
    print(f"  model fires: truth {tci(M[M.fires])}")
    print(f"  model not  : truth {tci(M[~M.fires])}")
    # capacity at the first P0 decision of each station-day: depth at the best bid
    S = N[N.fires & (N["first"] == 1)]
    cap = S.assign(usd=S.bsz * (1 - S.bid), pnl_usd=S.bsz * S.truth).groupby("d").agg(legs=("px", "size"), usd=("usd", "sum"), pnl=("pnl_usd", "sum"))
    print(f"capacity at the first decision's best bid only: median legs/day {cap.legs.median():.0f}, median NO cost deployable/day ${cap.usd.median():.0f}, total pnl over {len(cap)} days ${cap.pnl.sum():.0f} on ${cap.usd.sum():.0f} deployed ({cap.pnl.sum()/cap.usd.sum():+.2%})")
    print()
