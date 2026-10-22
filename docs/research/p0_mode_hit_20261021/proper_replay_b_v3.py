"""Charter 278 follow-up: the Proper rule (whole ladder, both sides, shares ∝ |s|,
s = log(b/q)) replayed strictly walk-forward on the served record under three
beliefs -- v3 (signed centre), v2-equivalent (without the centre column) and
the market-only pool -- priced at the normalised mid (the resting scenario,
fills assumed, as charter 265's reading).  Read-only.  Output proper_replay_b_v3.txt."""
import os, sys, sqlite3, math, collections, random
import numpy as np
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)
from bayes_basket_target import realisation_store as store, realisation_layer as layer
HERE = os.path.dirname(os.path.abspath(__file__))
out = open(os.path.join(HERE, "proper_replay_b_v3.txt"), "w")
def P(*a): print(*a, flush=True); print(*a, file=out)
conn = sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60)
conn.execute("PRAGMA query_only=ON")
def proper(b, q, y):
    s = np.log(b) - np.log(q); x = np.abs(s)
    yes = s > 0
    hit = (np.arange(len(q)) == y).astype(float)
    profit = float((x * np.sign(s) * (hit - q)).sum())
    cost = float((x * np.where(yes, q, 1.0 - q)).sum())
    return profit, cost, float((x[yes] * (hit - q)[yes]).sum()), float((x[yes] * q[yes]).sum()), float((x[~yes] * (q - hit)[~yes]).sum()), float((x[~yes] * (1 - q)[~yes]).sum())
def boot_roi(rows, B=400):
    by = collections.defaultdict(list)
    for d, pr, c in rows: by[d].append((pr, c))
    days = list(by); rnd = random.Random(9); v = []
    for _ in range(B):
        s = [z for d in (rnd.choice(days) for _ in days) for z in by[d]]
        c = sum(z[1] for z in s); v.append(sum(z[0] for z in s) / c if c > 0 else 0.0)
    v.sort(); return v[int(.025 * B)], v[int(.975 * B)]
for stage in ("P0-M", "P0", "P1"):
    decisions, counts = store.population(conn, cutoff_utc="2026-10-22T02:00:00Z", stage=stage)
    fwd = store.walk_forward(decisions)
    scored = sorted(fwd["layer"])
    P(f"\n==================== {stage}: 人口 {counts['decisions']} 决策 / {counts['station_days']} 站日，前推打分 {len(scored)} 决策 / {len(fwd['days'])} 天 ====================")
    variants = {"b v3（带符号中心列）": fwd["layer"], "b v2 等价（去中心列）": fwd["without_centre"], "纯市场池": fwd["market_only"], "市场 q 本身": None}
    P("   信念                     决策    总成本    总利润   ROI@中价 [日聚类95%]   YES腿ROI   NO腿ROI   | 分歧(中心差>0.25)决策 ROI | 一致决策 ROI")
    for name, beliefs in variants.items():
        rows = []; yes_p = yes_c = no_p = no_c = 0.0; dis = []; agr = []
        for i in scored:
            d = decisions[i]; q = np.asarray(d["q"]); b = q if beliefs is None else np.asarray(beliefs[i])
            if beliefs is None: continue
            pr, c, yp, yc, npf, nc = proper(b, q, d["y"]); w = d["weight"]
            rows.append((d["event_date"], pr * w, c * w)); yes_p += yp * w; yes_c += yc * w; no_p += npf * w; no_c += nc * w
            (agr if d["centres_agree"] else dis).append((d["event_date"], pr * w, c * w))
        if not rows: P(f"   {name:24s} —（q 对 q 没有腿）"); continue
        cost = sum(r[2] for r in rows); prof = sum(r[1] for r in rows); lo, hi = boot_roi(rows)
        roi = lambda rs: (sum(r[1] for r in rs) / sum(r[2] for r in rs)) if rs and sum(r[2] for r in rs) > 0 else float("nan")
        P(f"   {name:24s} {len(rows):5d}  {cost:8.1f}  {prof:+8.2f}  {prof/cost:+7.2%} [{lo:+.1%},{hi:+.1%}]   {yes_p/yes_c if yes_c else float('nan'):+6.1%}    {no_p/no_c if no_c else float('nan'):+6.1%}   |  n={len(dis)} {roi(dis):+.1%}  |  n={len(agr)} {roi(agr):+.1%}")
    # side-mass binary log loss: far/near of the market mode relative to the model's lean (sign of the centre term's tilt)
    P("   侧向二元 LogLoss（真值在市场众数之外、模型倾斜侧 / 反侧）：")
    for name, beliefs in (("b v3", fwd["layer"]), ("b v2 等价", fwd["without_centre"]), ("纯市场池", fwd["market_only"])):
        ll = []; wsum = 0.0
        for i in scored:
            d = decisions[i]; q = np.asarray(d["q"]); b = np.asarray(beliefs[i]); k = len(q); mode = d["mode"]
            ct = np.asarray(d["terms"])[:, layer.CENTRE_COLUMN]
            v1 = q * np.exp(ct); v1 = v1 / v1.sum(); lean = np.sign((np.arange(k) * (v1 - q)).sum())
            if lean == 0: continue
            near = (np.arange(k) - mode) * lean > 0; far = (np.arange(k) - mode) * lean < 0
            yn, yf = float(near[d["y"]]), float(far[d["y"]])
            for mass, yy in ((b[near].sum(), yn), (b[far].sum(), yf)):
                m = min(max(mass, 1e-3), 1 - 1e-3); ll.append(-d["weight"] * (yy * math.log(m) + (1 - yy) * math.log(1 - m)))
            wsum += d["weight"]
        P(f"     {name:10s} {sum(ll)/wsum:.4f} nat/决策")
conn.close()
