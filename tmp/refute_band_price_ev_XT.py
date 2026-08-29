import json, sqlite3, statistics, sys, re, math
sys.path.insert(0, "/Users/xiong/weather")
from scripts.paper_trading_ledger import _is_shoulder, FEE_RATE, MAKER_RESTING_SHOULDER_LAMBDA, MAKER_RESTING_TAIL_LAMBDA

DB="file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro"
con=sqlite3.connect(DB,uri=True,timeout=60)
print("FEE_RATE",FEE_RATE,"SHOULDER_LAMBDA",repr(MAKER_RESTING_SHOULDER_LAMBDA),"TAIL_LAMBDA",repr(MAKER_RESTING_TAIL_LAMBDA))

# settlement: check for multiple rows per station-day with differing settled_bucket
rows=con.execute("select station_id,event_date,stage,settled_bucket,settled_tmax_c,hit,stake_usdc,payout_usdc,pnl_usdc from paper_trade_settlement").fetchall()
sd={}
conflict=0
for st,ed,stg,sb,tc,h,sk,pay,pnl in rows:
    k=(st,ed)
    if k in sd and sd[k][0]!=sb: conflict+=1
    sd[k]=(sb,tc)
print("settlement rows",len(rows),"distinct station-days",len(sd),"bucket conflicts",conflict)

pos=con.execute(
 "select station_id,event_date,stage,ladder_center_bucket,posted_buckets_json,window_buckets_json,"
 "leg_shares_json,chain_shares_json,resting_fill_json,probabilities_json,prices_json,stake_usdc,shares,"
 "decision_at_utc,intended_legs_json from paper_trade_position").fetchall()
print("position rows",len(pos))

legs=[]; skipped_unsettled=0; recon_bad=[]
for (st,ed,stg,centre,pj,wj,lj,cj,fj,prj,pcj,stake,shares,dec,ilj) in pos:
    if (st,ed) not in sd: skipped_unsettled+=1; continue
    sb=sd[(st,ed)][0]
    posted=json.loads(pj or "[]")
    win=json.loads(wj or "[]")
    legsh=json.loads(lj or "{}")
    fills=json.loads(fj or "{}")
    probs=json.loads(prj or "{}")
    asks=json.loads(pcj or "{}")
    tot=0.0
    for b,q in legsh.items():
        q=float(q)
        if q<=0: continue
        f=fills.get(b)
        m=re.match(r"^(\d+)/(\d+)@([0-9.]+)$", f) if f else None
        px = float(m.group(3)) if m else None
        got = int(m.group(1)) if m else None
        rungs = int(m.group(2)) if m else None
        assert px is not None and px>0, (st,ed,stg,b,f)
        paid=px*(1.0+FEE_RATE)
        tot+=q*paid
        if b==centre: band="centre"
        elif _is_shoulder(b,centre,posted): band="shoulder"
        else: band="rest"
        legs.append(dict(st=st,ed=ed,stage=stg,bucket=b,band=band,q=q,px=paid,
                         hit=1 if b==sb else 0,p=float(probs.get(b) or 0.0),
                         ask=asks.get(b),got=got,rungs=rungs,
                         centre_on_posted=centre in posted, posted_n=len(posted),
                         centre_at_end=(posted and centre in posted and posted.index(centre) in (0,len(posted)-1))))
    if abs(tot-float(stake))>max(0.01,0.005*abs(float(stake))): recon_bad.append((st,ed,stg,stake,tot))
print("skipped unsettled position rows",skipped_unsettled)
print("legs",len(legs),"stake reconstruction mismatches",len(recon_bad))
for r in recon_bad[:5]: print("  ",r)

def wilson(k,n,z=1.96):
    if n==0: return (0.0,0.0)
    p=k/n; d=1+z*z/n; c=p+z*z/(2*n); m=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))
    return ((c-m)/d,(c+m)/d)
def qt(v,p):
    v=sorted(v); i=min(len(v)-1,max(0,int(round(p*(len(v)-1))))); return v[i]

