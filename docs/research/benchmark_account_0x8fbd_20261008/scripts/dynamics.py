import json, collections, statistics as st
R=json.load(open("baseline3_rows.json"))
for r in R:
    for k in ("q","ask","bid"): r[k]={int(a):b for a,b in r[k].items()}
by=collections.defaultdict(dict)
for r in R: by[(r["st"],r["ed"])][r["t"]]=r
SH={"FACT","NZWN","SAEZ","SBGR"}; 
def region(s): return "US-F" if s[0]=="K" else "SH" if s in SH else "NH-C"
def week(ed): return {"2026-09-06":"w1","2026-09-13":"w2","2026-09-20":"w3","2026-09-27":"w4","2026-10-04":"w5"}[max(k for k in ("2026-09-06","2026-09-13","2026-09-20","2026-09-27","2026-10-04") if k<=ed)] if ed>="2026-09-06" else "w0"
print("=== 1. market distribution drift between decision times (ordinal units; negative = colder) ===")
for a,b in (("D-1 06:00","D-1 12:00"),("D-1 12:00","D-1 18:00"),("D-1 18:00","D0 02:00"),("D0 02:00","D0 08:00")):
    pairs=[(d[a],d[b]) for d in by.values() if a in d and b in d]
    if len(pairs)<20: continue
    dmean=[y["mean"]-x["mean"] for x,y in pairs]; dmode=[y["mode"]-x["mode"] for x,y in pairs]
    print(f"  {a} -> {b}: n={len(pairs)} Δmean avg {st.mean(dmean):+.3f} (P<0 {st.mean(v<0 for v in dmean):.2f}, P>0 {st.mean(v>0 for v in dmean):.2f}) | Δmode avg {st.mean(dmode):+.3f} (down {st.mean(v<0 for v in dmode):.2f} / same {st.mean(v==0 for v in dmode):.2f} / up {st.mean(v>0 for v in dmode):.2f})")
    for g in ("NH-C","US-F","SH"):
        pp=[(x,y) for x,y in pairs if region(x["st"])==g]
        if len(pp)>=15: print(f"      {g:5s} n={len(pp)} Δmean {st.mean(y['mean']-x['mean'] for x,y in pp):+.3f} | mode down/up {st.mean(y['mode']<x['mode'] for x,y in pp):.2f}/{st.mean(y['mode']>x['mode'] for x,y in pp):.2f}")
    for h in ("H1","H2"):
        pp=[(x,y) for x,y in pairs if x["half"]==h]
        if pp: print(f"      {h} n={len(pp)} Δmean {st.mean(y['mean']-x['mean'] for x,y in pp):+.3f} | mode down/up {st.mean(y['mode']<x['mode'] for x,y in pp):.2f}/{st.mean(y['mode']>x['mode'] for x,y in pp):.2f}")
print("\n=== 2. D-1 12:00 cold-neighbour (mode-1) YES@ask: breakdown ===")
rows=[r for r in R if r["t"]=="D-1 12:00" and (r["mode"]-1) in r["ask"] and 0.01<r["ask"][r["mode"]-1]<0.99]
def summ(tag,rs):
    if len(rs)<10: return
    hit=st.mean(r["truth"]==r["mode"]-1 for r in rs); ask=st.mean(r["ask"][r["mode"]-1] for r in rs); q=st.mean(r["q"].get(r["mode"]-1,0) for r in rs)
    hitp=st.mean(r["truth"]==r["mode"]+1 for r in rs); askp=st.mean(r["ask"].get(r["mode"]+1,float('nan')) for r in rs if (r["mode"]+1) in r["ask"])
    print(f"  {tag:22s} n={len(rs):3d} cold: q={q:.3f} ask={ask:.3f} hit={hit:.3f} ret={(hit/ask-1)*100:+6.1f}% | warm: ask={askp:.3f} hit={hitp:.3f} ret={(hitp/askp-1)*100:+6.1f}% | mode hit {st.mean(r['truth']==r['mode'] for r in rs):.3f} q_mode {st.mean(r['qm'] for r in rs):.3f}")
summ("ALL",rows)
for g in ("NH-C","US-F","SH"): summ(g,[r for r in rows if region(r["st"])==g])
for w in ("w1","w2","w3","w4","w5"): summ(w,[r for r in rows if week(r["ed"])==w])
for b,(lo,hi) in (("q<0.35",(0,0.35)),("0.35-0.45",(0.35,0.45)),("0.45-0.55",(0.45,0.55)),("≥0.55",(0.55,1))): summ(b,[r for r in rows if lo<=r["qm"]<hi])
# ask spread check for cold nbr: distribution of ask - q
d=[r["ask"][r["mode"]-1]-r["q"].get(r["mode"]-1,0) for r in rows]; print(f"  cold nbr ask - normalized mid: mean {st.mean(d):+.3f} q10/q90 {sorted(d)[len(d)//10]:+.3f}/{sorted(d)[9*len(d)//10]:+.3f}")
# same at D-1 06:00 and D-2 12:00 (earlier) for the cold nbr
for t in ("D-2 12:00","D-1 06:00","D-1 18:00","D0 02:00"):
    rs=[r for r in R if r["t"]==t and (r["mode"]-1) in r["ask"] and 0.01<r["ask"][r["mode"]-1]<0.99]
    if len(rs)>=10:
        hit=st.mean(r["truth"]==r["mode"]-1 for r in rs); ask=st.mean(r["ask"][r["mode"]-1] for r in rs)
        print(f"  [{t}] cold nbr n={len(rs)} ask={ask:.3f} hit={hit:.3f} ret={(hit/ask-1)*100:+.1f}%")
print("\n=== 3. does the D-1 12:00 cold-nbr edge come from a later market shift? value of cold nbr at D-1 18:00 / D0 02:00 (normalized mid) vs ask paid at 12:00 ===")
for later in ("D-1 18:00","D0 02:00","D0 08:00"):
    pp=[]
    for d in by.values():
        if "D-1 12:00" in d and later in d:
            x=d["D-1 12:00"]; o=x["mode"]-1
            if o in x["ask"] and 0.01<x["ask"][o]<0.99 and o in d[later]["q"]:
                pp.append((x["ask"][o],d[later]["q"][o],x["truth"]==o))
    if pp: print(f"  {later}: n={len(pp)} paid(ask@12:00)={st.mean(p for p,_,_ in pp):.3f} -> mid later={st.mean(m for _,m,_ in pp):.3f} ({(st.mean(m for _,m,_ in pp)/st.mean(p for p,_,_ in pp)-1)*100:+.1f}%) | eventual hit {st.mean(h for *_,h in pp):.3f}")
    pp=[]
    for d in by.values():
        if "D-1 12:00" in d and later in d:
            x=d["D-1 12:00"]; o=x["mode"]+1
            if o in x["ask"] and 0.01<x["ask"][o]<0.99 and o in d[later]["q"]:
                pp.append((x["ask"][o],d[later]["q"][o],x["truth"]==o))
    if pp: print(f"  {later}: warm nbr n={len(pp)} paid={st.mean(p for p,_,_ in pp):.3f} -> mid later={st.mean(m for _,m,_ in pp):.3f} ({(st.mean(m for _,m,_ in pp)/st.mean(p for p,_,_ in pp)-1)*100:+.1f}%) | eventual hit {st.mean(h for *_,h in pp):.3f}")
