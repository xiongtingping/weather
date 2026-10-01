import json, numpy as np
from collections import defaultdict
S=__file__.rsplit('/',1)[0]
D=json.load(open(S+"/dataset_p1.json")); F=json.load(open(S+"/feat_p1.json"))
g=defaultdict(list)
for r,f in zip(D,F):
    if not 9<=f["local_hour"]<=16: continue
    p=np.array(r["p"]); p/=p.sum(); m=np.array(r["m"]); m/=m.sum(); h=r["hit"]
    cell="上午没下" if f["wet_hours"]==0 else ("下1h" if f["wet_hours"]==1 else "下≥2h")
    pit=p[:h].sum()+0.5*p[h]; mpit=m[:h].sum()+0.5*m[h]
    c=np.array(r["centers"]); 
    g[cell].append((r["weight"],p[h],m[h],pit,mpit,-np.log(max(p[h],1e-3)),-np.log(max(m[h],1e-3)), c[h]-(p*c).sum(), c[h]-(m*c).sum(),(r["station"],r["date"])))
for cell,v in g.items():
    a=np.array([x[:9] for x in v],float); w=a[:,0]
    sd=len({x[9] for x in v})
    def wm(j): return (w*a[:,j]).sum()/w.sum()
    print(f"{cell:6} 站日{sd:4} 行{len(v):4} | 真值桶概率 P1 {wm(1):.3f} 市场 {wm(2):.3f} | LogScore P1 {wm(5):.3f} 市场 {wm(6):.3f} | PIT均值 P1 {wm(3):.2f} 市场 {wm(4):.2f} | PIT<0.1 P1 {(w*(a[:,3]<0.1)).sum()/w.sum():.0%} >0.9 {(w*(a[:,3]>0.9)).sum()/w.sum():.0%} | 真值−中心 p10/p50/p90 P1 "+" ".join(f"{x:+.1f}" for x in np.percentile(a[:,7],[10,50,90])))