print()
hdr="%-9s %5s %8s %9s %9s %9s %7s %7s %7s %8s %7s %7s %7s %7s"
print(hdr%("band","legs","shares","cost_u","payout_u","net_u","legHit%","swHit%","paidSW","BEodds","meanPx","medPx","p10","p90"))
for band in ("centre","shoulder","rest","ALL"):
    ls = legs if band=="ALL" else [l for l in legs if l["band"]==band]
    n=len(ls); sh=sum(l["q"] for l in ls); cost=sum(l["q"]*l["px"] for l in ls)
    pay=sum(l["q"] for l in ls if l["hit"]); k=sum(l["hit"] for l in ls)
    swp=cost/sh; swh=pay/sh; px=[l["px"] for l in ls]
    print(hdr.replace("s ","s ")%(band,n,"%.0f"%sh,"%.1f"%cost,"%.1f"%pay,"%+.1f"%(pay-cost),
        "%.1f"%(100*k/n),"%.1f"%(100*swh),"%.4f"%swp,"%.1fx"%(1/swp),
        "%.4f"%statistics.fmean(px),"%.4f"%statistics.median(px),"%.4f"%qt(px,.10),"%.4f"%qt(px,.90)))
print()
for band in ("centre","shoulder","rest"):
    ls=[l for l in legs if l["band"]==band]
    n=len(ls); k=sum(l["hit"] for l in ls); sh=sum(l["q"] for l in ls)
    cost=sum(l["q"]*l["px"] for l in ls); pay=sum(l["q"] for l in ls if l["hit"])
    swp=cost/sh; swh=pay/sh; lo,hi=wilson(k,n)
    print("%-9s n=%3d hits=%2d legHit=%.4f [%.4f,%.4f] swHit=%.4f paid=%.4f  fair-paid=%+.4f  paid/fair=%s  ROI=%+.1f%% net=%+.1fu claim=%.4f"%(
      band,n,k,k/n,lo,hi,swh,swp,swh-swp,("%.2fx"%(swp/swh)) if swh>0 else "inf",
      100*(pay-cost)/cost,pay-cost,sum(l["p"]*l["q"] for l in ls)/sh))
    for bench in (0.139,0.184,0.253):
        pv=sum(math.comb(n,i)*bench**i*(1-bench)**(n-i) for i in range(0,k+1))
        print("      P[X<=%d|n=%d,p=%.3f]=%.3e"%(k,n,bench,pv))

print()
print("--- station-day level hit denominator (a band hits if ANY of its legs on that chain hit) ---")
for band in ("centre","shoulder","rest"):
    days={}
    for l in legs:
        if l["band"]!=band: continue
        days.setdefault((l["st"],l["ed"]),0)
        days[(l["st"],l["ed"])] |= l["hit"]
    n=len(days); k=sum(days.values())
    lo,hi=wilson(k,n)
    print("%-9s station-days=%3d hits=%2d rate=%.4f [%.4f,%.4f]"%(band,n,k,k/n if n else 0,lo,hi))

print()
print("--- paid vs ask at decision (prices_json) ---")
for band in ("centre","shoulder","rest"):
    ls=[l for l in legs if l["band"]==band and l["ask"] and float(l["ask"])>0]
    r=[l["px"]/float(l["ask"]) for l in ls]
    sh=sum(l["q"] for l in ls)
    print("%-9s n=%3d median paid/ask=%.3f  swPaid=%.4f swAsk=%.4f"%(band,len(ls),statistics.median(r),
      sum(l["px"]*l["q"] for l in ls)/sh, sum(float(l["ask"])*l["q"] for l in ls)/sh))

print()
print("--- ladder centre position on posted window ---")
seen={}
for l in legs: seen[(l["st"],l["ed"],l["stage"])]=(l["centre_on_posted"],l["centre_at_end"],l["posted_n"])
n=len(seen); print("chains-stages",n,"centre on posted",sum(1 for v in seen.values() if v[0]),
  "centre at endpoint",sum(1 for v in seen.values() if v[1]), "=%.0f%%"%(100*sum(1 for v in seen.values() if v[1])/n))
