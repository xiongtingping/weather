"""Temporal-block and early/late robustness of the frozen partial scores."""
import json
import numpy as np
from rebuild import HERE,old,guard


def main():
    guard();result=json.loads((HERE/'restricted_refitted_fit.json').read_text());rows={}
    for variant,groups in result['variants'].items():
        out={}
        for group,v in groups.items():
            if 'day_deltas' not in v:continue
            days=sorted(v['day_deltas']);x=np.array([v['day_deltas'][d] for d in days]);n=len(days);rng=np.random.default_rng(20261008);blocks={}
            for b in (2,4):
                starts=rng.integers(0,n,(5000,(n+b-1)//b));idx=((starts[:,:,None]+np.arange(b))%n).reshape(5000,-1)[:,:n]
                boot=x[idx].mean(1);blocks[str(b)]=np.quantile(boot,[.025,.975],axis=0).T.tolist()
            half=n//2
            out[group]=dict(days=n,mean=v['mean'],circular_blocks_ci95=blocks,early_mean=x[:half].mean(0).tolist(),late_mean=x[half:].mean(0).tolist())
        rows[variant]=out
    final=dict(schema='restricted_score_temporal_sensitivity_v1',source_sha=old.sha_file(HERE/'restricted_refitted_fit.json'),variants=rows,assumption='Circular contiguous blocks of observed daily aggregate scores; sensitivity only, no promotion. Weather dependence between days is retained within length 2 and 4.',production_modified=False,current_chain_certified=False)
    (HERE/'score_sensitivity.json').write_text(json.dumps(final,ensure_ascii=False,indent=2))
    for name in ['latest_6500_coverage_logscore','day1_6500_coverage_logscore']:
        print(name,rows[name]['all'],flush=True)


if __name__=='__main__':main()
