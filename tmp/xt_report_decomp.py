import sqlite3, json, sys, math, re
sys.path.insert(0,"/Users/xiong/weather")
from scripts import paper_trading_ledger as ptl
DB="file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro"
c=sqlite3.connect(DB,uri=True,timeout=60); c.row_factory=sqlite3.Row
rows=c.execute("""select p.*, s.settled_bucket from paper_trade_position p
 join paper_trade_settlement s on s.station_id=p.station_id and s.event_date=p.event_date""").fetchall()
FILL=re.compile(r"^(\d+)/(\d+)@([0-9.]+)$")
J=lambda x: json.loads(x) if x else {}
def wilson(k,n):
    if n==0: return (0,0)
    z=1.96;p=k/n;d=1+z*z/n
    ctr=(p+z*z/(2*n))/d;h=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/d
    return (100*(ctr-h),100*(ctr+h))

# --- A. unconditional distance-1 settle rate on THIS book's own rows ---
for label,centre_of in (("ladder_center_bucket", lambda r,pr: r["ladder_center_bucket"]),
                        ("argmax(probabilities_json)", lambda r,pr: max(pr,key=pr.get) if pr else None)):
    n=0; d1=0; d0=0; dm=0; dp=0
    for r in rows:
        pr=J(r["probabilities_json"]); uni=list(pr.keys()); sb=r["settled_bucket"]
        ctr=centre_of(r,pr)
        if not uni or sb not in uni or ctr not in uni: continue
        i=uni.index(ctr); j=uni.index(sb); n+=1
        if j-i==0: d0+=1
        if j-i==-1: dm+=1
        if j-i==1: dp+=1
    d1=dm+dp
    print(f"[A] centre={label:28s} rows={n}  d0={100*d0/n:5.1f}%  d=-1 {100*dm/n:5.1f}%  d=+1 {100*dp/n:5.1f}%  per-side avg {100*d1/2/n:5.1f}%")

# --- B. posted shoulder rungs: filled vs unfilled, claim / decision ask / realised / fill px ---
buckets={"filled":[], "unfilled":[]}
for r in rows:
    centre=r["ladder_center_bucket"]; posted=J(r["posted_buckets_json"]) or None
    pr=J(r["probabilities_json"]); px=J(r["prices_json"]); legs=J(r["leg_shares_json"]); inten=J(r["intended_legs_json"]); fills=J(r["resting_fill_json"])
    sb=r["settled_bucket"]
    for b,txt in fills.items():
        m=FILL.match(str(txt))
        if not m: continue
        got=int(m.group(1)); fpx=float(m.group(3))
        sh=float(legs.get(b,0) or 0); it=float(inten.get(b,0) or 0)
        if it<=0 and sh<=0: continue
        if not ptl._is_shoulder(b,centre,posted): continue
        key="filled" if (got>0 and sh>0) else "unfilled"
        buckets[key].append((float(pr.get(b,0) or 0), float(px.get(b,0) or 0), fpx if got>0 else None, 1 if str(b)==str(sb) else 0))
for k,v in buckets.items():
    n=len(v); claim=sum(x[0] for x in v)/n; ask=sum(x[1] for x in v)/n
    hit=sum(x[3] for x in v); lo,hi=wilson(hit,n)
    fp=[x[2] for x in v if x[2] is not None]
    print(f"[B] shoulder {k:8s} n={n:4d}  mean_claim={claim:.4f}  mean_decision_ask={ask:.4f}  realised={100*hit/n:5.1f}% [{lo:.1f},{hi:.1f}]"+(f"  mean_fill_px={sum(fp)/len(fp):.4f}" if fp else ""))

# Fisher exact filled vs unfilled (shoulder)
from math import comb
a=sum(x[3] for x in buckets["filled"]); b_=len(buckets["filled"])-a
cc=sum(x[3] for x in buckets["unfilled"]); d_=len(buckets["unfilled"])-cc
def fisher_less(a,b_,cc,d_):
    n=a+b_+cc+d_; r1=a+b_; c1=a+cc
    return sum(comb(r1,i)*comb(n-r1,c1-i) for i in range(0,a+1) if 0<=c1-i<=n-r1)/comb(n,c1)
print(f"[B] shoulder filled {a}/{a+b_} vs unfilled {cc}/{cc+d_}  Fisher one-sided p={fisher_less(a,b_,cc,d_):.3g}")

# --- C. decomposition in percentage points ---
print("[C] gap decomposition (per-rung settle rate): bucket-level(this book, per-side)  -> posted shoulder -> filled shoulder")
