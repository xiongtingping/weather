import json,sqlite3,sys,statistics
from collections import Counter,defaultdict
sys.path.insert(0,"tmp"); from stage_pick import DB
conn=sqlite3.connect(DB,uri=True,timeout=120)

# ---- TEST A: are the 104 selected decision rows the LAST row of their instant (complete prefix)?
rows=json.load(open("tmp/h1_edge_targets_rows.json"))
bad=[];  n_multi=0
for r in rows:
    base=r["at"][:23]
    got=list(conn.execute("SELECT decision_at_utc,envelope_json FROM basket_probability_bridge_history WHERE station_id=? AND event_date=? AND state='COMMITTED' AND stage='P1' AND decision_at_utc LIKE ?",(r["station"],r["date"],base+"%")))
    if len(got)>1: n_multi+=1
    mx=max(a for a,_ in got)
    if r["at"]!=mx:
        rh_full=json.loads([e for a,e in got if a==mx][0])["lineage"].get("metar_trajectory_running_high_c")
        bad.append((r["station"],r["date"],r["edge"],r["rh_c"],rh_full,len(got)))
print(f"TEST A: 104 targets; instants with >1 prefix row: {n_multi}; selected row NOT the complete prefix: {len(bad)}")
for b in bad[:15]: print("   ",b)

# ---- TEST B: night mass-above-ceiling split by degradation
recs=json.load(open("tmp/h1_night_ceiling_rows.json"))
def dedup(lo,hi,pred=None):
    byk={}
    for r in recs:
        if lo<=r["local_hour"]<hi and (pred is None or pred(r)):
            k=(r["station"],r["date"])
            if k not in byk or r["at"]>byk[k]["at"]: byk[k]=r
    return list(byk.values())
print("\nTEST B: night (local 17-24), station-day-level last complete prefix, split by applied_strength")
for name,pred in [("ALL",None),("strength==0",lambda r: float(r['strength'] or 0)==0),("strength>0",lambda r: float(r['strength'] or 0)>0)]:
    sel=dedup(17,24,pred)
    if not sel: print(f"  {name:14s} n=0"); continue
    cl=statistics.mean(r["mass_above"] for r in sel); ac=statistics.mean(r["actual_above"] for r in sel)
    print(f"  {name:14s} n={len(sel):4d}  claimed>ceiling {cl:.3f}  actual {ac:.3f}")
print("  strength distribution over ALL P1 rows lead>=0:",Counter(round(float(r['strength'] or 0),2) for r in recs).most_common(6))
# how many station-days ever have strength>0 at night?
sd_all={(r['station'],r['date']) for r in recs if 17<=r['local_hour']<24}
sd_pos={(r['station'],r['date']) for r in recs if 17<=r['local_hour']<24 and float(r['strength'] or 0)>0}
print(f"  night station-days: {len(sd_all)}, of which some row has strength>0: {len(sd_pos)}")

# ---- TEST C: the 19 edge>0.25 -- degradation state + prefix completeness
hi=[r for r in rows if r['edge']>0.25]
st=[]
for r in hi:
    e=list(conn.execute("SELECT envelope_json FROM basket_probability_bridge_history WHERE station_id=? AND event_date=? AND decision_at_utc=? AND stage='P1'",(r['station'],r['date'],r['at'])))
    lin=json.loads(e[0][0])['lineage']
    st.append((lin.get('metar_trajectory_applied_strength'),lin.get('metar_trajectory_degradation_reasons'),lin.get('observation_likelihood_mode','')[:28]))
print("\nTEST C: 19 edge>0.25 rows, (strength, degradation, mode):")
for k,v in Counter(st).most_common(): print("   ",v,"x",k)
