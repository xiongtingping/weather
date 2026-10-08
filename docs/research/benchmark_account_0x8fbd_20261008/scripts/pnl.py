import sys, json, collections, datetime as dt
sys.path.insert(0,'.')
from lib import *
fills,acts=load()
labels={}
for r in json.load(open("labels.json")):
    k=(r["station_id"],r["event_date"])
    if k not in labels or r["truth_revision_no"]>labels[k]["truth_revision_no"]: labels[k]=r
cur={p["asset"]:p for p in json.load(open("positions_current.json"))}
# per token ledger
led=collections.defaultdict(lambda: dict(buy_sh=0.0,buy_cost=0.0,sell_sh=0.0,sell_proc=0.0,merge_sh=0.0,redeem_sh=0.0,redeem_usdc=0.0,mk_buy_sh=0.0,tk_buy_sh=0.0,meta=None))
for f in fills:
    L=led[f["asset"]]; L["meta"]=f
    if f["side"]=="BUY":
        L["buy_sh"]+=f["size"]; L["buy_cost"]+=f["size"]*f["price"]
        if f["taker"]: L["tk_buy_sh"]+=f["size"]
        else: L["mk_buy_sh"]+=f["size"]
    else:
        L["sell_sh"]+=f["size"]; L["sell_proc"]+=f["size"]*f["price"]
# merges: activity MERGE rows have conditionId, size (pairs). each pair burns 1 YES + 1 NO, returns 1 USDC
merge_by_cid=collections.defaultdict(float); redeem=collections.defaultdict(float)
for a in acts:
    if a["type"]=="MERGE": merge_by_cid[a["cid"]]+=a["size"]
    if a["type"]=="REDEEM": redeem[(a["cid"],a["outcome"])]+=a["usdc"]
# market-level
mk=collections.defaultdict(lambda: dict(yes=None,no=None))
for asset,L in led.items():
    m=L["meta"]; mk[m["cid"]][m["outcome"].lower()]=(asset,L)
rows=[]
unres=set(); unres_val=0.0
for cid,d in mk.items():
    meta=(d["yes"] or d["no"])[1]["meta"]
    st,ed,bucket=meta["station"],meta["event_date"],meta["bucket"]
    lab=labels.get((st,ed))
    merged=merge_by_cid.get(cid,0.0)
    out=dict(cid=cid,station=st or meta["city"],event_date=ed,bucket=bucket,lo=meta["lo"],hi=meta["hi"],unit=meta["unit"],kind=meta["kind"],settled=lab["settled_bucket"] if lab else None,settled_tmax=lab["settled_tmax_c"] if lab else None,merged=merged)
    cash=merged  # merge returns 1 USDC per pair
    for side in ("yes","no"):
        if d[side] is None:
            out[f"{side}_buy_sh"]=0; out[f"{side}_cost"]=0; out[f"{side}_sell"]=0; out[f"{side}_hold"]=0; out[f"{side}_mk"]=0; continue
        asset,L=d[side]
        hold=L["buy_sh"]-L["sell_sh"]-merged
        out[f"{side}_buy_sh"]=L["buy_sh"]; out[f"{side}_cost"]=L["buy_cost"]; out[f"{side}_sell"]=L["sell_proc"]; out[f"{side}_hold"]=hold; out[f"{side}_mk"]=L["mk_buy_sh"]
        cash+=L["sell_proc"]-L["buy_cost"]
        # settle remaining holdings
        if hold>1e-6:
            if lab:
                win = (lab["settled_bucket"]==bucket)
                if meta["unit"]=="F":  # label is in our registry's label format; compare by tmax
                    t=lab["settled_tmax_c"]; tf=t*9/5+32
                    lo,hi=meta["lo"],meta["hi"]
                    win = (lo is None or tf>=lo-0.5) and (hi is None or tf<hi+0.5) if (lo is not None or hi is not None) else win
                else:
                    t=lab["settled_tmax_c"]; lo,hi=meta["lo"],meta["hi"]
                    win = (lo is None or t>=lo-0.5) and (hi is None or t<hi+0.5)
                val = hold*(1.0 if (win if side=="yes" else not win) else 0.0)
                cash+=val
                out[f"{side}_settle_val"]=val
            else:
                p=cur.get(asset)
                v=hold*(p["curPrice"] if p else 0.5)
                unres.add((st,ed)); unres_val+=v; cash+=v; out[f"{side}_mark"]=v
    out["pnl"]=cash; out["resolved"]=lab is not None
    rows.append(out)
json.dump(rows,open("market_pnl.json","w"),default=str)
res=[r for r in rows if r["resolved"]]; unr=[r for r in rows if not r["resolved"]]
print(f"markets {len(rows)} resolved {len(res)} unresolved {len(unr)} (marked at curPrice, value {unres_val:.0f})")
print(f"PnL resolved markets: {sum(r['pnl'] for r in res):.1f} USDC | unresolved mark: {sum(r['pnl'] for r in unr):.1f}")
tot_cost=sum(r["yes_cost"]+r["no_cost"] for r in res)
print(f"resolved: total buy cost {tot_cost:.0f}, return {sum(r['pnl'] for r in res)/tot_cost*100:.2f}% of cost")
# by event_date
bd=collections.defaultdict(lambda:[0,0,0])
for r in res: bd[r["event_date"]][0]+=r["pnl"]; bd[r["event_date"]][1]+=r["yes_cost"]+r["no_cost"]; bd[r["event_date"]][2]+=1
print("PnL by event_date (pnl, cost, n):"); 
for d in sorted(bd): print(f"  {d}: {bd[d][0]:8.1f}  {bd[d][1]:8.0f}  {bd[d][2]}")
# by side-structure: YES-only legs vs NO-only legs
yes_pnl=0; no_pnl=0; yes_cost=0; no_cost=0
for r in res:
    # attribute: compute pnl of yes leg alone and no leg alone ignoring merges? merges couple them; approximate: merged pairs valued at 1 split by cost proportion
    pass
# station-day level
sd=collections.defaultdict(lambda:[0,0])
for r in res: sd[(r["station"],r["event_date"])][0]+=r["pnl"]; sd[(r["station"],r["event_date"])][1]+=r["yes_cost"]+r["no_cost"]
v=sorted(x[0] for x in sd.values())
print(f"station-days resolved {len(sd)} | win-days {sum(1 for x in v if x>0)} | pnl quantiles {[round(v[int(q*(len(v)-1))],1) for q in (.05,.25,.5,.75,.95)]} | worst {v[:5]} best {v[-5:]}")
bs=collections.defaultdict(lambda:[0,0,0])
for (s,d),(p,c) in sd.items(): bs[s][0]+=p; bs[s][1]+=c; bs[s][2]+=1
print("by station (pnl, cost, days) sorted:")
for s,(p,c,n) in sorted(bs.items(),key=lambda x:-x[1][0]): print(f"  {s}: {p:8.1f} {c:7.0f} {n}")
# non-trade income
inc={t:sum(a['usdc'] for a in acts if a['type']==t) for t in ("REWARD","MAKER_REBATE","TAKER_REBATE")}
print("income:",inc)
