"""What the market-lean gate would have refused, priced on the real book.

Every seated ledger position is re-examined at its own decision instant:
the nearest book snapshot at-or-before it gives the lean, the served
probability gives the model mean, and the seat's own window gives the
midpoint.  Seats the gate would have refused are then read out at their
SETTLED P&L -- no simulation, just a partition of money that actually
moved.
"""
import sqlite3, json
from collections import defaultdict
from datetime import datetime

conn=sqlite3.connect("file:data/bayes_basket_target_ssot.db?mode=ro",uri=True,timeout=120)

def centre(name):
    t=str(name).replace("°","").strip()
    for suf,conv in (("F",lambda x:(x-32)*5/9),("C",float)):
        if t.endswith(suf):
            try: return conv(float(t[:-1]))
            except: return None
    try: return float(t)
    except: return None

def implied_mean(buckets):
    pts=[]
    for e in buckets:
        c=centre(e.get("bucket",""))
        if c is None: continue
        ask=e.get("best_ask"); bid=e.get("bid_price")
        p=((float(ask)+float(bid))/2 if ask is not None and bid is not None
           else float(ask) if ask is not None
           else float(bid) if bid is not None else None)
        if p is None: continue
        pts.append((c,max(0.0,min(1.0,p))))
    tot=sum(p for _,p in pts)
    return (sum(c*p for c,p in pts)/tot) if tot>0.2 and len(pts)>=3 else None

def model_mean(probs):
    pts=[(centre(k),float(v)) for k,v in probs.items() if centre(k) is not None and v>0]
    tot=sum(v for _,v in pts)
    return (sum(c*v for c,v in pts)/tot) if tot>0 else None

cols=[c[1] for c in conn.execute("PRAGMA table_info(paper_trade_position)")]
rows=conn.execute("SELECT * FROM paper_trade_position").fetchall()
groups=defaultdict(lambda: {"n":0,"stake":0.0,"pnl":0.0,"hit":0,"settled":0})
skipped=0
for r in rows:
    d=dict(zip(cols,r))
    st,ed,dec=d["station_id"],d["event_date"],d["decision_at_utc"]
    snap=conn.execute("SELECT buckets_json FROM basket_official_ws_snapshot"
      " WHERE station_id=? AND event_date=? AND observed_at<=?"
      " ORDER BY observed_at DESC LIMIT 1",(st,ed,dec)).fetchone()
    env=conn.execute("SELECT probabilities_json FROM basket_probability_envelope"
      " WHERE station_id=? AND event_date=? AND decision_at_utc<=?"
      " ORDER BY decision_at_utc DESC LIMIT 1",(st,ed,dec)).fetchone()
    if not snap or not env: skipped+=1; continue
    mm=implied_mean(json.loads(snap[0] or "[]"))
    mo=model_mean(json.loads(env[0] or "{}"))
    if mm is None or mo is None:
        key="无信号"
    else:
        lean=mm-mo
        window=json.loads(d["window_buckets_json"])
        mids=[centre(b) for b in window if centre(b) is not None]
        wmid=(min(mids)+max(mids))/2 if mids else None
        if wmid is None: skipped+=1; continue
        off=wmid-mo
        if abs(lean)<0.5 or abs(off)<0.5: key="放行(容差内)"
        elif (lean>0)!=(off>0): key="会被拒(逆市场)"
        else: key="放行(顺市场)"
    g=groups[key]
    g["n"]+=1; g["stake"]+=float(d["stake_usdc"] or 0)
    pnl=d.get("pnl_usdc")
    if pnl is not None:
        g["pnl"]+=float(pnl); g["settled"]+=1
        if d.get("hit"): g["hit"]+=1
print(f"席位 {len(rows)} 条，跳过 {skipped}")
print(f"\n{'分组':14s} {'席位':>4s} {'投入':>9s} {'已结盈亏':>9s} {'ROI':>7s} {'命中率':>6s}")
for k in ("会被拒(逆市场)","放行(顺市场)","放行(容差内)","无信号"):
    g=groups.get(k)
    if not g or not g["n"]: continue
    roi=g["pnl"]/g["stake"] if g["stake"] else 0
    hr=g["hit"]/g["settled"] if g["settled"] else 0
    print(f"{k:14s} {g['n']:4d} {g['stake']:9.1f} {g['pnl']:+9.2f} {roi:+7.1%} {hr:6.0%}")
