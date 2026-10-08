import json, collections, statistics as st
B=json.load(open("baseline2.json"))
TIMES=["D-1 12:00","D-1 18:00","D0 02:00"]
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
        x=rec[t]
        if not x["market_mode"] or not x["quotes"]: continue
        Q=x["quotes"]; lab2ord={b:v["ord"] for b,v in Q.items()}
        if rec["settled_bucket"] not in lab2ord: continue
        tro=lab2ord[rec["settled_bucket"]]
        q={lab2ord[b]:v for b,v in x["market_q"].items()}
        ask={v["ord"]:v["ask"] for v in Q.values() if v["ask"] is not None}; bid={v["ord"]:v["bid"] for v in Q.values() if v["bid"] is not None}
        mode,mean,med,qm=stats(q)
        ours=x["ours"]; omean=None; omode=None; op=None
        if ours:
            P={ours["ord"][b]:p for b,p in ours["p"].items()}; omode,omean,_,_=stats(P); op=P
        rows.append(dict(st=st_,ed=ed,t=t,q=q,ask=ask,bid=bid,mode=mode,mean=mean,med=med,qm=qm,truth=tro,omean=omean,omode=omode,op=op))
print("station-day-times with healthy market & truth:",collections.Counter(r["t"] for r in rows))
def band(qm): return "<0.35" if qm<0.35 else "0.35-0.45" if qm<0.45 else "0.45-0.55" if qm<0.55 else "0.55-0.65" if qm<0.65 else ">=0.65"
BANDS=["<0.35","0.35-0.45","0.45-0.55","0.55-0.65",">=0.65"]
print("\nA. market MODE calibration by concentration band: n, mean q_mode, mean ask(mode), hit; NO-on-mode at ask = 1-bid: paid, ret")
for t in TIMES+["ALL"]:
    rs=[r for r in rows if t=="ALL" or r["t"]==t]
    print(f" {t}:")
    for b in BANDS:
        x=[r for r in rs if band(r["qm"])==b and r["mode"] in r["ask"] and r["mode"] in r["bid"]]
        if len(x)<8: continue
        hit=st.mean(r["truth"]==r["mode"] for r in x); qm=st.mean(r["qm"] for r in x); ask=st.mean(r["ask"][r["mode"]] for r in x); nobid=st.mean(1-r["bid"][r["mode"]] for r in x)
        print(f"   q_mode {b:9s} n={len(x):3d} q={qm:.3f} ask={ask:.3f} hit={hit:.3f} | YES@ask ret {(hit/ask-1)*100:+6.1f}% | NO@(1-bid) paid={nobid:.3f} hit={1-hit:.3f} ret {((1-hit)/nobid-1)*100:+6.1f}%")
print("\nB. neighbours (mode±1) by concentration band: hit vs ask, YES@ask return")
for t in ["D-1 12:00","D0 02:00","ALL"]:
    rs=[r for r in rows if t=="ALL" or r["t"]==t]
    print(f" {t}:")
    for b in BANDS:
        for side in (-1,1):
            x=[r for r in rs if band(r["qm"])==b and (r["mode"]+side) in r["ask"] and 0.01<r["ask"][r["mode"]+side]<0.99]
            if len(x)<8: continue
            hit=st.mean(r["truth"]==r["mode"]+side for r in x); ask=st.mean(r["ask"][r["mode"]+side] for r in x); qq=st.mean(r["q"].get(r["mode"]+side,0) for r in x)
            print(f"   q_mode {b:9s} mode{side:+d} n={len(x):3d} q={qq:.3f} ask={ask:.3f} hit={hit:.3f} ret {(hit/ask-1)*100:+6.1f}%")
print("\nC. skew side: neighbour on market-mean side vs against, hit vs ask (ALL times)")
for name,pred in (("mean-side",lambda r,s:(r["mean"]>r["mode"])==(s>0)),("anti-mean-side",lambda r,s:(r["mean"]>r["mode"])!=(s>0))):
    for t in ["D-1 12:00","D0 02:00"]:
        xs=[]
        for r in rows:
            if r["t"]!=t or abs(r["mean"]-r["mode"])<0.05: continue
            for s in (-1,1):
                o=r["mode"]+s
                if o in r["ask"] and 0.01<r["ask"][o]<0.99 and pred(r,s): xs.append((r["truth"]==o,r["ask"][o],r["q"].get(o,0)))
        if xs: print(f"   {t} {name:14s} n={len(xs)} q={st.mean(x[2] for x in xs):.3f} ask={st.mean(x[1] for x in xs):.3f} hit={st.mean(x[0] for x in xs):.3f} ret {(st.mean(x[0] for x in xs)/st.mean(x[1] for x in xs)-1)*100:+.1f}%")
print("\nD. our-model side: neighbour on (our mean - market mean) side vs against")
for name,pred in (("model-side",lambda r,s:(r["omean"]>r["mean"])==(s>0)),("anti-model-side",lambda r,s:(r["omean"]>r["mean"])!=(s>0))):
    for t in ["D-1 12:00","D0 02:00"]:
        xs=[]
        for r in rows:
            if r["t"]!=t or r["omean"] is None or abs(r["omean"]-r["mean"])<0.1: continue
            for s in (-1,1):
                o=r["mode"]+s
                if o in r["ask"] and 0.01<r["ask"][o]<0.99 and pred(r,s): xs.append((r["truth"]==o,r["ask"][o],r["op"].get(o,0)))
        if xs: print(f"   {t} {name:16s} n={len(xs)} ourp={st.mean(x[2] for x in xs):.3f} ask={st.mean(x[1] for x in xs):.3f} hit={st.mean(x[0] for x in xs):.3f} ret {(st.mean(x[0] for x in xs)/st.mean(x[1] for x in xs)-1)*100:+.1f}%")
print("\nE. median vs mode when they differ; mean-bucket vs mode")
for t in ["D-1 12:00","D0 02:00","ALL"]:
    rs=[r for r in rows if (t=="ALL" or r["t"]==t) and r["med"]!=r["mode"]]
    if rs: print(f"   {t} median≠mode n={len(rs)}: median hit {st.mean(r['truth']==r['med'] for r in rs):.3f} vs mode hit {st.mean(r['truth']==r['mode'] for r in rs):.3f} | q(median) {st.mean(r['q'].get(r['med'],0) for r in rs):.3f} q(mode) {st.mean(r['qm'] for r in rs):.3f}")
    rs=[r for r in rows if (t=="ALL" or r["t"]==t) and round(r["mean"])!=r["mode"]]
    if rs: print(f"   {t} meanbucket≠mode n={len(rs)}: mean-bucket hit {st.mean(r['truth']==round(r['mean']) for r in rs):.3f} vs mode hit {st.mean(r['truth']==r['mode'] for r in rs):.3f} | q(meanbucket) {st.mean(r['q'].get(round(r['mean']),0) for r in rs):.3f}")
json.dump([{k:v for k,v in r.items() if k not in ("op",)} for r in rows],open("baseline_rows.json","w"))
