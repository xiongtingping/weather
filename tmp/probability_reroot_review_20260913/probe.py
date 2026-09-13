from common import *
c=connect()
tables=['basket_probability_envelope','basket_probability_bridge_latest','calibration_station_market_timezone_binding']
schema={t:query(c,f'pragma table_info({t})',limit=100) for t in tables}
save('schema.json',schema)
stations=query(c,'select distinct station_id,settlement_timezone from calibration_station_market_timezone_binding',limit=100)
save('stations.json',stations)
domains=[]
for s in stations:
    domains+=query(c,"select distinct station_id,stage,state,posterior_version from basket_probability_envelope where station_id=? and event_date='2026-09-12'",(s['station_id'],),limit=100)
save('domains.json',domains)
print('domains',len(domains),sorted(set((r['stage'],r['state'],r['posterior_version']) for r in domains)))
print('disk_free_gb',shutil.disk_usage(ROOT).free/1024**3,'time',now())
for stage in ['P0','P0-M','P1']:
    rows=query(c,"select * from basket_probability_envelope where station_id='ZBAA' and event_date='2026-09-12' and stage=? and state='COMMITTED' order by decision_at_utc desc limit 1",(stage,),limit=1)
    for r in rows:
        r['lineage_json']=json.loads(r['lineage_json'])
        save('example_'+stage+'.json',r)
        print(stage,{k:v for k,v in r.items() if k not in ('lineage_json','probabilities_json','bucket_ordinals_json')})
        print('keys',list(r['lineage_json']))
