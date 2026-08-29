import json, sqlite3, statistics, sys, re
sys.path.insert(0, "/Users/xiong/weather")
from scripts.paper_trading_ledger import _is_shoulder, FEE_RATE

con = sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=30)
settled = {(r[0], r[1]): (r[2], r[3]) for r in con.execute(
    "SELECT station_id,event_date,settled_bucket,settled_tmax_c FROM paper_trade_settlement")}
rows = con.execute(
    "SELECT station_id,event_date,stage,ladder_center_bucket,posted_buckets_json,"
    " window_buckets_json,leg_shares_json,resting_fill_json,probabilities_json,"
    " prices_json,stake_usdc,decision_at_utc"
    " FROM paper_trade_position ORDER BY station_id,event_date,decision_at_utc").fetchall()
print("positions", len(rows), "settled chains", len(settled), "FEE_RATE", FEE_RATE)

legs = []
stake_check = []
for (st, ed, stage, centre, postedj, winj, legj, fillj, probj, pricej, stake, dec) in rows:
    key = (st, ed)
    if key not in settled:
        continue
    settled_bucket = settled[key][0]
    posted = json.loads(postedj or "[]") or json.loads(winj or "[]")
    legshares = json.loads(legj or "{}")
    fills = json.loads(fillj or "{}")
    probs = json.loads(probj or "{}")
    prices = json.loads(pricej or "{}")
    tot = 0.0
    for bucket, q in legshares.items():
        q = float(q)
        if q <= 0:
            continue
        f = fills.get(bucket)
        price = None
        if f:
            m = re.match(r"(\d+)/(\d+)@([0-9.]+)", f)
            if m:
                price = float(m.group(3)) * (1.0 + FEE_RATE)
        if price is None or price <= 0:
            price = float(prices.get(bucket) or 0.0)
            src = "topbook"
        else:
            src = "fill"
        tot += q * price
        if bucket == centre:
            band = "centre"
        elif _is_shoulder(bucket, centre, posted):
            band = "shoulder"
        else:
            band = "rest"
        legs.append(dict(st=st, ed=ed, stage=stage, bucket=bucket, band=band,
                         q=q, price=price, src=src,
                         hit=1 if bucket == settled_bucket else 0,
                         p=float(probs.get(bucket) or 0.0),
                         centre_on_posted=centre in posted))
    stake_check.append((st, ed, stage, stake, tot))

bad = [x for x in stake_check if abs(x[3]-x[4]) > max(0.02, 0.02*abs(x[3]))]
print("stake reconstruction mismatches:", len(bad), "/", len(stake_check))
for b in bad[:5]: print("  ", b)
print("price source counts:", {s: sum(1 for l in legs if l["src"]==s) for s in {l["src"] for l in legs}})
print("centre off posted window legs:", sum(1 for l in legs if not l["centre_on_posted"]))

def q(vals, p):
    vals = sorted(vals)
    if not vals: return float("nan")
    i = min(len(vals)-1, max(0, int(round(p*(len(vals)-1)))))
    return vals[i]

print()
def q(vals,pp):
    vals=sorted(vals)
    i=min(len(vals)-1,max(0,int(round(pp*(len(vals)-1)))))
    return vals[i]
print("%-9s %5s %8s %9s %9s %9s %6s %7s %7s %7s %7s %7s %7s %7s" % (
 "band","legs","shares","cost_u","payout_u","net_u","hit%","BE_px","1/px","meanPx","medPx","p10","p90","claim%"))
for band in ("centre","shoulder","rest","ALL"):
    ls = legs if band=="ALL" else [l for l in legs if l["band"]==band]
    if not ls: continue
    n=len(ls); shares=sum(l["q"] for l in ls)
    cost=sum(l["q"]*l["price"] for l in ls); payout=sum(l["q"] for l in ls if l["hit"])
    hit=sum(l["hit"] for l in ls)/n*100
    shw=cost/shares
    pxs=[l["price"] for l in ls]
    claim=sum(l["p"]*l["q"] for l in ls)/shares*100
    print("%-9s %5d %8.0f %9.1f %9.1f %9.1f %6.1f %7.4f %7.1f %7.4f %7.4f %7.4f %7.4f %7.1f" % (
      band,n,shares,cost,payout,payout-cost,hit,shw,1/shw,statistics.fmean(pxs),
      statistics.median(pxs),q(pxs,0.10),q(pxs,0.90),claim))
