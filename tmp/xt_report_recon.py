import sqlite3, json, sys, math, re
sys.path.insert(0, "/Users/xiong/weather")
from scripts import paper_trading_ledger as ptl

DB="file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro"
c=sqlite3.connect(DB,uri=True,timeout=60); c.row_factory=sqlite3.Row

rows=c.execute("""
 select p.*, s.settled_bucket, s.settled_tmax_c, s.hit as row_hit
 from paper_trade_position p join paper_trade_settlement s on s.station_id=p.station_id and s.event_date=p.event_date
""").fetchall()
print("joined position rows:", len(rows), " positions:", c.execute("select count(*) from paper_trade_position").fetchone()[0],
      " settlements:", c.execute("select count(*) from paper_trade_settlement").fetchone()[0])
print("FEE_RATE", ptl.FEE_RATE, "SHOULDER_LAMBDA", repr(ptl.MAKER_RESTING_SHOULDER_LAMBDA), "TAIL_LAMBDA", repr(ptl.MAKER_RESTING_TAIL_LAMBDA), "EXEC", ptl.EXECUTION_MODE)

FILL=re.compile(r"^(\d+)/(\d+)@([0-9.]+)$")
def band(b, centre, posted):
    if centre is not None and str(b)==str(centre): return "centre"
    if ptl._is_shoulder(b, centre, posted): return "shoulder"
    return "rest"

def J(x):
    return json.loads(x) if x else {}

bought={}; posted_all={}
partial_rows=0; partial_examples=[]
for r in rows:
    centre=r["ladder_center_bucket"]; posted=J(r["posted_buckets_json"]) or None
    legs=J(r["leg_shares_json"]); intended=J(r["intended_legs_json"]); fills=J(r["resting_fill_json"])
    sb=r["settled_bucket"]
    for b,txt in fills.items():
        m=FILL.match(str(txt))
        if not m: continue
        got,tot,px=int(m.group(1)),int(m.group(2)),float(m.group(3))
        if 0<got<tot:
            partial_rows+=1
            if len(partial_examples)<4: partial_examples.append((r["station_id"],r["event_date"],r["stage"],b,txt,legs.get(b),intended.get(b)))
    # posted set = rungs actually offered with a non-zero intent
    for b,txt in fills.items():
        m=FILL.match(str(txt))
        if not m: continue
        got,tot,px=int(m.group(1)),int(m.group(2)),float(m.group(3))
        inten=float(intended.get(b,0.0) or 0.0)
        sh=float(legs.get(b,0.0) or 0.0)
        if inten<=0 and sh<=0:   # top-up with nothing to buy: not a real order this stage
            continue
        k=band(b,centre,posted)
        d=posted_all.setdefault(k,{"n":0,"hit":0,"fn":0,"fhit":0,"un":0,"uhit":0})
        d["n"]+=1; hit = 1 if (sb is not None and str(b)==str(sb)) else 0
        d["hit"]+=hit
        if got>0 and sh>0: d["fn"]+=1; d["fhit"]+=hit
        else: d["un"]+=1; d["uhit"]+=hit
    for b,sh in legs.items():
        sh=float(sh or 0.0)
        if sh<=0: continue
        m=FILL.match(str(fills.get(b,"")))
        px=float(m.group(3)) if m else None
        if px is None: continue
        k=band(b,centre,posted)
        d=bought.setdefault(k,{"legs":0,"sh":0.0,"cost":0.0,"hit":0,"hitsh":0.0,"payout":0.0,"pxs":[]})
        d["legs"]+=1; d["sh"]+=sh; d["cost"]+=sh*px*(1.0+ptl.FEE_RATE); d["pxs"].append(px)
        if sb is not None and str(b)==str(sb):
            d["hit"]+=1; d["hitsh"]+=sh; d["payout"]+=sh

def wilson(k,n):
    if n==0: return (0,0)
    z=1.96; p=k/n; d=1+z*z/n
    ctr=(p+z*z/(2*n))/d; half=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/d
    return (100*(ctr-half),100*(ctr+half))

print("\n=== BOUGHT legs (leg_shares_json>0) ===")
for k in ("centre","shoulder","rest"):
    d=bought.get(k)
    if not d: continue
    wp=d["cost"]/d["sh"]; hs=d["hitsh"]/d["sh"]; net=d["payout"]-d["cost"]
    lo,hi=wilson(d["hit"],d["legs"])
    print(f"{k:9s} legs={d['legs']:4d} shares={d['sh']:9.0f} wavg_px={wp:.4f} legHit={100*d['hit']/d['legs']:5.1f}% [{lo:.1f},{hi:.1f}] shareHit={100*hs:5.1f}% net={net:+9.1f}u roi={100*net/d['cost']:+7.1f}%")

print("\n=== POSTED rungs (real orders this stage): all / filled / unfilled ===")
for k in ("centre","shoulder","rest"):
    d=posted_all.get(k)
    if not d: continue
    lo,hi=wilson(d["hit"],d["n"])
    print(f"{k:9s} posted n={d['n']:4d} hit={100*d['hit']/d['n']:5.1f}% [{lo:.1f},{hi:.1f}] | filled n={d['fn']:4d} hit={100*d['fhit']/max(d['fn'],1):5.1f}% | unfilled n={d['un']:4d} hit={100*d['uhit']/max(d['un'],1):5.1f}%")

print("\npartial-fill rungs (0<got<tot):",partial_rows, partial_examples[:4])
