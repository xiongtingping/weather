"""Overlap between the live market-only far-tail rule (charter 234) and the model-conditional tail-NO slice.
   (1) the live lane's own fills, split by whether the served model had p under the bid when it bought;
   (2) the two rules fired on the same P0 >36h books: both / only 234 / only model-conditional."""
import os, sqlite3, sys
import numpy as np, pandas as pd
from scipy import stats
from common import boot, frame
from load_open import build_open

FEE = 0.05
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
F = frame()
O = build_open()
import gzip, json
names = {}
with gzip.open("decisions_open.jsonl.gz", "rt") as fh:
    for line in fh:
        r = json.loads(line)
        names[(r["st"], r["d"])] = {x[1]: x[0] for x in r["b"]}
O["k"] = [names.get((s, d), {}).get(o) for s, d, o in zip(O.st, O.d, O.ord)]
ALL = pd.concat([F[["st", "d", "stage", "t", "k", "p", "bid", "ask", "ver"]], O[["st", "d", "stage", "t", "k", "p", "bid", "ask", "ver"]]], ignore_index=True)
ALL = ALL[ALL.stage == "P0"].sort_values("t")

conn = sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True, isolation_level=None)
fills = pd.DataFrame(conn.execute(
    "SELECT station_id, event_date, bucket, evaluated_at_utc, yes_bid, market_q, rungs_from_mode, no_price, filled_shares, cost_usdc, fee_usdc_est, hours_to_settlement"
    " FROM tail_short_lane_evaluation WHERE filled_shares > 0").fetchall(),
    columns=["st", "d", "k", "at", "bid", "q", "rungs", "no_px", "sh", "cost", "fee", "h"])
label = dict(((s, d), b) for s, d, b in conn.execute(
    "SELECT station_id, event_date, settled_bucket FROM basket_station_day_settlement_label l WHERE truth_revision_no=(SELECT MAX(truth_revision_no)"
    " FROM basket_station_day_settlement_label x WHERE x.station_id=l.station_id AND x.event_date=l.event_date)"))
ps, vers = [], []
for r in fills.itertuples():
    s = ALL[(ALL.st == r.st) & (ALL.d == r.d) & (ALL.k == r.k) & (ALL.t <= r.at)]
    ps.append(s.p.iloc[-1] if len(s) else np.nan); vers.append(s.ver.iloc[-1] if len(s) else np.nan)
fills["p"] = ps; fills["ver"] = vers
fills["settled"] = [label.get((s, d)) for s, d in zip(fills.st, fills.d)]
fills["hit"] = np.where(fills.settled.isna(), np.nan, (fills.settled == fills.k).astype(float))
fills["pnl"] = np.where(fills.hit.isna(), np.nan, fills.sh * (1 - fills.hit) - fills.cost - fills.fee)
fills["model"] = np.where(fills.p.isna(), "no served p", np.where(fills.p < fills.bid, "model says dear (p<bid)", "model does not (p>=bid)"))
fills["unit"] = np.where(fills.k.str.contains("°F"), "F", "C")
print("=== (1) the live far-tail lane's own fills (real money), by what the served model said at the buy ===")
print(f"fills {len(fills)}, cost ${fills.cost.sum():.2f}; settled {int(fills.hit.notna().sum())}, open {int(fills.hit.isna().sum())}")
g = fills.groupby("model").agg(fills=("sh", "size"), cost=("cost", "sum"), settled=("hit", "count"), hits=("hit", "sum"), pnl=("pnl", "sum"), bid=("bid", "mean"), p=("p", "mean"))
g["roi_settled"] = g.pnl / fills[fills.hit.notna()].groupby("model").apply(lambda x: (x.cost + x.fee).sum(), include_groups=False)
print(g.round(4).to_string())
print("by unit:", fills.groupby(["unit", "model"]).size().to_dict())
print(fills[fills.hit == 1][["st", "d", "k", "bid", "p", "sh", "cost", "pnl", "model"]].to_string())

