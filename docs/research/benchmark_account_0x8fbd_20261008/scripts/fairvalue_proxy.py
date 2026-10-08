import json, glob, bisect, math, statistics as st, collections, datetime as dt
from zoneinfo import ZoneInfo
E=json.load(open("events_enriched.json")); reg=json.load(open("registry.json")); tz={r["station_id"]:r["settlement_timezone"] for r in reg}
H={}
for p in glob.glob("hist_b*.json")+["hist.json"]: H.update(json.load(open(p)))
S={cid:([h["t"] for h in hs],[h["p"] for h in hs]) for cid,hs in H.items()}
def mid_at(cid,ts,maxage=1800):
    if cid not in S: return None
    T,P=S[cid]; i=bisect.bisect_right(T,ts)-1
    return P[i] if i>=0 and ts-T[i]<=maxage else None
def mid_after(cid,ts,dh=1):
    if cid not in S: return None
    T,P=S[cid]; i=bisect.bisect_right(T,ts+dh*3600)-1
    return P[i] if i>=0 and T[i]>ts else None
rows=[]
for e in E:
    if not e["first_taker"] or e["truth_ord"] is None or e["mid0"] is None: continue
    m60=mid_after(e["cid"],e["ts"]); 
    if m60 is None: continue
    sgn=1 if e["outcome"]=="Yes" else -1
    own=lambda m: m if sgn>0 else 1-m
    hit=1.0 if ((e["truth_ord"]==e["my_ord"]) if sgn>0 else (e["truth_ord"]!=e["my_ord"])) else 0.0
    lt=dt.datetime.fromtimestamp(e["ts"],dt.timezone.utc).astimezone(ZoneInfo(tz.get(e["station"],"UTC")))
    r=dict(st=e["station"],ed=e["event_date"],side=e["outcome"],mid0=own(e["mid0"]),F=own(m60),M=own(m60)-own(e["mid0"]),hit=hit,price=e["first_price"],usdc=e["usdc"],lead=e["lead_h"],lhour=lt.hour,unit=e["unit"],half="H1" if e["event_date"]<"2026-09-30" else "H2")
    if e["market"]: r.update(d=e["market"]["d"],qm=e["market"]["q_mode"],skew=e["market"]["skew"],spread=e["market"]["spread"])
    if e["p0"] and e["p0"]["p_my"] is not None: r["ours"]=e["p0"]["p_my"] if sgn>0 else 1-e["p0"]["p_my"]
    rows.append(r)
print("taker-first episodes with mid before/after & truth:",len(rows))
W=lambda rs,fn: sum(x["usdc"]*fn(x) for x in rs)/sum(x["usdc"] for x in rs) if rs else float('nan')
U=lambda rs,fn: st.mean(fn(x) for x in rs) if rs else float('nan')
print(f"overall: M (F - mid0) mean {100*U(rows,lambda x:x['M']):+.1f}c (USDC-wtd {100*W(rows,lambda x:x['M']):+.1f}c) | hit - F {100*U(rows,lambda x:x['hit']-x['F']):+.1f}c | hit - mid0 {100*U(rows,lambda x:x['hit']-x['mid0']):+.1f}c | hit - price {100*U(rows,lambda x:x['hit']-x['price']):+.1f}c")
# 1. is F a good forecast? calibration of F vs mid0 vs ours (binary on their bucket/side)
def ll(p,h): p=min(.999,max(.001,p)); return -(h*math.log(p)+(1-h)*math.log(1-p))
print(f"binary logloss on their legs: mid0 {U(rows,lambda x:ll(x['mid0'],x['hit'])):.4f} | F(mid+60) {U(rows,lambda x:ll(x['F'],x['hit'])):.4f} | price {U(rows,lambda x:ll(x['price'],x['hit'])):.4f} | ours {U([x for x in rows if 'ours' in x],lambda x:ll(x['ours'],x['hit'])):.4f} (n={sum(1 for x in rows if 'ours' in x)})")
print("calibration by F decile: F mean -> realized")
rs=sorted(rows,key=lambda x:x["F"]); n=len(rs)
print("   "+" | ".join(f"{U(rs[i*n//10:(i+1)*n//10],lambda x:x['F']):.2f}->{U(rs[i*n//10:(i+1)*n//10],lambda x:x['hit']):.2f}" for i in range(10)))
# 2. what explains M? bins
def show(tag,groups):
    print(f"  M by {tag}:")
    for name,rs in groups:
        if len(rs)>=40: print(f"     {name:22s} n={len(rs):4d} M {100*U(rs,lambda x:x['M']):+.1f}c | hit-F {100*U(rs,lambda x:x['hit']-x['F']):+.1f}c | hit-mid0 {100*U(rs,lambda x:x['hit']-x['mid0']):+.1f}c | H1 M {100*U([x for x in rs if x['half']=='H1'],lambda x:x['M']):+.1f} H2 {100*U([x for x in rs if x['half']=='H2'],lambda x:x['M']):+.1f}")
