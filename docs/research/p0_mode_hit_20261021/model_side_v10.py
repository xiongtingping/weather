"""Model-side v10: (1) the deterministic global consensus F (as of each decision,
charter 267's readers) as a P0 centre feature -- slope, per system, and a
walk-forward shift gated by LogScore/CRPS/RMSE; (2) the 'two sides' proxies the
chain can see -- weighted share of family centres above the pool mean, pool
skewness -- as predictors of the residual.  Market never enters the fit; one
footnote line reports whether F explains the market's deviation."""
import gzip, glob, json, math, os, sqlite3, statistics, collections, random, sys
from datetime import date
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)
from bayes_basket_target.regional_point_lane import runs_for
from bayes_basket_target.deterministic_forecast_family import feature_at
HERE = os.path.dirname(os.path.abspath(__file__))
out = open(os.path.join(HERE, "model_side_v10.txt"), "w")
def P(*a): print(*a, flush=True); print(*a, file=out)
SRC = {"A": os.path.join(HERE, "..", "conditional_advantage_20261006", "rows"), "B": os.path.join(HERE, "rows")}
SHORT = {"ECMWF_IFS_ENS_OPEN_PF50":"ifs","ECMWF_AIFS_ENS_GLOBAL":"aifs","DWD_ICON_GLOBAL_EPS":"icon","NOAA_GEFS_05":"gefs","ECCC_GEM_GLOBAL_ENS":"gem","NOAA_AIGEFS_OPS_025":"aigefs","NOAA_AIGEFS_025":"aigefs"}
DET = {"ECMWF_AIFS_SINGLE_POINT":"aifs1","ECMWF_IFS_HRES_NATIVE_POINT":"hres","ECMWF_IFS_9KM_RELAY_POINT":"ifs9","NOAA_GFS_025_NATIVE_POINT":"gfs","NOAA_AIGFS_025_NATIVE_POINT":"aigfs","UKMO_GLOBAL_10KM_NATIVE_POINT":"ukmo","UKMO_GLOBAL_10KM_RELAY_POINT":"ukmo_r","CMA_GRAPES_GLOBAL_POINT":"cma","ECCC_GDPS_15KM_NATIVE_POINT":"gdps","DWD_ICON_GLOBAL_13KM_NATIVE_POINT":"icon_g","NOAA_GFS_SFLUX_13KM_NATIVE_POINT":"gfs_sflux","METEOFRANCE_ARPEGE_025_NATIVE_POINT":"arpege"}
CORE = ("aifs1","hres","ifs9","gfs","aigfs","ukmo","ukmo_r","cma","gdps","icon_g")
conn = sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True); conn.execute("PRAGMA query_only=1")
def floor_norm(p):
    s = sum(p); p = [max(x/s, 1e-3) for x in p]; s = sum(p); return [x/s for x in p]
def shift(p, delta):
    K = len(p); o = [0.0]*K
    for i, m in enumerate(p):
        lo = i + delta; a = math.floor(lo); f = lo - a
        for j, w in ((a, 1-f), (a+1, f)):
            if w <= 0: continue
            j = min(max(j, 0), K-1); o[j] += m*w
    return o
def crps(p, temps, y, rung):
    c = 0.0; F = 0.0
    for pk, tk in zip(p, temps):
        F += pk; c += (F - (1.0 if y <= tk + rung/2 else 0.0))**2 * rung
    return c
