from audit import *
import csv
m=json.loads((OUT/'manifest.json').read_text());a=json.loads((OUT/'activation.json').read_text())
scopes=[]; totals=collections.Counter(); per_station=[]
LEADS=['h00_12','h12_24','h24_48','h48_plus']
abbr=dict(zip(SYSTEMS,['IFS','AIFS','ICON','GEFS','GEM','AIGEFS']))
for station in m['stations']:
    p=json.loads((OUT/(station+'.json')).read_text());assert p['code']==m['code'] and p['asof']==m['asof']
    groups=collections.defaultdict(list)
    for v in p['cases']:
        groups[(v['system'],v['season'],v['lead'],v['cycle'])].append(v)
        assert v['members']==a['registry'][v['system']]['members']
        totals['cases']+=1;totals['member_links']+=v['members']
        totals['queries_after_asof']+=not v['all_queries_before_asof']
        totals['query_before_close']+=v['all_queries_before_close']
        totals['loader_eligible']+=v['loader_eligible']
    for v in p['scopes']:
        rr=groups[(v['system'],v['season'],v['lead'],v['cycle'])]
        assert len(rr)==len({r['date'] for r in rr})==v['days']
        assert sorted(r['date'] for r in rr if r['loader_eligible'])[-8:]==v['holdout_dates']
        hold=[r for r in rr if r['date'] in v['holdout_dates']]
        train=[];boundary=None
        if len(hold)==8:
            boundary=min(stamp(r['run_init']) for r in hold)
            begin=(dt.date.fromisoformat(min(r['date'] for r in hold))-dt.timedelta(days=60)).isoformat()
            train=[r for r in rr if begin<=r['date']<min(r['date'] for r in hold) and stamp(r['run_init'])<boundary]
        assert sorted(r['date'] for r in train)==v['train_dates']
        assert (len(train)>=20 and len(hold)==8)==v['count_20_8']
        unavailable=set(v['unknown_train_truth_dates']+v['different_train_truth_dates'])
        known=[r for r in train if r['date'] not in unavailable]
        assert len(known)==v['train_truth_known_same']
        known_received=[r for r in known if stamp(r['last_archive_query'])<=boundary]
        preday=lambda r: stamp(r['last_archive_query'])<dt.datetime.fromisoformat(r['date']).replace(tzinfo=ZoneInfo(a['timezones'][station])).astimezone(dt.timezone.utc)
        h_good=sum(preday(r) for r in hold)
        v.update(train_truth_and_receipt_known=len(known_received),holdout_received_before_day_start=h_good,receipt_timestamp_20_8_candidate=len(known_received)>=20 and len(hold)==8 and h_good==8)
        active=[r for r in a['rows'] if (r['station'],r['system'],r['season'],r['lead'],r['cycle'])==(station,v['system'],v['season'],v['lead'],v['cycle'])]
        assert len(active)<=1
        v['active_artifact_id']=active[0]['artifact_id'] if active else ''
        scopes.append(v)
    assert resource.getrusage(resource.RUSAGE_SELF).ru_maxrss<384*1024**2
cells=[]
for station in m['stations']:
    for lead in LEADS:
        rows=[r for r in scopes if r['station']==station and r['lead']==lead]
        seen=sorted({r['system'] for r in rows});count=sorted({r['system'] for r in rows if r['count_20_8']});receipt=sorted({r['system'] for r in rows if r['receipt_timestamp_20_8_candidate']})
        cells.append(dict(station=station,lead=lead,history_systems=' '.join(abbr[s] for s in seen),count_20_8_systems=' '.join(abbr[s] for s in count),receipt_timestamp_candidate_systems=' '.join(abbr[s] for s in receipt),count_20_8_n=len(count),receipt_timestamp_candidate_n=len(receipt)))
def csvout(name,rows):
    with (OUT/name).open('w') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader()
        for r in rows:w.writerow({k:json.dumps(v,ensure_ascii=False) if isinstance(v,list) else v for k,v in r.items()})
csvout('exact_scopes.csv',scopes);csvout('station_lead_matrix.csv',cells)
models=[]
for system in SYSTEMS:
    rr=[r for r in scopes if r['system']==system]
    models.append(dict(model=abbr[system],scopes=len(rr),count_20_8=sum(r['count_20_8'] for r in rr),receipt_candidate=sum(r['receipt_timestamp_20_8_candidate'] for r in rr),min_days=min(r['days'] for r in rr),max_days=max(r['days'] for r in rr),first=min(r['first'] for r in rr),count_ready_stations=len({r['station'] for r in rr if r['count_20_8']})))
summary=dict(asof=m['asof'],cutoff=m['cutoff'],stations=45,exact_scopes=len(scopes),totals=dict(totals),models=models,count_20_8=sum(r['count_20_8'] for r in scopes),receipt_timestamp_candidates=sum(r['receipt_timestamp_20_8_candidate'] for r in scopes),count_ready_with_unknown_training_truth=sum(r['count_20_8'] and r['train_truth_unavailable']>0 for r in scopes),count_ready_with_changed_training_truth=sum(r['count_20_8'] and r['train_truth_different']>0 for r in scopes),active_exact_scopes=len(a['rows']),active_profiles=dict(collections.Counter(r['profile'] for r in a['rows'])),active_authorities=dict(collections.Counter(r['authority'] for r in a['rows'])),active_formal_flags=dict(collections.Counter(str(r['formal']) for r in a['rows'])),promotion_counts=a['promotion_counts'],station_lead_cells=180,cells_with_two_count_candidates=sum(r['count_20_8_n']>=2 for r in cells),cells_with_two_receipt_candidates=sum(r['receipt_timestamp_candidate_n']>=2 for r in cells),leads={lead:dict(count_two=sum(r['count_20_8_n']>=2 for r in cells if r['lead']==lead),receipt_two=sum(r['receipt_timestamp_candidate_n']>=2 for r in cells if r['lead']==lead)) for lead in LEADS})
(OUT/'summary.json').write_text(json.dumps(summary,indent=2))
print(json.dumps(summary,indent=2))
