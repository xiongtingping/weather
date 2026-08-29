import json,re,sqlite3,statistics,sys
sys.path.insert(0,"/Users/xiong/weather")
from scripts.paper_trading_ledger import _is_shoulder
con=sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro",uri=True,timeout=30)
settled={(r[0],r[1]):r[2] for r in con.execute("SELECT station_id,event_date,settled_bucket FROM paper_trade_settlement")}
rows=con.execute("SELECT station_id,event_date,stage,ladder_center_bucket,posted_buckets_json,window_buckets_json,"
 "leg_shares_json,resting_fill_json,probabilities_json,prices_json FROM paper_trade_position").fetchall()
legs=[];endpoint=0;tot=0
for st,ed,stage,centre,pj,wj,lj,fj,probj,pricej in rows:
    if (st,ed) not in settled: continue
    sb=settled[(st,ed)]
    win=json.loads(pj or "[]") or json.loads(wj or "[]")
    if centre in win:
        tot+=1
        if win.index(centre) in (0,len(win)-1): endpoint+=1
    fills=json.loads(fj or "{}");probs=json.loads(probj or "{}");asks=json.loads(pricej or "{}")
    for b,q in json.loads(lj or "{}").items():
        q=float(q)
        if q<=0: continue
        m=re.match(r"(\d+)/(\d+)@([0-9.]+)",fills.get(b,""))
        px=float(m.group(3)) if m else float(asks.get(b) or 0)
        d=(win.index(b)-win.index(centre)) if (b in win and centre in win) else None
        legs.append(dict(b=b,d=d,q=q,px=px,hit=1 if b==sb else 0,p=float(probs.get(b) or 0),ask=float(asks.get(b) or 0)))
print("posted windows where ladder centre sits on an endpoint: %d/%d = %.0f%%"%(endpoint,tot,100*endpoint/tot))
print()
print("%-6s %5s %8s %8s %8s %7s %7s %8s %9s"%("signedD","legs","shares","paidPx","hit%","claim%","askPx","net_u","ROI%"))
ds=sorted({l["d"] for l in legs},key=lambda x:(x is None,x))
for d in ds:
    ls=[l for l in legs if l["d"]==d]
    sh=sum(l["q"] for l in ls);c=sum(l["q"]*l["px"] for l in ls);pay=sum(l["q"] for l in ls if l["hit"])
    print("%-6s %5d %8.0f %8.4f %8.1f %7.1f %7.4f %8.1f %9.1f"%(
      str(d),len(ls),sh,c/sh,100*sum(l["hit"] for l in ls)/len(ls),
      100*sum(l["p"]*l["q"] for l in ls)/sh,sum(l["ask"]*l["q"] for l in ls)/sh,pay-c,100*(pay-c)/c if c else 0))
