"""Per first-taker cluster event: the whole ladder at that instant (bid/ask/depth per bucket, hot
SSOT or cold tier), our served vector, the station's observations and yesterday's truth -> a choice
set of (bucket, side) alternatives with the one they took marked."""
import json, gzip, sqlite3, bisect, time, collections, sys
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
sys.path.insert(0,'/Users/xiong/weather')
from bayes_basket_target.materialization import floor_probabilities
t0=time.time()
E=json.load(open('events_enriched.json')); reg=json.load(open('registry.json')); ob=json.load(open('obs_temps.json'))
tz={r['station_id']:r['settlement_timezone'] for r in reg}
ft=[e for e in E if e['first_taker'] and e.get('truth_ord') is not None]
hot=sqlite3.connect('file:/Users/xiong/weather/data/bayes_basket_target_ssot.db?mode=ro',uri=True,timeout=30); hot.execute('PRAGMA query_only=1')
cold=sqlite3.connect('file:/Volumes/weather_data/weather_cold/ssot_rows/basket_official_ws_snapshot.sqlite?mode=ro&immutable=1',uri=True)
labels={}
for st,ed,b,t in hot.execute("select station_id,event_date,settled_bucket,settled_tmax_c from basket_station_day_settlement_label l where truth_revision_no=(select max(truth_revision_no) from basket_station_day_settlement_label x where x.station_id=l.station_id and x.event_date=l.event_date)"):
    labels[(st,ed)]=(b,float(t))
stmean=collections.defaultdict(list)
for (st,ed),(b,t) in labels.items(): stmean[st].append(t)
stmean={st:sum(v)/len(v) for st,v in stmean.items()}
obs=collections.defaultdict(list)
for o in ob:
    try: obs[o['station']].append((datetime.fromisoformat(o['valid'].replace('Z','+00:00')).timestamp(), float(o['t'])))
    except Exception: pass
