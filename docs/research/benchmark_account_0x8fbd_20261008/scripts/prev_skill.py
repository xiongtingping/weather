import json, collections, glob, statistics as st, sys
reg=json.load(open("registry.json")); base=json.load(open("baseline.json"))
lad=collections.defaultdict(list)
for r in reg: lad[(r["station_id"],r["event_date"])].append(r)
for k in lad: lad[k].sort(key=lambda r:(r["lower_c"] if r["lower_c"] is not None else -999))
def bucket_of(st_,ed,tc):
    L=lad.get((st_,ed))
    if not L or tc is None: return None
    t=tc*9/5+32 if L[0]["settlement_unit"]=="F" else tc
    for r in L:
        lo,hi=r["lower_c"],r["upper_c"]
        if (lo is None or t>=lo-0.5) and (hi is None or t<hi+0.5): return r["outcome"]
def ordinal(st_,ed,label):
    for i,r in enumerate(lad.get((st_,ed),[])):
        if r["outcome"]==label: return i
P={}
for f in ("prev_global.json","prev_regional.json"):
    try: P.update(json.load(open(f)))
    except Exception: pass
# daily max per (station, model, local date, lead)
tmax=collections.defaultdict(dict)
for key,v in P.items():
    if "error" in v: continue
    st_,m=key.split("|")
    for lead in ("d1","d2"):
        byday=collections.defaultdict(list)
        for t,x in zip(v["time"],v[lead]):
            if x is not None: byday[t[:10]].append(x)
        for d,xs in byday.items():
            if len(xs)>=20: tmax[(st_,m,lead)][d]=max(xs)
models=sorted(set(k[1] for k in tmax))
HI={"ZHHH","LIMC","EGLC","RCSS","RKSI","LTAC","KLAX","OEJN"}; LO={"KMIA","KATL","OPKC","KORD","EFHK","EPWA"}
rows=[]
for sd,rec in base.items():
    st_,ed=sd.split("|"); truth=rec["settled_bucket"]; tro=ordinal(st_,ed,truth)
    if tro is None: continue
    for lead,ref in (("d1","D-1 12:00"),("d2","D-1 12:00")):
        for m in models:
            tc=tmax.get((st_,m,lead),{}).get(ed)
            if tc is None: continue
            b=bucket_of(st_,ed,tc); bo=ordinal(st_,ed,b)
            if bo is None: continue
            mk=rec[ref]["market_mode"]; mko=ordinal(st_,ed,mk) if mk else None
            ours=rec[ref]["ours"]; oo=ordinal(st_,ed,ours["mode"]) if ours else None
            rows.append(dict(st=st_,ed=ed,lead=lead,m=m,hit=bo==tro,near=abs(bo-tro)<=1,err=bo-tro,mk_hit=(mko==tro) if mko is not None else None,mk_near=(abs(mko-tro)<=1) if mko is not None else None,our_hit=(oo==tro) if oo is not None else None,grp="HI" if st_ in HI else "LO" if st_ in LO else "MID"))
def summarize(tag,rs):
    if len(rs)<10: return
    n=len(rs); h=sum(r["hit"] for r in rs)/n; nr=sum(r["near"] for r in rs)/n; bias=st.mean(r["err"] for r in rs)
    mk=[r for r in rs if r["mk_hit"] is not None]; mkh=sum(r["mk_hit"] for r in mk)/len(mk) if mk else float('nan'); mh=sum(r["hit"] for r in mk)/len(mk) if mk else float('nan')
    ou=[r for r in rs if r["our_hit"] is not None]; ouh=sum(r["our_hit"] for r in ou)/len(ou) if ou else float('nan')
    print(f"  {tag:34s} n={n:4d} hit={h:.3f} ±1={nr:.3f} bias(buckets)={bias:+.2f} | same days: model {mh:.3f} vs market mode {mkh:.3f} (n={len(mk)}) | our mode {ouh:.3f}")
for lead in ("d1","d2"):
    print(f"=== lead {lead} (previous_day{lead[-1]} run), bucket hit of model daily max; market mode & our mode at D-1 12:00 on same station-days ===")
    for m in models: summarize(m,[r for r in rows if r["lead"]==lead and r["m"]==m])
    for g in ("HI","LO"):
        print(f"  -- {g} stations --")
        for m in models: summarize(m,[r for r in rows if r["lead"]==lead and r["m"]==m and r["grp"]==g])
# consensus: mean of available models per station-day (d1)
cons=collections.defaultdict(list)
for (st_,m,lead),dd in tmax.items():
    if lead!="d1": continue
    for d,x in dd.items(): cons[(st_,d)].append((m,x))
cr=[]
for (st_,ed),xs in cons.items():
    rec=base.get(f"{st_}|{ed}")
    if not rec: continue
    tro=ordinal(st_,ed,rec["settled_bucket"])
    if tro is None or len(xs)<3: continue
    mean=st.mean(x for _,x in xs); med=st.median(x for _,x in xs)
    for name,val in (("mean",mean),("median",med)):
        b=bucket_of(st_,ed,val); bo=ordinal(st_,ed,b)
        if bo is None: continue
        mk=rec["D-1 12:00"]["market_mode"]; mko=ordinal(st_,ed,mk) if mk else None; ours=rec["D-1 12:00"]["ours"]; oo=ordinal(st_,ed,ours["mode"]) if ours else None
        cr.append(dict(st=st_,ed=ed,lead="d1",m="CONSENSUS_"+name,hit=bo==tro,near=abs(bo-tro)<=1,err=bo-tro,mk_hit=(mko==tro) if mko is not None else None,mk_near=None,our_hit=(oo==tro) if oo is not None else None,grp="HI" if st_ in HI else "LO" if st_ in LO else "MID"))
print("=== consensus of all available models (d1) ===")
for name in ("CONSENSUS_mean","CONSENSUS_median"):
    summarize(name,[r for r in cr if r["m"]==name])
    for g in ("HI","LO"): summarize(name+" "+g,[r for r in cr if r["m"]==name and r["grp"]==g])
