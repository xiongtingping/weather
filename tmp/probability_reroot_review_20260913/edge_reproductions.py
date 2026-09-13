from common import *
import importlib.util,sys,tempfile
from unittest.mock import patch
sys.path.insert(0,str(ROOT))
from bayes_basket_target.contracts import ProbabilityEnvelope,content_sha256,ContractError
from bayes_basket_target.production_worker import TargetProductionWorker as Worker,_native_forward_packet_is_stale
import bayes_basket_target.production_worker as pw
import bayes_basket_target.p1_compiler as pc
from bayes_basket_target.native_ensemble_p0 import _stage_for_native_compile,_taf_envelope_witness,seated_evidence_received_at
from bayes_basket_target.ssot import apply_production_ssot_schema
from bayes_basket_target.materialization import insert_probability_envelope
from bayes_basket_target.probability_bridge import materialize_probability_bridge
def module(name,path):
 spec=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
f=module('worker_review_fixtures',ROOT/'tests/test_bayes_basket_target_production_worker.py')
g=module('p1_review_fixtures',ROOT/'tests/test_build_bayes_basket_target_p1_packets.py')
def update(env,**changes):return ProbabilityEnvelope.from_mapping({**env.to_dict(),**changes})
results={}
# The same complete worker path, once with a matching calibrated artifact,
# once with no station-matching artifact. No production DB/network is used.
for calibrated in [False,True]:
 with tempfile.TemporaryDirectory(prefix='reroot-',dir=OUT) as folder:
  folder=pathlib.Path(folder);station='RKPK' if calibrated else 'ZBAA';zone='Asia/Seoul' if calibrated else 'Asia/Shanghai'
  c=sqlite3.connect(':memory:');apply_production_ssot_schema(c)
  f._seed_target_registry_source_lane(c,station_id=station,event_date='2026-07-27',settlement_timezone=zone,source_lane='awc')
  r=f._reroot_scope_root(station_id=station,checkpoint_id='root-old',decision_at_utc='2026-07-26T10:00:00.000Z',fresh_until_utc='2026-07-26T16:00:00.000Z',probabilities={'34':.2,'35':.6,'36':.2})
  r=update(r,settlement_timezone=zone,lineage={**r.lineage,'settlement_timezone':zone})
  materialize_probability_bridge(c,r,created_at_utc=r.decision_at_utc)
  capture=f._reroot_taf_capture(station_id=station,revision_id='taf-one',sequence=1,issued_at_utc='2026-07-26T11:00:00Z',received_at_utc='2026-07-26T11:05:00Z',valid_until_utc='2026-07-27T18:00:00Z',source_sha_char='c',tx_c=35.0,supersedes=None)
  capture['settlement_timezone']=zone;capture['content_sha256']=content_sha256({k:v for k,v in capture.items() if k!='content_sha256'})
  inbox=folder/'inbox.json';inbox.write_text(json.dumps({'schema':'bayes_basket_target_taf_revision_inbox_v1','captures':[capture]}))
  db=folder/'read.db'
  with sqlite3.connect(db) as x:x.execute('create table marker(value text)')
  artifact_path=f._bridged_taf_artifact(folder)
  if calibrated:
   art=json.loads(artifact_path.read_text());art['station_id']=station
   art['content_sha256']=content_sha256({k:v for k,v in art.items() if k!='content_sha256'})
   artifact_path.write_text(json.dumps(art))
  worker=Worker(db,commit_conn=c,taf_artifact_path=artifact_path,taf_inbox_path=inbox)
  try:
   initial=worker._materialize_resident_p0m(as_of_utc='2026-07-26T12:00:00.000Z')
   first=f._p0m_rows(c,station)[-1]
   if ('taf_revision_artifact_id' in first['lineage'])!=calibrated:
    print('MODE_MISMATCH',calibrated,initial)
    raise AssertionError('fixture_calibration_mode')
   r2=update(r,checkpoint_id='root-new',decision_at_utc='2026-07-26T12:30:00.000Z',fresh_until_utc='2026-07-26T18:30:00.000Z',probabilities={'34':.1,'35':.5,'36':.4})
   insert_probability_envelope(c,r2,created_at_utc=r2.decision_at_utc)
   same=worker._materialize_resident_p0m(as_of_utc=r2.decision_at_utc)
   same_head=f._p0m_rows(c,station)[-1]['lineage']['p0_root_checkpoint_id']
   after=worker._materialize_resident_p0m(as_of_utc='2026-07-26T12:31:00.000Z')
   after_head=f._p0m_rows(c,station)[-1]['lineage']['p0_root_checkpoint_id']
   # Isolate the native information-time gate on a newer run: every other
   # input is the admitted calibrated fixture, only the run clock is supplied.
   r3=update(r2,checkpoint_id='root-third',decision_at_utc='2026-07-26T13:00:00.000Z',fresh_until_utc='2026-07-26T19:00:00.000Z')
   insert_probability_envelope(c,r3,created_at_utc=r3.decision_at_utc)
   with patch.object(Worker,'_p0_root_latest_run_init_utc',return_value=datetime.datetime(2026,7,26,12,tzinfo=datetime.timezone.utc)):
    older_taf=worker._materialize_resident_p0m(as_of_utc='2026-07-26T13:01:00.000Z')
   results['calibrated' if calibrated else 'observed']={'initial_mode':first['lineage'].get('taf_revision_state','CALIBRATED'),'same_cycle_head_root':same_head,'next_cycle_head_root':after_head,'same_cycle_state':same['state'],'run_after_taf_state':older_taf['state'],'run_after_taf_blockers':older_taf['blockers'],'run_after_taf_head':f._p0m_rows(c,station)[-1]['lineage']['p0_root_checkpoint_id']}
  finally:worker.close();c.close()
