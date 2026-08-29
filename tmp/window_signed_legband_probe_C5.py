import json, sqlite3, collections
conn=sqlite3.connect("file:data/bayes_basket_target_ssot.db?mode=ro",uri=True,timeout=30)
pos=collections.defaultdict(list)
for r in conn.execute("select station_id,event_date,stage,decision_at_utc,ladder_center_bucket,posted_buckets_json,window_buckets_json,probabilities_json,leg_shares_json from paper_trade_position"):
    pos[(r[0],r[1])].append(r)
setl={(r[0],r[1]):r for r in conn.execute("select station_id,event_date,stage,settled_bucket,hit from paper_trade_settlement")}
band=collections.defaultdict(lambda:[0,0]); postband=collections.defaultdict(lambda:[0,0])
daymass=collections.defaultdict(float); dayreal=collections.Counter(); days=0
for k,sr in sorted(setl.items()):
    cands=sorted(pos.get(k) or [],key=lambda r:r[3])
    if not cands: continue
    days+=1
    for cr in cands:
        p=json.loads(cr[7]); order=list(p); idx={b:i for i,b in enumerate(order)}
        c=idx.get(cr[4])
        if c is None: continue
        for b,q in (json.loads(cr[8] or "{}") or {}).items():
            if b not in idx or float(q)<=0: continue
            d=max(-3,min(3,idx[b]-c)); e=band[d]; e[0]+=1; e[1]+=(b==sr[3])
        for b in json.loads(cr[5] or "[]"):
            if b not in idx: continue
            d=max(-3,min(3,idx[b]-c)); e=postband[d]; e[0]+=1; e[1]+=(b==sr[3])
    r=cands[0]; p=json.loads(r[7]); order=list(p); idx={b:i for i,b in enumerate(order)}
    c=idx.get(r[4]); s=idx.get(sr[3])
    if c is None: continue
    for b,v in p.items():
        daymass[max(-3,min(3,idx[b]-c))]+=v
    if s is not None: dayreal[max(-3,min(3,s-c))]+=1
print("signed distance from ladder centre (+ = truth hotter than centre)\n")
print(" d   bought_legs  leg_hit%   posted_rungs  posted_hit%   model_mass/day  truth_days  truth%")
for d in range(-3,4):
    n,h=band[d]; pn,ph=postband[d]
    print("%3d  %6d %9s   %8d %10s   %10.3f %10d %7.1f%%"%(
        d,n,("%.1f"%(100*h/n)) if n else "-",pn,("%.1f"%(100*ph/pn)) if pn else "-",
        daymass[d]/days, dayreal[d], 100*dayreal[d]/days))
