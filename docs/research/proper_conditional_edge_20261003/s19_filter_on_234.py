"""The model as a FILTER on the live far-tail rule: at charter 234's own first firing, split by p < bid."""
import os
import numpy as np, pandas as pd
from common import frame

FEE = 0.05
F = frame()
def run(T, col, label):
    T = T.copy()
    mid = np.where(T.bid.notna() & T.ask.notna(), (T.bid + T.ask) / 2, np.where(T.ask.notna(), T.ask / 2, np.where(T.bid.notna(), (T.bid + 1) / 2, 0.0)))
    T["mid"] = mid; T["mrank"] = T.groupby("dec")["mid"].rank(ascending=False, method="first")
    T["adist"] = (T.ord - T.dec.map(T[T.mrank == 1].set_index("dec")["ord"])).abs()
    A = T[T.bid.notna() & (T.bid >= 0.02) & (T.adist >= 3) & T.q.notna() & (T.q <= 0.05)].sort_values("t").drop_duplicates(["st", "d", "ord"]).copy()
    A["pnl"] = A.bid - A.y - FEE * A.bid * (1 - A.bid); A["cost"] = (1 - A.bid) + FEE * A.bid * (1 - A.bid)
    A["dear"] = A[col] < A.bid
    days = A.d.unique(); rng = np.random.default_rng(5)
    def stat(S): return S.pnl.sum() / max(len(S), 1)
    by = {d: A[A.d == d] for d in days}
    bs = []
    for _ in range(3000):
        S = pd.concat([by[d] for d in rng.choice(days, len(days))])
        bs.append((stat(S[S.dear]), stat(S[~S.dear]), stat(S[S.dear]) - stat(S[~S.dear]), S[S.dear].pnl.sum() / S[S.dear].cost.sum(), S.pnl.sum() / S.cost.sum()))
    bs = np.array(bs); lo, hi = np.percentile(bs, [2.5, 97.5], axis=0)
    print(f"--- {label} ---  234's own first firing, n={len(A)} over {len(days)} days")
    print(f"  all 234 legs            : n={len(A):4d} hit={A.y.mean():.3f} pnl/share {stat(A):+.4f}  ROI on cost {A.pnl.sum()/A.cost.sum():+.4f} [{lo[4]:+.4f},{hi[4]:+.4f}]")
    print(f"  model says dear (p<bid) : n={A.dear.sum():4d} ({A.dear.mean():.0%}) hit={A[A.dear].y.mean():.3f} pnl/share {stat(A[A.dear]):+.4f} [{lo[0]:+.4f},{hi[0]:+.4f}]  ROI {A[A.dear].pnl.sum()/A[A.dear].cost.sum():+.4f} [{lo[3]:+.4f},{hi[3]:+.4f}]")
    print(f"  model does not (p>=bid) : n={(~A.dear).sum():4d} ({(~A.dear).mean():.0%}) hit={A[~A.dear].y.mean():.3f} pnl/share {stat(A[~A.dear]):+.4f} [{lo[1]:+.4f},{hi[1]:+.4f}]")
    print(f"  difference dear - not   : {stat(A[A.dear]) - stat(A[~A.dear]):+.4f} [{lo[2]:+.4f},{hi[2]:+.4f}]{'*' if lo[2] > 0 or hi[2] < 0 else ''}   share of bootstrap draws with dear > not: {(bs[:, 2] > 0).mean():.1%}")
    for u in ("C", "F"):
        if "unit" in A and (A.unit == u).sum() > 40:
            S = A[A.unit == u]
            print(f"    {u}: dear n={S.dear.sum()} hit={S[S.dear].y.mean():.3f} pnl {stat(S[S.dear]):+.4f} | not n={(~S.dear).sum()} hit={S[~S.dear].y.mean():.3f} pnl {stat(S[~S.dear]):+.4f}")
    A["half"] = np.where(A.d <= np.sort(days)[len(days) // 2 - 1], "first half", "second half")
    for h, S in A.groupby("half"):
        print(f"    {h}: dear {stat(S[S.dear]):+.4f} (n={S.dear.sum()}) | not {stat(S[~S.dear]):+.4f} (n={(~S.dear).sum()})")
S = F[(F.stage == "P0") & (F.h > 36)]
run(S[S.d >= "2026-09-12"], "p", "SERVED p, 09-12+, all stations")
run(S, "p", "SERVED p, whole record 08-15+, all stations")
if os.path.exists("replay_c.pkl"):
    C = pd.read_pickle("replay_c.pkl"); run(C[C.h > 36], "rp", "REPLAY current chain, C stations, 38 days")
