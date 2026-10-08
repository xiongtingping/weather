import json, collections, statistics as st, datetime as dt, sys
sys.path.insert(0,'.')
from lib import *
C=json.load(open("cluster.json"))
labels={}
for r in json.load(open("labels.json")):
    k=(r["station_id"],r["event_date"])
    if k not in labels or r["truth_revision_no"]>labels[k]["truth_revision_no"]: labels[k]=r
def norm(t):
    city,ed=parse_event(t.get("eventSlug") or ""); label,lo,hi,unit,kind=parse_bucket(t.get("title") or "")
    return dict(ts=t["timestamp"],city=city,station=CITY2ST.get(city),event_date=ed,bucket=label,lo=lo,hi=hi,unit=unit,outcome=t["outcome"],side=t["side"],price=float(t["price"]),size=float(t["size"]),asset=t["asset"],cid=t["conditionId"],tx=t["transactionHash"])
def won(f):
    lab=labels.get((f["station"],f["event_date"]))
    if not lab or f["lo"] is None and f["hi"] is None: return None
    t=lab["settled_tmax_c"]; t=t*9/5+32 if f["unit"]=="F" else t
    lo,hi=f["lo"],f["hi"]; return (lo is None or t>=lo-0.5) and (hi is None or t<hi+0.5)
tot=collections.Counter()
print(f"{'wallet':12s} {'fills':>6s} {'taker%':>6s} {'buyUSDC':>8s} {'YES%':>5s} {'sell998':>8s} {'merges':>7s} {'reward':>7s} {'resolved pnl':>12s} {'ret%':>6s} {'stations':>8s} {'first':>10s} {'last':>10s}")
for w,d in C.items():
    fills=[norm(t) for t in d["fills"]]; tk=collections.Counter((t["transactionHash"],t["asset"],t["side"],str(t["price"]),str(t["size"]),t["timestamp"]) for t in d["taker"])
    for f in fills:
        k=(f["tx"],f["asset"],f["side"],str(f["price"]),str(f["size"]),f["ts"]); f["taker"]=tk.get(k,0)>0
        if f["taker"]: tk[k]-=1
    buys=[f for f in fills if f["side"]=="BUY"]; sells=[f for f in fills if f["side"]=="SELL"]
    merges=sum(float(a.get("size") or 0) for a in d["activity"] if a["type"]=="MERGE"); reward=sum(float(a.get("usdcSize") or 0) for a in d["activity"] if a["type"] in ("REWARD","MAKER_REBATE","TAKER_REBATE"))
    # per-token ledger
    led=collections.defaultdict(lambda:[0.0,0.0,0.0,None])
    for f in fills:
        L=led[f["asset"]]; L[3]=f
        if f["side"]=="BUY": L[0]+=f["size"]; L[1]+=f["size"]*f["price"]
        else: L[2]+=f["size"]*f["price"]; L[0]-=f["size"]
    mg=collections.defaultdict(float)
    for a in d["activity"]:
        if a["type"]=="MERGE": mg[a["conditionId"]]+=float(a.get("size") or 0)
    pnl=0.0; cost_res=0.0; n_res=0
    bycid=collections.defaultdict(dict)
    for asset,(hold,cost,proc,f) in led.items(): bycid[f["cid"]][f["outcome"]]=(hold,cost,proc,f)
    for cid,sides in bycid.items():
        f=list(sides.values())[0][3]; w_=won(f)
        if w_ is None or (f["station"],f["event_date"]) not in labels: continue
        m=mg.get(cid,0.0); cash=m
        for o,(hold,cost,proc,ff) in sides.items():
            h=hold-m; cash+=proc-cost
            if h>1e-6: cash+=h*(1.0 if ((w_) if o=="Yes" else (not w_)) else 0.0)
            cost_res+=cost
        pnl+=cash; n_res+=1
    buy_usdc=sum(f["size"]*f["price"] for f in buys); yes=sum(f["size"]*f["price"] for f in buys if f["outcome"]=="Yes")
    tkshare=sum(f["size"]*f["price"] for f in buys if f["taker"])/buy_usdc if buy_usdc else 0
    print(f"{w[:12]:12s} {len(fills):6d} {100*tkshare:6.0f} {buy_usdc:8.0f} {100*yes/max(buy_usdc,1):5.0f} {sum(f['size']*f['price'] for f in sells):8.0f} {merges:7.0f} {reward:7.0f} {pnl:12.0f} {100*pnl/max(cost_res,1):6.1f} {len(set(f['station'] for f in buys if f['station'])):8d} {dt.datetime.fromtimestamp(min(f['ts'] for f in fills),dt.timezone.utc).strftime('%m-%d %H:%M'):>10s} {dt.datetime.fromtimestamp(max(f['ts'] for f in fills),dt.timezone.utc).strftime('%m-%d %H:%M'):>10s}")
    tot["buy"]+=buy_usdc; tot["pnl"]+=pnl; tot["cost_res"]+=cost_res; tot["reward"]+=reward; tot["fills"]+=len(fills)
print(f"CLUSTER (fetched wallets): fills {tot['fills']} buy {tot['buy']:.0f} resolved pnl {tot['pnl']:.0f} ({100*tot['pnl']/max(tot['cost_res'],1):.1f}% of resolved cost) rewards {tot['reward']:.0f}")
