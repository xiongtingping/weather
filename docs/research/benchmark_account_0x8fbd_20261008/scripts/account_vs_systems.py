import json, collections, statistics as st, math, datetime as dt, sys
sys.path.insert(0,'.')
from sysdist import *
dist,intervals=load(); F=json.load(open("fills_full.json")); B=json.load(open("baseline3.json"))
def Phi(x): return 0.5*(1+math.erf(x/math.sqrt(2)))
def dress(p,iv,shift,sigma=0.9):
    out={b:0.0 for b in iv}
    for b,pb in p.items():
        if b not in iv or pb<=0: continue
        c=centre(b,iv)+shift
        for b2,(lo,hi) in iv.items(): out[b2]+=pb*((Phi((hi-c)/sigma) if hi is not None else 1.0)-(Phi((lo-c)/sigma) if lo is not None else 0.0))
    return out
# strict as-of shift per (station, system): mean error over earlier event dates (>=5) else pooled earlier
errs=collections.defaultdict(list)  # (st,sys) -> [(ed, err)]
for sd,rec in B.items():
    st_,ed=sd.split("|"); iv=intervals.get((st_,ed)); tC=rec["settled_tmax_c"]
    if not iv: continue
    for s in SYS:
        L=dist.get((st_,ed,s))
        if not L: continue
        p=L[-1][1]; m=sum(centre(b,iv)*pb for b,pb in p.items() if b in iv)/max(sum(pb for b,pb in p.items() if b in iv),1e-9)
        errs[(st_,s)].append((ed,tC-m))
def shift_for(st_,s,ed):
    e=[x for d,x in errs.get((st_,s),[]) if d<ed]
    if len(e)>=5: return st.mean(e)
    allv=[x for (stn,ss),L in errs.items() if ss==s for d,x in L if d<ed]
    return st.mean(allv) if allv else 0.0
rows=[]
for f in F:
    if f["outcome"]!="Yes" or f["my_ord"] is None or f["truth_ord"] is None or not f["market"]: continue
    iv=intervals.get((f["station"],f["event_date"]))
    if not iv: continue
    labels=list(iv.keys()); my=labels[f["my_ord"]] if f["my_ord"]<len(labels) else None
    if my is None: continue
    q={int(k):v for k,v in f["market"]["q"].items()}; mode=max(q.items(),key=lambda kv:(kv[1],-kv[0]))[0]
    rec=dict(size=f["size"],price=f["price"],hit=f["truth_ord"]==f["my_ord"],d=f["my_ord"]-mode,sys={})
    for s in SYS:
        a=asof(dist,f["station"],f["event_date"],s,f["ts"])
        if not a: continue
        cal=dress(a[1],iv,shift_for(f["station"],s,f["event_date"]))
        warm=sum(v for b,v in cal.items() if labels.index(b)>mode); cold=sum(v for b,v in cal.items() if labels.index(b)<mode)
        rec["sys"][s]=dict(p_my=cal.get(my,0),mode_is_my=max(cal.items(),key=lambda kv:kv[1])[0]==my,warm=warm,cold=cold)
    rows.append(rec)
print("YES fills with systems:",len(rows))
W=lambda rs,fn: sum(r["size"]*fn(r) for r in rs)/sum(r["size"] for r in rs) if rs else float('nan')
print("\nA. per system: share of their YES fills where system p > paid (agree), hit when agree vs disagree, system-mode==their bucket share")
for s in SYS:
    rs=[r for r in rows if s in r["sys"]]
    if not rs: continue
    ag=[r for r in rs if r["sys"][s]["p_my"]>r["price"]]; dg=[r for r in rs if r["sys"][s]["p_my"]<=r["price"]]
    print(f"   {s:26s} n={len(rs):5d} agree {W(rs,lambda r:1.0 if r['sys'][s]['p_my']>r['price'] else 0.0):.2f} | hit agree {W(ag,lambda r:1.0 if r['hit'] else 0.0):.3f} (paid {W(ag,lambda r:r['price']):.3f}) | hit disagree {W(dg,lambda r:1.0 if r['hit'] else 0.0):.3f} (paid {W(dg,lambda r:r['price']):.3f}) | sys mode = their bucket {W(rs,lambda r:1.0 if r['sys'][s]['mode_is_my'] else 0.0):.2f}")
print("\nB. neighbour side choice vs each system's warm-vs-cold mass (|d|=1 fills): P(pick warm | system warm>cold) vs P(pick warm | cold>warm)")
nb=[r for r in rows if abs(r["d"])==1]
for s in SYS:
    rs=[r for r in nb if s in r["sys"]]
    w=[r for r in rs if r["sys"][s]["warm"]>r["sys"][s]["cold"]]; c=[r for r in rs if r["sys"][s]["warm"]<=r["sys"][s]["cold"]]
    if len(w)>30 and len(c)>30: print(f"   {s:26s} P(warm|sys warm)={W(w,lambda r:1.0 if r['d']>0 else 0.0):.3f} (n={len(w)}) P(warm|sys cold)={W(c,lambda r:1.0 if r['d']>0 else 0.0):.3f} (n={len(c)}) -> on system side {W(rs,lambda r:1.0 if ((r['d']>0)==(r['sys'][s]['warm']>r['sys'][s]['cold'])) else 0.0):.3f}")
