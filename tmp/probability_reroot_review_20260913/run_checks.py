from common import *
import sys
test_root=pathlib.Path(os.environ.get('REROOT_TEST_ROOT',str(ROOT)))
sys.path.insert(0,str(test_root))
import pytest
label=sys.argv[1]
args=sys.argv[2:]
os.chdir(test_root)
with (OUT/(label+'.log')).open('w') as log:
    oldout,olderr=sys.stdout,sys.stderr
    sys.stdout=sys.stderr=log
    try:result=pytest.main(['-q','--tb=short','-p','no:cacheprovider',*args])
    finally:sys.stdout,sys.stderr=oldout,olderr
print('EXIT',int(result),'log',str(OUT/(label+'.log')))
print('\n'.join((OUT/(label+'.log')).read_text().splitlines()[-22:]))
raise SystemExit(result)
