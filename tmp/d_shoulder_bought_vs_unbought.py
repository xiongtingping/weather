"""The 8x reconciliation: bought shoulder legs vs ALL rungs at the same ladder distance."""
import sqlite3, json, importlib.util, math
spec = importlib.util.spec_from_file_location("ptl", "scripts/paper_trading_ledger.py")
ptl = importlib.util.module_from_spec(spec); spec.loader.exec_module(ptl)

c = sqlite3.connect("file:data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60)
rows = c.execute(
    "SELECT p.position_id,p.leg_shares_json,p.probabilities_json,p.prices_json,"
    "       p.ladder_center_bucket,p.posted_buckets_json,s.settled_bucket"
    " FROM paper_trade_position p JOIN paper_trade_settlement s"
    "  ON s.station_id=p.station_id AND s.event_date=p.event_date").fetchall()

agree=disagree=0
cells={}
for (pid,legj,probj,pricej,lcb,postj,settled) in rows:
    probs=json.loads(probj or "{}") or {}
    prices=json.loads(pricej or "{}") or {}
    posted=json.loads(postj or "[]") or []
    legs={b:float(q) for b,q in (json.loads(legj or "{}") or {}).items() if float(q)>0}
    ladder=list(probs.keys())
    if lcb not in ladder: continue
    ci=ladder.index(lcb)
    for j,b in enumerate(ladder):
        d=abs(j-ci)
        bought = b in legs
        if bought:  # cross-check ladder distance against the production predicate
            sh = ptl._is_shoulder(b,lcb,posted)
            if (d==1) == sh: agree+=1
            else: disagree+=1
        key=(min(d,3), bought)
        cell=cells.setdefault(key,dict(n=0,hit=0,claim=0.0,price=0.0,npx=0))
        cell["n"]+=1; cell["hit"]+= 1 if b==settled else 0
        cell["claim"]+=float(probs[b])
        if prices.get(b) is not None:
            cell["npx"]+=1; cell["price"]+=float(prices[b])
print("ladder-distance==1 vs production _is_shoulder on bought legs: agree=%d disagree=%d"%(agree,disagree))
print("\n%-6s %-9s %5s %8s %8s %8s"%("dist","subset","n","claim","realised","real/claim"))
for d in (0,1,2,3):
    for bought in (True,False):
        cell=cells.get((d,bought))
        if not cell: continue
        cl=cell["claim"]/cell["n"]; re=cell["hit"]/cell["n"]
        print("%-6s %-9s %5d %8.3f %8.3f %8s"%(
            (">=3" if d==3 else str(d)),"BOUGHT" if bought else "not-bought",
            cell["n"],cl,re,"%.2f"%(re/cl) if cl else "-"))
# pooled over both subsets
print("\npooled (bought or not), by ladder distance:")
for d in (0,1,2,3):
    n=sum(cells[k]["n"] for k in cells if k[0]==d)
    h=sum(cells[k]["hit"] for k in cells if k[0]==d)
    cl=sum(cells[k]["claim"] for k in cells if k[0]==d)
    print("  dist %-3s n=%4d claim=%.3f realised=%.3f"%((">=3" if d==3 else str(d)),n,cl/n,h/n))
