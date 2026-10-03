"""Real resting orders (post-only GTD at the best bid + 1 tick) that reached the venue: how often they filled,
   and what the filled and the unfilled ones were worth at the price they rested at (adverse selection)."""
import collections, json, os, sqlite3
import numpy as np, pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
c = sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True, isolation_level=None)
rows = c.execute("""
SELECT i.order_intent_id, COALESCE(json_extract(i.intent_json,'$.lane'),'basket') lane, i.bucket, i.side, i.limit_price, i.requested_size,
       i.created_at, d.station_id, d.event_date, json_extract(i.intent_json,'$.expiration') exp
FROM basket_target_order_intents i JOIN auto_trade_decisions d ON d.decision_id=i.decision_id
WHERE i.order_action='BUY' AND i.created_at >= '2026-09-10' AND json_extract(i.intent_json,'$.post_only') IN (1,'true')""").fetchall()
I = pd.DataFrame(rows, columns=["id", "lane", "bucket", "side", "px", "size", "created", "st", "d", "exp"])
ev = collections.defaultdict(list)
ids = list(I.id)
for k in range(0, len(ids), 900):
    ch = ids[k:k + 900]
    for oid, et, at in c.execute(f"select order_intent_id, event_type, observed_at from basket_target_order_events where order_intent_id in ({','.join('?' * len(ch))})", ch):
        ev[oid].append((et, at))
I["acked"] = [any(e[0] == "POST_ACK" for e in ev.get(i, [])) for i in I.id]
rec = dict(c.execute("select order_intent_id, filled_size from basket_target_order_reconciliations").fetchall())
I["filled"] = [float(rec.get(i) or 0.0) for i in I.id]
label = dict(((s, d), b) for s, d, b in c.execute(
    "SELECT station_id, event_date, settled_bucket FROM basket_station_day_settlement_label l WHERE truth_revision_no=(SELECT MAX(truth_revision_no)"
    " FROM basket_station_day_settlement_label x WHERE x.station_id=l.station_id AND x.event_date=l.event_date)"))
R = I[I.acked].copy()
R["settled"] = [label.get((s, d)) for s, d in zip(R.st, R.d)]
R = R[R.settled.notna()].copy()
R["hit"] = (R.settled == R.bucket).astype(float)
R["win"] = np.where(R.side.str.upper().str.contains("NO"), 1 - R.hit, R.hit)      # the token bought pays 1 when it wins
R["pnl"] = R.win - R.px
R["is_filled"] = R.filled > 0
R["life_s"] = R.exp - pd.to_datetime(R.created, format="ISO8601").astype("int64") // 10**9
print(f"resting post-only BUY orders that reached the venue and whose day has settled: {len(R)}  (lanes: {R.lane.value_counts().to_dict()}; sides: {R.side.value_counts().to_dict()})")
print(f"signed lifetime (expiration - created): median {R.life_s.median():.0f}s")
def line(S):
    if len(S) < 10: return f"n={len(S)}"
    days = S.d.unique(); rng = np.random.default_rng(2); by = {d: S[S.d == d].pnl.values for d in days}
    bs = [np.concatenate([by[d] for d in rng.choice(days, len(days))]).mean() for _ in range(3000)]
    lo, hi = np.percentile(bs, [2.5, 97.5])
    return f"n={len(S):4d} price {S.px.mean():.3f} won {S.win.mean():.3f}  value at the resting price {100*S.pnl.mean():+.1f}c a share [{100*lo:+.1f},{100*hi:+.1f}]{'*' if lo>0 or hi<0 else ' '} days={len(days)}"
for lane in ["all"] + sorted(R.lane.unique()):
    S = R if lane == "all" else R[R.lane == lane]
    if len(S) < 20: continue
    print(f"--- {lane}: fill rate {S.is_filled.mean():.0%} ({int(S.is_filled.sum())}/{len(S)}) ---")
    print(f"   filled              : {line(S[S.is_filled])}")
    print(f"   rested, not filled  : {line(S[~S.is_filled])}")
    print(f"   all that rested     : {line(S)}")
# one order per station-day-bucket-lane (the first that rested): did that LEG ever fill, over all its re-posts?
G = R.sort_values("created").groupby(["lane", "st", "d", "bucket", "side"])
L = G.agg(posts=("id", "size"), filled=("filled", "sum"), px=("px", "first"), win=("win", "first"), d2=("d", "first")).reset_index()
L["leg_filled"] = L.filled > 0
print()
print(f"by LEG (a station-day-bucket re-posted until filled): {len(L)} legs, {L.leg_filled.mean():.0%} filled at some point; posts per leg median {L.posts.median():.0f}")
for lane, S in L.groupby("lane"):
    if len(S) < 10: continue
    f, u = S[S.leg_filled], S[~S.leg_filled]
    print(f"   {lane:12s} legs {len(S):4d} filled {len(f):4d} ({len(f)/len(S):.0%}) | filled legs: price {f.px.mean():.3f} won {f.win.mean():.3f} -> {100*(f.win-f.px).mean():+.1f}c | never filled: price {u.px.mean():.3f} won {u.win.mean():.3f} -> {100*(u.win-u.px).mean():+.1f}c")
