"""The board's 'two rulers by band' table, rebuilt on the same definition, then asked the question the table does not ask.
   Market = normalised mid of the median best ask / best bid (0 when none) over the 4 hours after the first P0 decision;
   model = that first P0's served vector; truth = settled bucket; fully priced ladders only."""
import numpy as np, pandas as pd
from scipy.optimize import minimize
from common import frame

EPOCH = "2026-09-14T05:52:13"
BANDS = [0, .05, .10, .15, .20, .25, .30, .35, .40, .45, .50, 1.01]
F = frame()
P = F[F.stage == "P0"].copy()
P["ts"] = pd.to_datetime(P.t, format="ISO8601")
t0 = P.groupby(["st", "d"])["ts"].transform("min")
W = P[(P.ts - t0) <= pd.Timedelta(hours=4)].copy()
W["bid0"] = W.bid.fillna(0.0)
g = W.groupby(["st", "d", "ord"])
R = g.agg(ask=("ask", "median"), bid=("bid0", "median"), n_ask=("ask", "count"), y=("y", "first"), k=("k", "first"), unit=("unit", "first")).reset_index()
first = P[P.ts == t0][["st", "d", "ord", "p", "t", "ver"]].drop_duplicates(["st", "d", "ord"])
R = R.merge(first, on=["st", "d", "ord"], how="inner")
ok = R.groupby(["st", "d"])["n_ask"].transform("min") > 0
R = R[ok].copy()
R["mid"] = (R.ask + R.bid) / 2
R["q"] = R.mid / R.groupby(["st", "d"])["mid"].transform("sum")
R["seg"] = np.where(R.t >= EPOCH, "current (09-14 epoch on)", "history")
R["sd"] = R.st + "|" + R.d

def bands(S, col):
    S = S.assign(b=pd.cut(S[col], BANDS, right=False))
    return S.groupby("b", observed=True).agg(n=("y", "size"), said=(col, "mean"), hit=("y", "mean"), ask=("ask", "mean"))

def murphy(S, col):
    t = bands(S, col); N = t.n.sum(); base = S.y.mean()
    rel = (t.n * (t.said - t.hit) ** 2).sum() / N; res = (t.n * (t.hit - base) ** 2).sum() / N
    return rel, res, base * (1 - base)

def scores(S):
    """Per station-day Brier (vector) and LogScore for q and p; event-day bootstrap of the differences."""
    S = S.copy()
    S["bq"] = (S.q - S.y) ** 2; S["bp"] = (S.p - S.y) ** 2
    sd = S.groupby("sd").agg(d=("d", "first"), bq=("bq", "sum"), bp=("bp", "sum"))
    yr = S[S.y == 1].set_index("sd")
    sd["lq"] = np.log(yr.q.clip(lower=1e-3)); sd["lp"] = np.log(yr.p.clip(lower=1e-3))
    return sd

def ci(x, d, B=3000, seed=3):
    codes, uniq = pd.factorize(np.asarray(d)); k = len(uniq)
    sx = np.bincount(codes, x, k); sn = np.bincount(codes, minlength=k).astype(float)
    idx = np.random.default_rng(seed).integers(0, k, size=(B, k)); bs = sx[idx].sum(1) / sn[idx].sum(1)
    lo, hi = np.percentile(bs, [2.5, 97.5])
    return f"{x.mean():+.4f} [{lo:+.4f},{hi:+.4f}]{'*' if lo > 0 or hi < 0 else ' '}"

def recal_lodo(S, col):
    """Recalibrate a ruler by its own band hit rates fitted on the OTHER event days, then renormalise each ladder."""
    S = S.assign(b=pd.cut(S[col], BANDS, right=False))
    out = pd.Series(index=S.index, dtype=float)
    tot = S.groupby("b", observed=True).y.agg(["sum", "count"])
    for d, s in S.groupby("d"):
        own = s.groupby("b", observed=True).y.agg(["sum", "count"]).reindex(tot.index).fillna(0)
        rate = ((tot["sum"] - own["sum"]) / (tot["count"] - own["count"]).clip(lower=1)).clip(lower=1e-3)
        out.loc[s.index] = s.b.map(rate).astype(float).values
    return out / out.groupby(S.sd).transform("sum")

