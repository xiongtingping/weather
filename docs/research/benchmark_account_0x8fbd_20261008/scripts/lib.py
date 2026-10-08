import json, re, datetime as dt, collections
CITY2ST={'amsterdam':'EHAM','ankara':'LTAC','atlanta':'KATL','austin':'KAUS','beijing':'ZBAA','buenos-aires':'SAEZ','busan':'RKPK','cape-town':'FACT','chengdu':'ZUUU','chicago':'KORD','chongqing':'ZUCK','dallas':'KDAL','denver':'KBKF','guangzhou':'ZGGG','helsinki':'EFHK','houston':'KHOU','istanbul':'LTFM','jeddah':'OEJN','karachi':'OPKC','kuala-lumpur':'WMKK','london':'EGLC','los-angeles':'KLAX','lucknow':'VILK','madrid':'LEMD','manila':'RPLL','mexico-city':'MMMX','miami':'KMIA','milan':'LIMC','moscow':'UUWW','munich':'EDDM','nyc':'KLGA','panama-city':'MPMG','paris':'LFPB','qingdao':'ZSQD','san-francisco':'KSFO','sao-paulo':'SBGR','seattle':'KSEA','seoul':'RKSI','shanghai':'ZSPD','shenzhen':'ZGSZ','singapore':'WSSS','taipei':'RCSS','tel-aviv':'LLBG','tokyo':'RJTT','toronto':'CYYZ','warsaw':'EPWA','wellington':'NZWN','wuhan':'ZHHH'}
MON={m:i for i,m in enumerate(['january','february','march','april','may','june','july','august','september','october','november','december'],1)}
def parse_event(eslug):
    # highest-temperature-in-<city>-on-<month>-<day>-<year>
    m=re.match(r"highest-temperature-in-(.+)-on-([a-z]+)-(\d+)-(\d{4})$",eslug)
    if not m: return None,None
    city,mon,day,yr=m.groups()
    return city, f"{int(yr):04d}-{MON[mon]:02d}-{int(day):02d}"
def parse_bucket(title):
    # returns (label, lower, upper, unit, kind)
    t=title
    m=re.search(r"be between (\-?\d+)-(\-?\d+)°F",t)
    if m: lo,hi=int(m.group(1)),int(m.group(2)); return f"{lo}-{hi}°F",lo,hi,'F','interior'
    m=re.search(r"be (\-?\d+)°F or higher",t)
    if m: lo=int(m.group(1)); return f"{lo}°F or higher",lo,None,'F','upper_tail'
    m=re.search(r"be (\-?\d+)°F or below",t)
    if m: hi=int(m.group(1)); return f"{hi}°F or below",None,hi,'F','lower_tail'
    m=re.search(r"be (\-?\d+)°C or higher",t)
    if m: lo=int(m.group(1)); return f"{lo}°C or higher",lo,None,'C','upper_tail'
    m=re.search(r"be (\-?\d+)°C or below",t)
    if m: hi=int(m.group(1)); return f"{hi}°C or below",None,hi,'C','lower_tail'
    m=re.search(r"be (\-?\d+)°C",t)
    if m: v=int(m.group(1)); return f"{v}°C",v,v,'C','interior'
    return None,None,None,None,None
def bucket_centre(lo,hi,unit):
    if lo is None: return hi-0.5 if unit=='F' else hi-0.5
    if hi is None: return lo+0.5
    return (lo+hi)/2 if unit=='F' else lo
def load():
    w=json.load(open(__file__.rsplit('/',1)[0]+"/window.json"))
    taker_keys=collections.Counter((t["transactionHash"],t["asset"],t["side"],str(t["price"]),str(t["size"]),t["timestamp"]) for t in w["taker"])
    fills=[]
    for t in w["fills"]:
        k=(t["transactionHash"],t["asset"],t["side"],str(t["price"]),str(t["size"]),t["timestamp"])
        is_taker=False
        if taker_keys.get(k,0)>0:
            taker_keys[k]-=1; is_taker=True
        city,ed=parse_event(t["eventSlug"])
        label,lo,hi,unit,kind=parse_bucket(t["title"])
        fills.append(dict(ts=t["timestamp"],utc=dt.datetime.fromtimestamp(t["timestamp"],dt.timezone.utc),city=city,station=CITY2ST.get(city),event_date=ed,
            bucket=label,lo=lo,hi=hi,unit=unit,kind=kind,outcome=t["outcome"],side=t["side"],price=float(t["price"]),size=float(t["size"]),
            asset=t["asset"],cid=t["conditionId"],tx=t["transactionHash"],taker=is_taker,title=t["title"]))
    fills.sort(key=lambda f:f["ts"])
    acts=[]
    for a in w["activity"]:
        city,ed=parse_event(a.get("eventSlug") or "")
        acts.append(dict(ts=a["timestamp"],utc=dt.datetime.fromtimestamp(a["timestamp"],dt.timezone.utc),type=a["type"],city=city,station=CITY2ST.get(city),event_date=ed,
            outcome=a.get("outcome"),side=a.get("side"),price=float(a.get("price") or 0),size=float(a.get("size") or 0),usdc=float(a.get("usdcSize") or 0),asset=a.get("asset"),cid=a.get("conditionId"),title=a.get("title"),tx=a["transactionHash"]))
    acts.sort(key=lambda f:f["ts"])
    return fills,acts
