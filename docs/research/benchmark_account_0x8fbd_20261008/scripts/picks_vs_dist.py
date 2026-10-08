import json, collections, statistics as st
F=json.load(open("fills_full.json"))
def stats(q):
    # q: dict ord->prob (normalized). returns mode, mean, median, qmode
    items=sorted(q.items()); tot=sum(v for _,v in items)
    mode=max(items,key=lambda kv:(kv[1],-kv[0]))[0]; mean=sum(o*v for o,v in items)/tot
    c=0; med=items[-1][0]
    for o,v in items:
        c+=v
        if c>=tot/2: med=o; break
    return mode,mean,med,q[mode]/tot
rows=[]
for r in F:
    m=r["market"]
    if not m or r["my_ord"] is None: continue
    q={int(k):v for k,v in m["q"].items()}
    if r["my_ord"] not in q: continue
    mk_mode,mk_mean,mk_med,qmode=stats(q)
    rec=dict(r=r,mk_mode=mk_mode,mk_mean=mk_mean,mk_med=mk_med,qmode=qmode,q_my=q[r["my_ord"]],d=r["my_ord"]-mk_mode)
    for stage in ("P0","P0-M","P1"):
        if stage in r["model"]:
            p={int(k):v for k,v in r["model"][stage]["p"].items()}
            mo,me,md,pm=stats(p); rec[stage]=dict(mode=mo,mean=me,med=md,p_my=p.get(r["my_ord"]),p_mode=p.get(mk_mode),pm=pm)
    # latest stage available
    rec["latest"]=next((rec[s] for s in ("P1","P0-M","P0") if s in rec),None)
    rows.append(rec)
print("fills with healthy market & bucket:",len(rows))
def share(rs,pred):
    w=sum(x["r"]["size"] for x in rs); return sum(x["r"]["size"] for x in rs if pred(x))/w if w else float('nan'), len(rs)
Y=[x for x in rows if x["r"]["outcome"]=="Yes"]; N=[x for x in rows if x["r"]["outcome"]=="No"]
nb=[x for x in Y if abs(x["d"])==1]
print(f"\n1. YES neighbour buys (|d|=1): n={len(nb)} | share on mode+1 = {share(nb,lambda x:x['d']>0)[0]:.3f}")
for name,key in (("market mean side (mean-mode)",lambda x:x["mk_mean"]-x["mk_mode"]),("market median side (med-mode)",lambda x:x["mk_med"]-x["mk_mode"]),
                 ("our P0 mean - market mean",lambda x:x["P0"]["mean"]-x["mk_mean"] if "P0" in x else 0),("our latest-stage mean - market mean",lambda x:x["latest"]["mean"]-x["mk_mean"] if x["latest"] else 0),
                 ("our P0 mode - market mode",lambda x:x["P0"]["mode"]-x["mk_mode"] if "P0" in x else 0)):
    pos=[x for x in nb if key(x)>0.05]; neg=[x for x in nb if key(x)<-0.05]; zero=[x for x in nb if abs(key(x))<=0.05]
    sp,n1=share(pos,lambda x:x["d"]>0); sn,n2=share(neg,lambda x:x["d"]>0)
    agree=share(pos+neg,lambda x:(x["d"]>0)==(key(x)>0))[0]
    print(f"   {name:36s}: P(pick +1 | feature>0)={sp:.3f} (n={n1}) | P(pick +1 | feature<0)={sn:.3f} (n={n2}) | zero n={len(zero)} | pick on feature side: {agree:.3f}")
# hit rates of their neighbour picks by whether on market-mean side
def hitshare(rs):
    rs=[x for x in rs if x["r"]["truth_ord"] is not None]; w=sum(x["r"]["size"] for x in rs)
    return (sum(x["r"]["size"] for x in rs if x["r"]["my_ord"]==x["r"]["truth_ord"])/w if w else float('nan')), (sum(x["r"]["size"]*x["r"]["price"] for x in rs)/w if w else float('nan')), len(rs)
