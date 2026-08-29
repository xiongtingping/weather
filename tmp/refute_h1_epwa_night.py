import json,sqlite3,sys
from collections import Counter
sys.path.insert(0,"tmp"); from stage_pick import DB
conn=sqlite3.connect(DB,uri=True,timeout=120)
rows=list(conn.execute("SELECT rowid,decision_at_utc,envelope_json FROM basket_probability_bridge_history WHERE station_id='EPWA' AND event_date='2026-08-21' AND state='COMMITTED' AND stage='P1' AND decision_at_utc>='2026-08-21T19:00' ORDER BY decision_at_utc"))
print("night rows",len(rows))
F=["metar_trajectory_running_high_c","lead_bucket","run_cycle","run_cycle_scope","source_lane","probability_pathway","report_type","canonical_observation_event_id","canonical_observation_committed_at","metar_trajectory_activation_id","observation_likelihood_mode","metar_trajectory_degradation_reasons","reported_high_c","event_consumed_once","materialization_schema"]
for rid,at,env in rows[:20]:
    lin=json.loads(env)["lineage"]
    print(at, " | ".join(f"{k.split('_')[-1]}={str(lin.get(k))[:26]}" for k in F))
print()
for k in F:
    print(k, Counter(str(json.loads(e)["lineage"].get(k))[:40] for _,_,e in rows).most_common(12))
