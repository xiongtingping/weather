"""Charter 278 follow-up 2: the same walk-forward beliefs priced at EXECUTABLE
prices -- YES legs at the best ask, NO legs at 1 - best bid (taker) -- on the
first decision of each station-day x horizon band whose book rungs_v4 holds.
Also saves the matched beliefs (proper_taker_match.pkl) for later cuts."""
import os, sys, sqlite3, math, collections, random, json, pickle
import numpy as np
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)
from bayes_basket_target import realisation_store as store, realisation_layer as layer
HERE = os.path.dirname(os.path.abspath(__file__))
out = open(os.path.join(HERE, "proper_taker_b_v3.txt"), "w")
def P(*a): print(*a, flush=True); print(*a, file=out)
BANDS = [(60, 1e9, "60h+"), (48, 60, "48-60h"), (36, 48, "36-48h"), (24, 36, "24-36h"), (12, 24, "12-24h"), (6, 12, "6-12h"), (-1e9, 6, "<6h")]
def band(h):
    for lo, hi, n in BANDS:
        if lo <= h < hi: return n
books = collections.defaultdict(dict)
for l in open(os.path.join(HERE, "rungs_v4.jsonl")):
    g = json.loads(l); books[(g["fam"], g["st"], g["d"], g["band"])][g["k"]] = (g.get("bid"), g.get("ask"))
conn = sqlite3.connect(f"file:{ROOT}/data/bayes_basket_target_ssot.db?mode=ro", uri=True, timeout=60); conn.execute("PRAGMA query_only=ON")
matched = {}
def boot(rows, B=400):
    by = collections.defaultdict(list)
    for d, pr, c in rows: by[d].append((pr, c))
    days = list(by); rnd = random.Random(11); v = []
    for _ in range(B):
        s = [z for d in (rnd.choice(days) for _ in days) for z in by[d]]; c = sum(z[1] for z in s); v.append(sum(z[0] for z in s) / c if c > 0 else 0.0)
    v.sort(); return v[int(.025 * B)], v[int(.975 * B)]
def legs(b, q, y, book, lean, mode, only=None, min_s=0.0):
    s = np.log(b) - np.log(q); hit = (np.arange(len(q)) == y).astype(float); prof = cost = 0.0; n = 0
    for k in range(len(q)):
        if abs(s[k]) <= min_s: continue
        bid, ask = book.get(k, (None, None))
        side = "YES" if s[k] > 0 else "NO"
        if only == "YES" and side != "YES": continue
        if only == "NO" and side != "NO": continue
        if only == "YES_far" and not (side == "YES" and (k - mode) * lean < 0): continue
        if only == "YES_near" and not (side == "YES" and (k - mode) * lean > 0): continue
        if only == "YES_mode" and not (side == "YES" and k == mode): continue
        x = abs(s[k])
        if side == "YES":
            if ask is None: continue
            prof += x * (hit[k] - ask); cost += x * ask; n += 1
        else:
            if bid is None or bid <= 0: continue
            prof += x * (bid - hit[k]); cost += x * (1 - bid); n += 1
    return prof, cost, n
for stage in ("P0-M", "P0", "P1"):
    decisions, counts = store.population(conn, cutoff_utc="2026-10-22T02:00:00Z", stage=stage)
    fwd = store.walk_forward(decisions)
    first = {}
    for i in sorted(fwd["layer"]):
        d = decisions[i]; h = d["hours_to_settlement"]
        fam = ("P0>36h" if h > 36 else "P0≤36h") if stage == "P0" else stage
        key = (fam, d["station_id"], d["event_date"], band(h))
        if key not in books: continue
        if key not in first or d["decision_at_utc"] < decisions[first[key]]["decision_at_utc"]: first[key] = i
    rows = []
    for key, i in first.items():
        d = decisions[i]; q = np.asarray(d["q"]); ct = np.asarray(d["terms"])[:, layer.CENTRE_COLUMN]
        v1 = q * np.exp(ct); v1 /= v1.sum(); lean = int(np.sign((np.arange(len(q)) * (v1 - q)).sum()))
        rows.append(dict(key=key, d=d["event_date"], q=q, y=d["y"], mode=d["mode"], lean=lean, book=books[key],
                         v3=np.asarray(fwd["layer"][i]), v2=np.asarray(fwd["without_centre"][i]), m=np.asarray(fwd["market_only"][i]),
                         agree=d["centres_agree"], h=d["hours_to_settlement"]))
    matched[stage] = rows
    P(f"\n==================== {stage}: 匹配到盘口的首决策 {len(rows)}（前推打分 {len(fwd['layer'])} 决策）====================")
    P("   腿集合                 信念      腿数    总成本    总利润    ROI@可成交价 [日聚类95%]")
    for only, label in ((None, "整梯双边(YES@卖一,NO@1−买一)"), ("YES", "只 YES 腿 @卖一"), ("NO", "只 NO 腿 @1−买一"), ("YES_far", "YES·市场众数之外的反侧(远侧)"), ("YES_near", "YES·市场众数之外的模型侧"), ("YES_mode", "YES·市场众数档")):
        for nm, bk in (("b v3", "v3"), ("b v2等价", "v2"), ("纯市场池", "m")):
            rs = []; N = 0
            for r in rows:
                if r["lean"] == 0 and only in ("YES_far", "YES_near"): continue
                pr, c, n = legs(r[bk], r["q"], r["y"], r["book"], r["lean"], r["mode"], only=only); rs.append((r["d"], pr, c)); N += n
            cost = sum(x[2] for x in rs); prof = sum(x[1] for x in rs)
            if cost <= 0: P(f"   {label:26s} {nm:8s} 无腿"); continue
            lo, hi = boot(rs); P(f"   {label:26s} {nm:8s} {N:6d}  {cost:8.1f}  {prof:+8.2f}   {prof/cost:+7.2%} [{lo:+.1%},{hi:+.1%}]")
    P("   —— YES 腿按 s 门槛（b v3 / v2）——")
    for ms in (0.0, 0.1, 0.2, 0.4):
        for nm, bk in (("b v3", "v3"), ("b v2等价", "v2")):
            rs = []; N = 0
            for r in rows:
                pr, c, n = legs(r[bk], r["q"], r["y"], r["book"], r["lean"], r["mode"], only="YES", min_s=ms); rs.append((r["d"], pr, c)); N += n
            cost = sum(x[2] for x in rs); prof = sum(x[1] for x in rs)
            if cost > 0: lo, hi = boot(rs); P(f"     s>{ms:.1f} {nm:8s} 腿 {N:5d} 成本 {cost:7.1f} ROI {prof/cost:+7.2%} [{lo:+.1%},{hi:+.1%}]")
conn.close()
pickle.dump(matched, open(os.path.join(HERE, "proper_taker_match.pkl"), "wb"))
P("saved proper_taker_match.pkl")
