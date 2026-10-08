import json, collections, statistics as st, math, datetime as dt, sys
sys.path.insert(0,'.')
from sysdist import *
from zoneinfo import ZoneInfo
B=json.load(open("baseline3.json")); PA=json.load(open("pooled_asof.json")); reg=json.load(open("registry.json")); tz={r["station_id"]:r["settlement_timezone"] for r in reg}
dist,intervals=load(); FLOOR=1e-3
def Phi(x): return 0.5*(1+math.erf(x/math.sqrt(2)))
def dress(p,iv,shift,sigma):
    out={b:0.0 for b in iv}
    for b,pb in p.items():
        if b not in iv or pb<=0: continue
        c=centre(b,iv)+shift
        for b2,(lo,hi) in iv.items():
            out[b2]+=pb*((Phi((hi-c)/sigma) if hi is not None else 1.0)-(Phi((lo-c)/sigma) if lo is not None else 0.0))
    return out
def logs(q,truth,labels):
    tot=sum(q.get(b,0) for b in labels); q={b:max(q.get(b,0)/tot if tot else 1/len(labels),FLOOR) for b in labels}; s=sum(q.values())
    return -math.log(q[truth]/s), sum((q[b]/s-(1.0 if b==truth else 0.0))**2 for b in labels), max(q.items(),key=lambda kv:kv[1])[0]==truth
TIMES=[("D-1 12:00",1,12),("D0 02:00",0,2)]
for name,db,hh in TIMES:
    R=[]
    for sd,rec in sorted(B.items(),key=lambda kv:kv[0].split("|")[1]):
        st_,ed=sd.split("|"); truth=rec["settled_bucket"]; tC=rec["settled_tmax_c"]; iv=intervals.get((st_,ed))
        if not iv or truth not in iv: continue
        z=ZoneInfo(tz.get(st_,"UTC")); d=dt.date.fromisoformat(ed)
        t=dt.datetime.combine(d-dt.timedelta(days=db),dt.time(hh,0),tzinfo=z).astimezone(dt.timezone.utc).timestamp()
        r=dict(st=st_,ed=ed,iv=iv,truth=truth,tC=tC,sys={},mkt=rec[name]["market_q"],served=rec[name]["ours"]["p"] if rec[name]["ours"] else None,pooled=(PA.get(sd,{}).get(name) or {}).get("p"),w=(PA.get(sd,{}).get(name) or {}).get("weights"))
        for s in SYS:
            a=asof(dist,st_,ed,s,t)
            if a:
                p=a[1]; m=sum(centre(b,iv)*pb for b,pb in p.items() if b in iv)/max(sum(pb for b,pb in p.items() if b in iv),1e-9); r["sys"][s]=dict(p=p,mean=m,err=tC-m)
        R.append(r)
    # strict as-of: shift from strictly earlier event dates (station-level if >=5 else system-level pooled earlier); sigma chosen on earlier days from grid (>=40 rows) else default 0.9
    hist_err=collections.defaultdict(list); hist_all=collections.defaultdict(list); hist_rows=collections.defaultdict(list)
    out=collections.defaultdict(list); wsum=collections.defaultdict(list); disagree=[]
    GRID=(0.6,0.9,1.2,1.5,1.9)
    cur_ed=None; buffer=[]
    def flush(buf):
        for r in buf:
            for s,v in r["sys"].items(): hist_err[(r["st"],s)].append(v["err"]); hist_all[s].append(v["err"]); hist_rows[s].append(r)
    for r in R:
        if r["ed"]!=cur_ed: flush(buffer); buffer=[]; cur_ed=r["ed"]
        buffer.append(r)
        labels=list(r["iv"].keys()); cal={}
        if r["ed"]<"2026-09-13": continue   # need >= one week of history
        for s,v in r["sys"].items():
            e=hist_err.get((r["st"],s),[]); shift=st.mean(e) if len(e)>=5 else (st.mean(hist_all[s]) if hist_all[s] else 0.0)
            hr=hist_rows[s][-300:]
            if len(hr)>=40:
                sg=min(GRID,key=lambda g: st.mean(logs(dress(x["sys"][s]["p"],x["iv"],(st.mean(hist_err[(x["st"],s)]) if len(hist_err[(x["st"],s)])>=5 else st.mean(hist_all[s])),g),x["truth"],list(x["iv"].keys()))[0] for x in hr[-120:]))
            else: sg=0.9
            q=dress(v["p"],r["iv"],shift,sg); cal[s]=q; out[f"CAL {s}"].append(logs(q,r["truth"],labels))
        if len(cal)>=3:
            out["CAL equal-mix"].append(logs({b:st.mean(cal[s].get(b,0) for s in cal) for b in labels},r["truth"],labels))
            core=[s for s in ("ECMWF_IFS_ENS_OPEN_PF50","DWD_ICON_GLOBAL_EPS","ECMWF_AIFS_ENS_GLOBAL") if s in cal]
            if len(core)>=2:
                mix={b:st.mean(cal[s].get(b,0) for s in core) for b in labels}; out["CAL mix IFS+ICON(+AIFS)"].append(logs(mix,r["truth"],labels))
                if r["mkt"] and all(b in r["iv"] for b in r["mkt"]):
                    mm=max(mix.items(),key=lambda kv:kv[1])[0]; km=max(r["mkt"].items(),key=lambda kv:kv[1])[0]
                    if mm!=km: disagree.append((r["truth"]==mm,r["truth"]==km))
            if r["w"]:
                ws={s:r["w"].get(s,0) for s in cal}; tw=sum(ws.values())
                if tw>0: out["CAL mix OUR learned weights"].append(logs({b:sum(ws[s]*cal[s].get(b,0) for s in cal)/tw for b in labels},r["truth"],labels))
                for s in SYS: 
                    if s in r["w"]: wsum[s].append(r["w"][s])
        if r["served"]: out["SERVED"].append(logs(r["served"],r["truth"],labels))
        if r["pooled"]: out["POOLED cross-ens input"].append(logs(r["pooled"],r["truth"],labels))
        if r["mkt"] and all(b in r["iv"] for b in r["mkt"]): out["MARKET q"].append(logs(r["mkt"],r["truth"],labels))
    print(f"\n=== {name}, STRICT as-of (event dates >= 09-13; shift & sigma from earlier days only) ===")
    for nm,xs in sorted(out.items(),key=lambda kv:st.mean(x[0] for x in kv[1])):
        print(f"   {nm:32s} n={len(xs):4d} LogS={st.mean(x[0] for x in xs):.4f} Brier={st.mean(x[1] for x in xs):.4f} mode-hit={st.mean(x[2] for x in xs):.3f}")
    print("   our learned weights (mean over station-days):",{s.split('_')[1]:round(st.mean(v),3) for s,v in wsum.items()})
    if disagree: print(f"   IFS+ICON mix mode ≠ market mode: n={len(disagree)} mix right {st.mean(a for a,b in disagree):.3f} | market right {st.mean(b for a,b in disagree):.3f}")