recs = []; runs_cache = {}
for win, dsrc in SRC.items():
    for f in sorted(glob.glob(os.path.join(dsrc, "*.jsonl.gz"))):
        bydays = collections.defaultdict(list)
        for line in gzip.open(f, "rt"):
            r = json.loads(line); bydays[r["d"]].append(r)
        for d, rows in bydays.items():
            rows.sort(key=lambda r: r["t"]); seen = set(); picked = []
            for r in rows:
                if r.get("tmax") is None or r.get("mc") is None or r.get("h") is None or r["y"] not in r["names"]: continue
                temps = r["temps"]
                if not temps or any(t is None for t in temps): continue
                fam = "P0>36h" if r["stage"] == "P0" and r["h"] > 36 else ("P0≤36h" if r["stage"] == "P0" else r["stage"])
                if fam in seen or fam == "P0≤36h": continue
                seen.add(fam); picked.append((fam, r))
            if not picked: continue
            st = picked[0][1]["st"]; tz = picked[0][1]["tz"]
            latest = max(r["t"] for _, r in picked)
            try:
                runs = runs_for(conn, station_id=st, event_date=d, as_of_utc=latest)
            except Exception as exc:
                runs = []
            for fam, r in picked:
                feats = feature_at(conn, station_id=st, event_date=d, as_of_utc=r["t"], settlement_timezone=tz, rows=runs) if runs else {}
                det = {DET[k]: v["day_max_c"] for k, v in feats.items() if k in DET}
                p = [float(x or 0) for x in r["p"]]
                if sum(p) <= 0: continue
                cen = {SHORT.get(k, k): float(v) for k, v in (r.get("centres") or {}).items() if isinstance(v, (int, float))}
                w = {SHORT.get(k, k): float(v) for k, v in (r.get("w") or {}).items()}
                mc = float(r["mc"]); y = float(r["tmax"])
                p_n = floor_norm(p); mean_t = sum(pk*tk for pk, tk in zip(p_n, temps)); var = sum(pk*(tk-mean_t)**2 for pk, tk in zip(p_n, temps))
                skew = (sum(pk*(tk-mean_t)**3 for pk, tk in zip(p_n, temps)) / var**1.5) if var > 1e-9 else 0.0
                above = sum(w.get(s, 0.0) for s, c in cen.items() if c > mc); tot = sum(w.get(s, 0.0) for s in cen)
                recs.append(dict(win=win, fam=fam, st=r["st"], d=d, t=r["t"], mc=mc, y=y, yi=r["names"].index(r["y"]), p=p_n, temps=temps,
                                 rung=float(r.get("rung") or 1.0), det=det, F=(statistics.median([det[s] for s in CORE if s in det]) if sum(1 for s in CORE if s in det) >= 3 else None),
                                 n_det=sum(1 for s in CORE if s in det), skew=skew, above=(above/tot if tot > 0 else None), n_sys=len(cen), unit=r.get("unit")))
conn.close()
import pickle; pickle.dump(recs, open(os.path.join(HERE, "model_side_v10_recs.pkl"), "wb"))
P(f"决策 {len(recs)}；有 F（≥3 家确定性）的 {sum(1 for r in recs if r['F'] is not None)}")
def ols(pairs):
    x = [a for a, b in pairs]; y = [b for a, b in pairs]; n = len(x)
    if n < 30: return None
    mx, my = sum(x)/n, sum(y)/n; sxx = sum((a-mx)**2 for a in x)
    if sxx <= 0: return None
    b = sum((a-mx)*(c-my) for a, c in zip(x, y))/sxx; a0 = my - b*mx
    se = math.sqrt(sum((c-(a0+b*a))**2 for a, c in zip(x, y))/(n-2)/sxx); return a0, b, se, n
def splits(rs): return {"A":[r for r in rs if r["win"]=="A"], "B":[r for r in rs if r["win"]=="B"], "奇":[r for r in rs if int(r["d"][-2:])%2], "偶":[r for r in rs if not int(r["d"][-2:])%2]}
def line(name, rs, fx, fy):
    pairs = [(fx(r), fy(r)) for r in rs if fx(r) is not None and fy(r) is not None]; o = ols(pairs)
    if not o: P(f"   {name:44s} n<30"); return
    s4 = {k: ols([(fx(r), fy(r)) for r in v if fx(r) is not None and fy(r) is not None]) for k, v in splits(rs).items()}
    P(f"   {name:44s} n={o[3]:5d} 斜率 {o[1]:+.3f}±{o[2]:.3f} 截距 {o[0]:+.2f}  四分割({' '.join(f'{k}{v[1]:+.2f}' for k,v in s4.items() if v)})")
def score(rs, new_mc):
    o = {"n":0,"r0":0,"r1":0,"l0":0,"l1":0,"c0":0,"c1":0}; dday = collections.defaultdict(list)
    for r in rs:
        m1 = new_mc(r)
        if m1 is None: continue
        p1 = floor_norm(shift(r["p"], (m1-r["mc"])/r["rung"]))
        l0 = -math.log(r["p"][r["yi"]]); l1 = -math.log(p1[r["yi"]])
        o["n"] += 1; o["r0"] += (r["mc"]-r["y"])**2; o["r1"] += (m1-r["y"])**2; o["l0"] += l0; o["l1"] += l1
        o["c0"] += crps(r["p"], r["temps"], r["y"], r["rung"]); o["c1"] += crps(p1, r["temps"], r["y"], r["rung"]); dday[r["d"]].append(l1-l0)
    n = o["n"]
    if n < 30: return None
    days = list(dday); rnd = random.Random(4); bs = []
    for _ in range(300):
        s = [z for d in (rnd.choice(days) for _ in days) for z in dday[d]]; bs.append(statistics.mean(s))
    bs.sort(); return dict(n=n, rmse=(math.sqrt(o["r0"]/n), math.sqrt(o["r1"]/n)), ls=(o["l0"]/n, o["l1"]/n, bs[7], bs[292]), crps=(o["c0"]/n, o["c1"]/n))
