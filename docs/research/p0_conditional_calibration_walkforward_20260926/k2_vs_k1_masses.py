"""Does K1 read the mass on either side of the centre?  No -- K2 does; what that adds over K1.

User: 「k1是否考虑中心两侧的概率质量？」  Same folds as centres.py; prints K2 last-fold
coefficients, the K2-K1 shift and the paired K2-K1 scores (three-day blocks).  Read-only.
"""
import sys
import numpy as np
sys.path.insert(0, "/Users/xiong/weather/docs/research/p0_conditional_calibration_walkforward_20260926")
import walkforward as W, centres as C
rows, _ = W.build_rows("/private/tmp/claude-501/-Users-xiong-weather/c3f1916f-ae2f-483e-a670-3ec3fa769254/scratchpad/decompose_cache.pkl")
for r in rows:
    pos, mas, mp, mm = C.centre_features(r); r["mean_m"] = mm
    r["f"] = {"p": {**{k: v - mp for k, v in pos.items() if k != "mean_p"}, **mas}}
days = sorted({r["date"] for r in rows}); tested = []; last = {}
for day in days:
    test = [r for r in rows if r["date"] == day]
    cutoff = min(W.instant(r["at"]) for r in test)
    train = [r for r in rows if r["date"] < day and r["known_at"] <= cutoff]
    if len({(r["station"], r["date"]) for r in train}) < W.MIN_TRAIN_STATION_DAYS: continue
    for arm, feats in (("K1", C.CENTRES), ("K2", C.CENTRES + C.MASSES)):
        m = C.fit(train, feats, "p", False); last[arm] = m
        for r in test: r.setdefault("d", {})[arm] = C.predict(m, r, "p")
    tested += test
feats, mu, sd, beta = last["K2"]
print("K2 last fold, per-SD effect on δ (°C) and per-unit coefficient:")
for f, b, s in zip(feats, beta[1:], sd): print(f"  {f:14s} per-SD {b:+.3f}°C   per-unit {b/s:+.3f}")
dd = np.array([r["d"]["K2"] - r["d"]["K1"] for r in tested])
print("K2 − K1 shift: median |diff| %.3f°C, 90%% %.3f°C" % (np.median(np.abs(dd)), np.percentile(np.abs(dd), 90)))
for r in tested:
    r["s"] = {a: C.shifted(r, "p", r["d"][a]) for a in ("K1", "K2")}
rng = np.random.default_rng(W.SEED)
for metric, j in (("LogScore", 0), ("Brier", 1)):
    d, num, den = W.per_day(tested, lambda r: r["s"]["K2"][j] - r["s"]["K1"][j])
    print(f"K2 − K1 {metric}: {num.sum()/den.sum():+.4f} {W.block_ci(d, num, den, rng)}")