# Consumer root checks are exercised with exact admitted envelope objects.
root=g._p0_root();p0m=g._predecessor(root);oldp1=g._existing_p1()
newpm=update(p0m,checkpoint_id='p0m-new-taf',decision_at_utc='2026-07-26T08:00:00Z',fresh_until_utc='2026-07-26T12:00:00Z',probabilities={'75°F or below':.1,'76-77°F':.2,'78°F or higher':.7})
w=object.__new__(Worker);w.conn=object()
with patch.object(pw,'target_registry_source_lane',return_value='awc'),patch.object(pw,'load_materialized_probability_envelopes',return_value=[root,p0m,oldp1,newpm]):
 reason=w._replay_root_mismatch_reason(station_id=root.station_id,event_date=root.event_date,as_of_utc='2026-07-26T08:30:00Z')
results['consumer_ignores_new_taf_root']={'reason':reason,'p1_root':oldp1.lineage['prediction_root_checkpoint_id'],'new_p0m':newpm.checkpoint_id}
newroot=update(root,checkpoint_id='p0-new-curve',decision_at_utc='2026-07-26T07:30:00Z',fresh_until_utc='2026-07-26T12:00:00Z',lineage={**root.lineage,'conditional_future_temperature_curve_sha256':'f'*64})
with patch.object(pw,'target_registry_source_lane',return_value='awc'),patch.object(pw,'load_materialized_probability_envelopes',return_value=[root,p0m,oldp1,newroot]):
 reason=w._replay_root_mismatch_reason(station_id=root.station_id,event_date=root.event_date,as_of_utc='2026-07-26T08:30:00Z')
