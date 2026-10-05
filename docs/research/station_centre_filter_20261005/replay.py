"""逐站校准排序 as a filter (熊先生 2026-10-05): buy only at stations whose last-7-day centre
calibration was good, replay ROI.  Rule = charter 249 Proper YES: P0, >36h to settle, the market's
mode bucket (normalised mid, ties to the lower rung), p above the price; one leg per station-day,
1 U per leg (charter 252).  Centre reading per station-day = mean over its P0 decisions of
(truth ordinal - sum p*ordinal), in rungs -- the board's 真值−均值 档.  Strict as-of: a leg decided
on UTC date T reads station-days with event date in [T-8, T-2] (7 days, all settled by T)."""
import sys, numpy as np, pandas as pd
sys.path.insert(0, "../proper_conditional_edge_20261003")
from load import load

FEE = 0.05
df = load()
P0 = df[df.stage == "P0"].copy()
# --- centre reading per station-day (all P0 decisions) ---
P0["pe"] = P0.p * P0.ord
dec = P0.groupby("dec").agg(st=("st", "first"), d=("d", "first"), em=("pe", "sum"), y=("y_ord", "first"))
dec["err"] = dec.y - dec.em
sd = dec.groupby(["st", "d"]).err.mean().reset_index()
sd["dd"] = pd.to_datetime(sd.d)

# --- candidate decisions: P0 >36h, market mode bucket ---
T = P0[(P0.h > 36) & P0.q.notna()].copy()
two = T.bid.notna() & T.ask.notna() & (T.bid > 0)
T["mid"] = np.where(two, (T.bid + T.ask) / 2, np.nan)
T = T[T.mid.notna()]
T["msum"] = T.groupby("dec").mid.transform("sum")
T = T.sort_values(["dec", "mid", "ord"], ascending=[True, False, True])
M = T.drop_duplicates("dec").sort_values("t")           # mode bucket of each decision
tick = M.tick.fillna(0.01)
M["mk"] = np.floor(np.minimum(M.bid + tick, M.ask - tick) / tick + 1e-6) * tick
legs = {}
for name, price, ok in (("taker@ask", M.ask + FEE * M.ask * (1 - M.ask), M.p > M.ask),
                        ("maker@bid+1", M.mk, (M.mk > 0) & (M.p > M.mk))):
    L = M[ok].assign(px=price[ok]).drop_duplicates(["st", "d"]).copy()   # first firing per station-day
    L["r"] = (L.y - L.px) / L.px                                         # return on 1 U
    legs[name] = L

# maker fill: 'through' = a later book on the same bucket (still P0 >36h) has ask <= my price
nxt = T[["st", "d", "ord", "t", "ask"]]
L = legs["maker@bid+1"]
j = L[["st", "d", "ord", "t", "mk"]].merge(nxt, on=["st", "d", "ord"], suffixes=("", "_n"))
j = j[(j.t_n > j.t) & (j.ask <= j.mk + 1e-9)]
L["through"] = L.set_index(["st", "d", "ord"]).index.isin(j.set_index(["st", "d", "ord"]).index)

def centre(row_t, st):
    T0 = pd.Timestamp(row_t).tz_localize(None).normalize() if pd.Timestamp(row_t).tzinfo else pd.Timestamp(row_t).normalize()
    return T0
def attach(L):
    L = L.copy()
    L["td"] = pd.to_datetime(L.t, utc=True).dt.tz_localize(None).dt.normalize()
    rows = []
    g = {k: v for k, v in sd.groupby("st")}
    for i, r in L.iterrows():
        S = g.get(r.st)
        if S is None: rows.append((np.nan, 0)); continue
        w = S[(S.dd >= r.td - pd.Timedelta(days=8)) & (S.dd <= r.td - pd.Timedelta(days=2))]
        rows.append((w.err.mean() if len(w) else np.nan, len(w)))
    L["bias7"], L["n7"] = zip(*rows)
    return L
