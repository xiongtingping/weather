import json, collections, statistics as st, math
R=json.load(open("baseline3_rows.json"))
B=json.load(open("baseline3.json"))
for r in R:
    for k in ("q","ask","bid"): r[k]={int(a):b for a,b in r[k].items()}
    rec=B[f"{r['st']}|{r['ed']}"][r["t"]]; ours=rec["ours"]
    r["op"]={ours["ord"][b]:p for b,p in ours["p"].items()} if ours else None
def ent(d):
    tot=sum(d.values()); return -sum((v/tot)*math.log(v/tot) for v in d.values() if v>0)
def leg(rs,getord,side,price_fn):
    xs=[]
    for r in rs:
        o=getord(r)
        if o is None: continue
        p=price_fn(r,o)
        if p is None or not 0.01<p<0.99: continue
        xs.append((p,1.0 if ((r["truth"]==o) if side>0 else (r["truth"]!=o)) else 0.0))
    if len(xs)<10: return None
    p=st.mean(x[0] for x in xs); h=st.mean(x[1] for x in xs); return len(xs),p,h,(h/p-1)*100
YES=lambda r,o:r["ask"].get(o); NO=lambda r,o:(1-r["bid"][o]) if o in r["bid"] else None
def show(tag,res,h1=None,h2=None):
    if res: print(f"   {tag:46s} n={res[0]:4d} paid={res[1]:.3f} hit={res[2]:.3f} ret={res[3]:+6.1f}%"+(f" | H1 {h1[3]:+.1f} (n={h1[0]})" if h1 else "")+(f" H2 {h2[3]:+.1f} (n={h2[0]})" if h2 else ""))
def both(tag,rs,getord,side,pf):
    show(tag,leg(rs,getord,side,pf),leg([r for r in rs if r["half"]=="H1"],getord,side,pf),leg([r for r in rs if r["half"]=="H2"],getord,side,pf))
print("=== E. tails (mode±2) and width ===")
for t in ("D-1 12:00","D0 02:00","D0 08:00"):
    rs=[r for r in R if r["t"]==t]
    print(f" {t}:")
    both("YES mode-2",rs,lambda r:r["mode"]-2,1,YES); both("YES mode+2",rs,lambda r:r["mode"]+2,1,YES)
    both("YES mode-2 or lower (sum) ~ via mode-2 only",rs,lambda r:r["mode"]-2,1,YES)
    # width: market entropy vs model entropy
    wr=[r for r in rs if r["op"]]
    narrow=[r for r in wr if ent(r["q"])<ent({o:p for o,p in r["op"].items() if o in r["q"]})-0.05]; wide=[r for r in wr if ent(r["q"])>ent({o:p for o,p in r["op"].items() if o in r["q"]})+0.05]
    print(f"   market narrower than our model: {len(narrow)} | wider: {len(wide)} | of {len(wr)}")
    both("  NO on mode | market narrower than model",narrow,lambda r:r["mode"],-1,NO); both("  NO on mode | market wider than model",wide,lambda r:r["mode"],-1,NO)
    both("  YES mode-1 | market narrower",narrow,lambda r:r["mode"]-1,1,YES); both("  YES mode+1 | market narrower",narrow,lambda r:r["mode"]+1,1,YES)
    both("  YES mode-2 | market narrower",narrow,lambda r:r["mode"]-2,1,YES); both("  YES mode+2 | market narrower",narrow,lambda r:r["mode"]+2,1,YES)
    # dispersion basket: NO mode + YES mode-1 + YES mode+1, equal 30U at ask
    pnl=[]; 
    for r in rs:
        legs=[]
        if r["mode"] in r["bid"] and 0.01<1-r["bid"][r["mode"]]<0.99: legs.append((1-r["bid"][r["mode"]], r["truth"]!=r["mode"]))
        for s in (-1,1):
            o=r["mode"]+s
            if o in r["ask"] and 0.01<r["ask"][o]<0.99: legs.append((r["ask"][o], r["truth"]==o))
        if len(legs)==3: pnl.append(sum(30*((1.0 if w else 0.0)/p-1) for p,w in legs)/90)
    if pnl: print(f"   dispersion basket (NO mode + YES ±1, 30U each): n={len(pnl)} mean ret {100*st.mean(pnl):+.1f}% | H1 {100*st.mean(x for x,r in zip(pnl,[r for r in rs if True]) if False) if False else ''}")
    # split by half for dispersion
    for h in ("H1","H2"):
        p2=[]
        for r in rs:
            if r["half"]!=h: continue
            legs=[]
            if r["mode"] in r["bid"] and 0.01<1-r["bid"][r["mode"]]<0.99: legs.append((1-r["bid"][r["mode"]], r["truth"]!=r["mode"]))
            for s in (-1,1):
                o=r["mode"]+s
                if o in r["ask"] and 0.01<r["ask"][o]<0.99: legs.append((r["ask"][o], r["truth"]==o))
            if len(legs)==3: p2.append(sum(30*((1.0 if w else 0.0)/p-1) for p,w in legs)/90)
        if p2: print(f"      {h}: n={len(p2)} mean ret {100*st.mean(p2):+.1f}%")
print("\n=== F. model vs market concentration: NO on mode when q_mode - our p_mode > x ===")
for t in ("D-1 12:00","D0 02:00","D0 08:00"):
    rs=[r for r in R if r["t"]==t and r["op"]]
    for x in (0.05,0.10,0.20):
        both(f"{t} NO mode | q_mode - p_mode > {x}",[r for r in rs if r["qm"]-r["op"].get(r["mode"],0)>x],lambda r:r["mode"],-1,NO)
    both(f"{t} YES mode | p_mode - q_mode > 0.10",[r for r in rs if r["op"].get(r["mode"],0)-r["qm"]>0.10],lambda r:r["mode"],1,YES)
