"""P1 path accuracy: hit rate by local hour, and how early it locks on.

Method guards, each from a prior mis-measurement on this exact lane:
- served row = MAX(rowid) within a decision burst (earlier rows are
  observation-blind prefixes);
- a hit AFTER the observed running high has reached the settled max is a
  confirmation, not a prediction (the early-prophet ruling), so lead time
  is measured against the moment the peak was first OBSERVED;
- lock-on = the last time argmax changed to the settled bucket and never
  left it again, which is stricter than "first hit".
"""
import sqlite3, json
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

conn=sqlite3.connect("file:data/bayes_basket_target_ssot.db?mode=ro",uri=True,timeout=120)
tz={r[0]:r[1] for r in conn.execute("SELECT station_id,settlement_timezone"
    " FROM calibration_station_market_timezone_binding")}
settled={(r[0],r[1]):str(r[2]) for r in conn.execute(
  "SELECT station_id,event_date,settled_bucket FROM paper_trade_settlement")}
truth={(r[0],r[1]):float(r[2]) for r in conn.execute(
  "SELECT station_id,local_date,official_tmax_c FROM calibration_station_daily_high_truth"
  " WHERE qc_status='ACCEPTED'")}

by_hour=defaultdict(lambda:[0,0,0])   # local hour -> [n, hit0, hit1]
pre_peak=defaultdict(lambda:[0,0])    # before/after peak -> [n, hit]
locks=[]; first_hits=[]
days=0
for (st,ed),sb in sorted(settled.items()):
    if st not in tz or (st,ed) not in truth: continue
    tmax=truth[(st,ed)]
    rows=conn.execute(
      "SELECT decision_at_utc, MAX(rowid) AS rid FROM basket_probability_envelope"
      " WHERE station_id=? AND event_date=? AND stage='P1'"
      " GROUP BY decision_at_utc ORDER BY decision_at_utc",(st,ed)).fetchall()
    if len(rows)<4: continue
    seq=[]
    peak_at=None
    for dec,rid in rows:
        pj,lj=conn.execute("SELECT probabilities_json,lineage_json"
          " FROM basket_probability_envelope WHERE rowid=?",(rid,)).fetchone()
        p=json.loads(pj or "{}"); l=json.loads(lj or "{}")
        if not p: continue
        am=max(p,key=lambda k:p[k])
        # ordinal distance via sorted bucket list
        keys=sorted(p, key=lambda k: json.loads(lj).get("x",0) if False else 0)
        seq.append((dec,am,l))
        rh=l.get("reported_high_c")
        if peak_at is None and rh is not None and float(rh)>=tmax-0.05:
            peak_at=dec
    if not seq: continue
    days+=1
    zone=ZoneInfo(tz[st])
    # ordinal map from any row's bucket_ordinals? use last row's probability keys order fallback
    # hit±1: need ordinals; take from envelope bucket_ordinals
    ordj=conn.execute("SELECT bucket_ordinals_json FROM basket_probability_envelope"
      " WHERE station_id=? AND event_date=? AND stage='P1' ORDER BY rowid DESC LIMIT 1",(st,ed)).fetchone()
    ords=json.loads(ordj[0] or "{}") if ordj else {}
    so=ords.get(sb)
    lock=None
    fh=None
    for dec,am,l in seq:
        h=datetime.fromisoformat(dec.replace("Z","+00:00")).astimezone(zone)
        if str(h.date())!=ed: continue
        hit0=1 if am==sb else 0
        d1=0
        if so is not None and am in ords:
            d1=1 if abs(int(ords[am])-int(so))<=1 else 0
        by_hour[h.hour][0]+=1; by_hour[h.hour][1]+=hit0; by_hour[h.hour][2]+=d1
        pre = peak_at is None or dec < peak_at
        pp=pre_peak["峰值前" if pre else "峰值后"]
        pp[0]+=1; pp[1]+=hit0
        if hit0 and fh is None: fh=dec
        if hit0 and lock is None: lock=dec
        if not hit0: lock=None
    def lead(t):
        if t is None or peak_at is None: return None
        return (datetime.fromisoformat(peak_at.replace("Z","+00:00"))
                -datetime.fromisoformat(t.replace("Z","+00:00"))).total_seconds()/3600
    L=lead(lock); F=lead(fh)
    if L is not None: locks.append(L)
    if F is not None: first_hits.append(F)

print(f"样本: {days} 个已结算站日")
print(f"\n按当地小时的 argmax 命中率（服务版，仅事件日当天）:")
print(f"{'时段':>6s} {'n':>5s} {'命中':>6s} {'±1桶':>6s}")
for band,hours in (("0-6",range(0,6)),("6-9",range(6,9)),("9-12",range(9,12)),
                   ("12-15",range(12,15)),("15-18",range(15,18)),("18-21",range(18,21)),("21-24",range(21,24))):
    n=sum(by_hour[h][0] for h in hours); h0=sum(by_hour[h][1] for h in hours); h1=sum(by_hour[h][2] for h in hours)
    if n: print(f"{band:>6s} {n:5d} {h0/n:6.0%} {h1/n:6.0%}")
print("\n峰值前 vs 峰值后（早盘预言家判据）:")
for k,(n,h) in pre_peak.items():
    print(f"  {k}: n={n}  命中 {h/n:.0%}")
locks.sort(); first_hits.sort()
def q(a,f): return a[int(f*(len(a)-1))] if a else None
print(f"\n锁定提前量（最终锁定正确桶的时刻 → 峰值被观测到的时刻，小时）:")
print(f"  n={len(locks)}  中位 {q(locks,.5):+.1f}h  p25 {q(locks,.25):+.1f}h  p75 {q(locks,.75):+.1f}h")
print(f"  正值=峰值之前锁定（真预测）；负值=峰值之后才锁定（确认）")
print(f"  峰值前锁定的比例: {sum(1 for x in locks if x>0)/len(locks):.0%}")
print(f"\n首次命中提前量: n={len(first_hits)} 中位 {q(first_hits,.5):+.1f}h  峰值前首中比例 {sum(1 for x in first_hits if x>0)/len(first_hits):.0%}")
