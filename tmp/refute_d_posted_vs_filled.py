"""Decompose the dist=1 'not bought' group: never posted vs posted-but-unfilled."""
import sqlite3, json, importlib.util
from collections import defaultdict
spec = importlib.util.spec_from_file_location("ptl","/Users/xiong/weather/scripts/paper_trading_ledger.py")
ptl = importlib.util.module_from_spec(spec); spec.loader.exec_module(ptl)
c = sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro",uri=True,timeout=60)
setl = {(st,ed):sb for st,ed,sb in c.execute(
   "SELECT station_id,event_date,settled_bucket FROM paper_trade_settlement")}
rows = c.execute("""SELECT station_id,event_date,stage,ladder_center_bucket,probabilities_json,
  prices_json,posted_buckets_json,window_buckets_json,leg_shares_json,intended_legs_json,
  held_before_json,chain_shares_json FROM paper_trade_position""").fetchall()

grp = defaultdict(lambda: dict(n=0,hit=0,claim=0.0,px=0.0,npx=0))
bystage = defaultdict(lambda: defaultdict(lambda: dict(n=0,hit=0)))
for (st,ed,stage,lcb,probj,pricej,postj,winj,legj,intj,heldj,chainj) in rows:
    if (st,ed) not in setl: continue
    settled=setl[(st,ed)]
    probs=json.loads(probj or "{}") or {}; prices=json.loads(pricej or "{}") or {}
    posted=set(json.loads(postj or "[]") or []); window=set(json.loads(winj or "[]") or [])
    intended=set((json.loads(intj or "{}") or {}).keys()) if (intj or "").startswith("{") else set(json.loads(intj or "[]") or [])
    held=set((json.loads(heldj or "{}") or {}).keys())
    legs={b for b,q in (json.loads(legj or "{}") or {}).items() if float(q)>0}
    ladder=list(probs.keys())
    if lcb not in ladder: continue
    ci=ladder.index(lcb)
    for j,b in enumerate(ladder):
        if abs(j-ci)!=1: continue
        if b in legs: cat="filled(bought)"
        elif b in posted: cat="posted,UNfilled"
        elif b in window: cat="window,unposted"
        else: cat="never offered"
        g=grp[cat]; g["n"]+=1; g["hit"]+=(b==settled); g["claim"]+=probs[b]
        if prices.get(b) is not None: g["npx"]+=1; g["px"]+=prices[b]
        s=bystage[stage][cat]; s["n"]+=1; s["hit"]+=(b==settled)

print("dist=1 rungs, decomposed (leg level, %d position rows joined):"%len({(r[0],r[1],r[2]) for r in rows if (r[0],r[1]) in setl}))
print("%-18s %5s %8s %8s %9s"%("category","n","claim","price","realised"))
for k in ("filled(bought)","posted,UNfilled","window,unposted","never offered"):
    g=grp.get(k)
    if not g: continue
    print("%-18s %5d %8.4f %8.4f %9.4f (%d/%d)"%(k,g["n"],g["claim"]/g["n"],
        (g["px"]/g["npx"]) if g["npx"] else float('nan'),g["hit"]/g["n"],g["hit"],g["n"]))
print("\nby stage:")
for stage in ("P0","P0-M","P1"):
    print(" ",stage, {k:"%d/%d=%.3f"%(v["hit"],v["n"],v["hit"]/v["n"]) for k,v in bystage[stage].items()})
