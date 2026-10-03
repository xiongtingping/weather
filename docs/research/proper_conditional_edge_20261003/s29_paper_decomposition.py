"""arXiv 2607.06166 on our data: profit = score gap + Bregman divergence - liquidity loss.
   (a) frictionless at the normalised mid (Theorem 1 / Lemma 1), Log and Brier;
   (b) bid-ask-aware proper bet (Appendix C.1: each bucket a binary, reference = the price actually paid), with the fee;
   (c) the lane's own sizing |log(p/q_mid)| on the legs its rule allows;
   (d) Corollary 5's momentum condition: does p predict the market's NEXT mid?
   P0, more than 36 hours out, ladders the market reference accepts."""
import numpy as np, pandas as pd
from common import frame

FEE = 0.05
def per_decision(T, col):
    T = T[(T.stage == "P0") & (T.h > 36) & T.q.notna()].copy() if "stage" in T else T[(T.h > 36) & T.q.notna()].copy()
    p, q, y = T[col].values, T.q.values, T.y.values.astype(float)
    T["p_"] = p
    # (a) frictionless multiclass
    sl = np.log(p / q); sb = p - q
    T["l_pi"] = sl * (y - q); T["l_D"] = q * np.log(q / p); T["l_cost"] = np.where(sl > 0, sl * q, -sl * (1 - q))
    T["l_dS"] = np.where(y == 1, np.log(p / q), 0.0)
    T["b_pi"] = sb * (y - q); T["b_D"] = 0.5 * (p - q) ** 2; T["b_cost"] = np.where(sb > 0, sb * q, -sb * (1 - q))
    # (b) bid-ask aware, binary rules, reference = executable price
    ask, bid = T.ask.values, T.bid.values
    yes = np.isfinite(ask) & (p > ask); no = np.isfinite(bid) & (p < bid) & ~yes
    ref = np.where(yes, ask, np.where(no, bid, np.nan))
    lg = lambda x: np.log(x / (1 - x))
    with np.errstate(invalid="ignore", divide="ignore"):
        s_log = np.where(yes | no, lg(np.clip(p, 1e-6, 1 - 1e-6)) - lg(np.clip(ref, 1e-6, 1 - 1e-6)), 0.0)   # + YES shares, - NO shares
        s_bri = np.where(yes | no, p - ref, 0.0)
        s_sys = np.where((yes & (sl > 0)) | (no & (sl < 0)), sl, 0.0)
        fee_sh = FEE * ref * (1 - ref)
        px = np.where(yes, ask, np.where(no, 1 - bid, 0.0))
        for name, s in (("xl", s_log), ("xb", s_bri), ("xs", s_sys)):
            T[name + "_pi"] = np.where(yes | no, s * (y - ref), 0.0)
            T[name + "_fee"] = np.where(yes | no, np.abs(s) * fee_sh, 0.0)
            T[name + "_cost"] = np.abs(s) * px
            T[name + "_yes"] = np.where(yes, 1.0, 0.0) * (np.abs(s) > 0); T[name + "_no"] = np.where(no, 1.0, 0.0) * (np.abs(s) > 0)
        pc = np.clip(p, 1e-6, 1 - 1e-6); rc = np.clip(np.nan_to_num(ref, nan=0.5), 1e-6, 1 - 1e-6)
        T["xl_D"] = np.where(yes | no, rc * np.log(rc / pc) + (1 - rc) * np.log((1 - rc) / (1 - pc)), 0.0)
        T["xl_dS"] = np.where(yes | no, (y * np.log(pc) + (1 - y) * np.log(1 - pc)) - (y * np.log(rc) + (1 - y) * np.log(1 - rc)), 0.0)
    T["side_yes"] = yes; T["side_no"] = no
    cols = [c for c in T.columns if c[:2] in ("l_", "b_", "xl", "xb", "xs") and c not in ("bsz",)]
    g = T.groupby("dec")
    D = g[cols].sum()
    for c in ("st", "d", "t", "first", "h"): D[c] = g[c].first()
    return D, T

def roi(D, num, den, w, B=3000, seed=5):
    codes, uniq = pd.factorize(D.d.values); k = len(uniq)
    sn = np.bincount(codes, w * D[num].values if isinstance(num, str) else w * num, k); sd = np.bincount(codes, w * D[den].values, k)
    idx = np.random.default_rng(seed).integers(0, k, size=(B, k)); r = sn[idx].sum(1) / sd[idx].sum(1); lo, hi = np.percentile(r, [2.5, 97.5])
    return f"{100*sn.sum()/sd.sum():+6.1f}% [{100*lo:+5.1f},{100*hi:+5.1f}]{'*' if lo>0 or hi<0 else ' '}"

