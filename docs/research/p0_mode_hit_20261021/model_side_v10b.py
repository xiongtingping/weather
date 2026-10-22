import os, math, statistics, collections, random, pickle
HERE = os.path.dirname(os.path.abspath(__file__))
recs = pickle.load(open(os.path.join(HERE, "model_side_v10_recs.pkl"), "rb"))
out = open(os.path.join(HERE, "model_side_v10b.txt"), "w")
def P(*a): print(*a, flush=True); print(*a, file=out)
def floor_norm(p):
    s=sum(p); p=[max(x/s,1e-3) for x in p]; s=sum(p); return [x/s for x in p]
def shift(p, delta):
    K=len(p); o=[0.0]*K
    for i,m in enumerate(p):
        lo=i+delta; a=math.floor(lo); f=lo-a
        for j,w in ((a,1-f),(a+1,f)):
            if w<=0: continue
            j=min(max(j,0),K-1); o[j]+=m*w
    return o
def crps(p,temps,y,rung):
    c=0.0; F=0.0
    for pk,tk in zip(p,temps):
        F+=pk; c+=(F-(1.0 if y<=tk+rung/2 else 0.0))**2*rung
    return c
def brier(p,yi): return sum((pk-(1.0 if k==yi else 0.0))**2 for k,pk in enumerate(p))
def ols(pairs):
    x=[a for a,b in pairs]; y=[b for a,b in pairs]; n=len(x)
    if n<30: return None
    mx,my=sum(x)/n,sum(y)/n; sxx=sum((a-mx)**2 for a in x)
    if sxx<=0: return None
    b=sum((a-mx)*(c-my) for a,c in zip(x,y))/sxx; a0=my-b*mx; se=math.sqrt(sum((c-(a0+b*a))**2 for a,c in zip(x,y))/(n-2)/sxx); return a0,b,se,n
def score(rs,new_mc):
    o=collections.defaultdict(float); dday=collections.defaultdict(list); n=0
    for r in rs:
        m1=new_mc(r)
        if m1 is None: continue
        p1=floor_norm(shift(r["p"],(m1-r["mc"])/r["rung"])); l0=-math.log(r["p"][r["yi"]]); l1=-math.log(p1[r["yi"]])
        n+=1; o["r0"]+=(r["mc"]-r["y"])**2; o["r1"]+=(m1-r["y"])**2; o["l0"]+=l0; o["l1"]+=l1; o["b0"]+=brier(r["p"],r["yi"]); o["b1"]+=brier(p1,r["yi"]); o["c0"]+=crps(r["p"],r["temps"],r["y"],r["rung"]); o["c1"]+=crps(p1,r["temps"],r["y"],r["rung"]); dday[r["d"]].append(l1-l0)
    if n<25: return None
    days=list(dday); rnd=random.Random(4); bs=[]
    for _ in range(300):
        s=[z for d in (rnd.choice(days) for _ in days) for z in dday[d]]; bs.append(statistics.mean(s))
    bs.sort(); return dict(n=n,rmse=(math.sqrt(o["r0"]/n),math.sqrt(o["r1"]/n)),ls=(o["l0"]/n,o["l1"]/n,bs[7],bs[292]),brier=(o["b0"]/n,o["b1"]/n),crps=(o["c0"]/n,o["c1"]/n))
def show(name,rs,fn):
    s=score(rs,fn)
    if not s: P(f"   {name:40s} n<25"); return
    P(f"   {name:40s} n={s['n']:4d} RMSE {s['rmse'][0]:.3f}→{s['rmse'][1]:.3f}  LogScore Δ {s['ls'][1]-s['ls'][0]:+.4f} [{s['ls'][2]:+.4f},{s['ls'][3]:+.4f}]  Brier {s['brier'][0]:.4f}→{s['brier'][1]:.4f}  CRPS {s['crps'][0]:.3f}→{s['crps'][1]:.3f}")
for fam in ("P0>36h","P0-M"):
    rs=[r for r in recs if r["fam"]==fam and r["F"] is not None]
    days=sorted({r["d"] for r in rs}); ev=[]
    for d in days:
        prior=[r for r in rs if r["d"]<d]
        if len({r["d"] for r in prior})<8: continue
        bias=statistics.mean(r["y"]-r["F"] for r in prior); o=ols([(r["F"]+bias-r["mc"],r["y"]-r["mc"]) for r in prior]); kap=o[1] if o else 0.0
        for r in rs:
            if r["d"]==d: r["kap"]=kap; r["bias"]=bias; ev.append(r)
    fn=lambda r: r["mc"]+r["kap"]*(r["F"]+r["bias"]-r["mc"])
    P(f"\n==================== {fam}: 前推 {len(ev)} 决策（{len({r['d'] for r in ev})} 天，{len({r['st'] for r in ev})} 站）κ 序列 {sorted({round(r['kap'],2) for r in ev})[:3]}…{sorted({round(r['kap'],2) for r in ev})[-3:]} ====================")
    show("全部",ev,fn)
    for mo in ("09","10"): show(f"  {mo} 月",[r for r in ev if r["d"][5:7]==mo],fn)
    for u in ("C","F"): show(f"  {u} 站",[r for r in ev if r["unit"]==u],fn)
    for k in (3,4,5,6): show(f"  F 家数={k}",[r for r in ev if r["n_det"]==k],fn)
    for k in (2,3,4,5,6): show(f"  池家数={k}",[r for r in ev if r["n_sys"]==k],fn)
    show("  池家数≥4",[r for r in ev if r["n_sys"]>=4],fn)
    by=collections.defaultdict(list)
    for r in ev: by[r["st"]].append(r)
    res=[]
    for st,g in by.items():
        if len(g)<8: continue
        d=statistics.mean(-math.log(floor_norm(shift(r["p"],(fn(r)-r["mc"])/r["rung"]))[r["yi"]])+math.log(r["p"][r["yi"]]) for r in g); res.append((d,st,len(g)))
    res.sort(); P(f"   按站（≥8 决策）：{sum(1 for d,_,_ in res if d<0)}/{len(res)} 站 LogScore 改善；最好 {[(s,round(d,3),n) for d,s,n in res[:3]]}；最差 {[(s,round(d,3),n) for d,s,n in res[-3:]]}")
    for nm,key in (("只用 aifs1","aifs1"),("只用 ifs9","ifs9"),("只用 ukmo_r","ukmo_r"),("只用 cma","cma")):
        g=[r for r in ev if key in r["det"]]
        if len(g)<30: continue
        kb=statistics.mean(r["y"]-r["det"][key] for r in g); o=ols([(r["det"][key]+kb-r["mc"],r["y"]-r["mc"]) for r in g]); k=o[1]
        show(f"  {nm} (样本内κ={k:+.2f})",g,lambda r,key=key,kb=kb,k=k: r["mc"]+k*(r["det"][key]+kb-r["mc"]))
    show("  固定 κ=0.3",ev,lambda r: r["mc"]+0.3*(r["F"]+r["bias"]-r["mc"]))
    show("  κ=0.3 不修 F 偏差",ev,lambda r: r["mc"]+0.3*(r["F"]-r["mc"]))
    show("  κ=0.15",ev,lambda r: r["mc"]+0.15*(r["F"]+r["bias"]-r["mc"]))
