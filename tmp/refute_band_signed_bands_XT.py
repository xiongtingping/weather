import json,sqlite3,sys,re,math,statistics
sys.path.insert(0,"/Users/xiong/weather")
from scripts.paper_trading_ledger import _is_shoulder,FEE_RATE
con=sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro",uri=True,timeout=60)
sd={(r[0],r[1]):r[2] for r in con.execute("select station_id,event_date,settled_bucket from paper_trade_settlement")}
legs=[]
empty_posted=0
for st,ed,stg,centre,pj,lj,fj,prj in con.execute(
 "select station_id,event_date,stage,ladder_center_bucket,posted_buckets_json,leg_shares_json,resting_fill_json,probabilities_json from paper_trade_position"):
    if (st,ed) not in sd: continue
    posted=json.loads(pj or "[]")
    if not posted: empty_posted+=1
    sb=sd[(st,ed)]; fills=json.loads(fj or "{}")
    for b,q in json.loads(lj or "{}").items():
        q=float(q)
        if q<=0: continue
        m=re.match(r"^(\d+)/(\d+)@([0-9.]+)$",fills[b]); px=float(m.group(3))*(1+FEE_RATE)
        band="centre" if b==centre else ("shoulder" if _is_shoulder(b,centre,posted) else "rest")
        d = posted.index(b)-posted.index(centre) if (b in posted and centre in posted) else None
        legs.append(dict(st=st,ed=ed,stage=stg,b=b,band=band,q=q,px=px,d=d,hit=1 if b==sb else 0))
print("rows with empty posted_buckets_json:",empty_posted,"legs:",len(legs))
print()
print("--- signed distance within shoulder ---")
for d in (-1,1):
    ls=[l for l in legs if l["band"]=="shoulder" and l["d"]==d]
    sh=sum(l["q"] for l in ls);cost=sum(l["q"]*l["px"] for l in ls);pay=sum(l["q"] for l in ls if l["hit"])
    print(" d=%+d n=%3d paid=%.4f legHit=%.1f%% swHit=%.1f%% net=%+.1fu"%(d,len(ls),cost/sh,100*sum(l["hit"] for l in ls)/len(ls),100*pay/sh,pay-cost))
print()
print("--- price bands ---")
for band in ("centre","shoulder","rest"):
    for lo,hi,lab in ((0,.02,"<0.02"),(.02,.05,"0.02-0.05"),(.05,.10,"0.05-0.10"),(.10,.20,"0.10-0.20"),(.20,1.01,">=0.20")):
        ls=[l for l in legs if l["band"]==band and lo<=l["px"]<hi]
        if not ls: continue
        sh=sum(l["q"] for l in ls);cost=sum(l["q"]*l["px"] for l in ls);pay=sum(l["q"] for l in ls if l["hit"])
        print("%-9s %-10s n=%3d paid=%.4f legHit=%5.1f%% net=%+9.1fu ROI=%+7.1f%%"%(band,lab,len(ls),cost/sh,100*sum(l["hit"] for l in ls)/len(ls),pay-cost,100*(pay-cost)/cost))
print()
print("--- stage x band ---")
for stg in ("P0","P0-M","P1"):
    for band in ("centre","shoulder","rest"):
        ls=[l for l in legs if l["band"]==band and l["stage"]==stg]
        if not ls: continue
        sh=sum(l["q"] for l in ls);cost=sum(l["q"]*l["px"] for l in ls);pay=sum(l["q"] for l in ls if l["hit"])
        print("%-5s %-9s n=%3d paid=%.4f swHit=%5.1f%%"%(stg,band,len(ls),cost/sh,100*pay/sh))
# Fisher exact filled vs unfilled shoulder
def fisher(a,b,c,d):
    from math import comb
    n=a+b+c+d; tot=0.0
    def p(a):
        b2=a+b-a if False else None
        return None
    # hypergeometric tail
    r1=a+b; r2=c+d; c1=a+c; c2=b+d
    denom=comb(n,c1)
    s=0.0
    for x in range(max(0,c1-r2), min(r1,c1)+1):
        pr=comb(r1,x)*comb(r2,c1-x)/denom
        if pr<=comb(r1,a)*comb(r2,c1-a)/denom + 1e-12: s+=pr
    return s
print()
print("Fisher exact, shoulder filled(3/139) vs unfilled(24/84): p=%.3e"%fisher(3,136,24,60))
