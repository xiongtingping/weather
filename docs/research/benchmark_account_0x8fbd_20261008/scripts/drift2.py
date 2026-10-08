import json, collections, sys, glob, bisect, datetime as dt
sys.path.insert(0,'.')
from lib import *
fills,acts=load()
H={}
for p in glob.glob("hist*.json"): H.update(json.load(open(p)))
labels={}
for r in json.load(open("labels.json")):
    k=(r["station_id"],r["event_date"])
    if k not in labels or r["truth_revision_no"]>labels[k]["truth_revision_no"]: labels[k]=r
def won(f):
    lab=labels.get((f["station"],f["event_date"]))
    if not lab: return None
    t=lab["settled_tmax_c"]
    if f["unit"]=="F": t=t*9/5+32
    lo,hi=f["lo"],f["hi"]; return (lo is None or t>=lo-0.5) and (hi is None or t<hi+0.5)
series={cid:([h["t"] for h in hs],[h["p"] for h in hs]) for cid,hs in H.items()}
def mid_at(cid,ts,maxage=1800):
    T,P=series[cid]; i=bisect.bisect_right(T,ts)-1
    if i<0 or ts-T[i]>maxage: return None
    return P[i]
def mid_after(cid,ts,dh):
    T,P=series[cid]; i=bisect.bisect_right(T,ts+dh*3600)-1
    return P[i] if i>=0 and T[i]>ts else None
buys=[f for f in fills if f["side"]=="BUY" and f["cid"] in series and f["station"]]
rows=[]
for f in buys:
    m=mid_at(f["cid"],f["ts"])
    if m is None: continue
    w=won(f)
    if w is None: continue
    sgn=1 if f["outcome"]=="Yes" else -1
    pyes=f["price"] if f["outcome"]=="Yes" else 1-f["price"]
    lead=(dt.datetime.fromisoformat(f["event_date"]).replace(tzinfo=dt.timezone.utc)+dt.timedelta(hours=12)-f["utc"]).total_seconds()/3600
    rows.append(dict(f=f,m=m,sgn=sgn,pyes=pyes,w=w,lead=lead,b1=mid_at(f["cid"],f["ts"]-3600,100000),b10=mid_at(f["cid"],f["ts"]-600,100000),d1=mid_after(f["cid"],f["ts"],1),d6=mid_after(f["cid"],f["ts"],6),d24=mid_after(f["cid"],f["ts"],24)))
print("fills with mid & label:",len(rows),"| markets with history:",len(series))
def W(rs,fn):
    x=[(r["f"]["size"],fn(r)) for r in rs]; x=[(s,v) for s,v in x if v is not None]
    return (sum(s*v for s,v in x)/sum(s for s,_ in x)) if x else float("nan")
def report(tag,rs):
    if len(rs)<30: return
    paid=W(rs,lambda r:r["f"]["price"]); mid=W(rs,lambda r:(r["m"] if r["sgn"]>0 else 1-r["m"]))
    hit=W(rs,lambda r:1.0 if ((r["w"]) if r["sgn"]>0 else (not r["w"])) else 0.0)
    pre1=W(rs,lambda r:None if r["b1"] is None else r["sgn"]*(r["m"]-r["b1"])); pre10=W(rs,lambda r:None if r["b10"] is None else r["sgn"]*(r["m"]-r["b10"]))
    d1=W(rs,lambda r:None if r["d1"] is None else r["sgn"]*(r["d1"]-r["m"])); d6=W(rs,lambda r:None if r["d6"] is None else r["sgn"]*(r["d6"]-r["m"])); d24=W(rs,lambda r:None if r["d24"] is None else r["sgn"]*(r["d24"]-r["m"]))
    print(f"{tag:34s} n={len(rs):5d} paid={paid:.3f} mid={mid:.3f} hit={hit:.3f} | spread_capture(mid-paid)={mid-paid:+.3f} info(hit-mid)={hit-mid:+.3f} total(hit-paid)={hit-paid:+.3f} | pre-move -1h {pre1:+.3f} -10m {pre10:+.3f} | post +1h {d1:+.3f} +6h {d6:+.3f} +24h {d24:+.3f}")
for side,sg in (("Yes",1),("No",-1)):
    for role in (True,False):
        rs=[r for r in rows if r["sgn"]==sg and r["f"]["taker"]==role]
        report(f"BUY {side} {'taker' if role else 'maker'}",rs)
print("--- by horizon (hours before event-day 12Z), taker vs maker, both sides pooled ---")
for a,b in [(-24,0),(0,12),(12,24),(24,36),(36,48),(48,200)]:
    for role in (True,False):
        rs=[r for r in rows if a<=r["lead"]<b and r["f"]["taker"]==role]
        report(f"  {a:4d}..{b:3d}h {'taker' if role else 'maker'}",rs)
print("--- NO on their own held-YES bucket (exit) vs other NO: taker only ---")
# exit classification as before
byc=collections.defaultdict(list)
for f in buys: byc[f["cid"]].append(("F",f["ts"],f))
for a in acts:
    if a["type"]=="MERGE": byc[a["cid"]].append(("M",a["ts"],a))
exit_ts=set()
for cid,evs in byc.items():
    evs.sort(key=lambda e:e[1]); net=0
    for k,ts,x in evs:
        if k=="M": net-=x["size"]; continue
        if x["outcome"]=="Yes": net+=x["size"]
        elif net>1e-6: exit_ts.add((x["asset"],x["ts"],x["price"],x["size"]))
isexit=lambda r:(r["f"]["asset"],r["f"]["ts"],r["f"]["price"],r["f"]["size"]) in exit_ts
for role in (True,False):
    report(f"  NO exit-of-YES {'taker' if role else 'maker'}",[r for r in rows if r["sgn"]<0 and r["f"]["taker"]==role and isexit(r)])
    report(f"  NO directional {'taker' if role else 'maker'}",[r for r in rows if r["sgn"]<0 and r["f"]["taker"]==role and not isexit(r)])
# total P&L decomposition in USDC on these fills (hold-to-settle counterfactual)
sc=sum(r["f"]["size"]*((r["m"] if r["sgn"]>0 else 1-r["m"])-r["f"]["price"]) for r in rows)
info=sum(r["f"]["size"]*((1.0 if ((r["w"]) if r["sgn"]>0 else (not r["w"])) else 0.0)-(r["m"] if r["sgn"]>0 else 1-r["m"])) for r in rows)
print(f"USDC decomposition over {len(rows)} fills (hold-to-settle): spread capture {sc:+.0f} | information/selection {info:+.0f} | total {sc+info:+.0f}")
for role in (True,False):
    rs=[r for r in rows if r["f"]["taker"]==role]
    sc=sum(r["f"]["size"]*((r["m"] if r["sgn"]>0 else 1-r["m"])-r["f"]["price"]) for r in rs)
    info=sum(r["f"]["size"]*((1.0 if ((r["w"]) if r["sgn"]>0 else (not r["w"])) else 0.0)-(r["m"] if r["sgn"]>0 else 1-r["m"])) for r in rs)
    print(f"   {'taker' if role else 'maker'}: spread {sc:+.0f} | information {info:+.0f} | cost {sum(r['f']['size']*r['f']['price'] for r in rs):.0f}")
