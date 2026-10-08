import json, sqlite3, collections, sys, datetime as dt, time
sys.path.insert(0,'.')
from lib import *
DB="/Users/xiong/weather/data/bayes_basket_target_ssot.db"
fills,acts=load(); reg=json.load(open("registry.json")); bycid={r["cid"]:r for r in reg}
buys=[f for f in fills if f["side"]=="BUY" and f["station"] and f["event_date"]]
conn=sqlite3.connect(f"file:{DB}?mode=ro",uri=True,isolation_level=None,timeout=30); conn.execute("PRAGMA query_only=1")
hist={}; probs={}
def H(st,ed):
    if (st,ed) not in hist: hist[(st,ed)]=conn.execute("select history_id,stage,decision_at_utc from basket_probability_bridge_history where station_id=? and event_date=? and state='COMMITTED' order by decision_at_utc",(st,ed)).fetchall()
    return hist[(st,ed)]
def P(hid):
    if hid not in probs: probs[hid]={b:p for b,p in conn.execute("select bucket,probability from basket_probability_bridge_bucket_probability where history_id=?",(hid,))}
    return probs[hid]
t0=time.time()
acc=collections.defaultdict(lambda:[0.0,0.0,0])  # key (stage, role, side) -> [sum size*dp, sum size, n]
acc_h=collections.defaultdict(lambda:[0.0,0.0,0])
for f in buys:
    r=bycid.get(f["cid"]); 
    if not r: continue
    label=r["outcome"]; ts_iso=f["utc"].strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    rows=H(f["station"],f["event_date"])
    for stage in ("P0","P0-M","P1"):
        prev=[x for x in rows if x[1]==stage and x[2]<=ts_iso]; nxt=[x for x in rows if x[1]==stage and x[2]>ts_iso]
        if not prev or not nxt: continue
        p0=P(prev[-1][0]).get(label); p1=P(nxt[0][0]).get(label)
        if p0 is None or p1 is None: continue
        sgn=1 if f["outcome"]=="Yes" else -1
        gap_h=(dt.datetime.fromisoformat(nxt[0][2].replace("Z","+00:00"))-f["utc"]).total_seconds()/3600
        k=(stage,"taker" if f["taker"] else "maker",f["outcome"]); a=acc[k]; a[0]+=f["size"]*sgn*(p1-p0); a[1]+=f["size"]; a[2]+=1
        if gap_h<=6:
            a=acc_h[k]; a[0]+=f["size"]*sgn*(p1-p0); a[1]+=f["size"]; a[2]+=1
conn.close()
print("elapsed",round(time.time()-t0),"s")
print("Does our model's NEXT update of the same stage move toward their side? share-weighted Δp (sign-adjusted: + = toward them)")
for k in sorted(acc):
    a=acc[k]; b=acc_h[k]
    print(f"  {k[0]:5s} {k[1]:5s} {k[2]:3s}: any-gap n={a[2]:5d} Δp={a[0]/a[1]:+.4f} | next within 6h n={b[2]:5d} Δp={(b[0]/b[1] if b[1] else float('nan')):+.4f}")
