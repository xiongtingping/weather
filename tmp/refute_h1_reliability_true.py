"""Reliability of every bucket claim at the 104 instants, using the TRUE
(complete-prefix) ceiling, plus the claimed-probability bands that produce the
'good unconditional calibration' quoted in the brief."""
import json,sqlite3,statistics,sys
from collections import defaultdict
rows=json.load(open("tmp/refute_h1_true_ceiling_rows.json"))
conn=sqlite3.connect("file:data/bayes_basket_target_ssot.db?mode=ro",uri=True,timeout=120)
truth={(str(a),str(b)):str(c) for a,b,c in conn.execute("SELECT station_id,event_date,settled_bucket FROM paper_trade_settlement")}
pairs=[]
for r in rows:
    k=(r["station"],r["date"])
    env=[json.loads(b) for (b,) in conn.execute("SELECT envelope_json FROM basket_probability_bridge_history WHERE station_id=? AND event_date=? AND decision_at_utc=? AND state='COMMITTED' AND stage='P1' ORDER BY rowid",(k[0],k[1],r["at"]))][-1]
    o,p=env["bucket_ordinals"],env["probabilities"]; tb=truth[k]
    co=r["true_ceil_ord"]
    if co is None: continue
    for b,v in p.items():
        if b not in o: continue
        rel="above" if o[b]>co else "at" if o[b]==co else "below"
        pairs.append({"p":v,"won":b==tb,"rel":rel,"late":r["local_hour"]>=17})
B=lambda s: statistics.mean((x["p"]-(1 if x["won"] else 0))**2 for x in s)
print("all bucket-claims: n=%d Brier=%.4f claimed=%.4f actual=%.4f"%(len(pairs),B(pairs),statistics.mean(x["p"] for x in pairs),sum(x["won"] for x in pairs)/len(pairs)))
print("\n%-22s%6s%10s%9s%9s"%("","n","claimed","actual","Brier"))
for lab,sel in (("LATE(>=17) above",[x for x in pairs if x["late"] and x["rel"]=="above"]),
                ("LATE(>=17) at ceil",[x for x in pairs if x["late"] and x["rel"]=="at"]),
                ("LATE(>=17) below",[x for x in pairs if x["late"] and x["rel"]=="below"]),
                ("EARLY(<17) above",[x for x in pairs if not x["late"] and x["rel"]=="above"]),
                ("EARLY(<17) at ceil",[x for x in pairs if not x["late"] and x["rel"]=="at"]),
                ("EARLY(<17) below",[x for x in pairs if not x["late"] and x["rel"]=="below"])):
    if sel: print("%-22s%6d%10.4f%9.4f%9.4f"%(lab,len(sel),statistics.mean(x["p"] for x in sel),sum(x["won"] for x in sel)/len(sel),B(sel)))
print("\n-- claimed-probability reliability bands (all pairs), and who fills them --")
BANDS=[(0,.02),(.02,.05),(.05,.12),(.12,.25),(.25,.50),(.50,1.01)]
print("%-14s%6s%10s%9s   %s"%("band","n","claimed","actual","composition (late-above / late-at / other)"))
for lo,hi in BANDS:
    s=[x for x in pairs if lo<=x["p"]<hi]
    if not s: continue
    la=sum(1 for x in s if x["late"] and x["rel"]=="above"); lat=sum(1 for x in s if x["late"] and x["rel"]=="at")
    print("%-14s%6d%10.4f%9.4f   %d / %d / %d"%(f"{lo}-{hi}",len(s),statistics.mean(x["p"] for x in s),sum(x["won"] for x in s)/len(s),la,lat,len(s)-la-lat))
