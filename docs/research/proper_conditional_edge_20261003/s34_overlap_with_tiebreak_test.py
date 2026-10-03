"""The Proper YES leg (charter 249: the market's modal bucket, p above the rung) split by whether the tiebreak lane's
   bucket test, moved into the same window (P0, >36h), names the same bucket: the model's modal bucket, market share
   >= 0.40, spread <= 3.1c, rung <= 0.60.  One leg a station-day, the bucket of the first decision the rule holds at.

   The rung is the lane's own (bid + 1 tick, clamped under the ask), re-posted on every later decision the rule still
   holds at, held to settlement once filled.  Fill read off the next book of the same bucket, as s33 reads it:
     through = the ask reaches my price;  lifted = through, or the bid level I outbid / joined is gone or smaller.
   Beside it the same legs taken at the ask of the plan decision, fee paid -- and the split the comparison needs:
   what the filled legs paid against the ask they could have taken, and what the filled and the unfilled legs each
   would have made as takers.  Served record (45 stations) and the current-chain replay (34 C stations)."""
import numpy as np, pandas as pd
from common import frame

FEE = 0.05
EPS = 1e-9


def prepare(T, col):
    T = T[(T.stage == "P0") & (T.h > 36) & T.q.notna()].copy()
    T["p_"] = T[col]
    T = T.sort_values(["st", "d", "ord", "t"])
    g = T.groupby(["st", "d", "ord"], sort=False)
    for c in ("bid", "ask", "bsz"):
        T["n" + c] = g[c].shift(-1)
    T["has_next"] = g["t"].shift(-1).notna()
    tick = T.tick.fillna(0.01)
    T["two"] = T.bid.notna() & T.ask.notna() & (T.bid > 0)
    T["rung"] = np.floor(np.minimum(T.bid + tick, T.ask - tick) / tick + 1e-6) * tick
    # Modal buckets, the lower rung on a tie (charter 249's rule for the market's).
    T["mrank"] = T.sort_values(["dec", "q", "ord"], ascending=[True, False, True]).groupby("dec").cumcount().reindex(T.index) + 1
    T["prank"] = T.sort_values(["dec", "p_", "ord"], ascending=[True, False, True]).groupby("dec").cumcount().reindex(T.index) + 1
    T["proper"] = T.two & (T.mrank == 1) & (T.p_ > T.q) & (T.rung > EPS) & (T.rung < T.p_ - EPS)
    T["tb"] = T.two & (T.prank == 1) & (T.q >= 0.40) & (T.ask - T.bid <= 0.031) & (T.rung <= 0.60 + EPS)
    with np.errstate(invalid="ignore"):
        thr = T.has_next & T.nask.notna() & (T.nask <= T.rung + EPS)
        gone = T.has_next & (T.nbid.isna() | (T.nbid < T.bid - EPS))
        less = T.has_next & T.nbid.notna() & ((T.nbid - T.bid).abs() < EPS) & (T.nbsz < T.bsz - EPS)
    T["thr"] = thr
    T["lif"] = thr | gone | ((T.rung > T.bid + EPS) & less)
    return T


def legs(T, rule):
    """One leg a station-day: the first decision the rule holds at; later rows of that bucket the rule holds at re-post."""
    L = T[T[rule]].sort_values("t")
    plan = L.drop_duplicates(["st", "d"]).set_index(["st", "d"])
    L = L.join(plan[["ord", "t"]].rename(columns={"ord": "plan_ord", "t": "plan_t"}), on=["st", "d"])
    L = L[(L.ord == L.plan_ord) & (L.t >= L.plan_t)]
    out = plan.reset_index()[["st", "d", "ord", "t", "h", "p_", "q", "bid", "ask", "rung", "y", "tb", "proper", "prank", "mrank"]].copy()
    out["posts"] = out.set_index(["st", "d"]).index.map(L.groupby(["st", "d"]).size())
    for name in ("thr", "lif"):
        F = L[L[name]].drop_duplicates(["st", "d"]).set_index(["st", "d"])
        out[name + "_px"] = out.set_index(["st", "d"]).index.map(F.rung)
    out["take_px"] = out.ask + FEE * out.ask * (1 - out.ask)
    return out


