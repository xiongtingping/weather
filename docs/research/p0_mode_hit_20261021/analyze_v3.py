import json, os, collections, statistics, random, math
HERE = os.path.dirname(os.path.abspath(__file__))
R = [json.loads(l) for l in open(os.path.join(HERE, "recs_v3.jsonl"))]
for r in R: r["odd"] = (int(r["d"][-2:]) + (31 if r["d"][5:7]=="10" else 0)) % 2
out = open(os.path.join(HERE, "analyze_v3.txt"), "w")
def P(*a): print(*a); print(*a, file=out)
def rate(rows, k): return sum(r[k] for r in rows)/len(rows)
def diff(rows): return rate(rows,"p_hit")-rate(rows,"q_hit")
def dlog(rows): return statistics.mean(r["d_log"] for r in rows)
def edge(rows):
    v = [r["p_hit"]-r["ask_pm"] for r in rows if r.get("ask_pm") is not None]; return statistics.mean(v) if v else float("nan")
def boot(rows, f, B=300):
    days = sorted({r["d"] for r in rows}); by = collections.defaultdict(list)
    for r in rows: by[r["d"]].append(r)
    rnd = random.Random(1); v = []
    for _ in range(B):
        s = [x for d in (rnd.choice(days) for _ in days) for x in by[d]]; v.append(f(s))
    v.sort(); return v[int(.025*B)], v[int(.975*B)]
def splits(rows): return {"A":[r for r in rows if r["win"]=="A"], "B":[r for r in rows if r["win"]=="B"], "奇":[r for r in rows if r["odd"]], "偶":[r for r in rows if not r["odd"]]}
def cell(name, rows, show_d=True):
    if len(rows) < 15: P(f"   {name:34s} n<15"); return
    s4 = splits(rows); lo, hi = boot(rows, diff); dl, dh = boot(rows, dlog)
    P(f"   {name:34s} n={len(rows):4d} 模型{rate(rows,'p_hit'):5.1%} 市{rate(rows,'q_hit'):5.1%} 差{diff(rows):+6.1%}[{lo:+.0%},{hi:+.0%}] 四分割({' '.join(f'{k}{diff(v):+.0%}' for k,v in s4.items() if len(v)>=8)}) | d=log p/q {dlog(rows):+.3f}[{dl:+.2f},{dh:+.2f}] ({' '.join(f'{k}{dlog(v):+.2f}' for k,v in s4.items() if len(v)>=8)}) | 卖一edge {edge(rows):+.1%}")
