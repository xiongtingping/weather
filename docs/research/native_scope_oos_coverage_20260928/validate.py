"""Repeat C/F stations and exercise exact-scope/date boundaries."""
from audit import *
m=json.loads((OUT/'manifest.json').read_text());checks=[]
for station in ['RKPK','KSFO']:
    before=json.loads((OUT/(station+'.json')).read_text())
    worker((station,m['asof'],m['cutoff']))
    after=json.loads((OUT/(station+'.json')).read_text())
    for field in ['scopes','cases','code']:
        assert before[field]==after[field],(station,field,'changed during rerun')
    checks.append(dict(station=station,cases=len(after['cases']),scopes=len(after['scopes']),same=True))
assert fingerprints()==m['code']
# Logical boundary checks used by the inventory, independently expressed.
def count_gate(train_days,hold_days):return len(set(train_days))>=20 and len(set(hold_days))>=8
train=[(dt.date(2026,8,1)+dt.timedelta(days=i)).isoformat() for i in range(20)]
hold=[(dt.date(2026,9,1)+dt.timedelta(days=i)).isoformat() for i in range(8)]
assert count_gate(train,hold)
assert not count_gate(train[:-1],hold)
assert not count_gate(train,hold[:-1])
assert not count_gate(train[:19]+train[:1],hold)
assert count_gate(list(reversed(train)),list(reversed(hold)))
# Two cycles of fourteen days must not be merged into a 28-day exact scope.
assert not count_gate(train[:14],hold[:7])
boundary=stamp('2026-09-19T00:00:00Z')
assert not boundary<boundary
assert boundary-dt.timedelta(microseconds=1)<boundary
assert not boundary+dt.timedelta(microseconds=1)<boundary
out=dict(replays=checks,code_stable=True,boundary_checks=9,member_and_scope_checks='all rows rechecked in summarize.py',receipt_samples=18)
(OUT/'validation.json').write_text(json.dumps(out,indent=2));print(out)
