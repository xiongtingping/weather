"""Exact activation inventory, separate from historical sample counts."""
from audit import *
started=time.monotonic(); c=connect()
def progress():
    return int(time.monotonic()-started>120 or resource.getrusage(resource.RUSAGE_SELF).ru_maxrss>384*1024**2)
c.set_progress_handler(progress,10000)
m=json.loads((OUT/'manifest.json').read_text()); at=m['asof']
c.execute('begin')
domains=[list(r) for r in c.execute('select distinct artifact_kind,authority_scope from calibration_native_ensemble_activation where station_id=?',('RKPK',))]
allrows=[]; tz={};registry={r[0]:dict(family=r[1],members=r[2]) for r in c.execute('select ensemble_system_id,independence_family_id,expected_member_count from calibration_native_ensemble_system') if r[0] in SYSTEMS}
for station in m['stations']:
    tt=[r[0] for r in c.execute("select distinct settlement_timezone from basket_target_market_registry where station_id=? and settlement_timezone<>''",(station,))]
    assert len(tt)==1;tz[station]=tt[0]
    assert (dt.datetime.fromisoformat(m['cutoff']).replace(tzinfo=ZoneInfo(tt[0]))+dt.timedelta(days=1)).astimezone(dt.timezone.utc)<stamp(at)
    seen=set()
    q="""select a.ensemble_system_id_or_star,a.season_bin,a.lead_bucket,a.run_cycle,a.generation,a.authority_scope,a.artifact_id,s.reliability_parameters_json,s.train_start_date,s.train_end_date,s.holdout_start_date,s.holdout_end_date,s.effective_sample_n,a.valid_from_utc,a.valid_until_utc,a.activated_at_utc from calibration_native_ensemble_activation a join calibration_native_ensemble_system_artifact s on s.system_artifact_id=a.artifact_id where a.station_id=? and a.artifact_kind='NATIVE_ENSEMBLE_SYSTEM_CALIBRATION' and julianday(a.valid_from_utc)<=julianday(?) and julianday(a.valid_until_utc)>julianday(?) and julianday(a.activated_at_utc)<=julianday(?) order by a.generation desc"""
    for r in c.execute(q,(station,at,at,at)):
        if r[0] not in SYSTEMS or r[:4] in seen:continue
        seen.add(r[:4]); rel=json.loads(r[7])
        allrows.append(dict(station=station,system=r[0],season=r[1],lead=r[2],cycle=r[3],generation=r[4],authority=r[5],artifact_id=r[6],profile=rel.get('oos_threshold_profile'),formal=rel.get('formal_p0_eligible'),train_start=r[8],train_end=r[9],holdout_start=r[10],holdout_end=r[11],effective_sample_n=r[12],train_source_counts=rel.get('train_source_counts'),valid_from=r[13],valid_until=r[14],activated_at=r[15]))
promotions=[list(r) for r in c.execute('select artifact_kind,decision,count(*) from calibration_native_ensemble_promotion where julianday(decided_at_utc)<=julianday(?) group by artifact_kind,decision',(at,))]
c.rollback();c.close()
(OUT/'activation.json').write_text(json.dumps(dict(asof=at,domains=domains,registry=registry,timezones=tz,rows=allrows,promotion_counts=promotions),indent=2))
print('activation',len(allrows),collections.Counter(r['profile'] for r in allrows),promotions)
