from common import *
c=connect('archive.db')
tables=query(c,"select name from sqlite_master where type='table' and (name like 'reported_trajectory_%' or name like '%source_poll%') order by name")
schema={}
for x in tables:
    name=x['name'];schema[name]={'columns':query(c,f'pragma table_info("{name}")'),'indexes':query(c,f'pragma index_list("{name}")')}
save('archive_schema.json',schema)
for name,s in schema.items():
    if any(k in name for k in ['observations','canonical_station_day','coverage','poll','diagnostic']):
        print(name,','.join(x['name'] for x in s['columns']))
t=connect()
stations=query(t,'select distinct station_id,settlement_timezone from calibration_station_market_timezone_binding')
stations=[x for x in stations if x['station_id'] not in {'LTFM','UUWW','LLBG'}]
assert len(stations)==45
save('stations.json',stations)
samples=[]
for station in ['ZGSZ','ZUUU','KDAL','EGLC']:
    rows=query(t,"select * from basket_probability_envelope where station_id=? and stage='P1' and event_date='2026-09-11' order by decision_at_utc desc limit 1",(station,))
    for r in rows:
        r['lineage_json']=json.loads(r['lineage_json']);samples.append(r)
        print('SAMPLE',station,r['decision_at_utc'],r['posterior_version'],r['state'])
        print('LINEAGE_KEYS',station,','.join(r['lineage_json']))
save('initial_envelopes.json',samples)
artifacts={}
for name in ['metar_phase_hazard_artifact.json','metar_ceiling_artifact.json','metar_reanchor_artifact.json']:
    p=ROOT/'data/runtime/bayes_basket_target_nwp'/name
    obj=json.loads(p.read_text());artifacts[name]=obj
    print('ARTIFACT',name,json.dumps({k:v for k,v in obj.items() if k not in ['bins','phase','continuation_full','continuation_phase_only','cells']})[:2400])
save('artifacts_snapshot.json',artifacts)
