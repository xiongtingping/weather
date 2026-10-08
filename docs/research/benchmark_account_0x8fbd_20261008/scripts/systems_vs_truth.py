import json, collections, statistics as st, math, datetime as dt, sys
sys.path.insert(0,'.')
from sysdist import *
from zoneinfo import ZoneInfo
B=json.load(open("baseline3.json")); PA=json.load(open("pooled_asof.json")); reg=json.load(open("registry.json")); tz={r["station_id"]:r["settlement_timezone"] for r in reg}
dist,intervals=load()
print("systems loaded:",{s:sum(1 for k in dist if k[2]==s) for s in SYS})
FLOOR=1e-3
def score(p,truth_label,labels):
    tot=sum(max(p.get(b,0),0) for b in labels)
    q={b:max(p.get(b,0),0)/tot if tot else 1/len(labels) for b in labels}
    q={b:max(v,FLOOR) for b,v in q.items()}; s=sum(q.values()); q={b:v/s for b,v in q.items()}
    brier=sum((q[b]-(1.0 if b==truth_label else 0.0))**2 for b in labels); logs=-math.log(q[truth_label])
    mode=max(q.items(),key=lambda kv:kv[1])[0]
    return brier,logs,mode,q[mode]
TIMES=[("D-1 12:00",1,12),("D0 02:00",0,2)]
res=collections.defaultdict(lambda:collections.defaultdict(list))
bias=collections.defaultdict(list)
for sd,rec in B.items():
    st_,ed=sd.split("|"); truth=rec["settled_bucket"]; tC=rec["settled_tmax_c"]
    iv=intervals.get((st_,ed))
    if not iv or truth not in iv: continue
    labels=list(iv.keys()); z=ZoneInfo(tz.get(st_,"UTC")); d=dt.date.fromisoformat(ed)
    for name,db,hh in TIMES:
        t=dt.datetime.combine(d-dt.timedelta(days=db),dt.time(hh,0),tzinfo=z).astimezone(dt.timezone.utc).timestamp()
        x=rec[name]; mkt=x["market_q"]; ours=x["ours"]; pooled=PA.get(sd,{}).get(name)
        sources={}
        if mkt and all(b in iv for b in mkt): sources["MARKET q"]=mkt
        if ours: sources[f"SERVED ({ours['stage']})"]=ours["p"]; sources["SERVED any"]=ours["p"]
        if pooled: sources["POOLED cross-ens (P0 input)"]=pooled["p"]
        for s in SYS:
            a=asof(dist,st_,ed,s,t)
            if a: 
                sources[s]=a[1]
                m=sum(centre(b,iv)*p for b,p in a[1].items() if b in iv)/max(sum(p for b,p in a[1].items() if b in iv),1e-9)
                bias[(name,s)].append(tC-m)
        # equal-weight raw average of available systems
        avail=[sources[s] for s in SYS if s in sources]
        if len(avail)>=3: sources["RAW EQUAL-MIX of systems"]={b:st.mean(a.get(b,0) for a in avail) for b in labels}
        for nm,p in sources.items():
            br,lg,mode,pm=score(p,truth,labels)
            res[name][nm].append((br,lg,mode==truth,pm))
for name,_,_ in TIMES:
    print(f"\n=== {name}: proper scores vs settled truth (floor 1e-3), station-days with each source ===")
    for nm,xs in sorted(res[name].items(),key=lambda kv:st.mean(x[1] for x in kv[1])):
        print(f"   {nm:30s} n={len(xs):4d} Brier={st.mean(x[0] for x in xs):.4f} LogS={st.mean(x[1] for x in xs):.4f} mode-hit={st.mean(x[2] for x in xs):.3f} mean p(mode)={st.mean(x[3] for x in xs):.3f}")
    # matched comparison on common station-days: market vs served vs pooled vs each system
    print("   raw system mean bias (truth - system mean, °C):",{s.split('_')[1]:f"{st.mean(bias[(name,s)]):+.2f} (n={len(bias[(name,s)])})" for s in SYS if bias[(name,s)]})
