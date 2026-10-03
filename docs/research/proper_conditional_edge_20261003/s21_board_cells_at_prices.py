"""The two cells of the board table where the model is the one that is right, taken at prices that can be traded.
   Board population (first P0, median ask / bid over the next 4 hours), current segment and history."""
import numpy as np, pandas as pd
from scipy import stats

FEE = 0.05
R = pd.read_pickle("board_population.pkl")
def line(S, col):
    if len(S) < 30: return f"n={len(S)}"
    codes, uniq = pd.factorize(S.d.values); k = len(uniq)
    sx = np.bincount(codes, S[col], k); sn = np.bincount(codes, minlength=k).astype(float)
    idx = np.random.default_rng(2).integers(0, k, size=(3000, k)); bs = sx[idx].sum(1) / sn[idx].sum(1)
    lo, hi = np.percentile(bs, [2.5, 97.5]); dm = S.groupby("d")[col].mean()
    return f"n={len(S):4d} price={S.px.mean():.3f} model={S.p.mean():.3f} market={S.q.mean():.3f} hit={S.y.mean():.3f}  pnl/share {S[col].mean():+.4f} [{lo:+.4f},{hi:+.4f}]{'*' if lo>0 or hi<0 else ' '} +d {int((dm>0).sum())}/{len(dm)}"
for seg in ("current (09-14 epoch on)", "history"):
    S = R[R.seg == seg].copy()
    print(f"##### {seg} #####")
    Y = S.assign(px=S.ask); Y["pnl"] = Y.y - Y.ask - FEE * Y.ask * (1 - Y.ask)
    print("YES at the ask (fee included):")
    for lo_, hi_ in ((0.15, 0.25), (0.25, 0.35), (0.35, 1.01), (0.25, 1.01)):
        B = Y[(Y.q >= lo_) & (Y.q < hi_)]
        print(f"  market mid {lo_:.2f}-{hi_:.2f}  all buckets        : {line(B, 'pnl')}")
        print(f"  {'':22s} model > market mid : {line(B[B.p > B.q], 'pnl')}")
        print(f"  {'':22s} model > ask (rule) : {line(B[B.p > B.ask], 'pnl')}")
        print(f"  {'':22s} model < market mid : {line(B[B.p < B.q], 'pnl')}")
    N = S[S.bid > 0].assign(px=lambda x: x.bid); N["pnl"] = N.bid - N.y - FEE * N.bid * (1 - N.bid)
    print("NO against the YES bid (fee included), buckets that have a bid:")
    for lo_, hi_ in ((0.0, 0.05), (0.05, 0.10), (0.10, 0.15), (0.0, 0.15)):
        B = N[(N.q >= lo_) & (N.q < hi_)]
        print(f"  market mid {lo_:.2f}-{hi_:.2f}  all with a bid     : {line(B, 'pnl')}")
        print(f"  {'':22s} model < bid (rule) : {line(B[B.p < B.bid], 'pnl')}")
        print(f"  {'':22s} model >= bid       : {line(B[B.p >= B.bid], 'pnl')}")
    print(f"share of buckets with any YES bid in the 4h window: market mid <5%: {(S[S.q<0.05].bid>0).mean():.0%}, 5-10%: {(S[(S.q>=0.05)&(S.q<0.10)].bid>0).mean():.0%}, 10-15%: {(S[(S.q>=0.10)&(S.q<0.15)].bid>0).mean():.0%}")
    sp = (S.ask - S.bid)
    print(f"median spread (ask - bid) by market mid: <5% {sp[S.q<0.05].median():.3f}, 5-15% {sp[(S.q>=0.05)&(S.q<0.15)].median():.3f}, 15-30% {sp[(S.q>=0.15)&(S.q<0.30)].median():.3f}, >=30% {sp[S.q>=0.30].median():.3f}; ladder ask sum median {S.groupby('sd').ask.sum().median():.2f}, mid sum median {S.groupby('sd').mid.sum().median():.2f}")
    print()
