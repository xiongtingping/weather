import json, statistics
rows=json.load(open("tmp/refute_h3_cases.json"))
for r in rows:
    r["exc"]=set(r["exc"]); c=r["cen"].get(r["target"]); r["d"]=None if c is None else c-r["rh"]

print("=== F1  renormalisation inflation (their 1.39x / 1.48x)")
for nm,sel in (("all",[r for r in rows if r["prior"]]),
               ("edge>0.25",[r for r in rows if r["prior"] and r["edge"]>.25]),
               ("edge<=0",[r for r in rows if r["prior"] and r["edge"]<=0])):
    cut=[sum(r["prior"].get(b,0.0) for b in r["exc"]) for r in sel]
    lift=[1/(1-c) for c in cut if c<1]
    tgt=[r["p"][r["target"]]/r["prior"][r["target"]] for r in sel if r["prior"].get(r["target"])]
    print("  %-10s n=%-3d cut_med %.0f%%  renorm %.2fx  target %.2fx" % (
        nm,len(sel),100*statistics.median(cut),statistics.median(lift),statistics.median(tgt)))

print("\n=== F2  can the layer reshape the >+1C region at all? (s>0 rows)")
same=0; tot=0
for r in rows:
    if r["s"]<=0: continue
    far=[float(r["lr"][b]) for b in r["p"] if b not in r["exc"]
         and r["cen"].get(b) is not None and r["cen"][b]-r["rh"]>1.0]
    if len(far)<2: continue
    tot+=1
    if max(far)-min(far) < 1e-12: same+=1
print("  rows where EVERY surviving bucket >+1C above running high got the identical")
print("  likelihood ratio (so it cancels in renormalisation): %d/%d" % (same,tot))

print("\n=== F3  the phenomenon is a clock phenomenon, not a strength phenomenon")
print("  H3's condition satisfied (s=0, identity likelihood, tail = stale P0 prior):")
for lo,hi in ((8,99),(3,8),(1,3),(0,1)):
    sel=[r for r in rows if r["s"]==0 and lo<=r["lead"]<hi]
    if not sel: continue
    print("    lead %2d-%-3dh  n=%-3d model %5.1f%%  actual %5.1f%%  over %5.2fx" % (
        lo,hi,len(sel),100*statistics.mean(r["p_t"] for r in sel),
        100*sum(r["won"] for r in sel)/len(sel),
        statistics.mean(r["p_t"] for r in sel)/max(sum(r["won"] for r in sel)/len(sel),1e-9)))
