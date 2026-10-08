import json, subprocess, time, sys, pathlib
lo,hi=sys.argv[1],sys.argv[2]
rows=json.load(open("market_pnl.json")); reg={r["cid"]:r for r in json.load(open("registry.json"))}
out_path=pathlib.Path(sys.argv[3] if len(sys.argv)>3 else "hist.json"); out=json.load(open(out_path)) if out_path.exists() else {}
todo=[r for r in rows if r["event_date"] and lo<=r["event_date"]<=hi and r["cid"] in reg and r["cid"] not in out]
print("todo",len(todo),file=sys.stderr); t0=time.time(); fail=0
for i,r in enumerate(todo):
    tok=reg[r["cid"]]["yes_token"]
    for a in range(5):
        p=subprocess.run(["curl","-s","-m","30",f"https://clob.polymarket.com/prices-history?market={tok}&interval=max&fidelity=1"],capture_output=True,text=True)
        try: j=json.loads(p.stdout); h=j["history"]; break
        except Exception: time.sleep(3+3*a); h=None
    if h is None: fail+=1; continue
    out[r["cid"]]=h
    if i%100==0: print(i,round(time.time()-t0),"s",file=sys.stderr); json.dump(out,open(out_path,"w"))
    time.sleep(0.3)
json.dump(out,open(out_path,"w")); print("done",len(out),"fail",fail,round(time.time()-t0),"s",file=sys.stderr)
