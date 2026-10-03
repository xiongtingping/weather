"""T6: mark the rule's taker legs to the LATER market (lower variance than the outcome; needs no truth)."""
import numpy as np, pandas as pd
from scipy import stats
from common import frame, boot, fmt
from load_open import build_open

FEE = 0.05
df = frame()
cols = ["dec", "st", "d", "stage", "t", "h", "first", "ver", "ord", "p", "q", "bid", "ask", "tick", "nsys"]
S = df[cols + ["y", "era"]].copy(); S["open"] = 0
O = build_open(); O["y"] = np.nan; O["era"] = "D:open(10-03+)"; O["open"] = 1
A = pd.concat([S, O], ignore_index=True)
print("open-day decisions by stage/version:")
od = O.drop_duplicates("dec")
print(od.groupby(["d", "stage"]).agg(n=("dec", "size"), vmin=("ver", "min"), vmax=("ver", "max"), hmin=("h", "min"), hmax=("h", "max")).round(1).to_string())

def later_q(A, hmax, label):
    """Per station-day-bucket: q at the first decision with h <= hmax (q complete)."""
    L = A[(A.h <= hmax) & A.q.notna()].sort_values("t")
    first = L.groupby(["st", "d"])["dec"].first()
    Lf = L[L.dec.isin(first.values)]
    return Lf.set_index(["st", "d", "ord"])["q"].rename(label), Lf.drop_duplicates(["st", "d"]).set_index(["st", "d"])["h"].rename(label + "_h")

def latest_q(A, label):
    L = A[A.q.notna()].sort_values("t")
    last = L.groupby(["st", "d"])["dec"].last()
    Lf = L[L.dec.isin(last.values)]
    return Lf.set_index(["st", "d", "ord"])["q"].rename(label), Lf.drop_duplicates(["st", "d"]).set_index(["st", "d"])["h"].rename(label + "_h")

q24, h24 = later_q(A, 24, "q24"); q12, h12 = later_q(A, 12, "q12"); qL, hL = latest_q(A, "qlast")
P = A[(A.stage == "P0") & (A.h > 36)].copy()
P = P.join(q24, on=["st", "d", "ord"]).join(q12, on=["st", "d", "ord"]).join(qL, on=["st", "d", "ord"]).join(hL, on=["st", "d"])
no = P[P.bid.notna() & (P.p < P.bid)].copy(); no["side"] = "NO"
fee = FEE * no.bid * (1 - no.bid)
for m in ("q24", "q12", "qlast"): no["mk_" + m] = no.bid - no[m] - fee
no["truth"] = no.bid - no.y - fee
ye = P[P.ask.notna() & (P.p > P.ask)].copy(); ye["side"] = "YES"
fee = FEE * ye.ask * (1 - ye.ask)
for m in ("q24", "q12", "qlast"): ye["mk_" + m] = ye[m] - ye.ask - fee
ye["truth"] = ye.y - ye.ask - fee
L = pd.concat([no, ye]); L["w"] = 1.0 / L.groupby(["st", "d", "ord", "side"])["p"].transform("size")
L["vg"] = pd.cut(L.ver, [0, 13, 18, 19, 20, 22, 24, 99], labels=["v<=13", "v14-18", "v19", "v20", "v21-22", "v23-24", "v25+"])

def tci(s, col):
    s = s[s[col].notna()]
    if s.w.sum() < 30: return f"n={s.w.sum():.0f}"
    dm = s.groupby("d").apply(lambda g: np.average(g[col], weights=g.w), include_groups=False)
    out = f"n={s.w.sum():5.0f} {fmt(boot(s[col], s.w, s.d)).split(' (')[0]}"
    if len(dm) >= 3:
        half = stats.t.ppf(0.975, len(dm) - 1) * dm.std(ddof=1) / np.sqrt(len(dm))
        out += f" t[{dm.mean()-half:+.4f},{dm.mean()+half:+.4f}] +days {int((dm > 0).sum())}/{len(dm)}"
    return out

print()
print("=== validation on settled days: the same legs marked to truth vs marked to the later market (pnl/share after fee) ===")
for era in ("A:<09-12", "B:09-12..25", "C:09-26+"):
    for side in ("NO", "YES"):
        s = L[(L.era == era) & (L.side == side)]
        print(f"{era:12s} {side:3s} truth      : {tci(s, 'truth')}")
        print(f"{'':12s} {'':3s} market@<=24h: {tci(s, 'mk_q24')}")
        print(f"{'':12s} {'':3s} market@<=12h: {tci(s, 'mk_q12')}")
print()
print("=== by model version, marked to the market at <=12h before settlement (settled + open days where it exists) ===")
for vg in ["v<=13", "v14-18", "v19", "v20", "v21-22", "v23-24", "v25+"]:
    for side in ("NO", "YES"):
        s = L[(L.vg == vg) & (L.side == side)]
        print(f"{vg:7s} {side:3s} mark@<=12h: {tci(s, 'mk_q12')}")
        print(f"{'':7s} {'':3s} mark@<=24h: {tci(s, 'mk_q24')}")
        if vg in ("v21-22", "v23-24", "v25+"):
            print(f"{'':7s} {'':3s} mark@latest book: {tci(s, 'mk_qlast')}  (median hours-to-settlement of the mark: {s.qlast_h.median():.0f})")
L.to_pickle("legs_marked.pkl")
