"""Maker rungs: bounds on what the rule's resting orders are worth.
   upper = every rung fills at its price;  lower = a rung fills only when the next book has traded THROUGH it (pure adverse selection)."""
import numpy as np, pandas as pd
from scipy import stats
from common import frame, boot, fmt

df = frame().sort_values(["st", "d", "ord", "t"])
g = df.groupby(["st", "d", "ord"], sort=False)
df["nbid"] = g["bid"].shift(-1); df["nask"] = g["ask"].shift(-1)
df["gap_h"] = (pd.to_datetime(g["t"].shift(-1)) - pd.to_datetime(df.t)).dt.total_seconds() / 3600
# later market mark (first complete q at <=12h)
Lq = df[(df.h <= 12) & df.q.notna()].sort_values("t")
first = Lq.groupby(["st", "d"])["dec"].first()
q12 = Lq[Lq.dec.isin(first.values)].set_index(["st", "d", "ord"])["q"].rename("q12")
P = df[(df.stage == "P0") & (df.h > 36) & df.bid.notna() & df.ask.notna()].copy()
P = P.join(q12, on=["st", "d", "ord"])
P["tick"] = P.tick.fillna(0.01)
print("median hours between consecutive books of a station-day:", round(P.gap_h.median(), 2), " p90:", round(P.gap_h.quantile(0.9), 2))

def rows(side):
    T = P.copy()
    if side == "NO":
        T["m"] = np.maximum(T.ask - T.tick, T.bid + T.tick)       # YES-equivalent price the NO rung sells YES at
        T = T[(T.m < T.ask + 1e-12) & (T.p < T.m - 1e-12)]
        T["through"] = (T.nbid >= T.m - 1e-12) & (T.gap_h <= 3)    # next book's bid at/above my offer: bought through
        T["truth"] = T.m - T.y; T["mark"] = T.m - T.q12
    else:
        T["m"] = np.minimum(T.bid + T.tick, T.ask - T.tick)
        T = T[(T.m > T.bid - 1e-12) & (T.p > T.m + 1e-12)]
        T["through"] = (T.nask <= T.m + 1e-12) & (T.gap_h <= 3)    # next book's ask at/below my bid: sold through
        T["truth"] = T.y - T.m; T["mark"] = T.q12 - T.m
    T["side"] = side
    return T

def line(s, col, w):
    s2 = s[s[col].notna()]; w2 = w[s[col].notna()]
    if w2.sum() < 30: return f"n={w2.sum():.0f}"
    dm = pd.DataFrame({"d": s2.d, "x": s2[col] * w2, "w": w2}).groupby("d").sum(); dm = dm.x / dm.w
    half = stats.t.ppf(0.975, len(dm) - 1) * dm.std(ddof=1) / np.sqrt(len(dm)) if len(dm) >= 3 else np.nan
    return f"{fmt(boot(s2[col], w2, s2.d)).split(' (')[0]} t[{dm.mean()-half:+.4f},{dm.mean()+half:+.4f}] +days {int((dm>0).sum())}/{len(dm)}"

for side in ("NO", "YES"):
    T = rows(side)
    for era in ("B:09-12..25", "C:09-26+"):
        s = T[T.era == era]
        w = 1.0 / s.groupby(["st", "d", "ord"])["p"].transform("size")
        thr = s[s.through]; wt = w[s.through]
        print(f"=== {side} maker rungs the rule allows | era {era} | rung-observations {len(s)}, station-day-buckets {w.sum():.0f} ===")
        print(f"  upper bound (all fill)        truth: {line(s, 'truth', w)}")
        print(f"                                mark@12h: {line(s, 'mark', w)}")
        print(f"  traded-through share of rung-observations: {len(thr)/len(s):.1%}")
        print(f"  lower bound (through only)    truth per filled share: {line(thr, 'truth', wt)}")
        print(f"                                mark@12h per filled share: {line(thr, 'mark', wt)}")
        nt = s[~s.through]; wn = w[~s.through]
        print(f"  rungs NOT traded through      truth: {line(nt, 'truth', wn)}")
        print()
