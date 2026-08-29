"""Cross-check the derived ceiling bucket against production's own
hard_excluded_buckets: production's lowest surviving bucket must equal it."""
import json,sqlite3,sys
from collections import Counter
conn=sqlite3.connect("file:data/bayes_basket_target_ssot.db?mode=ro",uri=True,timeout=120)
recs=json.load(open("tmp/h1_night_ceiling_rows.json"))
byk={}
for r in recs:
    if 17<=r["local_hour"]<24:
        k=(r["station"],r["date"])
        if k not in byk or r["at"]>byk[k]["at"]: byk[k]=r
d=Counter(); ex=[]
for k,r in byk.items():
    (e,)=conn.execute("SELECT envelope_json FROM basket_probability_bridge_history WHERE station_id=? AND event_date=? AND decision_at_utc=? AND stage='P1'",(k[0],k[1],r["at"])).fetchone()
    env=json.loads(e); o=env["bucket_ordinals"]
    exc={b.strip() for b in (env["lineage"].get("hard_excluded_buckets") or "").split(",") if b.strip()}
    surv=[b for b in o if b not in exc]
    lo=min(surv,key=lambda b:o[b])
    d[o[lo]-r["rh_ord"]]+=1
    if o[lo]!=r["rh_ord"]: ex.append((k,r["rh_bucket"],lo,r["mass_above"]))
print("night dedup n=%d ; (production lowest survivor ordinal - derived ceiling ordinal):"%len(byk),dict(sorted(d.items())))
for x in ex[:10]: print("   mismatch:",x)
