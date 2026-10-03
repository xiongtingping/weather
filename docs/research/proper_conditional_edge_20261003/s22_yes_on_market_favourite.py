"""YES at the ask on the market's own favourites (normalised mid >= 25%) where the model's p is above the ask.
   All P0 >36h decisions, one entry per station-day-bucket at the first firing; truth and the <=12h market mark."""
import numpy as np, pandas as pd
from scipy import stats
from common import boot, frame

FEE = 0.05
F = frame()
Lq = F[(F.h <= 12) & F.q.notna()].sort_values("t"); first = Lq.groupby(["st", "d"])["dec"].first()
q12 = Lq[Lq.dec.isin(first.values)].set_index(["st", "d", "ord"])["q"].rename("q12")
def run(T, col, label):
    T = T[T.q.notna() & T.ask.notna() & (T.q >= 0.25)].join(q12, on=["st", "d", "ord"]).copy()
    fee = FEE * T.ask * (1 - T.ask)
    T["truth"] = T.y - T.ask - fee; T["mark"] = T.q12 - T.ask - fee
    def line(S, c):
        S = S[S[c].notna()]
        if len(S) < 30: return f"n={len(S)}"
        dm = S.groupby("d")[c].mean(); half = stats.t.ppf(0.975, len(dm) - 1) * dm.std(ddof=1) / np.sqrt(len(dm))
        est, lo, hi, k = boot(S[c], np.ones(len(S)), S.d)
        return f"n={len(S):4d} ask={S.ask.mean():.3f} hit={S.y.mean():.3f} {est:+.4f} [{lo:+.4f},{hi:+.4f}]{'*' if lo>0 or hi<0 else ' '} t[{dm.mean()-half:+.4f},{dm.mean()+half:+.4f}] +d {int((dm>0).sum())}/{len(dm)}"
    key = ["st", "d", "ord"]
    fire = T[T[col] > T.ask].sort_values("t").drop_duplicates(key)
    rest = T[T[col] <= T.ask].sort_values("t").drop_duplicates(key)
    print(f"--- {label} ---")
    print(f"  model > ask : truth {line(fire, 'truth')}")
    print(f"                mark  {line(fire, 'mark')}")
    print(f"  model <= ask: truth {line(rest, 'truth')}")
    days = np.sort(fire.d.unique()); cut = days[len(days) // 2 - 1]
    print(f"  model > ask, first half (<= {cut}): {line(fire[fire.d <= cut], 'truth')}")
    print(f"  model > ask, second half           : {line(fire[fire.d > cut], 'truth')}")
    for lo_, hi_ in ((36, 48), (48, 60), (60, 200)):
        print(f"  model > ask, {lo_}-{hi_}h: {line(fire[(fire.h > lo_) & (fire.h <= hi_)], 'truth')}")
S = F[(F.stage == "P0") & (F.h > 36)]
run(S[S.d >= "2026-09-14"], "p", "SERVED, 09-14+, all stations")
run(S[S.d < "2026-09-14"], "p", "SERVED, before 09-14, all stations")
C = pd.read_pickle("replay_c.pkl"); run(C[C.h > 36], "rp", "REPLAY current chain, C stations, 38 days")
