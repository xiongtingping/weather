"""Robustness: rebuild the 104 targets forcing the belief to be the LAST P1 row
at its decision instant (the complete prefix), instead of the last row whose
held-window mass still exceeds 0.05.  If the phenomenon is an artifact of
picking a mid-replay prefix row, it should disappear here."""
import json,sqlite3,sys,statistics
from bisect import bisect_right
sys.path.insert(0,"tmp"); from stage_pick import DB, local_close, parse
conn=sqlite3.connect(DB,uri=True,timeout=120)
pos={}
for r in conn.execute("SELECT station_id,event_date,decision_at_utc,window_buckets_json,leg_shares_json FROM paper_trade_position ORDER BY decision_at_utc"):
    k=(str(r[0]),str(r[1])); e=pos.setdefault(k,{"held":set(),"opened":str(r[2])})
    legs=json.loads(r[4] or "{}")
    e["held"].update([b for b,s in legs.items() if (s or 0)>0] or json.loads(r[3]))
truth={(str(a),str(b)):str(c) for a,b,c in conn.execute("SELECT station_id,event_date,settled_bucket FROM paper_trade_settlement")}
tz={(str(a),str(b)):str(c) for a,b,c in conn.execute("SELECT DISTINCT station_id,event_date,settlement_timezone FROM basket_official_ws_snapshot")}
def bucket_of_temp(iv,t):
    for b,(lo,hi) in iv.items():
        if (lo is None or t>lo) and (hi is None or t<=hi): return b
    return None
rows=[]; nlast=0
for k in sorted(truth):
    if k not in pos or k not in tz: continue
    close=local_close(k[1],tz[k]); e=pos[k]
    ch=[(str(a),str(b),c) for a,b,c in conn.execute("SELECT stage,decision_at_utc,envelope_json FROM basket_probability_bridge_history WHERE station_id=? AND event_date=? AND state='COMMITTED' AND stage='P1' ORDER BY decision_at_utc",k)]
    ch=[x for x in ch if x[1]>=e["opened"] and (close-parse(x[1])).total_seconds()>=0]
    if not ch: continue
    # group by instant (truncate the microsecond prefix counter: same second+ms)
    byinst={}
    for s,at,b in ch:
        inst=at[:23]   # to milliseconds; the prefix counter lives in the last 3 digits
        byinst.setdefault(inst,[]).append((at,b))
    lastinst=max(byinst)
    at,blob=byinst[lastinst][-1]
    env=json.loads(blob); p=env["probabilities"]; o=env["bucket_ordinals"]
    tb=truth[k]
    if tb not in o: continue
    lin=env.get("lineage") or {}
    iv=json.loads(lin["bucket_intervals_c_json"]) if lin.get("bucket_intervals_c_json") else None
    rh=lin.get("metar_trajectory_running_high_c")
    co=None
    if iv and rh not in (None,""):
        cb=bucket_of_temp(iv,float(rh)); co=o.get(cb)
    outside={b:v for b,v in p.items() if b not in e["held"]}
    if not outside: continue
    tgt=max(outside,key=outside.get)
    b=[(str(a),c) for a,c in conn.execute("SELECT observed_at,buckets_json FROM basket_official_ws_snapshot WHERE station_id=? AND event_date=? ORDER BY observed_at",k)]
    i=bisect_right([x[0] for x in b],at)
    asks={x["bucket"]:float(x["best_ask"]) for x in json.loads(b[i-1][1]) if x.get("best_ask")} if i else {}
    if tgt not in asks: continue
    d=parse(at); lh=d.astimezone(close.tzinfo)
    rows.append({"station":k[0],"date":k[1],"at":at,"local_hour":lh.hour+lh.minute/60,
        "p_t":outside[tgt],"ask":asks[tgt],"edge":outside[tgt]-asks[tgt],"won":tgt==tb,
        "t_ord":o[tgt],"truth_ord":o[tb],"ceil_ord":co,"rh":rh,
        "above":(co is not None and o[tgt]>co),
        "mass_above":sum(v for bb,v in p.items() if co is not None and o.get(bb,-1)>co) if co is not None else None,
        "ceil_is_truth":(co==o[tb]),
        "held_mass":sum(v for bb,v in p.items() if bb in e["held"])})
print("targets (last-complete-prefix rule):",len(rows))
def blk(n,sel):
    if not sel: print("  %-14s n=0"%n); return
    a=[r for r in sel if r["ceil_ord"] is not None]
    print("  %-14s n=%3d  claimed %.3f  ask %.3f  actual %.3f | above ceil %3.0f%%  ceil==truth %3.0f%%  mass_above %.3f  median local %4.1fh"%(
        n,len(sel),statistics.mean(r["p_t"] for r in sel),statistics.mean(r["ask"] for r in sel),
        sum(r["won"] for r in sel)/len(sel),100*sum(r["above"] for r in a)/max(len(a),1),
        100*sum(r["ceil_is_truth"] for r in a)/max(len(a),1),
        statistics.mean(r["mass_above"] for r in a) if a else -1,
        statistics.median(r["local_hour"] for r in sel)))
blk("edge>0.25",[r for r in rows if r["edge"]>.25])
blk("edge .10-.25",[r for r in rows if .10<r["edge"]<=.25])
blk("edge 0-0.10",[r for r in rows if 0<r["edge"]<=.10])
blk("edge<=0",[r for r in rows if r["edge"]<=0])
json.dump(rows,open("tmp/final_report_lastrow_rows.json","w"))