print()
print("=== (2) the two rules on the same P0 >36h books ===")
def classify(T, col, label_):
    T = T.copy()
    mid = np.where(T.bid.notna() & T.ask.notna(), (T.bid + T.ask) / 2, np.where(T.ask.notna(), T.ask / 2, np.where(T.bid.notna(), (T.bid + 1) / 2, 0.0)))
    T["mid"] = mid; T["mrank"] = T.groupby("dec")["mid"].rank(ascending=False, method="first")
    T["adist"] = (T.ord - T.dec.map(T[T.mrank == 1].set_index("dec")["ord"])).abs()
    T = T[T.bid.notna() & (T.bid >= 0.02) & (T.adist >= 2)].copy()
    T["truth"] = T.bid - T.y - FEE * T.bid * (1 - T.bid)
    T["r234"] = T.q.notna() & (T.q <= 0.05) & (T.adist >= 3)
    T["rnew"] = (T[col] < T.bid) & (T.h <= 60)
    T["grid"] = (np.abs(T.bid * 100 - np.round(T.bid * 100)) < 1e-6)
    T["thick"] = T.bsz >= 7
    key = ["st", "d", "ord"]
    a = T[T.r234].sort_values("t").drop_duplicates(key).set_index(key)
    b = T[T.rnew].sort_values("t").drop_duplicates(key).set_index(key)
    both = a.index.intersection(b.index); only_a = a.index.difference(b.index); only_b = b.index.difference(a.index)
    def line(S, name):
        if len(S) < 30: print(f"  {name:58s} n={len(S)}"); return
        dm = S.groupby("d").truth.mean(); half = stats.t.ppf(0.975, len(dm) - 1) * dm.std(ddof=1) / np.sqrt(len(dm))
        est, lo, hi, k = boot(S.truth, np.ones(len(S)), S.d)
        print(f"  {name:58s} n={len(S):5d} bid={S.bid.mean():.3f} hit={S.y.mean():.3f} pnl/share {est:+.4f} [{lo:+.4f},{hi:+.4f}]{'*' if lo>0 or hi<0 else ' '} +d {int((dm>0).sum())}/{len(dm)}")
    print(f"--- {label_} ---  (one entry per station-day-bucket, at the rule's own first firing; taker at the bid, fee included)")
    line(a.reset_index(), "234 fires (q<=5%, 3+ rungs, bid>=2c, whole ladder quoted)")
    line(b.reset_index(), "model-conditional fires (2+ rungs, bid>=2c, p<bid, 36-60h)")
    line(b.loc[both].reset_index(), "  both rules -> same bucket bought twice if both lanes run")
    line(a.loc[only_a].reset_index(), "  only 234 (model does not say dear, or only >60h)")
    ob = b.loc[only_b].reset_index()
    line(ob, "  only model-conditional")
    line(ob[ob.adist == 2], "     of which exactly 2 rungs out")
    line(ob[(ob.adist >= 3) & ob.q.notna() & (ob.q > 0.05)], "     of which 3+ rungs, market mid above 5%")
    line(ob[ob.q.isna()], "     of which ladder not fully quoted (234 refuses the book)")
    bb = b.reset_index()
    print(f"  model-conditional legs: bid on the cent grid {bb.grid.mean():.0%}; best-bid depth >= 7 shares {bb.thick.mean():.0%}; both {(bb.grid & bb.thick).mean():.0%}")
    line(bb[bb.grid & bb.thick], "  model-conditional, sendable today (cent grid, depth>=7)")
    line(bb[~bb.grid], "  model-conditional, bid off the cent grid (refused today)")
    per = b.reset_index().groupby(["st", "d"]).size()
    print(f"  model-conditional legs per station-day: median {per.median():.0f}, share of station-days over 4 legs {(per > 4).mean():.0%}")
S = F[(F.stage == "P0") & (F.h > 36) & (F.d >= "2026-09-12")]
classify(S, "p", "SERVED 09-12+, all 45 stations")
classify(S[S.unit == "C"], "p", "SERVED 09-12+, C stations")
C = pd.read_pickle("replay_c.pkl") if os.path.exists("replay_c.pkl") else None
if C is not None:
    classify(C[C.h > 36], "rp", "REPLAY current chain, C stations, 38 days")
