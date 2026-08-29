"""Adversarial check: does the ceiling use only observations committed at or
before the decision instant, and had the day's true high already occurred?"""
import json,sqlite3,sys,statistics
sys.path.insert(0,"tmp"); from stage_pick import parse, local_close
conn=sqlite3.connect("file:data/bayes_basket_target_ssot.db?mode=ro",uri=True,timeout=120)
rows=json.load(open("tmp/refute_h1_true_ceiling_rows.json"))
bad=0; worst=None
for r in rows:
    got=conn.execute("SELECT envelope_json FROM basket_probability_bridge_history WHERE station_id=? AND event_date=? AND state='COMMITTED' AND stage='P1' AND decision_at_utc<=?",(r["station"],r["date"],r["at"])).fetchall()
    mx=max((json.loads(e)["lineage"].get("canonical_observation_committed_at") or "") for (e,) in got)
    if mx and mx.replace("Z","")>r["at"].replace("Z",""):
        bad+=1
        d=(parse(mx)-parse(r["at"])).total_seconds()/60
        if worst is None or d>worst[0]: worst=(d,r["station"],r["date"])
print(f"targets whose ceiling used an observation committed AFTER the decision: {bad}/{len(rows)}", "worst:",worst)

# archive.db: when was the day's high actually reached?
import os
ap=[p for p in ("data/archive.db","archive.db") if os.path.exists(p)]
print("archive:",ap)
a=sqlite3.connect(f"file:{ap[0]}?mode=ro",uri=True,timeout=120)
tzm={(str(x),str(y)):str(z) for x,y,z in conn.execute("SELECT DISTINCT station_id,event_date,settlement_timezone FROM basket_official_ws_snapshot")}
lat=[]; miss=0
for r in rows:
    q=a.execute("SELECT observed_max_c,max_valid_at FROM reported_trajectory_canonical_station_day_bounds WHERE station_id=? AND event_date=?",(r["station"],r["date"])).fetchone()
    if not q or not q[1]: miss+=1; continue
    close=local_close(r["date"],tzm[(r["station"],r["date"])])
    pk=parse(q[1]).astimezone(close.tzinfo)
    lat.append(dict(edge=r["edge"],peak_local=pk.hour+pk.minute/60,dec_local=r["local_hour"],
                    after_peak=(parse(r["at"])-parse(q[1])).total_seconds()/3600, above_true=r["above_true_ceiling"], won=r["won"]))
print("matched in archive:",len(lat),"missing:",miss)
for name,sel in (("edge>0.25",[x for x in lat if x["edge"]>.25]),("0.10-0.25",[x for x in lat if .10<x["edge"]<=.25]),("0-0.10",[x for x in lat if 0<x["edge"]<=.10]),("edge<=0",[x for x in lat if x["edge"]<=0])):
    if not sel: continue
    print(f"  {name:12s} n={len(sel):3d} median peak_local={statistics.median(x['peak_local'] for x in sel):5.1f}h  median hours AFTER true peak={statistics.median(x['after_peak'] for x in sel):5.2f}  frac decided after peak={sum(x['after_peak']>0 for x in sel)/len(sel):.2f}  hit={sum(x['won'] for x in sel)/len(sel):.3f}")
