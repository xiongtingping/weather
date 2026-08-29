"""Does the market's lean off the model predict where settlement lands?

The proposed mechanism: compare the book-implied distribution with the
model's, and let the sign of the disagreement gate top-ups.  Before wiring
anything, measure the one thing that decides it: conditioned on the market
sitting warm/cold of the model IN THE MORNING (before observation converges
everyone), does settlement land warm/cold of the model's centre more often
than baseline?

Morning-only on purpose: an evening snapshot "predicts" settlement because
the day is over -- the exact clock error already on record twice.
"""
import sqlite3, json, random
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

conn=sqlite3.connect("file:data/bayes_basket_target_ssot.db?mode=ro",uri=True,timeout=120)
tz={r[0]:r[1] for r in conn.execute("SELECT station_id,settlement_timezone"
    " FROM calibration_station_market_timezone_binding")}
truth={(r[0],r[1]):float(r[2]) for r in conn.execute(
    "SELECT station_id,local_date,official_tmax_c FROM calibration_station_daily_high_truth"
    " WHERE qc_status='ACCEPTED'")}

def bucket_centre_c(name):
    # "34°C" / "96°F" -> celsius numeric for ordering only (per-station consistent)
    t=name.replace("°","").strip()
    if t.endswith("F"):
        try: return (float(t[:-1])-32)*5/9
        except: return None
    if t.endswith("C"):
        try: return float(t[:-1])
        except: return None
    try: return float(t)
    except: return None

def implied_mean(buckets):
    """Market mean from mid prices (ask-only fallback), normalised."""
    pts=[]
    for e in buckets:
        c=bucket_centre_c(str(e.get("bucket","")))
        if c is None: continue
        ask=e.get("best_ask"); bid=e.get("bid_price")
        if ask is not None and bid is not None: p=(float(ask)+float(bid))/2
        elif ask is not None: p=float(ask)
        elif bid is not None: p=float(bid)
        else: continue
        p=max(0.0,min(1.0,p))
        pts.append((c,p))
    tot=sum(p for _,p in pts)
    if tot<=0.2 or len(pts)<3: return None
    return sum(c*p for c,p in pts)/tot

def model_mean(probs):
    pts=[]
    for k,v in probs.items():
        c=bucket_centre_c(k)
        if c is not None and v>0: pts.append((c,float(v)))
    tot=sum(p for _,p in pts)
    if tot<=0: return None
    return sum(c*p for c,p in pts)/tot

# per station-day: last snapshot before local noon, paired with the latest
# served probability at or before that snapshot.
samples=[]
days=set()
rows=conn.execute(
  "SELECT station_id,event_date,observed_at,buckets_json FROM basket_official_ws_snapshot"
  " WHERE event_date>='2026-08-10' ORDER BY station_id,event_date,observed_at").fetchall()
by_sd=defaultdict(list)
for st,ed,obs,bj in rows:
    if (st,ed) not in truth or st not in tz: continue
    by_sd[(st,ed)].append((obs,bj))
for (st,ed),snaps in by_sd.items():
    zone=ZoneInfo(tz[st])
    pick=None
    for obs,bj in snaps:
        local=datetime.fromisoformat(obs[:19]+"+00:00").astimezone(zone)
        if str(local.date())==ed and local.hour<12:
            pick=(obs,bj)
    if pick is None: continue
    obs,bj=pick
    mm=implied_mean(json.loads(bj or "[]"))
    if mm is None: continue
    r=conn.execute(
      "SELECT probabilities_json FROM basket_probability_envelope"
      " WHERE station_id=? AND event_date=? AND decision_at_utc<=?"
      " ORDER BY decision_at_utc DESC LIMIT 1",(st,ed,obs)).fetchone()
    if r is None: continue
    mo=model_mean(json.loads(r[0] or "{}"))
    if mo is None: continue
    lean=mm-mo                       # >0: market warm of model
    out=truth[(st,ed)]-mo            # >0: settlement warm of model
    samples.append((st,ed,lean,out))
    days.add(ed)

print(f"样本: {len(samples)} 站日（{len(days)} 个事件日），早盘(当地12时前)快照")
TOL=0.5
groups={"市场偏暖(>+0.5°C)":[s for s in samples if s[2]>TOL],
        "对齐(±0.5°C)":[s for s in samples if abs(s[2])<=TOL],
        "市场偏冷(<-0.5°C)":[s for s in samples if s[2]<-TOL]}
print(f"\n{'分组':22s} {'n':>4s} {'结算-模型均值 中位':>14s} {'结算偏暖比例':>10s}")
for g,rs in groups.items():
    if not rs: continue
    outs=sorted(x[3] for x in rs)
    med=outs[len(outs)//2]
    warm=sum(1 for x in rs if x[3]>0)/len(rs)
    print(f"{g:22s} {len(rs):4d} {med:14.2f}°C {warm:9.0%}")
base=sorted(x[3] for x in samples); print(f"\n全体基线: 中位 {base[len(base)//2]:+.2f}°C, 偏暖比例 {sum(1 for x in samples if x[3]>0)/len(samples):.0%}")

# 逐日 bootstrap: 市场偏暖组的"结算偏暖比例"减基线，是否稳健为正
random.seed(20260828)
by_day=defaultdict(list)
for s in samples: by_day[s[1]].append(s)
dlist=sorted(by_day)
def stat(sel_days):
    pool=[s for d in sel_days for s in by_day[d]]
    warm_grp=[s for s in pool if s[2]>TOL]
    if len(warm_grp)<5: return None
    return (sum(1 for x in warm_grp if x[3]>0)/len(warm_grp)
            - sum(1 for x in pool if x[3]>0)/len(pool))
pt=stat(dlist)
bs=[]
for _ in range(1000):
    v=stat([random.choice(dlist) for _ in dlist])
    if v is not None: bs.append(v)
bs.sort()
if bs and pt is not None:
    lo,hi=bs[int(0.025*len(bs))],bs[int(0.975*len(bs))]
    print(f"\n市场偏暖组超额偏暖率: {pt:+.0%}  逐日bootstrap 95%CI [{lo:+.0%},{hi:+.0%}]"
          f"  → {'显著' if lo>0 else '不显著'}")
# 对称性：偏冷组
def stat_cold(sel_days):
    pool=[s for d in sel_days for s in by_day[d]]
    grp=[s for s in pool if s[2]<-TOL]
    if len(grp)<5: return None
    return (sum(1 for x in grp if x[3]<0)/len(grp)
            - sum(1 for x in pool if x[3]<0)/len(pool))
pt2=stat_cold(dlist)
bs2=sorted(v for v in (stat_cold([random.choice(dlist) for _ in dlist]) for _ in range(1000)) if v is not None)
if bs2 and pt2 is not None:
    lo2,hi2=bs2[int(0.025*len(bs2))],bs2[int(0.975*len(bs2))]
    print(f"市场偏冷组超额偏冷率: {pt2:+.0%}  95%CI [{lo2:+.0%},{hi2:+.0%}]"
          f"  → {'显著' if lo2>0 else '不显著'}")
