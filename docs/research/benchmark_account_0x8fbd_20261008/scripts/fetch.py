import json, subprocess, time, sys, pathlib
USER="0x8fbd7cf5f806f563080864694415829f7229a959"
HERE=pathlib.Path(__file__).resolve().parent
def get(url):
    for attempt in range(6):
        r=subprocess.run(["curl","-s","-m","40",url],capture_output=True,text=True)
        try:
            page=json.loads(r.stdout)
        except Exception:
            time.sleep(2+2*attempt); continue
        if isinstance(page,dict) and ("Too Many" in str(page.get("error")) or page.get("error")):
            if "Too Many" in str(page.get("error")):
                time.sleep(8+8*attempt); continue
            return page
        return page
    return {"error":"retries exhausted","url":url}
def paged(base, limit=500, maxpages=200):
    rows=[]; off=0
    for _ in range(maxpages):
        page=get(f"{base}&limit={limit}&offset={off}")
        if not isinstance(page,list):
            print("STOP",base,off,str(page)[:200],file=sys.stderr); break
        rows+=page
        if len(page)<limit: break
        off+=limit; time.sleep(0.25)
    return rows
out={}
out["trades_all"]=paged(f"https://data-api.polymarket.com/trades?user={USER}&takerOnly=false")
print("trades_all",len(out["trades_all"]),file=sys.stderr)
out["trades_taker"]=paged(f"https://data-api.polymarket.com/trades?user={USER}&takerOnly=true")
print("trades_taker",len(out["trades_taker"]),file=sys.stderr)
out["activity"]=paged(f"https://data-api.polymarket.com/activity?user={USER}")
print("activity",len(out["activity"]),file=sys.stderr)
out["positions"]=get(f"https://data-api.polymarket.com/positions?user={USER}&limit=500&sizeThreshold=0")
print("positions",len(out["positions"]) if isinstance(out["positions"],list) else out["positions"],file=sys.stderr)
out["closed"]=get(f"https://data-api.polymarket.com/closed-positions?user={USER}&limit=500")
print("closed",len(out["closed"]) if isinstance(out["closed"],list) else str(out["closed"])[:200],file=sys.stderr)
out["value"]=get(f"https://data-api.polymarket.com/value?user={USER}")
print("value",out["value"],file=sys.stderr)
json.dump(out,open(HERE/"raw.json","w"))
