import json, math, statistics as st, collections
S=json.load(open("sd_sessions.json"))
for s in S:
    for k in ("q","bid","ask","yes_usdc","no_usdc","yes_sh","no_sh"): s[k]={int(a):b for a,b in s[k].items()}
# legs: (price, usdc, side, win, session half)
legs=[]
for s in S:
    if s["truth_ord"] is None: continue
    half="H1" if s["event_date"]<"2026-09-30" else "H2"
    for o,u in s["yes_usdc"].items():
        if o not in s["ask"] or s["yes_sh"].get(o,0)<=0: continue
        price=u/s["yes_sh"][o]; legs.append(dict(price=price,usdc=u,win=1.0 if s["truth_ord"]==o else 0.0,side="YES",half=half,mid=s["q"].get(o,price),lead=s["lead_h"]))
    for o,u in s["no_usdc"].items():
        if o not in s["bid"] or s["no_sh"].get(o,0)<=0: continue
        price=u/s["no_sh"][o]; legs.append(dict(price=price,usdc=u,win=1.0 if s["truth_ord"]!=o else 0.0,side="NO",half=half,mid=1-s["q"].get(o,1-price),lead=s["lead_h"]))
print("legs:",len(legs),"| USDC:",round(sum(l["usdc"] for l in legs)))
def implied(l,c): return min(0.999,max(0.001,l["price"]+c*l["usdc"]*(1-l["price"])))   # Kelly: f=(p-a)/(1-a) => p = a + f(1-a), f = usdc/bankroll, c=1/bankroll
def logloss(ls,c): return st.mean(-(l["win"]*math.log(implied(l,c))+(1-l["win"])*math.log(1-implied(l,c))) for l in ls)
def brier(ls,c): return st.mean((implied(l,c)-l["win"])**2 for l in ls)
H1=[l for l in legs if l["half"]=="H1"]; H2=[l for l in legs if l["half"]=="H2"]
CS=[0,0.0005,0.001,0.002,0.003,0.005,0.008,0.012,0.02,0.03]
print("A. Kelly inverse p = price + c*USDC*(1-price); c = 1/bankroll. Binary logloss by c:")
for name,trn,tst in (("fit H1 test H2",H1,H2),("fit H2 test H1",H2,H1)):
    best=min(CS,key=lambda c:logloss(trn,c))
    print(f"   {name}: c*={best} (bankroll≈{1/best if best else float('inf'):.0f} U) | test logloss c=0 (price) {logloss(tst,0):.4f} -> c* {logloss(tst,best):.4f} | test Brier {brier(tst,0):.4f} -> {brier(tst,best):.4f}")
    print("     train logloss profile:"," ".join(f"{c}:{logloss(trn,c):.4f}" for c in CS))
# calibration table at c=0.003 (approx fit) on all legs: by implied-edge (c*usdc*(1-price)) decile
c=0.003
legs.sort(key=lambda l:l["usdc"]*(1-l["price"])); n=len(legs)
print(f"B. calibration by size-implied edge decile (c={c}): decile | n | mean price | implied p | realized | USDC")
for d in range(10):
    ls=legs[d*n//10:(d+1)*n//10]; print(f"   D{d+1:2d} n={len(ls):4d} price={st.mean(l['price'] for l in ls):.3f} implied={st.mean(implied(l,c) for l in ls):.3f} realized={st.mean(l['win'] for l in ls):.3f} | USDC-wtd realized-price {sum(l['usdc']*(l['win']-l['price']) for l in ls)/sum(l['usdc'] for l in ls):+.3f} | USDC {sum(l['usdc'] for l in ls):.0f}")
# C. same but per side
for side in ("YES","NO"):
    ls=[l for l in legs if l["side"]==side]; ls.sort(key=lambda l:l["usdc"]); n2=len(ls)
    print(f"C. {side}: realized - price by USDC tercile:"," | ".join(f"{name}: n={len(x)} price {st.mean(l['price'] for l in x):.3f} realized {st.mean(l['win'] for l in x):.3f} edge {st.mean(l['win']-l['price'] for l in x):+.3f}" for name,x in (("small",ls[:n2//3]),("mid",ls[n2//3:2*n2//3]),("large",ls[2*n2//3:]))))
# D. does the implied p (c*) beat the MARKET MID as a forecast of the leg? (mid available)
for name,tst in (("H2",H2),("H1",H1)):
    lm=st.mean(-(l["win"]*math.log(min(.999,max(.001,l["mid"])))+(1-l["win"])*math.log(1-min(.999,max(.001,l["mid"])))) for l in tst)
    print(f"D. {name}: logloss market mid {lm:.4f} | price {logloss(tst,0):.4f} | implied c=0.003 {logloss(tst,0.003):.4f}")
