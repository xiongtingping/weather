import json, math, numpy as np
from collections import defaultdict
S=__file__.rsplit('/',1)[0]
D=json.load(open(S+"/dataset_p1.json")); F=json.load(open(S+"/sun_feat.json"))
E=[(r,f) for r,f in zip(D,F) if 9<=f["lh"]<=16 and f["fkt"] is not None and f["obs_mean"] is not None]
kts=np.array([f["fkt"] for _,f in E]); t1,t2=np.percentile(kts,[33.3,66.7])
print(f"Kt 三分位 {t1:.2f} / {t2:.2f}")
def fc(f): return "预报阴" if f["fkt"]<=t1 else ("预报晴" if f["fkt"]>=t2 else "预报中")
def ob(f,k="obs_mean"):
    x=f[k]; return None if x is None else ("实际晴" if x<=0.3 else ("实际阴" if x>=0.8 else "实际中"))
for key,title in (("obs_mean","实时：09时→决策时刻已见云量"),("rest_mean","神谕：决策后到16时的云量")):
    print("\n==",title)
    g=defaultdict(list)
    for r,f in E:
        o=ob(f,key)
        if o is None: continue
        p=np.array(r["p"]);p/=p.sum(); m=np.array(r["m"]);m/=m.sum(); h=r["hit"]; c=np.array(r["centers"])
        g[fc(f)+"·"+o].append((p[:h].sum()+.5*p[h],-math.log(max(p[h],1e-3)),-math.log(max(m[h],1e-3)),c[h]-(p*c).sum(),(r["station"],r["date"])))
    for c in sorted(g):
        a=np.array([x[:4] for x in g[c]]); sd=len({x[4] for x in g[c]})
        print(f"{c:10} 行{len(a):4} 站日{sd:4} | PIT均值 {a[:,0].mean():.2f} <0.1 {np.mean(a[:,0]<.1):4.0%} >0.9 {np.mean(a[:,0]>.9):4.0%} | LogScore P1 {a[:,1].mean():.3f} 市场 {a[:,2].mean():.3f} | 真值−中心 p10/p50/p90 "+" ".join(f"{x:+.1f}" for x in np.percentile(a[:,3],[10,50,90])))
