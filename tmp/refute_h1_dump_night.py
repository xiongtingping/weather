import json, sys
from collections import Counter, defaultdict
recs=json.load(open("tmp/h1_night_ceiling_rows.json"))
print("recs",len(recs))
# rebuild the dedup exactly as they did for 21-24
def dedup(lo,hi):
    byk={}
    for r in recs:
        if lo<=r["local_hour"]<hi:
            k=(r["station"],r["date"])
            if k not in byk or r["at"]>byk[k]["at"]: byk[k]=r
    return list(byk.values())
for lo,hi in [(17,19),(19,21),(21,24)]:
    sel=dedup(lo,hi)
    print(f"\n=== bin {lo}-{hi} n={len(sel)} ===")
    d=Counter(r["rh_ord"]-r["settled_ord"] for r in sel)
    print("rh_ord - settled_ord histogram:",dict(sorted(d.items())))
    d2=Counter(round(float(r["running_high_c"])-float(r["settled_tmax_c"]),1) for r in sel)
    print("running_high_c - settled_tmax_c:",dict(sorted(d2.items())))
    print("mean mass_above",sum(r["mass_above"] for r in sel)/len(sel),
          "mean mass_total",sum(r["mass_total"] for r in sel)/len(sel))
sel=dedup(21,24)
sel.sort(key=lambda r:-r["mass_above"])
print("\n-- 12 highest mass_above in 21-24 --")
for r in sel[:12]:
    print(f"{r['station']} {r['date']} at={r['at']} lh={r['local_hour']:.1f} rh={r['running_high_c']} bkt={r['rh_bucket']}(o{r['rh_ord']}) settled={r['settled_bucket']}(o{r['settled_ord']}) tmax={r['settled_tmax_c']} above={r['mass_above']:.3f} tot={r['mass_total']:.3f} strength={r['strength']} excl={str(r['excluded'])[:60]}")
print("\n-- 8 lowest --")
for r in sel[-8:]:
    print(f"{r['station']} {r['date']} at={r['at']} lh={r['local_hour']:.1f} rh={r['running_high_c']} bkt={r['rh_bucket']}(o{r['rh_ord']}) settled={r['settled_bucket']}(o{r['settled_ord']}) above={r['mass_above']:.3f} strength={r['strength']}")
