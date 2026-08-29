import sqlite3,json,sys,re,collections
sys.path.insert(0,"/Users/xiong/weather")
from scripts import paper_trading_ledger as ptl
c=sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro",uri=True,timeout=60); c.row_factory=sqlite3.Row
rows=c.execute("""select p.*, s.settled_bucket, s.settled_tmax_c from paper_trade_position p
 join paper_trade_settlement s on s.station_id=p.station_id and s.event_date=p.event_date""").fetchall()
FILL=re.compile(r"^(\d+)/(\d+)@([0-9.]+)$"); J=lambda x: json.loads(x) if x else {}
for r in rows:
    centre=r["ladder_center_bucket"]; posted=J(r["posted_buckets_json"]) or None
    pr=J(r["probabilities_json"]); px=J(r["prices_json"]); legs=J(r["leg_shares_json"]); fills=J(r["resting_fill_json"])
    sh_b=[b for b in (posted or []) if ptl._is_shoulder(b,centre,posted)]
    if len(sh_b)!=2: continue
    info={}
    for b in sh_b:
        m=FILL.match(str(fills.get(b,"")))
        info[b]=dict(claim=pr.get(b), ask=px.get(b), fill=float(m.group(3)) if m and int(m.group(1))>0 else None,
                     sh=float(legs.get(b,0) or 0), hit=str(b)==str(r["settled_bucket"]))
    won=[b for b in sh_b if info[b]["hit"]]
    if not won: continue
    b_w=won[0]; b_l=[b for b in sh_b if b!=b_w][0]
    if info[b_w]["sh"]>0 or info[b_l]["sh"]<=0: continue
    print(f"{r['station_id']} {r['event_date']} {r['stage']} centre={centre} settled={r['settled_bucket']} tmax={r['settled_tmax_c']}")
    for tag,b in (("WON  ",b_w),("BOUGHT",b_l)):
        i=info[b]; print(f"   {tag} {b:>12s} claim={i['claim']:.3f} ask={i['ask']:.3f} cap={0.4*i['claim']:.3f} fill={i['fill']} shares={i['sh']:.0f}")
