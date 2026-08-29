import json, sqlite3, collections
conn=sqlite3.connect("file:data/bayes_basket_target_ssot.db?mode=ro",uri=True,timeout=30)
pos=collections.defaultdict(list)
for r in conn.execute("select station_id,event_date,stage,decision_at_utc,ladder_center_bucket,posted_buckets_json,probabilities_json,leg_shares_json,prices_json from paper_trade_position"):
    pos[(r[0],r[1])].append(r)
setl={(r[0],r[1]):r for r in conn.execute("select station_id,event_date,stage,settled_bucket from paper_trade_settlement")}
agg=collections.defaultdict(lambda:[0,0,0.0,0.0])
for k,sr in sorted(setl.items()):
    for cr in sorted(pos.get(k) or [],key=lambda r:r[3]):
        p=json.loads(cr[6]); idx={b:i for i,b in enumerate(p)}
        c=idx.get(cr[4]); legs=json.loads(cr[7] or "{}") or {}; pr=json.loads(cr[8] or "{}") or {}
        if c is None: continue
        for b in json.loads(cr[5] or "[]"):
            if b not in idx or b not in pr: continue
            f=float(legs.get(b) or 0.0)>0.0
            e=agg[(abs(idx[b]-c)>0, f)]; e[0]+=1; e[1]+=(b==sr[3]); e[2]+=float(pr[b]); e[3]+=float(p[b])
for offc in (False,True):
    for f in (True,False):
        n,h,pp,mm=agg[(offc,f)]
        if not n: continue
        print("%-9s %-8s n=%3d settled %5.1f%%  mean ask %.3f  mean model p %.3f  (model/price %.2f)"%(
            "off-centre" if offc else "centre","FILLED" if f else "unfilled",n,100*h/n,pp/n,mm/n,(mm/n)/(pp/n)))
