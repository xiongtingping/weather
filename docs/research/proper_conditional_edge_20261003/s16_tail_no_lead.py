"""Tail-NO slice by lead bin: replayed current chain (C stations, 38 days) and served (all stations)."""
import numpy as np, pandas as pd
from scipy import stats
from common import boot, frame

FEE = 0.05
F = frame()
Lq = F[(F.h <= 12) & F.q.notna()].sort_values("t"); first = Lq.groupby(["st", "d"])["dec"].first()
q12 = Lq[Lq.dec.isin(first.values)].set_index(["st", "d", "ord"])["q"].rename("q12")
def prep(T):
    T = T.join(q12, on=["st", "d", "ord"])
    mid = np.where(T.bid.notna() & T.ask.notna(), (T.bid + T.ask) / 2, np.where(T.ask.notna(), T.ask / 2, np.where(T.bid.notna(), (T.bid + 1) / 2, 0.0)))
    T["mid"] = mid; T["mrank"] = T.groupby("dec")["mid"].rank(ascending=False, method="first")
    T["adist"] = (T.ord - T.dec.map(T[T.mrank == 1].set_index("dec")["ord"])).abs()
    N = T[T.bid.notna() & (T.adist >= 2) & (T.bid >= 0.02)].copy()
    fee = FEE * N.bid * (1 - N.bid); N["truth"] = N.bid - N.y - fee; N["mark"] = N.bid - N.q12 - fee; N["px"] = N.bid
    N["hb2"] = pd.cut(N.h, [36, 42, 48, 54, 60, 200], labels=["36-42", "42-48", "48-54", "54-60", ">60"])
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
C = pd.read_pickle("replay_c.pkl"); C = C[C.h > 36].copy()
for label, T, col in (("REPLAY current chain, C stations", C, "rp"), ("SERVED 09-12+, all stations", F[(F.stage == "P0") & (F.h > 36) & (F.d >= "2026-09-12")].copy(), "p"),
                      ("SERVED 09-26+ (v14-v20), all stations", F[(F.stage == "P0") & (F.h > 36) & (F.d >= "2026-09-26")].copy(), "p")):
    N = prep(T); N["fires"] = N[col] < N.bid
    print(f"##### {label} #####")
    for hb, s in N.groupby("hb2", observed=True):
        print(f"{hb:6s} fires truth {tci(s[s.fires])}")
        print(f"{'':6s} fires mark  {tci(s[s.fires], 'mark')}")
        print(f"{'':6s} not   truth {tci(s[~s.fires])}")
    # one entry per station-day-bucket: the FIRST decision at which the rule fires inside the window (what a lane would actually take)
    fs = N[N.fires].sort_values("t").drop_duplicates(["st", "d", "ord"])
    print(f"first firing per station-day-bucket: truth {tci(fs)}")
    print(f"                                     mark  {tci(fs, 'mark')}")
    print(f"   median hours to settlement at that entry: {fs.h.median():.0f}; share at >60h: {(fs.h>60).mean():.0%}")
    fs2 = N[N.fires & (N.h <= 60)].sort_values("t").drop_duplicates(["st", "d", "ord"])
    print(f"first firing inside 36-60h          : truth {tci(fs2)}")
    print(f"                                     mark  {tci(fs2, 'mark')}")
    ls = N[N.fires].sort_values("t").drop_duplicates(["st", "d", "ord"], keep="last")
    print(f"last firing inside the window       : truth {tci(ls)}")
    print()
