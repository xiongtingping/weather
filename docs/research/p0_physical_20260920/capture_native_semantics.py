"""Record source hashes and two bounded GRIB header spot checks, not OOS data."""
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from study import ROOT, Guard, save, iso


def main():
    guard=Guard();sources={}
    for name in ['noaa_gefs_archive','ecmwf_ifs_archive','eccc_geps_archive','dwd_icon_eps_archive',
                 'future_temperature_curve','native_raw_object_store']:
        p=ROOT/'bayes_basket_target'/f'{name}.py'
        if p.stat().st_size>1024**2: raise RuntimeError('source_size_limit')
        sources[str(p.relative_to(ROOT))]=hashlib.sha256(p.read_bytes()).hexdigest()
    selected=[('gefs','f02ac25b263e68641efcb5da9b27a6f28021675c87aa71c499a49ed6084dc852'),
              ('ecmwf_pf50','6833895f351dbfffae38b9f0ce5682a9cd220451a281a04a72e6d11c97327226')]
    objects=[]
    for name,digest in selected:
        p=ROOT/'data/runtime/official_nwp_sources'/name/'raw'/f'{digest}.grib2'
        if p.stat().st_size>2*1024**2: raise RuntimeError('grib_size_limit')
        if hashlib.sha256(p.read_bytes()).hexdigest()!=digest: raise ValueError('raw_hash_mismatch')
        command=['/opt/homebrew/bin/grib_ls','-j','-p',
                 'shortName,units,typeOfLevel,level,stepType,startStep,endStep,dataDate,dataTime,validityDate,validityTime',str(p)]
        result=subprocess.run(command,capture_output=True,text=True,timeout=15,check=True)
        if len(result.stdout)>65536: raise RuntimeError('header_output_limit')
        objects.append({'path':str(p.relative_to(ROOT)),'sha256':digest,'headers':json.loads(result.stdout),
                        'usage':'SEMANTICS_ONLY_NOT_OOS'})
    save('native_semantics_evidence.json',{'source_sha256':sources,'objects':objects,
         'checked_at':iso(datetime.now(timezone.utc)),'production_writes':0,'network_requests':0})
    print('source_hashes',len(sources),'grib_spot_checks',len(objects))


if __name__=='__main__':main()
