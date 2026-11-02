"""Independent bounded checks of retained packet hashes and SSOT receipt links."""
from audit import *
from bayes_basket_target.contracts import content_sha256
started=time.monotonic();c=connect()
c.set_progress_handler(lambda:int(time.monotonic()-started>60),10000)
base=ROOT/'data/runtime/bayes_basket_target_nwp/official_native_capture'
files=['ecmwf_2026-09-26_d1.json','aifs_2026-09-26_d1_12z.json','icon_2026-09-26_d1.json','gefs_2026-09-26_d1.json','eccc_2026-09-26_d1.json','aigefs_2026-09-26_d1_12z.json']
out=[];total=0
for name in files:
    p=base/name;n=p.stat().st_size;total+=n
    assert n<16*1024**2 and total<64*1024**2 and shutil.disk_usage(ROOT).free>4*1024**3
    packet=json.loads(p.read_text())
    assert packet['content_sha256']==content_sha256({k:v for k,v in packet.items() if k!='content_sha256'})
    for v in packet['captures']:
        if v['station_id'] not in ['RKPK','KSFO','CYYZ']:continue
        assert v['content_sha256']==content_sha256({k:x for k,x in v.items() if k!='content_sha256'})
        digest=v['field_receipt_set_sha256']
        rr=list(c.execute('select member_id,raw_object_ref,archive_query_at_utc,native_run_init_utc,source_object_sha256 from calibration_native_ensemble_historical_member_run where ensemble_system_id=? and station_id=? and target_local_date=?',(packet['ensemble_system_id'],v['station_id'],packet['event_date'])))
        matched=[r for r in rr if r[1].endswith(':'+digest)]
        assert matched,(name,v['station_id'],'receipt reference missing')
        assert all(stamp(r[2])==stamp(v['archive_query_at_utc']) for r in matched)
        fields=v.get('field_receipts')
        if fields is not None: assert content_sha256(fields)==digest
        out.append(dict(path=str(p),system=packet['ensemble_system_id'],station=v['station_id'],packet_sha256=packet['content_sha256'],capture_sha256=v['content_sha256'],receipt_set_sha256=digest,linked_rows=len(matched),unique_members=len({r[0] for r in matched}),run_init=matched[0][3],actual_query=v['archive_query_at_utc'],native_publication_at_known=v.get('native_publication_at_known'),receipt_set_recomputed=fields is not None))
    assert resource.getrusage(resource.RUSAGE_SELF).ru_maxrss<384*1024**2
c.close()
(OUT/'receipt_samples.json').write_text(json.dumps(dict(samples=out,bytes_read=total),indent=2))
print(len(out),'packet/capture hashes and reference joins verified')
