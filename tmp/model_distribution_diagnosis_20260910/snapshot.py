"""Read four closed event dates, latest served P0 per station/date/lead."""
import sys,pathlib,sqlite3,json,time,math,collections,threading,os,shutil,resource
import psutil
ROOT=pathlib.Path(__file__).resolve().parents[2]; OUT=pathlib.Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
from bayes_basket_target.center_width_calibration import bucket_centers_from_intervals
START=time.monotonic();proc=psutil.Process()
def guard():
    while True:
        if proc.memory_info().rss>400*1024**2 or shutil.disk_usage(ROOT).free<5*1024**3 or time.monotonic()-START>600: os._exit(73)
        time.sleep(.25)
threading.Thread(target=guard,daemon=True).start()
resource.setrlimit(resource.RLIMIT_FSIZE,(40*1024**2,40*1024**2))
c=sqlite3.connect(f'file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro',uri=True,timeout=2)
c.execute('pragma query_only=on');c.execute('pragma cache_size=-4096');c.execute('pragma temp_store=memory')
deadline=time.monotonic()+20;c.set_progress_handler(lambda:int(time.monotonic()>deadline),10000)
stations=sorted(r[0] for r in c.execute('select distinct station_id from calibration_station_market_timezone_binding') if r[0] not in {'LTFM','UUWW','LLBG'})
dates=['2026-08-28','2026-09-01','2026-09-05','2026-09-08']
rows=[];artifacts={};failures=[]
for st in stations:
    for day in dates:
        deadline=time.monotonic()+12
        try:
            selected=c.execute("select json_extract(lineage_json,'$.lead_bucket'),max(decision_at_utc) from basket_probability_envelope where station_id=? and event_date=? and stage='P0' and state='COMMITTED' and decision_at_utc<='2026-09-09T16:36:32Z' group by json_extract(lineage_json,'$.lead_bucket')",(st,day)).fetchall()
            for lead,ts in selected:
                if lead not in {'h00_12','h12_24','h24_48'}: continue
                r=c.execute("select envelope_id,probabilities_json,bucket_ordinals_json,lineage_json from basket_probability_envelope where station_id=? and event_date=? and decision_at_utc=? and stage='P0' and state='COMMITTED' order by rowid desc limit 1",(st,day,ts)).fetchone()
                eid,p,ordinals,l=r;p=json.loads(p);ordinals=json.loads(ordinals);l=json.loads(l)
                intervals=json.loads(l['bucket_intervals_c_json']);keys=sorted(ordinals,key=ordinals.get)
                centers=bucket_centers_from_intervals(intervals,ordered_keys=keys)
                mu=sum(p[k]*v for k,v in zip(keys,centers));variance=sum(p[k]*(v-mu)**2 for k,v in zip(keys,centers))
                rec={'station':st,'day':day,'lead':lead,'decision_at':ts,'envelope_id':eid,'probabilities':p,'intervals':intervals,
                     'bucket_mean_c':mu,'bucket_sigma_c':math.sqrt(variance),'entropy_nat':-sum(v*math.log(v) for v in p.values() if v>0),
                     'edge_mass':p[keys[0]]+p[keys[-1]], 'model_center_c':float(l['model_center_c'])}
                for label in ['center_width_json','consensus_bridge_json','system_weights_json','system_artifact_shas_json',
                              'native_calibrated_system_centers_c_json','provisional_uncalibrated_system_ids_json',
                              'run_init_utc_by_system_json','band_reliability_json']:
                    rec[label]=json.loads(l[label]) if label in l else None
                rec['cross_artifact_sha']=l.get('native_cross_ensemble_artifact_sha256')
                rows.append(rec)
                for sha in (rec['system_artifact_shas_json'] or {}).values():
                    if sha in artifacts: continue
                    a=c.execute('select ensemble_system_id,station_id,season_bin,lead_bucket,run_cycle,bias_parameters_json,spread_parameters_json,reliability_parameters_json,train_start_date,train_end_date,holdout_end_date from calibration_native_ensemble_system_artifact where content_sha256=?',(sha,)).fetchone()
                    if a:
                        artifacts[sha]=dict(zip(['system','station','season','lead','cycle','bias','spread','reliability','train_start','train_end','holdout_end'],a))
                        for k in ['bias','spread','reliability']: artifacts[sha][k]=json.loads(artifacts[sha][k])
        except Exception as e:failures.append({'station':st,'day':day,'error':str(e)})
    print(st,len(rows),len(artifacts),flush=True)
(OUT/'serving_snapshot.json').write_text(json.dumps({'dates':dates,'rows':rows,'artifacts':artifacts,'failures':failures},ensure_ascii=False))
print('DONE',len(rows),len(artifacts),failures,flush=True)
