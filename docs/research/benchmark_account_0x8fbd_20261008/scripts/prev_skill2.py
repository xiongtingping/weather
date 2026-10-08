import json, collections, statistics as st
reg=json.load(open("registry.json")); base=json.load(open("baseline.json"))
lad=collections.defaultdict(list)
for r in reg: lad[(r["station_id"],r["event_date"])].append(r)
for k in lad: lad[k].sort(key=lambda r:(r["lower_c"] if r["lower_c"] is not None else -999))
unit={r["station_id"]:r["settlement_unit"] for r in reg}
def bucket_ord(st_,ed,tc):
    L=lad.get((st_,ed))
    if not L or tc is None: return None
    t=tc*9/5+32 if L[0]["settlement_unit"]=="F" else tc
    for i,r in enumerate(L):
        lo,hi=r["lower_c"],r["upper_c"]
        if (lo is None or t>=lo-0.5) and (hi is None or t<hi+0.5): return i
def ordinal(st_,ed,label):
    for i,r in enumerate(lad.get((st_,ed),[])):
        if r["outcome"]==label: return i
P={}
for f in ("prev_global.json","prev_regional.json"):
    try: P.update(json.load(open(f)))
    except Exception: pass
tmax=collections.defaultdict(dict)
for key,v in P.items():
    if "error" in v: continue
    st_,m=key.split("|")
    byday=collections.defaultdict(list)
    for t,x in zip(v["time"],v["d1"]):
        if x is not None: byday[t[:10]].append(x)
    for d,xs in byday.items():
        if len(xs)>=20: tmax[(st_,m)][d]=max(xs)
REG={"ukmo_uk_deterministic_2km","icon_d2","icon_eu","italia_meteo_arpae_icon_2i","meteofrance_arome_france_hd","meteofrance_arpege_europe","knmi_harmonie_arome_europe","dmi_harmonie_arome_europe","kma_ldps","kma_gdps","jma_msm","gem_hrdps_continental","gem_regional","ncep_nbm_conus","gfs_hrrr","metno_nordic"}
truth={}
for sd,rec in base.items():
    st_,ed=sd.split("|"); truth[(st_,ed)]=rec["settled_tmax_c"]
# per (station, model): list of (ed, forecast, truth)
series=collections.defaultdict(list)
for (st_,m),dd in tmax.items():
    for ed,x in dd.items():
        if (st_,ed) in truth: series[(st_,m)].append((ed,x,truth[(st_,ed)]))
def loo_corrected_hits(st_,m,pool_bias):
    """leave-one-day-out bias correction using this station's other days (fallback: pooled model bias)"""
    out=[]
    for ed,x,t in series[(st_,m)]:
        others=[(tt-xx) for e2,xx,tt in series[(st_,m)] if e2!=ed]
        b=st.mean(others) if len(others)>=5 else pool_bias
        bo=bucket_ord(st_,ed,x+b); tro=bucket_ord(st_,ed,t)
        if bo is None or tro is None: continue
        mk=base[f"{st_}|{ed}"]["D-1 12:00"]["market_mode"]; mko=ordinal(st_,ed,mk) if mk else None
        mk2=base[f"{st_}|{ed}"]["D0 02:00"]["market_mode"]; mko2=ordinal(st_,ed,mk2) if mk2 else None
        out.append(dict(hit=bo==tro,raw_hit=bucket_ord(st_,ed,x)==tro,mk=(mko==tro) if mko is not None else None,mk2=(mko2==tro) if mko2 is not None else None,st=st_))
    return out
models=sorted(set(m for _,m in series))
print("LOO per-station bias-corrected single-bucket hit (d1 run), vs raw, vs market mode at D-1 12:00 and D0 02:00 on same days")
for m in models:
    allx=[t-x for k,v in series.items() if k[1]==m for _,x,t in v]
    if len(allx)<10: continue
    pool=st.mean(allx)
    rows=[]
    for (st_,mm) in series:
        if mm==m: rows+=loo_corrected_hits(st_,m,pool)
    n=len(rows)
    if n<10: continue
    h=sum(r["hit"] for r in rows)/n; rh=sum(r["raw_hit"] for r in rows)/n
    mk=[r for r in rows if r["mk"] is not None]; mk2=[r for r in rows if r["mk2"] is not None]
    s1=f"model {sum(r['hit'] for r in mk)/len(mk):.3f} vs mkt {sum(r['mk'] for r in mk)/len(mk):.3f} (n={len(mk)})" if mk else "-"
    s2=f"model {sum(r['hit'] for r in mk2)/len(mk2):.3f} vs mkt {sum(r['mk2'] for r in mk2)/len(mk2):.3f} (n={len(mk2)})" if mk2 else "-"
    print(f"  {m:32s} {'REG' if m in REG else 'GLB'} n={n:4d} pooled bias {pool:+.2f}°C | raw {rh:.3f} -> LOO-corrected {h:.3f} | D-1 12:00: {s1} | D0 02:00: {s2}")
# regional consensus per station (mean of corrected regional forecasts), LOO
print("--- station-level: best achievable? per station, LOO-corrected hit of each model with >=10 days, top model per station vs market D0 02:00 ---")
per=collections.defaultdict(dict)
for (st_,m),v in series.items():
    if len(v)<10: continue
    allx=[t-x for _,x,t in v]; rows=loo_corrected_hits(st_,m,st.mean(allx))
    if rows: per[st_][m]=(sum(r["hit"] for r in rows)/len(rows),len(rows),sum(1 for r in rows if r["mk2"]) and sum(r["mk2"] or 0 for r in rows)/max(1,sum(1 for r in rows if r["mk2"] is not None)))
HI={"ZHHH","LIMC","EGLC","RCSS","RKSI","LTAC","KLAX","OEJN"}
for st_ in sorted(per, key=lambda s:(s not in HI, s)):
    best=max(per[st_].items(),key=lambda kv:kv[1][0])
    print(f"  {st_}{'*' if st_ in HI else ' '} best {best[0]:30s} hit {best[1][0]:.3f} (n={best[1][1]}) | market D0 02:00 {best[1][2]:.3f} | n_models {len(per[st_])}")
