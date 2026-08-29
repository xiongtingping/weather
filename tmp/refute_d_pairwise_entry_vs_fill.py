"""Row-paired: when the winning dist=1 shoulder was NOT bought, had entry posted it anyway?"""
import sqlite3, json
from collections import Counter
c = sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro",uri=True,timeout=60)
setl={(st,ed):sb for st,ed,sb in c.execute("SELECT station_id,event_date,settled_bucket FROM paper_trade_settlement")}
rows=c.execute("""SELECT station_id,event_date,stage,ladder_center_bucket,probabilities_json,
 prices_json,posted_buckets_json,leg_shares_json FROM paper_trade_position""").fetchall()
pairs=Counter(); winners_where=Counter(); claimcmp=Counter(); pxcmp=Counter()
both_posted=0
for (st,ed,stage,lcb,probj,pricej,postj,legj) in rows:
    if (st,ed) not in setl: continue
    settled=setl[(st,ed)]
    probs=json.loads(probj or "{}") or {}; prices=json.loads(pricej or "{}") or {}
    posted=set(json.loads(postj or "[]") or [])
    legs={b for b,q in (json.loads(legj or "{}") or {}).items() if float(q)>0}
    ladder=list(probs.keys())
    if lcb not in ladder: continue
    ci=ladder.index(lcb)
    sh=[ladder[j] for j in (ci-1,ci+1) if 0<=j<len(ladder)]
    if len(sh)!=2: continue
    lo,hi=sh
    b_lo,b_hi = lo in legs, hi in legs
    if b_lo==b_hi:
        pairs["both bought" if b_lo else "neither bought"]+=1
        if b_lo and settled in sh: winners_where["both-bought row"]+=1
        continue
    pairs["exactly one bought"]+=1
    bought = lo if b_lo else hi
    unb    = hi if b_lo else lo
    if unb in posted: both_posted+=1
    if settled==bought: winners_where["bought leg won"]+=1
    elif settled==unb:
        winners_where["unbought leg won -- it WAS posted" if unb in posted
                      else "unbought leg won -- never posted"]+=1
    else: winners_where["neither shoulder won"]+=1
    # discrimination at entry
    if settled in (bought,unb):
        hi_claim = bought if probs[bought]>probs[unb] else unb
        hi_px    = bought if prices.get(bought,0)>prices.get(unb,0) else unb
        claimcmp["higher-claim shoulder won" if settled==hi_claim else "lower-claim shoulder won"]+=1
        pxcmp["higher-price shoulder won" if settled==hi_px else "lower-price shoulder won"]+=1
print("rows with both shoulders on ladder:", sum(pairs.values()))
for k,v in pairs.items(): print("  %-22s %d"%(k,v))
print("\nof the 'exactly one bought' rows, the unbought shoulder was still POSTED in %d"%both_posted)
print("\noutcome of 'exactly one bought' rows:")
for k,v in winners_where.items(): print("  %-40s %d"%(k,v))
print("\nentry-level discrimination on rows where truth hit a shoulder:")
for k,v in claimcmp.items(): print("  %-34s %d"%(k,v))
for k,v in pxcmp.items(): print("  %-34s %d"%(k,v))
