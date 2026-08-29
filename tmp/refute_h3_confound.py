"""Is H3's named mechanism the discriminator, or is it confounded with the clock?"""
import json, math, statistics
from collections import Counter
rows = json.load(open("tmp/refute_h3_cases.json"))
for r in rows:
    r["exc"]=set(r["exc"]); r["held"]=set(r["held"])
    c=r["cen"].get(r["target"]); r["d"]= None if c is None else c-r["rh"]

def line(nm, sel):
    if not sel: return
    print("  %-30s n=%-3d lead_med %5.2fh  d_med %+.1fC  model %5.1f%%  ask %5.1f%%  actual %5.1f%%" % (
        nm, len(sel), statistics.median(r["lead"] for r in sel),
        statistics.median(r["d"] for r in sel if r["d"] is not None),
        100*statistics.mean(r["p_t"] for r in sel),
        100*statistics.mean(r["ask"] for r in sel),
        100*sum(r["won"] for r in sel)/len(sel)))

print("=== C1  strength on/off, and their lead")
line("s=0", [r for r in rows if r["s"]==0]); line("s>0", [r for r in rows if r["s"]>0])

print("\n=== C2  positive-edge rows only, split by strength  (their 0/12 claim)")
pos=[r for r in rows if r["edge"]>0]
line("edge>0, s=0", [r for r in pos if r["s"]==0]); line("edge>0, s>0", [r for r in pos if r["s"]>0])
print("   edge>0 & s>0 hits: %d/%d" % (sum(r["won"] for r in pos if r["s"]>0),
                                       sum(1 for r in pos if r["s"]>0)))

print("\n=== C3  control for the clock: within lead strata, does strength matter?")
for lo,hi in ((0,3),(3,8),(8,99)):
    st=[r for r in rows if lo<=r["lead"]<hi]
    print(" lead %d-%dh (n=%d)" % (lo,hi,len(st)))
    line("   s=0",[r for r in st if r["s"]==0]); line("   s>0",[r for r in st if r["s"]>0])

print("\n=== C4  control for strength: within s=0 only, does the clock matter?")
z=[r for r in rows if r["s"]==0]
for lo,hi in ((0,1),(1,3),(3,8),(8,99)):
    line("   s=0 lead %d-%dh"%(lo,hi), [r for r in z if lo<=r["lead"]<hi])

print("\n=== C5  the mechanism as a background constant (their 18/19 headline)")
for nm,f in (("<=0",lambda e:e<=0),("0~0.10",lambda e:0<e<=.10),
             ("0.10-0.25",lambda e:.10<e<=.25),(">0.25",lambda e:e>.25)):
    sel=[r for r in rows if f(r["edge"])]
    ab=[r for r in sel if r["d"] is not None and r["d"]>0]
    won_ab=sum(r["won"] for r in ab)
    print("  edge %-10s target-above-high %2d/%-2d (%3.0f%%)   of those, realized %2d (%4.1f%%)  model on those %4.1f%%" % (
        nm, len(ab), len(sel), 100*len(ab)/len(sel), won_ab,
        100*won_ab/max(len(ab),1), 100*statistics.mean([r["p_t"] for r in ab]) if ab else 0))

print("\n=== C6  does the observation-likelihood layer even have room? survivors' increments")
frac=[]
for r in rows:
    surv=[b for b in r["p"] if b not in r["exc"] and r["cen"].get(b) is not None]
    if not surv: continue
    far=[b for b in surv if r["cen"][b]-r["rh"]>1.0]
    m=sum(r["p"][b] for b in far)
    frac.append((m, len(far)/len(surv)))
print("  share of P1 posterior mass sitting >+1C above running high: median %.0f%%" %
      (100*statistics.median(x[0] for x in frac)))
print("  share of SURVIVING buckets that are >+1C above running high: median %.0f%%" %
      (100*statistics.median(x[1] for x in frac)))
print("  (every one of those gets the same clamped raw ratio 0.25 -> a common factor")
print("   that CANCELS in renormalisation; the layer cannot reshape that region at all)")

print("\n=== C7  is the tail 'inflated', or is the whole conditional distribution just flat?")
buk=[]
for r in rows:
    for b,v in r["p"].items():
        c=r["cen"].get(b)
        if c is None or b in r["exc"]: continue
        buk.append(dict(p=v,won=b==r["truth"],d=c-r["rh"],lead=r["lead"],s=r["s"]))
for lo,hi,nm in ((-99,0.001,"<=running high"),(0.001,1,"+0~1C"),(1,2,"+1~2C"),
                 (2,3,"+2~3C"),(3,5,"+3~5C"),(5,99,">+5C")):
    sel=[x for x in buk if lo<=x["d"]<hi]
    if not sel: continue
    print("  %-16s buckets=%-4d claimed %5.1f%%  actual %5.1f%%  ratio %.2f" % (
        nm,len(sel),100*statistics.mean(x["p"] for x in sel),
        100*sum(x["won"] for x in sel)/len(sel),
        statistics.mean(x["p"] for x in sel)/max(sum(x["won"] for x in sel)/len(sel),1e-9)))