print()
print("--- unweighted (per-leg) hit rate vs per-leg price, and EV per leg ---")
for band in ("centre","shoulder","rest"):
    ls=[l for l in legs if l["band"]==band]
    ev=[l["hit"]-l["price"] for l in ls]
    print("%-9s n=%3d  meanPx=%.4f  hit=%.3f  mean(hit-px)=%+.4f  ROI_shareweighted=%+.1f%%" % (
      band,len(ls),statistics.fmean([l["price"] for l in ls]),
      statistics.fmean([l["hit"] for l in ls]),statistics.fmean(ev),
      100*(sum(l["q"] for l in ls if l["hit"])-sum(l["q"]*l["price"] for l in ls))/sum(l["q"]*l["price"] for l in ls)))
print()
print("--- price deciles within each band ---")
for band in ("centre","shoulder","rest"):
    ls=sorted([l for l in legs if l["band"]==band],key=lambda l:l["price"])
    for lo,hi,lab in ((0.0,0.02,"<0.02"),(0.02,0.05,"0.02-0.05"),(0.05,0.10,"0.05-0.10"),(0.10,0.20,"0.10-0.20"),(0.20,1.01,">=0.20")):
        sel=[l for l in ls if lo<=l["price"]<hi]
        if not sel: continue
        c=sum(l["q"]*l["price"] for l in sel); pay=sum(l["q"] for l in sel if l["hit"])
        print("%-9s %-10s n=%3d shwPx=%.4f hit=%5.1f%% net=%+9.1fu ROI=%+7.1f%%"%(
          band,lab,len(sel),c/sum(l["q"] for l in sel),100*sum(l["hit"] for l in sel)/len(sel),pay-c,100*(pay-c)/c if c else 0))
print()
print("--- by stage x band ---")
for stage in ("P0","P0-M","P1"):
    for band in ("centre","shoulder","rest"):
        ls=[l for l in legs if l["band"]==band and l["stage"]==stage]
        if not ls: continue
        c=sum(l["q"]*l["price"] for l in ls); pay=sum(l["q"] for l in ls if l["hit"])
        print("%-5s %-9s n=%3d shwPx=%.4f hit=%5.1f%% claim=%5.1f%% net=%+9.1fu"%(
          stage,band,len(ls),c/sum(l["q"] for l in ls),100*sum(l["hit"] for l in ls)/len(ls),
          100*sum(l["p"]*l["q"] for l in ls)/sum(l["q"] for l in ls),pay-c))

print()
print("--- fair value test: price paid vs realised hit (Wilson 95% CI) ---")
import math
def wilson(k,n,z=1.96):
    if n==0: return (0,0)
    p=k/n; d=1+z*z/n; c=p+z*z/(2*n); m=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))
    return ((c-m)/d,(c+m)/d)
for band in ("centre","shoulder","rest"):
    ls=[l for l in legs if l["band"]==band]
    n=len(ls); k=sum(l["hit"] for l in ls)
    shares=sum(l["q"] for l in ls); cost=sum(l["q"]*l["price"] for l in ls)
    shw=cost/shares; swhit=sum(l["q"] for l in ls if l["hit"])/shares
    lo,hi=wilson(k,n)
    print("%-9s n=%3d hits=%2d  leg_hit=%.3f [%.3f,%.3f]  share_wtd_hit=%.3f  paid=%.4f"
          "  fair-minus-paid=%+.4f  paid/fair=%s  claim=%.3f"%(
      band,n,k,k/n,lo,hi,swhit,shw,swhit-shw,
      ("%.2fx"%(shw/swhit)) if swhit>0 else "inf",
      sum(l["p"]*l["q"] for l in ls)/shares))
    # binomial p-value against the bucket-level 14-18% benchmark
    for bench in (0.139,0.184):
        pv=sum(math.comb(n,i)*bench**i*(1-bench)**(n-i) for i in range(0,k+1))
        print("      P[X<=%d | n=%d, p=%.3f] = %.2e"%(k,n,bench,pv))

print()
print("--- price paid vs the book's own ask at decision (adverse selection check) ---")
con2=sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro",uri=True,timeout=30)
askmap={}
for (st,ed,stage,pricej) in con2.execute(
   "SELECT station_id,event_date,stage,prices_json FROM paper_trade_position"):
    askmap[(st,ed,stage)]=json.loads(pricej or "{}")
for band in ("centre","shoulder","rest"):
    ls=[l for l in legs if l["band"]==band]
    rr=[]
    for l in ls:
        a=askmap.get((l["st"],l["ed"],l["stage"]),{}).get(l["bucket"])
        if a and a>0: rr.append((l["price"]/a,l["price"],a,l["q"]))
    if not rr: continue
    print("%-9s n=%3d  median paid/ask=%.2f  share-wtd paid=%.4f vs share-wtd ask=%.4f"%(
      band,len(rr),statistics.median([r[0] for r in rr]),
      sum(r[1]*r[3] for r in rr)/sum(r[3] for r in rr),
      sum(r[2]*r[3] for r in rr)/sum(r[3] for r in rr)))
