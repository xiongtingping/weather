"""How much of the edge>0.25 population survives if the belief simply refused to
keep mass above the already-measured high once the sun is down?  Rebuilt on the
strict last-complete-prefix population (tmp/final_report_lastrow_rows.json)."""
import json,sqlite3,sys,statistics
sys.path.insert(0,"tmp"); from stage_pick import DB
rows=json.load(open("tmp/final_report_lastrow_rows.json"))
conn=sqlite3.connect(DB,uri=True,timeout=120)
truth={(str(a),str(b)):str(c) for a,b,c in conn.execute("SELECT station_id,event_date,settled_bucket FROM paper_trade_settlement")}
pos={}
for r in conn.execute("SELECT station_id,event_date,window_buckets_json,leg_shares_json FROM paper_trade_position ORDER BY decision_at_utc"):
    k=(str(r[0]),str(r[1])); e=pos.setdefault(k,set())
    legs=json.loads(r[3] or "{}")
    e.update([b for b,s in legs.items() if (s or 0)>0] or json.loads(r[2]))
def rebuild(cut):
    out=[]; leak=[]
    for r in rows:
        k=(r["station"],r["date"])
        env=json.loads(list(conn.execute("SELECT envelope_json FROM basket_probability_bridge_history WHERE station_id=? AND event_date=? AND state='COMMITTED' AND stage='P1' AND decision_at_utc=?",(k[0],k[1],r["at"])))[-1][0])
        p=dict(env["probabilities"]); o=env["bucket_ordinals"]; co=r["ceil_ord"]
        if co is not None and r["local_hour"]>=cut:
            p={b:(0.0 if o.get(b,-1)>co else v) for b,v in p.items()}
            t=sum(p.values())
            if t<=0: continue
            p={b:v/t for b,v in p.items()}
        held=pos[k]; outside={b:v for b,v in p.items() if b not in held}
        if not outside: continue
        tgt=max(outside,key=outside.get)
        b=[(str(a),c) for a,c in conn.execute("SELECT observed_at,buckets_json FROM basket_official_ws_snapshot WHERE station_id=? AND event_date=? ORDER BY observed_at",k)]
        from bisect import bisect_right
        i=bisect_right([x[0] for x in b],r["at"])
        asks={x["bucket"]:float(x["best_ask"]) for x in json.loads(b[i-1][1]) if x.get("best_ask")} if i else {}
        if tgt not in asks: continue
        out.append({"edge":outside[tgt]-asks[tgt],"won":tgt==truth[k],"p":outside[tgt],"ask":asks[tgt]})
    return out
base=[r for r in rows if r["edge"]>.25]
print("baseline edge>0.25: n=%d, hit %d"%(len(base),sum(r["won"] for r in base)))
for cut in (15,17,19):
    o=rebuild(cut); hi=[x for x in o if x["edge"]>.25]
    print("cut local %2d:00 -> targets %3d  edge>0.25 survivors %2d  (their claimed %.3f ask %.3f hit %d)  all-target hit rate %.1f%%"%(
        cut,len(o),len(hi),statistics.mean(x["p"] for x in hi) if hi else 0,statistics.mean(x["ask"] for x in hi) if hi else 0,
        sum(x["won"] for x in hi),100*sum(x["won"] for x in o)/len(o)))
o=rebuild(99); print("no cut (control) -> targets %3d  edge>0.25 %2d  all-target hit %.1f%%"%(len(o),len([x for x in o if x['edge']>.25]),100*sum(x["won"] for x in o)/len(o)))
