import json, subprocess, time, sys
part=int(sys.argv[1]); nparts=int(sys.argv[2])
S=json.load(open("cluster_event_sample.json")); reg={r["cid"]:r for r in json.load(open("registry.json"))}
S=[e for i,e in enumerate(S) if i%nparts==part and e["first"]["cid"] in reg]
out=[]; t0=time.time(); fail=0
for i,e in enumerate(S):
    f=e["first"]; tok=reg[f["cid"]]["yes_token"]
    for a in range(5):
        p=subprocess.run(["curl","-s","-m","30",f"https://clob.polymarket.com/prices-history?market={tok}&startTs={f['ts']-3600}&endTs={f['ts']+3600}&fidelity=1"],capture_output=True,text=True)
        try: h=json.loads(p.stdout)["history"]; break
        except Exception: time.sleep(3+3*a); h=None
    if h is None: fail+=1; continue
    e=dict(e); e["hist"]=h; out.append(e)
    if i%50==0: json.dump(out,open(f"event_min_{part}.json","w"))
    time.sleep(0.25)
json.dump(out,open(f"event_min_{part}.json","w")); print("part",part,"done",len(out),"fail",fail,round(time.time()-t0),"s",file=sys.stderr)
