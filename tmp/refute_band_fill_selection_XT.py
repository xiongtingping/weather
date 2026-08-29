import json,sqlite3,sys,re,math
sys.path.insert(0,"/Users/xiong/weather")
from scripts.paper_trading_ledger import _is_shoulder,FEE_RATE
con=sqlite3.connect("file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro",uri=True,timeout=60)
sd={(r[0],r[1]):r[2] for r in con.execute("select station_id,event_date,settled_bucket from paper_trade_settlement")}
def wilson(k,n,z=1.96):
    if n==0: return (0,0)
    p=k/n;d=1+z*z/n;c=p+z*z/(2*n);m=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n));return ((c-m)/d,(c+m)/d)
rung=[]
for st,ed,stg,centre,pj,lj,fj,ilj in con.execute(
  "select station_id,event_date,stage,ladder_center_bucket,posted_buckets_json,leg_shares_json,resting_fill_json,intended_legs_json from paper_trade_position"):
    if (st,ed) not in sd: continue
    sb=sd[(st,ed)]; posted=json.loads(pj or "[]"); fills=json.loads(fj or "{}")
    legsh=json.loads(lj or "{}"); intended=json.loads(ilj or "{}")
    for b,f in fills.items():
        m=re.match(r"^(\d+)/(\d+)@([0-9.]+)$",f)
        got,tot,px=int(m.group(1)),int(m.group(2)),float(m.group(3))
        band="centre" if b==centre else ("shoulder" if _is_shoulder(b,centre,posted) else "rest")
        rung.append(dict(st=st,ed=ed,stage=stg,b=b,band=band,got=got,tot=tot,px=px,
                         hit=1 if b==sb else 0, inlegs=b in legsh and float(legsh.get(b,0))>0,
                         intended=float(intended.get(b,0.0))))
print("posted legs (rungs recorded) on settled chains:",len(rung))
print()
print("%-9s %-10s %5s %6s %6s %-18s"%("band","status","n","hits","rate","95%CI"))
for band in ("centre","shoulder","rest"):
    for status,f in (("ALL posted",lambda l:True),("filled",lambda l:l["got"]>0),("unfilled",lambda l:l["got"]==0)):
        ls=[l for l in rung if l["band"]==band and f(l)]
        n=len(ls);k=sum(l["hit"] for l in ls)
        lo,hi=wilson(k,n)
        print("%-9s %-10s %5d %6d %5.1f%% [%.3f,%.3f]"%(band,status,n,k,100*k/n if n else 0,lo,hi))
print()
print("legs present in leg_shares_json but got==0:",sum(1 for l in rung if l["inlegs"] and l["got"]==0))
print("legs with got>0 but absent from leg_shares_json:",sum(1 for l in rung if (not l["inlegs"]) and l["got"]>0))
for l in rung:
    if (not l["inlegs"]) and l["got"]>0:
        pass
byband={}
for l in rung:
    if (not l["inlegs"]) and l["got"]>0: byband[l["band"]]=byband.get(l["band"],0)+1
print("  those by band:",byband)
print("  of those, intended==0 (top-up already held):",sum(1 for l in rung if (not l["inlegs"]) and l["got"]>0 and l["intended"]<=0))
