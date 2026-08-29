"""Is the dist=1 buy/no-buy split explained by the value clamp (lambda*claim ceiling)?"""
import sqlite3, json, math
from collections import Counter, defaultdict
c=sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro",uri=True,timeout=60)
setl={(st,ed):sb for st,ed,sb in c.execute("SELECT station_id,event_date,settled_bucket FROM paper_trade_settlement")}
rows=c.execute("""SELECT station_id,event_date,ladder_center_bucket,probabilities_json,prices_json,
 posted_buckets_json,leg_shares_json FROM paper_trade_position""").fetchall()
LAM=0.40
tab=defaultdict(lambda: dict(n=0,hit=0))
cheaper=Counter()
for (st,ed,lcb,probj,pricej,postj,legj) in rows:
    if (st,ed) not in setl: continue
    settled=setl[(st,ed)]
    probs=json.loads(probj or "{}") or {}; prices=json.loads(pricej or "{}") or {}
    posted=set(json.loads(postj or "[]") or [])
    legs={b for b,q in (json.loads(legj or "{}") or {}).items() if float(q)>0}
    ladder=list(probs.keys())
    if lcb not in ladder: continue
    ci=ladder.index(lcb)
    for j in (ci-1,ci+1):
        if not (0<=j<len(ladder)): continue
        b=ladder[j]
        if b not in posted: continue          # only what entry actually chose
        px=prices.get(b); 
        if px is None: continue
        over = px > LAM*probs[b]
        key=("price>0.4*claim" if over else "price<=0.4*claim", b in legs)
        tab[key]["n"]+=1; tab[key]["hit"]+=(b==settled)
    # within-row: is the bought shoulder the cheaper one?
    sh=[ladder[j] for j in (ci-1,ci+1) if 0<=j<len(ladder)]
    if len(sh)==2 and all(s in prices for s in sh):
        b0,b1=sh; f0,f1=b0 in legs,b1 in legs
        if f0!=f1:
            bought=b0 if f0 else b1; unb=b1 if f0 else b0
            cheaper["bought is CHEAPER" if prices[bought]<prices[unb] else
                    ("tie" if prices[bought]==prices[unb] else "bought is dearer")]+=1
print("dist=1 rungs THAT ENTRY POSTED, split by the shoulder value ceiling (lambda=0.40):")
print("%-20s %-9s %5s %9s"%("ceiling test","filled?","n","realised"))
for k in sorted(tab): 
    v=tab[k]; print("%-20s %-9s %5d %9.4f (%d/%d)"%(k[0],"FILLED" if k[1] else "unfilled",v["n"],v["hit"]/v["n"],v["hit"],v["n"]))
print("\nwithin-row, of rows where exactly one shoulder was bought:")
for k,v in cheaper.items(): print("  %-20s %d"%(k,v))
# fisher on filled vs unfilled INSIDE the posted set
def fisher(a,b,cc,d):
    from math import comb
    n=a+b+cc+d; p=0.0
    for i in range(0,min(a+b,a+cc)+1):
        if i<=a: continue
    # one-sided p(X<=a)
    tot=0.0; obs=0.0
    for i in range(max(0,a+cc-(cc+d)), min(a+b,a+cc)+1):
        w=comb(a+b,i)*comb(cc+d,a+cc-i)
        tot+=w
        if i<=a: obs+=w
    return obs/tot
PF=[0,0];PU=[0,0]
for k,v in tab.items():
    (PF if k[1] else PU)[0]+=v["hit"]; (PF if k[1] else PU)[1]+=v["n"]
print("\nINSIDE entry's own posted dist=1 set: filled %d/%d=%.4f  unfilled %d/%d=%.4f  fisher p=%.3g"
      %(PF[0],PF[1],PF[0]/PF[1],PU[0],PU[1],PU[0]/PU[1],fisher(PF[0],PF[1]-PF[0],PU[0],PU[1]-PU[0])))
