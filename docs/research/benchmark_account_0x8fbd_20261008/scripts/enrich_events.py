import json, sqlite3, collections, datetime as dt, sys, time, bisect, glob
sys.path.insert(0,'.')
DB="/Users/xiong/weather/data/bayes_basket_target_ssot.db"
D=json.load(open("cluster_events.json")); fills=D["fills"]
reg=json.load(open("registry.json")); bycid={r["cid"]:r for r in reg}
lad=collections.defaultdict(list)
for r in reg: lad[(r["station_id"],r["event_date"])].append(r)
for k in lad: lad[k].sort(key=lambda r:(r["lower_c"] if r["lower_c"] is not None else -999))
def ord_label(st,ed,label):
    for i,r in enumerate(lad.get((st,ed),[])):
        if r["outcome"]==label: return i
labels={}
for r in json.load(open("labels.json")):
    k=(r["station_id"],r["event_date"])
    if k not in labels or r["truth_revision_no"]>labels[k]["truth_revision_no"]: labels[k]=r
H={}
for p in glob.glob("hist_b*.json")+["hist.json"]: H.update(json.load(open(p)))
S={cid:([h["t"] for h in hs],[h["p"] for h in hs]) for cid,hs in H.items()}
def mid(cid,ts):
    if cid not in S: return None
    T,P=S[cid]; i=bisect.bisect_right(T,ts)-1
    return P[i] if i>=0 and ts-T[i]<1800 else None
# episodes
by=collections.defaultdict(list)
for f in fills: by[(f["cid"],f["outcome"])].append(f)
eps=[]
for k,L in by.items():
    L.sort(key=lambda f:f["ts"]); ep=[L[0]]
    for f in L[1:]:
        if f["ts"]-ep[-1]["ts"]>3600: eps.append(ep); ep=[f]
        else: ep.append(f)
    eps.append(ep)
conn=sqlite3.connect(f"file:{DB}?mode=ro",uri=True,isolation_level=None,timeout=30); conn.execute("PRAGMA query_only=1")
hist={}; probs={}
def H_(st,ed):
    if (st,ed) not in hist: hist[(st,ed)]=conn.execute("select history_id,stage,decision_at_utc from basket_probability_bridge_history where station_id=? and event_date=? and state='COMMITTED' order by decision_at_utc",(st,ed)).fetchall()
    return hist[(st,ed)]
def P_(hid):
    if hid not in probs: probs[hid]={r[0]:r[1] for r in conn.execute("select ordinal,probability from basket_probability_bridge_bucket_probability where history_id=?",(hid,))}
    return probs[hid]
t0=time.time(); out=[]
for ep in eps:
    f0=ep[0]; st,ed=f0["station"],f0["event_date"]; r=bycid.get(f0["cid"])
    if not r: continue
    my=ord_label(st,ed,r["outcome"]); lab=labels.get((st,ed))
    if my is None: continue
    tro=ord_label(st,ed,lab["settled_bucket"]) if lab else None
    utc=dt.datetime.fromtimestamp(f0["ts"],dt.timezone.utc); ts_iso=utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    rec=dict(cid=f0["cid"],station=st,event_date=ed,outcome=f0["outcome"],my_ord=my,truth_ord=tro,ts=f0["ts"],first_taker=f0["taker"],first_w=f0["w"][:10],first_price=f0["price"],
             usdc=sum(f["size"]*f["price"] for f in ep),sh=sum(f["size"] for f in ep),paid=sum(f["size"]*f["price"] for f in ep)/sum(f["size"] for f in ep),n=len(ep),nw=len(set(f["w"] for f in ep)),tk_share=sum(f["size"]*f["price"] for f in ep if f["taker"])/sum(f["size"]*f["price"] for f in ep),
             lead_h=(dt.datetime.fromisoformat(ed).replace(tzinfo=dt.timezone.utc)+dt.timedelta(hours=12)-utc).total_seconds()/3600,unit=f0["unit"],hour=utc.hour,mid0=mid(f0["cid"],f0["ts"]),market=None,p0=None)
    row=conn.execute("select observed_at,buckets_json from basket_official_ws_snapshot where station_id=? and event_date=? and state='COMMITTED' and observed_at<=? order by observed_at desc limit 1",(st,ed,ts_iso)).fetchone()
    if row:
        stale=(utc-dt.datetime.fromisoformat(row[0].replace("Z","+00:00"))).total_seconds()
        if stale<=1800:
            B=json.loads(row[1]); q={}; bid={}; ask={}; bsh={}; ash={}
            for b in B:
                bb,aa=b.get("bid_price"),b.get("best_ask")
                if bb is not None and aa is not None and aa-bb<0.2: q[b["ordinal"]]=(bb+aa)/2; bid[b["ordinal"]]=bb; ask[b["ordinal"]]=aa; bsh[b["ordinal"]]=(b.get("bid_levels") or [{}])[0].get("shares"); ash[b["ordinal"]]=(b.get("ask_levels") or [{}])[0].get("shares")
            s=sum(q.values())
            if len(q)>=3 and 0.85<=s<=1.2:
                items=sorted(q.items()); mode=max(items,key=lambda kv:(kv[1],-kv[0]))[0]; mean=sum(o*v for o,v in items)/s
                rec["market"]=dict(q_my=q.get(my,0)/s,q_mode=q[mode]/s,d=my-mode,skew=mean-mode,spread=(ask[my]-bid[my]) if my in ask else None,ask_sh=ash.get(my),bid_sh=bsh.get(my),n2=len(q),mode=mode)
    hh=H_(st,ed); last=None
    for hid,stage,dec in hh:
        if dec<=ts_iso: last=(hid,stage)
    if last:
        P=P_(last[0]); tot=sum(P.values()) or 1; rec["p0"]=dict(stage=last[1],p_my=P.get(my,0)/tot,p_mode_mkt=(P.get(rec["market"]["mode"],0)/tot) if rec["market"] else None)
    out.append(rec)
conn.close(); json.dump(out,open("events_enriched.json","w"))
print("episodes enriched",len(out),"| with healthy market",sum(1 for r in out if r["market"]),"| with p0",sum(1 for r in out if r["p0"]),"| with truth",sum(1 for r in out if r["truth_ord"] is not None),"| elapsed",round(time.time()-t0),"s")
