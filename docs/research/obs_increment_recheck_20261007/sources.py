"""Explicit source selection; a satellite hostname alone is not lineage."""
from __future__ import annotations
import concurrent.futures
import gzip
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import ssl
import threading
import urllib.error
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import certifi
from audit import HERE, guard

CTX=ssl.create_default_context(cafile=certifi.where())
LOCK=threading.Lock()
TOTAL=0
RECEIPTS=[]
CACHE=HERE/'source_cache'
MAX_RESPONSE=2*1024**2

def get(url,label):
    global TOTAL
    guard();CACHE.mkdir(exist_ok=True)
    path=CACHE/(label+'.json.gz')
    if path.exists():
        with gzip.open(path,'rt') as f:cached=json.load(f)
        RECEIPTS.append({k:v for k,v in cached.items() if k!='payload'})
        return cached['payload']
    started=datetime.now(timezone.utc).isoformat(); chunks=[];size=0
    try:
        with urlopen(Request(url,headers={'User-Agent':'weather-physical-increment-research'}),context=CTX,timeout=25) as r:
            while True:
                b=r.read(65536)
                if not b:break
                size+=len(b)
                if size>MAX_RESPONSE:raise RuntimeError('response_cap')
                with LOCK:
                    TOTAL+=len(b)
                    if TOTAL>32*1024**2:raise RuntimeError('total_response_cap')
                chunks.append(b);guard()
        raw=b''.join(chunks)
        try:payload=json.loads(raw)
        except json.JSONDecodeError as e:
            RECEIPTS.append(dict(url=url,label=label,error='invalid_json:'+str(e),response_prefix=raw[:160].decode(errors='replace'),sha256=hashlib.sha256(raw).hexdigest(),bytes=size))
            return None
        receipt=dict(url=url,label=label,fetch_started_at=started,received_at=datetime.now(timezone.utc).isoformat(),sha256=hashlib.sha256(raw).hexdigest(),bytes=size,source_class='aggregator_republisher',historical_received_at_known=False,payload=payload)
        with gzip.open(path,'wt') as f:json.dump(receipt,f)
        RECEIPTS.append({k:v for k,v in receipt.items() if k!='payload'})
        return payload
    except (urllib.error.URLError,TimeoutError) as e:
        RECEIPTS.append(dict(url=url,label=label,error=str(e),fetch_started_at=started))
        return None

def api(station,lat,lon,tz,model,first='2026-09-01',last='2026-10-07'):
    q=dict(latitude=lat,longitude=lon,timezone=tz,start_date=first,end_date=last,hourly='shortwave_radiation')
    if model:q['models']=model
    return get('https://satellite-api.open-meteo.com/v1/archive?'+urlencode(q),station+'_'+(model or 'default')+'_'+first+'_'+last)

def main():
    stations=[('ZUUU',30.5785,103.9471,'Asia/Shanghai'),('EGLC',51.5053,.0553,'Europe/London'),('KLGA',40.7769,-73.874,'America/New_York'),('RJTT',35.5494,139.7798,'Asia/Tokyo')]
    jobs=[]
    for st,lat,lon,tz in stations:
        for model in ['', 'satellite_radiation_seamless', 'jma_jaxa_himawari' if lon>90 else 'eumetsat_lsa_saf_msg']:
            jobs.append((st,lat,lon,tz,model))
    def run(args):
        p=api(*args)
        if not p:return dict(station=args[0],model=args[-1],error=True)
        h=p.get('hourly',{});vals=h.get('shortwave_radiation',[])
        return dict(station=args[0],model=args[-1] or 'default',n=len(vals),non_null=sum(v is not None for v in vals),first=next((t for t,v in zip(h.get('time',[]),vals) if v is not None),None),last=next((t for t,v in reversed(list(zip(h.get('time',[]),vals))) if v is not None),None),unit=p.get('hourly_units'),day7={t:v for t,v in zip(h.get('time',[]),vals) if t.startswith('2026-10-07')})
    with concurrent.futures.ThreadPoolExecutor(3) as pool:results=list(pool.map(run,jobs))
    # Forecast field availability and historical reach, explicitly fixed lead.
    previous=[]
    for first,last in [('2026-09-01','2026-10-07'),('2025-01-01','2025-01-02')]:
        q=dict(latitude=30.5785,longitude=103.9471,timezone='Asia/Shanghai',start_date=first,end_date=last,hourly='temperature_2m_previous_day1,cloud_cover_previous_day1,cloud_cover_low_previous_day1,shortwave_radiation_previous_day1,precipitation_previous_day1')
        p=get('https://previous-runs-api.open-meteo.com/v1/forecast?'+urlencode(q),'ZUUU_previous_'+first+'_'+last)
        previous.append(dict(first=first,last=last,fields={k:dict(n=len(v),non_null=sum(x is not None for x in v)) for k,v in p.get('hourly',{}).items() if k!='time'} if p else {}))
    (HERE/'sources.json').write_text(json.dumps(dict(probes=results,previous_runs=previous,receipts=RECEIPTS),ensure_ascii=False,indent=2))
    print(json.dumps(dict(probes=results,previous_runs=previous),ensure_ascii=False,indent=2),flush=True)

def all_stations():
    import collections
    stations={}
    with gzip.open(HERE/'audited_rows.jsonl.gz','rt') as f:
        for line in f:
            r=json.loads(line);stations[r['station']]=(r['lat'],r['lon'],r['tz'])
    def run(item):
        st,(lat,lon,tz)=item
        model='jma_jaxa_himawari' if lon>=90 else 'eumetsat_lsa_saf_iodc' if lon>=50 else 'dwd_sis_europe_africa_v4' if -20<=lon<50 else 'eumetsat_lsa_saf_msg' if -60<=lon<-20 else None
        if model is None:return dict(station=st,model=None,status='NO_VERIFIED_SATELLITE_SOURCE')
        p=api(st,lat,lon,tz,model)
        if p is None:return dict(station=st,model=model,status='FETCH_FAILED')
        h=p.get('hourly',{});vs=h.get('shortwave_radiation',[])
        n=sum(v is not None for v in vs)
        print(st,model,n,'/',len(vs),flush=True)
        return dict(station=st,model=model,status='HISTORICAL_CANDIDATE_ONLY',hours=len(vs),non_null=n,source_class='aggregator_republisher',source_product='JAXA_HIMAWARI_SWR' if 'jaxa' in model else 'DWD_MTG_SIS' if 'dwd' in model else 'EUMETSAT_LSA_SAF_SWR',historical_arrival_known=False)
    with concurrent.futures.ThreadPoolExecutor(3) as p:result=list(p.map(run,sorted(stations.items())))
    (HERE/'station_sources.json').write_text(json.dumps(dict(stations=result,receipts=RECEIPTS),ensure_ascii=False,indent=2))
    print(collections.Counter(r['status'] for r in result),flush=True)

if __name__=='__main__':
    import sys
    all_stations() if len(sys.argv)>1 and sys.argv[1]=='all' else main()
