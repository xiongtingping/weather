from common import *
from zoneinfo import ZoneInfo
from collections import Counter
c=connect();a=connect('archive.db')
stations=json.loads((OUT/'stations.json').read_text())
dates=['2026-09-09','2026-09-10','2026-09-11']
envelopes=[];observations=[];truths=[];bounds=[];domains={};failures=[]
sql_env="select * from basket_probability_envelope where station_id=? and event_date=? and state='COMMITTED' and stage='P1' and decision_at_utc<=? order by decision_at_utc desc limit 1"
sql_obs="select * from reported_trajectory_observations where station_id=? and valid_at>=? and valid_at<? order by valid_at,canonical_committed_at"
def stamp(d):return d.astimezone(datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
for station in stations:
    sid=station['station_id'];zone=ZoneInfo(station['settlement_timezone'])
    for day in dates:
        try:
            start=datetime.datetime.fromisoformat(day).replace(tzinfo=zone);end=start+datetime.timedelta(days=1)
            obs=query(a,sql_obs,(sid,stamp(start),stamp(end)),limit=1800)
            for x in obs:x['event_date']=day
            observations+=obs
            domains[f'{sid}/{day}']=query(c,"select distinct stage,state,posterior_version from basket_probability_envelope where station_id=? and event_date=?",(sid,day),limit=200)
            truths+=query(c,'select * from calibration_station_daily_high_truth where station_id=? and local_date=?',(sid,day),limit=100)
            bounds+=query(a,'select * from reported_trajectory_canonical_station_day_bounds where station_id=? and event_date=?',(sid,day),limit=10)
            for hour in [8,12,16,20,23]:
                cutoff=stamp(start+datetime.timedelta(hours=hour))
                rows=query(c,sql_env,(sid,day,cutoff),limit=1)
                for r in rows:
                    r['sample_at_utc']=cutoff;r['sample_local_hour']=hour
                    for k in ['lineage_json','probabilities_json','bucket_ordinals_json']:r[k]=json.loads(r[k])
                    envelopes.append(r)
        except Exception as e:failures.append({'station':sid,'date':day,'error':str(e)})
    print(sid,len(envelopes),len(observations),flush=True)
save('envelopes.json',envelopes);save('observations.json',observations);save('truths.json',truths);save('bounds.json',bounds);save('domains.json',domains)
save('manifest.json',{'asof':now(),'event_dates':dates,'stations':stations,'sample_hours':[8,12,16,20,23],'authority':'READ_ONLY_DIAGNOSTIC_NOT_PROMOTION_OOS','resource_limits':LIMITS,'queries':{'envelopes':sql_env,'observations':sql_obs},'counts':{'envelopes':len(envelopes),'observations':len(observations),'truths':len(truths),'bounds':len(bounds)},'failures':failures})
print('DONE',len(envelopes),len(observations),len(truths),failures)
print('truth domains',Counter((x['source_lane'],x['qc_status'],x['source_role'],x['timestamps_complete'],x['authority_scope']) for x in truths))
print('obs domains',Counter((x['source_lane'],x['report_type'],x['correction_suspected']) for x in observations))
print('bound domains',Counter((x['selected_source_lane'],x['selection_reason']) for x in bounds))