def stat(S, price, label):
    """Per-share value and return on cost of buying at `price` and holding to settlement, event-day bootstrap."""
    S = S[S[price].notna()]
    if len(S) < 25:
        return f"{label}: n={len(S)} (too few)"
    pnl = (S.y - S[price]).values; cost = S[price].values
    codes, uniq = pd.factorize(S.d.values); k = len(uniq)
    sp = np.bincount(codes, pnl, k); sc = np.bincount(codes, cost, k); sn = np.bincount(codes, minlength=k)
    idx = np.random.default_rng(11).integers(0, k, size=(4000, k))
    per = sp[idx].sum(1) / sn[idx].sum(1); roi = sp[idx].sum(1) / sc[idx].sum(1)
    a, b = np.percentile(per, [2.5, 97.5]); c, e = np.percentile(roi, [2.5, 97.5])
    star = "*" if (a > 0 or b < 0) else " "
    return (f"{label}: n={len(S):4d} days={k:2d} paid {cost.mean():.3f} wins {S.y.mean():.3f} | per share {100*pnl.mean():+5.1f}c "
            f"[{100*a:+5.1f},{100*b:+5.1f}]{star} | return {100*sp.sum()/sc.sum():+5.1f}% [{100*c:+5.1f},{100*e:+5.1f}]")


def report(title, T):
    print(f"\n################ {title} ################")
    A = legs(T, "proper"); B = legs(T, "tb")
    kb = set(zip(B.st, B.d, B.ord)); ka = set(zip(A.st, A.d, A.ord))
    A["ever"] = [k in kb for k in zip(A.st, A.d, A.ord)]
    B["in_proper"] = [k in ka for k in zip(B.st, B.d, B.ord)]
    sd = T.groupby(["st", "d"]).ngroups
    print(f"station-days {sd} | Proper YES legs {len(A)} | tiebreak-test legs {len(B)} | same station-day and bucket {int(A.ever.sum())} "
          f"| both tests already hold at the Proper leg's own first decision {int(A.tb.sum())}")
    groups = (("OVERLAP (same station-day and bucket in both)", A[A.ever]),
              ("overlap, both tests hold at the Proper leg's first decision", A[A.tb]),
              ("Proper YES only", A[~A.ever]),
              ("all Proper YES legs", A),
              ("tiebreak test only (its own rung, re-posted while its test holds)", B[~B.in_proper]))
    for name, S in groups:
        if len(S) < 25:
            print(f"--- {name}: n={len(S)} (too few)"); continue
        print(f"--- {name}: {len(S)} legs, model p {S.p_.mean():.3f}, market share {S.q.mean():.3f}, bid {S.bid.mean():.3f} / ask {S.ask.mean():.3f}, "
              f"decisions rested {S.posts.mean():.1f}")
        print("   " + stat(S, "rung", "every first post fills     "))
        for rule, label in (("thr", "through (ask reaches me)   "), ("lif", "lifted (level behind me goes)")):
            print("   " + stat(S, rule + "_px", label) + f" | filled {S[rule + '_px'].notna().mean():4.0%}")
        print("   " + stat(S, "take_px", "taken at the ask, fee paid "))
        # The split: what the filled legs saved on price, and what each subset is worth to a taker.
        for rule in ("thr", "lif"):
            F = S[S[rule + "_px"].notna()]; U = S[S[rule + "_px"].isna()]
            if len(F) < 15 or len(U) < 15: continue
            print(f"      {rule}: filled {len(F)} legs paid {F[rule + '_px'].mean():.3f} against {F.take_px.mean():.3f} to take ({100*(F.take_px - F[rule + '_px']).mean():+.1f}c saved); "
                  f"as takers the filled legs make {100*(F.y - F.take_px).mean():+.1f}c (win {F.y.mean():.3f}, share {F.q.mean():.3f}), "
                  f"the unfilled {len(U)} make {100*(U.y - U.take_px).mean():+.1f}c (win {U.y.mean():.3f}, share {U.q.mean():.3f})")
    return A, B


F = frame()
report("SERVED record, 09-12 on, 45 stations", prepare(F[F.d >= "2026-09-12"], "p"))
report("SERVED record, 09-26 on (v14 and later)", prepare(F[F.d >= "2026-09-26"], "p"))
C = pd.read_pickle("replay_c.pkl"); C["stage"] = "P0"
report("REPLAY of the current chain, 34 C stations", prepare(C, "rp"))
report("SERVED record on the replay's own rows (C stations, same decisions)", prepare(C, "p"))
