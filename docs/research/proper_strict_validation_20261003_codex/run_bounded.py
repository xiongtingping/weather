"""Two bounded readers, resumable by exact engine/protocol identity; no writers."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib,json,os,resource,shutil,subprocess,sys,time
from pathlib import Path

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[2]
ENGINE=ROOT/'scripts/proper_edge_strict_evidence.py'
env={**os.environ,'OPENBLAS_NUM_THREADS':'1','OMP_NUM_THREADS':'1'}
resource.setrlimit(resource.RLIMIT_FSIZE,(32*1024**2,32*1024**2))


def tests():
    command=[sys.executable,'-m','pytest','-q','tests/test_proper_edge_strict_evidence.py','tests/test_native_ensemble_oos_p0_pipeline.py','tests/test_historical_republisher_oos_contract.py','tests/test_charter_231_proper_bet_lane.py','tests/test_charter_235_proper_yes_legs_stopped.py']
    logfile=HERE/'tests.log';started=time.monotonic()
    with logfile.open('w') as out:
        p=subprocess.Popen(command,cwd=ROOT,env=env,stdout=out,stderr=subprocess.STDOUT)
        try:
            while p.poll() is None:
                try:
                    rss=int(subprocess.check_output(['ps','-o','rss=','-p',str(p.pid)],text=True,timeout=2))*1024
                    if rss>1536*1024**2 or time.monotonic()-started>300 or shutil.disk_usage(HERE).free<2*1024**3 or logfile.stat().st_size>16*1024**2:raise RuntimeError('test_resource_budget')
                except subprocess.CalledProcessError:
                    if p.poll() is None:raise
                time.sleep(.25)
        except Exception:
            p.kill();p.wait();raise
    print(logfile.read_text()[-5000:])
    raise SystemExit(p.returncode)


def scan():
    index=json.loads((HERE/'input_index.json').read_text())
    expected_engine=hashlib.sha256(ENGINE.read_bytes()).hexdigest()
    expected_protocol=hashlib.sha256((HERE/'protocol.json').read_bytes()).hexdigest()
    def one(station):
        path=HERE/'stations'/f'{station}.json'
        if path.exists():
            previous=json.loads(path.read_text())
            if previous.get('engine_sha256')==expected_engine and previous.get('protocol_sha256')==expected_protocol:
                return station,'reused'
        if hashlib.sha256(ENGINE.read_bytes()).hexdigest()!=expected_engine:raise RuntimeError('engine_changed_mid_scan')
        p=subprocess.run([sys.executable,str(ENGINE),'station','--station',station],cwd=ROOT,env=env,capture_output=True,text=True,timeout=190)
        if len(p.stdout)+len(p.stderr)>1024**2:raise RuntimeError('log_budget_exceeded')
        if p.returncode:
            (HERE/f'failed_{station}.log').write_text(p.stderr)
            return station,f'FAILED {p.returncode}: {p.stderr[-500:]}'
        return station,p.stdout.strip()
    failures=[]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(one,s) for s in index['stations']]
        for f in as_completed(futures):
            st,out=f.result();print(st,out,flush=True)
            if 'FAILED' in out:failures.append(st)
    print('failed_stations',failures,flush=True)
    if failures:raise SystemExit(1)


if __name__=='__main__':
    if sys.argv[1]=='tests':tests()
    elif sys.argv[1]=='scan':scan()
    else:raise SystemExit('tests or scan required')
