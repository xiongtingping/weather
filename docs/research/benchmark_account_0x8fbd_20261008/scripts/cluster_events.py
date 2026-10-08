import json, collections, sys, random, datetime as dt
sys.path.insert(0,'.')
from lib import *
USER="0x8fbd7cf5f806f563080864694415829f7229a959"
C=json.load(open("cluster.json")); fills,_=load()
allf=[]
for f in fills:
    if f["side"]=="BUY": allf.append(dict(w=USER,ts=f["ts"],cid=f["cid"],outcome=f["outcome"],price=f["price"],size=f["size"],taker=f["taker"],station=f["station"],event_date=f["event_date"],bucket=f["bucket"],lo=f["lo"],hi=f["hi"],unit=f["unit"],tx=f["tx"]))
for w,d in C.items():
    tk=collections.Counter((t["transactionHash"],t["asset"],t["side"],str(t["price"]),str(t["size"]),t["timestamp"]) for t in d["taker"])
    for t in d["fills"]:
        if t["side"]!="BUY": continue
        k=(t["transactionHash"],t["asset"],t["side"],str(t["price"]),str(t["size"]),t["timestamp"]); is_t=tk.get(k,0)>0
        if is_t: tk[k]-=1
        city,ed=parse_event(t.get("eventSlug") or ""); label,lo,hi,unit,kind=parse_bucket(t.get("title") or "")
        allf.append(dict(w=w,ts=t["timestamp"],cid=t["conditionId"],outcome=t["outcome"],price=float(t["price"]),size=float(t["size"]),taker=is_t,station=CITY2ST.get(city),event_date=ed,bucket=label,lo=lo,hi=hi,unit=unit,tx=t["transactionHash"]))
allf=[f for f in allf if f["station"] and f["event_date"]]
allf.sort(key=lambda f:f["ts"])
print("cluster buy fills:",len(allf),"| wallets:",len(set(f['w'] for f in allf)),"| taker share by count:",round(sum(f['taker'] for f in allf)/len(allf),2))
# episodes per (cid,outcome): gap > 3600s starts a new episode
by=collections.defaultdict(list)
for f in allf: by[(f["cid"],f["outcome"])].append(f)
events=[]
for k,L in by.items():
    L.sort(key=lambda f:f["ts"]); ep=[L[0]]
    for f in L[1:]:
        if f["ts"]-ep[-1]["ts"]>3600: events.append(ep); ep=[f]
        else: ep.append(f)
    events.append(ep)
def summarize(ep):
    f0=ep[0]; t0=f0["ts"]
    foll=[f for f in ep if 0<f["ts"]-t0<=900]
    return dict(first=f0,n_fills_15=len(foll),n_wallets_15=len(set(f["w"] for f in foll)),usdc_15=sum(f["size"]*f["price"] for f in foll),usdc_first=f0["size"]*f0["price"],
                usdc_first_min=sum(f["size"]*f["price"] for f in ep if f["ts"]-t0<=60),ep_len=len(ep),ep_usdc=sum(f["size"]*f["price"] for f in ep),n_wallets_ep=len(set(f["w"] for f in ep)))
E=[summarize(ep) for ep in events]
print("episodes:",len(E),"| with follow-on within 15 min:",sum(1 for e in E if e["n_fills_15"]>0),"| with >=2 wallets within 15 min:",sum(1 for e in E if e["n_wallets_15"]>=1),"| first buyer taker share:",round(sum(e['first']['taker'] for e in E)/len(E),2))
print("first buyer wallet distribution:",collections.Counter(e["first"]["w"][:10] for e in E).most_common(8))
print("episode USDC q50/q90:",sorted(e["ep_usdc"] for e in E)[len(E)//2],sorted(e["ep_usdc"] for e in E)[9*len(E)//10])
json.dump({"fills":allf,"events":[dict(e,first=e["first"]) for e in E]},open("cluster_events.json","w"))
# sample events for minute fetch: stratify first-buyer taker/maker x follow-on yes/no, 200 each where available, resolved dates only
random.seed(13); sample=[]
for tk in (True,False):
    for fo in (True,False):
        pool=[e for e in E if e["first"]["taker"]==tk and (e["n_wallets_15"]>=1)==fo and e["first"]["event_date"]<="2026-10-07"]
        sample+=random.sample(pool,min(220,len(pool)))
        print(f"  stratum taker={tk} follow={fo}: pool {len(pool)} sampled {min(220,len(pool))}")
json.dump(sample,open("cluster_event_sample.json","w"))
