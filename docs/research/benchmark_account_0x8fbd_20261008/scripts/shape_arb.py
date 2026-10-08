import json, collections, sys, sqlite3, math, datetime as dt
sys.path.insert(0,'.')
from lib import *
DB="/Users/xiong/weather/data/bayes_basket_target_ssot.db"
fills,_=load(); reg=json.load(open("registry.json")); bycid={r["cid"]:r for r in reg}
labels={}
for r in json.load(open("labels.json")):
    k=(r["station_id"],r["event_date"])
    if k not in labels or r["truth_revision_no"]>labels[k]["truth_revision_no"]: labels[k]=r
buys=[f for f in fills if f["side"]=="BUY" and f["station"] and f["event_date"]]
conn=sqlite3.connect(f"file:{DB}?mode=ro",uri=True,isolation_level=None,timeout=30); conn.execute("PRAGMA query_only=1")
def fit_fair(mids, exclude):
    """mids: list of (ordinal, q) normalized two-sided mids. Fit discretized normal (mu, sigma) on buckets != exclude by least squares on q; return fitted mass at exclude."""
    pts=[(o,q) for o,q in mids if o!=exclude]
    if len(pts)<3: return None
    # moment fit
    s=sum(q for _,q in pts); mu=sum(o*q for o,q in pts)/s; var=sum(((o-mu)**2)*q for o,q in pts)/s
    sig=max(math.sqrt(var),0.6)
    def cdf(x): return 0.5*(1+math.erf((x-mu)/(sig*math.sqrt(2))))
    tot=sum(cdf(o+0.5)-cdf(o-0.5) for o,_ in pts)+(cdf(exclude+0.5)-cdf(exclude-0.5))
    return (cdf(exclude+0.5)-cdf(exclude-0.5))/tot
rows=[]
for i,f in enumerate(buys):
    if f["outcome"]!="Yes": continue
    ts_iso=f["utc"].strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    row=conn.execute("select observed_at,buckets_json from basket_official_ws_snapshot where station_id=? and event_date=? and state='COMMITTED' and observed_at<=? order by observed_at desc limit 1",(f["station"],f["event_date"],ts_iso)).fetchone()
    if not row: continue
    stale=(f["utc"]-dt.datetime.fromisoformat(row[0].replace("Z","+00:00"))).total_seconds()
    if stale>300: continue
    B=json.loads(row[1]); two=[]
    for b in B:
        bid,ask=b.get("bid_price"),b.get("best_ask")
        if bid is not None and ask is not None and ask-bid<0.2: two.append((b["ordinal"],(bid+ask)/2,b))
    if len(two)<4: continue
    s=sum(m for _,m,_ in two)
    if not 0.85<=s<=1.2: continue
    mids=[(o,m/s) for o,m,_ in two]
    tok=bycid.get(f["cid"],{}).get("yes_token"); mine=[b for o,m,b in two if b.get("token_id")==tok]
    if not mine: continue
    o=mine[0]["ordinal"]; fair=fit_fair(mids,o)
    if fair is None: continue
    q=[m for oo,m in mids if oo==o][0]
    lab=labels.get((f["station"],f["event_date"])); win=None
    if lab:
        t=lab["settled_tmax_c"]; t=t*9/5+32 if f["unit"]=="F" else t
        lo,hi=f["lo"],f["hi"]; win=(lo is None or t>=lo-0.5) and (hi is None or t<hi+0.5)
    rows.append(dict(taker=f["taker"],paid=f["price"],q=q,fair=fair,ask=mine[0]["best_ask"],bid=mine[0]["bid_price"],win=win,size=f["size"],gap=fair-q))
    # control: other buckets in same ladder (not bought now) -> their fair-q gap
    for oo,m in mids:
        if oo==o: continue
        fo=fit_fair(mids,oo)
        if fo is not None: rows.append(dict(taker=None,paid=None,q=m,fair=fo,gap=fo-m,size=1,win=None))
conn.close()
def W(rs,fn): 
    x=[(r["size"],fn(r)) for r in rs if fn(r) is not None]; return sum(s*v for s,v in x)/sum(s for s,_ in x) if x else float('nan')
for role,name in ((True,"taker YES"),(False,"maker YES"),(None,"control: other buckets same ladder")):
    rs=[r for r in rows if r["taker"]==role]
    if not rs: continue
    line=f"{name:36s} n={len(rs):5d} | normalized mid q={W(rs,lambda r:r['q']):.3f} fitted fair={W(rs,lambda r:r['fair']):.3f} gap(fair-q)={W(rs,lambda r:r['gap']):+.3f}"
    if role is not None:
        line+=f" | paid={W(rs,lambda r:r['paid']):.3f} ask={W(rs,lambda r:r['ask']):.3f} hit={W(rs,lambda r:(1.0 if r['win'] else 0.0) if r['win'] is not None else None):.3f}"
    print(line)
# gap distribution for taker fills: fraction with fair>q by >0.02
tk=[r for r in rows if r["taker"]==True]; ct=[r for r in rows if r["taker"] is None]
print("taker YES: share with fitted fair > normalized mid by >2c:",round(sum(r['size'] for r in tk if r['gap']>0.02)/sum(r['size'] for r in tk),3)," | control buckets:",round(sum(1 for r in ct if r['gap']>0.02)/len(ct),3))
