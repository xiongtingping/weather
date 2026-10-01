import os,sys,time,sqlite3,shutil,psutil,resource,json,hashlib
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
OUT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
resource.setrlimit(resource.RLIMIT_FSIZE,(128*1024**2,128*1024**2))
P=psutil.Process(); START=time.monotonic(); DEADLINE=START+600

def guard():
 if P.memory_info().rss>1200*1024**2: raise RuntimeError('RSS exceeds 1200MiB')
 if shutil.disk_usage(ROOT).free<3*1024**3: raise RuntimeError('free disk below 3GiB')
 if time.monotonic()>DEADLINE: raise RuntimeError('runtime exceeds 600 seconds')
 if sum(p.stat().st_size for p in OUT.glob('*') if p.is_file())>200*1024**2: raise RuntimeError('output exceeds 200MiB')
 return 0

def connect():
 guard();c=sqlite3.connect('file:'+str(ROOT/'data/bayes_basket_target_ssot.db')+'?mode=ro',uri=True,timeout=3)
 c.execute('pragma query_only=on');c.execute('pragma cache_size=-8192');c.execute('pragma temp_store=memory');c.set_progress_handler(guard,50000)
 return c

def save(name,x):
 guard();(OUT/name).write_text(json.dumps(x,ensure_ascii=False,indent=2,allow_nan=False))
