"""Recursive fee-field audit of the exact as-of registry generation and shadow receipts."""
import json
import evaluate as E

def fields(obj,path=''):
    out=[]
    if isinstance(obj,dict):
        for k,v in obj.items():
            at=path+'.'+k
            if 'fee' in k.lower():out.append({'path':at,'value':v if isinstance(v,(str,int,float,bool)) else type(v).__name__})
            out+=fields(v,at)
    elif isinstance(obj,list):
        for i,v in enumerate(obj):out+=fields(v,path+'['+str(i)+']')
    return out

def main():
    g=E.C.study.Guard();c=E.C.study.connect(g);a=json.loads((E.HERE/'execution_audit.json').read_text());out=[]
    for x in a['rows']:
        if x['status']!='first_quote_found':continue
        item={'sample_sha':x['sample_sha'],'station':x['station'],'day':x['day'],'fee_fields':[]}
        if x['asof_market_registry_row_n']:
            gen=c.execute('select snapshot_sha256 from basket_target_market_registry where station_id=? and event_date=? and refreshed_at<=? order by refreshed_at desc limit 1',(x['station'],x['day'],x['execution_at'])).fetchone()
            for r in c.execute('select registry_json from basket_target_market_registry where snapshot_sha256=? and station_id=? and event_date=? limit 100',(gen[0],x['station'],x['day'])):item['fee_fields']+=fields(json.loads(r[0]))
        if x['shadow_receipt_n']:
            for r in c.execute('select actual_basket_json,shadow_basket_json,actual_curve_json,shadow_curve_json from basket_shadow_objective_score where station_id=? and event_date=? and decision_at_utc>=? and decision_at_utc<=? limit 20',(x['station'],x['day'],x['prediction_at'],x['deadline'])):
                for k in r.keys():item['fee_fields']+=fields(json.loads(r[k]),k)
        out.append(item)
    E.C.study.save('nested_fee_audit.json',{'quote_eligible_n':len(out),'scopes_with_any_fee_field':sum(bool(x['fee_fields']) for x in out),'rows':out})
    print('fee-field scopes',sum(bool(x['fee_fields']) for x in out),'of',len(out))

if __name__=='__main__':main()
