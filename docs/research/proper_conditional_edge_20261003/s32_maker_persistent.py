"""NO legs rested as a maker (NO bid + 1 tick = YES offer at ask - tick), re-priced on every book while P0 and >36h and
   while the rule holds (p under the offer), until filled.  Two fill rules read off the next book:
     through  -- the YES bid reaches my offer (the market traded through it);
     lifted   -- the best YES ask of the book I undercut is gone or smaller on the next book (a buyer would have met my better offer first; a cancel looks the same).
   Compared with the taker entry (hit the YES bid at the first firing, fee paid)."""
import numpy as np, pandas as pd
from common import frame
FEE = 0.05
F = frame()
def prep(T, col):
    T = T[(T.h > 36)].copy()
    mid = np.where(T.bid.notna() & T.ask.notna(), (T.bid + T.ask) / 2, np.where(T.ask.notna(), T.ask / 2, np.where(T.bid.notna(), (T.bid + 1) / 2, 0.0)))
    T["mid"] = mid; T["mrank"] = T.groupby("dec")["mid"].rank(ascending=False, method="first")
    T["adist"] = (T.ord - T.dec.map(T[T.mrank == 1].set_index("dec")["ord"])).abs()
    T["pm"] = T[col]; T["tick"] = T.tick.fillna(0.01)
    T = T.sort_values(["st", "d", "ord", "t"])
    g = T.groupby(["st", "d", "ord"], sort=False)
    T["nbid"] = g["bid"].shift(-1); T["nask"] = g["ask"].shift(-1); T["nasz"] = g["asz"].shift(-1)
    return T
def simulate(T):
    out = []
    for (st, d, o), G in T.groupby(["st", "d", "ord"], sort=False):
        y = G.y.iloc[0]; adist0 = None; first_taker = None
        res = {"st": st, "d": d, "ord": o, "y": y, "posted": 0, "through": np.nan, "lifted": np.nan, "taker": np.nan, "taker_cost": np.nan}
        for r in G.itertuples():
            has = np.isfinite(r.bid) and np.isfinite(r.ask) and r.q == r.q
            if first_taker is None and np.isfinite(r.bid) and r.bid >= 0.02 and r.pm < r.bid and r.q == r.q:
                first_taker = r.bid - y - FEE * r.bid * (1 - r.bid); res["taker"] = first_taker; res["taker_cost"] = 1 - r.bid + FEE * r.bid * (1 - r.bid); res["adist_t"] = r.adist
            if not has: continue
            m = max(r.ask - r.tick, r.bid + r.tick)
            if not (m < r.ask + 1e-12) or not (r.pm < m - 1e-12) or m < 0.02: continue
            if res["posted"] == 0: res["adist"] = r.adist; res["m0"] = m; res["h0"] = r.h
            res["posted"] += 1
            if np.isnan(res["through"]) and np.isfinite(r.nbid) and r.nbid >= m - 1e-12:
                res["through"] = m - y; res["through_cost"] = 1 - m
            if np.isnan(res["lifted"]) and ((np.isfinite(r.nbid) and r.nbid >= m - 1e-12) or (np.isfinite(r.nask) and (r.nask > r.ask + 1e-12 or (abs(r.nask - r.ask) < 1e-12 and r.nasz < r.asz - 1e-9)))):
                res["lifted"] = m - y; res["lifted_cost"] = 1 - m
        last = G.iloc[-1]
        res["last_taker"] = (last.bid - y - FEE * last.bid * (1 - last.bid)) if (np.isfinite(last.bid) and last.bid >= 0.02 and last.pm < last.bid) else np.nan
        res["h_last"] = last.h
        if res["posted"] or first_taker is not None: out.append(res)
    return pd.DataFrame(out)
def line(x, d, cost=None):
    ok = x.notna()
    if ok.sum() < 30: return f"n={int(ok.sum())}"
    x, d = x[ok], d[ok]
    codes, uniq = pd.factorize(d.values); k = len(uniq)
    sx = np.bincount(codes, x, k); sn = np.bincount(codes, minlength=k).astype(float)
    idx = np.random.default_rng(3).integers(0, k, size=(3000, k)); b = sx[idx].sum(1) / sn[idx].sum(1); lo, hi = np.percentile(b, [2.5, 97.5])
    extra = f" ROI {x.sum()/cost[ok].sum():+.2%}" if cost is not None else ""
    return f"{100*x.mean():+.2f}c [{100*lo:+.2f},{100*hi:+.2f}]{'*' if lo>0 or hi<0 else ' '}{extra}"
C = pd.read_pickle("replay_c.pkl")
for label, T, col in (("SERVED 09-12+, 45 stations", F[(F.stage == "P0") & (F.d >= "2026-09-12")], "p"), ("REPLAY current chain, 34 C stations, 38 days", C, "rp")):
    S = simulate(prep(T, col))
    print(f"##### {label} #####")
    for name, m in (("NO legs 2+ rungs from the market mode", S.adist >= 2), ("NO legs at the mode or 1 rung", S.adist <= 1)):
        P = S[m & (S.posted > 0)]
        if len(P) < 50: continue
        n = len(P); ft, fl = P.through.notna().mean(), P.lifted.notna().mean()
        print(f"--- {name}: {n} legs posted (median first post {P.h0.median():.0f}h out, YES offer {P.m0.mean():.3f}) ---")
        print(f"   fill rule 'through': filled {ft:.0%} of legs; a filled share makes {line(P.through, P.d, P.through_cost)}; per posted leg {100*P.through.fillna(0).mean():+.2f}c")
        print(f"   fill rule 'lifted' : filled {fl:.0%} of legs; a filled share makes {line(P.lifted, P.d, P.lifted_cost)}; per posted leg {100*P.lifted.fillna(0).mean():+.2f}c")
        allfill = P.m0 - P.y
        print(f"   if every first post filled at once      : {line(allfill, P.d, 1 - P.m0)}")
        never = P[P.lifted.isna()]
        print(f"   legs never filled under 'lifted' ({len(never)}): would have made {100*(never.m0 - never.y).mean():+.2f}c at their first offer (hit rate {never.y.mean():.3f} vs filled {P[P.lifted.notna()].y.mean():.3f})")
    Tk = S[S.taker.notna() & (S.adist_t >= 2)]
    print(f"--- taker for comparison, 2+ rungs, first firing at a YES bid >= 2c: {len(Tk)} legs, {line(Tk.taker, Tk.d, Tk.taker_cost)} (every leg fills)")
    P = S[(S.adist >= 2) & (S.posted > 0)]
    for rule in ("through", "lifted"):
        combo = P[rule].where(P[rule].notna(), P.last_taker)
        took = P[rule].isna() & P.last_taker.notna()
        print(f"--- maker first ('{rule}'), then take at the window's last book if unfilled: positions on {combo.notna().mean():.0%} of legs ({P[rule].notna().mean():.0%} as maker + {took.mean():.0%} taken at ~{P.h_last[took].median():.0f}h); per position {line(combo, P.d)}; per posted leg {100*combo.fillna(0).mean():+.2f}c; the taken ones alone {line(P.last_taker[took], P.d[took])}")
    both = S[(S.adist >= 2) & (S.posted > 0) & S.taker.notna()]
    print(f"    legs available to both ways: {len(both)}; maker 'lifted' per posted leg {100*both.lifted.fillna(0).mean():+.2f}c vs taker {100*both.taker.mean():+.2f}c;  maker then taker at the window's end is not modelled")
    print()