def logit2(S):
    X = np.column_stack([np.ones(len(S)), np.log(S.q / (1 - S.q)), np.log(S.p.clip(1e-3, 1 - 1e-3) / (1 - S.p.clip(1e-3, 1 - 1e-3)))]); y = S.y.values.astype(float)
    f = lambda X, y: minimize(lambda b: (np.logaddexp(0, X @ b) - y * (X @ b)).sum(), np.zeros(3), jac=lambda b: X.T @ (1 / (1 + np.exp(-(X @ b))) - y), method="BFGS").x
    b = f(X, y); days = S.d.values; uniq = np.unique(days); rng = np.random.default_rng(8); idx = {d: np.where(days == d)[0] for d in uniq}; bs = []
    for _ in range(300):
        ii = np.concatenate([idx[d] for d in rng.choice(uniq, len(uniq))]); bs.append(f(X[ii], y[ii]))
    lo, hi = np.percentile(np.array(bs), [2.5, 97.5], axis=0)
    return f"market coef {b[1]:+.2f} [{lo[1]:+.2f},{hi[1]:+.2f}]   model coef {b[2]:+.2f} [{lo[2]:+.2f},{hi[2]:+.2f}]{'*' if lo[2] > 0 or hi[2] < 0 else ''}"

for seg in ("current (09-14 epoch on)", "history"):
    S = R[R.seg == seg]
    print(f"##### {seg}: {S.sd.nunique()} station-days, {S.d.nunique()} event days, {len(S)} buckets #####")
    tm, tp = bands(S, "q"), bands(S, "p")
    print("band          | market said  ask    hit    n   | model said   hit    n")
    for b in tm.index.union(tp.index):
        a = tm.loc[b] if b in tm.index else None; c = tp.loc[b] if b in tp.index else None
        print(f"{str(b):13s} | " + (f"{a.said:.3f}  {a.ask:.3f}  {a.hit:.3f} {int(a.n):5d}" if a is not None else " " * 27) + " | " + (f"{c.said:.3f}  {c.hit:.3f} {int(c.n):5d}" if c is not None else ""))
    rq, sq, unc = murphy(S, "q"); rp, sp, _ = murphy(S, "p")
    print(f"per-bucket Brier pieces (lower REL = says the right number; higher RES = tells buckets apart):")
    print(f"  market: REL {rq:.5f}  RES {sq:.5f}  -> UNC-RES+REL = {unc - sq + rq:.5f}")
    print(f"  model : REL {rp:.5f}  RES {sp:.5f}  -> UNC-RES+REL = {unc - sp + rp:.5f}")
    sd = scores(S)
    print(f"per station-day, model minus market:  Brier {ci((sd.bp - sd.bq).values, sd.d)}   LogScore {ci((sd.lp - sd.lq).values, sd.d)}")
    S2 = S.assign(q=recal_lodo(S, "q"))
    sd2 = scores(S2)
    print(f"  ... against the market RE-CALIBRATED on its own price (other days' band hit rates): Brier {ci((sd2.bp - sd2.bq).values, sd2.d)}   LogScore {ci((sd2.lp - sd2.lq).values, sd2.d)}")
    S3 = S2.assign(p=recal_lodo(S, "p"))
    sd3 = scores(S3)
    print(f"  ... both re-calibrated the same way:                                               Brier {ci((sd3.bp - sd3.bq).values, sd3.d)}   LogScore {ci((sd3.lp - sd3.lq).values, sd3.d)}")
    print(f"same bucket, both numbers in: {logit2(S)}")
    print(f"same bucket, market re-calibrated first: {logit2(S2)}")
    print("inside one market band, does the model's side tell which buckets hit?  (hit rate where model > market vs model < market)")
    S = S.assign(b=pd.cut(S.q, BANDS, right=False), up=S.p > S.q)
    for b, s in S.groupby("b", observed=True):
        u, v = s[s.up], s[~s.up]
        if len(u) < 30 or len(v) < 30: continue
        x = pd.concat([u.assign(z=u.y - v.y.mean())]); 
        print(f"  market {str(b):13s} model higher: n={len(u):4d} model {u.p.mean():.3f} market {u.q.mean():.3f} hit {u.y.mean():.3f} | model lower: n={len(v):4d} model {v.p.mean():.3f} market {v.q.mean():.3f} hit {v.y.mean():.3f}")
    print()
R.to_pickle("board_population.pkl")
