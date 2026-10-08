import json, collections, bisect, datetime as dt
SYS=("ECMWF_IFS_ENS_OPEN_PF50","ECMWF_AIFS_ENS_GLOBAL","DWD_ICON_GLOBAL_EPS","NOAA_GEFS_05","ECCC_GEM_GLOBAL_ENS","NOAA_AIGEFS_025")
def ts(s): return dt.datetime.fromisoformat(s.replace("Z","+00:00")).timestamp()
def load():
    S=json.load(open("sysbuckets.json")); I=json.load(open("icon_members.json"))
    dist=collections.defaultdict(list)   # (station,target,system) -> [(asof_ts, {bucket:p}, intervals)]
    intervals={}
    for r in S:
        dist[(r["station"],r["target"],r["system"])].append((ts(r["asof"]),r["p"],r["intervals"])); intervals[(r["station"],r["target"])]=r["intervals"]
    # ICON from members: group by (station,target,run,asof)
    g=collections.defaultdict(list)
    for m in I:
        if m["t"] is None: continue
        g[(m["station"],m["target"],m["run"],m["asof"])].append(m["t"])
    for (st,ed,run,asof),temps in g.items():
        iv=intervals.get((st,ed))
        if not iv or len(temps)<10: continue
        p={b:0.0 for b in iv}
        for t in temps:
            for b,(lo,hi) in iv.items():
                if (lo is None or t>=lo) and (hi is None or t<hi): p[b]+=1/len(temps); break
        dist[(st,ed,"DWD_ICON_GLOBAL_EPS")].append((ts(asof),p,iv))
    for k in dist: dist[k].sort(key=lambda x:x[0])
    return dist,intervals
def asof(dist,st,ed,system,t):
    L=dist.get((st,ed,system))
    if not L: return None
    i=bisect.bisect_right([x[0] for x in L],t)-1
    return L[i] if i>=0 else None
def centre(label,iv):
    lo,hi=iv[label]
    if lo is None: return hi-0.5
    if hi is None: return lo+0.5
    return (lo+hi)/2
