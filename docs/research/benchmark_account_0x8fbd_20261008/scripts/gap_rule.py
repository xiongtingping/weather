import json, collections, statistics as st
R=json.load(open("baseline3_rows.json")); B=json.load(open("baseline3.json"))
for r in R:
    for k in ("q","ask","bid"): r[k]={int(a):b for a,b in r[k].items()}
    rec=B[f"{r['st']}|{r['ed']}"][r["t"]]; ours=rec["ours"]
    r["op"]={ours["ord"][b]:p for b,p in ours["p"].items()} if ours else None; r["stage"]=ours["stage"] if ours else None
    r["gap"]=(r["qm"]-r["op"].get(r["mode"],0)) if r["op"] else None
SH={"FACT","NZWN","SAEZ","SBGR"}
def region(s): return "US-F" if s[0]=="K" else "SH" if s in SH else "NH-C"
def week(ed): return max(k for k in ("2026-09-01","2026-09-06","2026-09-13","2026-09-20","2026-09-27","2026-10-04") if k<=ed)[5:]
def nomode(rs,price="taker"):
    xs=[]
    for r in rs:
        if r["mode"] not in r["bid"] or r["mode"] not in r["ask"]: continue
        p=(1-r["bid"][r["mode"]]) if price=="taker" else (1-r["ask"][r["mode"]])
        if not 0.01<p<0.99: continue
        xs.append((p,1.0 if r["truth"]!=r["mode"] else 0.0))
    if len(xs)<10: return None
    p=st.mean(x[0] for x in xs); h=st.mean(x[1] for x in xs); return len(xs),p,h,(h/p-1)*100
def show(tag,res): 
    if res: print(f"   {tag:40s} n={res[0]:4d} NO paid={res[1]:.3f} mode lost={res[2]:.3f} ret={res[3]:+6.1f}%")
for t in ("D0 02:00","D0 08:00","D-1 12:00","D-1 18:00"):
    rs=[r for r in R if r["t"]==t and r["gap"] is not None]
    print(f"=== {t}: NO on market mode by (q_mode - our p_mode) gap; stage mix {dict(collections.Counter(r['stage'] for r in rs))} ===")
    for lo,hi in ((-1,-0.1),(-0.1,0.0),(0.0,0.1),(0.1,0.2),(0.2,0.3),(0.3,1)):
        show(f"gap {lo:+.1f}..{hi:+.1f}",nomode([r for r in rs if lo<=r["gap"]<hi]))
    g=[r for r in rs if r["gap"]>0.2]
    show("gap>0.2 taker (1-bid)",nomode(g)); show("gap>0.2 maker (1-ask)",nomode(g,"maker"))
    for b,(lo,hi) in (("q<0.45",(0,0.45)),("0.45-0.55",(0.45,0.55)),("≥0.55",(0.55,1))): show(f"gap>0.2 & q_mode {b}",nomode([r for r in g if lo<=r["qm"]<hi]))
    for reg in ("NH-C","US-F","SH"): show(f"gap>0.2 & {reg}",nomode([r for r in g if region(r["st"])==reg]))
    for w in ("09-06","09-13","09-20","09-27","10-04"): show(f"gap>0.2 & week {w}",nomode([r for r in g if week(r["ed"])==w]))
    for sg in ("P0","P0-M","P1"): show(f"gap>0.2 & our stage {sg}",nomode([r for r in g if r["stage"]==sg]))
    # when mode loses in gap>0.2 cases, where does truth go? our mode? neighbours?
    lost=[r for r in g if r["truth"]!=r["mode"]]
    if lost:
        om=max((r["op"].items()),key=lambda kv:kv[1])[0] if False else None
        c=collections.Counter()
        for r in lost:
            ours_mode=max(r["op"].items(),key=lambda kv:(kv[1],-kv[0]))[0]
            c["truth=our mode"]+= r["truth"]==ours_mode; c["truth=mode-1"]+= r["truth"]==r["mode"]-1; c["truth=mode+1"]+= r["truth"]==r["mode"]+1; c["further"]+= abs(r["truth"]-r["mode"])>=2
        print(f"   when mode lost (n={len(lost)}): truth=our mode {c['truth=our mode']/len(lost):.2f} | mode-1 {c['truth=mode-1']/len(lost):.2f} | mode+1 {c['truth=mode+1']/len(lost):.2f} | ≥2 away {c['further']/len(lost):.2f}")
    # YES our mode when gap>0.2 and ours mode != market mode, at ask
    xs=[]
    for r in g:
        om=max(r["op"].items(),key=lambda kv:(kv[1],-kv[0]))[0]
        if om!=r["mode"] and om in r["ask"] and 0.01<r["ask"][om]<0.99: xs.append((r["ask"][om],1.0 if r["truth"]==om else 0.0,r["op"][om]))
    if len(xs)>=10: print(f"   YES our mode (≠mkt) when gap>0.2: n={len(xs)} ask={st.mean(x[0] for x in xs):.3f} our p={st.mean(x[2] for x in xs):.3f} hit={st.mean(x[1] for x in xs):.3f} ret={(st.mean(x[1] for x in xs)/st.mean(x[0] for x in xs)-1)*100:+.1f}%")
    # our p vs q at the mode: calibration of each
    print(f"   calibration at mode over all rows: mean q_mode {st.mean(r['qm'] for r in rs):.3f} mean our p_mode {st.mean(r['op'].get(r['mode'],0) for r in rs):.3f} actual mode hit {st.mean(r['truth']==r['mode'] for r in rs):.3f}")
