import sqlite3, json
c=sqlite3.connect("file:data/bayes_basket_target_ssot.db?mode=ro",uri=True,timeout=60)
rows=c.execute("SELECT p.position_id,p.leg_shares_json,p.probabilities_json,p.prices_json,"
 "p.ladder_center_bucket,p.posted_buckets_json,p.window_buckets_json,s.settled_bucket"
 " FROM paper_trade_position p JOIN paper_trade_settlement s"
 " ON s.station_id=p.station_id AND s.event_date=p.event_date").fetchall()
side={}
endpoint=0; tot=0; truth_side={"above":0,"below":0,"centre":0,"off":0}
for (pid,legj,probj,pricej,lcb,postj,winj,settled) in rows:
    probs=json.loads(probj or "{}") or {}; prices=json.loads(pricej or "{}") or {}
    legs={b:float(q) for b,q in (json.loads(legj or "{}") or {}).items() if float(q)>0}
    win=json.loads(winj or "[]") or []
    ladder=list(probs.keys())
    if lcb not in ladder: continue
    ci=ladder.index(lcb)
    tot+=1
    if win and (win[0]==lcb or win[-1]==lcb): endpoint+=1
    si=ladder.index(settled) if settled in ladder else None
    if si is not None:
        truth_side["centre" if si==ci else ("above" if si>ci else "below")]+=1
    for j,b in enumerate(ladder):
        if abs(j-ci)!=1: continue
        s="above" if j>ci else "below"
        d=side.setdefault((s,b in legs),dict(n=0,hit=0,px=0.0,npx=0,claim=0.0))
        d["n"]+=1; d["hit"]+=1 if b==settled else 0; d["claim"]+=float(probs[b])
        if prices.get(b) is not None: d["npx"]+=1; d["px"]+=float(prices[b])
print("ladder centre sits on a WINDOW ENDPOINT in %d of %d rows (%.0f%%)"%(endpoint,tot,100*endpoint/tot))
print("truth side vs ladder centre:",truth_side)
print("\n%-7s %-11s %4s %7s %7s %8s"%("side","subset","n","claim","price","realised"))
for s in ("below","above"):
    for bought in (True,False):
        d=side.get((s,bought))
        if not d: continue
        print("%-7s %-11s %4d %7.3f %7.3f %8.3f"%(s,"BOUGHT" if bought else "not-bought",
            d["n"],d["claim"]/d["n"],d["px"]/max(d["npx"],1),d["hit"]/d["n"]))
