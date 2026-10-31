"""Walk-forward fit and judgement of the D0 observation model (PROTOCOL.md).

Target y = truth Tmax - B (official deterministic median, day-ahead).  Per
time band, ridge regression with station fixed effects on as-of features
(missing -> 0 plus an indicator); sigma from training residuals per band.
Distribution: Normal(B + yhat, sigma), mass below the running high moved to
the high's bucket, bucketised on the station's ladder, floored at 1e-3.

Judged against the market mid at the same instant (LogScore, Brier, event-day
block bootstrap), against the served P1 for reference, and by money under the
taker rule of jaxa_cloud_rad_p1_20261015/fit_pnl.py plus a maker variant
(YES at bid + 1 tick when our b exceeds it, filled with probability 1/2 -- an
assumption, labelled).  Arms: full, no-satellite (METAR only), and B alone
(station-bias-corrected baseline, the floor the features must beat).
"""

from __future__ import annotations

import gzip
import json
import math
import random
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy.stats import norm

HERE = Path(__file__).resolve().parent
import os as _os
DATA = Path(_os.environ.get("D0_DATA_DIR") or HERE)   # scripts/d0_refit_daily.sh
FLOOR = 1e-3
MIN_TRAIN_DAYS = 12
RIDGE = 3.0
FEE = 0.05
BANDS = {"07-10": (7, 10), "10-12": (10, 12), "12-14": (12, 14), "14-19": (14, 19)}
NUMERIC = ["t_minus_fc", "warmed_gap", "momentum", "high_minus_base", "fc_rise", "dew_dep", "dew_minus_fc_dew",
           "wind", "cloud_low", "precip_now", "precip_any", "sun_pct", "cloud_gap", "rain_gap", "hours_to_peak"]
SAT = {"sun_pct", "cloud_gap", "rain_gap"}


def load():
    rows = [json.loads(l) for l in gzip.open(DATA / "rows_d0.jsonl.gz", "rt")]
    out = []
    for r in rows:
        iv = r["intervals"]
        if not iv or r["truth"] is None:
            continue
        labels = sorted(iv, key=lambda b: (iv[b][0] if iv[b][0] is not None else -1e9))
        lo = np.array([iv[b][0] if iv[b][0] is not None else -1e9 for b in labels], float)
        hi = np.array([iv[b][1] if iv[b][1] is not None else 1e9 for b in labels], float)
        k = next((i for i in range(len(labels)) if lo[i] - 1e-6 <= r["truth"] < hi[i] - 1e-6), None)
        if k is None:
            continue
        feats = {
            "t_minus_fc": r["t_minus_fc"], "warmed_gap": r["warmed"] - r["fc_warmed"], "momentum": r["momentum"],
            "high_minus_base": r["high"] - r["base"], "fc_rise": r["fc_rise"], "dew_dep": r["dew_dep"],
            "dew_minus_fc_dew": None, "wind": r["wind"], "cloud_low": r["cloud_low"],
            "precip_now": float(r["precip_now"]), "precip_any": float(r["precip_any"]),
            "sun_pct": r["sun_pct"], "cloud_gap": r["cloud_gap"], "rain_gap": r["rain_gap"],
            "hours_to_peak": r["fc_peak_h"] - r["hour"],
        }
        out.append(dict(station=r["station"], day=r["day"], hour=r["hour"], truth=r["truth"], base=r["base"],
                        high=r["high"], y=r["truth"] - r["base"], lo=lo, hi=hi, labels=labels, k=k, feats=feats,
                        market=r["market"], p1=r["p1"]))
    return out


def design(rows, stations, use_sat):
    cols = [c for c in NUMERIC if use_sat or c not in SAT]
    X = []
    for r in rows:
        v = []
        for c in cols:
            x = r["feats"][c]
            v.append(0.0 if x is None else float(x))
            if c in SAT or c == "dew_dep":
                v.append(1.0 if x is None else 0.0)
        v.extend(1.0 if r["station"] == s else 0.0 for s in stations)
        X.append(v)
    return np.array(X)


