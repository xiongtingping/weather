"""Fit and walk forward the observed-minus-forecast increment likelihood on P1.

    P1_new(k)  proportional to  P1(k) * (L(k) + eps) ** w

L(k) is the probability that the day's high lands in bucket k under a
physical expectation built only from what is observed now and what the
forecast (issued a day earlier) said:

    sun ratio      r   = ln((SW_inferred_now + 30) / (SW_forecast_now + 30))
                         SW_inferred = clear-sky(t) * (1 - 0.75 N^3.4), N the
                         METAR's low cloud (precipitation -> N >= 0.875)
    sun increment  k   = exp(gamma * (r - r0)), clipped to [0.25, 2.5]
    remaining rise R_f = max over the rest of the day of the forecast hourly
                         temperature, minus the forecast temperature now (>= 0)
    expected high  M   = max(H, T_now + R_f * k)        H = running high
    final high     X   = max(H, M + e),  e ~ N(0, (s0 + s1 * R_f)^2)

So the observed temperature's departure from the forecast enters as the
level (T_now), the observed sky's departure from the forecast scales the
rise still to come, and every sign is fixed by construction: more cloud /
less sun than forecast can only shrink the rise (gamma >= 0).  Fitted:
w, gamma, r0, s0, s1, by the mean log score of the floored P1_new (1e-3,
charter 224) over station-days.

Walk-forward: for each test day, fit on all earlier days (>= MIN_TRAIN),
score that day.  Judged as charter 240: LogScore first (paired by
station-day, bootstrap over event days), Brier as a guard.
"""

from __future__ import annotations

import gzip
import json
import math
import random
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from scipy.optimize import minimize
from scipy.stats import norm

HERE = Path(__file__).resolve().parent
FLOOR = 1e-3
MIN_TRAIN_DAYS = 12
REFIT_EVERY = 4


def clear_sky(lat: float, lon: float, when: datetime) -> float:
    n = when.timetuple().tm_yday
    hour = when.hour + when.minute / 60.0
    g = 2 * math.pi / 365 * (n - 1 + (hour - 12) / 24)
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
            + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    eot = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                    - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    ha = math.radians((hour * 60 + eot + 4 * lon) / 4 - 180)
    phi = math.radians(lat)
    cz = math.sin(phi) * math.sin(decl) + math.cos(phi) * math.cos(decl) * math.cos(ha)
    return 0.0 if cz <= 0.01 else 1098.0 * cz * math.exp(-0.057 / cz)


def floor_norm(v: np.ndarray) -> np.ndarray:
    v = np.maximum(v, 0.0)
    v = v / v.sum()
    v = np.maximum(v, FLOOR)
    return v / v.sum()


def prepare(rows):
    out = []
    for r in rows:
        iv = r["intervals"]
        if not iv or r["temp_now"] is None:
            continue
        buckets = sorted(iv, key=lambda b: (iv[b][0] if iv[b][0] is not None else -1e9))
        lo = np.array([iv[b][0] if iv[b][0] is not None else -1e9 for b in buckets], float)
        hi = np.array([iv[b][1] if iv[b][1] is not None else 1e9 for b in buckets], float)
        p = np.array([float(r["p1"].get(b, 0.0)) for b in buckets])
        if p.sum() <= 0:
            continue
        truth = r["truth"]
        k = next((i for i in range(len(buckets)) if lo[i] - 1e-6 <= truth < hi[i] - 1e-6), None)
        if k is None:
            continue
        h = r["local_hour"]
        fc = r["fc"]
        hh = min(23, int(math.floor(h)))
        f_now = fc[hh]["t"] + (fc[min(23, hh + 1)]["t"] - fc[hh]["t"]) * (h - hh)
        rest = [fc[x]["t"] for x in range(hh + 1, 24)]
        rise_f = max(0.0, max([f_now] + rest) - f_now)
        when = datetime.fromisoformat(r["decided_at"].replace("Z", "+00:00"))
        n = r["cloud_now"]
        r_ratio = 0.0
        has_sky = False
        sw_fc = fc[min(23, int(math.ceil(h)))]["sw"]
        if n is not None and sw_fc is not None:
            if r["precip_now"]:
                n = max(n, 0.875)
            inferred = clear_sky(r["lat"], r["lon"], when - timedelta(minutes=0)) * (1 - 0.75 * n ** 3.4)
            r_ratio = math.log((inferred + 30.0) / (sw_fc + 30.0))
            has_sky = True
        out.append({
            "station": r["station"], "day": r["day"], "hour": h,
            "lo": lo, "hi": hi, "p": floor_norm(p), "k": k,
            "H": float(r["running_high"]), "T": float(r["temp_now"]),
            "rise_f": rise_f, "r": r_ratio, "has_sky": has_sky, "dT": float(r["temp_now"]) - f_now,
        })
    return out


