"""Bounded read-only snapshot. Run with PYTHONPATH=. .venv/bin/python."""
import collections, datetime as dt, hashlib, json, pathlib, resource, shutil, sqlite3, time
from zoneinfo import ZoneInfo
from bayes_basket_target.canonical_contract import FORMAL_TRUTH_LANES_SQL
from bayes_basket_target.probability_bridge import history_envelope_payload

ROOT=pathlib.Path(__file__).resolve().parents[3]; OUT=pathlib.Path(__file__).parent
START=time.monotonic(); NOW=dt.datetime.now(dt.timezone.utc); USED=0
resource.setrlimit(resource.RLIMIT_FSIZE,(32*1024**2,32*1024**2))
resource.setrlimit(resource.RLIMIT_CPU,(210,210))
def instant(s): return dt.datetime.fromisoformat(s.replace('Z','+00:00'))
def guard():
    rss=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if not 0<rss<768*1024**2 or time.monotonic()-START>300: raise RuntimeError('memory/time limit')
    if shutil.disk_usage(OUT).free<4*1024**3: raise RuntimeError('disk floor')
    if USED>2*1024**3: raise RuntimeError('read cap')
def connect():
    c=sqlite3.connect(f'file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro',uri=True,timeout=2)
    c.execute('pragma query_only=ON');c.execute('pragma cache_size=-8192');c.execute('pragma temp_store=MEMORY')
    c.setlimit(sqlite3.SQLITE_LIMIT_LENGTH,8*1024**2);c.set_progress_handler(lambda:(guard() or 0),10000)
    return c
def unpack(l,k):
    v=l.get(k)
    return json.loads(v) if isinstance(v,str) else v

