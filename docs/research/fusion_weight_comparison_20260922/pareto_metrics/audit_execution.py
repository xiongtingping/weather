"""First executable quote within a frozen wait window; certify blockers, not invented PnL."""
import json,collections,sqlite3
from datetime import timedelta,datetime
from zoneinfo import ZoneInfo
import evaluate as E
C=E.C

def main():
    g=C.study.Guard();c=C.study.connect(g);pairs=E.load();out=[];quotes=[];counts=collections.Counter();caps=collections.Counter()
    for x,_ in pairs:
        g.check();at=x['at']
        env=c.execute('select fresh_until_utc from basket_probability_envelope indexed by basket_probability_envelope_checkpoint_idx where checkpoint_id=? and stage=? limit 2',(x['checkpoint'],'P0')).fetchall()
        if len(env)!=1:raise RuntimeError('probability_envelope_not_unique')
        deadline=min(at+timedelta(seconds=300),C.study.stamp(env[0]['fresh_until_utc']),datetime.fromisoformat(x['event_date']).replace(tzinfo=ZoneInfo(x['timezone'])))
        lo=C.study.iso(at-timedelta(seconds=1));hi=C.study.iso(deadline+timedelta(seconds=1))
        rs=c.execute('select * from basket_official_ws_snapshot indexed by basket_official_ws_snapshot_asof_idx where station_id=? and event_date=? and state=? and observed_at<=? and quote_expires_at>? order by observed_at,sequence limit 101',(x['station'],x['event_date'],'COMMITTED',hi,lo)).fetchall()
        if len(rs)==101:raise RuntimeError('wait_window_quote_bound')
        eligible=[];reasons=collections.Counter()
        for raw in rs:
            if len(raw['buckets_json'])>2*1024**2:raise RuntimeError('quote_blob_limit')
            r=dict(raw);b=json.loads(r['buckets_json']);cash=json.loads(r['cash_phase_input_json'])
            names=[z.get('bucket') for z in b]
            if set(names)!=set(x['intervals_c']) or len(set(names))!=len(names):reasons['bucket_mismatch']+=1;continue
            if r['quote_source']!='OFFICIAL_CLOB_WS' or r['settlement_timezone']!=x['timezone']:reasons['source_or_timezone']+=1;continue
            if any(not z.get('book_observed_at') or not z.get('token_id') or not isinstance(z.get('ask_levels'),list) for z in b):reasons['incomplete_book']+=1;continue
            known=max(at,*(C.study.stamp(r[k]) for k in ['created_at','observed_at','quote_observed_at']),*(C.study.stamp(z['book_observed_at']) for z in b),C.study.stamp(cash['observed_at_utc']))
            if not known<deadline or not known<C.study.stamp(r['quote_expires_at']):reasons['not_available_in_window']+=1;continue
            eligible.append((known,r,b,cash))
        row={'sample_sha':x['sample_sha'],'station':x['station'],'day':x['event_date'],'prediction_at':x['decision_at'],'deadline':C.study.iso(deadline),'rejected_quotes':dict(reasons)}
        if not eligible:
            row['status']='no_complete_first_quote_within_300s';counts[row['status']]+=1;out.append(row);continue
        known,r,b,cash=min(eligible,key=lambda z:(z[0],C.study.stamp(z[1]['observed_at']),z[1]['sequence']))
        row.update(status='first_quote_found',execution_at=C.study.iso(known),delay_s=(known-at).total_seconds(),snapshot_id=r['snapshot_id'],quote_sha=r['content_sha256'])
        # Verify version-bound state. No synthetic inventory or bankroll defaults.
        inv=c.execute('select inventory_snapshot_id,observed_at,created_at,complete,inventory_version,content_sha256 from basket_confirmed_inventory_snapshot where station_id=? and event_date=? and inventory_version=? and state=? limit 2',(x['station'],x['event_date'],r['inventory_version'],'COMMITTED')).fetchall()
        order=c.execute('select order_state_snapshot_id,observed_at,created_at,known,order_state_version,content_sha256 from basket_order_state_snapshot where station_id=? and event_date=? and state=? and observed_at<=? and order_state_version=? order by observed_at desc limit 2',(x['station'],x['event_date'],'COMMITTED',C.study.iso(known+timedelta(seconds=1)),r['order_state_version'])).fetchall()
        row['inventory_asof']=len(inv)==1 and bool(inv[0]['complete']) and max(C.study.stamp(inv[0][k]) for k in ['observed_at','created_at'])<=known
        row['order_state_asof']=len(order)==1 and bool(order[0]['known']) and max(C.study.stamp(order[0][k]) for k in ['observed_at','created_at'])<=known
        eqs=c.execute('select equity_id,observed_at_utc,created_at_utc,cash_observed_at_utc,equity_usdc,content_sha256 from basket_target_account_equity indexed by basket_target_account_equity_observed_idx where observed_at_utc>=? and observed_at_utc<=? order by observed_at_utc desc limit 101',(C.study.iso(known-timedelta(seconds=1801)),C.study.iso(known+timedelta(seconds=1)))).fetchall()
        if len(eqs)==101:raise RuntimeError('capital_rows_bound')
        eq=[e for e in eqs if max(C.study.stamp(e[k]) for k in ['observed_at_utc','created_at_utc'])<=known and e['cash_observed_at_utc'] and 0<=(known-C.study.stamp(e['cash_observed_at_utc'])).total_seconds()<=300 and e['equity_usdc']>0]
        row['real_equity_asof']=bool(eq)
        row['equity_receipt_sha']=eq[0]['content_sha256'] if eq else None
        # A station-day has many registry generations. Resolve one as-of snapshot
        # first, then load only that generation instead of accumulating history.
        generation=c.execute('select snapshot_sha256,refreshed_at from basket_target_market_registry where station_id=? and event_date=? and refreshed_at<=? order by refreshed_at desc limit 1',(x['station'],x['event_date'],C.study.iso(known))).fetchone()
        registry=c.execute('select outcome,yes_token,registry_json,refreshed_at from basket_target_market_registry where snapshot_sha256=? and station_id=? and event_date=? limit 101',(generation['snapshot_sha256'],x['station'],x['event_date'])).fetchall() if generation else []
        if len(registry)==101:raise RuntimeError('registry_generation_rows_bound')
        rr=[dict(z) for z in registry if C.study.stamp(z['refreshed_at'])<=known]
        row['asof_market_registry_row_n']=len(rr)
        row['retained_fee_fields']=sorted({k for z in rr for k in json.loads(z['registry_json']) if 'fee' in k.lower()})
        row['explicit_quote_fee_fields']=sorted({k for z in b for k in z if 'fee' in k.lower()})
        # Existing baseline shadow receipt may preserve policy/costs, but cannot be assumed a fill.
        shadow=c.execute('select shadow_score_id,checkpoint_id,objective_form,bankroll_usdc,budget_usdc,decision_at_utc,created_at,content_sha256 from basket_shadow_objective_score indexed by basket_shadow_objective_scope_idx where station_id=? and event_date=? and decision_at_utc>=? and decision_at_utc<=? order by decision_at_utc limit 21',(x['station'],x['event_date'],C.study.iso(at),C.study.iso(deadline))).fetchall()
        if len(shadow)==21:raise RuntimeError('shadow_receipt_bound')
        row['shadow_receipt_n']=len(shadow)
        for k in ['inventory_asof','order_state_asof','real_equity_asof']:caps[k]+=row[k]
        caps['has_asof_registry']+=bool(rr);caps['has_fee_field']+=bool(row['retained_fee_fields'] or row['explicit_quote_fee_fields']);caps['has_shadow_receipt']+=bool(shadow)
        quotes.append({'sample_sha':x['sample_sha'],'execution_at':row['execution_at'],'snapshot_metadata':{k:v for k,v in r.items() if k not in ['buckets_json','cash_phase_input_json']},'buckets':b,'cash_phase_input':cash})
        counts[row['status']]+=1;out.append(row)
    ar=sqlite3.connect(f'file:{C.ROOT}/data/archive.db?mode=ro',uri=True,timeout=2)
    fields=[z[1] for z in ar.execute('pragma table_info(basket_official_ws_snapshot)')];ar.close()
    C.study.save('execution_window_quotes.json',quotes)
    C.study.save('execution_audit.json',{'sample_n':len(pairs),'wait_limit_seconds':300,'counts':counts,'capability_counts':caps,'rows':out,
        'archive_schema_missing_required_fields':sorted(set(['settlement_timezone','quote_expires_at','quote_freshness_policy_sha256'])-set(fields)),
        'economic_status':'NOT_IDENTIFIED','economic_claim':'No actual EV or portfolio log-growth inferred from incomplete execution evidence.','peak_mib':g.peak/1024**2})
    print(dict(counts),dict(caps),flush=True)

if __name__=='__main__':main()
