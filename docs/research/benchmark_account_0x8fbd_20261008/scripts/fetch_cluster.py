import json, subprocess, time, sys, collections
T=json.load(open("tapes.json"))
PREF=["0x6fdd2bda","0x30200b89","0x7d640ebd","0xf4d89f7e","0x5bae7577","0x0e0e8174","0x7076cb96"]
full={}
for rows in T.values():
    for r in rows:
        w=r["proxyWallet"].lower()
        for p in PREF:
            if w.startswith(p): full[p]=w
print("cluster wallets:",full,file=sys.stderr)
def get(url):
    for i in range(7):
        r=subprocess.run(["curl","-s","-m","40",url],capture_output=True,text=True)
        try: j=json.loads(r.stdout)
        except Exception: time.sleep(2+i); continue
        if isinstance(j,dict) and "Too Many" in str(j.get("error")): time.sleep(8+8*i); continue
        return j
    return None
FLOOR=1790000000
def pull(user,taker_only):
    rows=[]; end=None
    while True:
        off=0; got=[]
        while off<=9500:
            url=f"https://data-api.polymarket.com/trades?user={user}&takerOnly={'true' if taker_only else 'false'}&limit=500&offset={off}"+(f"&end={end}" if end else "")
            page=get(url)
            if not isinstance(page,list): break
            got+=page
            if len(page)<500: break
            off+=500; time.sleep(0.4)
        rows+=got
        if not got: break
        oldest=min(t["timestamp"] for t in got)
        if oldest<FLOOR or len(got)<500: break
        if end is not None and oldest>=end: break
        end=oldest; time.sleep(0.4)
    seen=set(); out=[]
    for t in rows:
        if t["timestamp"]<FLOOR: continue
        k=(t["transactionHash"],t["asset"],t["side"],str(t["price"]),str(t["size"]),t["timestamp"])
        if k in seen: continue
        seen.add(k); out.append({k2:t.get(k2) for k2 in ("side","asset","conditionId","outcome","price","size","timestamp","transactionHash","title","eventSlug")})
    return out
out={}; t0=time.time()
for p,w in full.items():
    allf=pull(w,False); tk=pull(w,True)
    act=[]; end=None
    # activity for merges/redeems (cap 5000 offsets)
    while True:
        off=0; got=[]
        while off<=4500:
            page=get(f"https://data-api.polymarket.com/activity?user={w}&limit=500&offset={off}"+(f"&end={end}" if end else ""))
            if not isinstance(page,list): break
            got+=page
            if len(page)<500: break
            off+=500; time.sleep(0.4)
        act+=[a for a in got if a["timestamp"]>=FLOOR and a["type"]!="TRADE"]
        if not got or min(t["timestamp"] for t in got)<FLOOR or len(got)<500: break
        end=min(t["timestamp"] for t in got); time.sleep(0.4)
    out[w]={"fills":allf,"taker":tk,"activity":act}
    print(w[:10],"fills",len(allf),"taker",len(tk),"non-trade activity",len(act),round(time.time()-t0),"s",file=sys.stderr)
    json.dump(out,open("cluster.json","w"))
print("done",file=sys.stderr)