def fit_band(train, stations, use_sat):
    X = design(train, stations, use_sat)
    y = np.array([r["y"] for r in train])
    mu, sd = X.mean(0), X.std(0) + 1e-9
    nf = X.shape[1] - len(stations)
    Xs = X.copy(); Xs[:, :nf] = (X[:, :nf] - mu[:nf]) / sd[:nf]
    A = np.hstack([Xs, np.ones((len(Xs), 1))])
    pen = np.full(A.shape[1], RIDGE); pen[nf:nf + len(stations)] = RIDGE * 3; pen[-1] = 0.0
    beta = np.linalg.solve(A.T @ A + np.diag(pen), A.T @ y)
    resid = y - A @ beta
    # Spread: ln|resid| on the spread features (hours to the forecast peak,
    # forecast rise left, how far the running high already sits above B),
    # ridge, mean-corrected so exp() is the residual sd, not its median.
    S = spread_design(train)
    smu, ssd = S.mean(0), S.std(0) + 1e-9
    Ss = np.hstack([(S - smu) / ssd, np.ones((len(S), 1))])
    target = np.log(np.abs(resid) + 0.05)
    gamma = np.linalg.solve(Ss.T @ Ss + np.diag([RIDGE] * (Ss.shape[1] - 1) + [0.0]), Ss.T @ target)
    scale = float(np.std(resid / np.exp(Ss @ gamma)))
    return dict(beta=beta, mu=mu, sd=sd, nf=nf, gamma=gamma, smu=smu, ssd=ssd, scale=scale)


# Per-station coefficients (user 2026-10-31 「每个站点一套自己的」,
# docs/research/station_own_d0_20261031): each station's own slopes and
# intercept on the band's fleet-standardised features, ridge STATION_LAMBDA
# toward the fleet fit's, and its own spread scale (mean squared standardised
# fleet residual, shrunk with n / (n + STATION_SCALE_K)).  Walk-forward
# 09-17..10-27: LogScore -0.016 [-0.024, -0.009], MAE -0.011 C, both halves;
# lambda picked on the first half held on the second (MAE -0.009 [-0.015, -0.002]).
STATION_LAMBDA = 200.0
STATION_SCALE_K = 30.0


def fit_stations(train, stations, model):
    """station -> {"beta": slopes + intercept on standardised features, "scale", "n"}."""

    nf = model["nf"]
    by = defaultdict(list)
    for r in train:
        by[r["station"]].append(r)
    out = {}
    for s, rows in by.items():
        X = design(rows, stations, True)
        A = np.hstack([(X[:, :nf] - model["mu"][:nf]) / model["sd"][:nf], np.ones((len(rows), 1))])
        y = np.array([r["y"] for r in rows])
        b0 = np.append(model["beta"][:nf], model["beta"][-1] + model["beta"][nf + stations.index(s)])
        beta = np.linalg.solve(A.T @ A + STATION_LAMBDA * np.eye(nf + 1), A.T @ y + STATION_LAMBDA * b0)
        sig = np.array([predict(model, r, stations, True)[1] for r in rows])
        ratio2 = float(np.mean(((y - A @ b0) / sig) ** 2))
        n = len(rows)
        out[s] = {"beta": beta.tolist(), "scale": math.sqrt((n * ratio2 + STATION_SCALE_K) / (n + STATION_SCALE_K)), "n": n}
    return out


def spread_design(rows):
    return np.array([[r["feats"]["hours_to_peak"], max(0.0, r["feats"]["hours_to_peak"]), r["feats"]["fc_rise"],
                      r["feats"]["high_minus_base"], abs(r["feats"]["t_minus_fc"])] for r in rows])


