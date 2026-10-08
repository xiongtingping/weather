"""Conditional binary log-loss on OUR actually-bought legs: our served p vs market mid vs paid price."""
import sqlite3, json, math, collections, statistics as st, datetime as dt, sys
DB="/Users/xiong/weather/data/bayes_basket_target_ssot.db"
conn=sqlite3.connect(f"file:{DB}?mode=ro",uri=True,isolation_level=None,timeout=30); conn.execute("PRAGMA query_only=1")
rows=conn.execute("""
SELECT i.decision_id, i.order_intent_id, c.station_id, c.event_date, i.bucket, i.side, i.limit_price, i.created_at,
       r.filled_size, r.filled_notional_usdc, i.token_id
FROM basket_target_order_intents i
JOIN basket_target_order_reconciliations r ON r.order_intent_id=i.order_intent_id
JOIN basket_target_execution_cycles c ON c.cycle_id=i.cycle_id
WHERE i.live_submit_authorized=1 AND i.order_action='BUY' AND r.filled_size>0
  AND i.created_at>='2026-09-06' AND c.event_date<='2026-10-07'
""").fetchall()
print("our filled BUY legs 09-06..10-07:",len(rows))
labels={(st_,ed):(b,t) for st_,ed,b,t in conn.execute("select station_id,event_date,settled_bucket,settled_tmax_c from basket_station_day_settlement_label where event_date between '2026-09-01' and '2026-10-07'")}
def lane_of(did): return did.split("_decision_")[0] if "_decision_" in did else ("basket" if did else "?")
def served(st_,ed,bucket,ts_iso):
    h=conn.execute("select history_id,stage from basket_probability_bridge_history where station_id=? and event_date=? and state='COMMITTED' and decision_at_utc<=? order by decision_at_utc desc limit 1",(st_,ed,ts_iso)).fetchone()
    if not h: return None,None
    P={b:p for b,p in conn.execute("select bucket,probability from basket_probability_bridge_bucket_probability where history_id=?",(h[0],))}
    t=sum(P.values()); 
    if not t or bucket not in P: return None,h[1]
    fl={b:max(v/t,1e-3) for b,v in P.items()}; s=sum(fl.values()); return fl[bucket]/s,h[1]
def market_mid(st_,ed,bucket,ts_iso):
    row=conn.execute("select buckets_json from basket_official_ws_snapshot where station_id=? and event_date=? and state='COMMITTED' and observed_at<=? and observed_at>=? order by observed_at desc limit 1",(st_,ed,ts_iso,(dt.datetime.fromisoformat(ts_iso.replace('Z','+00:00'))-dt.timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S.%fZ"))).fetchone()
    if not row: return None
    B=json.loads(row[0]); q={}
    for b in B:
        bid,ask=b.get("bid_price"),b.get("best_ask")
        if bid is not None and ask is not None and ask-bid<0.2: q[b["bucket"]]=(bid+ask)/2
    s=sum(q.values())
    if len(q)<3 or not 0.85<=s<=1.2 or bucket not in q: return None
    return q[bucket]/s
def ll(p,h): p=min(.999,max(.001,p)); return -(h*math.log(p)+(1-h)*math.log(1-p))
legs=[]
for did,iid,st_,ed,bucket,side,price,created,filled,notional,token in rows:
    lab=labels.get((st_,ed))
    if not lab: continue
    paid=notional/filled if filled else price
    p_yes,stage=served(st_,ed,bucket,created); q_yes=market_mid(st_,ed,bucket,created)
    if p_yes is None or q_yes is None: continue
    win_yes=1.0 if lab[0]==bucket else 0.0
    if side=="NO": p,q,win=1-p_yes,1-q_yes,1-win_yes
    else: p,q,win=p_yes,q_yes,win_yes
    legs.append(dict(lane=lane_of(did),st=st_,ed=ed,side=side,p=p,q=q,paid=paid,win=win,stage=stage,usdc=notional,half="H1" if ed<"2026-09-22" else "H2"))
print("legs with our p, market mid and truth:",len(legs),"| lanes:",collections.Counter(l["lane"] for l in legs))
def rep(tag,ls):
    if len(ls)<15: return
    U=lambda f: st.mean(f(l) for l in ls)
    print(f"  {tag:34s} n={len(ls):4d} USDC={sum(l['usdc'] for l in ls):7.0f} | logloss: ours {U(lambda l:ll(l['p'],l['win'])):.4f} | market mid {U(lambda l:ll(l['q'],l['win'])):.4f} | paid {U(lambda l:ll(l['paid'],l['win'])):.4f} | Brier ours {U(lambda l:(l['p']-l['win'])**2):.4f} mkt {U(lambda l:(l['q']-l['win'])**2):.4f} | realized {U(lambda l:l['win']):.3f} vs ours {U(lambda l:l['p']):.3f} mkt {U(lambda l:l['q']):.3f} paid {U(lambda l:l['paid']):.3f} | ret {(U(lambda l:l['win'])/U(lambda l:l['paid'])-1)*100:+.1f}%")
rep("ALL our bought legs",legs)
for lane in sorted(set(l["lane"] for l in legs)): rep(f"lane {lane}",[l for l in legs if l["lane"]==lane])
for side in ("YES","NO"): rep(f"side {side}",[l for l in legs if l["side"]==side])
for sg in ("P0","P0-M","P1"): rep(f"stage {sg}",[l for l in legs if l["stage"]==sg])
for hf in ("H1","H2"): rep(hf,[l for l in legs if l["half"]==hf])
# the conditional edge question: on our bought legs, is p closer to truth than q? share of legs where |p-win|<|q-win|
print("  share of legs where ours is closer to the outcome than the market:",round(st.mean(abs(l['p']-l['win'])<abs(l['q']-l['win']) for l in legs),3))
# by our disagreement with market (p-q) bands
for lo,hi in ((-1,0),(0,0.05),(0.05,0.1),(0.1,0.2),(0.2,1)): rep(f"p-q in [{lo},{hi})",[l for l in legs if lo<=l["p"]-l["q"]<hi])
conn.close()
