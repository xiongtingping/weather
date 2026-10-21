import json, os, random, collections, statistics, math
HERE = os.path.dirname(os.path.abspath(__file__))
R = [json.loads(l) for l in open(os.path.join(HERE, "recs.jsonl"))]
def boot(rows, f, B=1000, seed=1):
    days = sorted({r["d"] for r in rows}); by = collections.defaultdict(list)
    for r in rows: by[r["d"]].append(r)
    rnd = random.Random(seed); vals = []
    for _ in range(B):
        s = [x for d in (rnd.choice(days) for _ in days) for x in by[d]]
        if s: vals.append(f(s))
    vals.sort(); return vals[int(.025*len(vals))], vals[int(.975*len(vals))]
def rate(rows, k): return sum(r[k] for r in rows)/len(rows) if rows else float('nan')
def diff(rows): return rate(rows, "p_hit") - rate(rows, "q_hit")
def line(name, rows, ci=True):
    if not rows: return
    lo, hi = boot(rows, diff, 400) if ci and len(rows) >= 20 else (float('nan'),)*2
    both = sum(r["p_hit"] and r["q_hit"] for r in rows); po = sum(r["p_hit"] and not r["q_hit"] for r in rows)
    qo = sum(r["q_hit"] and not r["p_hit"] for r in rows)
    print(f"{name:34s} n={len(rows):5d} P0命中{rate(rows,'p_hit'):6.1%} 市场命中{rate(rows,'q_hit'):6.1%} 差{diff(rows):+6.1%} [{lo:+.1%},{hi:+.1%}]  P0独中{po:4d} 市场独中{qo:4d} 同中{both:4d}")
print("== 总体 ==")
for w in ("A", "B", None):
    rr = [r for r in R if w is None or r["win"] == w]; line(f"窗口{w or 'A+B'}", rr)
print("== 按距结算 ==")
for b in ["60h+", "48-60h", "36-48h", "24-36h", "12-24h", "<12h"]:
    for w in ("A", "B"):
        line(f"{b} 窗{w}", [r for r in R if r["band"] == b and r["win"] == w])
print("== 众数不同 ==")
D = [r for r in R if r["dmode"] != 0]
for w in ("A", "B", None):
    line(f"众数不同 窗{w or 'A+B'}", [r for r in D if w is None or r["win"] == w])
print("众数相同命中率", rate([r for r in R if r["dmode"] == 0], "p_hit"), len([r for r in R if r["dmode"] == 0]))
# features
FEATS = ["h", "p_pm", "q_qm", "q_at_pm", "p_at_qm", "q_second", "dmu", "sd_p", "sd_q", "ent_q", "sys_sd", "sys_side_frac",
         "n_sys", "spread_qm", "spread_pm", "maxsp", "ms", "bridge_gap", "clear", "clear_anom", "age_h"]
for r in D:
    r["absdmode"] = abs(r["dmode"]); r["absdmu"] = abs(r["dmu"]); r["gap_q"] = r["q_qm"] - r["q_at_pm"]
    r["gap_p"] = r["p_pm"] - r["p_at_qm"]; r["sharp_ratio"] = r["sd_p"]/r["sd_q"] if r["sd_q"] else None
    r["warm"] = int(r["dmode"] > 0)
FEATS += ["absdmode", "absdmu", "gap_q", "gap_p", "sharp_ratio", "warm", "qm_edge", "pm_edge"]
print("\n== 众数不同时: P0独中 vs 市场独中 特征均值 ==")
po = [r for r in D if r["p_hit"]]; qo = [r for r in D if r["q_hit"]]; nn = [r for r in D if not r["p_hit"] and not r["q_hit"]]
def mean(rows, k):
    v = [r[k] for r in rows if r.get(k) is not None]; return statistics.mean(v) if v else float('nan')
print(f"{'特征':14s} {'P0独中':>9s} {'市场独中':>9s} {'都不中':>9s}")
for k in FEATS: print(f"{k:14s} {mean(po,k):9.3f} {mean(qo,k):9.3f} {mean(nn,k):9.3f}")
json.dump(D, open(os.path.join(HERE, "disagree.json"), "w"))
