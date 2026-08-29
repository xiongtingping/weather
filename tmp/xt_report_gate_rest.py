import sqlite3,json,sys,re
sys.path.insert(0,"/Users/xiong/weather")
from scripts import paper_trading_ledger as ptl
c=sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro",uri=True,timeout=60); c.row_factory=sqlite3.Row
rows=c.execute("""select p.*, s.settled_bucket from paper_trade_position p
 join paper_trade_settlement s on s.station_id=p.station_id and s.event_date=p.event_date""").fetchall()
FILL=re.compile(r"^(\d+)/(\d+)@([0-9.]+)$"); J=lambda x: json.loads(x) if x else {}
rec=[]
for r in rows:
    centre=r["ladder_center_bucket"]; posted=J(r["posted_buckets_json"]) or None
    pr=J(r["probabilities_json"]); px=J(r["prices_json"]); legs=J(r["leg_shares_json"]); fills=J(r["resting_fill_json"])
    for b,sh in legs.items():
        sh=float(sh or 0)
        if sh<=0: continue
        m=FILL.match(str(fills.get(b,"")))
        if not m: continue
        fpx=float(m.group(3))
        if ptl._is_shoulder(b,centre,posted) or str(b)==str(centre): continue
        claim=float(pr.get(b,0) or 0); ask=float(px.get(b,0) or 0)
        rec.append((0.53*claim/ask if ask>0 else 99, sh, fpx, 1 if str(b)==str(r["settled_bucket"]) else 0))
print("filled REST legs n=",len(rec))
for lo,hi,name in [(0,1.0,"ask > cap  (market dearer than our valuation)"),(1.0,99,"ask <= cap (cheap by our own book)")]:
    g=[x for x in rec if lo<=x[0]<hi]
    n=len(g); sh=sum(x[1] for x in g); cost=sum(x[1]*x[2] for x in g); pay=sum(x[1] for x in g if x[3]); hit=sum(x[3] for x in g)
    print(f"  {name:46s} n={n:3d} shares={sh:7.0f} wpx={cost/sh:.4f} legHit={100*hit/n:5.1f}% net={pay-cost:+8.1f}u roi={100*(pay-cost)/cost:+7.1f}%")
