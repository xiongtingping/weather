"""Independent recomputation of D's central number. No reuse of their scripts."""
import sqlite3, json, importlib.util
from collections import defaultdict

spec = importlib.util.spec_from_file_location("ptl", "/Users/xiong/weather/scripts/paper_trading_ledger.py")
ptl = importlib.util.module_from_spec(spec); spec.loader.exec_module(ptl)
print("MAKER_RESTING_SHOULDER_LAMBDA=%r  MAKER_RESTING_TAIL_LAMBDA=%r"
      % (ptl.MAKER_RESTING_SHOULDER_LAMBDA, ptl.MAKER_RESTING_TAIL_LAMBDA))

c = sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60)
rows = c.execute("""
 SELECT p.position_id,p.station_id,p.event_date,p.stage,p.decision_at_utc,
        p.ladder_center_bucket,p.probabilities_json,p.prices_json,
        p.posted_buckets_json,p.window_buckets_json,
        p.leg_shares_json,p.chain_shares_json
   FROM paper_trade_position p""").fetchall()
setl = {}
for st,ed,stage,sb,hit,tmax in c.execute(
   "SELECT station_id,event_date,stage,settled_bucket,hit,settled_tmax_c FROM paper_trade_settlement"):
    setl.setdefault((st,ed),set()).add(sb)
print("positions=%d  settlement station-days=%d  multi-bucket days=%d"
      % (len(rows), len(setl), sum(1 for v in setl.values() if len(v)>1)))

# ---- cells
cells = defaultdict(lambda: dict(n=0,hit=0,claim=0.0,price=0.0,npx=0))
sh_cells = defaultdict(lambda: dict(n=0,hit=0,claim=0.0))
joined=0; skipped_no_settle=0; skipped_no_centre=0
day_leg = defaultdict(dict)   # (st,ed) -> bucket -> bought(bool)  for dedupe variant
day_meta = {}
for (pid,st,ed,stage,dt,lcb,probj,pricej,postj,winj,legj,chainj) in rows:
    key=(st,ed)
    if key not in setl: skipped_no_settle+=1; continue
    settled = next(iter(setl[key]))
    probs = json.loads(probj or "{}") or {}
    prices = json.loads(pricej or "{}") or {}
    posted = json.loads(postj or "[]") or []
    legs = {b:float(q) for b,q in (json.loads(legj or "{}") or {}).items() if float(q)>0}
    ladder = list(probs.keys())
    if lcb not in ladder: skipped_no_centre+=1; continue
    joined+=1
    ci = ladder.index(lcb)
    for j,b in enumerate(ladder):
        d = abs(j-ci)
        bought = b in legs
        cell = cells[(min(d,3),bought)]
        cell["n"]+=1; cell["hit"]+= (b==settled); cell["claim"]+=float(probs[b])
        if prices.get(b) is not None:
            cell["npx"]+=1; cell["price"]+=float(prices[b])
        # production predicate, POSTED window
        if ptl._is_shoulder(b, lcb, posted):
            sc = sh_cells[bought]
            sc["n"]+=1; sc["hit"]+=(b==settled); sc["claim"]+=float(probs[b])
        prev = day_leg[key].get(b, (False,None))
        day_leg[key][b] = (prev[0] or bought, d)
    day_meta[key]=settled
print("joined position rows=%d  skipped(no settlement)=%d skipped(centre off ladder)=%d"
      % (joined, skipped_no_settle, skipped_no_centre))

def show(title, table, keys):
    print("\n"+title)
    print("%-6s %-11s %6s %8s %9s %8s"%("dist","subset","n","claim","realised","re/cl"))
    for k in keys:
        cell=table.get(k)
        if not cell: continue
        cl=cell["claim"]/cell["n"]; re=cell["hit"]/cell["n"]
        print("%-6s %-11s %6d %8.4f %9.4f %8s"%(
            k[0] if not isinstance(k,tuple) else (">=3" if k[0]==3 else k[0]),
            ("BOUGHT" if (k[1] if isinstance(k,tuple) else k) else "not-bought"),
            cell["n"],cl,re,"%.2f"%(re/cl) if cl else "-"))

show("A) ladder-distance (probabilities_json key order), leg-level:",
     cells, [(d,b) for d in (0,1,2,3) for b in (True,False)])

print("\nB) production _is_shoulder on POSTED window, leg-level:")
for b in (True,False):
    sc=sh_cells.get(b)
    if sc: print("  %-11s n=%4d claim=%.4f realised=%.4f (%d/%d)"
                 %("BOUGHT" if b else "not-bought",sc["n"],sc["claim"]/sc["n"],sc["hit"]/sc["n"],sc["hit"],sc["n"]))

print("\nC) pooled by ladder distance (reproduce measurement (1)):")
for d in (0,1,2,3):
    n=sum(cells[k]["n"] for k in cells if k[0]==d)
    h=sum(cells[k]["hit"] for k in cells if k[0]==d)
    cl=sum(cells[k]["claim"] for k in cells if k[0]==d)
    print("  dist %-3s n=%4d claim=%.4f realised=%.4f"%((">=3" if d==3 else d),n,cl/n,h/n))

# ---- station-day deduped variant (each bucket counted once per station-day)
dd=defaultdict(lambda: dict(n=0,hit=0))
for key,bmap in day_leg.items():
    settled=day_meta[key]
    for b,(bought,d) in bmap.items():
        cell=dd[(min(d,3),bought)]; cell["n"]+=1; cell["hit"]+=(b==settled)
print("\nD) station-day deduped (each bucket once per station-day, bought=ever bought):")
for d in (0,1,2,3):
    for bt in (True,False):
        cell=dd.get((d,bt))
        if not cell: continue
        print("  dist %-3s %-11s n=%4d realised=%.4f (%d/%d)"%(
            (">=3" if d==3 else d),"BOUGHT" if bt else "not-bought",cell["n"],cell["hit"]/cell["n"],cell["hit"],cell["n"]))
