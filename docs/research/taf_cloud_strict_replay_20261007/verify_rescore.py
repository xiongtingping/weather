"""Ensure the cloud-source repair did not quietly change its comparator."""
import json
from rebuild import HERE,old,guard
from fit_storage import load_fit_report


def main():
    guard();before=HERE/'restricted_refitted_fit.pre_cloud_origin.json';after=HERE/'restricted_refitted_fit.json';a=load_fit_report(before);b=load_fit_report(after);checks={}
    p1={r['day']:r for r in a['p1_fit_receipts']};taf={r['day']:r for r in a['taf_fit_receipts']}
    if set(p1)!={r['day'] for r in b['p1_fit_receipts']}:raise ValueError('rescore_outer_population_changed')
    for r in b['p1_fit_receipts']:
        fields=('theta','scaling','clock_offsets','lambda_','training_row_ids_sha','test_rows','train_rows','cutoff','trained_through')
        if any(r[k]!=p1[r['day']][k] for k in fields):raise ValueError('rescore_baseline_changed:'+r['day'])
        checks[r['day']]=dict(p1_baseline_identical=True)
    for r in b['taf_fit_receipts']:
        if r!=taf[r['day']]:raise ValueError('rescore_taf_baseline_changed:'+r['day'])
    for key,value in a['variants'].items():
        if '_fixed_' in key and value!=b['variants'][key]:raise ValueError('fixed_sensitivity_changed_outside_training:'+key)
    result=dict(schema='cloud_origin_rescore_comparator_identity_v1',passed=True,baseline_p1_days=len(checks),baseline_taf_days=len(taf),fixed_strength_score_groups_identical=True,neutral_cloud_source_rejections=b['source_profile']['cloud_increment_refused_origin_not_visible'],before_sha=old.sha_file(before),after_sha=old.sha_file(after),production_modified=False)
    (HERE/'cloud_origin_rescore_validation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result),flush=True)


if __name__=='__main__':main()
