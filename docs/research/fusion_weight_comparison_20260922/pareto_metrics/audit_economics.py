"""Read-only indexed point-in-time market evidence audit; no fictitious fills."""
import json,collections,hashlib
from datetime import timedelta
import evaluate as E
C=E.C

def main():
    g=C.study.Guard();c=C.study.connect(g);pairs=E.load();rows=[];quotes=[];counts=collections.Counter();domains=set()
    # Discover domain on a bounded scope before using predicates; reject unfamiliar states/sources below.
    x=pairs[-1][0]
    domain=[tuple(r) for r in c.execute('select distinct state,quote_source,settlement_timezone from basket_official_ws_snapshot where station_id=? and event_date=?',(x['station'],x['event_date']))]
    for x,_ in pairs:
        g.check();at=x['at'];upper=C.study.iso(at+timedelta(seconds=1));lower=C.study.iso(at-timedelta(seconds=1))
        # String bounds widened one second, then every clock parsed as UTC.
        candidates=c.execute('select snapshot_id,station_id,event_date,settlement_timezone,observed_at,quote_observed_at,quote_expires_at,quote_source,created_at,content_sha256,remaining_budget_usdc,inventory_version,order_state_version from basket_official_ws_snapshot indexed by basket_official_ws_snapshot_asof_idx where station_id=? and event_date=? and state=? and observed_at<=? and quote_expires_at>? order by observed_at desc,sequence desc limit 21',(x['station'],x['event_date'],'COMMITTED',upper,lower)).fetchall()
        if len(candidates)==21:raise RuntimeError('quote_candidate_bound')
        valid=[dict(r) for r in candidates if max(C.study.stamp(r[k]) for k in ['observed_at','quote_observed_at','created_at'])<=at and C.study.stamp(r['quote_expires_at'])>at]
        row={'station':x['station'],'day':x['event_date'],'sample_sha':x['sample_sha'],'decision_at':x['decision_at'],'valid_snapshot_n':len(valid)}
        if not valid:
            last=c.execute('select observed_at,quote_expires_at from basket_official_ws_snapshot indexed by basket_official_ws_snapshot_asof_idx where station_id=? and event_date=? and state=? and observed_at<=? order by observed_at desc,sequence desc limit 1',(x['station'],x['event_date'],'COMMITTED',upper)).fetchone()
            row['status']='no_fresh_asof_snapshot' if last else 'no_prior_snapshot'
            if last:row.update(last_observed_at=last['observed_at'],last_expires_at=last['quote_expires_at'])
        else:
            r=max(valid,key=lambda r:C.study.stamp(r['observed_at']));domains.add((r['quote_source'],r['settlement_timezone']))
            body=c.execute('select buckets_json,cash_phase_input_json from basket_official_ws_snapshot where snapshot_id=?',(r['snapshot_id'],)).fetchone()
            if len(body['buckets_json'])>2*1024**2:raise RuntimeError('quote_blob_size')
            buckets=json.loads(body['buckets_json']);cash=json.loads(body['cash_phase_input_json'])
            reasons=[]
            if r['quote_source']!='OFFICIAL_CLOB_WS':reasons.append('not_official_ws')
            if r['settlement_timezone']!=x['timezone']:reasons.append('timezone_mismatch')
            names=[b.get('bucket') for b in buckets]
            if len(set(names))!=len(names) or set(names)!=set(x['intervals_c']):reasons.append('bucket_contract_labels_mismatch')
            if any(not b.get('book_observed_at') or C.study.stamp(b['book_observed_at'])>at for b in buckets):reasons.append('book_clock_not_asof')
            if any(not isinstance(b.get('ask_levels'),list) or not b.get('token_id') for b in buckets):reasons.append('depth_or_token_missing')
            row.update(snapshot_id=r['snapshot_id'],quote_sha256=r['content_sha256'],remaining_budget_usdc=r['remaining_budget_usdc'],basic_quote_issues=reasons)
            row['status']='basic_quote_rejected' if reasons else 'basic_quote_eligible_execution_unverified'
            quotes.append({'sample_sha':x['sample_sha'],'metadata':r,'buckets':buckets,'cash_phase_input':cash})
        counts[row['status']]+=1;rows.append(row)
    # Audit says what exists at exact original decision times, not actual economic success.
    C.study.save('economic_quote_audit.json',{'sample_n':len(pairs),'counts':counts,'domain_probe':domain,'accepted_source_domains':sorted(domains),'rows':rows,
                   'economic_result_status':'NOT_IDENTIFIED_PENDING_EXECUTION_EVIDENCE','peak_mib':g.peak/1024**2})
    C.study.save('asof_quotes.json',quotes)
    print('quote_counts',dict(counts),'retained',len(quotes),flush=True)

if __name__=='__main__':main()
