"""Bounded, read-only diagnostic helpers. Never writes production stores."""
import datetime, hashlib, json, os, pathlib, resource, shutil, sqlite3, threading, time
import psutil

ROOT = pathlib.Path(__file__).resolve().parents[2]
OUT = pathlib.Path(__file__).resolve().parent
START = time.monotonic()
PROC = psutil.Process()
LIMITS = dict(rss_mb=768, wall_seconds=900, min_free_gb=5, max_file_mb=48)
resource.setrlimit(resource.RLIMIT_FSIZE, (48*1024**2, 48*1024**2))

def guard():
    while True:
        try:
            rss=PROC.memory_info().rss+sum(p.memory_info().rss for p in PROC.children(recursive=True) if p.is_running())
            if (rss > LIMITS['rss_mb']*1024**2 or
                shutil.disk_usage(ROOT).free < LIMITS['min_free_gb']*1024**3 or
                time.monotonic()-START > LIMITS['wall_seconds']):
                raise RuntimeError('resource limit')
        except Exception:
            os.write(2,b'FAIL_CLOSED_RESOURCE_PROBE_OR_LIMIT\n'); os._exit(73)
        time.sleep(.2)
threading.Thread(target=guard,daemon=True).start()

def connect(name='bayes_basket_target_ssot.db'):
    c=sqlite3.connect(f'file:{ROOT}/data/{name}?mode=ro', uri=True,timeout=2)
    c.row_factory=sqlite3.Row
    c.execute('pragma query_only=on'); c.execute('pragma cache_size=-8192')
    c.execute('pragma temp_store=memory')
    return c

def query(c,sql,args=(),limit=5000,seconds=25):
    deadline=time.monotonic()+seconds
    c.set_progress_handler(lambda:int(time.monotonic()>deadline),10000)
    rows=c.execute(sql,args).fetchmany(limit+1)
    if len(rows)>limit: raise RuntimeError('ROW_LIMIT_EXCEEDED')
    return [dict(r) for r in rows]

def save(name,obj):
    path=OUT/name
    path.write_text(json.dumps(obj,ensure_ascii=False,indent=2,default=str)+'\n')
    return path

def now(): return datetime.datetime.now(datetime.timezone.utc).isoformat()
