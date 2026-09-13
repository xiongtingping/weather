import ast
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]
files={
 'bayes_basket_target/native_ensemble_oos.py':['_fit_parameters','_resolved_bias_c','_probabilities','_select_minimum_crps_subset','_shaped'],
 'bayes_basket_target/native_ensemble_p0.py':['_interval_probability','_calibrated_system_probability'],
 'bayes_basket_target/center_width_calibration.py':['apply_center_width_calibration'],
 'bayes_basket_target/band_reliability_store.py':['load_band_table','_forward_nll_delta','fit_band_reliability_table','_truth'],
 'bayes_basket_target/cross_consensus.py':['_centre_distances','apply_band_reliability'],
 'bayes_basket_target/production_worker.py':[],
 'bayes_basket_target/materialization.py':[],
 'tests/test_center_width_tiers.py':['test_every_serving_face_carries_the_tiers'],
 'tests/test_native_ensemble_oos_p0_pipeline.py':[],
}
out={'recorded_at_utc':datetime.now(timezone.utc).isoformat(),'git_head':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),'note':'Working tree changed concurrently. Whole-file hashes and exact reviewed function excerpts below are the review version; test phase logs explicitly refer to different working-tree phases.','sources':{},'evidence_hashes':{}}
for rel,names in files.items():
    data=(ROOT/rel).read_bytes();source=data.decode();tree=ast.parse(source)
    nodes=[n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name in names]
    out['sources'][rel]={'sha256':hashlib.sha256(data).hexdigest(),'excerpts':[{'name':n.name,'line':n.lineno,'source':ast.get_source_segment(source,n)} for n in nodes]}
for p in HERE.glob('*'):
    if p.is_file() and p.name not in ('source_manifest.json','final_validation.log'):out['evidence_hashes'][p.name]=hashlib.sha256(p.read_bytes()).hexdigest()
out['report_sha256']=hashlib.sha256((ROOT/'docs/research/MODEL_LAYER_REVIEW_2026-09-13.md').read_bytes()).hexdigest()
out['repository_status']=subprocess.check_output(['git','status','--porcelain'],cwd=ROOT,text=True).splitlines()
(HERE/'source_manifest.json').write_text(json.dumps(out,ensure_ascii=False,indent=2))

quality=json.loads((HERE/'quality_summary.json').read_text())
served=json.loads((HERE/'served_evidence.json').read_text())
invariants=json.loads((HERE/'invariant_evidence.json').read_text())
assert served['summary']==quality['latest_45_p0']
assert served['summary']['latest_p0_station_count']==45
assert served['summary']['cw_and_band_nonidentity']==44
assert served['summary']['provisional_station_count']==11
assert len(quality['observed_p0_peak_moves'])==3
assert quality['unique_single_system_artifact_count']==100
assert invariants['band_forward_training_overlap']['overlap_count']==8
shape=out['sources']['bayes_basket_target/native_ensemble_oos.py']['excerpts'][-1]
shapes=[x for x in out['sources']['bayes_basket_target/native_ensemble_oos.py']['excerpts'] if x['name']=='_shaped']
assert len(shapes)==1
assert shapes[0]['source'].index('apply_band_reliability(')<shapes[0]['source'].index('apply_center_width_calibration(')
text=(ROOT/'docs/research/MODEL_LAYER_REVIEW_2026-09-13.md').read_text()
links=re.findall(r'\]\((/[^)]+)\)',text)
for link in links:assert Path(re.sub(r':\d+$','',link)).exists(),link
for name in ['core_tests.log','pipeline_tests.log','reverse_tests.log','isolated_failures.log']:assert (HERE/name).stat().st_size>0
print(json.dumps({'report_numbers_validated':True,'local_links_validated':len(links),'source_files':len(files),'evidence_bytes':sum(p.stat().st_size for p in HERE.iterdir() if p.is_file()),'git_head':out['git_head']},ensure_ascii=False))
