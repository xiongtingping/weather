"""Model-side candidates, walk-forward (fit on prior event days only):
 (1) fusion: shrink each family's centre toward the cross-family median by a
     family-specific factor kappa_f (the 'lone wolf' retention), pool with the
     served weights, shift the served vector to the new centre;
 (2) P1 update tempering: pull the newest centre back toward the previous
     decision's centre by kappa (the run-to-run overshoot).
Scored by centre RMSE, LogScore and CRPS of the shifted vector vs the served one."""
import gzip, glob, json, math, os, statistics, collections, random
HERE = os.path.dirname(os.path.abspath(__file__))
out = open(os.path.join(HERE, "model_side_v9.txt"), "w")
def P(*a): print(*a); print(*a, file=out)
SRC = {"A": os.path.join(HERE, "..", "conditional_advantage_20261006", "rows"), "B": os.path.join(HERE, "rows")}
SHORT = {"ECMWF_IFS_ENS_OPEN_PF50":"ifs","ECMWF_AIFS_ENS_GLOBAL":"aifs","DWD_ICON_GLOBAL_EPS":"icon","NOAA_GEFS_05":"gefs","ECCC_GEM_GLOBAL_ENS":"gem","NOAA_AIGEFS_OPS_025":"aigefs","NOAA_AIGEFS_025":"aigefs"}
def shift(p, delta):
    K = len(p); o = [0.0]*K
    for i, m in enumerate(p):
        lo = i + delta; a = math.floor(lo); f = lo - a
        for j, w in ((a, 1-f), (a+1, f)):
            if w <= 0: continue
            j = min(max(j, 0), K-1); o[j] += m*w
    return o
def floor_norm(p):
    s = sum(p); p = [max(x/s, 1e-3) for x in p]; s = sum(p); return [x/s for x in p]
def crps(p, temps, y, rung):
    # CRPS on the ladder with rung-centre temps (interior), ends as centres too
    c = 0.0; F = 0.0
    for k, (pk, tk) in enumerate(zip(p, temps)):
        F += pk; lo, hi = tk - rung/2, tk + rung/2
        H = 1.0 if y <= hi else 0.0
        c += (F - H)**2 * rung
    return c
recs = []
for win, dsrc in SRC.items():
    for f in sorted(glob.glob(os.path.join(dsrc, "*.jsonl.gz"))):
        bydays = collections.defaultdict(list)
        for line in gzip.open(f, "rt"):
            r = json.loads(line); bydays[r["d"]].append(r)
        for d, rows in bydays.items():
            rows.sort(key=lambda r: r["t"]); seen = set(); prev = {}
            for r in rows:
                if r.get("tmax") is None or r.get("mc") is None or r.get("h") is None or r["y"] not in r["names"]: continue
                temps = r["temps"]
                if not temps or any(t is None for t in temps): continue
                stage = r["stage"]; fam = "P0>36h" if stage == "P0" and r["h"] > 36 else ("P0≤36h" if stage == "P0" else stage)
                p = [float(x or 0) for x in r["p"]]
                if sum(p) <= 0: continue
                rec = dict(win=win, fam=fam, stage=stage, st=r["st"], d=d, mc=float(r["mc"]), y=float(r["tmax"]), yi=r["names"].index(r["y"]),
                           p=floor_norm(p), temps=temps, rung=float(r.get("rung") or 1.0), first=(fam not in seen), prev_mc=prev.get(stage),
                           cen={SHORT.get(k,k): float(v) for k, v in (r.get("centres") or {}).items() if isinstance(v,(int,float))},
                           w={SHORT.get(k,k): float(v) for k, v in (r.get("w") or {}).items()})
                seen.add(fam); prev[stage] = float(r["mc"]); recs.append(rec)
def score(rs, new_mc):
    """RMSE, LogScore, CRPS of the served vector shifted to new_mc(r), vs served."""
    out = {"n": 0, "rmse0": 0, "rmse1": 0, "ls0": 0, "ls1": 0, "crps0": 0, "crps1": 0}; dls = []; dday = collections.defaultdict(list)
    for r in rs:
        m1 = new_mc(r)
        if m1 is None: continue
        delta = (m1 - r["mc"]) / r["rung"]
        p1 = floor_norm(shift(r["p"], delta))
        ls0 = -math.log(r["p"][r["yi"]]); ls1 = -math.log(p1[r["yi"]])
        out["n"] += 1; out["rmse0"] += (r["mc"]-r["y"])**2; out["rmse1"] += (m1-r["y"])**2; out["ls0"] += ls0; out["ls1"] += ls1
        out["crps0"] += crps(r["p"], r["temps"], r["y"], r["rung"]); out["crps1"] += crps(p1, r["temps"], r["y"], r["rung"]); dday[r["d"]].append(ls1-ls0)
    n = out["n"]
    if n < 30: return None
    days = list(dday); rnd = random.Random(4); bs = []
    for _ in range(300):
        s = [z for d in (rnd.choice(days) for _ in days) for z in dday[d]]; bs.append(statistics.mean(s))
    bs.sort()
    return dict(n=n, rmse=(math.sqrt(out["rmse0"]/n), math.sqrt(out["rmse1"]/n)), ls=(out["ls0"]/n, out["ls1"]/n, bs[7], bs[292]), crps=(out["crps0"]/n, out["crps1"]/n))
