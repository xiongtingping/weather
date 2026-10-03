"""Conditional slices: where (if anywhere) does the rule's taker trade realise, and where is the mid-slope positive?"""
import numpy as np, pandas as pd
from common import frame, boot, fmt

FEE = 0.05
df = frame()
dec = pd.read_pickle("dec_scores.pkl")
P = df[(df.stage == "P0") & (df.h > 36)].copy()
mid = np.where(P.bid.notna() & P.ask.notna(), (P.bid + P.ask) / 2, np.where(P.ask.notna(), P.ask / 2, np.where(P.bid.notna(), (P.bid + 1) / 2, 0.0)))
P["mid"] = mid
P["mrank"] = P.groupby("dec")["mid"].rank(ascending=False, method="first")
mode_ord = P[P.mrank == 1].set_index("dec")["ord"]
P["dist"] = P.ord - P.dec.map(mode_ord)
P["adist"] = P.dist.abs().clip(upper=4)
P["gap"] = P.dec.map(dec.gap)          # model mean ordinal - market mean ordinal (NaN where q incomplete)
P["agap"] = pd.cut(P.gap.abs(), [-1, 0.25, 0.5, 1.0, 9], labels=["<=0.25", "0.25-0.5", "0.5-1", ">1"])
P["nsb"] = pd.cut(P.nsys, [0, 2, 4, 9], labels=["2", "3-4", "5-6"])
P["hour_utc"] = P.t.str.slice(11, 13).astype(int)
P["sp"] = P.ask - P.bid

no = P[P.bid.notna() & (P.p < P.bid)].copy(); no["side"] = "NO"; no["px"] = no.bid
no["pnl"] = no.bid - no.y - FEE * no.bid * (1 - no.bid); no["cost"] = 1 - no.bid; no["claim"] = no.bid - no.p
ye = P[P.ask.notna() & (P.p > P.ask)].copy(); ye["side"] = "YES"; ye["px"] = ye.ask
ye["pnl"] = ye.y - ye.ask - FEE * ye.ask * (1 - ye.ask); ye["cost"] = ye.ask; ye["claim"] = ye.p - ye.ask
L = pd.concat([no, ye])
L["pxb"] = pd.cut(L.px, [0, .03, .08, .2, .4, 1], labels=["<=3c", "3-8c", "8-20c", "20-40c", ">40c"])
L["clb"] = pd.cut(L.claim, [0, .02, .05, .1, 2], labels=["<=2c", "2-5c", "5-10c", ">10c"])
L["dirn"] = np.where(L.dist < 0, "below mode", np.where(L.dist > 0, "above mode", "at mode"))

def show(L, key, eras=("B+C", "C")):
    print(f"--- taker fires, by {key} ---  realised pnl/share after fee [95% CI by event-date bootstrap]")
    for side in ("NO", "YES"):
        for val in sorted(L[key].dropna().unique(), key=str):
            line = f"{side:3s} {key}={str(val):10s}"
            for era in eras:
                s = L[(L.side == side) & (L[key] == val)]
                s = s[s.era.isin(["B:09-12..25", "C:09-26+"])] if era == "B+C" else s[s.era == "C:09-26+"]
                if len(s) == 0: line += f" | {era}: -"; continue
                w = 1.0 / s.groupby(["st", "d", "ord"])["pnl"].transform("size")
                if w.sum() < 30: line += f" | {era}: n={w.sum():.0f} (too few)"; continue
                line += f" | {era}: n={w.sum():5.0f} px={np.average(s.px, weights=w):.3f} {fmt(boot(s.pnl, w, s.d)).split(' (')[0]}"
            print(line)
    print()

for key in ("hb", "adist", "dirn", "pxb", "clb", "unit", "nsb", "agap"):
    show(L, key)

# station level, era B+C, both sides pooled, ROI on cost
print("--- taker fires by station (era B+C, both sides): legs, pnl/share, days positive ---")
S = L[L.era.isin(["B:09-12..25", "C:09-26+"])].copy()
S["w"] = 1.0 / S.groupby(["st", "d", "ord", "side"])["pnl"].transform("size")
rows = []
for st, s in S.groupby("st"):
    est, lo, hi, k = boot(s.pnl, s.w, s.d, B=1000)
    half = s.d <= "2026-09-22"
    a = np.average(s.pnl[half], weights=s.w[half]) if half.any() else np.nan
    b = np.average(s.pnl[~half], weights=s.w[~half]) if (~half).any() else np.nan
    rows.append((st, round(s.w.sum()), est, lo, hi, a, b))
R = pd.DataFrame(rows, columns=["st", "legs", "pnl", "lo", "hi", "first_half", "second_half"]).sort_values("pnl", ascending=False)
print(R.round(4).to_string(index=False))
print("stations with CI above zero:", list(R[R.lo > 0].st), " | both halves positive:", list(R[(R.first_half > 0) & (R.second_half > 0)].st))
print("rank correlation of station pnl, first half vs second half:", round(R[["first_half", "second_half"]].corr(method="spearman").iloc[0, 1], 3))
