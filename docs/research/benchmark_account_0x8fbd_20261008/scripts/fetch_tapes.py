import json, subprocess, time, sys, collections, random
sys.path.insert(0,'.')
from lib import *
fills,_=load()
byc=collections.Counter(f["cid"] for f in fills if f["side"]=="BUY")
# sample: markets with >=12 of their buy fills, spread across event dates 09-24..10-06, up to 70
cands=[c for c,n in byc.items() if n>=12]
meta={f["cid"]:f for f in fills}
random.seed(4); random.shuffle(cands)
bydate=collections.defaultdict(list)
for c in cands:
    ed=meta[c]["event_date"]
    if ed and "2026-09-24"<=ed<="2026-10-06": bydate[ed].append(c)
sample=[]
for ed in sorted(bydate):
    sample+=bydate[ed][:6]
sample=sample[:70]
print("markets sampled",len(sample),file=sys.stderr)
def get(url):
    for i in range(6):
        r=subprocess.run(["curl","-s","-m","40",url],capture_output=True,text=True)
        try: j=json.loads(r.stdout)
        except Exception: time.sleep(2+i); continue
        if isinstance(j,dict) and "Too Many" in str(j.get("error")): time.sleep(6+6*i); continue
        return j
    return None
tapes={}; t0=time.time()
for i,cid in enumerate(sample):
    rows=[]; end=None
    while True:
        off=0; got=[]
        while off<=9500:
            url=f"https://data-api.polymarket.com/trades?market={cid}&takerOnly=false&limit=500&offset={off}"+(f"&end={end}" if end else "")
            page=get(url)
            if not isinstance(page,list): break
            got+=page
            if len(page)<500: break
            off+=500; time.sleep(0.3)
        rows+=got
        if not got or len(got)<500 or off<=9500 and len(got)<500: break
        oldest=min(t["timestamp"] for t in got)
        if end is not None and oldest>=end: break
        end=oldest; time.sleep(0.3)
    # dedupe
    seen=set(); uniq=[]
    for t in rows:
        k=(t["transactionHash"],t["proxyWallet"],t["asset"],t["side"],str(t["price"]),str(t["size"]),t["timestamp"])
        if k in seen: continue
        seen.add(k); uniq.append({k2:t.get(k2) for k2 in ("proxyWallet","side","asset","outcome","price","size","timestamp","transactionHash","name")})
    tapes[cid]=uniq
    if i%10==0: print(i,len(uniq),round(time.time()-t0),"s",file=sys.stderr); json.dump(tapes,open("tapes.json","w"))
    time.sleep(0.3)
json.dump(tapes,open("tapes.json","w"))
print("done",len(tapes),"markets, rows",sum(len(v) for v in tapes.values()),round(time.time()-t0),"s",file=sys.stderr)
