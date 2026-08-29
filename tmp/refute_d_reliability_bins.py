"""Independent reliability table: bought legs vs all buckets at the same instants."""
import sqlite3, json
from collections import defaultdict
c=sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro",uri=True,timeout=60)
setl={(st,ed):sb for st,ed,sb in c.execute("SELECT station_id,event_date,settled_bucket FROM paper_trade_settlement")}
rows=c.execute("""SELECT station_id,event_date,probabilities_json,leg_shares_json,posted_buckets_json,
 ladder_center_bucket FROM paper_trade_position""").fetchall()
BINS=[0.0,0.02,0.05,0.10,0.20,0.35,1.01]
def bi(p):
    for i in range(len(BINS)-1):
        if BINS[i]<=p<BINS[i+1]: return i
    return len(BINS)-2
t=defaultdict(lambda: dict(n=0,hit=0,claim=0.0))
for (st,ed,probj,legj,postj,lcb) in rows:
    if (st,ed) not in setl: continue
    settled=setl[(st,ed)]
    probs=json.loads(probj or "{}") or {}
    legs={b for b,q in (json.loads(legj or "{}") or {}).items() if float(q)>0}
    posted=set(json.loads(postj or "[]") or [])
    for b,p in probs.items():
        gs=["ALL", "BOUGHT" if b in legs else "not-bought"]
        if b in posted: gs.append("POSTED")
        for g in gs:
            k=(g,bi(float(p)))
            t[k]["n"]+=1; t[k]["hit"]+=(b==settled); t[k]["claim"]+=float(p)
print("%-11s %-14s %6s %8s %9s %7s"%("subset","claim bin","n","claim","realised","re/cl"))
for g in ("ALL","BOUGHT","not-bought","POSTED"):
    tn=th=0; tc=0.0
    for i in range(len(BINS)-1):
        v=t.get((g,i))
        if not v: continue
        tn+=v["n"]; th+=v["hit"]; tc+=v["claim"]
        print("%-11s [%.2f,%.2f) %6d %8.4f %9.4f %7.2f"%(g,BINS[i],BINS[i+1],v["n"],
              v["claim"]/v["n"],v["hit"]/v["n"],(v["hit"]/v["n"])/(v["claim"]/v["n"]) if v["claim"] else 0))
    print("%-11s %-14s %6d %8.4f %9.4f %7.2f\n"%(g,"ALL BINS",tn,tc/tn,th/tn,(th/tn)/(tc/tn)))
