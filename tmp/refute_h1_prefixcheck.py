import json,sqlite3,sys
from collections import defaultdict,Counter
sys.path.insert(0,"tmp"); from stage_pick import DB, local_close, parse
conn=sqlite3.connect(DB,uri=True,timeout=120)
recs=json.load(open("tmp/h1_night_ceiling_rows.json"))
# dedup last-in-bin exactly as theirs
def dedup(lo,hi):
    byk={}
    for r in recs:
        if lo<=r["local_hour"]<hi:
            k=(r["station"],r["date"])
            if k not in byk or r["at"]>byk[k]["at"]: byk[k]=r
    return byk
sel=dedup(21,24)
# for each picked row: is it the max-microsecond row of its instant? is rh the max rh of that instant?
bad=0; checked=0; rhmaxmismatch=0
for k,r in sel.items():
    base=r["at"][:23]  # millisecond
    rows=list(conn.execute("SELECT decision_at_utc,envelope_json FROM basket_probability_bridge_history WHERE station_id=? AND event_date=? AND state='COMMITTED' AND stage='P1' AND decision_at_utc LIKE ?",(k[0],k[1],base+"%")))
    checked+=1
    ats=[a for a,_ in rows]
    if r["at"]!=max(ats): bad+=1
    rhs=[float(json.loads(e)["lineage"]["metar_trajectory_running_high_c"]) for _,e in rows]
    if float(r["running_high_c"])!=max(rhs): rhmaxmismatch+=1
print(f"21-24 dedup: checked={checked} picked-not-last-of-its-instant={bad} picked-rh-not-max-of-instant={rhmaxmismatch}")
print("actual_above sum:",sum(r['actual_above'] for r in sel.values()),"of",len(sel))
for k,r in sel.items():
    if r['actual_above']: print("  the ONE above-case:",k,r['at'],"rh",r['running_high_c'],"settled_o",r['settled_ord'],"rh_o",r['rh_ord'])