def show(name, rs, fn):
    s = score(rs, fn)
    if not s: P(f"   {name:44s} n<30"); return
    P(f"   {name:44s} n={s['n']:5d} RMSE {s['rmse'][0]:.3f}→{s['rmse'][1]:.3f}  LogScore {s['ls'][0]:.4f}→{s['ls'][1]:.4f} (Δ {s['ls'][1]-s['ls'][0]:+.4f} [{s['ls'][2]:+.4f},{s['ls'][3]:+.4f}])  CRPS {s['crps'][0]:.3f}→{s['crps'][1]:.3f}")
for fam in ("P0>36h", "P0-M", "P1"):
    rs = [r for r in recs if r["fam"] == fam and r["F"] is not None]
    P(f"\n==================== {fam} 首决策，有确定性共识 F 的 n={len(rs)}（F 家数均值 {statistics.mean(r['n_det'] for r in rs):.1f}） ====================")
    P("A. 诊断斜率：残差 (y−mc) 对 (F−mc)；对照 F 自身误差")
    line("y−mc 对 (F−mc)", rs, lambda r: r["F"]-r["mc"], lambda r: r["y"]-r["mc"])
    P(f"   F 无条件偏差 (y−F) {statistics.mean(r['y']-r['F'] for r in rs):+.3f}°C，RMSE F {math.sqrt(statistics.mean((r['F']-r['y'])**2 for r in rs)):.3f} vs 池中心 {math.sqrt(statistics.mean((r['mc']-r['y'])**2 for r in rs)):.3f} vs 等权(F+mc)/2 {math.sqrt(statistics.mean(((r['F']+r['mc'])/2-r['y'])**2 for r in rs)):.3f}")
    for s in CORE:
        line(f"  单家 {s}: y−mc 对 (c−mc)", [r for r in rs if s in r["det"]], lambda r, s=s: r["det"][s]-r["mc"], lambda r: r["y"]-r["mc"])
    P("B. 前推候选：mc′ = mc + κ·(F_cal − mc)，F_cal = F + 之前事件日的均值偏差，κ 只用之前事件日拟")
    days = sorted({r["d"] for r in rs}); ev = []
    for d in days:
        prior = [r for r in rs if r["d"] < d]
        if len({r["d"] for r in prior}) < 8: continue
        bias = statistics.mean(r["y"]-r["F"] for r in prior)
        pairs = [(r["F"]+bias-r["mc"], r["y"]-r["mc"]) for r in prior]; o = ols(pairs); kap = o[1] if o else 0.0
        for r in rs:
            if r["d"] == d: r["kap"] = kap; r["bias"] = bias; ev.append(r)
    if ev:
        P(f"   前推决策 {len(ev)}；末日 κ={ev[-1]['kap']:+.3f}，F 偏差修正 {ev[-1]['bias']:+.2f}°C")
        show("κ·(F_cal−mc) 平移后", ev, lambda r: r["mc"] + r["kap"]*(r["F"]+r["bias"]-r["mc"]))
        show("固定 κ=0.5 (F_cal 与池中心对半)", ev, lambda r: r["mc"] + 0.5*(r["F"]+r["bias"]-r["mc"]))
        show("固定 κ=1 (直接用 F_cal)", ev, lambda r: r["F"]+r["bias"])
        for k in ("A", "B"):
            show(f"  窗{k} κ 平移后", [r for r in ev if r["win"] == k], lambda r: r["mc"] + r["kap"]*(r["F"]+r["bias"]-r["mc"]))
        show("  |F_cal−mc|>1°C 的决策", [r for r in ev if abs(r["F"]+r["bias"]-r["mc"]) > 1.0], lambda r: r["mc"] + r["kap"]*(r["F"]+r["bias"]-r["mc"]))
    P("C. 两侧代理（模型内部）：池偏度、权重在池均值暖侧的份额 → 残差")
    line("y−mc 对 池偏度", rs, lambda r: r["skew"], lambda r: r["y"]-r["mc"])
    line("y−mc 对 (暖侧权重份额−0.5)", rs, lambda r: None if r["above"] is None else r["above"]-0.5, lambda r: r["y"]-r["mc"])
    line("y−mc 对 (F−mc)·[F 比 mc 暖]", rs, lambda r: max(r["F"]-r["mc"], 0.0), lambda r: r["y"]-r["mc"])
    line("y−mc 对 (F−mc)·[F 比 mc 冷]", rs, lambda r: min(r["F"]-r["mc"], 0.0), lambda r: r["y"]-r["mc"])