def main():
    global USED
    guard();c=connect()
    stations=sorted(r['station'] for r in json.loads((ROOT/'tmp/model_weight_verify_20260928/evidence.json').read_text())['latest_station_rows'])
    zones=dict(c.execute('select station_id,settlement_timezone from calibration_station_market_timezone_binding'))
    domains={k:[r[0] for r in c.execute(f'select distinct {k} from calibration_station_daily_high_truth')] for k in ['source_lane','qc_status','authority_scope']}
    truth={};counts=collections.Counter();excluded=[];rows=[]
    for r in c.execute(f'''select station_id,local_date,official_tmax_c,revision_no,truth_id,source_lane,qc_status,
      settlement_timezone,authority_scope,canonical_committed_at_utc,settlement_definition,content_sha256
      from calibration_station_daily_high_truth where local_date>='2026-09-01' and local_date<='2026-09-26'
      and source_lane in {FORMAL_TRUTH_LANES_SQL} order by revision_no'''):
        key=r[:2]
        if key in truth and truth[key][3]==r[3]: raise RuntimeError('ambiguous truth definition')
        truth[key]=r
    fields=['system_weights_json','fusion_blend_json','native_system_probabilities_json','native_calibrated_system_centers_c_json',
      'consensus_bridge_json','synoptic_cooling_json','neighbour_direction_json','center_width_json','band_reliability_json',
      'probability_floor_json','system_family_ids_json','hour_lead_bucket_by_system_json','system_activation_shas_json',
      'archive_query_at_utc_by_system_json','bucket_intervals_c_json','family_weight_caps_json']
    for st in stations:
      for offset in range(26):
        guard();day=(dt.date(2026,9,1)+dt.timedelta(days=offset)).isoformat();key=(st,day);counts['planned_station_days']+=1
        tr=truth.get(key)
        def reject(reason): counts[reason]+=1;excluded.append({'station':st,'date':day,'reason':reason})
        if not tr: reject('missing_truth');continue
        if tr[6]!='ACCEPTED' or tr[7]!=zones[st] or not tr[9] or instant(tr[9])>NOW: reject('truth_qc_clock_timezone');continue
        midnight=dt.datetime.fromisoformat(day).replace(tzinfo=ZoneInfo(zones[st])).astimezone(dt.timezone.utc)
        if midnight+dt.timedelta(days=1)>NOW: reject('not_closed');continue
        cutoff=midnight.isoformat(timespec='milliseconds').replace('+00:00','Z')
        h=c.execute('''select checkpoint_id,envelope_content_sha256,envelope_json,decision_at_utc,created_at_utc
          from basket_probability_bridge_history where station_id=? and event_date=? and stage='P0' and state='COMMITTED'
          and decision_at_utc<? and created_at_utc<? order by decision_at_utc desc,created_at_utc desc,checkpoint_id desc limit 1''',(st,day,cutoff,cutoff)).fetchone()
        if not h: reject('no_pre_midnight_p0');continue
        e=c.execute('select lineage_json,created_at,decision_at_utc,state,station_id,event_date from basket_probability_envelope where envelope_id=?',(h[0],)).fetchone()
        if not e or e[3:]!=('COMMITTED',st,day) or instant(e[1])>=midnight or e[2]!=h[3]: reject('envelope_identity_clock');continue
        raw=json.loads(e[0]);USED+=len(e[0])+len(h[2])
        if not raw.get('native_system_probabilities_json'): reject('single_system_distribution_not_saved');continue
        refs=[v[5:] for v in raw.values() if isinstance(v,str) and v.startswith('@ref/')]
        sizes=[]
        for sha in refs:
            q=c.execute('select length(document) from basket_probability_lineage_document where document_sha256=?',(sha,)).fetchone()
            if not q: raise RuntimeError('missing lineage document')
            if q[0]>8*1024**2: raise RuntimeError('document cap')
            sizes.append(q[0])
        if sum(sizes)>64*1024**2:raise RuntimeError('envelope cap')
        USED+=sum(sizes);guard()
        env=history_envelope_payload(c,checkpoint_id=h[0],envelope_content_sha256=h[1],stored=h[2]);lin=env['lineage']
        times=unpack(lin,'archive_query_at_utc_by_system_json');weights=unpack(lin,'system_weights_json')
        if not times or not set(weights)<=set(times) or any(instant(times[k])>instant(h[3]) for k in weights): reject('source_clock_missing_or_late');continue
        acts=unpack(lin,'system_activation_shas_json');act_errors=[];authorities=[]
        if not acts:reject('missing_activation_map');continue
        for name,sha in {**acts,'cross':lin.get('native_cross_activation_sha256')}.items():
            ac=c.execute('select station_id,activated_at_utc,valid_from_utc,valid_until_utc,authority_scope from calibration_native_ensemble_activation where content_sha256=?',(sha,)).fetchone()
            if not ac or ac[0]!=st or instant(ac[1])>instant(h[3]) or not instant(ac[2])<=instant(h[3])<instant(ac[3]):act_errors.append(name)
            if ac:authorities.append(ac[4])
        if act_errors:reject('activation_identity_clock');continue
        if env['settlement_timezone']!=zones[st] or lin.get('settlement_definition')!=tr[10]:reject('truth_settlement_definition');continue
        selection=c.execute('select selected_weights_json from calibration_cross_native_ensemble_selection_evidence where content_sha256=?',(lin.get('native_selection_evidence_sha256'),)).fetchone()
        if not selection:reject('selection_reference_missing');continue
        row={'station':st,'date':day,'tz':zones[st],'at':h[3],'created_at':e[1],'checkpoint':h[0],
             'envelope_sha':h[1],'posterior_version':env['posterior_version'],'stored_probability':env['probabilities'],
             'truth_c':tr[2],'truth_revision':tr[3],'truth_id':tr[4],'truth_lane':tr[5],'truth_sha':tr[11],
             'truth_authority':tr[8],'activation_authorities':sorted(set(authorities)),
             'formal_eligible':lin.get('native_formal_p0_eligible'),'selection_weights':json.loads(selection[0]),
             'inputs':{f:unpack(lin,f) for f in fields}}
        rows.append(row);counts['captured']+=1
      print(st,counts['captured'],flush=True)
    c.close();guard()
    result={'captured_at':NOW.isoformat(),'domains':domains,'counts':dict(counts),'excluded':excluded,'rows':rows,
            'peak_rss':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,'read_bytes':USED}
    encoded=json.dumps(result,ensure_ascii=False,indent=1);assert len(encoded.encode())<32*1024**2
    (OUT/'capture.json').write_text(encoded)
    print(json.dumps({k:v for k,v in result.items() if k not in ['rows','excluded']},ensure_ascii=False,indent=2))
if __name__=='__main__':main()