show("side",[(s,[x for x in rows if x["side"]==s]) for s in ("Yes","No")])
show("lead",[(f"{lo}..{hi}h",[x for x in rows if lo<=x["lead"]<hi]) for lo,hi in ((-24,0),(0,12),(12,24),(24,48),(48,200))])
show("local hour",[(f"{lo:02d}-{hi:02d}",[x for x in rows if lo<=x["lhour"]<hi]) for lo,hi in ((0,6),(6,12),(12,18),(18,24))])
show("bucket vs mode (d)",[(f"d={d}",[x for x in rows if x.get("d")==d]) for d in (-2,-1,0,1,2)])
show("q_mode",[(f"{lo}-{hi}",[x for x in rows if "qm" in x and lo<=x["qm"]<hi]) for lo,hi in ((0,0.35),(0.35,0.45),(0.45,0.55),(0.55,1))])
show("mid0 level",[(f"{lo}-{hi}",[x for x in rows if lo<=x["mid0"]<hi]) for lo,hi in ((0,0.15),(0.15,0.3),(0.3,0.5),(0.5,0.7),(0.7,1))])
show("ours - mid0",[(name,[x for x in rows if "ours" in x and lo<=x["ours"]-x["mid0"]<hi]) for lo,hi,name in ((-1,-0.1,"ours << mid"),(-0.1,-0.03,"ours < mid"),(-0.03,0.03,"ours ≈ mid"),(0.03,0.1,"ours > mid"),(0.1,1,"ours >> mid"))])
show("episode USDC",[(name,[x for x in rows if lo<=x["usdc"]<hi]) for lo,hi,name in ((0,5,"<5U"),(5,20,"5-20U"),(20,60,"20-60U"),(60,1e9,">=60U"))])
show("unit",[(u,[x for x in rows if x["unit"]==u]) for u in ("C","F")])
# 3. OLS of M on ex-ante observables (no size), H1 fit -> H2 predict; does predicted M rank realized edge?
def feats(x): return [1.0,x["mid0"],x["mid0"]**2,1.0 if x["side"]=="Yes" else 0.0,min(x["lead"],72)/24,1.0 if 0<=x["lhour"]<9 else 0.0,x.get("d",0) if x.get("d") is not None else 0,1.0 if x.get("d")==0 else 0.0,x.get("qm",0.45),(x.get("ours",x["mid0"])-x["mid0"]),1.0 if x["unit"]=="F" else 0.0]
def ols(X,y):
    n=len(X); k=len(X[0]); A=[[sum(X[i][a]*X[i][b] for i in range(n)) for b in range(k)]+[sum(X[i][a]*y[i] for i in range(n))] for a in range(k)]
    for c in range(k):
        p=max(range(c,k),key=lambda r:abs(A[r][c])); A[c],A[p]=A[p],A[c]
        if abs(A[c][c])<1e-12: continue
        for r in range(k):
            if r!=c: f=A[r][c]/A[c][c]; A[r]=[A[r][j]-f*A[c][j] for j in range(k+1)]
    return [A[i][k]/A[i][i] if abs(A[i][i])>1e-12 else 0.0 for i in range(k)]
names=["const","mid0","mid0²","YES","lead/24","local 0-9h","d","d==0","q_mode","ours-mid0","F-station"]
for name,trn,tst in (("fit H1 -> H2",[x for x in rows if x["half"]=="H1"],[x for x in rows if x["half"]=="H2"]),("fit H2 -> H1",[x for x in rows if x["half"]=="H2"],[x for x in rows if x["half"]=="H1"])):
    b=ols([feats(x) for x in trn],[x["M"] for x in trn]); pred=[sum(bb*f for bb,f in zip(b,feats(x))) for x in tst]
    ybar=st.mean(x["M"] for x in trn); r2_in=1-sum((x["M"]-sum(bb*f for bb,f in zip(b,feats(x))))**2 for x in trn)/sum((x["M"]-ybar)**2 for x in trn)
    order=sorted(range(len(tst)),key=lambda i:pred[i]); n=len(tst)
    print(f"\nOLS M ~ observables, {name}: in-sample R2 {r2_in:.3f}; betas",{k:round(v,3) for k,v in zip(names,b)})
    print("   test set by predicted-M quintile: pred M | actual M | hit - mid0 | hit - price")
    for q in range(5):
        rs=[tst[i] for i in order[q*n//5:(q+1)*n//5]]; pm=st.mean(pred[i] for i in order[q*n//5:(q+1)*n//5])
        print(f"     Q{q+1} pred {100*pm:+.1f}c | actual M {100*U(rs,lambda x:x['M']):+.1f}c | hit-mid0 {100*U(rs,lambda x:x['hit']-x['mid0']):+.1f}c | hit-price {100*U(rs,lambda x:x['hit']-x['price']):+.1f}c (n={len(rs)})")
# 4. station-level M stability H1 vs H2
bys=collections.defaultdict(lambda:{"H1":[],"H2":[]})
for x in rows: bys[x["st"]][x["half"]].append(x["M"])
pairs=[(st.mean(v["H1"]),st.mean(v["H2"]),s) for s,v in bys.items() if len(v["H1"])>=8 and len(v["H2"])>=8]
if len(pairs)>5:
    a=[p[0] for p in pairs]; b_=[p[1] for p in pairs]; ma,mb=st.mean(a),st.mean(b_); cov=sum((x-ma)*(y-mb) for x,y in zip(a,b_))/len(a)
    print(f"\nstation-level M: corr(H1,H2) across {len(pairs)} stations = {cov/(st.pstdev(a)*st.pstdev(b_)):+.2f} | top H1 stations:",sorted(pairs,key=lambda p:-p[0])[:5])
