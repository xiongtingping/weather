import json, math, statistics as st, collections
S=json.load(open("sd_sessions.json")); B=json.load(open("baseline3.json"))
for s in S:
    for k in ("q","bid","ask","yes_usdc","no_usdc","yes_sh","no_sh"): s[k]={int(a):b for a,b in s[k].items()}
    if s["ours"]: s["ours"]["p"]={int(a):b for a,b in s["ours"]["p"].items()}
FLOOR=1e-3
def deform(q,lam,shift):
    # q: dict ord->prob over contiguous ordinals; shift in bucket units (positive = warmer) via linear interpolation; then power lam; renormalize
    ords=sorted(q); lo,hi=ords[0],ords[-1]; out={}
    for o in ords:
        x=o-shift; i=math.floor(x); fr=x-i
        v=(q.get(i,0.0) if lo<=i<=hi else 0.0)*(1-fr)+(q.get(i+1,0.0) if lo<=i+1<=hi else 0.0)*fr
        out[o]=max(v,1e-6)**lam
    t=sum(out.values()); return {o:v/t for o,v in out.items()}
def score(sess,lam,shift):
    """USDC-weighted implied edge of their bets under belief = deform(q)"""
    tot=0.0
    for s in sess:
        b=deform(s["q"],lam,shift)
        for o,u in s["yes_usdc"].items():
            if o in s["ask"]: tot+=u*(b.get(o,0)-s["ask"][o])
        for o,u in s["no_usdc"].items():
            if o in s["bid"]: tot+=u*(s["bid"][o]-b.get(o,0))
    return tot
def agree(sess,lam,shift):
    pos=0.0; tot=0.0
    for s in sess:
        b=deform(s["q"],lam,shift)
        for o,u in s["yes_usdc"].items():
            if o in s["ask"]: tot+=u; pos+=u*(b.get(o,0)>s["ask"][o])
        for o,u in s["no_usdc"].items():
            if o in s["bid"]: tot+=u; pos+=u*(b.get(o,0)<s["bid"][o])
    return pos/tot if tot else float('nan')
def logs(p,truth):
    q={o:max(v,FLOOR) for o,v in p.items()}; t=sum(q.values()); return -math.log(q.get(truth,FLOOR)/t), sum((q[o]/t-(1.0 if o==truth else 0.0))**2 for o in q)
LAMS=[0.3,0.4,0.5,0.6,0.7,0.8,0.9,1.0,1.15,1.3,1.5]; SH=[round(x*0.1,1) for x in range(-10,11)]
def lead_class(h): return "D0" if h<12 else "D-1" if h<36 else "D-2+"
for cls in ("ALL","D-2+","D-1","D0"):
    sess=[s for s in S if cls=="ALL" or lead_class(s["lead_h"])==cls]
    best=max(((agree(sess,l,sh),l,sh) for l in LAMS for sh in SH),key=lambda x:x[0])
    base=agree(sess,1.0,0.0)
    print(f"=== {cls}: sessions={len(sess)} | bounded fit (max share of their USDC with implied positive edge): λ*={best[1]} shift*={best[2]:+.1f} -> {best[0]:.2f} vs market-as-belief {base:.2f}")
    print("   λ profile (shift*):"," ".join(f"{l}:{agree(sess,l,best[2]):.2f}" for l in LAMS))
    print("   shift profile (λ*):"," ".join(f"{sh:+.1f}:{agree(sess,best[1],sh):.2f}" for sh in SH[::2]))
    # evaluate implied belief vs truth on their sessions with truth, vs market q and ours
    withT=[s for s in sess if s["truth_ord"] is not None]
    res=collections.defaultdict(list)
    for s in withT:
        res["market q"].append(logs(s["q"],s["truth_ord"])); res[f"deformed (λ*,s*)"].append(logs(deform(s["q"],best[1],best[2]),s["truth_ord"]))
        res["deformed λ only"].append(logs(deform(s["q"],best[1],0.0),s["truth_ord"])); res["deformed shift only"].append(logs(deform(s["q"],1.0,best[2]),s["truth_ord"]))
        if s["ours"]: res["ours served"].append(logs(s["ours"]["p"],s["truth_ord"]))
    for k,v in sorted(res.items(),key=lambda kv:st.mean(x[0] for x in kv[1])): print(f"   {k:22s} n={len(v)} LogS={st.mean(x[0] for x in v):.4f} Brier={st.mean(x[1] for x in v):.4f}")
# cross-validation: fit on H1 sessions, evaluate on H2 truth (and reverse), ALL leads
H1=[s for s in S if s["event_date"]<"2026-09-30"]; H2=[s for s in S if s["event_date"]>="2026-09-30"]
for trn,tst,name in ((H1,H2,"fit H1 -> test H2"),(H2,H1,"fit H2 -> test H1")):
    best=max(((agree(trn,l,sh),l,sh) for l in LAMS for sh in SH),key=lambda x:x[0]); withT=[s for s in tst if s["truth_ord"] is not None]
    if not withT: print("CV",name,"no test sessions"); continue
    m=st.mean(logs(s["q"],s["truth_ord"])[0] for s in withT); d=st.mean(logs(deform(s["q"],best[1],best[2]),s["truth_ord"])[0] for s in withT)
    print(f"CV {name}: λ*={best[1]} shift*={best[2]:+.1f} | test LogS market {m:.4f} vs deformed {d:.4f} (n={len(withT)})")
# shape of their belief vs market: USDC-weighted implied edge by (ordinal - market mode)
prof=collections.defaultdict(lambda:[0.0,0.0])
for s in S:
    mode=max(s["q"].items(),key=lambda kv:(kv[1],-kv[0]))[0]
    for o,u in s["yes_usdc"].items():
        if o in s["ask"]: prof[max(-3,min(3,o-mode))][0]+=u; prof[max(-3,min(3,o-mode))][1]+=u
    for o,u in s["no_usdc"].items():
        if o in s["bid"]: prof[max(-3,min(3,o-mode))][0]-=u; prof[max(-3,min(3,o-mode))][1]+=u
print("net direction of their USDC by bucket - market mode (YES + / NO -), share of gross:",{k:f"{v[0]/v[1]:+.2f} (gross {v[1]:.0f})" for k,v in sorted(prof.items())})
