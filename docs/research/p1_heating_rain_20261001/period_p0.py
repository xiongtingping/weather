import sys, numpy as np, json, math
from collections import defaultdict
sys.path.insert(0, __file__.rsplit('/',1)[0])
import fut_wf as W
rows=W.load("p0")
cloudy=lambda f: f is not None and f["kt"] is not None and f["kt"]<=W.T1
# fixed rule (median of walk-forward picks): s=2, w=0.05 -- report by served-arithmetic period
per=defaultdict(lambda: defaultdict(lambda: np.zeros(3)))
def period(d): return "08-21..09-20 (0.15)" if d<"2026-09-21" else ("09-21..09-24 (0.07/两侧0.12/0.04)" if d<"2026-09-25" else "09-25..09-30 (现行0.18/0.06)")
res=defaultdict(list)
for r in rows:
    if not cloudy(r["f"]): 
        continue
    q=W.ap("shift",(2,0.05),r["p"],-1); d=W.sc(q,r["c"],r["h"])-r["b"]
    per[period(r["day"])][r["sd"]]+=r["w"]*d
    c=r["c"]; p=r["p"]; res[period(r["day"])].append((c[r["h"]]-(p*c).sum(), p[:r["h"]].sum()+.5*p[r["h"]]))
for k in sorted(per):
    M=np.array(list(per[k].values())); rng=np.random.default_rng(0)
    B=np.array([M[rng.integers(0,len(M),len(M))].mean(0) for _ in range(2000)])
    a=np.array(res[k])
    print(f"{k:34} 阴天站日{len(M):4} | 每阴天站日 dLog {M[:,0].mean():+.4f} [{np.percentile(B[:,0],2.5):+.4f},{np.percentile(B[:,0],97.5):+.4f}] dBrier {M[:,1].mean():+.5f} | 真值−中心 p10/p50/p90 "+" ".join(f"{x:+.1f}" for x in np.percentile(a[:,0],[10,50,90]))+f" | PIT<0.05 {np.mean(a[:,1]<.05):.0%} >0.95 {np.mean(a[:,1]>.95):.0%}")
# shape: same diag for sunny/middle
for nm,cond in (("预报晴",lambda f:f and f["kt"] is not None and f["kt"]>=W.T2),("预报中",lambda f:f and f["kt"] is not None and W.T1<f["kt"]<W.T2),("预报阴",cloudy)):
    a=np.array([(r["c"][r["h"]]-(r["p"]*r["c"]).sum(), r["p"][:r["h"]].sum()+.5*r["p"][r["h"]], r["p"][r["h"]]) for r in rows if cond(r["f"])])
    print(f"{nm} 行{len(a)} 真值−中心 p5/p10/p50/p90/p95 "+" ".join(f"{x:+.1f}" for x in np.percentile(a[:,0],[5,10,50,90,95]))+f" | PIT<0.05 {np.mean(a[:,1]<.05):.1%} >0.95 {np.mean(a[:,1]>.95):.1%} | 真值桶<1% {np.mean(a[:,2]<.01):.1%}")
