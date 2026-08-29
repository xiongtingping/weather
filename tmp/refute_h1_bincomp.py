import json
from collections import Counter, defaultdict
recs=json.load(open("tmp/h1_night_ceiling_rows.json"))
sel=[r for r in recs if 21<=r["local_hour"]<24]
print("rows in 21-24:",len(sel))
byk=defaultdict(list)
for r in sel: byk[(r["station"],r["date"])].append(r)
print("station-days:",len(byk))
cnt=sorted(((len(v),k) for k,v in byk.items()),reverse=True)
print("top 8 by rowcount:",cnt[:8])
print("bottom 8:",cnt[-8:])
# strength distribution
print("strength counter (all 21-24 rows):",Counter(round(float(r['strength'] or 0),2) for r in sel).most_common(8))
lastrows={}
for r in sel:
    k=(r["station"],r["date"])
    if k not in lastrows or r["at"]>lastrows[k]["at"]: lastrows[k]=r
print("strength counter (dedup last rows):",Counter(round(float(r['strength'] or 0),2) for r in lastrows.values()).most_common(8))
# heavy station-day detail
n,k=cnt[0]
v=sorted(byk[k],key=lambda r:r["at"])
print(f"\n== heavy station-day {k} n={n} ==")
for r in v[::max(1,n//25)]:
    print(f"  {r['at']} lh={r['local_hour']:.2f} rh={r['running_high_c']} o{r['rh_ord']} settled o{r['settled_ord']} above={r['mass_above']:.3f} act={r['actual_above']} str={r['strength']}")
print("  distinct rh values:",Counter(str(r['running_high_c']) for r in v))
print("  distinct decision_at:",len(set(r['at'] for r in v)))
