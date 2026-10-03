"""The whole Proper vector, BOTH sides, rested as a maker exactly as the lane prices a rung (own side's best bid + 1 tick,
   clamped under the ask; YES only under p, NO only under 1-p), sized |log(p/q)|, re-priced on every book while the rule
   holds, held to settlement once filled.  No slice is chosen: every bucket, both sides, every stage.
   Fill read off the next book of the same bucket:  through = the other side reaches my price;
   lifted = through, or the level I outbid (or joined) is gone / smaller on the next book (a cancel looks the same).
   Taker on the same legs (the paper's rule at the ask / bid, fee paid) for comparison."""
import numpy as np, pandas as pd
from common import frame

FEE = 0.05
def legs(T, col, windows):
    T = T[T.q.notna()].sort_values(["st", "d", "ord", "t"]).copy()
    g = T.groupby(["st", "d", "ord"], sort=False)
    for c in ("bid", "ask", "bsz", "asz"): T["n" + c] = g[c].shift(-1)
    T["has_next"] = g["t"].shift(-1).notna()
    p, q, y = T[col].values, T.q.values, T.y.values.astype(float)
    bid, ask, tick = T.bid.values, T.ask.values, T.tick.fillna(0.01).values
    nbid, nask, nbsz, nasz, bsz, asz, nx = T.nbid.values, T.nask.values, T.nbsz.values, T.nasz.values, T.bsz.values, T.asz.values, T.has_next.values
    s = np.log(p / q); two = np.isfinite(bid) & np.isfinite(ask) & (bid > 0)
    mid = np.where(two, (bid + ask) / 2, np.where(np.isfinite(ask), ask / 2, (np.nan_to_num(bid) + 1) / 2))
    T["mid"] = mid; T["mrank"] = T.groupby("dec")["mid"].rank(ascending=False, method="first")
    adist = (T.ord - T.dec.map(T[T.mrank == 1].set_index("dec")["ord"])).abs().values
    eps = 1e-9
    with np.errstate(invalid="ignore"):
        # YES rung
        py = np.floor(np.minimum(bid + tick, ask - tick) / tick + 1e-6) * tick
        live_y = two & (s > 0) & (py > eps) & (py < p - eps)
        imp_y = py > bid + eps
        thr_y = live_y & nx & np.isfinite(nask) & (nask <= py + eps)
        gone_b = nx & (~np.isfinite(nbid) | (nbid < bid - eps)); less_b = nx & np.isfinite(nbid) & (np.abs(nbid - bid) < eps) & (nbsz < bsz - eps)
        lif_y = thr_y | (live_y & gone_b) | (live_y & imp_y & less_b)
        # NO rung, in YES terms: an offer at m
        m = np.ceil(np.maximum(ask - tick, bid + tick) / tick - 1e-6) * tick
        live_n = two & (s < 0) & (m < 1 - eps) & (p < m - eps)
        imp_n = m < ask - eps
        thr_n = live_n & nx & np.isfinite(nbid) & (nbid >= m - eps)
        gone_a = nx & (~np.isfinite(nask) | (nask > ask + eps)); less_a = nx & np.isfinite(nask) & (np.abs(nask - ask) < eps) & (nasz < asz - eps)
        lif_n = thr_n | (live_n & gone_a) | (live_n & imp_n & less_a)
        # taker on the paper's rule
        tk_y = np.isfinite(ask) & (p > ask) & (s > 0); tk_n = np.isfinite(bid) & (bid > 0) & (p < bid) & (s < 0)
    sh = np.abs(s)
    base = T[["st", "d", "ord", "t", "stage", "h"]].copy(); base["adist"] = adist
    out = []
    for side, live, thr, lif, pnl, cost, tk, tpnl, tcost in (
        ("YES", live_y, thr_y, lif_y, sh * (y - py), sh * py, tk_y, sh * (y - ask - FEE * ask * (1 - ask)), sh * (ask + FEE * ask * (1 - ask))),
        ("NO", live_n, thr_n, lif_n, sh * (m - y), sh * (1 - m), tk_n, sh * (bid - y - FEE * bid * (1 - bid)), sh * (1 - bid + FEE * bid * (1 - bid)))):
        B = base.copy(); B["side"] = side; B["live"] = live; B["thr"] = thr; B["lif"] = lif; B["pnl"] = pnl; B["cost"] = cost
        B["tk"] = tk; B["tpnl"] = tpnl; B["tcost"] = tcost; B["sh"] = sh; B["y"] = y
        out.append(B[live | tk])
    B = pd.concat(out)
    res = []
    key = ["st", "d", "ord", "side"]
    for wname, wmask in windows:
        W = B[wmask(B)]
        L = W[W.live]
        post = L.drop_duplicates(key).set_index(key)[["pnl", "cost", "adist", "sh", "y"]].add_prefix("post_")
        R = post.copy(); R["window"] = wname
        for rule in ("thr", "lif"):
            f = L[L[rule]].drop_duplicates(key).set_index(key)[["pnl", "cost", "sh"]].add_prefix(rule + "_")
            R = R.join(f)
        tk = W[W.tk].drop_duplicates(key).set_index(key)[["tpnl", "tcost"]]
        R = R.join(tk, how="outer"); R["window"] = wname
        res.append(R.reset_index())
    return pd.concat(res)

