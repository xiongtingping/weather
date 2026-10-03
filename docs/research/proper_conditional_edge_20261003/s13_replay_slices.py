"""Conditional slices on the replayed current chain (C stations, P0 >36h, 38 days): where does the disagreement pay as a taker?"""
import numpy as np, pandas as pd
from scipy import stats
from scipy.optimize import minimize_scalar
from common import boot, fmt, frame

FEE = 0.05
C = pd.read_pickle("replay_c.pkl")
C = C[C.h > 36].copy()
C["era"] = np.where(C.d >= "2026-09-26", "C", np.where(C.d >= "2026-09-12", "B", "A"))
F = frame()
Lq = F[(F.h <= 12) & F.q.notna()].sort_values("t"); first = Lq.groupby(["st", "d"])["dec"].first()
C = C.join(Lq[Lq.dec.isin(first.values)].set_index(["st", "d", "ord"])["q"].rename("q12"), on=["st", "d", "ord"])
mid = np.where(C.bid.notna() & C.ask.notna(), (C.bid + C.ask) / 2, np.where(C.ask.notna(), C.ask / 2, np.where(C.bid.notna(), (C.bid + 1) / 2, 0.0)))
C["mid"] = mid
C["mrank"] = C.groupby("dec")["mid"].rank(ascending=False, method="first")
C["dist"] = C.ord - C.dec.map(C[C.mrank == 1].set_index("dec")["ord"])
C["adist"] = C.dist.abs().clip(upper=4)
C["hb"] = pd.cut(C.h, [36, 48, 60, 200], labels=["36-48", "48-60", ">60"])
C["half"] = np.where(C.d <= "2026-09-17", "H1(<=09-17)", "H2(>09-17)")

def tci(s, col="truth"):
    s = s[s[col].notna()]
    if len(s) == 0 or s.w.sum() < 40: return f"n={s.w.sum() if len(s) else 0:.0f}"
    dm = s.groupby("d").apply(lambda g: np.average(g[col], weights=g.w), include_groups=False)
    half = stats.t.ppf(0.975, len(dm) - 1) * dm.std(ddof=1) / np.sqrt(len(dm))
    est, lo, hi, k = boot(s[col], s.w, s.d)
    return f"n={s.w.sum():5.0f} px={np.average(s.px, weights=s.w):.3f} {est:+.4f} [{lo:+.4f},{hi:+.4f}]{'*' if lo>0 or hi<0 else ' '} t[{dm.mean()-half:+.4f},{dm.mean()+half:+.4f}] +d {int((dm>0).sum())}/{len(dm)}"

def legs(T, col, margin_fee=False):
    no = T[T.bid.notna()].copy(); fee = FEE * no.bid * (1 - no.bid)
    no = no[no[col] < no.bid - (fee if margin_fee else 0)].copy(); fee = FEE * no.bid * (1 - no.bid)
    no["side"] = "NO"; no["truth"] = no.bid - no.y - fee; no["mark"] = no.bid - no.q12 - fee; no["px"] = no.bid; no["claim"] = no.bid - no[col]
    ye = T[T.ask.notna()].copy(); fee = FEE * ye.ask * (1 - ye.ask)
    ye = ye[ye[col] > ye.ask + (fee if margin_fee else 0)].copy(); fee = FEE * ye.ask * (1 - ye.ask)
    ye["side"] = "YES"; ye["truth"] = ye.y - ye.ask - fee; ye["mark"] = ye.q12 - ye.ask - fee; ye["px"] = ye.ask; ye["claim"] = ye[col] - ye.ask
    L = pd.concat([no, ye]); L["w"] = 1.0 / L.groupby(["st", "d", "ord", "side"])["px"].transform("size")
    return L

L = legs(C, "rp")
L["clb"] = pd.cut(L.claim, [0, .02, .05, .1, 2], labels=["<=2c", "2-5c", "5-10c", ">10c"])
L["pxb"] = pd.cut(L.px, [0, .03, .08, .2, .4, 1], labels=["<=3c", "3-8c", "8-20c", "20-40c", ">40c"])
print("=== replayed current chain, raw rule (p vs bid/ask), taker pnl/share after fee, truth [boot CI] t-CI, positive days ===")
for key in ("hb", "adist", "clb", "pxb", "half", "era"):
    print(f"--- by {key} ---")
    for side in ("NO", "YES"):
        for val in sorted(L[key].dropna().unique(), key=str):
            s = L[(L.side == side) & (L[key] == val)]
            print(f"{side:3s} {key}={str(val):12s} truth {tci(s)}")
            if key in ("adist", "clb"):
                print(f"{'':20s} mark@12h {tci(s, 'mark')}")

# pooled rule
Q = C[C.q.notna()].copy(); Q["lq"] = np.log(Q.q); Q["lr"] = np.log(Q.rp)
def pooled(Q, w):
    z = np.exp((1 - w) * Q.lq.values + w * Q.lr.values); code, _ = pd.factorize(Q.dec.values)
    return z / np.bincount(code, z)[code]
print()
print("=== pooled rule r ~ q^(1-w) p^w with a fee margin, taker; fixed w ===")
for w in (1.0, 0.5, 0.35, 0.25, 0.15):
    T = Q.assign(r=pooled(Q, w)); Lp = legs(T, "r", margin_fee=True)
    for side in ("NO", "YES"):
        s = Lp[Lp.side == side]
        print(f"w={w:.2f} {side:3s} truth {tci(s)}")
        print(f"{'':10s} mark@12h {tci(s, 'mark')}")
        for half in ("H1(<=09-17)", "H2(>09-17)"):
            print(f"{'':10s} {half} truth {tci(s[s.half == half])}")
# walk-forward w
def ll(w, lq, lp, code, ymask):
    z = (1 - w) * lq + w * lp
    return -((z[ymask] - np.log(np.bincount(code, np.exp(z))))).sum()
days = sorted(Q.d.unique()); parts = []; wlog = []
for D in days:
    cut = (pd.Timestamp(D) - pd.Timedelta(days=3)).strftime("%Y-%m-%d")
    tr = Q[Q.d <= cut]
    if tr.d.nunique() < 7: continue
    code, _ = pd.factorize(tr.dec.values)
    w = minimize_scalar(ll, bounds=(0, 1), args=(tr.lq.values, tr.lr.values, code, tr.y.values == 1), method="bounded").x
    te = Q[Q.d == D]; parts.append(te.assign(r=pooled(te, w))); wlog.append((D, w))
T = pd.concat(parts); Lw = legs(T, "r", margin_fee=True)
print()
print("=== pooled rule, w fitted WALK-FORWARD on event dates <= D-3 (min 7 days) ===   fitted w path:", " ".join(f"{d[5:]}:{w:.2f}" for d, w in wlog[::3]))
for side in ("NO", "YES"):
    s = Lw[Lw.side == side]
    print(f"{side:3s} truth {tci(s)}")
    print(f"{'':3s} mark@12h {tci(s, 'mark')}")
