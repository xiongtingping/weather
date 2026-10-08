import json, sqlite3, collections, datetime as dt, sys, time
sys.path.insert(0,'.')
from lib import *
DB="/Users/xiong/weather/data/bayes_basket_target_ssot.db"
fills,_=load(); reg=json.load(open("registry.json")); bycid={r["cid"]:r for r in reg}
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
hist={}; probs={}
def H(st,ed):
    if (st,ed) not in hist: hist[(st,ed)]=conn.execute("select history_id,stage,decision_at_utc from basket_probability_bridge_history where station_id=? and event_date=? and state='COMMITTED' order by decision_at_utc",(st,ed)).fetchall()
    return hist[(st,ed)]
def P(hid):
    if hid not in probs: probs[hid]={r[0]:r[1] for r in conn.execute("select ordinal,probability from basket_probability_bridge_bucket_probability where history_id=?",(hid,))}
    return probs[hid]
t0=time.time(); out=[]
for i,f in enumerate(buys):
    ts_iso=f["utc"].strftime("%Y-%m-%dT%H:%M:%S.%fZ"); st,ed=f["station"],f["event_date"]
    rec=dict(idx=i,ts=f["ts"],station=st,event_date=ed,unit=f["unit"],outcome=f["outcome"],price=f["price"],size=f["size"],taker=f["taker"],
             my_ord=ord_label(st,ed,bycid[f["cid"]]["outcome"]) if f["cid"] in bycid else None,
             truth_ord=ord_label(st,ed,labels[(st,ed)]["settled_bucket"]) if (st,ed) in labels else None,
             n_ladder=len(lad.get((st,ed),[])),
             lead_h=(dt.datetime.fromisoformat(ed).replace(tzinfo=dt.timezone.utc)+dt.timedelta(hours=12)-f["utc"]).total_seconds()/3600,
             market=None, model={})
    row=conn.execute("select observed_at,buckets_json from basket_official_ws_snapshot where station_id=? and event_date=? and state='COMMITTED' and observed_at<=? order by observed_at desc limit 1",(st,ed,ts_iso)).fetchone()
    if row:
        stale=(f["utc"]-dt.datetime.fromisoformat(row[0].replace("Z","+00:00"))).total_seconds()
        if stale<=1800:
            B=json.loads(row[1]); q={}; bid={}; ask={}
            for b in B:
                bb,aa=b.get("bid_price"),b.get("best_ask")
                if bb is not None and aa is not None and aa-bb<0.2: q[b["ordinal"]]=(bb+aa)/2; bid[b["ordinal"]]=bb; ask[b["ordinal"]]=aa
            s=sum(q.values())
            if len(q)>=3 and 0.85<=s<=1.2:
                rec["market"]=dict(stale=stale,q={k:v/s for k,v in q.items()},bid=bid,ask=ask,sum=s)
    for hid,stage,dec in H(st,ed):
        if dec<=ts_iso: rec["model"][stage]=hid
    for stage,hid in list(rec["model"].items()): rec["model"][stage]=dict(dec=None,p=P(hid))
    out.append(rec)
conn.close()
json.dump(out,open("fills_full.json","w"))
print("fills",len(out),"with healthy market",sum(1 for r in out if r["market"]),"with P0",sum(1 for r in out if "P0" in r["model"]),"elapsed",round(time.time()-t0),"s")
