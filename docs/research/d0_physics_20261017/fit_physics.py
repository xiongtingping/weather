"""D0 refit on physical evidence (PROTOCOL.md, pre-registered 2026-10-17).

Arms on the D0 rig (d0_obs_model_20261015/rows_d0.jsonl.gz), walk-forward
exactly as fit_d0.py: B (station-bias baseline), A0 (current D0, all
features), P (physics only: cloud, rain, radiation, dew point, wind,
pressure, each also times the share of heating still to come and times the
forecast rise left), A0+P.  Paired against B and A0 per station-day,
event-day block bootstrap, Bonferroni over 4 bands x 2 questions.
"""

from __future__ import annotations

import gzip
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
RIG = HERE.parent / "d0_obs_model_20261015"
sys.path.insert(0, str(RIG))
import fit_d0 as F  # noqa: E402

PHYS = ["sun_pct", "cloud_gap", "cot", "cloud_low", "rain_gap", "rain_obs", "precip_now", "precip_any",
        "dew", "dew_dep", "wind", "pressure"]
NULLABLE = {"sun_pct", "cloud_gap", "cot", "rain_gap"}
Q_BONF = (100 - 5 / 8) / 100      # 99.375%


def attach_raw(data):
    raw = {}
    for line in gzip.open(RIG / "rows_d0.jsonl.gz", "rt"):
        r = json.loads(line)
        raw[(r["station"], r["day"], r["hour"])] = r
    for r in data:
        x = raw[(r["station"], r["day"], r["hour"])]
        h2p = r["feats"]["hours_to_peak"]
        r["share"] = max(0.0, h2p) / (max(0.0, h2p) + 1.0)
        r["phys"] = {k: (None if x.get(k) is None else float(x[k])) for k in PHYS}
        r["has_sat_cloud"] = x.get("cloud_gap") is not None
    return data


def a0_numeric(r):
    v = []
    for c in F.NUMERIC:
        x = r["feats"][c]
        v.append(0.0 if x is None else float(x))
        if c in F.SAT or c == "dew_dep":
            v.append(1.0 if x is None else 0.0)
    return v


def phys_numeric(r):
    v = []
    rise = r["feats"]["fc_rise"] or 0.0
    for k in PHYS:
        x = r["phys"][k]
        val = 0.0 if x is None else x
        v.extend([val, val * r["share"], val * rise])
        if k in NULLABLE:
            v.append(1.0 if x is None else 0.0)
    return v


COLS = {"A0": a0_numeric, "P": phys_numeric, "A0+P": lambda r: a0_numeric(r) + phys_numeric(r)}


def fit(train, stations, colfun):
    num = np.array([colfun(r) for r in train])
    dummies = np.array([[1.0 if r["station"] == s else 0.0 for s in stations] for r in train])
    y = np.array([r["y"] for r in train])
    mu, sd = num.mean(0), num.std(0) + 1e-9
    A = np.hstack([(num - mu) / sd, dummies, np.ones((len(num), 1))])
    nf = num.shape[1]
    pen = np.full(A.shape[1], F.RIDGE); pen[nf:nf + len(stations)] = F.RIDGE * 3; pen[-1] = 0.0
    beta = np.linalg.solve(A.T @ A + np.diag(pen), A.T @ y)
    resid = y - A @ beta
    S = F.spread_design(train)
    smu, ssd = S.mean(0), S.std(0) + 1e-9
    Ss = np.hstack([(S - smu) / ssd, np.ones((len(S), 1))])
    gamma = np.linalg.solve(Ss.T @ Ss + np.diag([F.RIDGE] * (Ss.shape[1] - 1) + [0.0]), Ss.T @ np.log(np.abs(resid) + 0.05))
    scale = float(np.std(resid / np.exp(Ss @ gamma)))
    return dict(beta=beta, mu=mu, sd=sd, gamma=gamma, smu=smu, ssd=ssd, scale=scale, colfun=colfun)


def predict(m, r, stations):
    num = (np.array(m["colfun"](r)) - m["mu"]) / m["sd"]
    x = np.concatenate([num, [1.0 if r["station"] == s else 0.0 for s in stations], [1.0]])
    S = (F.spread_design([r])[0] - m["smu"]) / m["ssd"]
    sigma = float(np.exp(np.append(S, 1.0) @ m["gamma"])) * m["scale"]
    return r["base"] + float(x @ m["beta"]), max(0.35, min(3.0, sigma))


def boot(by_day, rng, q):
    days = sorted(by_day)
    point = float(np.mean([v for d in days for v in by_day[d]]))
    bs = [float(np.mean([v for d in (rng.choice(days) for _ in days) for v in by_day[d]])) for _ in range(2000)]
    return point, float(np.percentile(bs, 100 - 100 * q)), float(np.percentile(bs, 100 * q))


