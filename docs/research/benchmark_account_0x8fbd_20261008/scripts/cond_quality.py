"""Conditional probability quality on the cluster's bought buckets: binary LL of F (post-fill mid), pre mid, price, ours, by condition.
Plus the all-candidates layer: multiclass LogScore of the pre-trade ladder vs the post-trade ladder (snapshots)."""
import json, glob, bisect, math, statistics as st, collections, datetime as dt, sqlite3
from zoneinfo import ZoneInfo
E=json.load(open("events_enriched.json")); reg=json.load(open("registry.json")); tz={r["station_id"]:r["settlement_timezone"] for r in reg}
H={}
for p in glob.glob("hist_b*.json")+["hist.json"]: H.update(json.load(open(p)))
S={cid:([h["t"] for h in hs],[h["p"] for h in hs]) for cid,hs in H.items()}
def mid_after(cid,ts,dm):
    if cid not in S: return None
    T,P=S[cid]; i=bisect.bisect_right(T,ts+dm*60)-1
    return P[i] if i>=0 and T[i]>ts else None
def ll(p,h): p=min(.999,max(.001,p)); return -(h*math.log(p)+(1-h)*math.log(1-p))
rows=[]
for e in E:
    if not e["first_taker"] or e["truth_ord"] is None or e["mid0"] is None: continue
    m60=mid_after(e["cid"],e["ts"],60)
    if m60 is None: continue
    sgn=1 if e["outcome"]=="Yes" else -1; own=lambda m: m if sgn>0 else 1-m
    hit=1.0 if ((e["truth_ord"]==e["my_ord"]) if sgn>0 else (e["truth_ord"]!=e["my_ord"])) else 0.0
    lt=dt.datetime.fromtimestamp(e["ts"],dt.timezone.utc).astimezone(ZoneInfo(tz.get(e["station"],"UTC")))
    r=dict(F=own(m60),mid=own(e["mid0"]),price=e["first_price"],hit=hit,lead=e["lead_h"],lhour=lt.hour,side=e["outcome"],unit=e["unit"],usdc=e["usdc"],st=e["station"],half="H1" if e["event_date"]<"2026-09-30" else "H2")
    if e["p0"] and e["p0"]["p_my"] is not None: r["ours"]=own(e["p0"]["p_my"]); r["stage"]=e["p0"]["stage"]
    if e["market"]: r["qm"]=e["market"]["q_mode"]; r["d"]=e["market"]["d"]; r["skew"]=e["market"]["skew"]
    rows.append(r)
print("legs:",len(rows))
def rep(tag,rs):
    if len(rs)<40: return
    U=lambda f: st.mean(f(x) for x in rs); wo=[x for x in rs if "ours" in x]
    print(f"  {tag:30s} n={len(rs):4d} | LL F {U(lambda x:ll(x['F'],x['hit'])):.3f} | pre-mid {U(lambda x:ll(x['mid'],x['hit'])):.3f} | price {U(lambda x:ll(x['price'],x['hit'])):.3f} | ours {st.mean(ll(x['ours'],x['hit']) for x in wo) if wo else float('nan'):.3f} | F−mid gain {U(lambda x:ll(x['mid'],x['hit'])-ll(x['F'],x['hit'])):+.3f} | ours−mid {st.mean(ll(x['ours'],x['hit'])-ll(x['mid'],x['hit']) for x in wo) if wo else float('nan'):+.3f} | hit {U(lambda x:x['hit']):.3f}")
