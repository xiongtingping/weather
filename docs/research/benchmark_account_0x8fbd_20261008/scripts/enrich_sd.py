import json, sqlite3, collections, datetime as dt, sys, time
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
# group cluster buys per station-day, and also split by "session" (first 6h after first fill) for the as-of snapshot
sd=collections.defaultdict(list)
for f in fills:
    if f["cid"] in bycid: sd[(f["station"],f["event_date"])].append(f)
conn=sqlite3.connect(f"file:{DB}?mode=ro",uri=True,isolation_level=None,timeout=30); conn.execute("PRAGMA query_only=1")
def snapshot(st,ed,ts_iso):
    row=conn.execute("select observed_at,buckets_json from basket_official_ws_snapshot where station_id=? and event_date=? and state='COMMITTED' and observed_at<=? order by observed_at desc limit 1",(st,ed,ts_iso)).fetchone()
    if not row: return None
    B=json.loads(row[1]); q={}; bid={}; ask={}
    for b in B:
        bb,aa=b.get("bid_price"),b.get("best_ask")
        if bb is not None and aa is not None and aa-bb<0.2: q[b["ordinal"]]=(bb+aa)/2; bid[b["ordinal"]]=bb; ask[b["ordinal"]]=aa
    s=sum(q.values())
    if len(q)<3 or not 0.85<=s<=1.2: return None
    return dict(obs=row[0],q={o:v/s for o,v in q.items()},bid=bid,ask=ask,n=len(B))
def ours(st,ed,ts_iso):
    h=conn.execute("select history_id,stage from basket_probability_bridge_history where station_id=? and event_date=? and state='COMMITTED' and decision_at_utc<=? order by decision_at_utc desc limit 1",(st,ed,ts_iso)).fetchone()
    if not h: return None
    P={r[0]:r[1] for r in conn.execute("select ordinal,probability from basket_probability_bridge_bucket_probability where history_id=?",(h[0],))}
    t=sum(P.values()); return dict(stage=h[1],p={o:v/t for o,v in P.items()})
t0=time.time(); out=[]
for (st,ed),L in sd.items():
    L.sort(key=lambda f:f["ts"]); lab=labels.get((st,ed))
    # windows: at first fill, and at first fill of each new "day session" (gap>6h)
    sessions=[]; cur=[L[0]]
    for f in L[1:]:
        if f["ts"]-cur[-1]["ts"]>6*3600: sessions.append(cur); cur=[f]
        else: cur.append(f)
    sessions.append(cur)
    for S in sessions:
        f0=S[0]; utc=dt.datetime.fromtimestamp(f0["ts"],dt.timezone.utc); ts_iso=utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        snap=snapshot(st,ed,ts_iso)
        if not snap: continue
        stale=(utc-dt.datetime.fromisoformat(snap["obs"].replace("Z","+00:00"))).total_seconds()
        if stale>1800: continue
        yes=collections.Counter(); no=collections.Counter(); yes_sh=collections.Counter(); no_sh=collections.Counter(); ypaid=collections.defaultdict(float); npaid=collections.defaultdict(float)
        for f in S:
            o=ord_label(st,ed,bycid[f["cid"]]["outcome"])
            if o is None: continue
            if f["outcome"]=="Yes": yes[o]+=f["size"]*f["price"]; yes_sh[o]+=f["size"]
            else: no[o]+=f["size"]*f["price"]; no_sh[o]+=f["size"]
        out.append(dict(station=st,event_date=ed,ts=f0["ts"],lead_h=(dt.datetime.fromisoformat(ed).replace(tzinfo=dt.timezone.utc)+dt.timedelta(hours=12)-utc).total_seconds()/3600,unit=f0["unit"],
                        truth_ord=ord_label(st,ed,lab["settled_bucket"]) if lab else None,q=snap["q"],bid=snap["bid"],ask=snap["ask"],ours=ours(st,ed,ts_iso),
                        yes_usdc=dict(yes),no_usdc=dict(no),yes_sh=dict(yes_sh),no_sh=dict(no_sh),n_fills=len(S),n_wallets=len(set(f["w"] for f in S))))
conn.close(); json.dump(out,open("sd_sessions.json","w"))
print("sessions",len(out),"| station-days",len(set((r['station'],r['event_date']) for r in out)),"| with truth",sum(1 for r in out if r["truth_ord"] is not None),"| with ours",sum(1 for r in out if r["ours"]),"| elapsed",round(time.time()-t0),"s")