def main() -> None:
    data = attach_raw(F.load())
    stations = sorted({r["station"] for r in data})
    days = sorted({r["day"] for r in data})
    acc = defaultdict(lambda: defaultdict(list))
    coef_report = {}
    for band, (h0, h1) in F.BANDS.items():
        rows_b = [r for r in data if h0 <= r["hour"] < h1]
        for i, day in enumerate(days):
            if i < F.MIN_TRAIN_DAYS:
                continue
            train = [r for r in rows_b if r["day"] < day]
            test = [r for r in rows_b if r["day"] == day]
            if not test or len(train) < 200:
                continue
            models = {arm: fit(train, stations, fn) for arm, fn in COLS.items()}
            bias = defaultdict(list)
            for r in train:
                bias[r["station"]].append(r["y"])
            allb = float(np.mean([r["y"] for r in train])); sig = float(np.std([r["y"] for r in train]))
            bmap = {s: (np.sum(v) + 3 * allb) / (len(v) + 3) for s, v in bias.items()}
            per = defaultdict(lambda: defaultdict(list))
            for r in test:
                out = {"B": (r["base"] + bmap.get(r["station"], allb), sig)}
                for arm, m in models.items():
                    out[arm] = predict(m, r, stations)
                mk = F.to_vec(r, r["market"]["q"]) if r["market"] else None
                key = (r["day"], r["station"])
                for arm, (c, s) in out.items():
                    v = F.vector(r, c, s)
                    lg, _ = F.scores(v, r["k"])
                    per[(arm, "mae", "all")][key].append(abs(c - r["truth"]))
                    per[(arm, "log", "all")][key].append(lg)
                    if r["has_sat_cloud"]:
                        per[(arm, "mae", "satcloud")][key].append(abs(c - r["truth"]))
                        per[(arm, "log", "satcloud")][key].append(lg)
                    if mk is not None:
                        per[(arm, "dlog_mkt", "all")][key].append(lg - F.scores(mk, r["k"])[0])
                        per[(arm, "mae_mkt", "all")][key].append(abs(c - r["truth"]))
                if mk is not None:
                    per[("market", "mae_mkt", "all")][key].append(abs(float((mk * F.rep(r)).sum()) - r["truth"]))
            for (arm, metric, sub), by in per.items():
                for key, vals in by.items():
                    acc[(arm, band, metric, sub)][(day, key[1])] = float(np.mean(vals))
        # coefficients of the physics arm on the whole band (for reading only)
        m = fit(rows_b, stations, COLS["P"])
        names = []
        for k in PHYS:
            names += [k, k + "×剩余份额", k + "×预报剩余升温"] + ([k + "缺失"] if k in NULLABLE else [])
        coef_report[band] = {n: round(float(b / s), 4) for n, b, s in zip(names, m["beta"][:len(names)], m["sd"])}
        print("band", band, "done", flush=True)
    rng = random.Random(20261017)
    report, lines = {}, []

    def paired(a, b, band, metric, sub):
        A, Bm = acc[(a, band, metric, sub)], acc[(b, band, metric, sub)]
        by_day = defaultdict(list)
        for (day, st), v in A.items():
            if (day, st) in Bm:
                by_day[day].append(v - Bm[(day, st)])
        return by_day

    for band in F.BANDS:
        for metric in ("mae", "log"):
            for a, b, q in (("P", "B", "Q1"), ("A0+P", "A0", "Q2"), ("A0", "B", "ref")):
                for sub in ("all", "satcloud"):
                    if q != "Q2" and sub == "satcloud":
                        continue
                    by_day = paired(a, b, band, metric, sub)
                    pt, lo, hi = boot(by_day, rng, Q_BONF)
                    n = sum(len(v) for v in by_day.values())
                    key = f"{q}|{a}-{b}|{band}|{metric}|{sub}"
                    report[key] = dict(point=pt, lo=lo, hi=hi, station_days=n, sig=(hi < 0 or lo > 0))
                    lines.append(f"{key:40s} n={n:5d} {pt:+.4f} [{lo:+.4f},{hi:+.4f}]{' *' if hi < 0 or lo > 0 else ''}")
        for arm in ("B", "A0", "P", "A0+P", "market"):
            vals = acc[(arm, band, "mae_mkt", "all")]
            if vals:
                report[f"mae|{arm}|{band}"] = float(np.mean(list(vals.values())))
        for arm in ("A0", "P", "A0+P"):
            by_day = defaultdict(list)
            for (day, _st), v in acc[(arm, band, "dlog_mkt", "all")].items():
                by_day[day].append(v)
            pt, lo, hi = boot(by_day, rng, 0.975)
            report[f"dlog_mkt|{arm}|{band}"] = dict(point=pt, lo=lo, hi=hi)
            lines.append(f"dlog_mkt {arm:5s} {band} {pt:+.4f} [{lo:+.4f},{hi:+.4f}]")
    report["physics_coefficients_per_sd"] = coef_report
    (HERE / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=1))
    print("\n".join(lines))
    for band in F.BANDS:
        print(band, {a: round(report.get(f"mae|{a}|{band}", float('nan')), 3) for a in ("market", "B", "P", "A0", "A0+P")})


if __name__ == "__main__":
    main()
