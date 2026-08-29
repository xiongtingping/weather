import json,sqlite3,sys
sys.path.insert(0,"tmp"); from stage_pick import DB
conn=sqlite3.connect(DB,uri=True,timeout=120)
rows=list(conn.execute("SELECT rowid,history_id,stage,checkpoint_id,decision_at_utc,fresh_until_utc,created_at_utc,envelope_json FROM basket_probability_bridge_history WHERE station_id='EPWA' AND event_date='2026-08-21' AND state='COMMITTED' AND stage='P1' ORDER BY decision_at_utc"))
print("P1 rows",len(rows))
for r in rows[:12]:
    e=json.loads(r[7]); lin=e.get("lineage") or {}
    print(f"rid={r[0]} ckpt={r[3][:40]} dec={r[4]} fresh={r[5]} created={r[6]}")
    print("   rh=",lin.get("metar_trajectory_running_high_c"),"center=",lin.get("model_center_c"),"mode=",lin.get("observation_likelihood_mode"),"str=",lin.get("metar_trajectory_applied_strength"),"degr=",str(lin.get("metar_trajectory_degradation_reasons"))[:80])
print("\nlineage keys:",sorted((json.loads(rows[0][7]).get("lineage") or {}).keys()))
print("\nenvelope top keys:",sorted(json.loads(rows[0][7]).keys()))