results['consumer_equal_probability_changed_curve']={'reason':reason,'same_probability':Worker._same_p0_forecast(root.checkpoint_id,newroot,[root,newroot]),'replay_selected_root':pc.select_p1_replay_base([root,p0m,oldp1,newroot],as_of_utc='2026-07-26T08:30:00Z')[0].checkpoint_id}
# The native TAF lookup tests both arrival and validity with a local DB.
c=sqlite3.connect(':memory:')
c.execute('create table calibration_forward_taf_capture(station_id text,event_date text,tx_c real,content_sha256 text,issued_at_utc text,revision_sequence integer,revision_id text,cloud_cover_fraction real,precipitation_probability real,wind_speed_mps real,received_at_utc text,valid_until_utc text)')
c.execute('insert into calibration_forward_taf_capture values(?,?,?,?,?,?,?,?,?,?,?,?)',('KATL','2026-07-26',30.0,'c'*64,'2026-07-26T06:00:00Z',1,'future-arrival',.3,.1,4.0,'2026-07-26T10:00:00Z','2026-07-26T07:00:00Z'))
witness=_taf_envelope_witness(c,station_id='KATL',event_date='2026-07-26',decision_at_utc='2026-07-26T08:00:00Z')
results['native_taf_future_arrival_and_expired']={'witness_selected':bool(witness),'revision':witness.get('forecast_revision_id') if witness else None};c.close()
runs={'A':'2026-07-26T00:00:00Z'};witness={'issued_at_utc':'2026-07-26T06:00:00Z'}
results['native_stage_not_stable']={'taf_advance':_stage_for_native_compile(predecessor_lineage={'run_init_utc_by_system_json':json.dumps(runs)},run_init_utc_by_system=runs,taf_witness=witness),'same_evidence_next_compile':_stage_for_native_compile(predecessor_lineage={'run_init_utc_by_system_json':json.dumps(runs),'taf_envelope_witness_json':json.dumps(witness)},run_init_utc_by_system=runs,taf_witness=witness)}
# A stale positive-weight member can still be renewed by a different seated member.
receipts={'A':'2026-07-25T00:00:00Z','B':'2026-07-26T08:00:00Z'}
results['fresh_seated_member_covers_old_member']={'selected_receipt':str(seated_evidence_received_at(receipts,seated_system_ids=['A','B'])),'probe_says_stale':_native_forward_packet_is_stale({'received_at_utc':receipts['B'],'archive_query_at_utc_by_system':receipts},as_of_utc='2026-07-26T08:30:00Z',seated_system_ids=['A','B']),'old_member_age_hours':32.5}
# Dense soft evidence is not a refresh trigger inside the existing cadence.
observations=[g._observation('o1',24,'2026-07-26T04:00:00Z'),g._observation('peak',26,'2026-07-26T07:00:00Z'),g._observation('cool',24,'2026-07-26T08:00:00Z')]
packets=pc.compile_station_p1_packets(predecessor=g._predecessor(),observations=observations,registry_rows=g._registry(),compilation_at_utc='2026-07-26T08:05:00Z')
head=ProbabilityEnvelope.from_mapping(packets[-1]['expected_envelope'])
soft={**g._observation('soft-cooling',22,'2026-07-26T08:10:00Z'),'evidence_role':'SOFT_PATH','source_lane':'asos_1min','report_type':'ASOS_1MIN'}
after=pc.compile_station_p1_packets(predecessor=head,prediction_root=g._predecessor(),observations=[*observations,soft],registry_rows=g._registry(),compilation_at_utc='2026-07-26T08:20:00Z',consumed_observation_ids={'o1','peak','cool'})
results['soft_only_arrival_before_cadence']={'new_packets':len(after),'head_fresh_until':head.fresh_until_utc}
refusals=[]
with patch.object(pc,'compile_metar_trajectory_update_proof',side_effect=ContractError('review_injected_refresh_refusal')):
 refused=pc.compile_station_p1_packets(predecessor=head,prediction_root=g._predecessor(),observations=observations,registry_rows=g._registry(),compilation_at_utc='2026-07-26T10:00:00Z',consumed_observation_ids={'o1','peak','cool'},state_refresh_refusals_out=refusals)
results['refused_refresh_now_reported']={'packets':len(refused),'reasons':refusals}
save('edge_reproductions.json',results);print(json.dumps(results,ensure_ascii=False,indent=2))
