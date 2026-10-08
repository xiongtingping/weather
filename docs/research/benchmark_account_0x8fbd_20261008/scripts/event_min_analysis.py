import json, glob, bisect, collections, statistics as st
E=[]
for p in glob.glob("event_min_*.json"): E+=json.load(open(p))
labels={}
for r in json.load(open("labels.json")):
    k=(r["station_id"],r["event_date"])
    if k not in labels or r["truth_revision_no"]>labels[k]["truth_revision_no"]: labels[k]=r
def won(f):
    lab=labels.get((f["station"],f["event_date"]))
    if not lab: return None
    t=lab["settled_tmax_c"]; t=t*9/5+32 if f["unit"]=="F" else t
    lo,hi=f["lo"],f["hi"]; return (lo is None or t>=lo-0.5) and (hi is None or t<hi+0.5)
rows=[]
for e in E:
    f=e["first"]; T=[h["t"] for h in e["hist"]]; P=[h["p"] for h in e["hist"]]
    if len(T)<10: continue
    def at(t):
        i=bisect.bisect_right(T,t)-1; return P[i] if i>=0 else None
    base=at(f["ts"]-1)
    if base is None: continue
    sgn=1 if f["outcome"]=="Yes" else -1; w=won(f)
    if w is None: continue
    hit=1.0 if (w if sgn>0 else not w) else 0.0
    yes_paid=f["price"] if sgn>0 else 1-f["price"]
    r=dict(taker=f["taker"],follow=e["n_wallets_15"]>=1,usdc15=e["usdc_15"],usdc_first=e["usdc_first"],base=base,hit=hit,sgn=sgn,paid_yes=yes_paid,
           pre60=at(f["ts"]-3600),pre10=at(f["ts"]-600),s1=at(f["ts"]+60),s5=at(f["ts"]+300),s15=at(f["ts"]+900),s60=at(f["ts"]+3600))
    rows.append(r)
print("events with minute data & label:",len(rows))
def grp(tag,rs):
    if len(rs)<20: return
    def m(key): 
        v=[r["sgn"]*(r[key]-r["base"]) for r in rs if r[key] is not None]; return 100*st.mean(v) if v else float('nan')
    paid=st.mean(r["sgn"]*(r["paid_yes"]-r["base"]) for r in rs)*100
    hit=st.mean(r["hit"] for r in rs); base_y=st.mean(r["base"] if r["sgn"]>0 else 1-r["base"] for r in rs); s60y=st.mean((r["s60"] if r["sgn"]>0 else 1-r["s60"]) for r in rs if r["s60"] is not None)
    print(f"  {tag:34s} n={len(rs):3d} | pre-60m {m('pre60'):+.1f} pre-10m {m('pre10'):+.1f} | paid-base {paid:+.1f} | +1m {m('s1'):+.1f} +5m {m('s5'):+.1f} +15m {m('s15'):+.1f} +60m {m('s60'):+.1f} c | mid before {base_y:.3f} -> after60 {s60y:.3f} | hit {hit:.3f} | info(hit-after60) {100*(hit-s60y):+.1f}c")
print("first-fill events, sign-adjusted mid moves relative to mid(t-1min), by first-buyer role and whether other cluster wallets followed within 15 min:")
for tk in (True,False):
    for fo in (True,False):
        grp(f"first={'taker' if tk else 'maker'} follow={'yes' if fo else 'no'}",[r for r in rows if r["taker"]==tk and r["follow"]==fo])
# dose-response: follow-on USDC terciles among taker-first events
tkr=[r for r in rows if r["taker"]]; tkr.sort(key=lambda r:r["usdc15"]); n=len(tkr)
for name,rs in (("follow USDC low",tkr[:n//3]),("mid",tkr[n//3:2*n//3]),("high",tkr[2*n//3:])):
    grp(f"taker-first, {name} (med {st.median(r['usdc15'] for r in rs):.0f}U)",rs)
# first-fill size dose
tkr.sort(key=lambda r:r["usdc_first"])
for name,rs in (("first-fill USDC low",tkr[:n//3]),("mid",tkr[n//3:2*n//3]),("high",tkr[2*n//3:])):
    grp(f"taker-first, {name} (med {st.median(r['usdc_first'] for r in rs):.0f}U)",rs)
