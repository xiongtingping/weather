import json, subprocess, time, random
USER="0x8fbd7cf5f806f563080864694415829f7229a959"
rows=json.load(open("market_pnl.json"))
pure=[r for r in rows if r["resolved"] and r["yes_sell"]==0 and r["no_sell"]==0 and r["merged"]==0]
print("pure hold-to-settle resolved markets:",len(pure),"of",sum(1 for r in rows if r["resolved"]),"| their pnl sum",round(sum(r["pnl"] for r in pure),1))
random.seed(11); sample=random.sample(pure,50)
def get(url):
    for i in range(6):
        r=subprocess.run(["curl","-s","-m","40",url],capture_output=True,text=True)
        try: j=json.loads(r.stdout)
        except Exception: time.sleep(2); continue
        if isinstance(j,dict) and "Too Many" in str(j.get("error")): time.sleep(6+6*i); continue
        return j
    return None
tm=ta=0; n=0; bad=[]
for r in sample:
    j=get(f"https://data-api.polymarket.com/positions?user={USER}&market={r['cid']}&sizeThreshold=0"); time.sleep(0.5)
    if not isinstance(j,list) or not j: print("nopos",r["station"],r["event_date"],r["bucket"],str(j)[:60]); continue
    api=sum(p["cashPnl"] for p in j); n+=1; tm+=r["pnl"]; ta+=api
    if abs(api-r["pnl"])>1: bad.append((round(r["pnl"],1),round(api,1),r["station"],r["event_date"],r["bucket"],[(p["outcome"],round(p["size"],1),p["avgPrice"],p["curPrice"]) for p in j]))
print(f"compared {n} | mine {tm:.1f} | api {ta:.1f}")
print("mismatches >1 USDC:",len(bad))
for b in bad[:8]: print("  ",b)
