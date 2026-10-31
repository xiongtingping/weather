"""Per-station D0 (user 2026-10-31 「所有模型，按照单站点重新优化，重新拟合参数，也就是每个站点一套自己的」, D0 after F, P0, MOS).

The board's D0 card (``docs/research/d0_obs_model_20261015/fit_d0.py``) is one
ridge regression per time band over every station, with a station fixed effect;
every slope and the spread model are the fleet's.  This walks fit_d0's own
daily walk-forward (12 training days first, each day fitted on the days before)
on the production refit's rows and replaces the centre by a per-station
regression: each station's own slopes and intercept, ridge ``lam`` toward the
fleet fit's (lam = 1 is the station nearly alone).  The spread model stays the
fleet's, and a second arm adds each station's own spread scale (its residual
ratio, shrunk with n / (n + 30)).  ``tuned`` picks, per station and band, the
arm with the lowest mean LogScore over its own earlier walk days.

    D0_DATA_DIR=data/runtime/d0_refit .venv/bin/python -I docs/research/station_own_d0_20261031/run.py
"""

from __future__ import annotations

import json
import os
import random
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
os.environ.setdefault("D0_DATA_DIR", str(ROOT / "data" / "runtime" / "d0_refit"))
sys.path.insert(0, str(ROOT / "docs" / "research" / "d0_obs_model_20261015"))
import fit_d0 as D  # noqa: E402

LAMS = (1.0, 10.0, 50.0, 200.0, 500.0, 1000.0, 3000.0)
SCALE_K = 30.0
TUNE_MIN_DAYS = 7


def design_std(rows, stations, model):
    """Fleet-standardised features (no station dummies) plus a constant."""

    X = D.design(rows, stations, True)
    nf = model["nf"]
    Xs = (X[:, :nf] - model["mu"][:nf]) / model["sd"][:nf]
    return np.hstack([Xs, np.ones((len(Xs), 1))])


def fleet_prior(model, stations, s):
    nf = model["nf"]
    b = model["beta"]
    return np.append(b[:nf], b[-1] + b[nf + stations.index(s)])


def sigma_of(model, r):
    S = (D.spread_design([r])[0] - model["smu"]) / model["ssd"]
    return float(np.exp(np.append(S, 1.0) @ model["gamma"])) * model["scale"]