rep("ALL",rows)
print("-- by lead"); [rep(f"lead {lo}..{hi}h",[x for x in rows if lo<=x["lead"]<hi]) for lo,hi in ((-24,0),(0,12),(12,24),(24,48),(48,200))]
print("-- by side"); [rep(f"side {s}",[x for x in rows if x["side"]==s]) for s in ("Yes","No")]
print("-- by price level"); [rep(f"price {lo}-{hi}",[x for x in rows if lo<=x["price"]<hi]) for lo,hi in ((0,0.15),(0.15,0.3),(0.3,0.5),(0.5,0.7),(0.7,1))]
print("-- by market concentration"); [rep(f"q_mode {lo}-{hi}",[x for x in rows if "qm" in x and lo<=x["qm"]<hi]) for lo,hi in ((0,0.35),(0.35,0.45),(0.45,0.55),(0.55,1))]
print("-- by bucket vs mode"); [rep(f"d={d}",[x for x in rows if x.get("d")==d]) for d in (-1,0,1)]
print("-- by our disagreement (ours - pre mid)"); [rep(n,[x for x in rows if "ours" in x and lo<=x["ours"]-x["mid"]<hi]) for lo,hi,n in ((-1,-0.1,"ours<<mid"),(-0.1,-0.03,"ours<mid"),(-0.03,0.03,"ours≈mid"),(0.03,0.1,"ours>mid"),(0.1,1,"ours>>mid"))]
print("-- by our stage"); [rep(f"stage {sg}",[x for x in rows if x.get("stage")==sg]) for sg in ("P0","P0-M","P1")]
print("-- by episode size"); [rep(n,[x for x in rows if lo<=x["usdc"]<hi]) for lo,hi,n in ((0,5,"<5U"),(5,20,"5-20U"),(20,60,"20-60U"),(60,1e9,">=60U"))]
print("-- by unit"); [rep(f"{u} stations",[x for x in rows if x["unit"]==u]) for u in ("C","F")]
print("-- by local hour"); [rep(f"local {lo:02d}-{hi:02d}",[x for x in rows if lo<=x["lhour"]<hi]) for lo,hi in ((0,6),(6,12),(12,18),(18,24))]
print("-- by half"); [rep(h_,[x for x in rows if x["half"]==h_]) for h_ in ("H1","H2")]
# ---- all-candidates layer: pre vs post ladder (snapshots) multiclass LogScore on their first-fill station-days
DB="/Users/xiong/weather/data/bayes_basket_target_ssot.db"
conn=sqlite3.connect(f"file:{DB}?mode=ro",uri=True,isolation_level=None,timeout=30); conn.execute("PRAGMA query_only=1")
def ladder(st_,ed,ts_iso,after=False):
    if after: row=conn.execute("select observed_at,buckets_json from basket_official_ws_snapshot where station_id=? and event_date=? and state='COMMITTED' and observed_at>=? order by observed_at asc limit 1",(st_,ed,ts_iso)).fetchone()
    else: row=conn.execute("select observed_at,buckets_json from basket_official_ws_snapshot where station_id=? and event_date=? and state='COMMITTED' and observed_at<=? order by observed_at desc limit 1",(st_,ed,ts_iso)).fetchone()
    if not row: return None
    B=json.loads(row[1]); q={}
    for b in B:
        bid,ask=b.get("bid_price"),b.get("best_ask")
        if bid is not None and ask is not None and ask-bid<0.2: q[b["ordinal"]]=(bid+ask)/2
    s=sum(q.values())
    if len(q)<3 or not 0.85<=s<=1.2: return None
    return row[0],{o:v/s for o,v in q.items()}
def logs(q,truth):
    fl={o:max(v,1e-3) for o,v in q.items()}; s=sum(fl.values()); return -math.log(fl.get(truth,1e-3)/s)
seen=set(); pre=[]; post=[]; ours=[]
for e in E:
    if not e["first_taker"] or e["truth_ord"] is None: continue
    key=(e["station"],e["event_date"],e["ts"]//3600)
    if key in seen: continue
    seen.add(key)
    t=dt.datetime.fromtimestamp(e["ts"],dt.timezone.utc)
    a=ladder(e["station"],e["event_date"],t.strftime("%Y-%m-%dT%H:%M:%S.%fZ"))
    b=ladder(e["station"],e["event_date"],(t+dt.timedelta(minutes=60)).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),after=True)
    if not a or not b: continue
    if (t-dt.datetime.fromisoformat(a[0].replace("Z","+00:00"))).total_seconds()>1800: continue
    if (dt.datetime.fromisoformat(b[0].replace("Z","+00:00"))-t).total_seconds()>3*3600: continue
    if e["truth_ord"] not in a[1] or e["truth_ord"] not in b[1]: continue
    pre.append(logs(a[1],e["truth_ord"])); post.append(logs(b[1],e["truth_ord"]))
    h=conn.execute("select history_id from basket_probability_bridge_history where station_id=? and event_date=? and state='COMMITTED' and decision_at_utc<=? order by decision_at_utc desc limit 1",(e["station"],e["event_date"],t.strftime("%Y-%m-%dT%H:%M:%S.%fZ"))).fetchone()
    if h:
        P={o:p for o,p in conn.execute("select ordinal,probability from basket_probability_bridge_bucket_probability where history_id=?",(h[0],))}
        tt=sum(P.values()); ours.append(logs({o:v/tt for o,v in P.items()},e["truth_ord"]))
conn.close()
print(f"\nALL-CANDIDATES layer (whole ladder, multiclass LogScore) on {len(pre)} station-day-hours around their first fills: pre-trade ladder {st.mean(pre):.4f} | post-trade ladder (next snapshot ≥60min) {st.mean(post):.4f} | ours {st.mean(ours):.4f} (n={len(ours)}) | post−pre {st.mean(post)-st.mean(pre):+.4f} | share post better {st.mean(b<a for a,b in zip(pre,post)):.3f}")
