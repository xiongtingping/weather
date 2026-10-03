"""(i) bucket-level logistic Y ~ logit(q) + logit(p) [+spread], by slice.  (ii) pooled-rule taker PnL as a function of the pool weight w."""
import numpy as np, pandas as pd
from scipy.optimize import minimize
from scipy import stats
from common import frame, boot, fmt

FEE = 0.05
P = pd.read_pickle("p0_gt36.pkl")
P = P[P.q.notna()].copy()
P["lq"] = np.log(P.q / (1 - P.q)); P["lp"] = np.log(P.p / (1 - P.p))
P["sp"] = (P.ask - P.bid)
P["w"] = 1.0 / P.groupby(["st", "d", "ord"])["y"].transform("size")

def logit_fit(X, y, w):
    def nll(b):
        z = X @ b
        return (w * (np.logaddexp(0, z) - y * z)).sum()
    def grad(b):
        z = X @ b
        return X.T @ (w * (1 / (1 + np.exp(-z)) - y))
    return minimize(nll, np.zeros(X.shape[1]), jac=grad, method="BFGS").x

def coef(S, B=300, seed=2):
    X = np.column_stack([np.ones(len(S)), S.lq.values, S.lp.values]); y = S.y.values.astype(float); w = S.w.values
    b = logit_fit(X, y, w)
    days = S.d.values; uniq = np.unique(days); rng = np.random.default_rng(seed)
    idx_by = {d: np.where(days == d)[0] for d in uniq}
    bs = []
    for _ in range(B):
        ii = np.concatenate([idx_by[d] for d in rng.choice(uniq, len(uniq))])
        bs.append(logit_fit(X[ii], y[ii], w[ii]))
    bs = np.array(bs); lo, hi = np.percentile(bs, [2.5, 97.5], axis=0)
    return (f"market coef {b[1]:+.2f} [{lo[1]:+.2f},{hi[1]:+.2f}]  model coef {b[2]:+.2f} [{lo[2]:+.2f},{hi[2]:+.2f}]"
            f"{'*' if lo[2] > 0 or hi[2] < 0 else ' '}  n={len(S)} days={len(uniq)}")

print("=== (i) Y ~ logit(market mid q) + logit(model p), bucket level, P0 >36h.  model coef > 0 = the model adds to the price ===")
tight = (P.sp <= 0.03)
for era in ("A:<09-12", "B:09-12..25", "C:09-26+"):
    E = P[P.era == era]
    print(f"{era:12s} all buckets      : {coef(E)}")
    print(f"{'':12s} two-sided, sp<=3c: {coef(E[E.sp <= 0.03])}")
    print(f"{'':12s} centre (|rungs|<=1): {coef(E[E.adist <= 1])}")
    print(f"{'':12s} tails  (|rungs|>=2): {coef(E[E.adist >= 2])}")
    print(f"{'':12s} tails, bid>=2c     : {coef(E[(E.adist >= 2) & (E.bid >= 0.02)])}")

print()
print("=== (ii) pooled-rule taker: r ~ q^(1-w) p^w; buy NO where r < bid - fee, YES where r > ask + fee.  realised pnl/share after fee ===")
P["lqn"] = np.log(P.q); P["lpn"] = np.log(P.p)
def pooled(P, w):
    z = np.exp((1 - w) * P.lqn.values + w * P.lpn.values)
    code, _ = pd.factorize(P.dec.values)
    return z / np.bincount(code, z)[code]
def tinterval(s):
    dm = s.groupby("d").apply(lambda g: np.average(g.pnl, weights=g.ww), include_groups=False)
    n = len(dm)
    if n < 3: return ""
    half = stats.t.ppf(0.975, n - 1) * dm.std(ddof=1) / np.sqrt(n)
    return f" day-mean t-CI [{dm.mean()-half:+.4f},{dm.mean()+half:+.4f}] pos days {int((dm>0).sum())}/{n}"
for w in (1.0, 0.7, 0.5, 0.3, 0.2, 0.1):
    r = pooled(P, w)
    T = P.assign(r=r)
    feeb = FEE * T.bid * (1 - T.bid); feea = FEE * T.ask * (1 - T.ask)
    no = T[T.bid.notna() & (T.r < T.bid - feeb)].copy(); no["pnl"] = no.bid - no.y - FEE * no.bid * (1 - no.bid); no["side"] = "NO"
    ye = T[T.ask.notna() & (T.r > T.ask + feea)].copy(); ye["pnl"] = ye.y - ye.ask - FEE * ye.ask * (1 - ye.ask); ye["side"] = "YES"
    L = pd.concat([no, ye]); L["ww"] = 1.0 / L.groupby(["st", "d", "ord", "side"])["pnl"].transform("size")
    for era in ("B:09-12..25", "C:09-26+"):
        for side in ("NO", "YES"):
            s = L[(L.era == era) & (L.side == side)]
            if s.ww.sum() < 30:
                print(f"w={w:.1f} {era:12s} {side:3s} legs={s.ww.sum():.0f} (too few)"); continue
            print(f"w={w:.1f} {era:12s} {side:3s} legs={s.ww.sum():5.0f} {fmt(boot(s.pnl, s.ww, s.d))}{tinterval(s) if era.startswith('C') else ''}")
