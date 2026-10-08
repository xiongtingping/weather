import json, collections, statistics as st, sys
B=json.load(open(sys.argv[1] if len(sys.argv)>1 else "baseline3.json"))
TIMES=["D-2 12:00","D-1 06:00","D-1 12:00","D-1 18:00","D0 02:00","D0 08:00"]
def stats(q):
    items=sorted(q.items()); tot=sum(v for _,v in items)
    mode=max(items,key=lambda kv:(kv[1],-kv[0]))[0]; mean=sum(o*v for o,v in items)/tot
    c=0; med=items[-1][0]
    for o,v in items:
        c+=v
        if c>=tot/2: med=o; break
    return mode,mean,med,q[mode]/tot
rows=[]
for sd,rec in B.items():
    st_,ed=sd.split("|")
    for t in TIMES:
        x=rec.get(t)
        if not x or not x["market_mode"] or not x["quotes"]: continue
        Q=x["quotes"]; lab2ord={b:v["ord"] for b,v in Q.items()}
        if rec["settled_bucket"] not in lab2ord: continue
        tro=lab2ord[rec["settled_bucket"]]; q={lab2ord[b]:v for b,v in x["market_q"].items()}
        ask={v["ord"]:v["ask"] for v in Q.values() if v["ask"] is not None}; bid={v["ord"]:v["bid"] for v in Q.values() if v["bid"] is not None}
        mode,mean,med,qm=stats(q); ours=x["ours"]; omean=omode=None; op=None
        if ours:
            op={ours["ord"][b]:p for b,p in ours["p"].items()}; omode,omean,_,_=stats(op)
        rows.append(dict(st=st_,ed=ed,t=t,q=q,ask=ask,bid=bid,mode=mode,mean=mean,med=med,qm=qm,truth=tro,omean=omean,omode=omode,op=op,half="H1" if ed<"2026-09-22" else "H2",unit="F" if st_[0]=="K" else "C"))
print("rows:",collections.Counter(r["t"] for r in rows))
def band(qm): return "<0.35" if qm<0.35 else "0.35-0.45" if qm<0.45 else "0.45-0.55" if qm<0.55 else "0.55-0.65" if qm<0.65 else ">=0.65"
BANDS=["<0.35","0.35-0.45","0.45-0.55","0.55-0.65",">=0.65"]
def leg(rs,getord,side,price_fn):
    """returns n, avg price, hit, return for YES (side=+1) or NO (side=-1) legs on bucket getord(r) at price price_fn(r,o)"""
    xs=[]
    for r in rs:
        o=getord(r)
        if o is None: continue
        p=price_fn(r,o)
        if p is None or not 0.01<p<0.99: continue
        win=(r["truth"]==o) if side>0 else (r["truth"]!=o)
        xs.append((p,1.0 if win else 0.0))
    if len(xs)<8: return None
    p=st.mean(x[0] for x in xs); h=st.mean(x[1] for x in xs)
    return len(xs),p,h,(h/p-1)*100
YES=lambda r,o:r["ask"].get(o); NO=lambda r,o:(1-r["bid"][o]) if o in r["bid"] else None
def show(tag,res):
    if res: print(f"   {tag:44s} n={res[0]:4d} paid={res[1]:.3f} hit={res[2]:.3f} ret={res[3]:+6.1f}%")
print("\n=== A. mode calibration by concentration band (ALL times pooled) and NO-on-mode return ===")
for b in BANDS:
    rs=[r for r in rows if band(r["qm"])==b]
    res=leg(rs,lambda r:r["mode"],-1,NO)
    if res: print(f"   q_mode {b:9s} n={res[0]:4d} q={st.mean(r['qm'] for r in rs):.3f} mode hit={1-res[2]:.3f} | NO@(1-bid) paid={res[1]:.3f} ret={res[3]:+6.1f}%")
