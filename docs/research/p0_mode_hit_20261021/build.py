#!/usr/bin/env python3
"""P0 mode bucket vs market mode bucket, truth hit, with per-decision features.

Unit: per station-day x horizon band, the first P0 decision whose market vector
is complete.  Market vector = market_reference_v1 normalized mid (as extracted).
Windows: A = 09-12..10-05 (conditional_advantage rows), B = 10-06..10-20 (rows/).
Output: recs.jsonl
"""
import gzip, glob, json, math, os, statistics
HERE = os.path.dirname(os.path.abspath(__file__))
SRC = {"A": os.path.join(HERE, "..", "conditional_advantage_20261006", "rows"),
       "B": os.path.join(HERE, "rows")}
BANDS = [(60, 1e9, "60h+"), (48, 60, "48-60h"), (36, 48, "36-48h"), (24, 36, "24-36h"), (12, 24, "12-24h"), (-1e9, 12, "<12h")]
F_ST = {"KATL","KAUS","KBKF","KDAL","KDEN","KHOU","KLAX","KLGA","KMIA","KORD","KSEA","KSFO"}

def band(h):
    for lo, hi, n in BANDS:
        if lo <= h < hi: return n

def mom(v, t):
    s = sum(v); m = sum(a*b for a, b in zip(v, t))/s
    return m, math.sqrt(max(sum(a*(b-m)**2 for a, b in zip(v, t))/s, 0))

def ent(v):
    return -sum(x*math.log(x) for x in v if x > 0)

out = open(os.path.join(HERE, "recs.jsonl"), "w")
n = 0
for win, d in SRC.items():
    for f in sorted(glob.glob(os.path.join(d, "*.jsonl.gz"))):
        seen = set()
        for line in gzip.open(f, "rt"):
            r = json.loads(line)
            if r["stage"] != "P0" or r.get("q") is None or r.get("h") is None: continue
            if r["y"] not in r["names"]: continue
            b = band(r["h"]); key = (r["d"], b)
            if key in seen: continue
            p = [float(x or 0) for x in r["p"]]; q = [float(x or 0) for x in r["q"]]
            if sum(p) <= 0 or sum(q) <= 0: continue
            sp, sq = sum(p), sum(q); p = [x/sp for x in p]; q = [x/sq for x in q]
            seen.add(key)
            K = len(p); idx = list(range(K)); yi = r["names"].index(r["y"])
            pm = max(idx, key=lambda i: p[i]); qm = max(idx, key=lambda i: q[i])
            mp, sdp = mom(p, idx); mq, sdq = mom(q, idx)
            cen = r.get("centres") or {}
            rung = r.get("rung") or 1.0
            # system centres in bucket units relative to market mean temp
            temps = r["temps"]
            valid = [(i, t) for i, t in enumerate(temps) if t is not None]
            # map temp -> continuous ordinal by linear fit over interior buckets
            def t2o(tc):
                inner = valid[1:-1] if len(valid) > 3 else valid
                (i0, t0), (i1, t1) = inner[0], inner[-1]
                return i0 + (tc - t0) * (i1 - i0) / (t1 - t0)
            sysc = [t2o(c) for c in cen.values() if isinstance(c, (int, float))]
            sys_sd = statistics.pstdev(sysc) if len(sysc) > 1 else None
            side = (1 if pm > qm else -1) if pm != qm else 0
            sys_side_frac = (sum(1 for c in sysc if (c - mq) * side > 0) / len(sysc)) if (sysc and side) else None
            # system modes vs market mode
            ss = r.get("sys_stats") or {}
            bk = r["book"]
            def sprd(i):
                bid, ask = bk[i][0], bk[i][1]
                return (ask - bid) if (bid is not None and ask is not None) else None
            rec = dict(win=win, st=r["st"], d=r["d"], band=b, h=round(r["h"], 2), pv=r["pv"],
                       unit="F" if r["st"] in F_ST else "C", season=r.get("season"), lead=r.get("lead"),
                       K=K, yi=yi, pm=pm, qm=qm, dmode=pm - qm,
                       p_hit=int(pm == yi), q_hit=int(qm == yi),
                       p_pm=p[pm], q_qm=q[qm], q_at_pm=q[pm], p_at_qm=p[qm],
                       q_second=sorted(q)[-2], p_second=sorted(p)[-2],
                       mean_p=mp, mean_q=mq, dmu=mp - mq, sd_p=sdp, sd_q=sdq,
                       ent_p=ent(p), ent_q=ent(q),
                       n_sys=len(sysc), sys_sd=sys_sd, sys_side_frac=sys_side_frac,
                       qm_edge=int(qm in (0, K-1)), pm_edge=int(pm in (0, K-1)),
                       spread_qm=sprd(qm), spread_pm=sprd(pm), maxsp=r.get("maxsp"), ms=r.get("ms"),
                       bridge_gap=(r.get("bridge") or {}).get("center_gap_c"),
                       clear=(r.get("cooling") or {}).get("clearness_index"),
                       clear_anom=None, t=r["t"], ytrue_minus_qm=yi - qm, ytrue_minus_pm=yi - pm,
                       logp=math.log(max(p[yi], 1e-3)), logq=math.log(max(q[yi], 1e-3)))
            c = r.get("cooling") or {}
            if c.get("clearness_index") is not None and c.get("history_clearness_mean") is not None and c.get("history_clearness_sd"):
                rec["clear_anom"] = (c["clearness_index"] - c["history_clearness_mean"]) / c["history_clearness_sd"]
            # hours since newest arrival
            arr = [v for v in (r.get("arrival") or {}).values() if isinstance(v, str)]
            if arr:
                from datetime import datetime
                ta = max(datetime.fromisoformat(a.replace("Z", "+00:00")) for a in arr)
                td = datetime.fromisoformat(r["t"].replace("Z", "+00:00"))
                rec["age_h"] = (td - ta).total_seconds()/3600
            out.write(json.dumps(rec) + "\n"); n += 1
print("recs", n)
