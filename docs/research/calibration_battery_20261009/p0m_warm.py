"""The battery's largest single defect: P0-M within 36h is warm (mean z -0.275, PIT piled below the centre).
Where does it come from?  By lead band, local hour, period (charter 222 radiation 09-25, charter 241 censoring 10-01),
unit, station; and the P0 -> P0-M adjustment on the same station-day-band: does the adjustment move warm, and does
truth follow?  Plus P1's 0.6-0.7 bin over-claim by band."""
import json, gzip, math, collections
import numpy as np
exec(open('calib_battery.py').read().split('stage=np.array')[0])  # loaders + per-row fields + boot_mean
rows_by=collections.defaultdict(dict)
for r in rows: rows_by[(r['station'],r['event_date'],r['band'])][r['stage']]=r
def report(tag, R):
    if len(R)<40: return
    m,lo,hi=boot_mean([r['tc']-r['mu'] for r in R],[r['event_date'] for r in R]); z,zl,zh=boot_mean([r['z'] for r in R],[r['event_date'] for r in R])
    print(f"  {tag:34s} n={len(R):4d} bias {m:+.3f}°C [{lo:+.3f},{hi:+.3f}]{'*' if lo>0 or hi<0 else ' '} | mean z {z:+.3f} [{zl:+.3f},{zh:+.3f}] | σ {np.mean([r['sig'] for r in R]):.2f} | LogScore {np.mean([r['ls'] for r in R]):.3f}")
PM=[r for r in rows if r['stage']=='P0-M' and r['hours']<=36]
print("== P0-M ≤36h warm bias, sliced ==")
for band in ('h0_12','h12_24','h24_36'): report(f'band {band}',[r for r in PM if r['band']==band])
for blk in range(0,24,6): report(f'local hour {blk:02d}-{blk+6:02d}',[r for r in PM if blk<=r['lhour']<blk+6])
for lo,hi,lab in (('2026-09-05','2026-09-25','before 09-25 (pre-222 radiation 0.18/0.06)'),('2026-09-25','2026-10-01','09-25..09-30'),('2026-10-01','2026-10-09','≥10-01 (post-241 censoring)')): report(lab,[r for r in PM if lo<=r['event_date']<hi])
for u in ('C','F'): report(f'unit {u}',[r for r in PM if r['unit']==u])
print("\n== same station-day-band: P0 vs P0-M (the TAF/physics adjustment) ==")
pairs=[(d['P0'],d['P0-M']) for d in rows_by.values() if 'P0' in d and 'P0-M' in d and d['P0-M']['hours']<=36]
dm=[b['mu']-a['mu'] for a,b in pairs]; days=[a['event_date'] for a,b in pairs]
m,lo,hi=boot_mean(dm,days); print(f"  pairs n={len(pairs)} | P0-M mean − P0 mean {m:+.3f}°C [{lo:+.3f},{hi:+.3f}] | share warm-adjusted {np.mean([x>0.05 for x in dm]):.2f} cold-adjusted {np.mean([x<-0.05 for x in dm]):.2f}")
for lab,sel in (('adjusted warm (>+0.05)',lambda x:x>0.05),('adjusted cold (<−0.05)',lambda x:x<-0.05),('unchanged',lambda x:abs(x)<=0.05)):
    P=[(a,b) for (a,b),x in zip(pairs,dm) if sel(x)]
    if len(P)<40: continue
    bp0,l0,h0=boot_mean([a['tc']-a['mu'] for a,b in P],[a['event_date'] for a,b in P]); bpm,l1,h1=boot_mean([b['tc']-b['mu'] for a,b in P],[a['event_date'] for a,b in P])
    dls,dl,dh=boot_mean([b['ls']-a['ls'] for a,b in P],[a['event_date'] for a,b in P])
    print(f"  {lab:24s} n={len(P):4d} | P0 bias {bp0:+.3f} → P0-M bias {bpm:+.3f} [{l1:+.3f},{h1:+.3f}] | ΔLogScore P0-M−P0 {dls:+.4f} [{dl:+.4f},{dh:+.4f}] | mean adjustment {np.mean([b['mu']-a['mu'] for a,b in P]):+.2f}°C")
print("\n== P0-M ≤36h by station (bias in °C), stations with ≥40 decisions and interval excluding 0 ==")
st=collections.defaultdict(list)
for r in PM: st[r['station']].append(r)
for s,R in sorted(st.items(),key=lambda kv:np.mean([r['tc']-r['mu'] for r in kv[1]])):
    if len(R)<40: continue
    m,lo,hi=boot_mean([r['tc']-r['mu'] for r in R],[r['event_date'] for r in R])
    if lo>0 or hi<0: print(f"  {s} ({R[0]['unit']}) n={len(R)} bias {m:+.2f} [{lo:+.2f},{hi:+.2f}]")
print(f"  (stations ≥40: {sum(1 for R in st.values() if len(R)>=40)}; warm-biased share of all stations ≥40: {np.mean([np.mean([r['tc']-r['mu'] for r in R])<0 for R in st.values() if len(R)>=40]):.2f})")
print("\n== P1: the 0.6–0.7 bin over-claim, by band and local hour ==")
P1=[r for r in rows if r['stage']=='P1']
for lab,sel in (('all P1',lambda r:True),('h0_12',lambda r:r['band']=='h0_12'),('h12_24',lambda r:r['band']=='h12_24'),('local 06-12',lambda r:6<=r['lhour']<12),('local 12-18',lambda r:12<=r['lhour']<18),('local 18-24',lambda r:r['lhour']>=18)):
    said=[];landed=[]
    for r in P1:
        if not sel(r): continue
        for i,p in enumerate(r['f']):
            if 0.5<=p<0.75: said.append(p); landed.append(i==r['ti'])
    if len(said)>=50: print(f"  {lab:12s} buckets claiming 0.50–0.75: n={len(said)} said {np.mean(said):.3f} landed {np.mean(landed):.3f}")