for st in obs: obs[st].sort()
def iso(ts): return datetime.fromtimestamp(ts,timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
COLD_INDEX={}  # (station, event_date) -> sorted list of (observed_at, rowid); loaded once per event_date
def _load_cold_date(ed):
    if ed in COLD_INDEX: return
    COLD_INDEX[ed]=True
    for st,obs_at,rid in cold.execute("select station_id, observed_at, rowid from basket_official_ws_snapshot where event_date=? and state='COMMITTED'",(ed,)):
        COLD_INDEX.setdefault((st,ed),[]).append((obs_at,rid))
    for k,v in COLD_INDEX.items():
        if isinstance(v,list): v.sort()
def snap(conn, st, ed, ts, lo_s, hi_s):
    hi_iso, lo_iso = iso(ts-lo_s), iso(ts-hi_s)
    if conn is cold:
        _load_cold_date(ed); lst=COLD_INDEX.get((st,ed),[])
        keys=[x[0] for x in lst]; i=bisect.bisect_right(keys,hi_iso)-1
        if i<0 or lst[i][0]<lo_iso: return (None,None)
        row=cold.execute("select buckets_json, observed_at from basket_official_ws_snapshot where rowid=?",(lst[i][1],)).fetchone()
        return (json.loads(row[0]), row[1]) if row else (None,None)
    row=conn.execute("select buckets_json, observed_at from basket_official_ws_snapshot where station_id=? and event_date=? and state='COMMITTED' and observed_at<=? and observed_at>=? order by observed_at desc limit 1",(st,ed,hi_iso,lo_iso)).fetchone()
    return (json.loads(row[0]), row[1]) if row else (None,None)
def served(st,ed,ts):
    h=hot.execute("select history_id,stage from basket_probability_bridge_history where station_id=? and event_date=? and state='COMMITTED' and decision_at_utc<=? order by decision_at_utc desc limit 1",(st,ed,iso(ts))).fetchone()
    if not h: return None,None
    P={b:p for b,p in hot.execute("select bucket,probability from basket_probability_bridge_bucket_probability where history_id=?",(h[0],))}
    s=sum(P.values())
    if s<=0: return None,h[1]
    f,_=floor_probabilities({b:v/s for b,v in P.items()}); z=sum(f.values()); return {b:v/z for b,v in f.items()}, h[1]
out=gzip.open('choice_rows.jsonl.gz','wt'); n=0; miss=collections.Counter()
for e in ft:
    st,ed,ts=e['station'],e['event_date'],e['ts']
    conn=hot if ed>='2026-10-01' else cold
    B,obs_at=snap(conn,st,ed,ts,0,1800)
    if not B: miss['no_book']+=1; continue
    P,stage=served(st,ed,ts)
    if not P: miss['no_p']+=1; continue
    B60,_=snap(conn,st,ed,ts,3600,5400)
    mid60={b['bucket']:((b['bid_price']+b['best_ask'])/2 if (b.get('bid_price') is not None and b.get('best_ask') is not None) else None) for b in (B60 or [])}
    bk=sorted(B,key=lambda b:b['ordinal'])
    two={b['bucket']:(b['bid_price'],b['best_ask']) for b in bk if b.get('bid_price') is not None and b.get('best_ask') is not None and 0<b['best_ask']-b['bid_price']<0.2}
    if len(two)<3: miss['thin_book']+=1; continue
    mids={b:(bid+ask)/2 for b,(bid,ask) in two.items()}; s=sum(mids.values())
    if not 0.85<=s<=1.2: miss['unhealthy']+=1; continue
    q={b:m/s for b,m in mids.items()}
    mode=max(q,key=q.get); ords={b['bucket']:b['ordinal'] for b in bk}
    our_mode=max(P,key=P.get) if all(k in ords for k in P) else max((k for k in P if k in ords),key=P.get)
    # centres in C from bucket intervals in registry rows (lower_c/upper_c); fall back to ordinal
    zone=ZoneInfo(tz.get(st,'UTC')); local=datetime.fromtimestamp(ts,zone); lday=local.date().isoformat()
    O=obs.get(st,[]); i=bisect.bisect_right([x[0] for x in O],ts)
    cur=O[i-1][1] if i>0 and ts-O[i-1][0]<=3*3600 else None
    day0=datetime(local.year,local.month,local.day,tzinfo=zone).timestamp()
    todays=[t for (tt,t) in O[:i] if tt>=day0]; tmax_today=max(todays) if todays else None
    yday=(datetime.fromisoformat(ed)-timedelta(days=1)).date().isoformat(); ytruth=labels.get((st,yday),(None,None))[1]
    truth=labels.get((st,ed)); 
    if not truth: miss['no_truth']+=1; continue
    chosen_bucket=next((b for b,o in ords.items() if o==e['my_ord']),None)
    if chosen_bucket not in two: miss['chosen_not_quoted']+=1; continue
    alts=[]
    for b,(bid,ask) in two.items():
        o=ords[b]; row=next(x for x in bk if x['bucket']==b)
        lower,upper=None,None
        ask_sh=(row.get('ask_levels') or [{}])[0].get('shares'); bid_sh=(row.get('bid_levels') or [{}])[0].get('shares')
        mom=(mids[b]-mid60[b]) if mid60.get(b) is not None else None
        for side in ('Yes','No'):
            price=ask if side=='Yes' else 1-bid
            alts.append(dict(bucket=b,ord=o,side=side,price=price,q=q[b],spread=ask-bid,depth=(ask_sh if side=='Yes' else bid_sh),
                             d_mode=o-ords[mode],d_our=o-ords[our_mode],p=P.get(b,0.0),mom=mom,
                             chosen=int(b==chosen_bucket and side==e['outcome'])))
    n+=1
    out.write(json.dumps(dict(station=st,event_date=ed,ts=ts,lead_h=e['lead_h'],unit=e['unit'],stage=stage,usdc=e['usdc'],wallet=e['first_w'],
        truth_ord=e['truth_ord'],truth_bucket=truth[0],truth_c=truth[1],cur_obs=cur,tmax_today=tmax_today,ytruth_c=ytruth,st_mean_c=stmean.get(st),
        is_d0=(lday==ed),mode_ord=ords[mode],our_mode_ord=ords[our_mode],book_at=obs_at,alts=alts),ensure_ascii=False)+'\n')
    if n%250==0: print(n, dict(miss), round(time.time()-t0),'s', flush=True)
out.close(); print('rows',n,'missing',dict(miss),round(time.time()-t0),'s')
