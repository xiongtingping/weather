from common import *
import numpy as np
from collections import Counter,defaultdict
def dt(s):return datetime.datetime.fromisoformat(s.replace('Z','+00:00'))
rows=json.loads((OUT/'observations.json').read_text());a=connect('archive.db')
awc=[r for r in rows if r['source_lane']=='awc' and not r['correction_suspected']]
polls={};by_provider=defaultdict(list);late=Counter();days=defaultdict(list);missing=[]
for r in awc:
    pid=r['source_poll_id']
    if pid not in polls:
        found=query(a,'select * from reported_trajectory_source_polls where poll_id=?',(pid,),limit=1)
        polls[pid]=found[0] if found else None
    poll=polls[pid]; lag=(dt(r['first_seen_at'])-dt(r['valid_at'])).total_seconds()
    if not poll:missing.append(pid);continue
    p=json.loads(poll['poll_json']); provider=p.get('provider_id') or p.get('transport') or 'unknown'
    by_provider[str(provider)].append(lag)
    days[r['event_date']].append(lag)
    if lag>1800:late[(r['event_date'],r['first_seen_at'])]+=1
def q(xs):return {'n':len(xs),'p50_sec':float(np.quantile(xs,.5)),'p90_sec':float(np.quantile(xs,.9)),'over_15min':sum(x>900 for x in xs)}
out={'providers':{k:q(v) for k,v in by_provider.items()},'days':{k:q(v) for k,v in days.items()},'late_batches':[{'date':k[0],'seen':k[1],'n':v} for k,v in late.most_common(15)],'polls_missing':len(missing),'sample_poll_json':[json.loads(x['poll_json']) for x in list(polls.values())[:3] if x]}
save('latency_detail.json',out)
save('polls_snapshot.json',[{k:v for k,v in r.items() if k!='poll_json'} for r in polls.values() if r])
print(json.dumps({k:v for k,v in out.items() if k!='sample_poll_json'},ensure_ascii=False,indent=2)[:6500])
