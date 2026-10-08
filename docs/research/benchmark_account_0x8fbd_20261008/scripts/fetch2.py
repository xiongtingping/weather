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
def paged_end(base, end_ts, floor_ts, limit=500):
    """walk backwards: fetch with end=end_ts and offsets up to cap, then move end to oldest ts seen."""
    rows=[]; end=end_ts
    while True:
        off=0; oldest=None; got=0
        while off<=9500:
            page=get(f"{base}&limit={limit}&offset={off}&end={end}")
            if not isinstance(page,list):
                print("STOP",off,end,str(page)[:120],file=sys.stderr); return rows
            rows+=page; got+=len(page)
            if page: oldest=min(oldest or 10**12, min(p["timestamp"] for p in page))
            if len(page)<limit: break
            off+=limit; time.sleep(0.5)
        print(" window end",end,"got",got,"oldest",oldest,file=sys.stderr)
        if not oldest or oldest<=floor_ts or got==0 or (off<=9500 and got<limit): break
        if oldest>=end: break
        end=oldest  # inclusive -> dedupe later
        time.sleep(0.5)
    return rows
raw=json.load(open("raw.json"))
FLOOR=1790000000  # 2026-09-21 14:13Z, before the 09-23 restart
# fills: continue from oldest in raw all-view
oldest_all=min(t["timestamp"] for t in raw["trades_all"])
more=paged_end(f"https://data-api.polymarket.com/trades?user={USER}&takerOnly=false", oldest_all, FLOOR)
print("extra fills",len(more),file=sys.stderr)
oldest_act=min(a["timestamp"] for a in raw["activity"])
more_act=paged_end(f"https://data-api.polymarket.com/activity?user={USER}", oldest_act, FLOOR)
print("extra activity",len(more_act),file=sys.stderr)
def key(t): return (t["transactionHash"],t["asset"],t["side"],str(t["price"]),str(t["size"]),t["timestamp"],t.get("type","TRADE"))
fills={}
for t in raw["trades_all"]+more:
    if t["timestamp"]<FLOOR: continue
    fills.setdefault(key(t),[]).append(t)
dups=sum(len(v)-1 for v in fills.values())
allfills=[v[0] for v in fills.values()]
print("fills unique",len(allfills),"dupkeys collapsed",dups,"range",dt.datetime.fromtimestamp(min(t['timestamp'] for t in allfills),dt.timezone.utc),"->",dt.datetime.fromtimestamp(max(t['timestamp'] for t in allfills),dt.timezone.utc),file=sys.stderr)
acts={}
for a in raw["activity"]+more_act:
    if a["timestamp"]<FLOOR: continue
    acts.setdefault(key(a),[]).append(a)
allacts=[v[0] for v in acts.values()]
print("activity unique",len(allacts),"range",dt.datetime.fromtimestamp(min(t['timestamp'] for t in allacts),dt.timezone.utc),"->",dt.datetime.fromtimestamp(max(t['timestamp'] for t in allacts),dt.timezone.utc),file=sys.stderr)
print("activity types",collections.Counter(a["type"] for a in allacts),file=sys.stderr)
taker=[t for t in raw["trades_taker"] if t["timestamp"]>=FLOOR]
json.dump({"fills":allfills,"taker":taker,"activity":allacts},open("window.json","w"))
