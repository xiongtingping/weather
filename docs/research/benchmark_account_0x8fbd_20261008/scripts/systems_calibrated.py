import json, collections, statistics as st, math, datetime as dt, sys
sys.path.insert(0,'.')
from sysdist import *
from zoneinfo import ZoneInfo
B=json.load(open("baseline3.json")); PA=json.load(open("pooled_asof.json")); reg=json.load(open("registry.json")); tz={r["station_id"]:r["settlement_timezone"] for r in reg}
dist,intervals=load()
FLOOR=1e-3
def Phi(x): return 0.5*(1+math.erf(x/math.sqrt(2)))
def dress(p,iv,shift,sigma):
    labels=list(iv.keys()); out={b:0.0 for b in labels}
    for b,pb in p.items():
        if b not in iv or pb<=0: continue
        c=centre(b,iv)+shift
        for b2,(lo,hi) in iv.items():
            a=Phi((hi-c)/sigma) if hi is not None else 1.0; z=Phi((lo-c)/sigma) if lo is not None else 0.0
            out[b2]+=pb*(a-z)
    return out
def logs(q,truth,labels):
    tot=sum(q.get(b,0) for b in labels); q={b:max(q.get(b,0)/tot if tot else 1/len(labels),FLOOR) for b in labels}; s=sum(q.values())
    return -math.log(q[truth]/s), sum((q[b]/s-(1.0 if b==truth else 0.0))**2 for b in labels), max(q.items(),key=lambda kv:kv[1])[0]==truth
TIMES=[("D-1 12:00",1,12),("D0 02:00",0,2)]
# collect rows: per (time) list of dict(st, ed, half, iv, truth, sys->{p, mean}, mkt, served, pooled)
rows=collections.defaultdict(list)
for sd,rec in B.items():
    st_,ed=sd.split("|"); truth=rec["settled_bucket"]; tC=rec["settled_tmax_c"]; iv=intervals.get((st_,ed))
    if not iv or truth not in iv: continue
    z=ZoneInfo(tz.get(st_,"UTC")); d=dt.date.fromisoformat(ed)
    for name,db,hh in TIMES:
        t=dt.datetime.combine(d-dt.timedelta(days=db),dt.time(hh,0),tzinfo=z).astimezone(dt.timezone.utc).timestamp()
        r=dict(st=st_,ed=ed,half="H1" if ed<"2026-09-22" else "H2",iv=iv,truth=truth,tC=tC,sys={},mkt=rec[name]["market_q"],served=rec[name]["ours"]["p"] if rec[name]["ours"] else None,pooled=(PA.get(sd,{}).get(name) or {}).get("p"),w=(PA.get(sd,{}).get(name) or {}).get("weights"))
        for s in SYS:
            a=asof(dist,st_,ed,s,t)
            if a:
                p=a[1]; m=sum(centre(b,iv)*pb for b,pb in p.items() if b in iv)/max(sum(pb for b,pb in p.items() if b in iv),1e-9)
                r["sys"][s]=dict(p=p,mean=m,err=tC-m)
        rows[name].append(r)
for name,_,_ in TIMES:
    R=rows[name]
    print(f"\n=== {name}: n={len(R)} station-days. LOO per-station shift (other days, >=5 else system pooled) + Gaussian dressing (sigma cross-fitted H1<->H2 from grid) ===")
    # per station & system error lists
    errs=collections.defaultdict(list)
    for r in R:
        for s,v in r["sys"].items(): errs[(r["st"],s)].append((r["ed"],v["err"]))
    pooled_err={s:st.mean(e for (stn,ss),L in errs.items() if ss==s for _,e in L) for s in SYS}
    def shift_for(r,s):
        others=[e for ed,e in errs[(r["st"],s)] if ed!=r["ed"]]
        return st.mean(others) if len(others)>=5 else pooled_err[s]
    GRID=(0.6,0.9,1.2,1.5,1.9,2.4)
    # choose sigma per system on the other half
    sig={}
    for s in SYS:
        for h in ("H1","H2"):
            train=[r for r in R if r["half"]!=h and s in r["sys"]]
            best=min(GRID,key=lambda sg: st.mean(logs(dress(r["sys"][s]["p"],r["iv"],shift_for(r,s),sg),r["truth"],list(r["iv"].keys()))[0] for r in train) if train else 0)
            sig[(s,h)]=best
    print("   cross-fitted sigma (°C) per system [applied-on H1 / H2]:",{s.split('_')[1]:(sig[(s,'H1')],sig[(s,'H2')]) for s in SYS})
    out=collections.defaultdict(list)
    for r in R:
        labels=list(r["iv"].keys()); cal={}
        for s,v in r["sys"].items():
            q=dress(v["p"],r["iv"],shift_for(r,s),sig[(s,r["half"])]); cal[s]=q; out[f"CAL {s}"].append(logs(q,r["truth"],labels))
        if len(cal)>=3:
            eq={b:st.mean(cal[s].get(b,0) for s in cal) for b in labels}; out["CAL equal-mix (avail systems)"].append(logs(eq,r["truth"],labels))
            core=[s for s in ("ECMWF_IFS_ENS_OPEN_PF50","DWD_ICON_GLOBAL_EPS","ECMWF_AIFS_ENS_GLOBAL") if s in cal]
            if len(core)>=2: out["CAL mix IFS+ICON(+AIFS)"].append(logs({b:st.mean(cal[s].get(b,0) for s in core) for b in labels},r["truth"],labels))
            if r["w"]:
                ws={s:r["w"].get(s,0) for s in cal}; tw=sum(ws.values())
                if tw>0: out["CAL mix with OUR learned weights"].append(logs({b:sum(ws[s]*cal[s].get(b,0) for s in cal)/tw for b in labels},r["truth"],labels))
        if r["served"]: out["SERVED"].append(logs(r["served"],r["truth"],labels))
        if r["pooled"]: out["POOLED cross-ens input"].append(logs(r["pooled"],r["truth"],labels))
        if r["mkt"] and all(b in r["iv"] for b in r["mkt"]): out["MARKET q"].append(logs(r["mkt"],r["truth"],labels))
        # market-anchored blend: 0.5 market + 0.5 served, where market exists
        if r["mkt"] and r["served"] and all(b in r["iv"] for b in r["mkt"]):
            tot=sum(r["mkt"].values()); mq={b:r["mkt"].get(b,0)/tot for b in labels}
            out["BLEND 0.5 market + 0.5 served"].append(logs({b:0.5*mq[b]+0.5*r["served"].get(b,0) for b in labels},r["truth"],labels))
            out["BLEND 0.75 market + 0.25 served"].append(logs({b:0.75*mq[b]+0.25*r["served"].get(b,0) for b in labels},r["truth"],labels))
            out["MARKET q (same days)"].append(logs(r["mkt"],r["truth"],labels)); out["SERVED (same days as market)"].append(logs(r["served"],r["truth"],labels))
    for nm,xs in sorted(out.items(),key=lambda kv:st.mean(x[0] for x in kv[1])):
        print(f"   {nm:36s} n={len(xs):4d} LogS={st.mean(x[0] for x in xs):.4f} Brier={st.mean(x[1] for x in xs):.4f} mode-hit={st.mean(x[2] for x in xs):.3f}")