def main() -> None:
    data = D.load()
    stations = sorted({r["station"] for r in data})
    days = sorted({r["day"] for r in data})
    print("rows", len(data), "stations", len(stations), "days", days[0], days[-1], flush=True)
    arms = ["fleet"] + [f"own_lam{int(l)}" for l in LAMS] + [f"own_lam{int(l)}_scale" for l in LAMS]
    # (band, station, day) -> arm -> [logscore, |err|] averaged over the station-day's hours
    rec = defaultdict(lambda: defaultdict(list))
    for band, (h0, h1) in D.BANDS.items():
        rows_b = [r for r in data if h0 <= r["hour"] < h1]
        for i, day in enumerate(days):
            if i < D.MIN_TRAIN_DAYS:
                continue
            train = [r for r in rows_b if r["day"] < day]
            test = [r for r in rows_b if r["day"] == day]
            if not test or len(train) < 200:
                continue
            fleet = D.fit_band(train, stations, True)
            by_st = defaultdict(list)
            for r in train:
                by_st[r["station"]].append(r)
            own: dict[str, dict] = {}
            for s, tr in by_st.items():
                A = design_std(tr, stations, fleet)
                y = np.array([r["y"] for r in tr])
                b0 = fleet_prior(fleet, stations, s)
                sig = np.array([D.predict(fleet, r, stations, True)[1] for r in tr])
                fleet_res = y - A @ b0
                ratio2 = float(np.mean((fleet_res / sig) ** 2))
                n = len(tr)
                own[s] = {"n": n, "beta": {}, "scale": float(np.sqrt((n * ratio2 + SCALE_K) / (n + SCALE_K)))}
                for lam in LAMS:
                    own[s]["beta"][lam] = np.linalg.solve(A.T @ A + lam * np.eye(A.shape[1]), A.T @ y + lam * b0)
            for r in test:
                centre_f, sigma_f = D.predict(fleet, r, stations, True)
                outs = {"fleet": (centre_f, sigma_f)}
                o = own.get(r["station"])
                a = design_std([r], stations, fleet)[0]
                for lam in LAMS:
                    if o is None:
                        outs[f"own_lam{int(lam)}"] = outs[f"own_lam{int(lam)}_scale"] = (centre_f, sigma_f)
                        continue
                    c = r["base"] + float(a @ o["beta"][lam])
                    outs[f"own_lam{int(lam)}"] = (c, sigma_f)
                    outs[f"own_lam{int(lam)}_scale"] = (c, max(0.35, min(3.0, sigma_f * o["scale"])))
                key = (band, r["station"], day)
                for arm, (c, sg) in outs.items():
                    lg, _br = D.scores(D.vector(r, c, sg), r["k"])
                    rec[key][arm].append((lg, abs(c - r["truth"])))
        print("band", band, "done", flush=True)
    rows = []
    for (band, st, day), arms_v in rec.items():
        rows.append({"band": band, "station": st, "day": day,
                     "ls": {a: float(np.mean([x[0] for x in v])) for a, v in arms_v.items()},
                     "mae": {a: float(np.mean([x[1] for x in v])) for a, v in arms_v.items()}})
    # tuned: per station x band, the arm with the lowest mean LogScore over its earlier walk days
    grp = defaultdict(list)
    for r in rows:
        grp[(r["band"], r["station"])].append(r)
    for rs in grp.values():
        rs.sort(key=lambda r: r["day"])
        for j, r in enumerate(rs):
            prior = rs[:j]
            pick = "fleet"
            if len(prior) >= TUNE_MIN_DAYS:
                pick = min(arms, key=lambda a: np.mean([p["ls"][a] for p in prior]))
            r["pick"] = pick
            r["ls"]["tuned"], r["mae"]["tuned"] = r["ls"][pick], r["mae"][pick]
    (HERE / "walk.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    report(rows, arms + ["tuned"])


def boot(rows, a, b, metric, rng):
    per = defaultdict(list)
    for r in rows:
        per[r["day"]].append(r[metric][a] - r[metric][b])
    days = sorted(per)
    point = float(np.mean([v for d in days for v in per[d]]))
    bs = sorted(float(np.mean([v for d in (rng.choice(days) for _ in days) for v in per[d]])) for _ in range(2000))
    mid = days[len(days) // 2]
    halves = [float(np.mean([v for d in days if (d < mid) == h for v in per[d]])) for h in (True, False)]
    return point, bs[50], bs[1949], halves


def report(rows, arms):
    rng = random.Random(20261031)
    out = {}
    for band in list(D.BANDS) + ["all"]:
        sel = [r for r in rows if band == "all" or r["band"] == band]
        days = sorted({r["day"] for r in sel})
        print(f"\n{band}: {len(sel)} station-days, {len(days)} days {days[0]}..{days[-1]}")
        print(f"  {'arm':18s} {'LogScore':>8s} {'MAE':>6s}  ΔLogScore vs fleet [95%] halves | ΔMAE [95%] | stations better")
        out[band] = {}
        for a in arms:
            ls = float(np.mean([r["ls"][a] for r in sel]))
            mae = float(np.mean([r["mae"][a] for r in sel]))
            line = f"  {a:18s} {ls:8.4f} {mae:6.3f}"
            e = {"logscore": ls, "mae": mae}
            if a != "fleet":
                d, lo, hi, h = boot(sel, a, "fleet", "ls", rng)
                dm, mlo, mhi, _h = boot(sel, a, "fleet", "mae", rng)
                st = defaultdict(list)
                for r in sel:
                    st[r["station"]].append(r["ls"][a] - r["ls"]["fleet"])
                better = sum(1 for v in st.values() if np.mean(v) < 0)
                e.update(d_logscore=[d, lo, hi], halves=h, d_mae=[dm, mlo, mhi], stations_better=[better, len(st)])
                line += f"  {d:+.4f} [{lo:+.4f}, {hi:+.4f}] {h[0]:+.4f}/{h[1]:+.4f} | {dm:+.3f} [{mlo:+.3f}, {mhi:+.3f}] | {better}/{len(st)}"
            out[band][a] = e
            print(line)
    picks = defaultdict(int)
    for r in rows:
        picks[r["pick"]] += 1
    print("\ntuned picks:", dict(sorted(picks.items(), key=lambda x: -x[1])))
    (HERE / "reading.json").write_text(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
