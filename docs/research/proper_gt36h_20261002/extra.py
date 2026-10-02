"""补充：穿价判据改为触价(≤)；剔除地板腿(p≤0.0011)；按 NO 价格分档。车道口径=每站日首个可下注时点、只 NO。"""
import sys, numpy as np
from collections import defaultdict
import replay as R
rows = R.load(sys.argv[1]); by = defaultdict(list)
for r in rows: by[(r["st"], r["day"])].append(r)
for v in by.values(): v.sort(key=lambda r: -r["H"])
rng = np.random.default_rng(R.SEED)
def f(x): return "—" if not x else f"{100*x['roi']:+6.1f}% [{100*x['lo']:+6.1f},{100*x['hi']:+6.1f}] n={x['n']:4d} d={x['days']:2d} 胜={x['win']:.0%} 成本={x['cost']:.0f}"
def go(mode, keep=lambda p,c: True, strict=True):
    items=[]
    for seq in by.values():
        i = next((i for i,r in enumerate(seq) if R.legs(r,("NO",))), None)
        if i is None: continue
        r=seq[i]
        for k,side,w in R.legs(r,("NO",)):
            p=r["p"][k]
            c = R.touch_fill(seq,i,k,side,w,p,strict=strict) if mode=="touch" else R.price(r,k,side,mode,p)
            if c is None or not (0<c<1) or not keep(p,c): continue
            b=R.bet(k,side,w,c,r["y"]); b.update(day=r["day"]); items.append(b)
    return items
for lab,mode,strict in (("taker","taker",True),("maker乐观","maker",True),("穿价<","touch",True),("触价≤","touch",False)):
    it=go(mode,strict=strict)
    print(f"{lab:8s} 全 {f(R.pooled(it,rng))}")
    print(f"{'':8s} 非地板 {f(R.pooled([b for b,(p) in zip(it,[0]*len(it))],rng)) if False else ''}", end="")
    nf=go(mode,keep=lambda p,c: p>0.0011,strict=strict); print(f"非地板腿 {f(R.pooled(nf,rng))}")
    for lo,hi in ((0,0.5),(0.5,0.9),(0.9,0.98),(0.98,1)):
        b=go(mode,keep=lambda p,c,lo=lo,hi=hi: lo<=c<hi,strict=strict); print(f"   NO价[{lo},{hi}) {f(R.pooled(b,rng))}")