for k in legs: legs[k] = attach(legs[k])

def boot(S, c="r"):
    codes, u = pd.factorize(S.d.values); k = len(u)
    s = np.bincount(codes, S[c], k); n = np.bincount(codes, minlength=k)
    idx = np.random.default_rng(5).integers(0, k, (4000, k))
    b = s[idx].sum(1) / n[idx].sum(1); lo, hi = np.percentile(b, [2.5, 97.5])
    return f"n={len(S):4d} 天={k:2d} 命中={S.y.mean():.2f} 均价={S.px.mean():.3f} ROI {100*S[c].mean():+6.1f}% [{100*lo:+6.1f},{100*hi:+6.1f}]{'*' if lo>0 or hi<0 else ' '}"

def report(L, label):
    print(f"===== {label} =====")
    ok = L.n7 >= 3
    L = L.assign(ab=L.bias7.abs())
    print(" 全部腿                      ", boot(L))
    print(" 有7天读数(≥3站日)           ", boot(L[ok]))
    print(" |偏差|≤0.3档 (看板绿)       ", boot(L[ok & (L.ab <= 0.3)]))
    print(" 0.3<|偏差|≤0.8 (黄)         ", boot(L[ok & (L.ab > 0.3) & (L.ab <= 0.8)]))
    print(" |偏差|>0.8 (红)             ", boot(L[ok & (L.ab > 0.8)]))
    # rank within the day: top half by |bias| among stations with readings that day
    Lr = L[ok].copy(); Lr["rk"] = Lr.groupby(Lr.td).ab.rank(pct=True)
    print(" 当日中心名次前一半          ", boot(Lr[Lr.rk <= 0.5]))
    print(" 当日中心名次后一半          ", boot(Lr[Lr.rk > 0.5]))
    for era, m in (("09-12 前", L.d < "2026-09-12"), ("09-12 起", L.d >= "2026-09-12"), ("09-26 起", L.d >= "2026-09-26")):
        G = L[m & ok]
        if len(G) > 20:
            print(f"   {era}: 绿 {boot(G[G.ab <= 0.3])}\n            非绿 {boot(G[G.ab > 0.3])}")

report(legs["taker@ask"], "吃单 @卖一（扣手续费）")
Lm = legs["maker@bid+1"]
report(Lm, "挂单 @买一+1，全部成交（乐观）")
report(Lm[Lm.through], "挂单 @买一+1，只算穿价成交（后续卖一≤挂价）")
print("穿价成交率:", f"{Lm.through.mean():.0%}")

def diff(L, label):
    L = L[L.n7 >= 3].assign(g=lambda x: x.bias7.abs() <= 0.3)
    codes, u = pd.factorize(L.d.values); k = len(u); g = L.g.values; r = L.r.values
    sa = np.bincount(codes, r * g, k); na = np.bincount(codes, g * 1.0, k)
    sb = np.bincount(codes, r * ~g, k); nb = np.bincount(codes, (~g) * 1.0, k)
    idx = np.random.default_rng(11).integers(0, k, (4000, k))
    out = sa[idx].sum(1) / na[idx].sum(1) - sb[idx].sum(1) / nb[idx].sum(1)
    est = sa.sum() / na.sum() - sb.sum() / nb.sum(); lo, hi = np.nanpercentile(out, [2.5, 97.5])
    print(f"  {label:34s} 绿−非绿 {100*est:+6.1f} 点 [{100*lo:+6.1f},{100*hi:+6.1f}]{'*' if lo>0 or hi<0 else ' '}")
print("===== 差值（按事件日整块重抽） =====")
for name, L in (("吃单", legs["taker@ask"]), ("挂单全成交", Lm), ("挂单穿价成交", Lm[Lm.through])):
    diff(L, name + " 全期"); diff(L[L.d >= "2026-09-12"], name + " 09-12 起")
