import json, subprocess, time, sys, random
sys.path.insert(0,'.')
from lib import *
fills,_=load(); reg={r["cid"]:r for r in json.load(open("registry.json"))}
buys=[f for f in fills if f["side"]=="BUY" and f["cid"] in reg and f["station"] and f["event_date"]<="2026-10-07"]
random.seed(21)
tk=random.sample([f for f in buys if f["taker"]],800); mk=random.sample([f for f in buys if not f["taker"]],300)
out=[]; t0=time.time(); fail=0
for i,f in enumerate(tk+mk):
    tok=reg[f["cid"]]["yes_token"]
    for a in range(5):
        p=subprocess.run(["curl","-s","-m","30",f"https://clob.polymarket.com/prices-history?market={tok}&startTs={f['ts']-3600}&endTs={f['ts']+3600}&fidelity=1"],capture_output=True,text=True)
        try: h=json.loads(p.stdout)["history"]; break
        except Exception: time.sleep(3+3*a); h=None
    if h is None: fail+=1; continue
    out.append(dict(ts=f["ts"],price=f["price"],size=f["size"],outcome=f["outcome"],taker=f["taker"],station=f["station"],event_date=f["event_date"],bucket=f["bucket"],lo=f["lo"],hi=f["hi"],unit=f["unit"],cid=f["cid"],hist=h))
    if i%100==0: print(i,round(time.time()-t0),"s",file=sys.stderr); json.dump(out,open("minute_hist.json","w"))
    time.sleep(0.25)
json.dump(out,open("minute_hist.json","w")); print("done",len(out),"fail",fail,round(time.time()-t0),"s",file=sys.stderr)
