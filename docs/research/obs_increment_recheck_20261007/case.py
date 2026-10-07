"""ZUUU case held out from all fitting; probabilities are counterfactual."""
import gzip,json,math
import numpy as np
from audit import HERE,ROOT
from experiment import prepare,satellite_maps,pack,update,physical_means,physical_update
from bayes_basket_target import p1_direct_update as D

def main():
    audit=json.load(open(HERE/'audit.json'));maps=satellite_maps()
    artifact=json.load(open(ROOT/'data/runtime/bayes_basket_target_nwp/p1_observation_likelihood_artifact.json'));block=artifact['direct_update'];names=D.x_layout();idx=D.STATE_COLUMNS.index('cloud_cover_fraction')
    prev=json.load(gzip.open(HERE/'source_cache/ZUUU_previous_2026-09-01_2026-10-07.json.gz','rt'))['payload']['hourly']
    fc=[dict(t=prev['temperature_2m_previous_day1'][prev['time'].index(f'2026-10-07T{h:02d}:00')],sw=prev['shortwave_radiation_previous_day1'][prev['time'].index(f'2026-10-07T{h:02d}:00')],pr=prev['precipitation_previous_day1'][prev['time'].index(f'2026-10-07T{h:02d}:00')]) for h in range(24)]
    out=[];cloud_effect=[]
    for c in audit['case']:
        if c['hour']<12:continue
        receipt=c['receipt']
        if receipt['artifact_sha256']!=artifact['content_sha256']:raise ValueError('case_artifact_changed')
        late,mid=D.clock_gates(receipt['clock_hours'],D.gate_half_width(block))
        coefficient=(block['theta_rise'][names.index('cloud_cover_fraction')]+late*block['theta_rise'][names.index('late_x_cloud_cover_fraction')]+mid*block['theta_rise'][names.index('mid_x_cloud_cover_fraction')])/block['scale'][idx]
        cloud_effect.append(dict(hour=c['hour'],sct_to_bkn_delta_logodds_rise=.375*coefficient,artifact_sha=artifact['content_sha256'],same_other_features=True))
        m=c['observation'];r=dict(station='ZUUU',day='2026-10-07',decided_at=c['at'],tz='Asia/Shanghai',local_hour=c['hour']+int(c['at'][14:16])/60,p1=c['p'],intervals=c['intervals'],hard_excluded=c['hard_excluded'].split(','),truth=25.,running_high=float(c['running_high']),temp_now=m['temp'],precip_now=m['rain'],observation_valid_at=m['valid'],fc=fc)
        for lag in (0,1,2):
            data,_=prepare([r],maps,lag)
            if not data:continue
            x=data[0];p,s,u,w,k,z=pack(data);b=x['keys'].index('25°C')
            fits=json.load(open(HERE/f'fits_lag{lag}.json'))['fitted'];last=sorted(d for d in fits if d.startswith('nll_radiation_'))[-1];beta=fits[last]['beta'];new=update(p,s,u,z,beta)
            pf=json.load(open(HERE/f'physical_fits_lag{lag}.json'));lastday=max(pf);params=pf[lastday]['params'];physical,m1,m0=physical_update(data,params)
            unit,_=physical_means(data,1.)
            out.append(dict(hour=c['hour'],at=c['at'],lag_hours=lag,baseline_p25=float(p[0,b]),fitted_p25=float(new[0,b]),forced_p25={str(strength):float(update(p,s,u,z,[strength,0.])[0,b]) for strength in (1.,2.,4.)},physical_ratio_p25=float(physical[0,b]),physical_gamma=params['gamma'],neutral_expected_high_c=float(m0[0]),physical_expected_high_c=float(m1[0]),unit_gamma_high_c=float(unit[0]),rad=x['rad'],radiation=x['rad_evidence'],fitted_beta=beta,parameter_fit_day=lastday,case_truth_role='observational_candidate_not_oos',counterfactual_only=True))
    (HERE/'case.json').write_text(json.dumps(dict(rows=out,cloud_effect=cloud_effect,warning='Oct 7 not closed and not OOS; 25C used only for choosing displayed bucket, never fitting'),ensure_ascii=False,indent=2))
    for r in out:
        if r['lag_hours']==1:print(r['hour'],'rad',round(r['rad'],3),'p25',round(r['baseline_p25'],3),'->',round(r['fitted_p25'],3),'physical high',round(r['physical_expected_high_c'],3),'gamma',round(r['physical_gamma'],3))

if __name__=='__main__':main()
