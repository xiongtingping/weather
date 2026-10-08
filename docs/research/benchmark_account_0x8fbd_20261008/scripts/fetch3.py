import json, subprocess, time, sys, datetime as dt, collections
USER="0x8fbd7cf5f806f563080864694415829f7229a959"
def get(url):
    for i in range(8):
        r=subprocess.run(["curl","-s","-m","40",url],capture_output=True,text=True)
        try: j=json.loads(r.stdout)
        except Exception: time.sleep(2+i); continue
        if isinstance(j,dict) and "Too Many" in str(j.get("error")): time.sleep(8+8*i); continue
        return j
    return {"error":"exhausted","url":url}
w=json.load(open("window.json"))
FLOOR=1790000000
end=min(a["timestamp"] for a in w["activity"])
rows=[]
while True:
    off=0; oldest=None; got=0
    while off<=4500:
        page=get(f"https://data-api.polymarket.com/activity?user={USER}&limit=500&offset={off}&end={end}")
        if not isinstance(page,list): print("STOP",off,end,str(page)[:100],file=sys.stderr); break
        rows+=page; got+=len(page)
        if page: oldest=min(oldest or 10**12, min(p["timestamp"] for p in page))
        if len(page)<500: break
        off+=500; time.sleep(0.5)
    print(" window end",end,"got",got,"oldest",oldest,file=sys.stderr)
    if not oldest or oldest<=FLOOR or got<500: break
    end=oldest; time.sleep(0.5)
def key(t): return (t["transactionHash"],t["asset"],t["side"],str(t["price"]),str(t["size"]),t["timestamp"],t.get("type","TRADE"))
acts={key(a):a for a in w["activity"]}
for a in rows:
    if a["timestamp"]>=FLOOR: acts.setdefault(key(a),a)
w["activity"]=list(acts.values())
print("activity unique",len(w["activity"]),"range",dt.datetime.fromtimestamp(min(t['timestamp'] for t in w['activity']),dt.timezone.utc),"->",dt.datetime.fromtimestamp(max(t['timestamp'] for t in w['activity']),dt.timezone.utc),file=sys.stderr)
print(collections.Counter(a["type"] for a in w["activity"]),file=sys.stderr)
json.dump(w,open("window.json","w"))
