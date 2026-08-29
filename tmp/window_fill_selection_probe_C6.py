import json, sqlite3, collections
conn=sqlite3.connect("file:data/bayes_basket_target_ssot.db?mode=ro",uri=True,timeout=30)
pos=collections.defaultdict(list)
for r in conn.execute("select station_id,event_date,stage,decision_at_utc,ladder_center_bucket,posted_buckets_json,window_buckets_json,probabilities_json,leg_shares_json,intended_legs_json,prices_json from paper_trade_position"):
    pos[(r[0],r[1])].append(r)
setl={(r[0],r[1]):r for r in conn.execute("select station_id,event_date,stage,settled_bucket,hit from paper_trade_settlement")}
t=collections.defaultdict(lambda:[0,0])
px=collections.defaultdict(lambda:[0.0,0])
for k,sr in sorted(setl.items()):
    for cr in sorted(pos.get(k) or [],key=lambda r:r[3]):
        p=json.loads(cr[7]); order=list(p); idx={b:i for i,b in enumerate(order)}
        c=idx.get(cr[4]);  legs=json.loads(cr[8] or "{}") or {}
        prices=json.loads(cr[10] or "{}") or {}
        if c is None: continue
        for b in json.loads(cr[5] or "[]"):
            if b not in idx: continue
            d=max(-2,min(2,idx[b]-c)); f = float(legs.get(b) or 0.0) > 0.0
            e=t[(d,f)]; e[0]+=1; e[1]+= (b==sr[3])
            if b in prices:
                q=px[(d,f)]; q[0]+=float(prices[b]); q[1]+=1
print(" d  filled?  posted_rungs  settled_here%   mean_ask   model_p_mean")
for d in range(-2,3):
    for f in (True,False):
        n,h=t[(d,f)]
        if not n: continue
        s,c2=px[(d,f)]
        print("%3d  %-6s %8d %12s %10s"%(d,"FILLED" if f else "unfilled",n,"%.1f%%"%(100*h/n), "%.3f"%(s/c2) if c2 else "-"))