def report(label, T, col):
    D, B = per_decision(T, col)
    for pop, S in (("every P0 decision >36h (each station-day once)", D), ("the P0 first decision only", D[D["first"] == 1])):
        w = (1.0 / S.groupby(["st", "d"])["h"].transform("size")).values
        n_sd = S.groupby(["st", "d"]).ngroups
        ds = np.average(S.l_dS, weights=w); dd = np.average(S.l_D, weights=w)
        print(f"--- {label} | {pop}: {n_sd} station-days, {S.d.nunique()} event days ---")
        print(f"  (a) at the normalised mid, no friction, per unit lambda:  Log   score gap {ds:+.3f} + divergence D {dd:+.3f} = {ds+dd:+.3f}   ROI on cost {roi(S, 'l_pi', 'l_cost', w)}")
        bs = np.average(S.b_pi - S.b_D, weights=w); bd = np.average(S.b_D, weights=w)
        print(f"{'':60s}Brier score gap {bs:+.4f} + divergence D {bd:+.4f} = {bs+bd:+.4f} ROI on cost {roi(S, 'b_pi', 'b_cost', w)}")
        for name, tag in (("xl", "paper's bid-ask-aware Log bet (binary, reference = price paid)"), ("xb", "paper's bid-ask-aware Brier bet"), ("xs", "the lane's sizing |log(p/q_mid)| on legs its rule allows")):
            net = S[name + "_pi"].values - S[name + "_fee"].values
            print(f"  (b) {tag:62s}: before fee {roi(S, name + '_pi', name + '_cost', w)}   after fee {roi(S, net, name + '_cost', w)}")
        xs, xd = np.average(S.xl_dS, weights=w), np.average(S.xl_D, weights=w)
        print(f"      Log bet's own decomposition against the price paid: score gap {xs:+.3f} + D {xd:+.3f} = {xs+xd:+.3f} per unit lambda (cost {np.average(S.xl_cost, weights=w):.3f}, fee {np.average(S.xl_fee, weights=w):.3f})")
    # NO legs and YES legs apart, bid-ask-aware Log bet, every decision
    Bx = B.copy(); Bx["w"] = Bx.dec.map(1.0 / D.groupby(["st", "d"])["h"].transform("size"))
    for side, m in (("YES legs", Bx.side_yes), ("NO legs", Bx.side_no)):
        S = Bx[m & (Bx.xl_cost > 0)]
        net = S.xl_pi.values - S.xl_fee.values
        print(f"  (b) bid-ask-aware Log bet, {side}: after fee {roi(S, net, 'xl_cost', S.w.values)}   share of cost {S.eval('w * xl_cost').sum() / Bx.eval('w * xl_cost').sum():.0%}")
    return D, B

F = frame()
D1, B1 = report("SERVED 09-12+, 45 stations", F[F.d >= "2026-09-12"], "p")
D2, B2 = report("SERVED 09-26+ (v14-v20)", F[F.d >= "2026-09-26"], "p")
C = pd.read_pickle("replay_c.pkl"); C["stage"] = "P0"
D3, B3 = report("REPLAY current chain, 34 C stations, 38 days", C, "rp")

print()
print("=== (d) Corollary 5, the momentum condition: the Log position x_t = log(p_t/q_t) marked to the NEXT mid q_{t+1} (no friction) ===")
def momentum(label, T, col):
    T = T[(T.h > 36) & T.q.notna()].sort_values(["st", "d", "ord", "t"]).copy()
    g = T.groupby(["st", "d", "ord"], sort=False)
    T["qn"] = g["q"].shift(-1); T["tn"] = g["t"].shift(-1)
    T = T[T.qn.notna()].copy()
    # both ladders must be the same decision pair: keep decisions whose every bucket has a next mid
    full = T.groupby("dec")["qn"].transform("size") == T.groupby("dec")["nb"].transform("first") if "nb" in T else True
    T = T[full] if not isinstance(full, bool) else T
    p, q, qn = T[col].values, T.q.values, T.qn.values
    T["mtm"] = np.log(p / q) * (qn - q); T["D"] = q * np.log(q / p); T["cost"] = np.where(p > q, np.log(p / q) * q, -np.log(p / q) * (1 - q))
    S = T.groupby("dec").agg(st=("st", "first"), d=("d", "first"), mtm=("mtm", "sum"), D=("D", "sum"), cost=("cost", "sum"))
    w = (1.0 / S.groupby(["st", "d"])["mtm"].transform("size")).values
    m, dd = np.average(S.mtm, weights=w), np.average(S.D, weights=w)
    print(f"{label:46s} steps {len(S):5d}: next-step score gap {m-dd:+.4f} + D {dd:+.4f} = {m:+.4f} per unit lambda per step;  ROI on cost per step {roi(S, 'mtm', 'cost', w)}")
momentum("SERVED 09-12+, 45 stations", F[(F.stage == "P0") & (F.d >= "2026-09-12")], "p")
momentum("SERVED 09-26+", F[(F.stage == "P0") & (F.d >= "2026-09-26")], "p")
momentum("REPLAY current chain, C stations", C, "rp")