def roi(S, num, den):
    S = S[S[num].notna()]
    if len(S) < 40: return f"   n={len(S):4d}"
    codes, uniq = pd.factorize(S.d.values); k = len(uniq)
    sn = np.bincount(codes, S[num], k); sd = np.bincount(codes, S[den], k)
    idx = np.random.default_rng(5).integers(0, k, size=(3000, k)); r = sn[idx].sum(1) / sd[idx].sum(1); lo, hi = np.percentile(r, [2.5, 97.5])
    return f"{100*sn.sum()/sd.sum():+5.1f}% [{100*lo:+5.1f},{100*hi:+5.1f}]{'*' if lo>0 or hi<0 else ' '}"

def show(label, R):
    print(f"##### {label} #####")
    print(f"{'window':10s} {'legs':22s} {'posted':>6s} | {'all first posts fill':24s} | {'through: filled, ROI':32s} | {'lifted: filled, ROI':32s} | taker on the rule (fee paid)")
    for wname, W in R.groupby("window", sort=False):
        for name, S in (("both sides, every bucket", W), ("  YES legs", W[W.side == "YES"]), ("  NO legs", W[W.side == "NO"]),
                        ("  YES at market mode", W[(W.side == "YES") & (W.post_adist == 0)]), ("  YES 1 rung", W[(W.side == "YES") & (W.post_adist == 1)]), ("  YES 2+ rungs", W[(W.side == "YES") & (W.post_adist >= 2)]),
                        ("  NO at market mode", W[(W.side == "NO") & (W.post_adist == 0)]), ("  NO 1 rung", W[(W.side == "NO") & (W.post_adist == 1)]), ("  NO 2+ rungs", W[(W.side == "NO") & (W.post_adist >= 2)])):
            P = S[S.post_cost.notna()]
            if len(P) < 40: continue
            ft = P.thr_cost.notna().mean(); fl = P.lif_cost.notna().mean()
            print(f"{wname:10s} {name:22s} {len(P):6d} | {roi(P, 'post_pnl', 'post_cost'):24s} | {ft:4.0%} {roi(P, 'thr_pnl', 'thr_cost'):26s} | {fl:4.0%} {roi(P, 'lif_pnl', 'lif_cost'):26s} | {roi(S, 'tpnl', 'tcost')} (n={int(S.tpnl.notna().sum())})")
    print()

WIN = [("P0 >36h", lambda B: (B.stage == "P0") & (B.h > 36)), ("P0-M", lambda B: B.stage == "P0-M"), ("P1 >3h", lambda B: (B.stage == "P1") & (B.h > 3)), ("all >3h", lambda B: B.h > 3)]
F = frame()
R1 = legs(F[F.d >= "2026-09-12"], "p", WIN); show("SERVED 09-12+, 45 stations", R1)
R2 = legs(F[F.d >= "2026-09-26"], "p", WIN); show("SERVED 09-26+ (v14-v20)", R2)
C = pd.read_pickle("replay_c.pkl"); C["stage"] = "P0"
R3 = legs(C, "rp", WIN[:1]); show("REPLAY current chain, 34 C stations, 38 days (P0 vectors only)", R3)
R1.to_pickle("maker_legs_served.pkl")

print("=== portfolios inside P0 >36h, both sides: share of posted cost, and ROI under each execution assumption ===")
def port(label, R):
    W = R[(R.window == "P0 >36h")]
    cells = {"whole vector (both sides, every bucket)": W.side.notna(),
             "YES at the market mode + NO 2+ rungs": ((W.side == "YES") & (W.post_adist == 0)) | ((W.side == "NO") & (W.post_adist >= 2)),
             "everything except YES 2+ rungs and NO at the mode": ~(((W.side == "YES") & (W.post_adist >= 2)) | ((W.side == "NO") & (W.post_adist == 0))),
             "YES 2+ rungs + NO at the mode (the other way)": ((W.side == "YES") & (W.post_adist >= 2)) | ((W.side == "NO") & (W.post_adist == 0))}
    tot = W.post_cost.sum()
    print(f"--- {label} ---")
    for name, m in cells.items():
        S = W[m & W.post_cost.notna()]; T = W[m]
        print(f"  {name:52s} cost share {S.post_cost.sum()/tot:4.0%} | all fill {roi(S,'post_pnl','post_cost')} | through {S.thr_cost.notna().mean():4.0%} {roi(S,'thr_pnl','thr_cost')} | lifted {S.lif_cost.notna().mean():4.0%} {roi(S,'lif_pnl','lif_cost')} | taker {roi(T,'tpnl','tcost')}")
    for side, a in (("YES", 0), ("YES", 1), ("YES", 2), ("NO", 0), ("NO", 1), ("NO", 2)):
        S = W[(W.side == side) & ((W.post_adist == a) if a < 2 else (W.post_adist >= 2)) & W.post_cost.notna()]
        if len(S) < 40: continue
        px = (S.post_cost / S.post_sh).mean()
        per = lambda c, k: 100 * (S[c] / S[c.split("_")[0] + "_sh"]).dropna().mean() if S[c].notna().sum() >= 30 else float('nan')
        print(f"     {side:3s} {'at mode' if a == 0 else ('1 rung' if a == 1 else '2+ rungs'):9s} cost share {S.post_cost.sum()/tot:4.0%}  price paid {px:.3f}  wins {(S.post_y if side=='YES' else 1-S.post_y).mean():.3f}  per share: all fill {per('post_pnl',0):+.2f}c  through {per('thr_pnl',0):+.2f}c  lifted {per('lif_pnl',0):+.2f}c")
port("SERVED 09-12+, 45 stations", R1); port("SERVED 09-26+", R2); port("REPLAY current chain, C stations", R3)
