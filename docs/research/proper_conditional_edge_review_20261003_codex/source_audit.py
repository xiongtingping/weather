"""Bounded read-only lineage, revision and replay-cache checks."""
import os,time,threading,subprocess,shutil,resource,gzip,json,re,sqlite3,bisect
from pathlib import Path
HERE=Path(__file__).resolve().parent;ROOT=HERE.parents[2];SRC=HERE.parent/'proper_conditional_edge_20261003';START=time.monotonic()
resource.setrlimit(resource.RLIMIT_FSIZE,(16*1024**2,16*1024**2))
def guard():
 while True:
  try:
   assert int(subprocess.check_output(['ps','-o','rss=','-p',str(os.getpid())],text=True))*1024<1200*1024**2
   assert shutil.disk_usage(HERE).free>2*1024**3 and time.monotonic()-START<180
  except Exception:os._exit(77)
  time.sleep(.5)
threading.Thread(target=guard,daemon=True).start()
import numpy as np,pandas as pd
out={};labels={};first={};budget=0
with gzip.open(SRC/'decisions.jsonl.gz','rt') as f:
 for line in f:
  budget+=len(line);assert budget<512*1024**2
  r=json.loads(line);labels[r['st'],r['d']]=(r['rev'],r['y'],r['tmax'])
  if r['stage']=='P0' and r['first']:first[r['st'],r['d']]=r['t']
conn=sqlite3.connect(f'file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro',uri=True,isolation_level=None,timeout=2)
conn.execute('pragma query_only=1');conn.execute('pragma cache_size=-4000')
timer=time.monotonic();conn.set_progress_handler(lambda:int(time.monotonic()-timer>5),1000)
out['label_station_values']=[r[0] for r in conn.execute('select distinct station_id from basket_station_day_settlement_label')]
out['truth_lane_values']=[r[0] for r in conn.execute('select distinct source_lane from calibration_station_daily_high_truth')]
mismatch=[];first_diff=[];revised=[]
for (st,d),old in labels.items():
 timer=time.monotonic()
 row=conn.execute('select truth_revision_no,settled_bucket,settled_tmax_c from basket_station_day_settlement_label where station_id=? and event_date=? order by truth_revision_no desc limit 1',(st,d)).fetchone()
 if tuple(row or ())!=old:mismatch.append([st,d,old,row])
 if (st,d) in first:
  got=conn.execute("select min(decision_at_utc) from basket_policy_training_fact where station_id=? and event_date=? and stage='P0'",(st,d)).fetchone()[0]
  if got!=first[st,d]:first_diff.append([st,d,first[st,d],got])
 rows=conn.execute("select revision_no,official_tmax_c,canonical_committed_at_utc from calibration_station_daily_high_truth where station_id=? and local_date=? and source_lane in ('AWC','SETTLEMENT_SOURCE') and qc_status='ACCEPTED' order by revision_no",(st,d)).fetchall()
 if len(rows)>1 and rows[0][1]!=rows[-1][1]:revised.append([st,d,rows[0],rows[-1]])
conn.close()
out.update(label_count=len(labels),label_mismatch=mismatch,first_fact_mismatch=first_diff,changed_truth_revisions=revised)
out['elapsed']=time.monotonic()-START
(HERE/'source_audit.json').write_text(json.dumps(out,ensure_ascii=False,indent=2))
print(json.dumps(out,ensure_ascii=False))
