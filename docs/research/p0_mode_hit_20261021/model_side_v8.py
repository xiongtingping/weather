"""Model-side only: is the P0/P0-M centre over-dispersed (forecast-dependent
bias)?  Anchors are internal or climatological -- trailing truth means, the
previous day's truth, the cross-family median, the previous run -- never the
market.  Output model_side_v8.txt."""
import gzip, glob, json, math, os, sqlite3, statistics, collections, random
from datetime import date, timedelta
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
out = open(os.path.join(HERE, "model_side_v8.txt"), "w")
def P(*a): print(*a); print(*a, file=out)
conn = sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True); conn.execute("PRAGMA query_only=1")
truth = {}
for st, d, t in conn.execute("SELECT station_id, event_date, settled_tmax_c FROM basket_station_day_settlement_label l WHERE truth_revision_no=(SELECT MAX(truth_revision_no) FROM basket_station_day_settlement_label x WHERE x.station_id=l.station_id AND x.event_date=l.event_date)"):
    if t is not None: truth[(st, d)] = float(t)
conn.close()
def trailing(st, d, n):
    v = [truth.get((st, (date.fromisoformat(d)-timedelta(days=k)).isoformat())) for k in range(1, n+1)]
    v = [x for x in v if x is not None]; return statistics.mean(v) if len(v) >= max(3, n//2) else None
SRC = {"A": os.path.join(HERE, "..", "conditional_advantage_20261006", "rows"), "B": os.path.join(HERE, "rows")}
F_ST = {"KATL","KAUS","KBKF","KDAL","KDEN","KHOU","KLAX","KLGA","KMIA","KORD","KSEA","KSFO"}
SHORT = {"ECMWF_IFS_ENS_OPEN_PF50":"ifs","ECMWF_AIFS_ENS_GLOBAL":"aifs","DWD_ICON_GLOBAL_EPS":"icon","NOAA_GEFS_05":"gefs","ECCC_GEM_GLOBAL_ENS":"gem","NOAA_AIGEFS_OPS_025":"aigefs","NOAA_AIGEFS_025":"aigefs"}
recs = []
for win, dsrc in SRC.items():
    for f in sorted(glob.glob(os.path.join(dsrc, "*.jsonl.gz"))):
        bydays = collections.defaultdict(list)
        for line in gzip.open(f, "rt"):
            r = json.loads(line); bydays[r["d"]].append(r)
        for d, rows in bydays.items():
            rows.sort(key=lambda r: r["t"]); seen = set(); prev_mc = {}
            for r in rows:
                if r.get("tmax") is None or r.get("mc") is None or r.get("h") is None: continue
                stage = r["stage"]; fam = "P0>36h" if stage == "P0" and r["h"] > 36 else ("P0≤36h" if stage == "P0" else stage)
                mc = float(r["mc"]); y = float(r["tmax"])
                rec = dict(win=win, fam=fam, st=r["st"], d=d, h=r["h"], mc=mc, y=y, unit="F" if r["st"] in F_ST else "C", season=r.get("season"),
                           first=(fam not in seen), prev_mc=prev_mc.get(stage), n_sys=len(r.get("centres") or {}))
                seen.add(fam); prev_mc[stage] = mc
                rec["t7"] = trailing(r["st"], d, 7); rec["t30"] = trailing(r["st"], d, 30); rec["y1"] = truth.get((r["st"], (date.fromisoformat(d)-timedelta(days=1)).isoformat()))
                cen = {SHORT.get(k, k): float(v) for k, v in (r.get("centres") or {}).items() if isinstance(v, (int, float))}
                rec["cen"] = cen; rec["med"] = statistics.median(cen.values()) if len(cen) >= 3 else None
                rec["w"] = {SHORT.get(k, k): float(v) for k, v in (r.get("w") or {}).items()}
                # PIT under the served vector (uniform within a rung), on the ladder's temps
                p = [float(x or 0) for x in r["p"]]; temps = r["temps"]; s = sum(p)
                if s > 0 and temps and all(t is not None for t in temps[1:-1]):
                    p = [x/s for x in p]; rung = float(r.get("rung") or 1.0); pit = 0.0
                    for k, (pk, tk) in enumerate(zip(p, temps)):
                        if tk is None: continue
                        lo, hi = tk - rung/2, tk + rung/2
                        if k == 0: lo = -1e9
                        if k == len(p)-1: hi = 1e9
                        if y >= hi: pit += pk
                        elif y > lo: pit += pk * (y - lo) / (hi - lo) if hi > lo else pk*0.5
                    rec["pit"] = min(max(pit, 0.0), 1.0)
                recs.append(rec)
def ols(pairs):
    x = [a for a, b in pairs]; y = [b for a, b in pairs]; n = len(x)
    if n < 30: return None
    mx, my = sum(x)/n, sum(y)/n; sxx = sum((a-mx)**2 for a in x)
    if sxx <= 0: return None
    b = sum((a-mx)*(c-my) for a, c in zip(x, y))/sxx; a0 = my - b*mx
    se = math.sqrt(sum((c-(a0+b*a))**2 for a, c in zip(x, y))/(n-2)/sxx); return a0, b, se, n
def splits(rs): return {"A":[r for r in rs if r["win"]=="A"], "B":[r for r in rs if r["win"]=="B"], "奇":[r for r in rs if int(r["d"][-2:])%2], "偶":[r for r in rs if not int(r["d"][-2:])%2]}
def line(name, rs, fx, fy):
    pairs = [(fx(r), fy(r)) for r in rs if fx(r) is not None and fy(r) is not None]
    o = ols(pairs)
    if not o: P(f"   {name:40s} n<30"); return
    s4 = {k: ols([(fx(r), fy(r)) for r in v if fx(r) is not None and fy(r) is not None]) for k, v in splits(rs).items()}
    P(f"   {name:40s} n={o[3]:5d} 斜率 {o[1]:+.3f}±{o[2]:.3f} 截距 {o[0]:+.2f}°C  四分割斜率({' '.join(f'{k}{v[1]:+.2f}' for k,v in s4.items() if v)})")
for fam in ("P0>36h", "P0-M", "P1"):
    rs = [r for r in recs if r["fam"] == fam and r["first"]]
    P(f"\n\n==================== {fam} 首决策 n={len(rs)}（无条件残差 真值−模型中心 {statistics.mean(r['y']-r['mc'] for r in rs):+.3f}°C）====================")
    P("A. 残差 对 预报异常 的斜率（负＝预报异常越大真值越往回缩＝中心未收缩）")
    line("y−mc 对 (mc − 近7天真值均值)", rs, lambda r: None if r["t7"] is None else r["mc"]-r["t7"], lambda r: r["y"]-r["mc"])
    line("y−mc 对 (mc − 近30天真值均值)", rs, lambda r: None if r["t30"] is None else r["mc"]-r["t30"], lambda r: r["y"]-r["mc"])
    line("y−mc 对 (mc − 昨日真值)", rs, lambda r: None if r["y1"] is None else r["mc"]-r["y1"], lambda r: r["y"]-r["mc"])
    line("y−mc 对 (mc − 六家中心中位数)", rs, lambda r: None if r["med"] is None else r["mc"]-r["med"], lambda r: r["y"]-r["mc"])
    line("对照：真值异常 对 预报异常 (y−t7 对 mc−t7)", rs, lambda r: None if r["t7"] is None else r["mc"]-r["t7"], lambda r: None if r["t7"] is None else r["y"]-r["t7"])
    P("B. 按预报异常分层：残差均值与 PIT 均值（PIT 0.5 居中；<0.5 真值偏冷于模型）")
    have = [r for r in rs if r["t7"] is not None and r.get("pit") is not None]
    v = sorted(r["mc"]-r["t7"] for r in have); cuts = [v[len(v)//5], v[2*len(v)//5], v[3*len(v)//5], v[4*len(v)//5]]
    bins = [(-1e9, cuts[0]), (cuts[0], cuts[1]), (cuts[1], cuts[2]), (cuts[2], cuts[3]), (cuts[3], 1e9)]
    for lo, hi in bins:
        g = [r for r in have if lo < r["mc"]-r["t7"] <= hi]
        if len(g) < 30: continue
        P(f"   mc−t7 ∈ ({lo:+.1f},{hi:+.1f}]  n={len(g):4d}  残差均值 {statistics.mean(r['y']-r['mc'] for r in g):+.3f}°C  PIT均值 {statistics.mean(r['pit'] for r in g):.3f}  PIT sd {statistics.pstdev(r['pit'] for r in g):.3f}  |残差|均值 {statistics.mean(abs(r['y']-r['mc']) for r in g):.2f}")
    P("C. 单家：各家中心残差 对 (该家中心 − 其他家中位数) 的斜率（负＝孤狼错；该家校准缺斜率）")
    for sysn in ("ifs", "aifs", "icon", "gefs", "gem", "aigefs"):
        def fx(r, s=sysn):
            c = r["cen"]
            if s not in c or len(c) < 3: return None
            others = [v for k, v in c.items() if k != s]; return c[s] - statistics.median(others)
        line(f"  {sysn}: y−c 对 (c−其他中位数)", rs, fx, lambda r, s=sysn: (r["y"]-r["cen"][s]) if s in r["cen"] else None)
    P("   单家：各家中心残差 对 (该家中心 − 近30天真值均值)（负＝该家预报异常偏大）")
    for sysn in ("ifs", "aifs", "icon", "gefs", "gem", "aigefs"):
        line(f"  {sysn}: y−c 对 (c−t30)", rs, lambda r, s=sysn: (r["cen"][s]-r["t30"]) if s in r["cen"] and r["t30"] is not None else None, lambda r, s=sysn: (r["y"]-r["cen"][s]) if s in r["cen"] else None)
    P("D. 融合对照：服务中心 vs 六家中位数 vs 等权均值 的 RMSE（°C），按家数≥4 的决策")
    g = [r for r in rs if len(r["cen"]) >= 4]
    if g:
        rm = lambda f: math.sqrt(statistics.mean((f(r)-r["y"])**2 for r in g))
        P(f"   n={len(g)} 服务中心 {rm(lambda r: r['mc']):.3f}  中位数 {rm(lambda r: statistics.median(r['cen'].values())):.3f}  等权均值 {rm(lambda r: statistics.mean(r['cen'].values())):.3f}  去最远一家后均值 {rm(lambda r: statistics.mean(sorted(r['cen'].values(), key=lambda c: abs(c-statistics.median(r['cen'].values())))[:-1])):.3f}")
        dis = [r for r in g if max(r["cen"].values())-min(r["cen"].values()) > 2.0]
        if len(dis) >= 30:
            rm2 = lambda f: math.sqrt(statistics.mean((f(r)-r["y"])**2 for r in dis))
            P(f"   六家极差>2°C 的 {len(dis)} 决策: 服务中心 {rm2(lambda r: r['mc']):.3f}  中位数 {rm2(lambda r: statistics.median(r['cen'].values())):.3f}  等权 {rm2(lambda r: statistics.mean(r['cen'].values())):.3f}")
    P("E. 轮次间：y−mc 对 (mc − 同站日上一轮 mc)（非首决策；负＝新一轮越动越过头）")
    rs2 = [r for r in recs if r["fam"] == fam and r["prev_mc"] is not None and abs(r["mc"]-r["prev_mc"]) > 0.05]
    line("y−mc 对 (mc−prev_mc)", rs2, lambda r: r["mc"]-r["prev_mc"], lambda r: r["y"]-r["mc"])
    P("F. 分季节相位/单位：y−mc 对 (mc−t7) 斜率")
    for s in ("DESCENT", "ASCENT", "PEAK", "TROUGH"):
        line(f"  {s}", [r for r in rs if r.get("season") == s], lambda r: None if r["t7"] is None else r["mc"]-r["t7"], lambda r: r["y"]-r["mc"])
    for u in ("C", "F"):
        line(f"  {u} 站", [r for r in rs if r["unit"] == u], lambda r: None if r["t7"] is None else r["mc"]-r["t7"], lambda r: r["y"]-r["mc"])