def show(name, rows, fn):
    s = score(rows, fn)
    if not s: P(f"   {name:44s} n<30"); return
    P(f"   {name:44s} n={s['n']:5d} RMSE {s['rmse'][0]:.3f}→{s['rmse'][1]:.3f}  LogScore {s['ls'][0]:.4f}→{s['ls'][1]:.4f} (Δ {s['ls'][1]-s['ls'][0]:+.4f} [{s['ls'][2]:+.4f},{s['ls'][3]:+.4f}])  CRPS {s['crps'][0]:.3f}→{s['crps'][1]:.3f}")
# ---------- (1) fusion: deviation shrinkage toward the cross-family median, walk-forward kappa_f
P("\n==================== (1) 融合候选：各家中心向六家中位数按家族系数收缩（κ_f 只用之前事件日拟合）====================")
rs = [r for r in recs if r["fam"] == "P0>36h" and r["first"] and len(r["cen"]) >= 3 and r["w"]]
days = sorted({r["d"] for r in rs})
def fit_kappa(prior):
    acc = collections.defaultdict(lambda: [0.0, 0.0])
    for r in prior:
        med = statistics.median(r["cen"].values())
        for f, c in r["cen"].items():
            x = c - med; acc[f][0] += x*x; acc[f][1] += x*(r["y"]-med)
    return {f: (a[1]/a[0] if a[0] > 1e-9 else 1.0) for f, a in acc.items()}
def shrunk_mc(r, kap):
    med = statistics.median(r["cen"].values()); ws = {f: r["w"].get(f, 0.0) for f in r["cen"]}; tot = sum(ws.values())
    if tot <= 0: return None
    return sum(ws[f]/tot * (med + kap.get(f, 1.0)*(r["cen"][f]-med)) for f in r["cen"])
rows_eval = []
for i, d in enumerate(days):
    prior = [r for r in rs if r["d"] < d]
    if len({r["d"] for r in prior}) < 8: continue
    kap = fit_kappa(prior)
    for r in rs:
        if r["d"] == d: r["kap"] = kap; rows_eval.append(r)
P(f"   前推打分决策 {len(rows_eval)}；最后一天的 κ_f = " + ", ".join(f"{f} {k:+.2f}" for f, k in sorted(rows_eval[-1]['kap'].items())))
show("收缩后中心 vs 服务中心", rows_eval, lambda r: shrunk_mc(r, r["kap"]))
show("对照：六家中位数", rows_eval, lambda r: statistics.median(r["cen"].values()))
show("对照：等权均值", rows_eval, lambda r: statistics.mean(r["cen"].values()))
for k in ("A", "B"):
    show(f"  窗{k} 收缩后中心", [r for r in rows_eval if r["win"] == k], lambda r: shrunk_mc(r, r["kap"]))
dis = [r for r in rows_eval if max(r["cen"].values()) - min(r["cen"].values()) > 2.0]
show("  六家极差>2°C 的决策", dis, lambda r: shrunk_mc(r, r["kap"]))
# ---------- (2) update tempering, per stage, walk-forward kappa
P("\n==================== (2) 轮次更新回拉：mc′ = mc − κ·(mc − 上一轮 mc)，κ 只用之前事件日拟合 ====================")
for fam in ("P0>36h", "P0-M", "P1"):
    rs2 = [r for r in recs if r["fam"] == fam and r["prev_mc"] is not None and abs(r["mc"]-r["prev_mc"]) > 0.05]
    days2 = sorted({r["d"] for r in rs2}); ev = []
    for d in days2:
        prior = [r for r in rs2 if r["d"] < d]
        if len({r["d"] for r in prior}) < 8: continue
        sxx = sum((r["mc"]-r["prev_mc"])**2 for r in prior); sxy = sum((r["mc"]-r["prev_mc"])*(r["y"]-r["mc"]) for r in prior)
        kap = sxy/sxx if sxx > 0 else 0.0
        for r in rs2:
            if r["d"] == d: r["kap2"] = kap; ev.append(r)
    if not ev: continue
    P(f"   {fam}: 前推决策 {len(ev)}，最后一天 κ={ev[-1]['kap2']:+.3f}")
    show(f"  {fam} 回拉后", ev, lambda r: r["mc"] + r["kap2"]*(r["mc"]-r["prev_mc"]))
    for k in ("A", "B"):
        show(f"    窗{k}", [r for r in ev if r["win"] == k], lambda r: r["mc"] + r["kap2"]*(r["mc"]-r["prev_mc"]))
