import sys, random
sys.path.insert(0, __file__.rsplit('/',1)[0])
import fut_wf as W
rows=W.load("p0")
rnd=random.Random(11); coin={}
def rand(f):
    k=id(f)
    if k not in coin: coin[k]=rnd.random()<0.26
    return -1 if coin[k] else 0
for nm,sg in [("对照:全部→冷",lambda f:-1),("对照:随机26%→冷",rand),
              ("对照:预报晴→冷",lambda f:-1 if f["kt"] is not None and f["kt"]>=W.T2 else 0),
              ("对照:预报中→冷",lambda f:-1 if f["kt"] is not None and W.T1<f["kt"]<W.T2 else 0),
              ("主:预报阴→冷",lambda f:-1 if f["kt"] is not None and f["kt"]<=W.T1 else 0),
              ("主:预报很阴(≤0.40)→冷",lambda f:-1 if f["kt"] is not None and f["kt"]<=0.40 else 0)]:
    W.run(rows,nm,sg,"shift")