print("\n=== B. rules by decision time (taker at ask / NO at 1-bid), with H1=09-06..09-21 / H2=09-22..10-07 split ===")
RULES=[("NO on mode (all)",lambda r:r["mode"],-1,NO,lambda r:True),
       ("NO on mode, q_mode<0.45",lambda r:r["mode"],-1,NO,lambda r:r["qm"]<0.45),
       ("NO on mode, q_mode>=0.55",lambda r:r["mode"],-1,NO,lambda r:r["qm"]>=0.55),
       ("YES on mode (Proper-like)",lambda r:r["mode"],1,YES,lambda r:True),
       ("YES mode-1 (cold nbr)",lambda r:r["mode"]-1,1,YES,lambda r:True),
       ("YES mode+1 (warm nbr)",lambda r:r["mode"]+1,1,YES,lambda r:True),
       ("YES mode-1 if q_mode<0.45",lambda r:r["mode"]-1,1,YES,lambda r:r["qm"]<0.45),
       ("YES mode+1 if q_mode<0.45",lambda r:r["mode"]+1,1,YES,lambda r:r["qm"]<0.45),
       ("YES market-mean-side nbr",lambda r:r["mode"]+(1 if r["mean"]>r["mode"] else -1),1,YES,lambda r:abs(r["mean"]-r["mode"])>0.05),
       ("YES anti-mean-side nbr",lambda r:r["mode"]-(1 if r["mean"]>r["mode"] else -1),1,YES,lambda r:abs(r["mean"]-r["mode"])>0.05),
       ("YES median bucket when ≠ mode",lambda r:r["med"],1,YES,lambda r:r["med"]!=r["mode"]),
       ("YES our mode when ≠ market mode",lambda r:r["omode"],1,YES,lambda r:r["omode"] is not None and r["omode"]!=r["mode"]),
       ("YES our mode when ours p>ask",lambda r:r["omode"],1,YES,lambda r:r["omode"] is not None and r["op"] and r["ask"].get(r["omode"]) is not None and r["op"].get(r["omode"],0)>r["ask"][r["omode"]]),
       ("YES our-mean-side nbr",lambda r:r["mode"]+(1 if r["omean"]>r["mean"] else -1),1,YES,lambda r:r["omean"] is not None and abs(r["omean"]-r["mean"])>0.1),
       ("NO on mode when ours p_mode < 1-… (ours p<bid)",lambda r:r["mode"],-1,NO,lambda r:r["op"] is not None and r["mode"] in r["bid"] and r["op"].get(r["mode"],0)<r["bid"][r["mode"]]),
       ("NO on mode when ours p_mode >= bid",lambda r:r["mode"],-1,NO,lambda r:r["op"] is not None and r["mode"] in r["bid"] and r["op"].get(r["mode"],0)>=r["bid"][r["mode"]])]
for t in ["D-1 12:00","D-1 18:00","D0 02:00","D0 08:00"]:
    print(f" --- {t} ---")
    for name,getord,side,pf,cond in RULES:
        rs=[r for r in rows if r["t"]==t and cond(r)]
        res=leg(rs,getord,side,pf)
        if not res: continue
        h1=leg([r for r in rs if r["half"]=="H1"],getord,side,pf); h2=leg([r for r in rs if r["half"]=="H2"],getord,side,pf)
        print(f"   {name:40s} n={res[0]:4d} paid={res[1]:.3f} hit={res[2]:.3f} ret={res[3]:+6.1f}% | H1 {(f'{h1[3]:+.1f}% (n={h1[0]})' if h1 else '-'):18s} H2 {(f'{h2[3]:+.1f}% (n={h2[0]})' if h2 else '-')}")
print("\n=== C. when our mode ≠ market mode: who is right (by time) ===")
for t in ["D-1 12:00","D0 02:00","D0 08:00"]:
    rs=[r for r in rows if r["t"]==t and r["omode"] is not None and r["omode"]!=r["mode"]]
    if rs: print(f"   {t} n={len(rs)} ({len(rs)/sum(1 for r in rows if r['t']==t and r['omode'] is not None):.0%} of days): market right {st.mean(r['truth']==r['mode'] for r in rs):.3f} | ours right {st.mean(r['truth']==r['omode'] for r in rs):.3f} | neither {st.mean(r['truth'] not in (r['mode'],r['omode']) for r in rs):.3f} | ask(our mode) {st.mean(r['ask'].get(r['omode'],float('nan')) for r in rs if r['omode'] in r['ask']):.3f}")
print("\n=== D. market skew (mean-mode) sign vs truth side, and C/F split of nbr pricing (D0 02:00) ===")
rs=[r for r in rows if r["t"]=="D0 02:00"]
for name,pred in (("mean warmer than mode",lambda r:r["mean"]-r["mode"]>0.05),("mean colder than mode",lambda r:r["mean"]-r["mode"]<-0.05)):
    x=[r for r in rs if pred(r)]
    print(f"   {name}: n={len(x)} truth above mode {st.mean(r['truth']>r['mode'] for r in x):.3f} below {st.mean(r['truth']<r['mode'] for r in x):.3f} at mode {st.mean(r['truth']==r['mode'] for r in x):.3f}")
for u in ("C","F"):
    x=[r for r in rs if r["unit"]==u]
    show(f"{u} stations YES mode-1",leg(x,lambda r:r["mode"]-1,1,YES)); show(f"{u} stations YES mode+1",leg(x,lambda r:r["mode"]+1,1,YES)); show(f"{u} stations NO on mode",leg(x,lambda r:r["mode"],-1,NO))
json.dump([{k:v for k,v in r.items() if k!="op"} for r in rows],open("baseline3_rows.json","w"))