def predict(model, r, stations, use_sat):
    X = design([r], stations, use_sat)[0]
    nf = model["nf"]
    X[:nf] = (X[:nf] - model["mu"][:nf]) / model["sd"][:nf]
    yhat = float(np.append(X, 1.0) @ model["beta"])
    S = (spread_design([r])[0] - model["smu"]) / model["ssd"]
    sigma = float(np.exp(np.append(S, 1.0) @ model["gamma"])) * model["scale"]
    return r["base"] + yhat, max(0.35, min(3.0, sigma))


def vector(r, centre, sigma):
    cdf_hi = norm.cdf((r["hi"] - centre) / sigma); cdf_lo = norm.cdf((r["lo"] - centre) / sigma)
    p = cdf_hi - cdf_lo
    below = norm.cdf((r["high"] - centre) / sigma)
    inside = (r["lo"] <= r["high"] + 1e-9) & (r["high"] < r["hi"] - 1e-9)
    p = np.where(r["hi"] <= r["high"] + 1e-9, 0.0, p)
    p = np.where(inside, below + (cdf_hi - norm.cdf((r["high"] - centre) / sigma)), p)
    p = np.maximum(p, 0); p = p / p.sum(); p = np.maximum(p, FLOOR)
    return p / p.sum()


def rep(r):
    lo, hi = r["lo"], r["hi"]
    w = np.nanmedian([b - a for a, b in zip(lo, hi) if abs(a) < 1e8 and abs(b) < 1e8]) or 1.0
    return np.where(lo < -1e8, hi - w / 2, np.where(hi > 1e8, lo + w / 2, (lo + hi) / 2))


def scores(v, k):
    v = np.maximum(v, FLOOR); v = v / v.sum()
    return -math.log(v[k]), float(((v - np.eye(len(v))[k]) ** 2).sum())


def to_vec(r, mapping):
    v = np.array([float(mapping.get(l, 0.0)) for l in r["labels"]])
    return None if v.sum() <= 0 else v / v.sum()


def pnl_taker(r, b):
    q = r["market"]["quotes"]; win = np.zeros(len(b)); win[r["k"]] = 1
    total = 0.0
    for i, l in enumerate(r["labels"]):
        ask, bid = q.get(l, [None, None])
        if ask and b[i] > ask:
            w = math.log(b[i] / ask); total += w * (win[i] - ask - FEE * ask * (1 - ask))
        if bid and b[i] < bid:
            p = 1 - bid; w = math.log((1 - b[i]) / p); total += w * ((1 - win[i]) - p - FEE * p * (1 - p))
    return total


def pnl_maker(r, b):
    q = r["market"]["quotes"]; win = np.zeros(len(b)); win[r["k"]] = 1
    total = 0.0
    for i, l in enumerate(r["labels"]):
        ask, bid = q.get(l, [None, None])
        if bid is not None and ask is not None and bid + 0.01 < ask and b[i] > bid + 0.01:
            p = bid + 0.01; w = math.log(b[i] / p); total += 0.5 * w * (win[i] - p)
    return total


def boot(by_day, rng):
    days = sorted(by_day)
    point = float(np.mean([v for d in days for v in by_day[d]]))
    bs = [float(np.mean([v for d in (rng.choice(days) for _ in days) for v in by_day[d]])) for _ in range(2000)]
    return point, float(np.percentile(bs, 2.5)), float(np.percentile(bs, 97.5))


