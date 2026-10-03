"""Checks prompted by the 2026-10-03 review (proper_conditional_edge_review_20261003_codex):
   (1) tail-NO on the P0 FIRST decision only; (2) the replay headline without the stations whose training truth was
   revised inside the evaluation window; (3) the sub-second join fix; (4) C minus F, tested directly."""
import sqlite3, os
import numpy as np, pandas as pd
from common import frame, boot

FEE = 0.05
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
F = frame()
def tail(T, col):
    T = T[T.h > 36].copy()
    mid = np.where(T.bid.notna() & T.ask.notna(), (T.bid + T.ask) / 2, np.where(T.ask.notna(), T.ask / 2, np.where(T.bid.notna(), (T.bid + 1) / 2, 0.0)))
    T["mid"] = mid; T["mrank"] = T.groupby("dec")["mid"].rank(ascending=False, method="first")
    T["adist"] = (T.ord - T.dec.map(T[T.mrank == 1].set_index("dec")["ord"])).abs()
    T = T[T.bid.notna() & (T.adist >= 2) & (T.bid >= 0.02) & (T[col] < T.bid)].copy()
    T["pnl"] = T.bid - T.y - FEE * T.bid * (1 - T.bid)
    return T
def line(S):
    if len(S) < 30: return f"n={len(S)}"
    est, lo, hi, k = boot(S.pnl, np.ones(len(S)), S.d, B=5000)
    return f"n={len(S):4d} {100*est:+.3f}c [{100*lo:+.3f},{100*hi:+.3f}]{'*' if lo>0 or hi<0 else ' '} days={k}"
C = pd.read_pickle("replay_c.pkl")
S = F[(F.stage == "P0") & (F.d >= "2026-09-12")]
key = ["st", "d", "ord"]
print("=== (1) entry timing: the same slice at the FIRST P0 decision, at the first firing, and at the first firing inside 36-60h ===")
for label, T, col in (("replay, C stations", C, "rp"), ("served 09-12+, 45 stations", S, "p")):
    L = tail(T, col)
    print(f"{label:28s} first P0 decision only : {line(L[L['first'] == 1])}   median h={L[L['first']==1].h.median():.0f}")
    print(f"{'':28s} first firing           : {line(L.sort_values('t').drop_duplicates(key))}")
    L60 = L[L.h <= 60].sort_values('t').drop_duplicates(key)
    print(f"{'':28s} first firing in 36-60h : {line(L60)}   median h={L60.h.median():.0f}")
    sendable = L[(L.h <= 60) & L.q.notna() & (L.bsz >= 7) & (np.abs(L.bid * 100 - np.round(L.bid * 100)) < 1e-6)].sort_values('t').drop_duplicates(key)
    print(f"{'':28s} ... sendable, ladder quoted: {line(sendable)}")

print()
print("=== (2) replay training truth revised inside the evaluation window ===")
conn = sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True, isolation_level=None)
rows = conn.execute("SELECT station_id, local_date, revision_no, official_tmax_c, canonical_committed_at_utc FROM calibration_station_daily_high_truth"
                    " WHERE source_lane IN ('AWC','SETTLEMENT_SOURCE') AND qc_status='ACCEPTED' AND local_date >= '2026-06-01' ORDER BY station_id, local_date, revision_no").fetchall()
R = pd.DataFrame(rows, columns=["st", "d", "rev", "v", "at"])
g = R.groupby(["st", "d"])
chg = g.filter(lambda x: x.v.round(3).nunique() > 1)
late = chg[chg.groupby(["st", "d"])["at"].transform("max") >= "2026-08-14"]
cst = set(C.st.unique())
late_c = late[late.st.isin(cst)]
sd = late_c.groupby(["st", "d"]).agg(first_v=("v", "first"), last_v=("v", "last"), first_at=("at", "min"), last_at=("at", "max")).reset_index()
sd["delta"] = sd.last_v - sd.first_v
print(f"C-station station-days whose formal truth value changed with the newer revision committed on/after 2026-08-14: {len(sd)} across {sd.st.nunique()} stations; |change| >= 1C: {(sd.delta.abs() >= 1).sum()}")
print(sd.sort_values("d").to_string(index=False))
bad = set(sd.st)
L = tail(C, "rp"); ff = L[L.h <= 60].sort_values("t").drop_duplicates(key)
print(f"replay tail-NO, first firing 36-60h, all 34 C stations          : {line(ff)}")
print(f"replay tail-NO, without the {len(bad)} stations with such a revision : {line(ff[~ff.st.isin(bad)])}")
print(f"replay tail-NO, only those {len(bad)} stations                      : {line(ff[ff.st.isin(bad)])}")

print()
print("=== (4) C minus F on the served record, tested directly (first firing 36-60h, sendable legs) ===")
L = tail(S, "p"); ff = L[(L.h <= 60) & L.q.notna()].sort_values("t").drop_duplicates(key)
days = ff.d.unique(); rng = np.random.default_rng(3); by = {d: ff[ff.d == d] for d in days}; diffs = []
for _ in range(5000):
    X = pd.concat([by[d] for d in rng.choice(days, len(days))])
    c, f = X[X.unit == "C"].pnl, X[X.unit == "F"].pnl
    if len(c) and len(f): diffs.append(c.mean() - f.mean())
lo, hi = np.percentile(diffs, [2.5, 97.5])
print(f"C {100*ff[ff.unit=='C'].pnl.mean():+.3f}c (n={(ff.unit=='C').sum()})  F {100*ff[ff.unit=='F'].pnl.mean():+.3f}c (n={(ff.unit=='F').sum()})  C - F = {100*(ff[ff.unit=='C'].pnl.mean()-ff[ff.unit=='F'].pnl.mean()):+.3f}c [{100*lo:+.3f},{100*hi:+.3f}]")

print()
print("=== (2b) the same exposure by time: every revision above landed by 2026-09-25T13:04Z (charter 225's repair) or 09-30; decisions after that trained on truth that was known ===")
L = tail(C, "rp"); L = L[L.h <= 60]
for name, m in (("decided before 2026-09-25T13:04Z (training truth includes values not yet known)", L.t < "2026-09-25T13:04"),
                ("decided after  2026-09-25T13:04Z (training truth as known, bar MPMG 09-21)", L.t >= "2026-09-25T13:04")):
    print(f"replay tail-NO first firing, {name}: {line(L[m].sort_values('t').drop_duplicates(key))}")
Ls = tail(S, "p"); Ls = Ls[Ls.h <= 60]
print(f"served (always as known), decided after 2026-09-25T13:04Z: {line(Ls[Ls.t >= '2026-09-25T13:04'].sort_values('t').drop_duplicates(key))}")
big = sd[sd.delta.abs() >= 2]
print(f"revisions of 2C or more: {len(big)} station-days, event dates {big.d.min()}..{big.d.max()}, stations {sorted(big.st.unique())}")
