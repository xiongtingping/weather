import sqlite3,json,sys,re,math
sys.path.insert(0,"/Users/xiong/weather")
from scripts import paper_trading_ledger as ptl
c=sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro",uri=True,timeout=60); c.row_factory=sqlite3.Row
rows=c.execute("""select p.*, s.settled_bucket, s.settled_tmax_c from paper_trade_position p
 join paper_trade_settlement s on s.station_id=p.station_id and s.event_date=p.event_date""").fetchall()
FILL=re.compile(r"^(\d+)/(\d+)@([0-9.]+)$"); J=lambda x: json.loads(x) if x else {}
recs=[]
for r in rows:
    centre=r["ladder_center_bucket"]; posted=J(r["posted_buckets_json"]) or None
    pr=J(r["probabilities_json"]); px=J(r["prices_json"]); legs=J(r["leg_shares_json"]); inten=J(r["intended_legs_json"]); fills=J(r["resting_fill_json"])
    for b,txt in fills.items():
        m=FILL.match(str(txt))
        if not m: continue
        got=int(m.group(1)); fpx=float(m.group(3))
        sh=float(legs.get(b,0) or 0); it=float(inten.get(b,0) or 0)
        if it<=0 and sh<=0: continue
        if not ptl._is_shoulder(b,centre,posted): continue
        recs.append(dict(st=r["station_id"],ed=r["event_date"],stage=r["stage"],b=b,centre=centre,
                         claim=float(pr.get(b,0) or 0), ask=float(px.get(b,0) or 0), fpx=fpx if got>0 else None,
                         filled=1 if (got>0 and sh>0) else 0, hit=1 if str(b)==str(r["settled_bucket"]) else 0,
                         sb=r["settled_bucket"], tmax=r["settled_tmax_c"]))
print("posted shoulder rungs n=",len(recs))
edges=[0,0.05,0.10,0.15,0.25,1.01]
print(f"{'decision ask bin':>18} {'n':>4} {'fill%':>6} {'claim':>6} {'settle%':>8} {'settle|filled':>14} {'settle|unfilled':>16}")
for lo,hi in zip(edges,edges[1:]):
    g=[x for x in recs if lo<=x["ask"]<hi]
    if not g: continue
    f=[x for x in g if x["filled"]]; u=[x for x in g if not x["filled"]]
    print(f"{lo:.2f}-{hi:.2f}".rjust(18)+f" {len(g):4d} {100*len(f)/len(g):5.0f}% {sum(x['claim'] for x in g)/len(g):6.3f} {100*sum(x['hit'] for x in g)/len(g):7.1f}%"
          +f" {(str(sum(x['hit'] for x in f))+'/'+str(len(f))):>14} {(str(sum(x['hit'] for x in u))+'/'+str(len(u))):>16}")
# claim-vs-ask discrimination on the two sides of the centre, per row
import collections
pairs=collections.defaultdict(dict)
for x in recs: pairs[(x["st"],x["ed"],x["stage"])][x["b"]]=x
both=0; claim_pick=0; price_pick=0; claim_right=0; price_right=0; winner_exists=0
for k,d in pairs.items():
    if len(d)!=2: continue
    (b1,x1),(b2,x2)=list(d.items())
    if x1["hit"]==x2["hit"]: continue
    winner_exists+=1
    w=b1 if x1["hit"] else b2
    hi_claim=b1 if x1["claim"]>=x2["claim"] else b2
    hi_ask=b1 if x1["ask"]>=x2["ask"] else b2
    claim_right+= (hi_claim==w); price_right+= (hi_ask==w)
print(f"\nrows with exactly one of the two shoulders settling: n={winner_exists}; model's higher-claim side right {claim_right} ({100*claim_right/max(winner_exists,1):.0f}%); market's higher-ask side right {price_right} ({100*price_right/max(winner_exists,1):.0f}%)")
# example station-days for the plain-language section
ex=[x for x in recs if x["hit"] and not x["filled"]]
ex.sort(key=lambda x:-x["ask"])
print("\nexamples: shoulder that WON but never filled (highest decision ask first)")
for x in ex[:6]:
    print(f"  {x['st']} {x['ed']} {x['stage']} centre={x['centre']} bucket={x['b']} claim={x['claim']:.3f} ask={x['ask']:.3f} settled={x['sb']} tmax={x['tmax']}")
ex2=[x for x in recs if x["filled"] and not x["hit"]]
ex2.sort(key=lambda x: x["fpx"] or 9)
print("\nexamples: shoulder that FILLED cheap and lost")
for x in ex2[:6]:
    print(f"  {x['st']} {x['ed']} {x['stage']} centre={x['centre']} bucket={x['b']} claim={x['claim']:.3f} ask={x['ask']:.3f} fill={x['fpx']:.4f} settled={x['sb']} tmax={x['tmax']}")

print("\n=== counterfactual: pay the decision-time ask on every posted shoulder rung (equal stake) ===")
for lo,hi in zip(edges,edges[1:]):
    g=[x for x in recs if lo<=x["ask"]<hi]
    if not g: continue
    a=sum(x["ask"] for x in g)/len(g); h=sum(x["hit"] for x in g)/len(g)
    print(f"  ask {lo:.2f}-{hi:.2f}: n={len(g):3d} mean_ask={a:.3f} settle={100*h:5.1f}%  EV/share={h-a:+.3f}  ROI={100*(h-a)/a:+6.1f}%")
g=recs; a=sum(x["ask"] for x in g)/len(g); h=sum(x["hit"] for x in g)/len(g)
print(f"  ALL       : n={len(g):3d} mean_ask={a:.3f} settle={100*h:5.1f}%  EV/share={h-a:+.3f}  ROI={100*(h-a)/a:+6.1f}%")
print("\n=== value cap (0.4*claim) vs decision ask -> fill rate ===")
import statistics
for lo,hi in [(0,0.5),(0.5,1.0),(1.0,2.0),(2.0,99)]:
    g=[x for x in recs if x["ask"]>0 and lo<=(0.4*x["claim"])/x["ask"]<hi]
    if not g: continue
    print(f"  cap/ask {lo}-{hi}: n={len(g):3d} fill={100*sum(x['filled'] for x in g)/len(g):5.0f}%  settle={100*sum(x['hit'] for x in g)/len(g):5.1f}%")