def main() -> None:
    data = load()
    stations = sorted({r["station"] for r in data})
    days = sorted({r["day"] for r in data})
    print("rows", len(data), "stations", len(stations), "days", len(days), "with market", sum(r["market"] is not None for r in data))
    arms = {"full": True, "metar_only": False, "base_only": None}
    acc = defaultdict(lambda: defaultdict(list))   # (arm, band, metric) -> day -> values
    for band, (h0, h1) in BANDS.items():
        rows_b = [r for r in data if h0 <= r["hour"] < h1]
        for i, day in enumerate(days):
            if i < MIN_TRAIN_DAYS:
                continue
            train = [r for r in rows_b if r["day"] < day]
            test = [r for r in rows_b if r["day"] == day]
            if not test or len(train) < 200:
                continue
            models = {}
            for arm, use_sat in arms.items():
                if use_sat is None:
                    bias = defaultdict(list)
                    for r in train:
                        bias[r["station"]].append(r["y"])
                    allb = np.mean([r["y"] for r in train]); sig = float(np.std([r["y"] for r in train]))
                    models[arm] = ("base", {s: (np.sum(v) + 3 * allb) / (len(v) + 3) for s, v in bias.items()}, allb, sig)
                else:
                    models[arm] = ("ridge", fit_band(train, stations, use_sat), use_sat)
            per_sd = defaultdict(lambda: defaultdict(list))
            for r in test:
                mk = to_vec(r, r["market"]["q"]) if r["market"] else None
                p1 = to_vec(r, r["p1"]) if r["p1"] else None
                for arm, m in models.items():
                    if m[0] == "base":
                        centre, sigma = r["base"] + m[1].get(r["station"], m[2]), m[3]
                    else:
                        centre, sigma = predict(m[1], r, stations, m[2])
                    v = vector(r, centre, sigma)
                    sd_key = (r["day"], r["station"])
                    lg, br = scores(v, r["k"])
                    per_sd[(arm, "mae")][sd_key].append(abs(centre - r["truth"]))
                    per_sd[(arm, "bias")][sd_key].append(centre - r["truth"])
                    per_sd[(arm, "log")][sd_key].append(lg)
                    if mk is not None:
                        lgm, brm = scores(mk, r["k"])
                        per_sd[(arm, "dlog_mkt")][sd_key].append(lg - lgm)
                        per_sd[(arm, "dbrier_mkt")][sd_key].append(br - brm)
                        per_sd[(arm, "taker")][sd_key].append(pnl_taker(r, v))
                        per_sd[(arm, "maker")][sd_key].append(pnl_maker(r, v))
                    if mk is not None and arm == "full":
                        per_sd[("market", "mae")][sd_key].append(abs(float((mk * rep(r)).sum()) - r["truth"]))
                        per_sd[("market", "mode_hit")][sd_key].append(float(np.argmax(mk) == r["k"]))
                    if arm == "full":
                        per_sd[(arm, "mode_hit")][sd_key].append(float(np.argmax(v) == r["k"]))
                    if p1 is not None:
                        lgp, brp = scores(p1, r["k"])
                        if arm == "full":
                            per_sd[("p1", "mae")][sd_key].append(abs(float((p1 * rep(r)).sum()) - r["truth"]))
                            per_sd[("p1", "mode_hit")][sd_key].append(float(np.argmax(p1) == r["k"]))
                        per_sd[(arm, "dlog_p1")][sd_key].append(lg - lgp)
                        if mk is not None and arm == "full":
                            per_sd[("p1", "dlog_mkt")][sd_key].append(lgp - lgm)
                            per_sd[("p1", "taker")][sd_key].append(pnl_taker(r, p1))
                            per_sd[("p1", "maker")][sd_key].append(pnl_maker(r, p1))
            for (arm, metric), by in per_sd.items():
                for sd_key, vals in by.items():
                    acc[(arm, band, metric)][day].append(float(np.mean(vals)))
        print("band", band, "done", flush=True)
    rng = random.Random(20261015)
    report = {}
    lines = []
    for (arm, band, metric), by_day in sorted(acc.items()):
        point, lo, hi = boot(by_day, rng)
        n = sum(len(v) for v in by_day.values())
        report[f"{arm}|{band}|{metric}"] = dict(point=point, lo=lo, hi=hi, station_days=n, days=len(by_day))
        lines.append(f"{arm:10s} {band:6s} {metric:11s} sd={n:4d} {point:+.4f} [{lo:+.4f},{hi:+.4f}]")
    (DATA / "walk_forward_d0.json").write_text(json.dumps(report, indent=1))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
