"""(a) is the pool weight real or mid noise?  tight books only.  (b) walk-forward pool.  (f) day by day."""
import numpy as np, pandas as pd
from scipy.optimize import minimize_scalar
from common import frame, boot, fmt

df = frame()
df = df[df.q.notna()].copy()
df["lp"] = np.log(df.p); df["lq"] = np.log(df.q)
df["sp"] = df.ask - df.bid
mid = (df.ask + df.bid) / 2
df["wide"] = ((mid >= 0.05) | (df.q >= 0.05)) & ~(df.sp <= 0.03)
dec = pd.read_pickle("dec_scores.pkl")
dec["tight"] = ~df.groupby("dec")["wide"].any()
P = dec[(dec.stage == "P0") & (dec.h > 36)]
print("P0>36h decisions:", len(P), "tight share:", round(P.tight.mean(), 3), " by era:", P.groupby("era").tight.mean().round(3).to_dict())

def ll(w, lq, lp, code, ymask, wy):
    z = (1 - w) * lq + w * lp
    denom = np.bincount(code, np.exp(z))
    return -(wy * (z[ymask] - np.log(denom))).sum()

def fit(ids):
    sub = df[df.dec.isin(ids)]
    code, uniq = pd.factorize(sub.dec.values)
    ymask = sub.y.values == 1
    wy = dec.loc[uniq, "w"].values
    return minimize_scalar(ll, bounds=(-1, 2), args=(sub.lq.values, sub.lp.values, code, ymask, wy), method="bounded").x

def fit_ci(mask, B=200, seed=1):
    S = dec[mask]
    w = fit(S.index)
    days = S.d.unique(); rng = np.random.default_rng(seed)
    by = {d: S.index[S.d == d] for d in days}
    # bootstrap by resampling days with replacement: weight multiplicity via repeated wy
    sub = df[df.dec.isin(S.index)]
    code, uniq = pd.factorize(sub.dec.values); ymask = sub.y.values == 1
    dday = dec.loc[uniq, "d"].values; wy0 = dec.loc[uniq, "w"].values
    ws = []
    for _ in range(B):
        pick = rng.choice(days, len(days)); mult = pd.Series(pick).value_counts()
        m = pd.Series(dday).map(mult).fillna(0).values
        ws.append(minimize_scalar(ll, bounds=(-1, 2), args=(sub.lq.values, sub.lp.values, code, ymask, wy0 * m), method="bounded").x)
    lo, hi = np.percentile(ws, [2.5, 97.5])
    return f"w={w:+.3f} [{lo:+.3f},{hi:+.3f}] days={len(days)} dec={len(S)}"

base = (dec.stage == "P0") & (dec.h > 36)
print("=== (a) log-pool weight on the model, P0 >36h: all books vs tight books (every bucket priced >=5c has spread <=3c) ===")
for era in ("A:<09-12", "B:09-12..25", "C:09-26+"):
    print(f"{era:12s} all books : {fit_ci(base & (dec.era == era))}")
    print(f"{era:12s} tight only: {fit_ci(base & (dec.era == era) & dec.tight)}")
    s = dec[base & (dec.era == era) & dec.tight]
    print(f"{'':12s} tight only: dLogScore {fmt(boot(s.dls, s.w, s.d))}   dBrier {fmt(boot(s.dbr, s.w, s.d))}")

print()
print("=== (f) P0 >36h, by event date: dLogScore model-market (station-day weighted), log-pool w fitted on that day alone ===")
S = dec[base]
for d, s in S.groupby("d"):
    if s.groupby("st").ngroups < 15: continue
    vers = sorted(s.ver.unique())
    print(f"{d} sd={s.groupby('st').ngroups:3d} ver={vers[0]}-{vers[-1]:<3d} dLS={np.average(s.dls, weights=s.w):+.3f} dBrier={np.average(s.dbr, weights=s.w):+.3f} model better on {np.average(s.dls>0, weights=s.w):.0%} w_day={fit(s.index):+.2f}")

print()
print("=== (b) walk-forward pool: w fitted on event dates <= D-3 (truth known when D is decided), scored on D; pooled minus market ===")
days = sorted(S.d.unique())
rows = []
for D in days:
    cut = (pd.Timestamp(D) - pd.Timedelta(days=3)).strftime("%Y-%m-%d")
    for window, lab in ((None, "expanding"), (10, "last10d")):
        tr = S[S.d <= cut]
        if window:
            lo = (pd.Timestamp(cut) - pd.Timedelta(days=window)).strftime("%Y-%m-%d")
            tr = tr[tr.d > lo]
        if tr.d.nunique() < 5: continue
        w = min(max(fit(tr.index), 0.0), 1.0)
        te = S[S.d == D]
        sub = df[df.dec.isin(te.index)]
        z = (1 - w) * sub.lq.values + w * sub.lp.values
        code, uniq = pd.factorize(sub.dec.values)
        r = np.exp(z) / np.bincount(code, np.exp(z))[code]
        ym = sub.y.values == 1
        dls = np.log(r[ym]) - sub.lq.values[ym]
        dbr = np.bincount(code, (r - sub.y.values) ** 2) - np.bincount(code, (sub.q.values - sub.y.values) ** 2)
        rows.append(pd.DataFrame({"d": D, "lab": lab, "w_fit": w, "dls": dls, "dbr": dbr, "w": dec.loc[uniq, "w"].values, "era": dec.loc[uniq, "era"].values}))
R = pd.concat(rows)
for lab in ("expanding", "last10d"):
    for era in ("all", "B:09-12..25", "C:09-26+"):
        s = R[(R.lab == lab) & ((R.era == era) | (era == "all"))]
        print(f"{lab:9s} {era:12s} mean fitted w={s.groupby('d').w_fit.first().mean():.3f}  dLS {fmt(boot(s.dls, s.w, s.d))}  dBrier {fmt(boot(s.dbr, s.w, s.d))}")
