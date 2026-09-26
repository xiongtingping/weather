"""What K1 is in degrees, and its centre against the basket's (charter 54: the model's mean bucket).

User: 「按四个中心的关系挪动模型：具体是什么？和目前篮子策略的以均值桶为中心有什么区别？」
Opening decision per station-day on the tested days; windows are {c-1, c, c+1}.  Read-only, prints.
"""
import sys
from collections import Counter, defaultdict
import numpy as np
sys.path.insert(0, "/Users/xiong/weather/docs/research/p0_conditional_calibration_walkforward_20260926")
import walkforward as W, centres as C
rows, _ = W.build_rows("/private/tmp/claude-501/-Users-xiong-weather/c3f1916f-ae2f-483e-a670-3ec3fa769254/scratchpad/decompose_cache.pkl")
for r in rows:
    pos, mas, mp, mm = C.centre_features(r); r["mean_m"] = mm
    r["f"] = {"p": {**{k: v - mp for k, v in pos.items() if k != "mean_p"}, **mas}}
days = sorted({r["date"] for r in rows}); tested = []
last_model = None
for day in days:
    test = [r for r in rows if r["date"] == day]
    cutoff = min(W.instant(r["at"]) for r in test)
    train = [r for r in rows if r["date"] < day and r["known_at"] <= cutoff]
    if len({(r["station"], r["date"]) for r in train}) < W.MIN_TRAIN_STATION_DAYS: continue
    model = C.fit(train, C.CENTRES, "p", False); last_model = model
    for r in test: r["k1"] = C.predict(model, r, "p"); tested.append(r)
# coefficients of the last fold, in °C per °C
feats, mu, sd, beta = last_model
b = beta[1:] / sd; b0 = beta[0] - float(np.dot(beta[1:], mu / sd))
print("K1 last fold (09-23), δ = %.3f" % b0, " ".join(f"{c:+.3f}×({f})" for f, c in zip(feats, b)))
x = np.array([r["f"]["p"]["mean_m"] for r in tested]); d = np.array([r["k1"] for r in tested])
slope = float(np.dot(x, d) / np.dot(x, x)); r2 = 1 - np.sum((d - slope * x) ** 2) / np.sum((d - d.mean()) ** 2)
print("fitted δ vs (market mean − model mean): through-origin slope %.2f, R² %.2f; median |market−model| %.2f°C, median |δ| %.2f°C" % (slope, r2, np.median(np.abs(x)), np.median(np.abs(d))))
# opening decision per station-day: centres and 3-bucket windows
first = {}
for r in sorted(tested, key=lambda r: r["at"]): first.setdefault((r["station"], r["date"]), r)
stats = Counter(); n = len(first)
def mean_bucket(vec, centers):
    return int(np.argmin(np.abs(np.asarray(centers) - float(np.dot(vec, centers)))))
for r in first.values():
    c = np.asarray(r["centers"]); h = r["hit"]
    q = W.D.as_vec(W.D.shift_tail(dict(zip(r["keys"], r["p"])), delta_c=r["k1"], keys=r["keys"], centers=list(c), open_edges=r["open_edges"]), r["keys"])
    cm, cmk, ck1 = mean_bucket(r["p"], c), mean_bucket(r["m"], c), mean_bucket(q, c)
    stats["model_hit"] += cm == h; stats["market_hit"] += cmk == h; stats["k1_hit"] += ck1 == h
    stats["model_win3"] += abs(cm - h) <= 1; stats["market_win3"] += abs(cmk - h) <= 1; stats["k1_win3"] += abs(ck1 - h) <= 1
    stats["differ"] += cm != cmk; stats["k1_eq_market"] += ck1 == cmk
    if cm != cmk:
        stats["d_model_hit"] += cm == h; stats["d_market_hit"] += cmk == h; stats["d_k1_hit"] += ck1 == h
        stats["d_model_win3"] += abs(cm - h) <= 1; stats["d_market_win3"] += abs(cmk - h) <= 1; stats["d_k1_win3"] += abs(ck1 - h) <= 1
print(f"opening station-days {n}; model mean bucket != market mean bucket: {stats['differ']} ({stats['differ']/n:.0%}); K1 mean bucket == market mean bucket: {stats['k1_eq_market']/n:.0%}")
for tag in ("model", "k1", "market"):
    print(f"  {tag:6s} centre hit {stats[tag+'_hit']/n:.1%}  3-bucket window {stats[tag+'_win3']/n:.1%} | when model≠market: hit {stats['d_'+tag+'_hit']/stats['differ']:.1%} window {stats['d_'+tag+'_win3']/stats['differ']:.1%}")
