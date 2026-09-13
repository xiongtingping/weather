from common import *
c=connect()
names=query(c,"select name from sqlite_master where type='table' order by name",limit=2000)
names=[x['name'] for x in names if any(k in x['name'] for k in ['trajectory','metar','phase_hazard','regional','probability_envelope','daily_high_truth','official_ws','timezone_binding','auto_trade_decisions'])]
result={}
for name in names:
    result[name]={'columns':query(c,f'pragma table_info("{name}")'), 'indexes':query(c,f'pragma index_list("{name}")')}
save('schema.json',result)
print('snapshot',now(),'free_gb',round(shutil.disk_usage(ROOT).free/1024**3,2))
for name,entry in result.items(): print(name,','.join(x['name'] for x in entry['columns']))
