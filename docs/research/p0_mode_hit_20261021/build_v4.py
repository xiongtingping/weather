#!/usr/bin/env python3
"""v4: rung-level and window-level records.  For each first-band decision
(P0>36h / P0-M / P1), every rung k: offset from market mode, p, q, ask, bid,
hit; plus window records (pairs/triples around model mode, market mode, and
the contested pair).  Also fresh-run flags."""
import gzip, glob, json, math, os, collections
from datetime import datetime
HERE = os.path.dirname(os.path.abspath(__file__))
SRC = {"A": os.path.join(HERE, "..", "conditional_advantage_20261006", "rows"), "B": os.path.join(HERE, "rows")}
BANDS = [(60, 1e9, "60h+"), (48, 60, "48-60h"), (36, 48, "36-48h"), (24, 36, "24-36h"), (12, 24, "12-24h"), (6, 12, "6-12h"), (-1e9, 6, "<6h")]
F_ST = {"KATL","KAUS","KBKF","KDAL","KDEN","KHOU","KLAX","KLGA","KMIA","KORD","KSEA","KSFO"}
def band(h):
    for lo, hi, n in BANDS:
        if lo <= h < hi: return n
def iso(s): return datetime.fromisoformat(s.replace("Z", "+00:00"))
def norm(v):
    v = [float(x or 0) for x in v]; s = sum(v); return [x/s for x in v] if s > 0 else None
rung_out = open(os.path.join(HERE, "rungs_v4.jsonl"), "w"); win_out = open(os.path.join(HERE, "windows_v4.jsonl"), "w")
nr = nw = 0
for win, dsrc in SRC.items():
    for f in sorted(glob.glob(os.path.join(dsrc, "*.jsonl.gz"))):
        bydays = collections.defaultdict(list)
        for line in gzip.open(f, "rt"):
            r = json.loads(line); bydays[r["d"]].append(r)
        for d, rows in bydays.items():
            rows.sort(key=lambda r: r["t"]); seen = set(); prev_pm = {}
            for r in rows:
                if r.get("q") is None or r.get("h") is None or r["y"] not in r["names"]: continue
                p, q = norm(r["p"]), norm(r["q"])
                if not p or not q: continue
                K = len(p); yi = r["names"].index(r["y"]); pm = max(range(K), key=lambda i: p[i]); qm = max(range(K), key=lambda i: q[i])
                stage = r["stage"]; fam = "P0>36h" if stage == "P0" and r["h"] > 36 else ("P0≤36h" if stage == "P0" else stage)
                key = (fam, band(r["h"]))
                # fresh-run flags (relative to previous decision of same stage in this station-day)
                mode_changed = int(prev_pm.get(stage) is not None and prev_pm[stage] != pm); prev_pm[stage] = pm
                arr = [v for v in (r.get("arrival") or {}).values() if isinstance(v, str)]
                age_h = (iso(r["t"]) - max(iso(a) for a in arr)).total_seconds()/3600 if arr else None
                if key in seen: continue
                seen.add(key)
                bk = r["book"]
                base = dict(win=win, st=r["st"], d=d, fam=fam, band=key[1], h=round(r["h"], 2), unit="F" if r["st"] in F_ST else "C", K=K, yi=yi, pm=pm, qm=qm,
                            agree=int(pm == qm), mode_changed=mode_changed, age_h=age_h, q_qm=q[qm], p_pm=p[pm])
                for k in range(K):
                    bid, ask = (bk[k][0], bk[k][1]) if bk[k] else (None, None)
                    off = k - qm; pos = "tail_lo" if k == 0 else ("tail_hi" if k == K-1 else str(max(-3, min(3, off))))
                    rec = dict(base, k=k, off=off, pos=pos, offm=k-pm, p=p[k], q=q[k], ask=ask, bid=bid, hit=int(k == yi), lr=math.log(max(p[k],1e-3)/max(q[k],1e-3)))
                    rung_out.write(json.dumps(rec)+"\n"); nr += 1
                # windows
                def window(name, ks):
                    ks = [k for k in ks if 0 <= k < K]
                    if not ks: return
                    asks = [bk[k][1] for k in ks if bk[k] and bk[k][1] is not None]
                    if len(asks) != len(ks): return
                    rec = dict(base, wname=name, ks=ks, M=sum(p[k] for k in ks), Q=sum(q[k] for k in ks), A=sum(asks), hit=int(yi in ks))
                    win_out.write(json.dumps(rec)+"\n")
                window("pm±0", [pm]); window("pm±1", [pm-1, pm, pm+1]); window("pm,pm+1", [pm, pm+1]); window("pm-1,pm", [pm-1, pm])
                window("qm±1", [qm-1, qm, qm+1]); window("pair{pm,qm}", sorted({pm, qm}))
                # model side: model mode and the bucket beyond it away from the market
                if pm != qm:
                    s = 1 if pm > qm else -1; window("pm+beyond", [pm, pm+s]); window("pm..qm", list(range(min(pm,qm), max(pm,qm)+1)))
                    window("beyond_only", [pm+s])
                nw += 1
print("rungs", nr, "windows", nw)
