"""Keep each fit object bounded while preserving exact training identities."""
import gzip
import json
from rebuild import HERE,guard,MAX_BYTES
from bayes_basket_target.contracts import content_sha256


def write_fit_report(path,result):
    guard();stored=dict(result);dst=HERE/'component_fit_receipts';dst.mkdir(exist_ok=True);written=0
    for key in ('taf_fit_receipts','p1_fit_receipts'):
        refs=[]
        for receipt in result[key]:
            body=json.dumps(receipt,ensure_ascii=False).encode();written+=len(body)
            if len(body)>2*1024**2 or written>MAX_BYTES:raise RuntimeError('component_fit_receipt_output_limit')
            sha=content_sha256(receipt);target=dst/(sha+'.json.gz')
            if not target.exists():
                with gzip.open(target,'wb') as out:out.write(body)
            refs.append(dict(day=receipt['day'],path=str(target.relative_to(HERE)),sha=sha))
        stored[key]=refs
    stored['receipt_storage']='bounded_content_addressed_receipts_v1';body=json.dumps(stored,ensure_ascii=False,indent=2)
    if len(body.encode())>2*1024**2:raise RuntimeError('fit_report_object_limit')
    path.write_text(body)


def load_fit_report(path):
    body=path.read_bytes()
    if len(body)>2*1024**2:raise RuntimeError('fit_report_object_limit')
    result=json.loads(body);used=0
    if result.get('receipt_storage')=='bounded_content_addressed_receipts_v1':
        for key in ('taf_fit_receipts','p1_fit_receipts'):
            receipts=[]
            for ref in result[key]:
                with gzip.open(HERE/ref['path'],'rb') as inp:body=inp.read(2*1024**2+1)
                used+=len(body)
                if len(body)>2*1024**2 or used>MAX_BYTES:raise RuntimeError('component_fit_receipt_input_limit')
                r=json.loads(body)
                if content_sha256(r)!=ref['sha'] or r['day']!=ref['day']:raise ValueError('component_fit_receipt_identity_failed')
                receipts.append(r)
            result[key]=receipts
    return result
