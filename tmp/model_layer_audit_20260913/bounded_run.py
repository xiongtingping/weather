"""Audit-only resource wrapper; never opens production for writing."""
import os
from pathlib import Path
import resource
import signal
import subprocess
import sys
import time
import psutil

root = Path(__file__).resolve().parent
label, seconds, *command = sys.argv[1:]
log = root / (label + '.log')
budget = float(seconds)
minimum_free = 3 * 1024**3
maximum_rss = 1200 * 1024**2
maximum_output = 16 * 1024**2
start = time.monotonic()
peak = 0
failure = None

def probe():
    if psutil.disk_usage(str(root)).free < minimum_free:
        raise RuntimeError('disk_reserve_breached')

def limits():
    resource.setrlimit(resource.RLIMIT_FSIZE, (maximum_output, maximum_output))
    resource.setrlimit(resource.RLIMIT_CPU, (int(budget), int(budget) + 1))

probe()
with log.open('wb') as output:
    child = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT,
                             start_new_session=True, preexec_fn=limits,
                             env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
    try:
        while child.poll() is None:
            probe()
            processes = [psutil.Process(child.pid)]
            processes += processes[0].children(recursive=True)
            rss = sum(p.memory_info().rss for p in processes if p.is_running())
            peak = max(peak, rss)
            if rss > maximum_rss:
                raise RuntimeError('rss_limit_breached')
            if time.monotonic() - start > budget:
                raise RuntimeError('wall_time_limit_breached')
            if log.stat().st_size > maximum_output:
                raise RuntimeError('output_limit_breached')
            time.sleep(.15)
    except (Exception, KeyboardInterrupt) as exc:
        failure = str(exc)
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    rc = child.wait()
print({'label': label, 'exit_code': rc, 'failure': failure,
       'seconds': round(time.monotonic() - start, 2), 'peak_rss_mib': round(peak/1024**2, 1),
       'log': str(log)})
print(log.read_text(errors='replace')[-22000:])
sys.exit(1 if failure else rc)