def likelihood(x, theta):
    w, gamma, r0, s0, s1 = theta
    kk = math.exp(gamma * (x["r"] - r0)) if x["has_sky"] else 1.0
    kk = min(2.5, max(0.25, kk))
    m = max(x["H"], x["T"] + x["rise_f"] * kk)
    s = max(0.15, s0 + s1 * x["rise_f"])
    cdf_hi = norm.cdf((x["hi"] - m) / s)
    cdf_lo = norm.cdf((x["lo"] - m) / s)
    lik = cdf_hi - cdf_lo
    # X = max(H, M + e): all mass below H lands on H's bucket.
    below = norm.cdf((x["H"] - m) / s)
    inside = (x["lo"] <= x["H"] + 1e-9) & (x["H"] < x["hi"] - 1e-9)
    lik = np.where(x["hi"] <= x["H"] + 1e-9, 0.0, lik)
    lik = np.where(inside, below + (cdf_hi - norm.cdf((x["H"] - m) / s)), lik)
    return lik


def updated(x, theta):
    w = theta[0]
    lik = likelihood(x, theta)
    return floor_norm(x["p"] * (lik + 1e-4) ** w)


def score(v, k):
    return -math.log(v[k]), float(((v - np.eye(len(v))[k]) ** 2).sum())


def fit(train, start=(1.0, 1.0, 0.0, 0.6, 0.15)):
    # One row per (station, day, hour): the mean over station-days.
    def loss(theta):
        tot = 0.0
        for x in train:
            tot += score(updated(x, theta), x["k"])[0]
        return tot / len(train)
    bounds = [(0.0, 4.0), (0.0, 3.0), (-2.0, 2.0), (0.2, 3.0), (0.0, 1.5)]
    res = minimize(loss, start, method="L-BFGS-B", bounds=bounds, options={"maxiter": 30})
    return tuple(float(v) for v in res.x), float(res.fun)


def main():
    rows = [json.loads(l) for l in gzip.open(HERE / "rows.jsonl.gz", "rt")]
    data = prepare(rows)
    print("rows", len(data), "station-days", len({(x["station"], x["day"]) for x in data}),
          "with sky", sum(x["has_sky"] for x in data))
    days = sorted({x["day"] for x in data})
    by_day = defaultdict(list)
    for x in data:
        by_day[x["day"]].append(x)
    results = []
    theta = (1.0, 1.0, 0.0, 0.6, 0.15)
    fitted = {}
    for i, d in enumerate(days):
        if i < MIN_TRAIN_DAYS:
            continue
        if (i - MIN_TRAIN_DAYS) % REFIT_EVERY == 0:
            train = [x for dd in days[:i] for x in by_day[dd]]
            # thin the training rows for speed: every other hour
            train = [x for x in train if int(x["hour"]) % 2 == 0]
            theta, loss = fit(train, start=theta)
        fitted[d] = theta
        for x in by_day[d]:
            base = score(x["p"], x["k"])
            new = score(updated(x, theta), x["k"])
            results.append({"day": d, "station": x["station"], "hour": x["hour"],
                            "log_base": base[0], "log_new": new[0], "brier_base": base[1], "brier_new": new[1],
                            "r": x["r"], "has_sky": x["has_sky"], "rise_f": x["rise_f"]})
        print(d, "theta", [round(v, 3) for v in theta], "train loss", round(loss, 4), flush=True)
    json.dump({"fitted": fitted, "results": results}, open(HERE / "walk_forward.json", "w"))
    report(results)


def report(results):
    def summary(sel, label):
        if not sel:
            return
        by_sd = defaultdict(list)
        for r in sel:
            by_sd[(r["day"], r["station"])].append(r)
        sd = [(k[0], np.mean([r["log_new"] - r["log_base"] for r in v]), np.mean([r["brier_new"] - r["brier_base"] for r in v]))
              for k, v in by_sd.items()]
        days = sorted({s[0] for s in sd})
        rng = random.Random(7)
        boot_l, boot_b = [], []
        by_day = defaultdict(list)
        for s in sd:
            by_day[s[0]].append(s)
        for _ in range(2000):
            pick = [rng.choice(days) for _ in days]
            vals = [s for d in pick for s in by_day[d]]
            boot_l.append(np.mean([v[1] for v in vals])); boot_b.append(np.mean([v[2] for v in vals]))
        ml, mb = np.mean([s[1] for s in sd]), np.mean([s[2] for s in sd])
        print(f"{label:34s} n_sd={len(sd):4d}  ΔLogScore {ml:+.4f} [{np.percentile(boot_l,2.5):+.4f}, {np.percentile(boot_l,97.5):+.4f}]"
              f"  ΔBrier {mb:+.4f} [{np.percentile(boot_b,2.5):+.4f}, {np.percentile(boot_b,97.5):+.4f}]")
    summary(results, "all hours")
    summary([r for r in results if 12 <= r["hour"] < 18], "12-18 local")
    summary([r for r in results if r["has_sky"] and r["r"] < -0.5], "sun well below forecast (r<-0.5)")
    summary([r for r in results if r["has_sky"] and r["r"] > 0.3], "sun above forecast (r>0.3)")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "report":
        report(json.load(open(HERE / "walk_forward.json"))["results"])
    else:
        main()
