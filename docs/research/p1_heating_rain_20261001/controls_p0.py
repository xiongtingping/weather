import sys, random
sys.path.insert(0, __file__.rsplit('/',1)[0])
import fut_wf as W
rows=W.load("p0")
rnd=random.Random(7); coin={}
def rand(f):
    k=id(f)
    if k not in coin: coin[k]=rnd.random()<0.40
    return -1 if coin[k] else 0
arms=[("对照:全部加宽",lambda f:-1),
      ("对照:随机40%加宽",rand),
      ("对照:预报晴(≥0.63)加宽",lambda f:-1 if f["kt"] is not None and f["kt"]>=W.T2 else 0),
      ("对照:预报中(0.50–0.63)加宽",lambda f:-1 if f["kt"] is not None and W.T1<f["kt"]<W.T2 else 0),
      ("主:预报阴(≤0.50)加宽",lambda f:-1 if f["kt"] is not None and f["kt"]<=W.T1 else 0)]
for nm,sg in arms: W.run(rows,nm,sg,"widen")
print("-- 方向")
W.run(rows,"反向:阴→暖/晴→冷",lambda f:0 if f["kt"] is None else (1 if f["kt"]<=W.T1 else (-1 if f["kt"]>=W.T2 else 0)),"shift")
W.run(rows,"只阴→冷",lambda f:-1 if f["kt"] is not None and f["kt"]<=W.T1 else 0,"shift")
W.run(rows,"只晴→暖",lambda f:1 if f["kt"] is not None and f["kt"]>=W.T2 else 0,"shift")