def terc(rows, k, name=None):
    have = [r for r in rows if isinstance(r.get(k), (int, float))]
    if len(have) < 45: return
    v = sorted(r[k] for r in have)
    if len(set(v)) <= 4:
        for x in sorted(set(v)): cell(f"{name or k}={x}", [r for r in have if r[k]==x])
    else:
        q1, q2 = v[len(v)//3], v[2*len(v)//3]
        cell(f"{name or k} 低≤{q1:.2f}", [r for r in have if r[k]<=q1]); cell(f"{name or k} 中", [r for r in have if q1<r[k]<=q2]); cell(f"{name or k} 高>{q2:.2f}", [r for r in have if r[k]>q2])
for stage in ("P0", "P0-M"):
    D = [r for r in R if r["stage"]==stage]
    P(f"\n\n==================== {stage} 首个众数不同决策（每站日一行）n={len(D)} ====================")
    cell("全部", D)
    P("\n--- 方向1：市场来不来认错（结算前市场众数是否转到模型桶 pm0）---")
    po = [r for r in D if r["p_hit"]]; qo = [r for r in D if r["q_hit"]]; nn = [r for r in D if not r["p_hit"] and not r["q_hit"]]
    for nm, g in (("模型独中", po), ("市场独中", qo), ("都不中", nn)):
        g2 = [r for r in g if r.get("mkt_came_around") is not None]
        if not g2: continue
        ca = rate(g2, "mkt_came_around"); by36 = [r for r in g2 if r.get("mkt_moved_to_pm_by_36h") is not None]
        fq = statistics.mean(r["final_q_at_pm0"] for r in g2); mx = [r["max_q_pm0_gt36"] for r in g2 if r.get("max_q_pm0_gt36") is not None]
        P(f"   {nm:6s} n={len(g2):4d} 结算前市场众数转到模型桶 {ca:5.1%} | 36h 线前已转 {rate(by36,'mkt_moved_to_pm_by_36h') if by36 else float('nan'):5.1%} | 模型桶终价均值 {fq:.2f} | >36h 窗内模型桶最高价均值 {statistics.mean(mx) if mx else float('nan'):.2f} (入场卖一均 {statistics.mean(r['ask_pm'] for r in g2 if r.get('ask_pm') is not None):.2f})")
    # money: for model-only wins, how often could a >36h exit at >= ask+0.1 have been taken? and for all disagreements, P&L buy at ask hold to settlement vs sell at max within window
    g = [r for r in D if r.get("ask_pm") is not None and r.get("max_q_pm0_gt36") is not None]
    hold = statistics.mean(r["p_hit"]-r["ask_pm"] for r in g)
    P(f"   全部众数不同 n={len(g)}: 按卖一买模型桶持有到结算 {hold:+.1%}/份；若在 >36h 窗内以该桶最高中价卖出(理想化) {statistics.mean(r['max_q_pm0_gt36']-r['ask_pm'] for r in g):+.1%}/份")
    P("   市场认错组 vs 不认错组：")
    cell("市场结算前转到模型桶", [r for r in D if r.get("mkt_came_around")==1]); cell("市场到最后不认", [r for r in D if r.get("mkt_came_around")==0])
    cell("36h线前市场已转到模型桶", [r for r in D if r.get("mkt_moved_to_pm_by_36h")==1]); cell("36h线前市场未转", [r for r in D if r.get("mkt_moved_to_pm_by_36h")==0])
    P("\n--- 方向2：盘口陈旧度 ---")
    terc(D, "book_stale_h", "盘口最新档龄(h)"); terc(D, "book_oldest_h", "盘口最旧档龄(h)"); terc(D, "snap_age_min", "快照龄(min)")
    cell("盘口最新档龄≥6h", [r for r in D if (r.get("book_stale_h") or 0) >= 6]); cell("盘口最新档龄≥12h", [r for r in D if (r.get("book_stale_h") or 0) >= 12])
    P("\n--- 方向3：美东时钟 ---")
    terc(D, "et_hour", "ET小时"); cell("ET夜间(23–07)", [r for r in D if r["et_night"]]); cell("ET白天", [r for r in D if not r["et_night"]])
    cell("ET周末", [r for r in D if r["et_weekend"]]); cell("ET工作日", [r for r in D if not r["et_weekend"]])
    for h in range(0, 24, 4): cell(f"ET {h:02d}–{h+3:02d}h", [r for r in D if h <= r["et_hour"] < h+4])
    P("\n--- 方向4：锚定持续性 / 变温幅度 ---")
    cell("市场众数=昨日真值桶", [r for r in D if r.get("qm_is_y1")==1]); cell("市场众数≠昨日真值桶", [r for r in D if r.get("qm_is_y1")==0])
    cell("模型众数=昨日真值桶", [r for r in D if r.get("pm_is_y1")==1])
    cell("模型预报变温比市场大", [r for r in D if r.get("model_bigger_change")==1]); cell("模型预报变温比市场小", [r for r in D if r.get("model_bigger_change")==0])
    cell("模型变温≥3桶", [r for r in D if (r.get("abs_pm_change") or 0) >= 3]); cell("模型变温≥2桶", [r for r in D if (r.get("abs_pm_change") or 0) >= 2]); cell("模型变温≤1桶", [r for r in D if r.get("abs_pm_change") is not None and r["abs_pm_change"] <= 1])
    cell("模型7日距平比市场大", [r for r in D if r.get("model_bigger_anom")==1]); cell("模型7日距平比市场小", [r for r in D if r.get("model_bigger_anom")==0])
    terc(D, "pm_anom7", "模型众数7日距平"); 
    cell("模型比市场暖∧市场众数=昨日", [r for r in D if r.get("qm_is_y1")==1 and r["warm"]]); cell("模型比市场冷∧市场众数=昨日", [r for r in D if r.get("qm_is_y1")==1 and not r["warm"]])
    P("\n--- 方向5：市场龄 ---")
    terc(D, "market_age_h", "市场龄(h)"); cell("市场龄<24h", [r for r in D if r.get("market_age_h") is not None and r["market_age_h"] < 24]); cell("市场龄≥72h", [r for r in D if r.get("market_age_h") is not None and r["market_age_h"] >= 72])
    P("\n--- 方向6：站点物理分组（真值日际波动）---")
    terc(D, "vol", "站真值日际σ(°C)")
    P("\n--- 方向8：六家各自众数落在哪 ---")
    cell("落模型桶的家数 > 落市场桶", [r for r in D if r["sys_mode_in_pm"] > r["sys_mode_in_qm"]]); cell("落模型桶 = 落市场桶", [r for r in D if r["sys_mode_in_pm"] == r["sys_mode_in_qm"]]); cell("落模型桶 < 落市场桶", [r for r in D if r["sys_mode_in_pm"] < r["sys_mode_in_qm"]])
    cell("全部家在模型桶(≥3家)", [r for r in D if r["sys_mode_in_pm"] >= 3 and r["sys_mode_in_qm"] == 0]); cell("有家落市场桶", [r for r in D if r["sys_mode_in_qm"] >= 1])
    cell("多数家在别处", [r for r in D if r["sys_mode_elsewhere"] > r["sys_mode_in_pm"]])
    P("\n--- 方向5b：同日跨站聚集（日型）---")
    byd = collections.defaultdict(list)
    for r in D: byd[r["d"]].append(r)
    days = [(d, len(v), sum(x["p_hit"] for x in v), sum(x["q_hit"] for x in v)) for d, v in byd.items() if len(v) >= 10]
    pbar = rate(D, "p_hit"); 
    obs_var = statistics.pvariance([m/n for d, n, m, q in days]); exp_var = statistics.mean([pbar*(1-pbar)/n for d, n, m, q in days])
    P(f"   日级模型命中率方差 {obs_var:.4f} vs 二项期望 {exp_var:.4f} → 比值 {obs_var/exp_var:.2f}（>1.3 表示按日成簇）")
    # leave-one-out same-day others
    for r in D:
        others = [x for x in byd[r["d"]] if x["st"] != r["st"]]
        r["others_net"] = (sum(x["p_hit"] for x in others) - sum(x["q_hit"] for x in others))/len(others) if len(others) >= 5 else None
        oth_reg = [x for x in others if x["region"] == r["region"]]
        r["region_net"] = (sum(x["p_hit"] for x in oth_reg) - sum(x["q_hit"] for x in oth_reg))/len(oth_reg) if len(oth_reg) >= 3 else None
    terc(D, "others_net", "同日其它站 模型−市场 净胜(事后)"); terc(D, "region_net", "同日同区其它站 净胜(事后)")
    P("   日级最好/最差 8 天：")
    for d, n, m, q in sorted(days, key=lambda x: -(x[2]-x[3])/x[1])[:8]: P(f"     {d} n={n} 模型{m} 市场{q}")
    for d, n, m, q in sorted(days, key=lambda x: (x[2]-x[3])/x[1])[:4]: P(f"     {d} n={n} 模型{m} 市场{q}")
    P("\n--- 方向7：按对数分 d 排的最好格（四分割 d 都>0）---")
    feats = ["book_stale_h","snap_age_min","et_hour","market_age_h","vol","pm_anom7","abs_pm_change","sys_mode_in_pm","sys_mode_in_qm","absdmu","sd_q","q_at_pm","p_pm","q_qm"]
    good = []
    for k in feats:
        have = [r for r in D if isinstance(r.get(k), (int, float))]
        if len(have) < 45: continue
        v = sorted(r[k] for r in have); q1, q2 = v[len(v)//3], v[2*len(v)//3]
        for nm, sub in ((f"{k}低", [r for r in have if r[k]<=q1]), (f"{k}中", [r for r in have if q1<r[k]<=q2]), (f"{k}高", [r for r in have if r[k]>q2])):
            s4 = splits(sub)
            if all(len(v) >= 10 for v in s4.values()) and all(dlog(v) > 0 for v in s4.values()): good.append((nm, len(sub), dlog(sub), diff(sub)))
    P("   " + (", ".join(f"{nm} n={n} d={dl:+.3f} 差{df:+.1%}" for nm, n, dl, df in good) if good else "无"))
