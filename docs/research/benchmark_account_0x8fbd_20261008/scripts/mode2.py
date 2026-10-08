import json, collections, sys, sqlite3, datetime as dt, time
from zoneinfo import ZoneInfo
sys.path.insert(0,'.')
from lib import *
DB="/Users/xiong/weather/data/bayes_basket_target_ssot.db"
fills,_=load(); reg=json.load(open("registry.json")); tz={r["station_id"]:r["settlement_timezone"] for r in reg}; bycid={r["cid"]:r for r in reg}
labels={}
for r in json.load(open("labels.json")):
    k=(r["station_id"],r["event_date"])
    if k not in labels or r["truth_revision_no"]>labels[k]["truth_revision_no"]: labels[k]=r
lad=collections.defaultdict(list)
for r in reg: lad[(r["station_id"],r["event_date"])].append(r)
for k in lad: lad[k].sort(key=lambda r:(r["lower_c"] if r["lower_c"] is not None else -999))
def ord_label(st,ed,label):
    for i,r in enumerate(lad.get((st,ed),[])):
        if r["outcome"]==label: return i
buys=[f for f in fills if f["side"]=="BUY" and f["station"] and f["event_date"]]
conn=sqlite3.connect(f"file:{DB}?mode=ro",uri=True,isolation_level=None,timeout=30); conn.execute("PRAGMA query_only=1")
t0=time.time(); out=[]
for i,f in enumerate(buys):
    ts_iso=f["utc"].strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    row=conn.execute("select observed_at,buckets_json from basket_official_ws_snapshot where station_id=? and event_date=? and state='COMMITTED' and observed_at<=? order by observed_at desc limit 1",(f["station"],f["event_date"],ts_iso)).fetchone()
    rec=dict(idx=i,mode=None,healthy=False,stale=None,two_sided=0,n=0,spread=None,mid_sum=None,my_ord=ord_label(f["station"],f["event_date"],bycid[f["cid"]]["outcome"]) if f["cid"] in bycid else None)
    if row:
        obs=dt.datetime.fromisoformat(row[0].replace("Z","+00:00")); rec["stale"]=(f["utc"]-obs).total_seconds()
        B=json.loads(row[1]); mids=[]
        for b in B:
            bid,ask=b.get("bid_price"),b.get("best_ask")
            if bid is not None and ask is not None and ask-bid<0.2: mids.append(((bid+ask)/2,-b["ordinal"],b))
        rec["n"]=len(B); rec["two_sided"]=len(mids)
        if mids:
            s=sum(m[0] for m in mids); rec["mid_sum"]=s
            if 0.85<=s<=1.2 and len(mids)>=3:
                rec["healthy"]=True; mode=max(mids)[2]; rec["mode"]=mode["ordinal"]
                mine=[m[2] for m in mids if m[2].get("token_id")==bycid.get(f["cid"],{}).get("yes_token")]
                if mine: rec["spread"]=mine[0]["best_ask"]-mine[0]["bid_price"]; rec["q"]=((mine[0]["bid_price"]+mine[0]["best_ask"])/2)/s
    out.append(rec)
conn.close(); print("elapsed",round(time.time()-t0),"s | fills",len(out),"healthy-ladder modes",sum(r["healthy"] for r in out),"two-sided median",sorted(r["two_sided"] for r in out)[len(out)//2])
json.dump(out,open("fills_mode2.json","w"))
H={r["idx"]:r for r in out}
for side in ("Yes","No"):
    d=collections.Counter()
    for i,f in enumerate(buys):
        r=H[i]
        if f["outcome"]!=side or not r["healthy"] or r["my_ord"] is None or (r["stale"] or 1e9)>1800: continue
        d[max(-3,min(3,r["my_ord"]-r["mode"]))]+=f["size"]
    t=sum(d.values()); print(f"BUY {side} (healthy two-sided ladder, snapshot<=30min): bucket minus market mode share%:",{k:round(100*v/t,1) for k,v in sorted(d.items())},"n_shares",round(t))
# (b) redo with healthy mode at first pre-D0 YES buy
def truth_ord(st,ed):
    lab=labels.get((st,ed)); return ord_label(st,ed,lab["settled_bucket"]) if lab else None
sd=collections.defaultdict(lambda: dict(yes=collections.Counter(), first=None))
for i,f in enumerate(buys):
    z=ZoneInfo(tz.get(f["station"],"UTC")); lt=f["utc"].astimezone(z)
    if (dt.date.fromisoformat(f["event_date"])-lt.date()).days<1 or f["outcome"]!="Yes": continue
    k=(f["station"],f["event_date"]); o=H[i]["my_ord"]
    if o is None: continue
    sd[k]["yes"][o]+=f["size"]*f["price"]
    if sd[k]["first"] is None and H[i]["healthy"] and (H[i]["stale"] or 1e9)<=1800: sd[k]["first"]=i
M={r["idx"]:r for r in json.load(open("fills_model.json"))["fills"]}
hit=collections.Counter(); n=0
for k,d in sd.items():
    if d["first"] is None: continue
    tr=truth_ord(*k); mk=H[d["first"]]["mode"]; m=M.get(d["first"],{}).get("model",{}).get("P0")
    if tr is None or mk is None or not m or m.get("mode_ord") is None: continue
    their=max(d["yes"].items(),key=lambda x:x[1])[0]; n+=1
    hit["their"]+=their==tr; hit["market"]+=mk==tr; hit["ourP0"]+=m["mode_ord"]==tr
    if their!=mk: hit["dis n"]+=1; hit["dis their"]+=their==tr; hit["dis market"]+=mk==tr; hit["dis ourP0"]+=m["mode_ord"]==tr
print(f"(b) healthy-ladder station-days n={n}: their favoured YES hit {hit['their']/n:.3f} | market mode hit {hit['market']/n:.3f} | our P0 mode hit {hit['ourP0']/n:.3f}")
dn=hit["dis n"]; print(f"    their≠market (n={dn}): their right {hit['dis their']/dn:.3f} market right {hit['dis market']/dn:.3f} ourP0 right {hit['dis ourP0']/dn:.3f}")
