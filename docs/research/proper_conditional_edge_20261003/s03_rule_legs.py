"""T4: the Proper rule's own triggers at executable prices (taker both sides; maker bounds)."""
import sys
import numpy as np, pandas as pd
from common import frame, boot, fmt, HBL

FEE = 0.05
df = frame()
mid = np.where(df.bid.notna() & df.ask.notna(), (df.bid + df.ask) / 2,
               np.where(df.ask.notna(), df.ask / 2, np.where(df.bid.notna(), (df.bid + 1) / 2, np.nan)))
df["mid"] = mid
df["mode_ord"] = df.assign(m=df.mid.fillna(-1)).groupby("dec")["m"].transform(lambda s: s.values.argmax())
# ordinal within the decision is 0..nb-1 in book order; map argmax position -> ordinal
first_ord = df.groupby("dec")["ord"].transform("min")
df["dist"] = df.ord - (df.mode_ord + first_ord)
df["tick"] = df.tick.fillna(0.01)

def legs(d):
    out = []
    # taker NO: hit the YES bid
    t = d[d.bid.notna() & (d.p < d.bid)].copy()
    t["side"] = "NO"; t["mode"] = "taker"; t["yes_px"] = t.bid
    t["cost"] = (1 - t.bid) + FEE * t.bid * (1 - t.bid)
    t["pnl"] = (1 - t.y) - t.cost; t["claim"] = t.bid - t.p
    out.append(t)
    t = d[d.ask.notna() & (d.p > d.ask)].copy()
    t["side"] = "YES"; t["mode"] = "taker"; t["yes_px"] = t.ask
    t["cost"] = t.ask + FEE * t.ask * (1 - t.ask)
    t["pnl"] = t.y - t.cost; t["claim"] = t.p - t.ask
    out.append(t)
    # maker NO at NO bid + tick  == selling YES at ask - tick (needs both sides; clamp under NO ask)
    b = d[d.bid.notna() & d.ask.notna()].copy()
    m = b.copy(); m["yes_px"] = np.maximum(m.ask - m.tick, m.bid + m.tick)
    m = m[(m.yes_px < m.ask + 1e-12) & (m.yes_px > m.bid - 1e-12) & (m.p < m.yes_px - 1e-12)]
    m["side"] = "NO"; m["mode"] = "maker"; m["cost"] = 1 - m.yes_px
    m["pnl"] = (1 - m.y) - m.cost; m["claim"] = m.yes_px - m.p
    out.append(m)
    m = b.copy(); m["yes_px"] = np.minimum(m.bid + m.tick, m.ask - m.tick)
    m = m[(m.yes_px > m.bid - 1e-12) & (m.p > m.yes_px + 1e-12)]
    m["side"] = "YES"; m["mode"] = "maker"; m["cost"] = m.yes_px
    m["pnl"] = m.y - m.cost; m["claim"] = m.p - m.yes_px
    out.append(m)
    return pd.concat(out)

def report(L, label, keys=()):
    # each station-day x bucket x side counted once per group: weight 1/(decisions carrying it)
    L = L.copy()
    L["w"] = 1.0 / L.groupby(["st", "d", "ord", "side", "mode", *keys], observed=True)["pnl"].transform("size")
    rows = []
    for key, s in L.groupby(["mode", "side", *keys], observed=True):
        if s.w.sum() < 25:
            continue
        per_share = boot(s.pnl, s.w, s.d)
        roi = boot(s.pnl / 1.0, s.w, s.d)  # placeholder
        # ROI on capital with equal shares: sum pnl / sum cost
        codes, uniq = pd.factorize(s.d.values); k = len(uniq)
        sp = np.bincount(codes, s.w * s.pnl, k); sc = np.bincount(codes, s.w * s.cost, k)
        idx = np.random.default_rng(3).integers(0, k, size=(2000, k))
        r = sp[idx].sum(1) / sc[idx].sum(1); lo, hi = np.percentile(r, [2.5, 97.5])
        claim = np.average(s.claim, weights=s.w); hit = np.average(s.y, weights=s.w); px = np.average(s.yes_px, weights=s.w)
        rows.append((*key, round(s.w.sum()), k, f"{px:.3f}", f"{hit:.3f}", f"{claim:+.3f}", fmt(per_share).split(" (")[0],
                     f"{sp.sum()/sc.sum():+.3f} [{lo:+.3f},{hi:+.3f}]{'*' if lo>0 or hi<0 else ' '}"))
    cols = ["mode", "side", *keys, "legs", "days", "yes_px", "hit", "claimed/sh", "realised/sh [95%CI]", "ROI [95%CI]"]
    print(f"--- {label} ---")
    print(pd.DataFrame(rows, columns=cols).to_string(index=False))
    print()

P = df[(df.stage == "P0") & (df.h > 36)]
L = legs(P)
L.to_pickle("legs_p0.pkl")
print("=== T4a P0, >36h (the Proper window), every decision, each station-day-bucket-side once ===")
report(L, "all eras")
report(L, "by era", keys=("era",))
print("=== T4b P0 first decision only (the instant the lane plans) ===")
report(L[L["first"] == 1], "first decision, all eras")
report(L[L["first"] == 1], "first decision by era", keys=("era",))
