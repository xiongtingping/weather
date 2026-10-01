import json, math, numpy as np
from collections import defaultdict, Counter
S=__file__.rsplit('/',1)[0]
D=json.load(open(S+"/dataset_p1.json")); L=json.load(open(S+"/latency_p1.json"))
def cell(o):
    if o is None or not 9<=o["lh"]<=16: return None
    a="看见在下" if o["now"] else ("刚停" if o["any2h"] else "看见没下")
    return a+"·"+("下一报有雨" if o["next_rain"] else "下一报无雨")
print("== 诊断（9–16时 P1 决策点）")
g=defaultdict(list)
for r,o in zip(D,L):
    c=cell(o)
    if not c: continue
    p=np.array(r["p"]);p/=p.sum(); m=np.array(r["m"]);m/=m.sum(); h=r["hit"]; cc=np.array(r["centers"])
    g[c].append((p[h],m[h],p[:h].sum()+.5*p[h],-math.log(max(p[h],1e-3)),-math.log(max(m[h],1e-3)),cc[h]-(p*cc).sum(),(r["station"],r["date"])))
for c in sorted(g):
    a=np.array([x[:6] for x in g[c]]); sd=len({x[6] for x in g[c]})
    print(f"{c:14} 行{len(a):5} 站日{sd:4} | 真值桶 P1 {a[:,0].mean():.3f} 市场 {a[:,1].mean():.3f} | LogScore P1 {a[:,3].mean():.3f} 市场 {a[:,4].mean():.3f} | PIT均值 {a[:,2].mean():.2f} <0.1 {np.mean(a[:,2]<.1):.0%} >0.9 {np.mean(a[:,2]>.9):.0%} | 真值−中心 p10/p50/p90 "+" ".join(f"{x:+.1f}" for x in np.percentile(a[:,5],[10,50,90])))
