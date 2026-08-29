"""Recompute the 104 targets' ceiling using the TRUE running high known at the
decision instant = max(metar_trajectory_running_high_c) over every P1 row with
decision_at_utc <= the selected row (fixes the stale-prefix ceiling)."""
import json,sqlite3,sys,statistics
from collections import Counter
sys.path.insert(0,"tmp"); from stage_pick import DB
conn=sqlite3.connect(DB,uri=True,timeout=120)
rows=json.load(open("tmp/h1_edge_targets_rows.json"))
def bucket_of_temp(iv,t):
    for b,(lo,hi) in iv.items():
        if (lo is None or t>lo) and (hi is None or t<=hi): return b
    return None
out=[]
for r in rows:
    got=list(conn.execute("SELECT decision_at_utc,envelope_json FROM basket_probability_bridge_history WHERE station_id=? AND event_date=? AND state='COMMITTED' AND stage='P1' AND decision_at_utc<=? ORDER BY decision_at_utc",(r["station"],r["date"],r["at"])))
    rhmax=None; env=None
    for a,e in got:
        lin=json.loads(e)["lineage"]
        v=lin.get("metar_trajectory_running_high_c")
        if v not in (None,""):
            v=float(v)
            if rhmax is None or v>rhmax: rhmax=v
        if a==r["at"]: env=json.loads(e)
    o=env["bucket_ordinals"]; p=env["probabilities"]
    iv=json.loads(env["lineage"]["bucket_intervals_c_json"])
    cb=bucket_of_temp(iv,rhmax); co=o.get(cb)
    r2=dict(r); r2["true_rh"]=rhmax; r2["true_ceil_ord"]=co
    r2["above_true_ceiling"]= (co is not None and r["t_ord"]>co)
    r2["mass_above_true_ceiling"]= sum(v for b,v in p.items() if o.get(b,-1)>co) if co is not None else None
    r2["stale"]= float(r["rh_c"])!=rhmax
    out.append(r2)
json.dump(out,open("tmp/refute_h1_true_ceiling_rows.json","w"))
def blk(name,sel):
    if not sel: print(f"  {name:16s} n=0"); return
    a=[r for r in sel if r["true_ceil_ord"] is not None]
    print(f"  {name:16s} n={len(sel):3d}  claimed {statistics.mean(r['p_t'] for r in sel):.3f}  ask {statistics.mean(r['ask'] for r in sel):.3f}  actual {sum(r['won'] for r in sel)/len(sel):.3f} | "
          f"above STALE ceiling {100*sum(r['above_ceiling'] for r in sel)/len(sel):3.0f}%  above TRUE ceiling {100*sum(r['above_true_ceiling'] for r in a)/len(a):3.0f}%  "
          f"stale-prefix rows {sum(r['stale'] for r in sel):2d}  median local {statistics.median(r['local_hour'] for r in sel):4.1f}h  mass>trueceil {statistics.mean(r['mass_above_true_ceiling'] for r in a):.3f}")
print("== 104 targets, ceiling recomputed from the true running high at that instant ==")
blk("edge>0.25",[r for r in out if r['edge']>.25])
blk("edge 0.10-0.25",[r for r in out if .10<r['edge']<=.25])
blk("edge 0-0.10",[r for r in out if 0<r['edge']<=.10])
blk("edge<=0",[r for r in out if r['edge']<=0])
hi=[r for r in out if r['edge']>.25]
print("\nedge>0.25 detail (19):")
for r in sorted(hi,key=lambda x:-x['edge']):
    print(f"  {r['station']} {r['date']} lh={r['local_hour']:5.1f} edge={r['edge']:.3f} p={r['p_t']:.3f} ask={r['ask']:.3f} tgt_o={r['t_ord']} truth_o={r['truth_ord']} staleceil_o={r['ceil_ord']} trueceil_o={r['true_ceil_ord']} above_true={r['above_true_ceiling']} stale={r['stale']}")
print("\nabove TRUE ceiling among edge>0.25:",sum(r['above_true_ceiling'] for r in hi),"/",len(hi))
print("night (local>=17) among edge>0.25:",sum(r['local_hour']>=17 for r in hi),"/",len(hi))
print("above TRUE ceiling among edge<=0:",sum(r['above_true_ceiling'] for r in out if r['edge']<=0),"/",sum(1 for r in out if r['edge']<=0))