for name,pred in (("on market-mean side",lambda x:(x["d"]>0)==(x["mk_mean"]>x["mk_mode"])),("against market-mean side",lambda x:(x["d"]>0)!=(x["mk_mean"]>x["mk_mode"])),
                  ("on our-P0-mean side",lambda x:"P0" in x and (x["d"]>0)==(x["P0"]["mean"]>x["mk_mean"])),("against our-P0-mean side",lambda x:"P0" in x and (x["d"]>0)!=(x["P0"]["mean"]>x["mk_mean"]))):
    h,p,n=hitshare([x for x in nb if pred(x)]); print(f"   YES neighbour {name:26s}: hit {h:.3f} paid {p:.3f} ret {(h/p-1)*100:+.1f}% n={n}")
# 2. NO on mode: q_mode and model p_mode
nm=[x for x in N if x["d"]==0]
print(f"\n2. NO on market mode: n={len(nm)} | mean q_mode {st.mean(x['qmode'] for x in nm):.3f} | q_mode quartiles {[round(sorted(x['qmode'] for x in nm)[int(k*(len(nm)-1))],3) for k in (.25,.5,.75)]}")
allq=[x["qmode"] for x in rows]; print(f"   q_mode at ALL their fills (any side): mean {st.mean(allq):.3f} quartiles {[round(sorted(allq)[int(k*(len(allq)-1))],3) for k in (.25,.5,.75)]}")
withp=[x for x in nm if "P0" in x and x["P0"]["p_mode"] is not None]
print(f"   our P0 p(mode) - q_mode at their NO-on-mode fills: mean {st.mean(x['P0']['p_mode']-x['qmode'] for x in withp):+.3f} | share with P0 p_mode < q_mode: {sum(1 for x in withp if x['P0']['p_mode']<x['qmode'])/len(withp):.3f}")
h,p,n=hitshare(nm); print(f"   NO-on-mode hit(NO wins) {h:.3f} paid {p:.3f} ret {(h/p-1)*100:+.1f}%  [hit here = mode lost]")
# by q_mode band
for a,b in ((0,0.35),(0.35,0.45),(0.45,0.55),(0.55,1)):
    rs=[x for x in nm if a<=x["qmode"]<b]
    if len(rs)<30: continue
    w=sum(x["r"]["size"] for x in rs); lost=sum(x["r"]["size"] for x in rs if x["r"]["truth_ord"] is not None and x["r"]["truth_ord"]!=x["mk_mode"])/sum(x["r"]["size"] for x in rs if x["r"]["truth_ord"] is not None)
    print(f"     q_mode {a:.2f}-{b:.2f}: n={len(rs)} NO paid {sum(x['r']['size']*x['r']['price'] for x in rs)/w:.3f} mode lost {lost:.3f}")
# 3. YES price vs market q and model p
print(f"\n3. YES buys: paid - q_my mean {st.mean(x['r']['price']-x['q_my'] for x in Y):+.3f} | P0 p_my - q_my mean {st.mean(x['P0']['p_my']-x['q_my'] for x in Y if 'P0' in x and x['P0']['p_my'] is not None):+.3f}")
# 4. where do their YES buys sit relative to market mean/median exactly (ordinal distance to mean bucket)
dm=collections.Counter(); w=0
for x in Y:
    k=round(x["r"]["my_ord"]-x["mk_mean"]); dm[max(-3,min(3,k))]+=x["r"]["size"]; w+=x["r"]["size"]
print("4. YES bucket minus market MEAN bucket (rounded), share%:",{k:round(100*v/w,1) for k,v in sorted(dm.items())})
dm=collections.Counter(); w=0
for x in Y:
    k=x["r"]["my_ord"]-x["mk_med"]; dm[max(-3,min(3,k))]+=x["r"]["size"]; w+=x["r"]["size"]
print("   YES bucket minus market MEDIAN bucket, share%:",{k:round(100*v/w,1) for k,v in sorted(dm.items())})
dm=collections.Counter(); w=0
for x in Y:
    if "P0" not in x: continue
    k=round(x["r"]["my_ord"]-x["P0"]["mean"]); dm[max(-3,min(3,k))]+=x["r"]["size"]; w+=x["r"]["size"]
print("   YES bucket minus OUR P0 MEAN bucket (rounded), share%:",{k:round(100*v/w,1) for k,v in sorted(dm.items())})
# 5. market skew magnitude at their fills vs typical: |mean-mode|
print(f"5. |market mean - mode| at their YES fills: mean {st.mean(abs(x['mk_mean']-x['mk_mode']) for x in Y):.3f}; at NO fills {st.mean(abs(x['mk_mean']-x['mk_mode']) for x in N):.3f}")
