"""Re-measure the leg bands on the rebuilt ledger, using the ledger's OWN _is_shoulder."""
import sqlite3, json, sys
sys.path.insert(0, "scripts")
import importlib.util
spec = importlib.util.spec_from_file_location("ptl", "scripts/paper_trading_ledger.py")
ptl = importlib.util.module_from_spec(spec); spec.loader.exec_module(ptl)
_is_shoulder = ptl._is_shoulder
print("MAKER_RESTING_SHOULDER_LAMBDA =", repr(ptl.MAKER_RESTING_SHOULDER_LAMBDA))
print("MAKER_RESTING_TAIL_LAMBDA =", repr(getattr(ptl, "MAKER_RESTING_TAIL_LAMBDA", "<absent>")))

c = sqlite3.connect("file:data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60)
rows = c.execute(
    "SELECT p.position_id,p.station_id,p.event_date,p.stage,p.leg_shares_json,"
    "       p.probabilities_json,p.prices_json,p.ladder_center_bucket,"
    "       p.window_buckets_json,p.posted_buckets_json,s.settled_bucket"
    " FROM paper_trade_position p JOIN paper_trade_settlement s"
    "  ON s.station_id=p.station_id AND s.event_date=p.event_date").fetchall()

bands = {}
for (pid,st,ed,stage,legj,probj,pricej,lcb,winj,postj,settled) in rows:
    probs=json.loads(probj or "{}") or {}
    prices=json.loads(pricej or "{}") or {}
    posted=json.loads(postj or "[]") or []
    legs={b:float(q) for b,q in (json.loads(legj or "{}") or {}).items() if float(q)>0}
    for b,q in legs.items():
        if b==lcb: band="centre"
        elif _is_shoulder(b,lcb,posted): band="shoulder"
        else: band="rest"
        d=bands.setdefault(band,dict(n=0,hit=0,q=0.0,cost=0.0,pay=0.0,claim=0.0,price=0.0,npx=0))
        y=1 if b==settled else 0
        d["n"]+=1; d["hit"]+=y; d["q"]+=q; d["claim"]+=float(probs.get(b,0.0))
        px=prices.get(b)
        if px is not None:
            d["npx"]+=1; d["price"]+=float(px)
            d["cost"]+=float(px)*q; d["pay"]+=y*q
print("\n%-9s %5s %6s %8s %8s %10s %10s %10s"%("band","legs","hit","claim","price","shares","cost u","net u"))
for band in ("centre","shoulder","rest"):
    d=bands.get(band)
    if not d: continue
    print("%-9s %5d %6.3f %8.3f %8.3f %10.1f %10.1f %+10.1f"%(
        band,d["n"],d["hit"]/d["n"],d["claim"]/d["n"],d["price"]/max(d["npx"],1),
        d["q"],d["cost"],d["pay"]-d["cost"]))

# where does the bought bucket sit relative to the ladder centre and to the model centre?
env=c.execute("SELECT 1").fetchone()
