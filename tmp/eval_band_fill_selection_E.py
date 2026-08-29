import json,re,sqlite3,statistics,sys
sys.path.insert(0,"/Users/xiong/weather")
from scripts.paper_trading_ledger import _is_shoulder
con=sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro",uri=True,timeout=30)
settled={(r[0],r[1]):r[2] for r in con.execute("SELECT station_id,event_date,settled_bucket FROM paper_trade_settlement")}
rows=con.execute("SELECT station_id,event_date,stage,ladder_center_bucket,posted_buckets_json,"
 "window_buckets_json,resting_fill_json,probabilities_json,prices_json FROM paper_trade_position").fetchall()
posted=[]
for st,ed,stage,centre,pj,wj,fj,probj,pricej in rows:
    if (st,ed) not in settled: continue
    sb=settled[(st,ed)]
    win=json.loads(pj or "[]") or json.loads(wj or "[]")
    fills=json.loads(fj or "{}"); probs=json.loads(probj or "{}"); asks=json.loads(pricej or "{}")
    for b,f in fills.items():
        m=re.match(r"(\d+)/(\d+)@([0-9.]+)",f)
        if not m: continue
        got,depth,px=int(m.group(1)),int(m.group(2)),float(m.group(3))
        band="centre" if b==centre else ("shoulder" if _is_shoulder(b,centre,win) else "rest")
        posted.append(dict(band=band,filled=got>0,frac=got/depth if depth else 0,px=px,
                           hit=1 if b==sb else 0,p=float(probs.get(b) or 0),ask=float(asks.get(b) or 0)))
print("posted rungs (leg level):",len(posted))
print("%-9s %-9s %5s %6s %7s %7s %8s"%("band","fill","n","hit%","claim%","realised/claim","meanAsk"))
for band in ("centre","shoulder","rest"):
    for lab,sel in (("filled",True),("unfilled",False)):
        ls=[l for l in posted if l["band"]==band and l["filled"]==sel]
        if not ls: continue
        h=sum(l["hit"] for l in ls)/len(ls); c=statistics.fmean([l["p"] for l in ls])
        print("%-9s %-9s %5d %6.1f %7.1f %8.2f %10.4f"%(band,lab,len(ls),100*h,100*c,(h/c if c else 0),
              statistics.fmean([l["ask"] for l in ls])))
