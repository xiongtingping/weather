"""Among the two distance-1 rungs of one row, which signal picks the winner?"""
import sqlite3, json
from math import comb
c=sqlite3.connect("file:data/bayes_basket_target_ssot.db?mode=ro",uri=True,timeout=60)
rows=c.execute("SELECT p.position_id,p.probabilities_json,p.prices_json,"
 "p.ladder_center_bucket,s.settled_bucket FROM paper_trade_position p"
 " JOIN paper_trade_settlement s ON s.station_id=p.station_id AND s.event_date=p.event_date").fetchall()
cw=cl_=pw=pl=0; used=0
for (pid,probj,pricej,lcb,settled) in rows:
    probs=json.loads(probj or "{}") or {}; prices=json.loads(pricej or "{}") or {}
    ladder=list(probs.keys())
    if lcb not in ladder: continue
    ci=ladder.index(lcb)
    pair=[ladder[j] for j in (ci-1,ci+1) if 0<=j<len(ladder)]
    if len(pair)!=2 or settled not in pair: continue
    used+=1
    hi_c=max(pair,key=lambda b: probs[b]); 
    if hi_c==settled: cw+=1
    else: cl_+=1
    if all(prices.get(b) is not None for b in pair):
        hi_p=max(pair,key=lambda b: float(prices[b]))
        if hi_p==settled: pw+=1
        else: pl+=1
def sgn(w,l):
    n=w+l
    return sum(comb(n,k) for k in range(w,n+1))/2**n if n else float('nan')
print("rows where the truth is one of the two distance-1 rungs: %d"%used)
print("higher-CLAIM rung is the winner: %d/%d = %.3f  (one-tail p vs coin = %.3f)"%(cw,cw+cl_,cw/(cw+cl_),sgn(cw,cl_)))
print("higher-PRICE rung is the winner: %d/%d = %.3f  (one-tail p vs coin = %.4g)"%(pw,pw+pl,pw/(pw+pl),sgn(pw,pl)))
