"""Per-case anatomy of the 19 edge>0.25 targets: where the model's mass sat,
what the ceiling was, whether the ceiling bucket was inside the held window,
and what the model claimed for the ceiling bucket vs what settled."""
import json,sqlite3,sys,statistics
sys.path.insert(0,"tmp"); from stage_pick import DB, local_close, parse
conn=sqlite3.connect(DB,uri=True,timeout=120)
rows=json.load(open("tmp/refute_h1_true_ceiling_rows.json"))
pos={}
for r in conn.execute("SELECT station_id,event_date,window_buckets_json,leg_shares_json FROM paper_trade_position ORDER BY decision_at_utc"):
    k=(str(r[0]),str(r[1])); e=pos.setdefault(k,set())
    legs=json.loads(r[3] or "{}")
    e.update([b for b,s in legs.items() if (s or 0)>0] or json.loads(r[2]))
out=[]
for r in rows:
    k=(r["station"],r["date"])
    env=json.loads(list(conn.execute("SELECT envelope_json FROM basket_probability_bridge_history WHERE station_id=? AND event_date=? AND state='COMMITTED' AND stage='P1' AND decision_at_utc=?",(k[0],k[1],r["at"])))[-1][0])
    p=env["probabilities"]; o=env["bucket_ordinals"]; lin=env["lineage"]
    inv={v:kk for kk,v in o.items()}
    co=r["true_ceil_ord"]; held=pos[k]
    ceil_b = inv.get(co)
    r2=dict(r)
    r2["ceil_bucket"]=ceil_b
    r2["p_ceil"]=p.get(ceil_b)
    r2["ceil_in_held"]= ceil_b in held
    r2["ceil_is_truth"]= (co==r["truth_ord"])
    r2["strength"]=float(lin.get("metar_trajectory_applied_strength") or 0)
    r2["reasons"]=lin.get("metar_trajectory_degradation_reasons","")
    r2["mode"]=lin.get("observation_likelihood_mode","")
    r2["excl"]=lin.get("hard_excluded_buckets","")
    r2["held"]=sorted(held,key=lambda b:o.get(b,-1))
    r2["nbuckets"]=len(p)
    out.append(r2)
json.dump(out,open("tmp/final_report_case_detail.json","w"))
hi=[r for r in out if r["edge"]>.25]
print("== 19 edge>0.25 ==")
print(" ceiling bucket == settled bucket : %d/%d"%(sum(r["ceil_is_truth"] for r in hi),len(hi)))
print(" ceiling bucket inside held window: %d/%d"%(sum(r["ceil_in_held"] for r in hi),len(hi)))
print(" applied_strength == 0            : %d/%d"%(sum(r["strength"]==0 for r in hi),len(hi)))
print(" mean p(ceiling bucket)           : %.3f"%statistics.mean(r["p_ceil"] for r in hi))
print(" mean p(target)                   : %.3f"%statistics.mean(r["p_t"] for r in hi))
print(" mean mass above true ceiling     : %.3f"%statistics.mean(r["mass_above_true_ceiling"] for r in hi))
print(" target won                       : %d/%d"%(sum(r["won"] for r in hi),len(hi)))
print(" ceiling bucket won               : %d/%d"%(sum(r["ceil_is_truth"] for r in hi),len(hi)))
for label,sel in (("edge<=0",[r for r in out if r["edge"]<=0]),("edge 0-0.10",[r for r in out if 0<r["edge"]<=.10]),("edge .10-.25",[r for r in out if .10<r["edge"]<=.25]),("edge>0.25",hi)):
    print("%-13s n=%2d  p_ceil=%.3f ceil_is_truth=%3.0f%%  ceil_in_held=%3.0f%%  s=0:%3.0f%%  mass_above=%.3f  local=%4.1f"%(
        label,len(sel),statistics.mean(r["p_ceil"] for r in sel),
        100*sum(r["ceil_is_truth"] for r in sel)/len(sel),
        100*sum(r["ceil_in_held"] for r in sel)/len(sel),
        100*sum(r["strength"]==0 for r in sel)/len(sel),
        statistics.mean(r["mass_above_true_ceiling"] for r in sel),
        statistics.median(r["local_hour"] for r in sel)))
print()
for r in sorted(hi,key=lambda x:-x["edge"])[:19]:
    print("%s %s lh=%4.1f rh=%s ceil=%-14s p_ceil=%.3f in_held=%d | tgt=%-14s p=%.3f ask=%.3f | truth=%s s=%.2f %s"%(
        r["station"],r["date"],r["local_hour"],r["true_rh"],r["ceil_bucket"],r["p_ceil"],r["ceil_in_held"],
        r["target"],r["p_t"],r["ask"],r["ceil_is_truth"],r["strength"],r["reasons"]))
