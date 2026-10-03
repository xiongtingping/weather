"""Tail-NO slice by unit / stage / lead (served p), and the Proper portfolio as sized (shares ~ |s|, NO only, taker)."""
import numpy as np, pandas as pd
from scipy import stats
from common import boot, fmt, frame

FEE = 0.05
F = frame()
F = F[F.d >= "2026-09-12"].copy()
mid = np.where(F.bid.notna() & F.ask.notna(), (F.bid + F.ask) / 2, np.where(F.ask.notna(), F.ask / 2, np.where(F.bid.notna(), (F.bid + 1) / 2, 0.0)))
F["mid"] = mid; F["mrank"] = F.groupby("dec")["mid"].rank(ascending=False, method="first")
F["adist"] = (F.ord - F.dec.map(F[F.mrank == 1].set_index("dec")["ord"])).abs()

def tci(s, col="truth", keys=("st", "d", "ord")):
    if len(s) == 0: return "n=0"
    w = 1.0 / s.groupby(list(keys))["px"].transform("size")
    if w.sum() < 40: return f"n={w.sum():.0f}"
    dm = pd.DataFrame({"d": s.d, "x": s[col] * w, "w": w}).groupby("d").sum(); dm = dm.x / dm.w
    half = stats.t.ppf(0.975, len(dm) - 1) * dm.std(ddof=1) / np.sqrt(len(dm))
    est, lo, hi, k = boot(s[col], w, s.d)
    return f"n={w.sum():5.0f} bid={np.average(s.px, weights=w):.3f} hit={np.average(s.y, weights=w):.3f} {est:+.4f} [{lo:+.4f},{hi:+.4f}]{'*' if lo>0 or hi<0 else ' '} t[{dm.mean()-half:+.4f},{dm.mean()+half:+.4f}] +d {int((dm>0).sum())}/{len(dm)}"

N = F[F.bid.notna() & (F.adist >= 2) & (F.bid >= 0.02)].copy()
N["px"] = N.bid; N["truth"] = N.bid - N.y - FEE * N.bid * (1 - N.bid); N["fires"] = N.p < N.bid
print("=== tail NO (2+ rungs from market mode, bid >= 2c), served p, 09-12+ : by stage and lead ===")
for stage, hb in (("P0", ">60"), ("P0", "48-60"), ("P0", "36-48"), ("P0", "24-36"), ("P0-M", "36-48"), ("P0-M", "24-36"), ("P1", "12-24"), ("P1", "6-12"), ("P1", "0-6")):
    s = N[(N.stage == stage) & (N.hb == hb)]
    print(f"{stage:4s} {hb:6s} fires: {tci(s[s.fires])}")
    print(f"{'':11s} not  : {tci(s[~s.fires])}")
print()
P = N[(N.stage == "P0") & (N.h > 36)]
print("=== same slice, P0 >36h, by unit ===")
for u in ("C", "F"):
    s = P[P.unit == u]
    print(f"{u} fires: {tci(s[s.fires])}")
    print(f"{'':1s} not  : {tci(s[~s.fires])}")
print("=== by number of seated systems ===")
for lab, lo, hi in (("2", 0, 2), ("3-4", 3, 4), ("5-6", 5, 9)):
    s = P[(P.nsys >= lo) & (P.nsys <= hi)]
    print(f"nsys {lab:3s} fires: {tci(s[s.fires])}")

print()
print("=== the Proper portfolio as the lane sizes it: NO legs only, shares ~ |log(p/q)|, taker at the YES bid, P0 first decision >36h ===")
G = F[(F.stage == "P0") & (F.h > 36) & (F["first"] == 1) & F.q.notna() & F.bid.notna()].copy()
G = G[G.p < G.bid].copy()
G["s"] = np.abs(np.log(G.p / G.q)); G["cost"] = (1 - G.bid) + FEE * G.bid * (1 - G.bid); G["pnl"] = 1 - G.y - G.cost
def port(S, label):
    if len(S) == 0: print(label, "n=0"); return
    codes, uniq = pd.factorize(S.d.values); k = len(uniq)
    sp = np.bincount(codes, S.s * S.pnl, k); sc = np.bincount(codes, S.s * S.cost, k)
    idx = np.random.default_rng(1).integers(0, k, size=(4000, k)); r = sp[idx].sum(1) / sc[idx].sum(1)
    lo, hi = np.percentile(r, [2.5, 97.5])
    print(f"{label:44s} legs={len(S):5d} days={k:2d} ROI on cost {sp.sum()/sc.sum():+.4f} [{lo:+.4f},{hi:+.4f}]{'*' if lo>0 or hi<0 else ' '}  win days {int((sp>0).sum())}/{k}")
port(G, "all NO legs (rule as shipped)")
port(G[G.adist <= 1], "  NO legs at / next to the market mode")
port(G[G.adist >= 2], "  NO legs 2+ rungs out")
port(G[(G.adist >= 2) & (G.bid >= 0.02)], "  NO legs 2+ rungs out, bid >= 2c")
port(G[G.d >= "2026-09-26"], "all NO legs, 09-26+")
port(G[(G.d >= "2026-09-26") & (G.adist >= 2) & (G.bid >= 0.02)], "  2+ rungs out, bid >= 2c, 09-26+")
