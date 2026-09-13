from common import *
import urllib.request, subprocess
urls={
 'awc_api.html':'https://aviationweather.gov/data/api/',
 'awc_metar_help.html':'https://aviationweather.gov/help/data/',
 'asos.html':'https://www.weather.gov/asos/',
 'asos_user_guide.pdf':'https://www.weather.gov/media/asos/aum-toc.pdf',
 'fmh1.pdf':'https://www.icams-portal.gov/resources/ofcm/fmh/FMH1/fmh1_2019.pdf',
}
result=[];total=0
for name,url in urls.items():
    try:
        req=urllib.request.Request(url,headers={'User-Agent':'P1-evidence-review/1.0'})
        with urllib.request.urlopen(req,timeout=20) as r:
            chunks=[];size=0
            while True:
                chunk=r.read(65536)
                if not chunk:break
                size+=len(chunk);total+=len(chunk)
                if size>8*1024**2 or total>20*1024**2:raise RuntimeError('RESPONSE_LIMIT')
                chunks.append(chunk)
        body=b''.join(chunks);(OUT/name).write_bytes(body)
        result.append({'url':url,'file':name,'retrieved_at':now(),'bytes':len(body),'sha256':hashlib.sha256(body).hexdigest()})
    except Exception as e:
        error=str(e)
        try:
            target=OUT/name
            proc=subprocess.run(['/usr/bin/curl','--fail','--location','--max-time','25','--max-filesize','8388608','--silent','--show-error',url,'--output',str(target)],capture_output=True,timeout=30)
            if proc.returncode:raise RuntimeError(proc.stderr.decode()[:300])
            body=target.read_bytes();total+=len(body)
            if total>20*1024**2:raise RuntimeError('TOTAL_RESPONSE_LIMIT')
            result.append({'url':url,'file':name,'retrieved_at':now(),'bytes':len(body),'sha256':hashlib.sha256(body).hexdigest(),'transport':'curl_system_trust','initial_error':error})
        except Exception as second:result.append({'url':url,'error':str(second),'initial_error':error,'retrieved_at':now()})
    print(result[-1],flush=True)
save('official_sources.json',result)
