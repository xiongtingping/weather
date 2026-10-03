"""Current chain (walk-forward replay, charter-247 form) vs served vs market, on the SAME served books.  Celsius stations, P0 > 36h."""
import numpy as np, pandas as pd
from scipy.optimize import minimize_scalar
from scipy import stats
from common import boot, fmt

FEE = 0.05
C = pd.read_pickle("replay_c.pkl")
C["era"] = np.where(C.d >= "2026-09-26", "C:09-26+", np.where(C.d >= "2026-09-12", "B:09-12..25", "A:<09-12"))
C = C[C.h > 36].copy()
Q = C[C.q.notna()].copy()
for name, col in (("served", "p"), ("replay", "rp")):
    Q["l_" + name] = np.log(Q[col])
Q["lq"] = np.log(Q.q)
g = Q.groupby("dec", sort=False)
dec = g.agg(st=("st", "first"), d=("d", "first"), era=("era", "first"), h=("h", "first"), ver=("ver", "first"))
yr = Q[Q.y == 1].set_index("dec")
for name in ("served", "replay"):
    dec["dls_" + name] = yr["l_" + name] - yr["lq"]
    col = "p" if name == "served" else "rp"
    dec["dbr_" + name] = ((Q[col] - Q.y) ** 2).groupby(Q.dec).sum() - ((Q.q - Q.y) ** 2).groupby(Q.dec).sum()
dec["ls_q"] = yr["lq"]
dec["w"] = 1.0 / dec.groupby(["st", "d"])["h"].transform("size")
dec["hb"] = pd.cut(dec.h, [36, 48, 60, 200], labels=["36-48", "48-60", ">60"])

print("=== scores vs market on identical books (C stations, P0 >36h): served chain of the day vs today's chain replayed walk-forward ===")
print("era           n_sd days | served: dLogScore            dBrier               | replay(current chain): dLogScore            dBrier")
for era in ("A:<09-12", "B:09-12..25", "C:09-26+", "all"):
    s = dec if era == "all" else dec[dec.era == era]
    print(f"{era:12s} {s.groupby(['st','d']).ngroups:5d} {s.d.nunique():3d} | {fmt(boot(s.dls_served, s.w, s.d)).split(' (')[0]} {fmt(boot(s.dbr_served, s.w, s.d)).split(' (')[0]} | {fmt(boot(s.dls_replay, s.w, s.d)).split(' (')[0]} {fmt(boot(s.dbr_replay, s.w, s.d)).split(' (')[0]}")
print("replay by lead bin (all eras):")
for hb, s in dec.groupby("hb", observed=True):
    print(f"  {hb:6s} n_sd={s.groupby(['st','d']).ngroups:4d}  dLS {fmt(boot(s.dls_replay, s.w, s.d))}   dBrier {fmt(boot(s.dbr_replay, s.w, s.d)).split(' (')[0]}")
print("replay by week:")
dec["wk"] = pd.to_datetime(dec.d).dt.strftime("%m-%d").str.slice(0, 5)
for wk, s in dec.groupby(pd.to_datetime(dec.d).dt.to_period("W")):
    print(f"  {str(wk):22s} n_sd={s.groupby(['st','d']).ngroups:4d}  served dLS {np.average(s.dls_served, weights=s.w):+.3f}  replay dLS {np.average(s.dls_replay, weights=s.w):+.3f}  replay dBrier {np.average(s.dbr_replay, weights=s.w):+.3f}  market LS {np.average(s.ls_q, weights=s.w):+.3f}")

def ll(w, lq, lp, code, ymask, wy):
    z = (1 - w) * lq + w * lp
    return -(wy * (z[ymask] - np.log(np.bincount(code, np.exp(z))))).sum()
def fit_ci(S, lcol, B=200, seed=4):
    sub = Q[Q.dec.isin(S.index)]
    code, uniq = pd.factorize(sub.dec.values); ym = sub.y.values == 1
    dd = dec.loc[uniq, "d"].values; wy0 = dec.loc[uniq, "w"].values
    f = lambda wy: minimize_scalar(ll, bounds=(-1, 2), args=(sub.lq.values, sub[lcol].values, code, ym, wy), method="bounded").x
    w = f(wy0); days = np.unique(dd); rng = np.random.default_rng(seed); ws = []
    for _ in range(B):
        mult = pd.Series(rng.choice(days, len(days))).value_counts()
        ws.append(f(wy0 * pd.Series(dd).map(mult).fillna(0).values))
    lo, hi = np.percentile(ws, [2.5, 97.5])
    return f"{w:+.3f} [{lo:+.3f},{hi:+.3f}]"
print()
print("=== log-pool weight on the model (0 = price already contains it) ===")
for era in ("A:<09-12", "B:09-12..25", "C:09-26+", "all"):
    s = dec if era == "all" else dec[dec.era == era]
    print(f"{era:12s} served w = {fit_ci(s, 'l_served')}   replay(current chain) w = {fit_ci(s, 'l_replay')}   days={s.d.nunique()}")

# taker rule with the replay vector; mark to the later market as well
from common import frame
F = frame()
Lq = F[(F.h <= 12) & F.q.notna()].sort_values("t")
first = Lq.groupby(["st", "d"])["dec"].first()
q12 = Lq[Lq.dec.isin(first.values)].set_index(["st", "d", "ord"])["q"].rename("q12")
C = C.join(q12, on=["st", "d", "ord"])
def legs(C, col):
    no = C[C.bid.notna() & (C[col] < C.bid)].copy(); no["side"] = "NO"; fee = FEE * no.bid * (1 - no.bid)
    no["truth"] = no.bid - no.y - fee; no["mark"] = no.bid - no.q12 - fee; no["px"] = no.bid
    ye = C[C.ask.notna() & (C[col] > C.ask)].copy(); ye["side"] = "YES"; fee = FEE * ye.ask * (1 - ye.ask)
    ye["truth"] = ye.y - ye.ask - fee; ye["mark"] = ye.q12 - ye.ask - fee; ye["px"] = ye.ask
    L = pd.concat([no, ye]); L["w"] = 1.0 / L.groupby(["st", "d", "ord", "side"])["px"].transform("size")
    return L
def tci(s, col):
    s = s[s[col].notna()]
    if s.w.sum() < 30: return f"n={s.w.sum():.0f}"
    dm = s.groupby("d").apply(lambda g: np.average(g[col], weights=g.w), include_groups=False)
    half = stats.t.ppf(0.975, len(dm) - 1) * dm.std(ddof=1) / np.sqrt(len(dm))
    return f"n={s.w.sum():5.0f} {fmt(boot(s[col], s.w, s.d)).split(' (')[0]} t[{dm.mean()-half:+.4f},{dm.mean()+half:+.4f}] +days {int((dm>0).sum())}/{len(dm)}"
print()
print("=== the Proper rule as a taker, pnl/share after fee: served vector vs replayed current chain (same books) ===")
for name, col in (("served", "p"), ("replay", "rp")):
    L = legs(C, col)
    for era in ("A:<09-12", "B:09-12..25", "C:09-26+", "all"):
        for side in ("NO", "YES"):
            s = L[(L.side == side)] if era == "all" else L[(L.side == side) & (L.era == era)]
            print(f"{name:6s} {era:12s} {side:3s} truth: {tci(s, 'truth')}")
            print(f"{'':6s} {'':12s} {'':3s} mark@12h: {tci(s, 'mark')}")
    L.to_pickle(f"legs_c_{name}.pkl")
